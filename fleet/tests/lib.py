"""Independent oracles, synthetic fleets and the fake infra/job for the POC 10 acceptance suite (fleet/CONTRACT.md).

Nothing here knows how the solver works. Every function builds raw artifacts the way infra/job writes them,
runs a contract CLI as a black box, or computes an *expected* answer from the raw files by the contract's
definitions. Python 3.10+ standard library only.
"""
import hashlib
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("AVOS_SUITE_ROOT") or os.path.abspath(os.path.join(HERE, "..", ".."))
FIX = os.path.join(HERE, "fixtures")
FIX_MANIFEST = os.path.join(FIX, "MANIFEST.sha256")
FLEET = os.path.join(ROOT, "fleet", "bin", "fleet")
SPLIT_REL = "data/splits/retail_user_disjoint_v1.json"
SPLIT = os.path.join(ROOT, SPLIT_REL)
PRICES = os.path.join(FIX, "prices.json")
FIX_RESULTS = os.path.join(FIX, "results")

ROLES = {"policy": "Qwen/Qwen3-30B-A3B-Instruct-2507", "simulator": "google/gemma-3-27b-it",
         "nl_judge": "deepseek-ai/DeepSeek-V4-Flash-0731"}
LABEL_MODEL = "openai/gpt-oss-120b"
INFRA_REASONS = ("user_error", "timeout", "infrastructure_error", "unexpected_error")
AGENT_REASONS = ("agent_error", "too_many_errors", "context_window_exceeded", "max_steps", "agent_stop", "user_stop")
B_BOOT, SEED_BOOT = 2000, 300
AGENT_CLASS = {"unknown", "gateway_timeout", "gateway_5xx_unattributed"}


class NotImplementedCLI(Exception):
    """A contract path is missing: the check reports FAIL 'not implemented: <path>'."""


# ---------------------------------------------------------------- process helpers

def _run(cmd, env=None, timeout=900, rss=False, cwd=None, stdin=None):
    e = dict(os.environ)
    e.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if env:
        e.update(env)
    stats = None
    if rss:
        stats = os.path.join(e.get("TMPDIR", "/tmp"), f".rss.{os.getpid()}.{time.time_ns()}.json")
        wrapper = ("import json,resource,subprocess,sys;r=subprocess.run(sys.argv[2:]);"
                   "json.dump({'maxrss':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss},open(sys.argv[1],'w'));"
                   "sys.exit(r.returncode)")
        cmd = [sys.executable, "-c", wrapper, stats, *cmd]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, env=e, timeout=timeout, cwd=cwd or ROOT, input=stdin)
    res = {"rc": p.returncode, "out": p.stdout, "err": p.stderr, "secs": time.perf_counter() - t0, "maxrss_mib": None}
    if stats and os.path.exists(stats):
        raw = json.load(open(stats))["maxrss"]
        os.unlink(stats)
        res["maxrss_mib"] = raw / 2**20 if sys.platform == "darwin" else raw / 1024
    return res


def fleet(*args, env=None, timeout=900, rss=False, cwd=None):
    if not os.path.exists(FLEET):
        raise NotImplementedCLI(os.path.relpath(FLEET, ROOT))
    return _run([sys.executable, FLEET, *map(str, args)], env=env, timeout=timeout, rss=rss, cwd=cwd)


def fleet_popen(*args, env=None, log=None):
    if not os.path.exists(FLEET):
        raise NotImplementedCLI(os.path.relpath(FLEET, ROOT))
    e = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    out = open(log, "ab") if log else subprocess.DEVNULL
    return subprocess.Popen([sys.executable, FLEET, *map(str, args)], env=e, cwd=ROOT, stdout=out, stderr=out,
                            stdin=subprocess.DEVNULL)


def no_traceback(res):
    return "Traceback" not in (res["err"] or "") and "Traceback" not in (res["out"] or "")


def short(s, n=160):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def wait_until(pred, timeout_s, poll_s=0.2):
    """Bounded wait for evidence; returns the predicate's first truthy value or None."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        v = pred()
        if v:
            return v
        time.sleep(poll_s)
    return None


# ---------------------------------------------------------------- files

def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha_text(s):
    return hashlib.sha256(s.encode()).hexdigest()


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj, indent=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent)
        f.write("\n")


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def tree_snapshot(root):
    snap = {}
    for d, _, fs in os.walk(root):
        for n in fs:
            p = os.path.join(d, n)
            snap[os.path.relpath(p, root)] = (os.path.getsize(p), sha_file(p))
    return snap


def same_size_replace(path, old, new, count=-1):
    """Replace text keeping byte size and mtime (what a size+mtime fingerprint cannot see)."""
    assert len(old.encode()) == len(new.encode()), (old, new)
    st = os.stat(path)
    data = open(path, "rb").read()
    assert old.encode() in data, f"{old!r} not in {path}"
    data = data.replace(old.encode(), new.encode(), count)
    with open(path, "r+b") as f:
        f.seek(0)
        f.write(data)
        f.truncate()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def tamper_files(files, mode, rnd):
    if mode == "truncate":
        for p in files:
            n = os.path.getsize(p)
            with open(p, "r+b") as f:
                f.truncate(n // 2)
    elif mode == "flip":
        for p in files:
            data = bytearray(open(p, "rb").read())
            if data:
                data[rnd.randrange(len(data))] ^= 0x55
                open(p, "wb").write(bytes(data))
    elif mode == "swap":
        for a, b in zip(files[0::2], files[1::2]):
            da, db = open(a, "rb").read(), open(b, "rb").read()
            open(a, "wb").write(db)
            open(b, "wb").write(da)
    elif mode == "delete":
        for p in files:
            os.unlink(p)
    return len(files)


def files_under(*roots):
    out = []
    for r in roots:
        for d, _, fs in os.walk(r):
            out += [os.path.join(d, n) for n in fs]
    return sorted(out)


def fixture_drift():
    want = {}
    for line in open(FIX_MANIFEST, encoding="utf-8"):
        if line.strip():
            h, rel = line.rstrip("\n").split("  ", 1)
            want[rel] = h
    have = {}
    for d, _, fs in os.walk(FIX):
        for n in fs:
            rel = os.path.relpath(os.path.join(d, n), FIX)
            if rel != "MANIFEST.sha256":
                have[rel] = sha_file(os.path.join(d, n))
    return sorted(r for r in set(want) | set(have) if want.get(r) != have.get(r))


def write_fixture_manifest():
    rows = []
    for d, _, fs in os.walk(FIX):
        for n in fs:
            rel = os.path.relpath(os.path.join(d, n), FIX)
            if rel != "MANIFEST.sha256":
                rows.append(f"{sha_file(os.path.join(d, n))}  {rel}")
    open(FIX_MANIFEST, "w", encoding="utf-8").write("\n".join(sorted(rows)) + "\n")


# ---------------------------------------------------------------- synthetic artifacts (shaped like infra/job's)

def ts_of(ms):
    """Host-clock timestamp for an epoch in ms."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + f".{int(ms % 1000):03d}Z"


T0_MS = 1_792_000_000_000  # a fixed epoch (2026-10-14) so synthetic fleets are reproducible


def llm_row(i, vm_id, model, status, ms, pt, ct, t_ms, host="ip-172-31-44-187", upstream_status="same", error=None,
            finish_reason="auto", legacy=False):
    r = {"event_id": f"{t_ms:012x}-{vm_id[-6:]}{i:06d}-{i:010d}", "kind": "llm.call", "ts": ts_of(t_ms),
         "writer": f"{host}/gateway/{vm_id}", "seq": i, "host": host,
         "principal": f"spiffe://avoslab.com/vm/{vm_id}/agent/tau2", "vm_id": vm_id, "authz_ref": f"a{i}",
         "model": model, "status": status, "ms": ms, "dropped": False,
         "prompt_tokens": pt if status == 200 else None, "completion_tokens": ct if status == 200 else None}
    if not legacy:
        r["upstream_status"] = status if upstream_status == "same" else upstream_status
        r["error"] = error
        r["finish_reason"] = ("stop" if status == 200 else None) if finish_reason == "auto" else finish_reason
    return r


def deny_row(i, vm_id, model, policy_ids, t_ms, host="ip-172-31-44-187"):
    return {"event_id": f"{t_ms:012x}-{vm_id[-6:]}{i:06d}-{i:010d}", "kind": "authz.decision", "ts": ts_of(t_ms),
            "writer": f"{host}/gateway/{vm_id}", "seq": i, "host": host,
            "principal": f"spiffe://avoslab.com/vm/{vm_id}/agent/tau2", "vm_id": vm_id, "action": "llm.call",
            "resource": f"Model::{json.dumps(model)}", "args": {}, "decision": "deny", "policy_ids": policy_ids,
            "errors": []}


def allow_row(i, vm_id, model, t_ms):
    r = deny_row(i, vm_id, model, ["llm-all"], t_ms)
    r["decision"] = "allow"
    return r


def write_run(results, run, task, spec, rnd, roles=ROLES, slot="vm-01", t_start_ms=None):
    """One run directory as infra/job leaves it, scripted by `spec` (see KINDS). Returns the spec used."""
    d = os.path.join(results, run)
    os.makedirs(d, exist_ok=True)
    vm_id = f"{slot}-{rnd.randrange(16**6):06x}"
    t0 = t_start_ms if t_start_ms is not None else T0_MS + rnd.randrange(3_600_000)
    status, stage, http = spec["status"], spec.get("stage"), spec.get("http", "200")
    wall = spec.get("wall_ms", rnd.randrange(15_000, 180_000))
    rows_spec = spec.get("rows")
    if rows_spec is None:
        n = rnd.randrange(4, 30)
        rows_spec = []
        for i in range(n):
            role = "policy" if i % 3 != 2 else "simulator"
            rows_spec.append((roles[role], 200, rnd.randrange(300, 4000), rnd.randrange(300, 3000), rnd.randrange(20, 400)))
        if spec.get("judge_rows"):
            rows_spec.append((roles["nl_judge"], 200, rnd.randrange(500, 3000), rnd.randrange(800, 2000), rnd.randrange(10, 80)))
        for extra in spec.get("extra_rows", ()):
            rows_spec.append(extra)
    events = []
    if spec.get("events", "ok") != "missing" and status != "missing" and stage != "create":
        t = t0 + 3000
        seq = 0
        budget = max(1000, wall - 4000)
        llm_total = sum(r[2] for r in rows_spec)
        scale = min(1.0, budget / llm_total) if llm_total else 1.0
        rows_spec = [(r[0], r[1], max(1, int(r[2] * scale)) if spec.get("fit_wall", True) else r[2], r[3], r[4], *r[5:]) for r in rows_spec]
        for pid in spec.get("denies", ()):
            seq += 1
            events.append(deny_row(seq, vm_id, spec.get("deny_model", roles["policy"]), pid, t))
            t += 50
        for row in rows_spec:
            m, s, ms, pt, ct = row[:5]
            seq += 1
            events.append(allow_row(seq, vm_id, m, t))
            t += ms
            seq += 1
            events.append(llm_row(seq, vm_id, m, s, ms, pt, ct, t, **(row[5] if len(row) > 5 else {})))
            t += 30
        text = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events)
        if spec.get("events") == "truncate":
            text = text[: len(text) - 40]
        elif spec.get("events") == "garbage":
            text = text + "not json at all\n"
        with open(os.path.join(d, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write(text)
    if status == "missing":
        write_json(os.path.join(d, "result.json"), {"simulations": [{"reward_info": None, "termination_reason": "user_stop"}]})
        return spec
    base = {"run": run, "vm": slot, "vm_id": vm_id, "harness": "tau2", "task": f"retail/{task}"}
    if status == "aborted":
        job = {**base, "status": "aborted", "stage": stage or "harness", "exit_code": spec.get("exit_code", 1), "wall_ms": wall}
    else:
        job = {**base, "vm_spiffe_id": f"spiffe://avoslab.com/vm/{vm_id}",
               "agent_spiffe_id": f"spiffe://avoslab.com/vm/{vm_id}/agent/tau2", "status": status, "http": http,
               "wall_ms": wall, "harness_version": "96a3ef92329baf908ffe9dfe188cea34e8701d2a"}
        if http == "200":
            job["reward"] = spec.get("reward")
            job["termination_reason"] = spec.get("termination_reason", "user_stop")
            if job["reward"] is None:
                job["error"] = spec.get("error", "")
            msgs = [{"role": "assistant" if e["model"] == roles["policy"] else "user", "content": "x",
                     "usage": {"prompt_tokens": e["prompt_tokens"], "completion_tokens": e["completion_tokens"]}}
                    for e in events if e["kind"] == "llm.call" and e["status"] == 200 and e["model"] != roles["nl_judge"]]
            if spec.get("witness") == "retried":          # τ² kept only the second attempt
                msgs = msgs[len(msgs) // 2:]
            elif spec.get("witness") == "tampered" and msgs:
                msgs[0]["usage"]["completion_tokens"] += 1
            sim = {"reward_info": ({"reward": job["reward"]} if job["reward"] is not None else None),
                   "termination_reason": job["termination_reason"], "messages": msgs}
            sims = [sim, dict(sim)] if spec.get("witness") == "two_sims" else [sim]
            write_json(os.path.join(d, "result.json"), {"simulations": sims})
        else:
            with open(os.path.join(d, "error.log"), "w") as f:
                f.write(f"http {http}\n")
    write_json(os.path.join(d, "job.json"), job)
    comp = spec.get("completeness", "complete")
    if comp is not None:
        if comp == "complete":
            c = {"run": run, "vm_id": vm_id, "status": "complete", "ratio": 1.0}
        elif comp == "incomplete":
            c = {"run": run, "vm_id": vm_id, "status": "incomplete", "ratio": spec.get("ratio", 0.9)}
        else:
            c = {"run": run, "vm_id": vm_id, "status": "unverified", "ratio": None}
        write_json(os.path.join(d, "completeness.json"), c)
    return spec


def KINDS(roles=ROLES):
    """Named trial shapes: name -> spec for write_run. `cause` is the contract's expected cause for each."""
    p, s = roles["policy"], roles["simulator"]
    return {
        "done_pass": {"status": "done", "reward": 1.0, "judge_rows": True},
        "done_fail": {"status": "done", "reward": 0.0, "termination_reason": "max_steps"},
        "done_5xx_retried": {"status": "done", "reward": 1.0, "extra_rows": [(p, 502, 900, 0, 0)]},
        "failed_404": {"status": "failed", "reward": None, "termination_reason": "infrastructure_error",
                       "extra_rows": [(roles["nl_judge"], 404, 120, 0, 0)], "error": "NotFoundError 404"},
        "failed_5xx": {"status": "failed", "reward": None, "termination_reason": "infrastructure_error",
                       "extra_rows": [(p, 502, 30000, 0, 0), (p, 503, 100, 0, 0)]},
        "failed_gateway_timeout": {"status": "failed", "http": "timeout", "wall_ms": 1_200_000,
                                   "extra_rows": [(p, 502, 600_102, 0, 0, {"upstream_status": None, "error": "gateway_timeout"})]},
        "failed_unreachable": {"status": "failed", "reward": None, "termination_reason": "infrastructure_error",
                               "extra_rows": [(p, 502, 40, 0, 0, {"upstream_status": None, "error": "upstream_unreachable: connection refused"})]},
        "failed_5xx_legacy": {"status": "failed", "reward": None, "termination_reason": "infrastructure_error",
                              "extra_rows": [(p, 502, 30000, 0, 0, {"legacy": True})]},
        "failed_length": {"status": "failed", "reward": None, "termination_reason": "infrastructure_error",
                          "extra_rows": [(p, 200, 90_000, 8000, 32000, {"finish_reason": "length"})]},
        "failed_length_then_timeout": {"status": "failed", "http": "timeout", "wall_ms": 1_200_000,
                                       "extra_rows": [(p, 200, 90_000, 8000, 32000, {"finish_reason": "length"}),
                                                      (p, 502, 600_102, 0, 0, {"upstream_status": None, "error": "gateway_timeout"})]},
        "done_retried_sim": {"status": "done", "reward": 1.0, "witness": "retried", "judge_rows": True},
        "done_witness_tampered": {"status": "done", "reward": 1.0, "witness": "tampered"},
        "done_two_sims": {"status": "done", "reward": 1.0, "witness": "two_sims"},
        "failed_retried_sim": {"status": "failed", "reward": None, "termination_reason": "agent_error", "witness": "retried"},
        "done_length": {"status": "done", "reward": 0.0, "termination_reason": "max_steps", "extra_rows": [(p, 200, 90_000, 8000, 32000, {"finish_reason": "length"})]},
        "failed_ctx": {"status": "failed", "reward": None, "termination_reason": "context_window_exceeded"},
        "failed_agent_error": {"status": "failed", "reward": None, "termination_reason": "agent_error"},
        "failed_too_many_errors": {"status": "failed", "reward": None, "termination_reason": "too_many_errors"},
        "failed_user_error": {"status": "failed", "reward": None, "termination_reason": "user_error"},
        "failed_tau2_timeout": {"status": "failed", "reward": None, "termination_reason": "timeout"},
        "failed_infra_no_rows": {"status": "failed", "reward": None, "termination_reason": "infrastructure_error", "rows": []},
        "failed_unexpected": {"status": "failed", "reward": None, "termination_reason": "unexpected_error"},
        "failed_unknown_reason": {"status": "failed", "reward": None, "termination_reason": "something_new"},
        "aborted_create": {"status": "aborted", "stage": "create", "exit_code": 2, "wall_ms": 54},
        "aborted_session": {"status": "aborted", "stage": "session", "exit_code": 1, "wall_ms": 9000},
        "aborted_harness": {"status": "aborted", "stage": "harness", "exit_code": 143},
        "aborted_unknown": {"status": "aborted", "stage": "unknown", "exit_code": -1},
        "timeout": {"status": "failed", "http": "timeout", "wall_ms": 1_200_000},
        "http_500": {"status": "failed", "http": "500"},
        "denied": {"status": "failed", "reward": None, "termination_reason": "infrastructure_error", "denies": [["default-deny"]]},
        "unrouted": {"status": "failed", "reward": None, "termination_reason": "infrastructure_error",
                     "denies": [["no-route"]], "deny_model": "nobody/no-such-model", "rows": []},
        "unrouted_then_timeout": {"status": "failed", "http": "timeout", "denies": [["no-route"]], "deny_model": "x/y", "rows": []},
        "no_job": {"status": "missing"},
        "truncated_events": {"status": "done", "reward": 1.0, "events": "truncate"},
        "garbage_events": {"status": "done", "reward": 0.0, "events": "garbage"},
        "truncated_404": {"status": "failed", "reward": None, "termination_reason": "infrastructure_error",
                          "extra_rows": [(p, 404, 120, 0, 0)], "events": "truncate"},
        "no_events": {"status": "done", "reward": 1.0, "events": "missing"},
        "no_completeness": {"status": "done", "reward": 1.0, "completeness": None},
        "incomplete_trace": {"status": "done", "reward": 0.0, "completeness": "incomplete", "ratio": 0.875},
        "unverified_trace": {"status": "done", "reward": 1.0, "completeness": "unverified"},
        "unattributed_model": {"status": "done", "reward": 1.0,
                               "extra_rows": [("mystery/model-9b", 200, 700, 1000, 100), ("mystery/model-9b", 200, 600, 900, 90)]},
    }


EXPECTED_CAUSE = {
    "done_pass": None, "done_fail": None, "done_5xx_retried": None,
    "failed_404": "upstream_4xx", "failed_5xx": "upstream_5xx", "failed_gateway_timeout": "gateway_timeout",
    "failed_unreachable": "upstream_unreachable", "failed_5xx_legacy": "gateway_5xx_unattributed", "failed_length": "agent_length",
    "failed_length_then_timeout": "gateway_timeout", "done_length": None, "done_retried_sim": "witness_mismatch",
    "done_witness_tampered": "witness_mismatch", "done_two_sims": "witness_mismatch", "failed_retried_sim": "witness_mismatch", "failed_ctx": "agent_context_window_exceeded",
    "failed_agent_error": "agent_agent_error", "failed_too_many_errors": "agent_too_many_errors",
    "failed_user_error": "sim_user_error", "failed_tau2_timeout": "sim_timeout",
    "failed_infra_no_rows": "sim_infrastructure_error", "failed_unexpected": "sim_unexpected_error",
    "failed_unknown_reason": "unknown", "aborted_create": "vm_create_failed", "aborted_session": "session_open_failed",
    "aborted_harness": "harness_aborted", "aborted_unknown": "harness_aborted", "timeout": "harness_timeout",
    "http_500": "harness_http_error", "denied": "authz_denied", "unrouted": "model_unrouted",
    "unrouted_then_timeout": "model_unrouted", "no_job": "no_job_json", "truncated_events": None, "garbage_events": None,
    "truncated_404": "sim_infrastructure_error", "no_events": None, "no_completeness": None, "incomplete_trace": None,
    "unverified_trace": None, "unattributed_model": "role_mismatch",
}


def write_fleet(d, tasks, trials_per_task, concurrency, results_root, lines, roles=ROLES, sessions=None,
                status="done", peak=1500.0, mem_mib=2048, part="train", name="synthetic"):
    os.makedirs(d, exist_ok=True)
    meta = {"fleet": name, "harness": "tau2", "domain": "retail", "tasks": list(tasks), "trials_per_task": trials_per_task,
            "concurrency": concurrency, "mem_mib": mem_mib,
            "split": {"path": SPLIT_REL, "sha256": sha_file(SPLIT), "part": part},
            "roles": dict(roles), "results_root": os.path.abspath(results_root),
            "sessions": sessions or [{"started": ts_of(T0_MS), "ended": ts_of(T0_MS + 7_200_000), "pid": 4242}],
            "status": status, "peak_guest_mem_mib": peak}
    write_json(os.path.join(d, "fleet.json"), meta)
    with open(os.path.join(d, "trials.jsonl"), "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, sort_keys=True) + "\n")
    return meta


def journal_line(task, run, slot, status, interrupted=False, t0=T0_MS, wall=60_000):
    return {"task": task, "run": run, "slot": slot, "status": status, "interrupted": interrupted,
            "started": ts_of(t0), "ended": ts_of(t0 + wall)}


def build_synthetic_fleet(tmp, name, plan, roles=ROLES, concurrency=8, seed=1, trials_per_task=None, extra_lines=(),
                          sessions=None, status="done", peak=1500.0):
    """plan: {task: [kind, ...]} -> (fleet dir, results root, {run: kind}). Lines are in a shuffled end order."""
    rnd = random.Random(seed)
    results = os.path.join(tmp, name + "_results")
    fdir = os.path.join(tmp, name + "_fleet")
    kinds = KINDS(roles)
    lines, runs = [], {}
    t = T0_MS
    for task, ks in plan.items():
        for i, k in enumerate(ks):
            run = f"run_retail-{task}_{i}"
            spec = dict(kinds[k])
            slot = f"vm-{rnd.randrange(1, concurrency + 1):02d}"
            write_run(results, run, task, spec, rnd, roles=roles, slot=slot, t_start_ms=t)
            wall = spec.get("wall_ms", 60_000)
            st = "aborted" if spec["status"] == "aborted" else ("done" if spec["status"] == "done" else "failed")
            lines.append(journal_line(task, run, slot, st, t0=t, wall=wall))
            runs[run] = k
            t += rnd.randrange(1000, 120_000)
    rnd.shuffle(lines)
    lines += list(extra_lines)
    n = trials_per_task or max(len(v) for v in plan.values())
    write_fleet(fdir, list(plan.keys()), n, concurrency, results, lines, roles=roles, sessions=sessions,
                status=status, peak=peak, name=name)
    return fdir, results, runs


def write_labels(path, rows_spec, model=LABEL_MODEL, runs=None):
    """Label rows the way label/CONTRACT.md §4 shapes them (only the fields M12 reads matter)."""
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "labels.jsonl"), "w", encoding="utf-8") as f:
        for i, (pt, ct) in enumerate(rows_spec):
            row = {"label_id": "0" * 64, "level": 1, "run": runs[i] if runs else f"run_retail-0_{i}", "labeller": f"model:{model}",
                   "model": model, "verdict": "pass", "usage": {"prompt_tokens": pt, "completion_tokens": ct}}
            f.write(json.dumps(row) + "\n")
        f.write(json.dumps({"label_id": "1" * 64, "level": 2, "run": "run_retail-0_0", "labeller": "human:x", "verdict": "pass"}) + "\n")


# ---------------------------------------------------------------- the oracle (fleet/CONTRACT.md §4–§7)

def parse_ts(s):
    t = time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    import calendar
    return calendar.timegm(t) * 1000 + int(s[20:23])


def read_events(path):
    """-> list of rows, or None when the file is missing or holds a line that is not a JSON object."""
    if not os.path.exists(path):
        return None
    rows = []
    try:
        with open(path, "rb") as f:
            for raw in f:
                if not raw.strip():
                    continue
                ev = json.loads(raw)
                if not isinstance(ev, dict):
                    return None
                rows.append(ev)
    except (ValueError, OSError):
        return None
    return rows


def trial_facts(results_root, run, task, slot, roles):
    d = os.path.join(results_root, run)
    job = None
    try:
        job = read_json(os.path.join(d, "job.json"))
        if not isinstance(job, dict):
            job = None
    except (OSError, ValueError):
        job = None
    status = job["status"] if job and job.get("status") in ("done", "failed", "aborted") else "missing"
    rows = read_events(os.path.join(d, "events.jsonl")) if os.path.isdir(d) else None
    llm = "unverified"
    if rows is not None:
        by_model = {}
        total_ms = calls = 0
        for e in rows:
            if e.get("kind") != "llm.call":
                continue
            m = str(e.get("model"))
            b = by_model.setdefault(m, {"calls": 0, "ok": 0, "prompt_tokens": 0, "completion_tokens": 0, "ms": 0})
            b["calls"] += 1
            calls += 1
            ms = e.get("ms") if isinstance(e.get("ms"), (int, float)) else 0
            b["ms"] += ms
            total_ms += ms
            if e.get("status") == 200:
                b["ok"] += 1
                b["prompt_tokens"] += e.get("prompt_tokens") or 0
                b["completion_tokens"] += e.get("completion_tokens") or 0
        llm = {"calls": calls, "ms": total_ms, "by_model": by_model}
    cause = assign_cause(status, job, rows, roles, d)
    cls = None if cause is None else ("agent" if cause.startswith("agent_") or cause in AGENT_CLASS else "infra")
    wall = job.get("wall_ms") if job and isinstance(job.get("wall_ms"), int) else None
    reward = job.get("reward") if job and isinstance(job.get("reward"), (int, float)) else None
    m8 = "unverified"
    try:
        c = read_json(os.path.join(d, "completeness.json"))
        if c.get("status") in ("complete", "incomplete") and isinstance(c.get("ratio"), (int, float)):
            m8 = {"status": "witness_mismatch" if cause == "witness_mismatch" else c["status"], "ratio": float(c["ratio"])}
    except (OSError, ValueError, AttributeError):
        pass
    host_side = (wall - llm["ms"]) if (wall is not None and llm != "unverified") else "unverified"
    return {"run": run, "task": task, "slot": slot, "status": status, "reward": reward,
            "termination_reason": job.get("termination_reason") if job else None, "wall_ms": wall,
            "cause": cause, "cause_class": cls,
            "valid": cause is None or cls == "agent",
            "passed": (reward == 1.0) if (status == "done" and cause is None) else None, "llm": llm, "host_side_ms": host_side, "m8": m8}


def role_mismatch(rows, roles):
    """Rows exist, and either a status-200 row names a model outside the roles or the policy model has none."""
    ok = [str(e.get("model")) for e in rows if e.get("kind") == "llm.call" and e.get("status") == 200]
    if not any(e.get("kind") == "llm.call" for e in rows):
        return False
    return any(m not in set(roles.values()) for m in ok) or roles["policy"] not in ok


def witness_mismatch(d, rows, roles):
    """result.json exists and is not one simulation whose usage-bearing messages equal, in order and in tokens,
    the trial's status-200 policy/simulator rows."""
    try:
        sims = read_json(os.path.join(d, "result.json")).get("simulations")
    except (OSError, ValueError, AttributeError):
        return False
    if not isinstance(sims, list):
        return False
    if len(sims) != 1:
        return True
    msgs = sims[0].get("messages") if isinstance(sims[0], dict) else None
    if not isinstance(msgs, list) or rows is None:
        return False
    w = [(m["usage"].get("prompt_tokens"), m["usage"].get("completion_tokens")) for m in msgs if isinstance(m, dict) and isinstance(m.get("usage"), dict)]
    s = [(e.get("prompt_tokens"), e.get("completion_tokens")) for e in rows
         if e.get("kind") == "llm.call" and e.get("status") == 200 and e.get("model") != roles.get("nl_judge")]
    return w != s


def assign_cause(status, job, rows, roles, d=None):
    if status == "missing":
        return "no_job_json"
    if rows is not None and status != "done":
        denies = [e for e in rows if e.get("kind") == "authz.decision" and e.get("decision") == "deny"]
        if any("no-route" in (e.get("policy_ids") or []) for e in denies):
            return "model_unrouted"
        if denies:
            return "authz_denied"
        calls = [e for e in rows if e.get("kind") == "llm.call" and isinstance(e.get("status"), int)]
        five = [e for e in calls if e["status"] >= 500]
        if any(isinstance(e.get("upstream_status"), int) and e["upstream_status"] >= 500 for e in five):
            return "upstream_5xx"
        if any(e.get("upstream_status") is None and str(e.get("error") or "").startswith("upstream_unreachable") for e in five):
            return "upstream_unreachable"
        if any(e.get("error") == "gateway_timeout" for e in five):
            return "gateway_timeout"
        if five:
            return "gateway_5xx_unattributed"
        if any(400 <= e["status"] < 500 for e in calls):
            return "upstream_4xx"
    if rows is not None and role_mismatch(rows, roles):
        return "role_mismatch"
    if d and witness_mismatch(d, rows, roles):
        return "witness_mismatch"
    if status == "done":
        return None
    if rows is not None and any(e.get("kind") == "llm.call" and e.get("status") == 200 and e.get("finish_reason") == "length" for e in rows):
        return "agent_length"
    if status == "aborted":
        stage = job.get("stage")
        return {"create": "vm_create_failed", "session": "session_open_failed"}.get(stage, "harness_aborted")
    http = job.get("http")
    if http == "timeout":
        return "harness_timeout"
    if http is not None and str(http) != "200":
        return "harness_http_error"
    tr = job.get("termination_reason")
    if tr in INFRA_REASONS:
        return f"sim_{tr}"
    if tr in AGENT_REASONS:
        return f"agent_{tr}"
    return "unknown"


def pass_k(c, n, k):
    return None if n < k else math.comb(c, k) / math.comb(n, k)


def bootstrap_ci(vals):
    T = len(vals)
    if T < 2:
        return "unverified"
    rng = random.Random(SEED_BOOT)
    means = sorted(statistics.fmean([vals[rng.randrange(T)] for _ in range(T)]) for _ in range(B_BOOT))
    return [means[50], means[1949]]


def median(xs):
    return statistics.median(xs)


def p90(xs):
    s = sorted(xs)
    return s[math.ceil(0.9 * len(s)) - 1]


def fleet_wall_h(meta, lines):
    total = 0
    ends = [parse_ts(l["ended"]) for l in lines if l.get("ended")]
    for s in meta.get("sessions", []):
        a = parse_ts(s["started"])
        b = parse_ts(s["ended"]) if s.get("ended") else (max(ends) if ends else None)
        if b is None:
            continue
        total += max(0, b - a)
    return total / 3_600_000 if meta.get("sessions") else None


def matrix_oracle(fleet_dir, prices, labels=None):
    meta = read_json(os.path.join(fleet_dir, "fleet.json"))
    lines = read_jsonl(os.path.join(fleet_dir, "trials.jsonl"))
    roles = meta["roles"]
    role_of = {m: r for r, m in roles.items()}
    seen, trials, warnings = set(), [], []
    for ln in lines:
        if ln.get("interrupted"):
            continue
        if ln["run"] in seen:
            warnings.append(f"duplicate journal line for {ln['run']}")
            continue
        seen.add(ln["run"])
        trials.append(trial_facts(meta["results_root"], ln["run"], str(ln["task"]), ln.get("slot"), roles))
        if trials[-1]["status"] == "missing":
            warnings.append(f"{ln['run']}: no job.json (counted as a trial, cause no_job_json)")
    trials.sort(key=lambda t: t["run"])
    ntr = len(trials)
    done = [t for t in trials if t["status"] == "done"]
    m2 = {"done": len(done), "trials": ntr, "value": (len(done) / ntr) if ntr else "unverified"}
    # M8
    ver = [t["m8"] for t in trials if t["m8"] != "unverified"]
    m8 = {"complete": sum(1 for v in ver if v["status"] == "complete"), "incomplete": sum(1 for v in ver if v["status"] != "complete"),
          "unverified": ntr - len(ver), "mean_ratio": statistics.fmean(v["ratio"] for v in ver) if ver else "unverified"}
    # M11
    per_task = []
    for task in meta["tasks"]:
        ts = [t for t in trials if t["task"] == str(task)]
        valid = [t for t in ts if t["valid"]]
        n, c = len(valid), sum(1 for t in valid if t["passed"])
        per_task.append({"task": str(task), "n": n, "c": c, "infra": sum(1 for t in ts if t["cause_class"] == "infra"),
                         "trials": len(ts), "pass1": pass_k(c, n, 1), "pass4": pass_k(c, n, 4),
                         "avg4": statistics.fmean((t["reward"] or 0.0) for t in valid) if n else None})
    m11 = {"tasks": per_task, "infra_excluded": sum(p["infra"] for p in per_task),
           "tasks_short_4": [p["task"] for p in per_task if p["pass4"] is None and p["trials"]],
           "tasks_all_infra": [p["task"] for p in per_task if p["n"] == 0 and p["trials"]]}
    for key in ("pass1", "pass4", "avg4"):
        vals = [p[key] for p in per_task if p[key] is not None]
        m11[key] = statistics.fmean(vals) if vals else "unverified"
        m11[key + "_ci95"] = bootstrap_ci(vals) if vals else "unverified"
    causes = Counter(t["cause"] for t in trials if t["cause"])
    m11["causes"] = dict(sorted(causes.items()))
    m11["by_class"] = {"agent": sum(1 for t in trials if t["cause_class"] == "agent"),
                       "infra": sum(1 for t in trials if t["cause_class"] == "infra")}
    # M12
    models = prices.get("models", {}) if isinstance(prices, dict) else {}
    hosts = prices.get("hosts", {}) if isinstance(prices, dict) else {}
    wall_h = fleet_wall_h(meta, lines)
    pass1 = m11["pass1"]
    agg = {}
    for t in trials:
        if t["llm"] == "unverified":
            continue
        for m, b in t["llm"]["by_model"].items():
            a = agg.setdefault(m, {"calls": 0, "ok": 0, "prompt_tokens": 0, "completion_tokens": 0})
            for k in a:
                a[k] += b[k]

    def price(m, pt, ct, calls=1):
        if calls == 0:
            return 0.0
        p = models.get(m)
        if not p or not all(isinstance(p.get(k), (int, float)) for k in ("prompt_per_mtok", "completion_per_mtok")):
            return "unverified"
        return (pt * p["prompt_per_mtok"] + ct * p["completion_per_mtok"]) / 1e6

    def per_task_cost(total):
        if total == "unverified" or not ntr:
            return "unverified", "unverified"
        pt = total / ntr
        succ = pt / pass1 if isinstance(pass1, float) and pass1 > 0 else "unverified"
        return pt, succ

    m12 = {}
    for role in ("policy", "simulator", "nl_judge"):
        m = roles[role]
        a = agg.get(m, {"calls": 0, "ok": 0, "prompt_tokens": 0, "completion_tokens": 0})
        tok = price(m, a["prompt_tokens"], a["completion_tokens"], a["calls"])
        if role == "policy":
            g = hosts.get("gpu", {})
            total = g["usd_per_h"] * wall_h if isinstance(g.get("usd_per_h"), (int, float)) and wall_h is not None else "unverified"
            basis = "gpu_hours"
            ceiling = tok / ntr if tok != "unverified" and ntr else "unverified"
        else:
            total, basis, ceiling = tok, "tokens", "unverified"
        pt, succ = per_task_cost(total)
        m12[role] = {"model": m, "source": "gateway", "outside_gateway": False, **a, "usd_total": total, "usd_per_task": pt,
                     "usd_per_successful_task": succ, "basis": basis, "token_ceiling_usd_per_task": ceiling}
    # label judge
    lj = {"model": "unverified", "source": "label rows", "outside_gateway": True, "calls": "unverified", "ok": "unverified",
          "prompt_tokens": "unverified", "completion_tokens": "unverified", "usd_total": "unverified",
          "usd_per_task": "unverified", "usd_per_successful_task": "unverified", "basis": "tokens",
          "token_ceiling_usd_per_task": "unverified"}
    if labels:
        rows = []
        for p in files_under(labels) if os.path.isdir(labels) else [labels]:
            if p.endswith(".jsonl"):
                rows += [r for r in read_jsonl(p) if r.get("level") == 1 and isinstance(r.get("usage"), dict)]
        foreign = [r for r in rows if r.get("run") not in seen]
        rows = [r for r in rows if r.get("run") in seen]
        if foreign:
            warnings.append(f"{len(foreign)} label rows for runs outside this fleet ignored")
        ms = sorted({r.get("model") for r in rows})
        if not rows:
            lj.update({"calls": 0, "ok": 0, "prompt_tokens": 0, "completion_tokens": 0, "usd_total": 0.0, "usd_per_task": 0.0,
                       "usd_per_successful_task": 0.0 if isinstance(pass1, float) and pass1 > 0 else "unverified"})
        elif len(ms) == 1:
            m = ms[0]
            pt_ = sum(r["usage"].get("prompt_tokens") or 0 for r in rows)
            ct_ = sum(r["usage"].get("completion_tokens") or 0 for r in rows)
            total = price(m, pt_, ct_)
            pt, succ = per_task_cost(total)
            lj.update({"model": m, "calls": len(rows), "ok": len(rows), "prompt_tokens": pt_, "completion_tokens": ct_,
                       "usd_total": total, "usd_per_task": pt, "usd_per_successful_task": succ})
    m12["label_judge"] = lj
    un = {"calls": 0, "ok": 0, "prompt_tokens": 0, "completion_tokens": 0}
    un_models = sorted(m for m in agg if m not in role_of)
    for m in un_models:
        for k in un:
            un[k] += agg[m][k]
    tot = 0.0
    for m in un_models:
        pr = price(m, agg[m]["prompt_tokens"], agg[m]["completion_tokens"], agg[m]["calls"])
        tot = "unverified" if pr == "unverified" or tot == "unverified" else tot + pr
    m12["unattributed"] = {"models": un_models, **un, "usd_total": tot if un_models else 0.0}
    if un_models:
        warnings.append("unattributed models: " + ", ".join(un_models))
    # M13
    walls = [t["wall_ms"] / 1000 for t in done if t["wall_ms"] is not None]
    m13 = ({"n": len(walls), "median_s": median(walls), "p90_s": p90(walls), "worst_s": max(walls), "concurrency": meta["concurrency"]}
           if walls else {"n": 0, "median_s": "unverified", "p90_s": "unverified", "worst_s": "unverified", "concurrency": meta["concurrency"]})
    # M16
    f = hosts.get("fleet", {})
    host_rate = f.get("usd_per_h") if isinstance(f.get("usd_per_h"), (int, float)) else "unverified"
    m16 = {"host_usd_per_h": host_rate, "fleet_wall_h": wall_h if wall_h is not None else "unverified", "completed": len(done)}
    m16["value"] = (host_rate * wall_h / len(done)) if (host_rate != "unverified" and wall_h is not None and done) else "unverified"
    bl = {}
    for name, b in sorted((prices.get("baselines", {}) if isinstance(prices, dict) else {}).items()):
        med = m13["median_s"]
        try:
            if med == "unverified":
                raise KeyError("median")
            if b["kind"] == "vm":
                bl[name] = b["usd_per_h"] * max(b["min_s"], med + b["boot_s"]) / 3600
            elif b["kind"] == "serverless":
                bl[name] = (b["vcpu"] * b["usd_per_vcpu_h"] + b["gb"] * b["usd_per_gb_h"]) * max(b["min_s"], med) / 3600
            else:
                bl[name] = "unverified"
        except (KeyError, TypeError):
            bl[name] = "unverified"
    m16["baselines"] = bl
    nums = [v for v in bl.values() if v != "unverified"]
    m16["ratio_to_best_baseline"] = (m16["value"] / min(nums)) if nums and m16["value"] != "unverified" and min(nums) > 0 else "unverified"
    return {"m2": m2, "m8": m8, "m11": m11, "m12": m12, "m13": m13, "m16": m16, "trials": trials,
            "warnings": sorted(warnings),
            "inputs": {"fleet": meta["fleet"], "trials_per_task": meta["trials_per_task"], "concurrency": meta["concurrency"],
                       "roles": roles}}


def m17_oracle(sweep_dir):
    levels = []
    for n in os.listdir(sweep_dir):
        if n.startswith("level_") and os.path.isdir(os.path.join(sweep_dir, n)):
            d = os.path.join(sweep_dir, n)
            meta = read_json(os.path.join(d, "fleet.json"))
            lines = [l for l in read_jsonl(os.path.join(d, "trials.jsonl")) if not l.get("interrupted")]
            hs, walls, llms, skipped = [], [], [], 0
            for ln in lines:
                t = trial_facts(meta["results_root"], ln["run"], str(ln["task"]), ln.get("slot"), meta["roles"])
                if t["status"] != "done":
                    continue
                if t["llm"] == "unverified" or t["wall_ms"] is None:
                    skipped += 1
                    continue
                hs.append(t["host_side_ms"])
                walls.append(t["wall_ms"])
                llms.append(t["llm"]["ms"])
            peak = meta.get("peak_guest_mem_mib")
            levels.append({"concurrency": meta["concurrency"], "trials": len(lines), "skipped": skipped,
                           "host_side_ms": {"median": median(hs), "n": len(hs)} if hs else "unverified",
                           "wall_ms_median": median(walls) if walls else "unverified",
                           "llm_ms_median": median(llms) if llms else "unverified",
                           "peak_guest_mem_mib": peak if isinstance(peak, (int, float)) else "unverified"})
    levels.sort(key=lambda l: l["concurrency"])
    out = {"levels": levels, "rule": "median host-side trial time ≤ 1.05 × lowest level"}
    if not levels or levels[0]["host_side_ms"] == "unverified":
        out.update({"baseline_concurrency": levels[0]["concurrency"] if levels else "unverified",
                    "baseline_host_side_ms": "unverified", "m17": "unverified", "ceiling_reached": False})
        return out
    base = levels[0]["host_side_ms"]["median"]
    m17, reached = levels[0]["concurrency"], False
    for l in levels[1:]:
        if l["host_side_ms"] == "unverified" or l["host_side_ms"]["median"] > 1.05 * base:
            reached = l["host_side_ms"] != "unverified"
            break
        m17 = l["concurrency"]
    out.update({"baseline_concurrency": levels[0]["concurrency"], "baseline_host_side_ms": base, "m17": m17, "ceiling_reached": reached})
    return out


# ---------------------------------------------------------------- comparison

def diff(got, want, path="", tol=1e-9, out=None):
    """Every key/element of `want` must be in `got` with the same value (numbers within tol); extra keys in got are fine."""
    out = [] if out is None else out
    if isinstance(want, dict):
        if not isinstance(got, dict):
            out.append(f"{path}: want object, got {short(got, 60)}")
            return out
        for k, v in want.items():
            if k not in got:
                out.append(f"{path}.{k}: missing")
            else:
                diff(got[k], v, f"{path}.{k}", tol, out)
    elif isinstance(want, list):
        if not isinstance(got, list) or len(got) != len(want):
            out.append(f"{path}: want list of {len(want)}, got {short(got, 80)}")
            return out
        for i, (g, w) in enumerate(zip(got, want)):
            diff(g, w, f"{path}[{i}]", tol, out)
    elif isinstance(want, bool) or want is None or isinstance(want, str):
        if got != want:
            out.append(f"{path}: want {want!r}, got {short(got, 80)!r}")
    elif isinstance(want, (int, float)):
        if isinstance(got, bool) or not isinstance(got, (int, float)) or not math.isclose(got, want, rel_tol=tol, abs_tol=tol):
            out.append(f"{path}: want {want!r}, got {short(got, 80)!r}")
    else:
        if got != want:
            out.append(f"{path}: want {want!r}, got {got!r}")
    return out


# ---------------------------------------------------------------- the fake infra/job (CONTRACT §9)

FAKE_JOB = r'''#!/usr/bin/env python3
"""Fake infra/job for the POC 10 suite: claims run ids like the real one, writes the same files, honours SLOT,
aborts on SIGTERM, refuses a busy slot, and is scripted per task by AVOS_FAKE_JOB_PLAN."""
import json, os, random, signal, sys, time
sys.path.insert(0, os.environ["AVOS_FAKE_LIB_DIR"])
import lib

RESULTS = os.environ.get("AVOS_FLEET_RESULTS") or os.environ.get("AVOS_RESULTS") or "/var/lib/avos/results"
PLAN = json.load(open(os.environ["AVOS_FAKE_JOB_PLAN"])) if os.environ.get("AVOS_FAKE_JOB_PLAN") else {}
LOG = os.path.join(RESULTS, ".fake", "log.jsonl")


def log(**kw):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps({"t": time.time(), "pid": os.getpid(), **kw}) + "\n")


def main():
    if len(sys.argv) != 4 or sys.argv[1] != "run":
        print("usage: job run <harness> <domain/task>", file=sys.stderr)
        return 2
    harness, task_path = sys.argv[2], sys.argv[3]
    task = task_path.split("/", 1)[1]
    slot = os.environ.get("SLOT") or "vm-01"
    os.makedirs(os.path.join(RESULTS, ".slots"), exist_ok=True)
    t0 = time.time()
    run = None
    for t in range(10000):
        cand = f"run_retail-{task}_{t}"
        try:
            os.mkdir(os.path.join(RESULTS, cand))
            run, trial = cand, t
            break
        except FileExistsError:
            continue
    out = os.path.join(RESULTS, run)
    lock = os.path.join(RESULTS, ".slots", slot)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode()); os.close(fd)
    except FileExistsError:
        json.dump({"run": run, "vm": slot, "vm_id": "", "harness": harness, "task": task_path, "status": "aborted",
                   "stage": "create", "exit_code": 1, "wall_ms": int((time.time() - t0) * 1000), "error": "slot busy"},
                  open(os.path.join(out, "job.json"), "w"))
        log(run=run, slot=slot, event="slot_busy")
        print(f"{slot} exists; run: vm destroy {slot}", file=sys.stderr)
        return 1
    json.dump({k: os.environ.get(k) for k in ("SLOT", "MODEL", "USER_MODEL", "JUDGE_MODEL", "MEM_MIB", "TIMEOUT_S")},
              open(os.path.join(out, "env.json"), "w"))
    specs = PLAN.get("tasks", {}).get(task, [])
    spec = specs[trial] if trial < len(specs) else PLAN.get("default", "done_pass")
    if isinstance(spec, str):
        spec = {"kind": spec}
    kind, sleep_s = spec.get("kind", "done_pass"), float(spec.get("sleep", 0.3))
    roles = {"policy": os.environ.get("MODEL") or lib.ROLES["policy"],
             "simulator": os.environ.get("USER_MODEL") or lib.ROLES["simulator"],
             "nl_judge": os.environ.get("JUDGE_MODEL") or lib.ROLES["nl_judge"]}
    log(run=run, slot=slot, event="start", kind=kind, sleep=sleep_s)
    aborted = {"flag": False}

    def on_term(signum, frame):
        aborted["flag"] = True
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, on_term)
    try:
        time.sleep(sleep_s)
        rnd = random.Random(f"{run}:{kind}")
        s = dict(lib.KINDS(roles)[kind])
        s.setdefault("wall_ms", int((time.time() - t0) * 1000) + rnd.randrange(10_000, 60_000))
        lib.write_run(RESULTS, run, task, s, rnd, roles=roles, slot=slot, t_start_ms=int(t0 * 1000))
        job = json.load(open(os.path.join(out, "job.json"))) if os.path.exists(os.path.join(out, "job.json")) else None
        log(run=run, slot=slot, event="end", status=job["status"] if job else "missing")
        if job:
            print(json.dumps(job))
        return 0 if job and job["status"] == "done" else 1
    except SystemExit as e:
        if aborted["flag"] and not os.path.exists(os.path.join(out, "job.json")):
            json.dump({"run": run, "vm": slot, "vm_id": f"{slot}-dead00", "harness": harness, "task": task_path,
                       "status": "aborted", "stage": "harness", "exit_code": 143, "wall_ms": int((time.time() - t0) * 1000)},
                      open(os.path.join(out, "job.json"), "w"))
            log(run=run, slot=slot, event="aborted")
        return int(e.code or 0)
    finally:
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    sys.exit(main())
'''


def install_fake_job(tmp):
    p = os.path.join(tmp, "fakejob")
    with open(p, "w") as f:
        f.write(FAKE_JOB)
    os.chmod(p, 0o755)
    return p


def fake_log(results):
    p = os.path.join(results, ".fake", "log.jsonl")
    return read_jsonl(p) if os.path.exists(p) else []


def max_overlap(log):
    """Peak number of fake jobs running at once, from their start/end/aborted log lines."""
    evs = []
    for l in log:
        if l["event"] == "start":
            evs.append((l["t"], 1))
        elif l["event"] in ("end", "aborted"):
            evs.append((l["t"], -1))
    evs.sort()
    cur = peak = 0
    for _, d in evs:
        cur += d
        peak = max(peak, cur)
    return peak


def fleet_env(tmp, results, job, plan_path=None, extra=None):
    e = {"AVOS_FLEET_RESULTS": results, "AVOS_RESULTS": results, "AVOS_FAKE_LIB_DIR": HERE, "PYTHONDONTWRITEBYTECODE": "1",
         "AVOS_TRACE_ROOT": os.path.join(tmp, "trace"), "TMPDIR": os.path.join(tmp, "tmp"), "HOME": os.path.join(tmp, "home"),
         "XDG_CACHE_HOME": os.path.join(tmp, "home", ".cache"), "XDG_DATA_HOME": os.path.join(tmp, "home", ".local")}
    for k in ("AVOS_TRACE_ROOT", "TMPDIR", "HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        os.makedirs(e[k], exist_ok=True)
    if plan_path:
        e["AVOS_FAKE_JOB_PLAN"] = plan_path
    e.update(extra or {})
    return e


def fleet_dirs_of(results, tasks):
    """Run directories under results for the given tasks."""
    out = []
    for n in os.listdir(results):
        if n.startswith("run_retail-") and n.rsplit("_", 1)[0].split("-", 1)[1] in set(map(str, tasks)):
            out.append(n)
    return sorted(out)


# ---------------------------------------------------------------- self-checks of the oracle

def selftest():
    """Hand-computed cases the oracle must reproduce; raises AssertionError otherwise."""
    assert pass_k(3, 4, 1) == 0.75 and pass_k(2, 4, 4) == 0.0 and pass_k(4, 4, 4) == 1.0 and pass_k(2, 3, 4) is None
    assert pass_k(0, 0, 1) is None and pass_k(1, 5, 4) == 0.0 and abs(pass_k(4, 5, 4) - 0.2) < 1e-12 and pass_k(3, 5, 4) == 0.0
    assert math.isclose(pass_k(5, 8, 4), math.comb(5, 4) / math.comb(8, 4))
    assert bootstrap_ci([0.5]) == "unverified"
    ci = bootstrap_ci([0.0, 0.25, 0.5, 0.75, 1.0])
    assert ci == bootstrap_ci([0.0, 0.25, 0.5, 0.75, 1.0]) and 0.0 <= ci[0] <= 0.5 <= ci[1] <= 1.0
    assert bootstrap_ci([1.0] * 7) == [1.0, 1.0]
    assert p90([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == 9 and p90([5]) == 5 and median([1, 4]) == 2.5
    # causes, one per shape, in precedence order
    rows404 = [llm_row(1, "vm-01-abcdef", "m", 404, 10, 0, 0, T0_MS)]
    rows5 = [llm_row(1, "vm-01-abcdef", "m", 502, 10, 0, 0, T0_MS), llm_row(2, "vm-01-abcdef", "m", 404, 10, 0, 0, T0_MS)]
    deny = [deny_row(1, "vm-01-abcdef", "m", ["no-route"], T0_MS)] + rows5
    assert assign_cause("missing", None, None, ROLES) == "no_job_json"
    assert assign_cause("failed", {"http": "timeout"}, deny, ROLES) == "model_unrouted"
    assert assign_cause("aborted", {"stage": "create"}, [deny_row(1, "v", "m", ["default-deny"], T0_MS)], ROLES) == "authz_denied"
    assert assign_cause("failed", {"http": "200", "termination_reason": "user_stop"}, rows5, ROLES) == "upstream_5xx"
    gt = [llm_row(1, "v", "m", 502, 600_102, 0, 0, T0_MS, upstream_status=None, error="gateway_timeout")]
    un = [llm_row(1, "v", "m", 502, 40, 0, 0, T0_MS, upstream_status=None, error="upstream_unreachable: refused")]
    lg = [llm_row(1, "v", "m", 503, 40, 0, 0, T0_MS, legacy=True)]
    ln = [llm_row(1, "v", ROLES["policy"], 200, 90_000, 8000, 32000, T0_MS, finish_reason="length")]
    assert assign_cause("failed", {"http": "timeout"}, gt, ROLES) == "gateway_timeout"
    assert assign_cause("failed", {"http": "timeout"}, gt + rows5, ROLES) == "upstream_5xx"
    assert assign_cause("failed", {"http": "200", "termination_reason": "infrastructure_error"}, un, ROLES) == "upstream_unreachable"
    assert assign_cause("failed", {"http": "200", "termination_reason": "infrastructure_error"}, lg, ROLES) == "gateway_5xx_unattributed"
    assert assign_cause("failed", {"http": "200", "termination_reason": "infrastructure_error"}, ln, ROLES) == "agent_length"
    assert assign_cause("done", {"http": "200"}, ln, ROLES) is None
    assert assign_cause("failed", {"http": "200", "termination_reason": "agent_error"}, rows404, ROLES) == "upstream_4xx"
    assert assign_cause("aborted", {"stage": "create"}, None, ROLES) == "vm_create_failed"
    assert assign_cause("aborted", {"stage": "session"}, [], ROLES) == "session_open_failed"
    assert assign_cause("aborted", {"stage": "harness"}, [], ROLES) == "harness_aborted"
    assert assign_cause("aborted", {"stage": "unknown"}, None, ROLES) == "harness_aborted"
    assert assign_cause("failed", {"http": "timeout"}, [], ROLES) == "harness_timeout"
    assert assign_cause("failed", {"http": "500"}, [], ROLES) == "harness_http_error"
    assert assign_cause("failed", {"http": "200", "termination_reason": "user_error"}, [], ROLES) == "sim_user_error"
    assert assign_cause("failed", {"http": "200", "termination_reason": "infrastructure_error"}, None, ROLES) == "sim_infrastructure_error"
    assert assign_cause("failed", {"http": "200", "termination_reason": "context_window_exceeded"}, [], ROLES) == "agent_context_window_exceeded"
    assert assign_cause("failed", {"http": "200", "termination_reason": "max_steps"}, [], ROLES) == "agent_max_steps"
    assert assign_cause("failed", {"http": "200", "termination_reason": "whatever"}, [], ROLES) == "unknown"
    assert assign_cause("failed", {"http": "200"}, [], ROLES) == "unknown"
    okp = [llm_row(1, "vm-01-abcdef", ROLES["policy"], 200, 10, 5, 5, T0_MS)]
    oks = [llm_row(1, "vm-01-abcdef", ROLES["simulator"], 200, 10, 5, 5, T0_MS)]
    assert assign_cause("done", {"http": "200"}, okp, ROLES) is None
    assert assign_cause("done", {"http": "200"}, okp + [llm_row(2, "vm-01-abcdef", "x/y", 200, 1, 1, 1, T0_MS)], ROLES) == "role_mismatch"
    assert assign_cause("done", {"http": "200"}, oks, ROLES) == "role_mismatch"          # policy never answered
    assert assign_cause("done", {"http": "200"}, [], ROLES) is None and assign_cause("done", {"http": "200"}, None, ROLES) is None
    assert assign_cause("done", {"http": "200"}, okp + rows5, ROLES) is None            # retried 5xx on a done trial
    assert assign_cause("failed", {"http": "200", "termination_reason": "agent_error"}, oks, ROLES) == "role_mismatch"
    # a done trial with a retried 5xx has no cause; truncated events skip the gateway rules
    assert read_events(os.path.join(FIX_RESULTS, "run_retail-0_27", "events.jsonl"))[0]["kind"] == "vm.boot"
    # the synthetic kinds produce exactly the expected causes
    import tempfile
    tmp = tempfile.mkdtemp(prefix="p10-self-")
    try:
        kinds = KINDS()
        plan = {str(i): [k] for i, k in enumerate(kinds)}
        fdir, results, runs = build_synthetic_fleet(tmp, "self", plan)
        m = matrix_oracle(fdir, read_json(PRICES))
        got = {t["run"]: t["cause"] for t in m["trials"]}
        for run, k in runs.items():
            assert got[run] == EXPECTED_CAUSE[k], (k, got[run], EXPECTED_CAUSE[k])
        by = {t["run"]: t for t in m["trials"]}
        tk = {v: k for k, v in runs.items()}
        assert by[tk["truncated_events"]]["llm"] == "unverified" and by[tk["truncated_events"]]["host_side_ms"] == "unverified"
        assert by[tk["no_events"]]["llm"] == "unverified" and by[tk["garbage_events"]]["llm"] == "unverified"
        assert by[tk["no_completeness"]]["m8"] == "unverified" and by[tk["unverified_trace"]]["m8"] == "unverified"
        assert by[tk["incomplete_trace"]]["m8"] == {"status": "incomplete", "ratio": 0.875}
        assert by[tk["failed_ctx"]]["valid"] and not by[tk["failed_404"]]["valid"] and not by[tk["no_job"]]["valid"]
        assert by[tk["unrouted_then_timeout"]]["cause"] == "model_unrouted"
        assert m["m12"]["unattributed"]["models"] == ["mystery/model-9b"] and m["m12"]["unattributed"]["calls"] == 2
        assert not by[tk["unattributed_model"]]["valid"] and by[tk["unattributed_model"]]["cause_class"] == "infra"
        assert m["m12"]["label_judge"]["usd_total"] == "unverified" and m["m12"]["policy"]["basis"] == "gpu_hours"
        for k in ("gateway_timeout", "gateway_5xx_unattributed"):
            assert by[tk["failed_" + k.replace("gateway_5xx_unattributed", "5xx_legacy")]]["cause_class"] == "agent"
        assert by[tk["failed_gateway_timeout"]]["valid"] and not by[tk["failed_unreachable"]]["valid"]
        lab = os.path.join(tmp, "labels")
        write_labels(lab, [(100, 10), (200, 20), (300, 30)], runs=[tk["done_pass"], "run_retail-0_999", "run_other-1_0"])
        m2 = matrix_oracle(fdir, read_json(PRICES), lab)
        assert m2["m12"]["label_judge"]["calls"] == 1 and m2["m12"]["label_judge"]["prompt_tokens"] == 100
        assert any("2 label rows" in w for w in m2["warnings"])
        write_labels(lab, [(100, 10)], runs=["run_other-1_0"])
        m3 = matrix_oracle(fdir, read_json(PRICES), lab)
        assert m3["m12"]["label_judge"]["calls"] == 0 and m3["m12"]["label_judge"]["usd_total"] == 0.0
        for k in ("done_retried_sim", "done_witness_tampered", "done_two_sims", "failed_retried_sim"):
            t = by[tk[k]]
            assert t["cause"] == "witness_mismatch" and t["cause_class"] == "infra" and not t["valid"] and t["passed"] is None, k
            assert t["m8"]["status"] == "witness_mismatch", t["m8"]
        assert by[tk["done_pass"]]["m8"]["status"] == "complete" and by[tk["done_length"]]["cause"] is None
        assert m["m8"]["incomplete"] >= 4
        for r in ("run_retail-0_22", "run_retail-7_4"):
            assert not witness_mismatch(os.path.join(FIX_RESULTS, r), read_events(os.path.join(FIX_RESULTS, r, "events.jsonl")), ROLES | {"nl_judge": "none"})
        assert m["m2"]["trials"] == len(kinds) and m["m8"]["unverified"] == 3
        # host-side time is wall minus the llm sum
        t = by[tk["done_pass"]]
        assert t["host_side_ms"] == t["wall_ms"] - t["llm"]["ms"] and t["llm"]["calls"] == sum(b["calls"] for b in t["llm"]["by_model"].values())
        # m12 arithmetic by hand for the simulator
        pr = read_json(PRICES)["models"][ROLES["simulator"]]
        s = m["m12"]["simulator"]
        assert math.isclose(s["usd_total"], (s["prompt_tokens"] * pr["prompt_per_mtok"] + s["completion_tokens"] * pr["completion_per_mtok"]) / 1e6)
        assert math.isclose(s["usd_per_task"], s["usd_total"] / m["m2"]["trials"])
        # m17: knee at 4
        sd = os.path.join(tmp, "sweep")
        for L, hs in ((1, 10_000), (2, 10_200), (4, 10_400), (8, 12_000), (16, 20_000)):
            plant_level(sd, L, [hs] * 5, llm_ms=50_000)
        o = m17_oracle(sd)
        assert o["m17"] == 4 and o["ceiling_reached"] and [l["concurrency"] for l in o["levels"]] == [1, 2, 4, 8, 16]
        shutil.rmtree(os.path.join(sd, "level_8")); shutil.rmtree(os.path.join(sd, "level_16"))
        o = m17_oracle(sd)
        assert o["m17"] == 4 and not o["ceiling_reached"]
        plant_level(sd, 8, [10_000] * 3, peak=None)
        assert m17_oracle(sd)["levels"][-1]["peak_guest_mem_mib"] == "unverified"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return True


def plant_level(sweep_dir, L, host_side_list, llm_ms=50_000, llm_ms_list=None, peak="auto", statuses=None, events=None):
    """A level directory whose done trials have the given host-side times (wall = host_side + llm)."""
    d = os.path.join(sweep_dir, f"level_{L}")
    results = os.path.join(sweep_dir, f"results_{L}")
    rnd = random.Random(L)
    lines = []
    for i, hs in enumerate(host_side_list):
        run = f"run_retail-0_{i}"
        lm = llm_ms_list[i] if llm_ms_list else llm_ms
        st = statuses[i] if statuses else "done"
        spec = {"status": st, "reward": 1.0, "wall_ms": hs + lm, "rows": [(ROLES["policy"], 200, lm, 100, 10)], "fit_wall": False}
        if st == "failed":
            spec.update({"reward": None, "termination_reason": "agent_error"})
        if events:
            spec["events"] = events[i]
        write_run(results, run, "0", spec, rnd, slot=f"vm-{(i % L) + 1:02d}", t_start_ms=T0_MS + i * 1000)
        lines.append(journal_line("0", run, f"vm-{(i % L) + 1:02d}", st, wall=hs + lm))
    write_fleet(d, ["0"], len(host_side_list), L, results, lines, peak=700.0 * L if peak == "auto" else peak, name=f"level_{L}")
    return d
