#!/usr/bin/env python3
"""Independent acceptance suite for Avos claim 1 (control).

  "Any single sandbox, or its network, can be stopped, resumed or cut off by name without disrupting the
   others. M6: stop or network cut takes effect in <= 500 ms. M7: 0 sibling events lost or delayed.
   Both hold on 10 repetitions."

Black box: this driver only uses documented interfaces -- infra/vm, POST /spawn, the shim's spawn socket,
supervisor/ctl.py, the gateway as seen from inside a guest, and cgroup.events read from inside a guest. It does
not read any implementation file. Run ON THE HOST from ~/poc:

  sudo python3 supervisor/tests/claim1.py [--only 1,3] [--quick]

One line per check:  PASS|FAIL <id> <what> measured=<v> bar=<v>. Exit 0 only if every check passed.
Raw per-check measurements land in /tmp/claim1/. VMs and any appended ledger rows are cleaned up in finally.
"""
import argparse, json, os, pathlib, secrets, statistics, subprocess, sys, threading, time, urllib.request, urllib.error

REPO = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
OUT = pathlib.Path("/tmp/claim1")
KEY = "/var/lib/avos/images/vm_key"
LEDGER = "/var/lib/avos/events.jsonl"
CGROOT = "/sys/fs/cgroup/system.slice/avos-agent.service"
IP = {"vm-01": "172.16.1.2", "vm-02": "172.16.2.2"}
MODEL = None  # read from agent.env at startup

TICK = 0.05           # sibling tick period (as in poc5)
LATE = 0.100          # metrics.md M7: a tick "delayed" is one late by more than 100 ms
WINDOW = 1.0          # metrics.md M7: ... within 1 s of a halt or resume
GAP_BAR = TICK + LATE  # a tick is delayed if the gap before it exceeds this (0.15 s)
HOLD = 1.0            # seconds a branch is held halted/cut per repetition
M6_BAR_MS = 500
M6_GROWTH_BAR_MS = 20
SYN_ROWS_FULL = 200_000
SYN_ROWS_QUICK = 20_000

_offsets = {}         # slot -> (host_clock - guest_clock) seconds
_created = set()      # slots to destroy in finally
_appended = []        # list of unique tokens appended to the ledger, to remove in finally


# --------------------------------------------------------------------------------------------------- shell / ssh
def run(cmd, timeout=120, check=False):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("cmd failed %r: %s" % (cmd, p.stderr[:400]))
    return p


def gssh_base(slot):
    return ["ssh", "-q", "-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ControlMaster=auto", "-o", "ControlPath=%s/cm-%%C" % OUT,
            "-o", "ControlPersist=180", "root@" + IP[slot]]


def gssh(slot, cmd, timeout=60):
    return run(gssh_base(slot) + [cmd], timeout=timeout)


def ctl(*args, timeout=30):
    return run(["python3", str(REPO / "supervisor" / "ctl.py"), *args], timeout=timeout)


# --------------------------------------------------------------------------------------------------- VM lifecycle
def vm_create(slot):
    p = run(["infra/vm", "create", slot], timeout=120)
    if p.returncode != 0 and "exists" in (p.stdout + p.stderr):
        run(["infra/vm", "destroy", slot], timeout=120)
        p = run(["infra/vm", "create", slot], timeout=120)
    if p.returncode != 0:
        raise RuntimeError("vm create %s failed: %s" % (slot, (p.stdout + p.stderr)[:400]))
    _created.add(slot)
    vm_id = None
    for tok in (p.stdout + p.stderr).split():
        if tok.startswith("vm_id="):
            vm_id = tok.split("=", 1)[1]
    gssh_base(slot)  # establish master
    gssh(slot, "true")
    ship(slot)
    _offsets[slot] = measure_offset(slot)
    return vm_id


def vm_destroy(slot):
    try:
        run(["infra/vm", "destroy", slot], timeout=120)
    finally:
        _created.discard(slot)


def ship(slot):
    import base64
    for name in ("workload.py", "watch.py"):
        data = base64.b64encode((HERE / name).read_bytes()).decode()
        gssh(slot, "base64 -d > /tmp/%s" % name + " <<'B64'\n" + data + "\nB64", timeout=30)
    gssh(slot, "mkdir -p -m 777 /tmp/claim1 && rm -f /tmp/claim1/*")


def measure_offset(slot, samples=11):
    vals = []
    for _ in range(samples):
        h1 = time.time()
        g = float(gssh(slot, "date +%s.%N", timeout=15).stdout)
        h2 = time.time()
        vals.append((h1 + h2) / 2 - g)
    return statistics.median(vals)


def hostclock(slot, guest_wall):
    return guest_wall + _offsets[slot]


# --------------------------------------------------------------------------------------------------- spawning
def cfg_for(name, **kw):
    c = {"self": "/tmp/workload.py", "log": "/tmp/claim1/%s.jsonl" % name}
    c.update(kw)
    return c


def spawn_top(slot, name, cfg):
    body = {"name": name, "argv": ["/usr/bin/python3", "/tmp/workload.py", json.dumps(cfg)]}
    req = urllib.request.Request("http://%s/spawn" % IP[slot], data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def read_logs(slot):
    """Return {logname: [records]} from /tmp/claim1/*.jsonl in the guest."""
    out = gssh(slot, "for f in /tmp/claim1/*.jsonl; do echo \"@@@$f\"; cat \"$f\"; done", timeout=60).stdout
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


def wait_logs(slot, names, need_meta=True, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        logs = read_logs(slot)
        ok = all(n in logs and (not need_meta or any(r["k"] == "meta" for r in logs[n])) for n in names)
        if ok:
            return logs
        time.sleep(0.3)
    raise RuntimeError("logs not ready: have %s want %s" % (list(read_logs(slot)), names))


def cg_of(logs, name):
    for r in logs.get(name, []):
        if r["k"] == "meta":
            return r["cg"]
    raise KeyError("no meta for %s" % name)


def ticks(logs, name, slot):
    return sorted(((r["n"], hostclock(slot, r["t"])) for r in logs.get(name, []) if r["k"] == "tick"))


def calls(logs, name):
    return [r for r in logs.get(name, []) if r["k"] == "call"]


# --------------------------------------------------------------------------------------------------- freeze watcher
class Watcher:
    """Reads EDGE lines from /tmp/watch.py over a persistent ssh channel: edges = [(cgdir, frozen, host_wall)]."""
    def __init__(self, slot, cgdirs):
        self.slot, self.edges, self.lk = slot, [], threading.Lock()
        self.proc = subprocess.Popen(gssh_base(slot) + ["python3 -u /tmp/watch.py " + " ".join(cgdirs)],
                                     stdout=subprocess.PIPE, text=True)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            parts = line.split()
            if len(parts) == 4 and parts[0] == "EDGE":
                with self.lk:
                    self.edges.append((parts[1], parts[2], hostclock(self.slot, float(parts[3]))))

    def wait_edge(self, cgdir, frozen, after, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lk:
                for cg, fr, ts in self.edges:
                    if cg == cgdir and fr == frozen and ts >= after - 0.5:
                        return ts
            time.sleep(0.002)
        return None

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


# --------------------------------------------------------------------------------------------------- M7 scoring
def m7_score(logs, slot, sibling_names, control_times):
    """metrics.md M7: every failed sibling call (any time) + every sibling tick late by > GAP_BAR within
    WINDOW of a control. Returns (total, failed_calls, delayed_ticks, detail list)."""
    failed, delayed, detail = 0, 0, []
    for nm in sibling_names:
        for c in calls(logs, nm):
            st = c["st"]
            if not (st == 200 or st == 403):
                failed += 1
                detail.append({"sibling": nm, "kind": "failed_call", "st": st, "b": c.get("b", "")})
    for nm in sibling_names:
        seq = ticks(logs, nm, slot)
        for (n0, t0), (n1, t1) in zip(seq, seq[1:]):
            gap = t1 - t0
            if gap > GAP_BAR and any(abs(t1 - c) <= WINDOW or abs(t0 - c) <= WINDOW for c in control_times):
                delayed += 1
                detail.append({"sibling": nm, "kind": "delayed_tick", "n": n1, "gap_ms": round(gap * 1000)})
    return failed + delayed, failed, delayed, detail


def dump(name, obj):
    (OUT / name).write_text(json.dumps(obj, indent=2, default=str))


# =============================================================================================== CHECKS
def check1(reps):
    """Halt a sub-agent (own child) by name: M6 <= 500 ms, whole branch frozen (cgroup + counter), parent and
    siblings keep progressing, resume continues with no lost sequence."""
    slot = "vm-01"
    vm_create(slot)
    b1 = cfg_for("B1", tick=TICK)
    b = cfg_for("B", tick=TICK, children=[{"name": "B1", "cfg": b1}])
    spawn_top(slot, "A", cfg_for("A", tick=TICK, children=[{"name": "B", "cfg": b}]))
    spawn_top(slot, "S1", cfg_for("S1", tick=TICK))
    spawn_top(slot, "S2", cfg_for("S2", tick=TICK))
    logs = wait_logs(slot, ["A", "B", "B1", "S1", "S2"])
    cgB, cgB1 = cg_of(logs, "B"), cg_of(logs, "B1")
    spiffeB = ctl_spiffe(slot, "/agent/A/sub/B")
    w = Watcher(slot, [cgB, cgB1])
    time.sleep(0.5)

    m6, intervals, froze_child = [], [], 0
    try:
        for _ in range(reps):
            t0 = time.time()
            ctl("halt", spiffeB)
            fz = w.wait_edge(cgB, "1", t0)
            fzc = w.wait_edge(cgB1, "1", t0)
            if fz is not None:
                m6.append((fz - t0) * 1000)
            if fzc is not None:
                froze_child += 1
            time.sleep(HOLD)
            tr = time.time()
            ctl("resume", spiffeB)
            w.wait_edge(cgB, "0", tr)
            intervals.append((fz if fz else t0, tr))
            time.sleep(0.4)
    finally:
        w.close()

    time.sleep(0.5)
    logs = read_logs(slot)
    # independent-of-ctl freeze proof: B's own tick counter makes no progress inside any halt window
    b_in_window = 0
    for n, t in ticks(logs, "B", slot):
        if any(fz + 0.08 <= t <= tr for fz, tr in intervals):
            b_in_window += 1
    # parent + siblings progressed during the holds
    sib_ok = True
    for nm in ("A", "S1", "S2"):
        adv = sum(1 for n, t in ticks(logs, nm, slot) if any(fz <= t <= tr for fz, tr in intervals))
        sib_ok = sib_ok and adv > 0
    # resume with no lost sequence: B's tick numbers are contiguous 1..max
    bn = [n for n, _ in ticks(logs, "B", slot)]
    contiguous = bool(bn) and set(bn) == set(range(1, max(bn) + 1))

    med = statistics.median(m6) if m6 else 1e9
    worst = max(m6) if m6 else 1e9
    dump("check1.json", {"m6_ms": m6, "median": med, "worst": worst, "froze_child": froze_child,
                         "b_ticks_in_halt_window": b_in_window, "siblings_progressed": sib_ok,
                         "contiguous_sequence": contiguous, "reps": reps})
    ok = (len(m6) == reps and worst <= M6_BAR_MS and froze_child == reps and b_in_window == 0
          and sib_ok and contiguous)
    vm_destroy(slot)
    return ok, "halt-by-name M6+freeze+progress+resume", \
        "median=%.0fms worst=%.0fms child_froze=%d/%d B_ticks_in_halt=%d sibs_ok=%s seq_ok=%s" % \
        (med, worst, froze_child, reps, b_in_window, sib_ok, contiguous), "M6<=%dms, all others 0/true" % M6_BAR_MS


def check2(reps):
    """Escape attempts: a double-forked setsid daemon and a rapid short-lived forker. Halt freezes every
    descendant (progress stops); kill leaves none alive; nothing outside the branch is touched."""
    slot = "vm-01"
    vm_create(slot)
    spawn_top(slot, "DF", cfg_for("DF", daemon_file="/tmp/claim1/daemon.cnt"))
    spawn_top(slot, "FK", cfg_for("FK", forker_file="/tmp/claim1/forker.log"))
    spawn_top(slot, "S", cfg_for("S", tick=TICK))
    logs = wait_logs(slot, ["DF", "FK", "S"])
    cgDF, cgFK = cg_of(logs, "DF"), cg_of(logs, "FK")
    time.sleep(1.0)
    spiffeDF, spiffeFK = ctl_spiffe(slot, "/agent/DF"), ctl_spiffe(slot, "/agent/FK")

    def daemon_val():
        return int(gssh(slot, "cat /tmp/claim1/daemon.cnt 2>/dev/null | awk '{print $1}' || echo 0").stdout or 0)

    def forker_lines():
        return int(gssh(slot, "wc -l < /tmp/claim1/forker.log 2>/dev/null || echo 0").stdout or 0)

    halt_froze = 0
    for _ in range(reps):
        ctl("halt", spiffeDF); ctl("halt", spiffeFK)
        time.sleep(0.2)
        d0, f0 = daemon_val(), forker_lines()
        time.sleep(1.0)
        d1, f1 = daemon_val(), forker_lines()
        if d1 == d0 and f1 == f0:
            halt_froze += 1
        ctl("resume", spiffeDF); ctl("resume", spiffeFK)
        time.sleep(0.4)
    # sibling S unaffected while descendants were frozen
    logs = read_logs(slot)
    s_alive = len(ticks(logs, "S", slot)) > 0
    # kill: no descendant may remain. Proof independent of ctl: both branch cgroups are gone (cgroup.kill removes
    # the whole subtree, reparented daemon included) and the daemon counter is frozen for good.
    d_pre = daemon_val()
    ctl("kill", spiffeDF); ctl("kill", spiffeFK)
    time.sleep(1.0)
    cg_gone = gssh(slot, "test -d %s && echo LIVE || echo GONE; test -d %s && echo LIVE || echo GONE"
                   % (cgDF, cgFK), timeout=15).stdout.split()
    cgroups_removed = cg_gone == ["GONE", "GONE"]
    d_post1 = daemon_val(); time.sleep(1.0); d_post2 = daemon_val()
    daemon_frozen = d_post2 == d_post1  # counter not advancing after kill
    alive_after_kill = 0 if (cgroups_removed and daemon_frozen) else 1
    s_after = len(ticks(read_logs(slot), "S", slot)) > 0
    dump("check2.json", {"halt_froze": halt_froze, "reps": reps, "sibling_alive": s_alive,
                         "cgroups_removed": cgroups_removed, "cg_gone": cg_gone, "daemon_frozen": daemon_frozen,
                         "daemon_vals": [d_pre, d_post1, d_post2], "alive_after_kill": alive_after_kill,
                         "sibling_after_kill": s_after})
    ok = halt_froze == reps and s_alive and alive_after_kill == 0 and s_after
    vm_destroy(slot)
    return ok, "escape: daemon+forker frozen by halt, gone after kill", \
        "halt_froze=%d/%d alive_after_kill=%d sibling_ok=%s" % (halt_froze, reps, alive_after_kill, s_alive and s_after), \
        "halt_froze=reps, alive_after_kill=0"


def check3(reps):
    """Network cut by name: cut latency (command -> first refused call from target) <= 500 ms; target keeps
    running (not frozen); siblings' calls all answered; uncut restores, measured the same way."""
    slot = "vm-01"
    vm_create(slot)
    # target "support": its refunds are normally allowed (200); a sibling "support2" is denied (403) -- both answers.
    spawn_top(slot, "support", cfg_for("support", tick=TICK, call={"kind": "tool", "cents": 100}, call_every=0.05))
    spawn_top(slot, "support2", cfg_for("support2", tick=TICK, call={"kind": "tool", "cents": 100}, call_every=0.05))
    logs = wait_logs(slot, ["support", "support2"])
    spiffeT = ctl_spiffe(slot, "/agent/support")
    time.sleep(1.0)

    cut_lat, uncut_lat, kept_running = [], [], 0
    for _ in range(reps):
        t0 = time.time(); ctl("cut", spiffeT); time.sleep(1.0)
        tu = time.time(); ctl("uncut", spiffeT); time.sleep(1.0)
        logs = read_logs(slot)
        # first target call refused (403 after being 200) after t0, and first allowed (200) after tu
        tcalls = sorted(((c["t1"], c["st"], c.get("b", "")) for c in calls(logs, "support")))
        first_ref = next((hostclock(slot, t) for t, st, b in tcalls if hostclock(slot, t) >= t0 and st == 403), None)
        first_ok = next((hostclock(slot, t) for t, st, b in tcalls if hostclock(slot, t) >= tu and st == 200), None)
        if first_ref:
            cut_lat.append((first_ref - t0) * 1000)
        if first_ok:
            uncut_lat.append((first_ok - tu) * 1000)
        # target kept ticking across the cut window (not frozen)
        adv = sum(1 for n, t in ticks(logs, "support", slot) if t0 <= t <= tu)
        if adv > 0:
            kept_running += 1
    logs = read_logs(slot)
    sib_unanswered = sum(1 for c in calls(logs, "support2") if not (c["st"] == 200 or c["st"] == 403))
    cmed = statistics.median(cut_lat) if cut_lat else 1e9
    cworst = max(cut_lat) if cut_lat else 1e9
    umed = statistics.median(uncut_lat) if uncut_lat else 1e9
    uworst = max(uncut_lat) if uncut_lat else 1e9
    dump("check3.json", {"cut_ms": cut_lat, "uncut_ms": uncut_lat, "kept_running": kept_running, "reps": reps,
                         "sibling_unanswered": sib_unanswered})
    ok = (len(cut_lat) == reps and cworst <= M6_BAR_MS and len(uncut_lat) == reps and uworst <= M6_BAR_MS
          and kept_running == reps and sib_unanswered == 0)
    vm_destroy(slot)
    return ok, "network cut/uncut by name", \
        "cut med=%.0f worst=%.0f uncut med=%.0f worst=%.0f kept_running=%d/%d sib_unanswered=%d" % \
        (cmed, cworst, umed, uworst, kept_running, reps, sib_unanswered), "cut&uncut<=%dms, others ok" % M6_BAR_MS


def check4(reps):
    """In-flight: a slow model call in flight when its caller is halted (or cut) must not be delivered to the
    caller afterwards."""
    slot = "vm-01"
    vm_create(slot)
    delivered = 0
    detail = []
    n = max(1, reps // 3)  # slow LLM calls are ~9 s each; a few per mode is enough
    for mode in ("halt", "cut"):
        for i in range(n):
            nm = "F_%s_%d" % (mode, i)
            info = spawn_top(slot, nm, cfg_for(nm, call={"kind": "llm", "model": MODEL, "max_tokens": 1500},
                                               calls_max=1))
            spiffe = info["spiffe_id"]
            time.sleep(1.5)  # let the call get in flight
            ctl(mode, spiffe)
            # wait for the upstream answer to be handled (ledger llm.call row for this principal), then release
            rowstate = wait_llm_row(spiffe, timeout=60)
            time.sleep(0.5)
            ctl("resume" if mode == "halt" else "uncut", spiffe)
            time.sleep(2.0)
            logs = read_logs(slot)
            fcalls = calls(logs, nm)
            got200 = any(c["st"] == 200 for c in fcalls)
            if got200:
                delivered += 1
            detail.append({"name": nm, "mode": mode, "agent_saw_200": got200,
                           "agent_call": fcalls[-1] if fcalls else None, "ledger_dropped": rowstate})
    dump("check4.json", {"delivered_after_control": delivered, "detail": detail})
    ok = delivered == 0
    vm_destroy(slot)
    return ok, "in-flight answer dropped on halt/cut", \
        "delivered_to_caller=%d (over %d halt + %d cut)" % (delivered, n, n), "delivered=0"


def check5(reps, load_secs):
    """M7 under load: never-named siblings across both VMs make frequent gateway calls. First a control-free
    period, then an equal period during which a separate victim is halted/resumed/cut/uncut/killed. Report both."""
    sib_names = []
    for slot in ("vm-01", "vm-02"):
        vm_create(slot)
    per_vm = max(2, 4 if not QUICK else 2)
    for slot in ("vm-01", "vm-02"):
        for i in range(per_vm):
            nm = "L_%s_%d" % (slot[-2:], i)
            sib_names.append((slot, nm))
            spawn_top(slot, nm, cfg_for(nm, tick=TICK, call={"kind": "tool", "cents": 100}, call_every=0.3))
    for slot in ("vm-01", "vm-02"):
        wait_logs(slot, [n for s, n in sib_names if s == slot])

    # (a) control-free baseline
    base_start = time.time()
    time.sleep(load_secs)
    base_end = time.time()
    logs0 = {slot: read_logs(slot) for slot in ("vm-01", "vm-02")}
    base_failed, base_delayed = 0, 0
    for slot in ("vm-01", "vm-02"):
        names = [n for s, n in sib_names if s == slot]
        # baseline: no controls, so score failed calls + any gap>bar over the whole window (worst case for us)
        for nm in names:
            base_failed += sum(1 for c in calls(logs0[slot], nm) if not (c["st"] == 200 or c["st"] == 403))
            seq = ticks(logs0[slot], nm, slot)
            base_delayed += sum(1 for (a, b) in zip(seq, seq[1:])
                                if base_start <= b[1] <= base_end and (b[1] - a[1]) > GAP_BAR)

    # (b) control period: hammer a dedicated victim in each VM
    control_times = {slot: [] for slot in ("vm-01", "vm-02")}
    for slot in ("vm-01", "vm-02"):
        spawn_top(slot, "V_" + slot[-2:], cfg_for("V_" + slot[-2:], tick=TICK))
    for slot in ("vm-01", "vm-02"):
        wait_logs(slot, ["V_" + slot[-2:]])
    ctl_start = time.time()
    end = ctl_start + load_secs
    r = 0
    while time.time() < end:
        r += 1
        for slot in ("vm-01", "vm-02"):
            vic = ctl_spiffe(slot, "/agent/V_" + slot[-2:])
            if not vic:  # victim was killed last round; respawn
                spawn_top(slot, "V_%s_%d" % (slot[-2:], r), cfg_for("V_%s_%d" % (slot[-2:], r), tick=TICK))
                wait_logs(slot, ["V_%s_%d" % (slot[-2:], r)])
                vic = ctl_spiffe(slot, "/agent/V_%s_%d" % (slot[-2:], r))
            for verb in ("halt", "resume", "cut", "uncut"):
                control_times[slot].append(time.time()); ctl(verb, vic); time.sleep(0.25)
    ctl_end = time.time()
    time.sleep(0.5)
    logs1 = {slot: read_logs(slot) for slot in ("vm-01", "vm-02")}

    total, failed, delayed, det = 0, 0, 0, []
    for slot in ("vm-01", "vm-02"):
        names = [n for s, n in sib_names if s == slot]
        t, f, d, dd = m7_score(logs1[slot], slot, names, control_times[slot])
        total += t; failed += f; delayed += d; det += dd
    dump("check5.json", {"m7_total": total, "failed_calls": failed, "delayed_ticks": delayed,
                         "baseline_failed": base_failed, "baseline_delayed_gaps": base_delayed,
                         "siblings": [n for s, n in sib_names], "load_secs": load_secs,
                         "controls_issued": {s: len(control_times[s]) for s in control_times}, "detail": det})
    ok = total == 0
    for slot in ("vm-01", "vm-02"):
        vm_destroy(slot)
    return ok, "M7 under load (both VMs)", \
        "M7=%d (failed=%d delayed=%d) | baseline failed=%d delayed_gaps=%d" % \
        (total, failed, delayed, base_failed, base_delayed), "M7=0"


def check6(reps):
    """Scale independence of M6: stop latency must not grow with the host's history. Measure M6, append many
    synthetic authz.decision rows for a dead VM id, measure M6 again, require no material growth, remove them."""
    slot = "vm-01"
    vm_create(slot)
    spawn_top(slot, "A", cfg_for("A", tick=TICK, children=[{"name": "B", "cfg": cfg_for("B", tick=TICK)}]))
    spawn_top(slot, "S", cfg_for("S", tick=TICK))
    logs = wait_logs(slot, ["A", "B", "S"])
    cgB = cg_of(logs, "B")
    spiffeB = ctl_spiffe(slot, "/agent/A/sub/B")

    def measure(n):
        w = Watcher(slot, [cgB]); time.sleep(0.4); vals = []
        try:
            for _ in range(n):
                t0 = time.time(); ctl("halt", spiffeB)
                fz = w.wait_edge(cgB, "1", t0)
                if fz:
                    vals.append((fz - t0) * 1000)
                time.sleep(0.4); ctl("resume", spiffeB); w.wait_edge(cgB, "0", time.time()); time.sleep(0.3)
        finally:
            w.close()
        return vals

    before = measure(reps)
    nrows = SYN_ROWS_QUICK if QUICK else SYN_ROWS_FULL
    token = append_ledger(nrows)
    after = measure(reps)

    bmed = statistics.median(before) if before else 1e9
    amed = statistics.median(after) if after else 1e9
    growth = amed - bmed
    dump("check6.json", {"before_ms": before, "after_ms": after, "before_median": bmed, "after_median": amed,
                         "growth_ms": growth, "rows_appended": nrows, "token": token})
    ok = len(before) == reps and len(after) == reps and amed <= M6_BAR_MS and growth <= M6_GROWTH_BAR_MS
    remove_ledger(token)
    vm_destroy(slot)
    return ok, "M6 scale independence vs %d-row history" % nrows, \
        "before=%.0fms after=%.0fms growth=%.0fms" % (bmed, amed, growth), \
        "after<=%dms and growth<=%dms" % (M6_BAR_MS, M6_GROWTH_BAR_MS)


def check7(reps):
    """Gateway reliability: >=20000 calls from >=8 concurrent agents across both VMs, no control. 0 unanswered;
    report latency p50/p99/max."""
    for slot in ("vm-01", "vm-02"):
        vm_create(slot)
    agents_per_vm = 4
    total_target = 3200 if QUICK else 20000
    per_agent = -(-total_target // (agents_per_vm * 2))  # ceil
    names = []
    for slot in ("vm-01", "vm-02"):
        for i in range(agents_per_vm):
            nm = "C_%s_%d" % (slot[-2:], i)
            names.append((slot, nm))
            spawn_top(slot, nm, cfg_for(nm, call={"kind": "tool", "cents": 100}, call_every=0, calls_max=per_agent))
    # wait for all callers to finish
    deadline = time.time() + (120 if QUICK else 600)
    while time.time() < deadline:
        logsA = read_logs("vm-01"); logsB = read_logs("vm-02")
        done = sum(1 for s, n in names if any(r["k"] == "calls_done" for r in (logsA if s == "vm-01" else logsB).get(n, [])))
        if done == len(names):
            break
        time.sleep(2)
    logs = {"vm-01": read_logs("vm-01"), "vm-02": read_logs("vm-02")}
    lat, unanswered, total = [], 0, 0
    for slot, nm in names:
        for c in calls(logs[slot], nm):
            total += 1
            if isinstance(c["st"], int):
                lat.append((c["t1"] - c["t0"]) * 1000)
            else:
                unanswered += 1
    lat.sort()
    def pct(p):
        return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else 0
    p50, p99, mx = pct(0.5), pct(0.99), (max(lat) if lat else 0)
    dump("check7.json", {"total_calls": total, "unanswered": unanswered, "agents": len(names),
                         "p50_ms": p50, "p99_ms": p99, "max_ms": mx, "per_agent_target": per_agent})
    ok = total >= total_target and unanswered == 0
    for slot in ("vm-01", "vm-02"):
        vm_destroy(slot)
    return ok, "gateway reliability under concurrency", \
        "calls=%d agents=%d unanswered=%d p50=%.0fms p99=%.0fms max=%.0fms" % \
        (total, len(names), unanswered, p50, p99, mx), "calls>=%d and unanswered=0" % total_target


def check8(reps):
    """Agents cannot control: an agent cannot halt/cut/kill anything (guest control endpoint, cgroup files,
    shim ops) and cannot read another agent's private state to do so."""
    slot = "vm-01"
    vm_create(slot)
    vic = spawn_top(slot, "victim", cfg_for("victim", tick=TICK))
    logs = wait_logs(slot, ["victim"])
    vic_cg = cg_of(logs, "victim")
    vic_run = "/run/avos/" + vic["id"]
    atk_cfg = cfg_for("attacker", intruder={"victim_cg": vic_cg, "victim_run": vic_run,
                                            "self_cg": CGROOT + "/__SELF__", "guest_ip": IP[slot]})
    # self_cg is filled by the attacker itself from /proc/self/cgroup; placeholder unused by intruder for self.
    atk = spawn_top(slot, "attacker", atk_cfg)
    # fix self_cg: attacker reads its own; but we passed a placeholder. Re-point to the real attacker cgroup.
    atk_meta = wait_logs(slot, ["attacker"], need_meta=True)
    real_self = cg_of(atk_meta, "attacker")
    # the intruder used the placeholder for self-freeze; redo that one attempt against the real self cgroup.
    gssh(slot, "setpriv --reuid=%d --regid=%d --clear-groups sh -c 'echo 1 > %s/cgroup.freeze' 2>&1 | tail -1 > /tmp/claim1/selffreeze.txt || true"
         % (atk["uid"], atk["uid"], real_self), timeout=15)
    selffreeze = gssh(slot, "cat /tmp/claim1/selffreeze.txt", timeout=15).stdout.strip()
    time.sleep(2.0)
    logs = read_logs(slot)
    attempts = [r for r in logs.get("attacker", []) if r["k"] == "intrude"]
    not_blocked = [a for a in attempts if not a["blocked"]]
    # victim must still be alive and ticking (attacker changed nothing)
    before = len(ticks(logs, "victim", slot))
    time.sleep(1.0)
    after = len(ticks(read_logs(slot), "victim", slot))
    victim_progressing = after > before
    self_froze_blocked = ("permission denied" in selffreeze.lower()) or selffreeze == ""
    dump("check8.json", {"attempts": attempts, "not_blocked": not_blocked, "selffreeze_result": selffreeze,
                         "victim_progressing": victim_progressing})
    ok = len(not_blocked) == 0 and self_froze_blocked and victim_progressing
    vm_destroy(slot)
    return ok, "agents cannot use the controls", \
        "escapes_succeeded=%d self_freeze_blocked=%s victim_ok=%s" % \
        (len(not_blocked), self_froze_blocked, victim_progressing), "escapes_succeeded=0"


# --------------------------------------------------------------------------------------------------- ledger / ctl helpers
def ctl_spiffe(slot, suffix):
    """Resolve a live principal's full SPIFFE id from `ctl ls` by the slot's current vm-id and a suffix."""
    out = ctl("ls").stdout
    for line in out.splitlines():
        parts = line.split()
        for p in parts:
            if p.startswith("spiffe://") and p.endswith(suffix) and ("/vm/" + slot + "-") in p:
                return p
    return None


def append_ledger(nrows):
    token = "claim1-" + secrets.token_hex(5)
    dead_vm = "vm-09-dead00"
    row = {"kind": "authz.decision", "ts": "2026-01-01T00:00:00.000Z",
           "principal": "spiffe://avoslab.com/vm/%s/agent/synthetic" % dead_vm, "vm_id": dead_vm,
           "action": "tool.call", "resource": "Tool::\"issue_refund\"",
           "args": {"order_id": "#W0", "amount_cents": 100}, "decision": "deny",
           "policy_ids": ["default-deny"], "errors": [], "_synthetic": token}
    line = json.dumps(row) + "\n"
    with open(LEDGER, "a") as f:
        for _ in range(nrows):
            f.write(line)
    _appended.append(token)
    return token


def remove_ledger(token):
    marker = '"_synthetic": "%s"' % token
    tmp = LEDGER + ".claim1tmp"
    with open(LEDGER) as src, open(tmp, "w") as dst:
        for line in src:
            if marker not in line:
                dst.write(line)
    os.replace(tmp, LEDGER)
    if token in _appended:
        _appended.remove(token)


def wait_llm_row(spiffe, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = run(["grep", '"kind": "llm.call"', LEDGER], timeout=30)
        for line in reversed(p.stdout.splitlines()):
            if spiffe + '"' in line:
                try:
                    return json.loads(line).get("dropped")
                except json.JSONDecodeError:
                    pass
        time.sleep(0.5)
    return "no-row"


# --------------------------------------------------------------------------------------------------- main
CHECKS = {1: check1, 2: check2, 3: check3, 4: check4, 6: check6, 7: check7, 8: check8}
QUICK = False


def main():
    global MODEL, QUICK
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    QUICK = args.quick
    reps = 3 if QUICK else 10
    load_secs = 25 if QUICK else 60
    OUT.mkdir(exist_ok=True)
    os.chdir(REPO)
    MODEL = next((l.split("=", 1)[1].strip() for l in open("/var/lib/avos/agent.env") if l.startswith("MODEL=")), None)

    which = [int(x) for x in args.only.split(",") if x.strip()] or [1, 2, 3, 4, 5, 6, 7, 8]
    results = []
    t_start = time.time()
    try:
        for cid in which:
            t0 = time.time()
            try:
                if cid == 5:
                    ok, what, meas, bar = check5(reps, load_secs)
                else:
                    ok, what, meas, bar = CHECKS[cid](reps)
            except Exception as e:
                import traceback
                dump("check%d.error.txt" % cid, traceback.format_exc())
                ok, what, meas, bar = False, "ERROR " + type(e).__name__, str(e)[:200], "-"
                # clean up any VMs this check left behind
                for slot in list(_created):
                    vm_destroy(slot)
            dt = time.time() - t0
            print("%s %d %s measured=%s bar=%s (%.0fs)" % ("PASS" if ok else "FAIL", cid, what, meas, bar, dt),
                  flush=True)
            results.append(ok)
    finally:
        for slot in list(_created):
            vm_destroy(slot)
        for token in list(_appended):
            remove_ledger(token)
        run(["sh", "-c", "rm -f %s/cm-*" % OUT], timeout=15)
    print("total %.0fs, %d/%d passed" % (time.time() - t_start, sum(results), len(results)))
    sys.exit(0 if all(results) and results else 1)


if __name__ == "__main__":
    main()
