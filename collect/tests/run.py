#!/usr/bin/env python3
"""Independent acceptance suite for POC 7 (collect traces for auditability). Black box: drives the live system
only through `infra/vm`, `POST /spawn`, the shim's spawn socket, `supervisor/ctl.py`, `infra/job`, systemctl,
iptables, the gateway as seen from inside a guest, and the contract surface `collect/bin/avos`
(collect/CONTRACT.md). Ground truth is the agents' own logs (guest_agent.py) and the HTTP answers they saw.

Run ON THE HOST from ~/poc:   sudo python3 collect/tests/run.py [--quick] [--only ID]
Locally, no host:             python3 collect/tests/run.py --selftest

One line per check: PASS|FAIL <id> <what> measured=<v> bar=<v>. Exit 0 only if every executed check passed.
Raw data in /tmp/collect-tests/. VMs, iptables rules, scratch stores and stopped services are cleaned up in finally.
"""
import argparse, base64, json, os, pathlib, re, shutil, statistics, subprocess, sys, tempfile, time, traceback
import urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
OUT = pathlib.Path("/tmp/collect-tests")
AVOS = REPO / "collect" / "bin" / "avos"
KEY = "/var/lib/avos/images/vm_key"
IP = {"vm-01": "172.16.1.2", "vm-02": "172.16.2.2"}
GLOG = "/tmp/collect"  # log dir inside a guest
BASE_SERVICES = ["avos-gateway", "avos-registrar"]
QUICK = False

BAR = {"flush_s": 5.0, "m9_first_s": 20.0, "m9_cold_s": 3.0, "m9_warm_s": 1.0, "m9_big_cold_s": 6.0, "m9_big_warm_s": 2.0,
       "lat1_p50_ms": 20, "lat1_p99_ms": 60, "latc_p50_ms": 55, "latc_p99_ms": 130, "rss_mb": 30,
       "claim1_calls": 20000, "n_day": 1_000_000, "n_lat": 1000}

_created = set()
_iptables = []
_killed = set()
_stage_cache = {}


class NotImplementedCLI(Exception):
    pass


# --------------------------------------------------------------------------------------------------- shell / ssh
def run(cmd, timeout=120, cwd=None, env=None, input_=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd or str(REPO), env=e, input=input_)
    return p


def gssh_base(slot):
    return ["ssh", "-q", "-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ControlMaster=auto", "-o", "ControlPath=%s/cm-%%C" % OUT,
            "-o", "ControlPersist=180", "-o", "ConnectTimeout=10", "root@" + IP[slot]]


def gssh(slot, cmd, timeout=60):
    return run(gssh_base(slot) + [cmd], timeout=timeout)


def ctl(*args):
    return run(["python3", str(REPO / "supervisor" / "ctl.py"), *args], timeout=60)


def dump(name, obj):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, indent=1, default=str))


def systemctl(*args, timeout=60):
    return run(["systemctl", *args], timeout=timeout)


def is_active(unit):
    return systemctl("is-active", unit).stdout.strip() == "active"


def ensure_active(units):
    for u in units:
        if not is_active(u):
            systemctl("start", u)
            for _ in range(50):
                if is_active(u):
                    break
                time.sleep(0.1)


# --------------------------------------------------------------------------------------------------- the interface
def avos(*args, env=None, timeout=900):
    if not AVOS.exists():
        raise NotImplementedCLI("collect/bin/avos")
    t0 = time.perf_counter()
    p = run(["python3", str(AVOS), *map(str, args)], timeout=timeout, env=env)
    return {"rc": p.returncode, "out": p.stdout, "err": p.stderr, "secs": time.perf_counter() - t0}


def avos_json(*args, **kw):
    r = avos(*args, **kw)
    try:
        j = json.loads(r["out"])
    except ValueError:
        raise AssertionError("avos %s: rc=%d stdout not JSON: %r stderr=%r" % (args[0], r["rc"], r["out"][:160], r["err"][:160]))
    return j, r


def avos_ndjson(*args, **kw):
    r = avos(*args, **kw)
    if r["rc"] != 0:
        raise AssertionError("avos %s rc=%d stderr=%r" % (" ".join(map(str, args[:3])), r["rc"], r["err"][:200]))
    try:
        return [json.loads(l) for l in r["out"].splitlines() if l.strip()], r
    except ValueError:
        raise AssertionError("avos %s: stdout not NDJSON: %r" % (args[0], r["out"][:160]))


def store_info():
    j, _ = avos_json("store")
    for k in ("root", "files", "retention", "services", "flush_ms"):
        if k not in j:
            raise AssertionError("avos store lacks %r" % k)
    return j


# --------------------------------------------------------------------------------------------------- VM lifecycle
def vm_create(slot):
    p = run(["infra/vm", "create", slot], timeout=180)
    if p.returncode != 0 and "exists" in (p.stdout + p.stderr):
        run(["infra/vm", "destroy", slot], timeout=120)
        p = run(["infra/vm", "create", slot], timeout=180)
    if p.returncode != 0:
        raise RuntimeError("vm create %s failed: %s" % (slot, (p.stdout + p.stderr)[-300:]))
    _created.add(slot)
    vm_id = next((t.split("=", 1)[1] for t in (p.stdout + p.stderr).split() if t.startswith("vm_id=")), None)
    gssh(slot, "true")
    data = base64.b64encode((HERE / "guest_agent.py").read_bytes()).decode()
    gssh(slot, "base64 -d > /tmp/guest_agent.py <<'B64'\n" + data + "\nB64\nmkdir -p -m 777 %s && rm -f %s/*" % (GLOG, GLOG), timeout=30)
    return vm_id


def vm_destroy(slot):
    try:
        run(["infra/vm", "destroy", slot], timeout=180)
    finally:
        _created.discard(slot)


def spawn_top(slot, name, cfg, timeout=20):
    body = {"name": name, "argv": ["/usr/bin/python3", "/tmp/guest_agent.py", json.dumps(cfg)]}
    req = urllib.request.Request("http://%s/spawn" % IP[slot], data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read())
    if "spiffe_id" not in j:
        raise RuntimeError("spawn %s: no spiffe_id in %r" % (name, j))
    return j


def read_logs(slot):
    out = gssh(slot, "for f in %s/*.jsonl; do echo \"@@@$f\"; cat \"$f\"; done 2>/dev/null" % GLOG, timeout=60).stdout
    logs, cur = {}, None
    for line in out.splitlines():
        if line.startswith("@@@"):
            cur = pathlib.Path(line[3:]).stem
            logs[cur] = []
        elif line.strip() and cur is not None:
            try:
                logs[cur].append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return logs


def wait_logs(slot, pred, timeout):
    deadline = time.time() + timeout
    while True:
        logs = read_logs(slot)
        if pred(logs):
            return logs
        if time.time() > deadline:
            return logs
        time.sleep(0.5)


def exited(logs, names):
    return all(any(r.get("k") == "exit" for r in logs.get(n, [])) for n in names)


def guest_rss_mb(slot):
    out = gssh(slot, "ps -o rss=,args= -C python3 | grep /agent.py | head -1 | awk '{print int($1/1024)}'").stdout.strip()
    return int(out) if out.isdigit() else None


def cfg_for(name, steps, timeout=30):
    return {"log": "%s/%s.jsonl" % (GLOG, name), "timeout": timeout, "steps": steps}


def sub(parent_spiffe, name):
    return parent_spiffe + "/sub/" + name


def flush_wait(store):
    time.sleep(max(1.0, min(BAR["flush_s"], store.get("flush_ms", 5000) / 1000.0)) + 0.5)


# =============================================================================================== STAGES
def stage_tree():
    """Done 1-4: one agent (support) with three sub-agents in vm-01, a decoy support agent in vm-02 doing the
    latency calls; one refusal, one halt/resume of B2; then destroy and restart."""
    store = store_info()
    ctx = {"store0": store}
    vm1, vm2 = vm_create("vm-01"), vm_create("vm-02")
    ctx.update(vm1=vm1, vm2=vm2)
    n_lat = 300 if QUICK else BAR["n_lat"]
    b1 = cfg_for("B1", [{"tool": 100}, {"tool": 100}, {"tool": 100}, {"llm": 8}])
    b2 = cfg_for("B2", [{"llm": 8}, {"idle": 8}, {"tool": 48000}, {"llm": 8}])
    b3 = cfg_for("B3", [{"llm": 8}, {"tool": 100}, {"tool": 100}, {"tool": 100}])
    sup = cfg_for("support", [{"child": {"name": "B1", "cfg": b1}}, {"child": {"name": "B2", "cfg": b2}},
                              {"child": {"name": "B3", "cfg": b3}}] + [{"tool": 100}] * 10 + [{"llm": 8}, {"tool": 48000}, {"idle": 30}])
    decoy = cfg_for("decoy", [{"idle": 45}, {"calls": {"tool": 100, "n": n_lat, "every": 0}}])
    r_sup = spawn_top("vm-01", "support", sup)
    r_dec = spawn_top("vm-02", "support", decoy)
    P = {"support": r_sup["spiffe_id"], "B1": sub(r_sup["spiffe_id"], "B1"), "B2": sub(r_sup["spiffe_id"], "B2"),
         "B3": sub(r_sup["spiffe_id"], "B3"), "decoy": r_dec["spiffe_id"], "vm1": lib.TD + "/vm/" + vm1}
    ctx["P"] = P
    logs = wait_logs("vm-01", lambda L: any(r.get("k") == "phase" and r.get("p") == "idle_start" for r in L.get("B2", [])), 120)
    ctx["b2_idle_reached"] = any(r.get("k") == "phase" and r.get("p") == "idle_start" for r in logs.get("B2", []))
    ctx["rss_mb"] = guest_rss_mb("vm-01")
    time.sleep(0.5)
    h = ctl("halt", P["B2"])
    time.sleep(1.5)
    r = ctl("resume", P["B2"])
    mh, mr = lib.RECEIPT.search(h.stdout + h.stderr), lib.RECEIPT.search(r.stdout + r.stderr)
    ctx["controls"] = [("halt", mh.group(0) if mh else None), ("resume", mr.group(0) if mr else None)]
    ctx["ctl_rc"] = (h.returncode, r.returncode)
    logs1 = wait_logs("vm-01", lambda L: exited(L, ["support", "B1", "B2", "B3"]), 150)
    logs2 = wait_logs("vm-02", lambda L: exited(L, ["decoy"]), 120)
    ctx["logs"] = {**{k: v for k, v in logs1.items()}, "decoy": logs2.get("decoy", [])}
    ctx["gt"] = {n: lib.ground_truth(ctx["logs"].get(n, [])) for n in ("support", "B1", "B2", "B3", "decoy")}
    flush_wait(store)
    outs, raw = {}, {}
    for n, p in P.items():
        outs[n], rr = avos_ndjson("trace", "--principal", p)
        raw[n] = rr["out"]
    ctx["outs"], ctx["raw_b2"] = outs, raw["B2"]
    ctx["run_out"], _ = avos_ndjson("trace", "--run", vm1)
    ctx["comp"], _ = avos_json("completeness", "--run", vm1)
    ctx["store1"] = store_info()
    ctx["snap1"] = lib.snapshot(ctx["store1"]["files"])
    dump("tree_outputs.json", {"P": P, "controls": ctx["controls"], "comp": ctx["comp"], "store": ctx["store1"],
                               "counts": {n: lib.count_kinds(o) for n, o in outs.items()}, "gt": ctx["gt"]})
    # Done 4: destroy, query again; restart services, query again
    vm_destroy("vm-01"); vm_destroy("vm-02")
    flush_wait(store)
    _, rr = avos_ndjson("trace", "--principal", P["B2"])
    ctx["raw_b2_destroyed"] = rr["out"]
    ctx["run_out_destroyed"], _ = avos_ndjson("trace", "--run", vm1)
    ctx["comp_destroyed"], _ = avos_json("completeness", "--run", vm1)
    units = BASE_SERVICES + list(store.get("services") or [])
    for u in units:
        systemctl("restart", u)
    ensure_active(units)
    time.sleep(1.0)
    flush_wait(store)
    _, rr = avos_ndjson("trace", "--principal", P["B2"])
    ctx["raw_b2_restarted"] = rr["out"]
    ctx["units"] = units
    ctx["active_after"] = {u: is_active(u) for u in units}
    ctx["store2"] = store_info()
    ctx["prefix_broken"] = lib.prefix_broken(ctx["snap1"])
    return ctx


def stage_tau2():
    """A real tau2 job through infra/job; the harness transcript is the independent witness."""
    store = store_info()
    task = json.load(open(REPO / "data/splits/retail_user_disjoint_v1.json"))["train"][0]
    p = run(["infra/job", "run", "tau2", "retail/%s" % task], timeout=900)
    line = next((l for l in reversed(p.stdout.splitlines()) if l.startswith("{")), None)
    if not line:
        raise RuntimeError("infra/job printed no JSON line: rc=%d %s" % (p.returncode, (p.stdout + p.stderr)[-300:]))
    job = json.loads(line)
    ctx = {"job": job}
    rd = pathlib.Path("/var/lib/avos/results") / job["run"]
    jj = json.load(open(rd / "job.json")) if (rd / "job.json").exists() else {}
    ctx["vm_id"] = job.get("vm_id") or jj.get("vm_id")
    ctx["agent"] = jj.get("agent_spiffe_id") or (lib.TD + "/vm/%s/agent/tau2" % ctx["vm_id"])
    ctx["witness_llm"] = None
    if (rd / "result.json").exists():
        ctx["witness_llm"] = witness_llm_count(rd / "result.json")
    flush_wait(store)
    ctx["by_run"], r1 = avos_ndjson("trace", "--run", job["run"])
    ctx["by_vm"], r2 = avos_ndjson("trace", "--run", ctx["vm_id"]) if ctx["vm_id"] else ([], {"out": ""})
    ctx["same"] = r1["out"] == r2["out"]
    ctx["agent_out"], _ = avos_ndjson("trace", "--principal", ctx["agent"])
    ctx["comp"], _ = avos_json("completeness", "--run", job["run"])
    dump("tau2.json", {k: v for k, v in ctx.items() if k not in ("by_run", "by_vm", "agent_out")})
    return ctx


def witness_llm_count(result_path):
    """A tau2 result.json is a SimulationResults file: the run is simulations[0]; one model call per message with usage."""
    j = json.load(open(result_path))
    sim = j["simulations"][0] if isinstance(j, dict) and j.get("simulations") else j
    return sum(1 for m in sim.get("messages", []) if isinstance(m, dict) and m.get("usage"))


def inject(kind, slot):
    """Returns a description; raises on a misconfigured injection."""
    if kind in ("gateway", "registrar") or kind.startswith("svc:"):
        unit = {"gateway": "avos-gateway", "registrar": "avos-registrar"}.get(kind, kind[4:])
        _killed.add(unit)
        p = systemctl("kill", "-s", "KILL", unit)
        return "systemctl kill -s KILL %s rc=%d" % (unit, p.returncode)
    if kind == "path":
        rule = ["-i", "tap-" + slot, "-p", "tcp", "--dport", "8443", "-j", "DROP"]
        p = run(["iptables", "-I", "INPUT", "1", *rule], timeout=30)
        if p.returncode != 0:
            raise RuntimeError("iptables insert failed: " + p.stderr[:200])
        _iptables.append(rule)
        time.sleep(4.0)
        run(["iptables", "-D", "INPUT", *rule], timeout=30)
        _iptables.remove(rule)
        return "guest->gateway path dropped 4 s"
    if kind == "guest":
        p = gssh(slot, "systemctl kill -s KILL avos-agent; sleep 0.3; systemctl is-active avos-agent || systemctl start avos-agent; systemctl is-active avos-agent")
        return "guest avos-agent killed; now " + p.stdout.strip()
    raise ValueError(kind)


def stage_loss(kind):
    """Done 5: callers hammer the gateway; one component is killed (or the path cut) mid-run; two short-lived
    agents are spawned during the outage. Afterwards every lost event must be reported by M8."""
    store = store_info()
    slot = "vm-01"
    vm1 = vm_create(slot)
    stop = GLOG + "/stop"
    gssh(slot, "rm -f " + stop)
    r_s = spawn_top(slot, "support", cfg_for("support", [{"loop": {"tool": 100, "every": 0.02, "stop": stop}}], timeout=2))
    r_o = spawn_top(slot, "other", cfg_for("other", [{"loop": {"tool": 48000, "every": 0.02, "stop": stop}}], timeout=2))
    P = {"support": r_s["spiffe_id"], "other": r_o["spiffe_id"]}
    wait_logs(slot, lambda L: all(sum(1 for r in L.get(n, []) if r.get("k") == "call") >= 10 for n in ("support", "other")), 60)
    t_inj = time.time()
    desc = inject(kind, slot)
    time.sleep(0.3)
    for nm in ("s1", "s2"):
        try:
            rj = spawn_top(slot, nm, cfg_for(nm, [{"tool": 100}], timeout=2), timeout=8)
            P[nm] = rj["spiffe_id"]
        except Exception as e:
            P[nm] = None
            desc += "; spawn %s failed (%s)" % (nm, type(e).__name__)
    time.sleep(2.0)
    ensure_active(BASE_SERVICES + list(store.get("services") or []))
    time.sleep(4.0)
    gssh(slot, "touch " + stop)
    if kind != "guest":
        wait_logs(slot, lambda L: exited(L, ["support", "other"]), 30)
    logs = read_logs(slot)
    vm_destroy(slot)
    flush_wait(store)
    gt = {n: lib.ground_truth(logs.get(n, [])) for n in P if P[n]}
    outs = {n: avos_ndjson("trace", "--principal", P[n])[0] for n in P if P[n]}
    comp, _ = avos_json("completeness", "--run", vm1)
    lost = {n: lib.lost_events(gt[n], outs[n]) for n in gt}
    ctx = {"kind": kind, "desc": desc, "vm1": vm1, "P": P, "comp": comp, "lost": lost, "t_inj": t_inj,
           "attempts": sum(g["attempts"] for g in gt.values()), "exc": sum(g["exc"] for g in gt.values()),
           "stored": {n: lib.count_kinds(o) for n, o in outs.items()}, "gt": gt}
    dump("loss_%s.json" % kind.replace(":", "_"), ctx)
    return ctx


def drop_caches():
    run(["sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"], timeout=60)


def stage_scale():
    """M9: one day of >= 1M events imported into a scratch store root; cold/warm query for small/median/largest."""
    store_info()  # raises NotImplementedCLI before any generation work
    root = OUT / "store"
    day_path = OUT / "day.jsonl"
    try:
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        env = {"AVOS_TRACE_ROOT": str(root)}
        n = 100_000 if QUICK else BAR["n_day"]
        t0 = time.perf_counter()
        gen = lib.gen_day(str(day_path), n)
        picks = lib.pick_principals(gen["counts"])
        want = lib.audit_oracle_stream(str(day_path), list(picks.values()))
        ctx = {"n": gen["n"], "gen_s": time.perf_counter() - t0, "picks": picks, "sizes": {k: len(want[p]) for k, p in picks.items()},
               "day": gen["day"], "env": env, "root": str(root)}
        imp, r = avos_json("import", str(day_path), env=env, timeout=1800)
        ctx["import"] = {"rc": r["rc"], "secs": r["secs"], "imported": imp.get("imported")}
        first_p = picks["median"]
        got, r = avos_ndjson("trace", "--principal", first_p, "--day", gen["day"], env=env)
        ctx["first"] = {"secs": r["secs"], "problem": lib.compare_audit(want[first_p], got)}
        ctx["q"] = {}
        for name in ("small", "median", "largest"):
            p = picks[name]
            drop_caches()
            got_c, rc_ = avos_ndjson("trace", "--principal", p, "--day", gen["day"], env=env)
            got_w, rw_ = avos_ndjson("trace", "--principal", p, "--day", gen["day"], env=env)
            ctx["q"][name] = {"cold_s": rc_["secs"], "warm_s": rw_["secs"], "events": len(got_w),
                              "problem": lib.compare_audit(want[p], got_c) or lib.compare_audit(want[p], got_w)}
        ctx["comp"], _ = avos_json("completeness", "--run", gen["vm_ids"][3], env=env)
        st, _ = avos_json("store", env=env)
        ctx["store"] = st
        ctx["valid"] = validate_files(st["files"], "scale")
        dump("scale.json", ctx)
        return ctx
    finally:
        day_path.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)


def _import_day(root, day_path):
    """Scratch root + import; returns (env, store listing)."""
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    env = {"AVOS_TRACE_ROOT": str(root)}
    imp, r = avos_json("import", str(day_path), env=env, timeout=1800)
    if r["rc"] != 0:
        raise AssertionError("import into %s rc=%d %s" % (root, r["rc"], r["err"][:160]))
    st, _ = avos_json("store", env=env)
    return env, st


def _q(p, env, day=None):
    """One query; returns (rows or None if it refused with an explicit error, raw result)."""
    r = avos("trace", "--principal", p, *(["--day", day] if day else []), env=env)
    if r["rc"] != 0:
        return None, r
    try:
        return [json.loads(l) for l in r["out"].splitlines() if l.strip()], r
    except ValueError:
        return [], r


def _judge(want, rows, r):
    """Correct answer, or an explicit refusal (non-zero exit with a message), passes; wrong or empty fails."""
    if rows is None:
        return "refused rc=%d" % r["rc"] if r["err"].strip() else "rc=%d with no message" % r["rc"]
    prob = lib.compare_audit(want, rows)
    return "ok" if prob is None else ("EMPTY answer" if not rows else prob)


def stage_index():
    """Stale cache probes: an answer must always come from the root's current content."""
    store_info()
    n = 20_000 if QUICK else 50_000
    A, B, A2 = OUT / "idx-A.jsonl", OUT / "idx-B.jsonl", OUT / "idx-A2.jsonl"
    rootR, rootS = OUT / "idx-root-R", OUT / "idx-root-S"
    try:
        ga = lib.gen_day(str(A), n, day="2026-09-01", seed=21, host="hday")
        gb = lib.gen_day(str(B), n, day="2026-09-02", seed=22, host="hdayb")
        lib.mutate_day(str(A), str(A2))
        probe, big = ga["smallest"], ga["largest"]  # fixed names: present in both days with different content
        assert gb["smallest"] == probe and gb["largest"] == big
        wa = lib.audit_oracle_stream(str(A), [probe, big])
        wb = lib.audit_oracle_stream(str(B), [probe, big])
        wa2 = lib.audit_oracle_stream(str(A2), [probe, big])
        wab = lib.audit_oracle_stream([str(A), str(B)], [probe, big])
        wab_dayA = lib.audit_oracle_stream([str(A), str(B)], [probe, big], day="2026-09-01")
        ctx = {"n": n, "sizes": {"A": A.stat().st_size, "A2": A2.stat().st_size, "B": B.stat().st_size}}
        # --- I1: two roots, queried in turn
        envR, stR = _import_day(rootR, A)
        envS, stS = _import_day(rootS, B)
        seq = [("R", probe, envR, wa), ("S", probe, envS, wb), ("R", probe, envR, wa), ("S", big, envS, wb), ("R", big, envR, wa), ("S", probe, envS, wb)]
        ctx["two_roots"] = [(tag, p.rsplit("/", 1)[1], _judge(w[p], *_q(p, env))) for tag, p, env, w in seq]
        # --- I3: append a second day to root R after a warm query
        _q(probe, envR); _q(big, envR)
        imp, r = avos_json("import", str(B), env=envR, timeout=1800)
        ctx["append_rc"] = r["rc"]
        ctx["append"] = {"probe_all": _judge(wab[probe], *_q(probe, envR)), "big_all": _judge(wab[big], *_q(big, envR)),
                         "big_dayA": _judge(wab_dayA[big], *_q(big, envR, "2026-09-01")),
                         "big_dayB": _judge(lib.audit_oracle_stream([str(A), str(B)], [big], day="2026-09-02")[big], *_q(big, envR, "2026-09-02"))}
        # --- I2: same root path, re-created with a different day of identical byte size, files given the old mtimes
        envR, stR = _import_day(rootR, A)
        _q(probe, envR); _q(big, envR)
        before = {f: (os.stat(f).st_size, os.stat(f).st_mtime_ns) for f in stR["files"]}
        envR, stR2 = _import_day(rootR, A2)
        after = {f: os.stat(f).st_size for f in stR2["files"]}
        same_layout = sorted(before) == sorted(after) and all(before[f][0] == after[f] for f in after)
        if same_layout:
            for f, (sz, mt) in before.items():
                os.utime(f, ns=(mt, mt))
        ctx["same_path"] = {"same_layout_and_size": same_layout, "files": len(after),
                            "probe": _judge(wa2[probe], *_q(probe, envR)), "big": _judge(wa2[big], *_q(big, envR))}
        dump("index.json", ctx)
        return ctx
    finally:
        for x in (A, B, A2):
            x.unlink(missing_ok=True)
        for d in (rootR, rootS):
            shutil.rmtree(d, ignore_errors=True)


def validate_files(files, tag):
    """Concatenate the store's files in order and run the POC 6 validator and verifier as black boxes."""
    cat = OUT / ("store-%s.jsonl" % tag)
    n = 0
    with open(cat, "wb") as w:
        for f in files:
            with open(f, "rb") as r:
                for line in r:
                    if line.strip():
                        w.write(line if line.endswith(b"\n") else line + b"\n")
                        n += 1
    res = {"events": n}
    for tool in ("validate", "verify"):
        t0 = time.perf_counter()
        p = run(["python3", str(REPO / "trace" / "bin" / tool), str(cat)], timeout=1800)
        try:
            j = json.loads(p.stdout)
        except ValueError:
            j = {}
        res[tool] = {"rc": p.returncode, "ok": j.get("ok"), "errors": (j.get("errors") or [])[:3], "secs": time.perf_counter() - t0}
    cat.unlink(missing_ok=True)
    return res


def stage_store():
    st = store_info()
    return {"store": st, "valid": validate_files(st["files"], "live")}


def stage_claim1():
    p = run(["just", "claim1"] + (["--quick"] if QUICK else []), timeout=2400)
    (OUT / "claim1.txt").write_text(p.stdout + p.stderr)
    lines = lib.parse_check_lines(p.stdout)
    gw = lib.parse_claim1_gateway(lines.get("7", (False, ""))[1])
    return {"rc": p.returncode, "lines": lines, "gw": gw}


def stage_poc4():
    p = run(["just", "poc4"], timeout=1200)
    (OUT / "poc4.txt").write_text(p.stdout + p.stderr)
    return {"rc": p.returncode, "fail": "FAIL" in p.stdout, "m5": re.search(r"M5[^\n]*", p.stdout).group(0) if re.search(r"M5[^\n]*", p.stdout) else ""}


def stage_trace():
    p = run(["python3", "trace/tests/run.py"] + (["--quick"] if QUICK else []), timeout=1800)
    (OUT / "trace-suite.txt").write_text(p.stdout + p.stderr)
    lines = lib.parse_check_lines(p.stdout)
    return {"rc": p.returncode, "passed": sum(1 for ok, _ in lines.values() if ok), "total": len(lines)}


# =============================================================================================== CHECKS
def d1(c):
    gt, P = c["gt"], c["P"]
    exc = sum(g["exc"] for g in gt.values())
    facts = {"all_exited": all(gt[n]["exited"] for n in ("support", "B1", "B2", "B3", "decoy")),
             "b2_idle": c["b2_idle_reached"], "receipts": all(r for _, r in c["controls"]), "ctl_rc": c["ctl_rc"] == (0, 0),
             "support_allowed_refunds": gt["support"]["tool_ok"] >= 1, "b2_model_calls": sum(1 for r in gt["B2"]["calls"] if r["kind"] == "llm") >= 2,
             "b2_refusal": gt["B2"]["denied"] >= 1, "tree_llm_ok": sum(g["llm_ok"] for g in gt.values()) >= 1, "unanswered_0": exc == 0}
    ok = all(facts.values())
    return ok, "tree of support + B1,B2,B3 (+ decoy in vm-02) did real gateway work, one refusal, one halt/resume of B2", \
        "calls=%d unanswered=%d support_ok=%d b2_denied=%d receipts=%s %s" % (
            sum(g["attempts"] for g in gt.values()), exc, gt["support"]["tool_ok"], gt["B2"]["denied"], [r for _, r in c["controls"]],
            "" if ok else {k: v for k, v in facts.items() if not v}), "every fact true"


def d2_b2(c):
    probs = lib.check_timeline(c["outs"]["B2"], c["P"]["B2"], c["logs"].get("B2", []), c["controls"])
    return not probs, "avos trace --principal B2 == B2's own log: spawn, calls, the refusal (policy id), halt (receipt), resume, exit; nothing else", \
        "events=%d problems=%s" % (len(c["outs"]["B2"]), probs[:2]), "0 problems"


def d2_siblings(c):
    probs = []
    for n in ("support", "B1", "B3"):
        probs += ["%s: %s" % (n, p) for p in lib.check_timeline(c["outs"][n], c["P"][n], c["logs"].get(n, []), [])]
    probs += lib.check_disjoint({n: c["outs"][n] for n in ("support", "B1", "B2", "B3", "decoy")})
    probs += ["decoy event in vm-01" for e in c["outs"]["decoy"] if e.get("vm_id") != c["vm2"]]
    return not probs, "siblings' and parent's timelines are exact too; the five answers share no event", \
        "problems=%s" % probs[:2], "0 problems"


def d2_run(c):
    ro, vm1 = c["run_out"], c["vm1"]
    ids = {e["event_id"] for e in ro}
    union = {e["event_id"] for n in ("support", "B1", "B2", "B3") for e in c["outs"][n]}
    probs = []
    if not lib.is_sorted(ro):
        probs.append("not sorted")
    foreign = [e for e in ro if e.get("vm_id") != vm1]
    if foreign:
        probs.append("%d events of another VM" % len(foreign))
    if not union <= ids:
        probs.append("%d principal events missing from --run" % len(union - ids))
    if not any(e["kind"] == "vm.boot" and e["principal"] == c["P"]["vm1"] for e in ro):
        probs.append("no vm.boot")
    return not probs, "avos trace --run <vm_id> is the VM's whole life and nothing from vm-02", \
        "events=%d problems=%s" % (len(ro), probs), "0 problems"


def mine_run(c):
    m = {k: 0 for k in lib.KINDS7}
    for n in ("support", "B1", "B2", "B3"):
        for k in ("authz.decision", "tool.call", "llm.call"):
            m[k] += c["gt"][n]["mine"][k]
        m["agent.spawn"] += 1
        m["agent.exit"] += 1 if c["gt"][n]["exited"] else 0
    vm_self = lib.count_kinds(c["outs"]["vm1"])
    for k in ("authz.decision", "tool.call", "llm.call"):
        m[k] += vm_self.get(k, 0)
    m["halt"], m["resume"] = 1, 1
    return m


def d3_counts(c):
    probs = []
    for n in ("support", "B1", "B2", "B3"):
        got, want = lib.count_kinds(c["outs"][n]), dict(c["gt"][n]["mine"], **{"agent.spawn": 1})
        if n == "B2":
            want.update(halt=1, resume=1)
        for k, v in want.items():
            if got.get(k, 0) != v:
                probs.append("%s %s stored=%d observed=%d" % (n, k, got.get(k, 0), v))
    m = mine_run(c)
    st = c["comp"].get("stored") or {}
    for k in lib.KINDS7:
        if st.get(k) != m[k]:
            probs.append("run %s stored=%s observed=%d" % (k, st.get(k), m[k]))
    return not probs, "per principal and per run: stored tool/model/decision/spawn/exit/halt counts == what the agents saw", \
        "run_observed=%s problems=%s" % (m, probs[:3]), "all equal"


def d3_m8(c):
    lost = sum(lib.lost_events(c["gt"][n], c["outs"][n]) for n in ("support", "B1", "B2", "B3"))
    probs = lib.check_m8(c["comp"], lost, clean=True, mine=mine_run(c))
    return not probs, "M8 on the clean tree run: status complete, ratio exactly 1.0, happened from live counters == stored == observed", \
        "status=%s ratio=%r lost=%d problems=%s" % (c["comp"].get("status"), c["comp"].get("ratio"), lost, probs[:2]), "complete, 1.0"


def d3_tau2(c):
    job, probs = c["job"], []
    if job.get("status") != "done":
        probs.append("job status %s %s" % (job.get("status"), job.get("error", "")))
    kinds = lib.count_kinds(c["by_run"])
    for k in ("session.open", "session.close", "agent.spawn", "llm.call", "authz.decision"):
        if not kinds.get(k):
            probs.append("no %s" % k)
    if not c["same"]:
        probs.append("--run run_id != --run vm_id")
    if not lib.is_sorted(c["by_run"]):
        probs.append("not sorted")
    stored_llm = lib.count_kinds(c["agent_out"]).get("llm.call", 0)
    if c["witness_llm"] is not None and stored_llm != c["witness_llm"]:
        probs.append("llm.call stored=%d witness=%d" % (stored_llm, c["witness_llm"]))
    probs += lib.check_m8(c["comp"], 0, clean=True)
    return not probs, "a real tau2 job: session, agent, every model call the transcript witnesses, M8 complete", \
        "run=%s status=%s llm_stored=%d witness=%s m8=%s problems=%s" % (
            job.get("run"), job.get("status"), stored_llm, c["witness_llm"], c["comp"].get("status"), probs[:2]), "0 problems"


def d4_destroy(c):
    probs = []
    if c["raw_b2_destroyed"] != c["raw_b2"]:
        probs.append("B2 answer changed after destroy")
    if not any(e["kind"] == "vm.exit" for e in c["run_out_destroyed"]):
        probs.append("no vm.exit after destroy")
    probs += lib.check_m8(c["comp_destroyed"], 0, clean=True)
    return not probs, "destroy the VM: B2's answer byte-identical, vm.exit flushed, M8 still complete", \
        "problems=%s" % probs, "identical"


def d4_restart(c):
    probs = []
    if c["raw_b2_restarted"] != c["raw_b2"]:
        probs.append("B2 answer changed after restart")
    probs += ["%s not active" % u for u, a in c["active_after"].items() if not a]
    if c["prefix_broken"]:
        probs.append("store files rewritten: %s" % c["prefix_broken"][:2])
    if not set(c["store1"]["files"]) <= set(c["store2"]["files"]):
        probs.append("a store file vanished")
    ret = c["store2"].get("retention") or {}
    if ret.get("keep_days") is not None and ret["keep_days"] < 30:
        probs.append("retention < 30 days")
    return not probs, "restart %s: answer byte-identical, store append-only (earlier bytes a prefix), retention declared" % ",".join(c["units"]), \
        "retention=%s problems=%s" % (ret, probs), "identical, append-only"


def d5(kind):
    def f(c):
        lost = sum(c["lost"].values())
        probs = lib.check_m8(c["comp"], lost, clean=False)
        if kind == "path" and c["exc"] == 0:
            probs.append("injection ineffective: no agent call failed")
        return not probs, "%s mid-run: every event the agents saw but the store lacks is reported by M8 (never 'complete')" % c["desc"].split(";")[0], \
            "lost=%d status=%s ratio=%r attempts=%d unanswered=%d problems=%s" % (
                lost, c["comp"].get("status"), c["comp"].get("ratio"), c["attempts"], c["exc"], probs[:2]), "lost>0 => not complete"
    return f


def m9_import(c):
    probs = []
    if c["import"]["rc"] != 0 or c["import"]["imported"] != c["n"]:
        probs.append("import rc=%s imported=%s of %d" % (c["import"]["rc"], c["import"]["imported"], c["n"]))
    if c["first"]["problem"] or c["first"]["secs"] > BAR["m9_first_s"]:
        probs.append("first query %.1fs problem=%s" % (c["first"]["secs"], c["first"]["problem"]))
    v = c["valid"]
    if not (v["validate"]["rc"] == 0 and v["verify"]["rc"] == 0):
        probs.append("store validate=%s verify=%s" % (v["validate"]["errors"][:1] or v["validate"]["rc"], v["verify"]["errors"][:1] or v["verify"]["rc"]))
    return not probs, "one day of %d events imported; first query (index build allowed) correct; store validates and verifies" % c["n"], \
        "import=%.0fs first=%.2fs sizes=%s validate=%.0fs problems=%s" % (c["import"]["secs"], c["first"]["secs"], c["sizes"], v["validate"]["secs"], probs[:2]), \
        "first<=%.0fs, rc 0" % BAR["m9_first_s"]


def m9(name):
    def f(c):
        q = c["q"][name]
        cold_bar = BAR["m9_big_cold_s"] if name == "largest" else BAR["m9_cold_s"]
        warm_bar = BAR["m9_big_warm_s"] if name == "largest" else BAR["m9_warm_s"]
        ok = q["problem"] is None and q["cold_s"] <= cold_bar and q["warm_s"] <= warm_bar
        return ok, "M9 %s principal (%d events) over the day, cold (caches dropped) and warm, answer == oracle" % (name, q["events"]), \
            "cold=%.2fs warm=%.2fs correct=%s" % (q["cold_s"], q["warm_s"], q["problem"] is None), "cold<=%.0fs warm<=%.0fs" % (cold_bar, warm_bar)
    return f


def m8_imported(c):
    probs = lib.check_m8(c["comp"], 0, clean=False)
    if c["comp"].get("status") != "unverified":
        probs.append("imported run reads %s" % c["comp"].get("status"))
    return not probs, "an imported run has no live counters: M8 must read unverified, ratio null", \
        "status=%s ratio=%r problems=%s" % (c["comp"].get("status"), c["comp"].get("ratio"), probs[:2]), "unverified"


def _idx_ok(v):
    return v == "ok" or v.startswith("refused")


def i1(c):
    bad = [x for x in c["two_roots"] if not _idx_ok(x[2])]
    return not bad, "two scratch roots (same principal names, different days) queried in turn: each answer is its own root's rows", \
        "%s" % [x[2] for x in c["two_roots"]], "6/6 correct (or explicit error)"


def i2(c):
    sp = c["same_path"]
    ok = _idx_ok(sp["probe"]) and _idx_ok(sp["big"])
    return ok, "same root path re-imported with a different day of identical byte size%s: answers are the new rows (hash-checked)" % \
        (" and identical mtimes" if sp["same_layout_and_size"] else " (layout differed: mtimes not equalised)"), \
        "probe=%s big=%s sizes=%s" % (sp["probe"], sp["big"], c["sizes"]), "new content (or explicit error)"


def i3(c):
    ap = c["append"]
    ok = c["append_rc"] == 0 and all(_idx_ok(v) for v in ap.values())
    return ok, "a second day imported into a root after warm queries: answers include it; --day splits it", \
        "import_rc=%d %s" % (c["append_rc"], ap), "all correct (or explicit error)"


def s1(c):
    v = c["valid"]
    ok = v["validate"]["rc"] == 0 and v["verify"]["rc"] == 0 and v["events"] >= 1
    return ok, "live store (%d files, %s): POC 6 validate and verify pass on the concatenation" % (len(c["store"]["files"]), c["store"]["root"]), \
        "events=%d validate=%s verify=%s (%.0fs)" % (v["events"], v["validate"]["ok"], v["verify"]["ok"], v["validate"]["secs"] + v["verify"]["secs"]), "both exit 0"


def o1(c):
    lat = c["gt"]["decoy"]["latency_ms"]
    n_lat = 300 if QUICK else BAR["n_lat"]
    p50, p99 = lib.percentile(lat, 0.5), lib.percentile(lat, 0.99)
    ok = len(lat) >= n_lat and c["gt"]["decoy"]["exc"] == 0 and p50 <= BAR["lat1_p50_ms"] and p99 <= BAR["lat1_p99_ms"]
    return ok, "gateway tool-call latency, one agent, %d back-to-back calls, collection on (POC 4: median 15 ms, worst 30 ms)" % n_lat, \
        "n=%d p50=%.0fms p99=%.0fms" % (len(lat), p50 or -1, p99 or -1), "p50<=%d p99<=%d" % (BAR["lat1_p50_ms"], BAR["lat1_p99_ms"])


def o2(c):
    gw = c["gw"]
    ok = bool(gw) and gw["p50"] <= BAR["latc_p50_ms"] and gw["p99"] <= BAR["latc_p99_ms"]
    return ok, "gateway latency under 8 concurrent agents from the claim 1 suite (recorded p50 41 ms, p99 84 ms)", \
        "%s" % (gw,), "p50<=%d p99<=%d" % (BAR["latc_p50_ms"], BAR["latc_p99_ms"])


def o3(c):
    r = c["rss_mb"]
    return r is not None and r <= BAR["rss_mb"], "guest agent (shim + forwarder) RSS with collection on (recorded 25 MB)", "%s MB" % r, "<=%d MB" % BAR["rss_mb"]


def r1(c):
    need = 8
    passed = sum(1 for ok, _ in c["lines"].values() if ok)
    calls_bar = 3200 if QUICK else BAR["claim1_calls"]
    gw = c["gw"] or {}
    ok = c["rc"] == 0 and passed == need and len(c["lines"]) == need and gw.get("unanswered") == 0 and gw.get("calls", 0) >= calls_bar
    return ok, "just claim1%s: 8/8 incl. gateway reliability" % (" --quick" if QUICK else ""), \
        "passed=%d/%d calls=%s unanswered=%s p50=%s p99=%s" % (passed, len(c["lines"]), gw.get("calls"), gw.get("unanswered"), gw.get("p50"), gw.get("p99")), \
        "8/8, calls>=%d, unanswered=0" % calls_bar


def r2(c):
    return c["rc"] == 0 and not c["fail"], "just poc4 still passes", "rc=%d fail_seen=%s %s" % (c["rc"], c["fail"], c["m5"][:60]), "rc 0, no FAIL"


def r3(c):
    return c["rc"] == 0 and c["passed"] == c["total"] and c["total"] > 0, "POC 6 suite python3 trace/tests/run.py%s" % (" --quick" if QUICK else ""), \
        "%d/%d rc=%d" % (c["passed"], c["total"], c["rc"]), "all pass"


def check_list(services):
    L = [("D1_tree", "tree", d1), ("D2_b2_timeline", "tree", d2_b2), ("D2_siblings", "tree", d2_siblings), ("D2_run", "tree", d2_run),
         ("D3_counts", "tree", d3_counts), ("D3_m8_clean", "tree", d3_m8), ("D3_tau2", "tau2", d3_tau2),
         ("D4_destroy", "tree", d4_destroy), ("D4_restart", "tree", d4_restart)]
    for kind in ["gateway", "registrar"] + ["svc:" + s for s in services] + ["path", "guest"]:
        L.append(("D5_" + kind.replace("svc:", "kill_").replace(":", "_"), "loss:" + kind, d5(kind)))
    L += [("S1_store_valid", "store", s1), ("M9_import", "scale", m9_import), ("M9_small", "scale", m9("small")),
          ("M9_median", "scale", m9("median")), ("M9_largest", "scale", m9("largest")), ("M8_imported", "scale", m8_imported),
          ("I1_two_roots", "index", i1), ("I2_same_path_same_mtime", "index", i2), ("I3_append_invalidates", "index", i3),
          ("O1_latency_single", "tree", o1), ("O3_guest_rss", "tree", o3),
          ("R1_claim1", "claim1", r1), ("O2_latency_concurrent", "claim1", o2), ("R2_poc4", "poc4", r2), ("R3_trace_suite", "trace", r3)]
    return L


STAGES = {"tree": stage_tree, "tau2": stage_tau2, "store": stage_store, "scale": stage_scale, "index": stage_index, "claim1": stage_claim1,
          "poc4": stage_poc4, "trace": stage_trace}


def stage(name):
    if name not in _stage_cache:
        try:
            fn = (lambda: stage_loss(name.split(":", 1)[1])) if name.startswith("loss:") else STAGES[name]
            _stage_cache[name] = ("ok", fn())
        except NotImplementedCLI as e:
            _stage_cache[name] = ("ni", str(e))
        except Exception as e:
            (OUT / ("stage_%s.error.txt" % name.replace(":", "_"))).write_text(traceback.format_exc())
            _stage_cache[name] = ("err", "%s: %s" % (type(e).__name__, str(e)[:160]))
        finally:
            for slot in list(_created):
                vm_destroy(slot)
            cleanup_injections()
    return _stage_cache[name]


def cleanup_injections():
    for rule in list(_iptables):
        run(["iptables", "-D", "INPUT", *rule], timeout=30)
        _iptables.remove(rule)
    if _killed:
        ensure_active(sorted(_killed))


# =============================================================================================== SELFTEST
def selftest():
    FIX = HERE / "fixtures"
    res = []

    def ok(name, cond, detail=""):
        res.append(cond)
        print("%s selftest %s %s" % ("PASS" if cond else "FAIL", name, detail))

    log = [json.loads(l) for l in open(FIX / "b2_log.jsonl") if l.strip()]
    good = [json.loads(l) for l in open(FIX / "b2_query.jsonl") if l.strip()]
    P = good[0]["principal"]
    controls = [("halt", "hr_1a2b"), ("resume", "hr_3c4d")]
    gt = lib.ground_truth(log)
    ok("ground_truth", gt["attempts"] == 3 and gt["llm_ok"] == 2 and gt["denied"] == 1 and gt["exited"] and gt["mine"]["authz.decision"] == 3, str(gt["mine"]))
    ok("timeline_accepts_good", lib.check_timeline(good, P, log, controls) == [], str(lib.check_timeline(good, P, log, controls)[:1]))
    ok("timeline_receipt_wildcard", lib.check_timeline(good, P, log, [("halt", None), ("resume", None)]) == [])

    def mut(fn):
        evs = [json.loads(json.dumps(e)) for e in good]
        fn(evs)
        return lib.check_timeline(evs, P, log, controls)
    sib = json.load(open(FIX / "sibling_event.json"))
    ok("catches_sibling_event", any("foreign" in p for p in mut(lambda E: E.insert(3, sib))))
    ok("catches_missing_refusal", any("timeline differs" in p for p in mut(lambda E: E.pop(5))))
    ok("catches_missing_halt", any("timeline differs" in p for p in mut(lambda E: E.pop(3))))
    ok("catches_wrong_receipt", any("timeline differs" in p for p in mut(lambda E: E[3].update(receipt="hr_ffff", authority={"receipt": "hr_ffff", "by": "ubuntu"}))))
    ok("catches_authority_receipt_mismatch", any("receipt/by" in p for p in mut(lambda E: E[3]["authority"].update(receipt="hr_0000"))))
    ok("catches_wrong_authz_ref", any("authz_ref" in p for p in mut(lambda E: E[2]["authority"].update(authz_ref="h.gateway.0000000001"))))
    ok("catches_deny_without_policy", any("without policy_ids" in p or "policy_ids" in p for p in mut(lambda E: (E[5].update(policy_ids=[]), E[5]["authority"].update(policy_ids=[])))))
    ok("catches_policy_id_not_what_agent_saw", any("what the agent saw" in p for p in mut(lambda E: (E[5].update(policy_ids=["other"]), E[5]["authority"].update(policy_ids=["other"])))))
    ok("catches_reorder", any("order" in p or "differs" in p for p in mut(lambda E: E.__setitem__(slice(1, 3), [E[2], E[1]]))))
    ok("catches_duplicate_id", any("duplicate" in p for p in mut(lambda E: E.append(dict(E[-1])))))
    ok("catches_missing_authority", any("authority missing" in p for p in mut(lambda E: E[1].pop("authority"))))
    ok("catches_extra_decision", any("differs" in p for p in mut(lambda E: E.insert(6, dict(E[5], event_id="h.gateway.0000000099", seq=99, ts="2026-09-21T09:46:49.730Z")))))
    ok("catches_halt_not_naming_principal", any("target/nodes" in p for p in mut(lambda E: E[3].update(nodes=[P + "x"], target=P + "x"))))
    ok("disjoint_catches_shared_event", lib.check_disjoint({"B1": [good[1]], "B2": good}) != [] and lib.check_disjoint({"B1": [sib], "B2": good}) == [])
    ok("lost_events_counts", lib.lost_events(gt, good) == 0 and lib.lost_events(gt, good[:-1]) == 1 and lib.lost_events(gt, [e for e in good if e["kind"] != "authz.decision"]) == 3)
    ok("policy_ids_from_body", lib.policy_ids_from_body(log[4]["b"]) == ["default-deny"] and lib.policy_ids_from_body("denied hr_1234") is None)
    for case in json.load(open(FIX / "m8_cases.json")):
        probs = lib.check_m8(case["rec"], case["lost"], case["clean"], case.get("mine"))
        ok("m8_" + case["name"], (probs == []) == case["ok"], str(probs[:1]))
    ok("tau2_witness_from_simulations", witness_llm_count(REPO / "data/poc6/run_retail-0_9/result.json") == 14)
    lines = lib.parse_check_lines(open(FIX / "claim1_sample.txt").read())
    gw_ok = lib.parse_claim1_gateway("PASS 7 gateway reliability under concurrency measured=calls=20000 agents=8 unanswered=0 p50=41ms p99=84ms max=1073ms bar=x")
    gw_bad = lib.parse_claim1_gateway(open(FIX / "claim1_sample.txt").read().splitlines()[2])
    ok("claim1_parsers", lines["1"][0] and gw_ok == {"calls": 20000, "unanswered": 0, "p50": 41, "p99": 84} and gw_bad["unanswered"] == 3 and lines["7"][0] is False)
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "b.jsonl")
        open(a, "wb").write(b"line1\n"); open(b, "wb").write(b"x\n")
        snap = lib.snapshot([a, b])
        open(a, "ab").write(b"line2\n")
        ok("append_only_accepts_append", lib.prefix_broken(snap) == [])
        open(b, "wb").write(b"y\n")
        ok("append_only_catches_rewrite", lib.prefix_broken(snap) == [b])
        open(a, "wb").write(b"lin")
        ok("append_only_catches_truncate", set(lib.prefix_broken(snap)) == {a, b})
        # generator: a small day validates with the POC 6 tools, every principal has an oracle, the picks are sane
        day = os.path.join(tmp, "day.jsonl")
        t0 = time.perf_counter()
        gen = lib.gen_day(day, 20_000, seed=2)
        gen_s = time.perf_counter() - t0
        rows = [json.loads(l) for l in open(day)]
        picks = lib.pick_principals(gen["counts"])
        want = lib.audit_oracle_stream(day, list(picks.values()))
        ok("gen_day_count", abs(len(rows) - 20_000) < 20_000 * 0.05 and gen["n"] == len(rows), "%d rows in %.1fs" % (len(rows), gen_s))
        ok("gen_day_one_day", all(r["ts"].startswith(gen["day"]) for r in rows))
        mix = lib.count_kinds(rows)
        ok("gen_day_real_mix", 0.55 < mix["authz.decision"] / len(rows) < 0.85 and mix["llm.call"] > 0 and mix["halt"] > 0 and mix["tool.call"] > 0, str({k: mix[k] for k in ("authz.decision", "tool.call", "llm.call", "halt")}))
        ok("gen_day_picks", len(want[picks["small"]]) <= 4 < len(want[picks["median"]]) < len(want[picks["largest"]]) and picks["largest"] == gen["largest"],
           str({k: len(want[p]) for k, p in picks.items()}))
        sub_halt = [r for r in rows if r["kind"] == "halt" and len(r["nodes"]) > 1]
        if sub_halt:
            s = sub_halt[0]["nodes"][1]
            w = lib.audit_oracle_stream(day, [s])[s]
            ok("oracle_includes_parent_halt_for_sub", any(x[0] == sub_halt[0]["event_id"] for x in w))
        # chain and validity through the POC 6 black boxes
        for tool in ("validate", "verify"):
            p = run(["python3", str(REPO / "trace" / "bin" / tool), day], timeout=300)
            try:
                j = json.loads(p.stdout)
            except ValueError:
                j = {"ok": None, "errors": [p.stdout[:100]]}
            ok("gen_day_%s" % tool, p.returncode == 0 and j.get("ok") is True, str((j.get("errors") or [])[:1]))
        # the audit oracle agrees with the POC 6 reference query (independent implementation)
        p = run(["python3", str(REPO / "trace" / "bin" / "query"), "audit", "--principal", picks["median"], day], timeout=300)
        got = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        ok("oracle_matches_poc6_query", p.returncode == 0 and lib.compare_audit(want[picks["median"]], got) is None, str(lib.compare_audit(want[picks["median"]], got)))
        bad = got[:-1]
        ok("compare_audit_catches_missing", lib.compare_audit(want[picks["median"]], bad) is not None)
        if got:
            got2 = [dict(got[0], authority={})] + got[1:]
            ok("compare_audit_catches_authority", lib.compare_audit(want[picks["median"]], got2) is not None or got[0]["authority"] == {})
            got3 = [dict(got[0], hash="0" * 64)] + got[1:]
            ok("compare_audit_catches_foreign_row_hash", lib.compare_audit(want[picks["median"]], got3) is not None)
        # stale-index oracle: a mutated day has the same size, the same ids, different hashes, and is still valid
        day2 = os.path.join(tmp, "day2.jsonl")
        lib.mutate_day(day, day2)
        want2 = lib.audit_oracle_stream(day2, [picks["median"], gen["smallest"]])
        ok("mutate_day_same_size_same_ids", os.path.getsize(day) == os.path.getsize(day2)
           and [w[0] for w in want2[picks["median"]]] == [w[0] for w in want[picks["median"]]])
        w_small_a = lib.audit_oracle_stream(day, [gen["smallest"]])[gen["smallest"]]
        ok("mutate_day_hashes_differ", any(a[2] != b[2] for a, b in zip(want2[gen["smallest"]], w_small_a)))
        # the POC 6 reference query over the OLD day is exactly a stale answer for the new day: the oracle rejects it
        stale = [json.loads(l) for l in run(["python3", str(REPO / "trace" / "bin" / "query"), "audit", "--principal", gen["smallest"], day], timeout=300).stdout.splitlines() if l.strip()]
        ok("stale_answer_detected", stale and lib.compare_audit(want2[gen["smallest"]], stale) is not None)
        for tool in ("validate", "verify"):
            pp = run(["python3", str(REPO / "trace" / "bin" / tool), day2], timeout=300)
            ok("mutate_day_%s" % tool, pp.returncode == 0 and json.loads(pp.stdout).get("ok") is True)
        # two days (different hosts) concatenate into one valid store; the oracle over both includes both
        dayb = os.path.join(tmp, "dayb.jsonl")
        genb = lib.gen_day(dayb, 5_000, day="2026-09-02", seed=22, host="hdayb")
        both = os.path.join(tmp, "both.jsonl")
        open(both, "w").write(open(day).read() + open(dayb).read())
        pp = run(["python3", str(REPO / "trace" / "bin" / "verify"), both], timeout=300)
        wboth = lib.audit_oracle_stream([day, dayb], [gen["largest"]])[gen["largest"]]
        wA = lib.audit_oracle_stream([day, dayb], [gen["largest"]], day=gen["day"])[gen["largest"]]
        ok("two_days_one_store", pp.returncode == 0 and genb["largest"] == gen["largest"]
           and len(wboth) == len(want[gen["largest"]]) + len(lib.audit_oracle_stream(dayb, [gen["largest"]])[gen["largest"]])
           and len(wA) == len(want[gen["largest"]]))
    print("selftest %d/%d" % (sum(res), len(res)))
    return all(res)


# =============================================================================================== main
def main():
    global QUICK
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    QUICK = a.quick
    OUT.mkdir(exist_ok=True)
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if os.geteuid() != 0:
        print("run as root on the host (sudo python3 collect/tests/run.py)")
        sys.exit(2)
    os.chdir(REPO)
    services = []
    try:
        services = list(store_info().get("services") or [])
    except NotImplementedCLI:
        pass
    except Exception as e:
        print("note: avos store unusable at start: %s" % str(e)[:160])
    checks = [c for c in check_list(services) if not a.only or c[0].startswith(a.only)]
    results = []
    t_start = time.time()
    try:
        for cid, st, fn in checks:
            t0 = time.time()
            status, ctx = stage(st)
            if status == "ni":
                ok, what, meas, bar = False, "not implemented: %s" % ctx, "-", "-"
            elif status == "err":
                ok, what, meas, bar = False, "ERROR in stage %s" % st, ctx, "-"
            else:
                try:
                    ok, what, meas, bar = fn(ctx)
                except Exception as e:
                    (OUT / ("check_%s.error.txt" % cid)).write_text(traceback.format_exc())
                    ok, what, meas, bar = False, "ERROR %s" % type(e).__name__, str(e)[:200], "-"
            print("%s %s %s measured=%s bar=%s (%.0fs)" % ("PASS" if ok else "FAIL", cid, what, meas, bar, time.time() - t0), flush=True)
            results.append(ok)
    finally:
        for slot in list(_created):
            vm_destroy(slot)
        cleanup_injections()
        ensure_active(BASE_SERVICES + services)
        run(["sh", "-c", "rm -f %s/cm-* %s/store-*.jsonl" % (OUT, OUT)], timeout=15)
        shutil.rmtree(OUT / "store", ignore_errors=True)
    print("total %.0fs, %d/%d passed" % (time.time() - t_start, sum(results), len(results)))
    sys.exit(0 if results and all(results) else 1)


if __name__ == "__main__":
    main()
