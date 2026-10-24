#!/usr/bin/env python3
"""POC 10 acceptance suite (fleet/CONTRACT.md). Black box: only fleet/bin/fleet, a fake infra/job the suite
controls, raw fixtures and (with --host) the real host over ssh are touched.

  python3 fleet/tests/run.py              full local run (includes the 10,000-trial scale check)
  python3 fleet/tests/run.py --quick      skips the 10,000-trial scale check
  python3 fleet/tests/run.py --selftest   proves the suite's own oracles and fake job do what they claim
  python3 fleet/tests/run.py --host       adds the live checks on avos-dev over ssh (repo pushed to ~/poc)
  python3 fleet/tests/run.py --only P     only checks whose id starts with P

Exit 0 only when every executed check passes. One line per check:
  PASS|FAIL|SKIP  <check id>  [<target>]  <measured vs threshold>
"""
import argparse
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lib

CHECKS = []
TH = {"stop_s": 60, "resume_wait_s": 120, "matrix_696_s": 20.0, "matrix_10k_s": 180.0, "rss_mib": 512,
      "m17_pct": 1.05, "host_trial_wait_s": 480, "host_resume_s": 720, "host_sweep_s": 600, "host_total_s": 900,
      "peak_mib_min_per_vm": 32, "peak_mib_max_per_vm": 1600}
HOST = "avos-dev"
HOST_REPO = "~/poc"
HOST_TMP = "/tmp/p10-tests"
HOST_MEM = 1024


def check(cid, target, scale=False, host=False):
    def deco(fn):
        CHECKS.append((cid, target, scale, host, fn))
        return fn
    return deco


class Ctx:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="poc10-tests-")
        self.transcripts = []
        self.ssh_ctl = None
        self.ssh_dir = None
        self.host_t0 = None
        self.baseline = None
        self.cache = {}
        self.prices = lib.read_json(lib.PRICES)

    def scratch(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    # --- local CLI
    def fleet(self, *args, env=None, expect_rc=None, timeout=900, rss=False):
        res = lib.fleet(*args, env=env, timeout=timeout, rss=rss)
        self.transcripts.append((res["out"] + res["err"], " ".join(map(str, args[:3]))))
        if not lib.no_traceback(res):
            last = [l for l in (res["err"] + res["out"]).strip().splitlines() if l.strip()][-1:]
            raise AssertionError(f"traceback ({' '.join(map(str, args[:2]))}): {lib.short(last[0] if last else '', 200)}")
        if expect_rc is not None and res["rc"] not in (expect_rc if isinstance(expect_rc, tuple) else (expect_rc,)):
            raise AssertionError(f"fleet {' '.join(map(str, args[:4]))}: rc={res['rc']} (want {expect_rc}) stderr={lib.short(res['err'], 300)!r} stdout={lib.short(res['out'], 200)!r}")
        return res

    def matrix_json(self, fdir, prices=None, labels=None, env=None, expect_rc=0, rss=False, timeout=900):
        args = ["matrix", "--fleet", fdir, "--prices", prices or lib.PRICES, "--json"]
        if labels:
            args += ["--labels", labels]
        res = self.fleet(*args, env=env, expect_rc=expect_rc, rss=rss, timeout=timeout)
        if expect_rc == 0:
            try:
                res["json"] = json.loads(res["out"])
            except ValueError:
                raise AssertionError(f"matrix --json stdout is not one JSON object: {lib.short(res['out'], 200)!r}")
        return res

    def env(self, results, job, plan=None, extra=None):
        plan_path = None
        if plan is not None:
            plan_path = os.path.join(self.tmp, f"plan_{len(self.transcripts)}_{time.time_ns()}.json")
            lib.write_json(plan_path, plan)
        return lib.fleet_env(self.tmp, results, job, plan_path, extra)

    def fake(self):
        if "fake" not in self.cache:
            self.cache["fake"] = lib.install_fake_job(self.tmp)
        return self.cache["fake"]

    # --- ssh (host checks)
    def ssh_base(self):
        if self.ssh_ctl is None:
            d = tempfile.mkdtemp(prefix="p10-", dir="/tmp")
            self.ssh_dir, self.ssh_ctl = d, os.path.join(d, "cm-%C")
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self.ssh_ctl}", "-o", "ControlPersist=300", HOST]

    def ssh(self, cmd, timeout=900, stdin=None):
        p = subprocess.run(self.ssh_base() + [cmd], capture_output=True, text=True, timeout=timeout, input=stdin)
        self.transcripts.append((p.stdout + p.stderr, "ssh"))
        return p

    def ssh_close(self):
        if self.ssh_ctl:
            subprocess.run(["ssh", "-o", f"ControlPath={self.ssh_ctl}", "-O", "exit", HOST], capture_output=True)
            self.ssh_ctl = None
            shutil.rmtree(self.ssh_dir, ignore_errors=True)

    def host_cleanup(self):
        """Remove the suite's scratch on the host and stop any runner it started (one-shot ssh, no control master)."""
        self.ssh_close()
        subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=no", "-o", "ControlPath=none", HOST,
                        f"sudo pkill -TERM -f 'fleet [rs](un|weep) --out {HOST_TMP}' 2>/dev/null; sleep 2; sudo rm -rf {HOST_TMP}"],
                       capture_output=True, timeout=120)


# ====================================================================== local checks

@check("A0_cli_surface", "CONTRACT §1 exit codes")
def a0(ctx):
    ctx.fleet(expect_rc=2)
    ctx.fleet("bogus", expect_rc=2)
    ctx.fleet("matrix", expect_rc=2)
    ctx.fleet("run", expect_rc=2)
    empty = ctx.scratch("a0_empty")
    ctx.fleet("matrix", "--fleet", empty, "--prices", lib.PRICES, "--json", expect_rc=2)
    ctx.fleet("m17", empty, expect_rc=2)
    fdir, _, _ = lib.build_synthetic_fleet(ctx.tmp, "a0", {"0": ["done_pass"]})
    bad = os.path.join(ctx.tmp, "a0_bad_prices.json")
    open(bad, "w").write("{not json")
    ctx.fleet("matrix", "--fleet", fdir, "--prices", bad, "--json", expect_rc=2)
    ctx.fleet("matrix", "--fleet", fdir, "--prices", os.path.join(ctx.tmp, "nope.json"), "--json", expect_rc=2)
    ctx.fleet("matrix", "--fleet", fdir, "--prices", lib.PRICES, "--json", expect_rc=0)
    return "7 usage errors exit 2, no traceback; a valid fleet exits 0"


@check("R1_refuse_held_out", "statement (held-out never run in POC 10)")
def r1(ctx):
    results = ctx.scratch("r1_results")
    env = ctx.env(results, ctx.fake())
    before = set(os.listdir(results))
    for tasks, part in (("10", "train"), ("0,10", "train"), ("64", "train"), ("64", "held_out"), ("0", "held_out"), ("999", "train")):
        out = os.path.join(ctx.tmp, f"r1_fleet_{tasks.replace(',', '_')}_{part}")
        res = ctx.fleet("run", "--out", out, "--tasks", tasks, "--part", part, "--trials", 1, "--concurrency", 1,
                        "--job", ctx.fake(), "--json", env=env, expect_rc=2)
        bad = [t for t in tasks.split(",") if t in ("10", "64", "999") or (part == "held_out" and t == "0")]
        if not any(b in (res["err"] + res["out"]) for b in bad):
            raise AssertionError(f"refusal of {tasks}/{part} does not name the offending task: {lib.short(res['err'], 200)!r}")
        if os.path.exists(os.path.join(out, "trials.jsonl")) or set(os.listdir(results)) != before:
            raise AssertionError(f"refused fleet {tasks}/{part} created something")
    return "6 refusals exit 2 naming the task, nothing created"


def _run_fleet(ctx, name, tasks, trials, conc, plan, roles=None, extra_env=None, expect_rc=0, results=None, timeout=300):
    results = results or ctx.scratch(f"{name}_results")
    out = os.path.join(ctx.tmp, f"{name}_fleet")
    env = ctx.env(results, ctx.fake(), plan, extra_env)
    roles = roles or lib.ROLES
    args = ["run", "--out", out, "--tasks", ",".join(tasks), "--trials", trials, "--concurrency", conc, "--job", ctx.fake(),
            "--policy", roles["policy"], "--simulator", roles["simulator"], "--judge", roles["nl_judge"], "--json"]
    res = ctx.fleet(*args, env=env, expect_rc=expect_rc, timeout=timeout)
    return out, results, env, res, args


def _journal(out):
    return lib.read_jsonl(os.path.join(out, "trials.jsonl")) if os.path.exists(os.path.join(out, "trials.jsonl")) else []


def _assert_fleet_dir(out, tasks, trials, conc, results):
    meta = lib.read_json(os.path.join(out, "fleet.json"))
    for k in ("fleet", "harness", "domain", "tasks", "trials_per_task", "concurrency", "mem_mib", "split", "roles",
              "results_root", "sessions", "status", "peak_guest_mem_mib"):
        if k not in meta:
            raise AssertionError(f"fleet.json lacks {k}")
    if meta["tasks"] != list(tasks) or meta["trials_per_task"] != trials or meta["concurrency"] != conc:
        raise AssertionError(f"fleet.json plan differs: {meta['tasks']} {meta['trials_per_task']} {meta['concurrency']}")
    if os.path.realpath(meta["results_root"]) != os.path.realpath(results):
        raise AssertionError(f"results_root {meta['results_root']} != {results}")
    if meta["split"].get("sha256") != lib.sha_file(lib.SPLIT) or meta["split"].get("part") != "train":
        raise AssertionError("fleet.json split sha/part wrong")
    if len({meta["roles"].get(r) for r in ("policy", "simulator", "nl_judge")}) != 3:
        raise AssertionError(f"roles not three distinct models: {meta['roles']}")
    for s in meta["sessions"]:
        for k in ("started", "ended", "pid"):
            if k not in s:
                raise AssertionError(f"session lacks {k}")
        lib.parse_ts(s["started"])
    return meta


def _assert_plan_complete(out, tasks, trials, results, preexisting=()):
    lines = _journal(out)
    live = [l for l in lines if not l.get("interrupted")]
    runs = [l["run"] for l in live]
    if len(set(runs)) != len(runs):
        raise AssertionError(f"duplicate run ids on non-interrupted lines: {sorted(set(r for r in runs if runs.count(r) > 1))}")
    per = {t: sum(1 for l in live if str(l["task"]) == t) for t in tasks}
    if any(per[t] != trials for t in tasks):
        raise AssertionError(f"non-interrupted trials per task {per}, want {trials} each")
    for l in lines:
        for k in ("task", "run", "slot", "status", "interrupted", "started", "ended"):
            if k not in l:
                raise AssertionError(f"journal line lacks {k}: {l}")
        d = os.path.join(results, l["run"])
        if not os.path.exists(os.path.join(d, "job.json")):
            raise AssertionError(f"{l['run']} has no job.json")
        job = lib.read_json(os.path.join(d, "job.json"))
        if job["status"] != l["status"]:
            raise AssertionError(f"{l['run']}: journal status {l['status']} != job.json {job['status']}")
        if not l.get("interrupted") and not os.path.exists(os.path.join(d, "completeness.json")):
            raise AssertionError(f"{l['run']} has no completeness.json")
        if not l.get("interrupted") and "status" not in lib.read_json(os.path.join(d, "completeness.json")):
            raise AssertionError(f"{l['run']}/completeness.json has no status")
    created = set(lib.fleet_dirs_of(results, tasks)) - set(preexisting)
    if created != set(l["run"] for l in lines):
        raise AssertionError(f"run directories not matching the journal: dirs-journal={sorted(created - set(runs))[:5]} journal-dirs={sorted(set(runs) - created)[:5]}")
    return lines


@check("R2_run_concurrency_slots", "done test 1 (runner at concurrency N, one SLOT per job)")
def r2(ctx):
    tasks, trials, conc = ["0", "1", "5"], 2, 2
    plan = {"default": {"kind": "done_pass", "sleep": 1.2}, "tasks": {"5": [{"kind": "done_pass", "sleep": 1.2}, {"kind": "failed_ctx", "sleep": 1.2}]}}
    roles = {"policy": "test/policy-9b", "simulator": "test/sim-27b", "nl_judge": "test/judge"}
    t0 = time.time()
    out, results, env, res, args = _run_fleet(ctx, "r2", tasks, trials, conc, plan, roles=roles)
    wall = time.time() - t0
    meta = _assert_fleet_dir(out, tasks, trials, conc, results)
    if meta["status"] != "done" or meta["roles"] != roles:
        raise AssertionError(f"status {meta['status']} roles {meta['roles']}")
    lines = _assert_plan_complete(out, tasks, trials, results)
    if any(l.get("interrupted") for l in lines):
        raise AssertionError("interrupted lines in an uninterrupted run")
    summ = json.loads(res["out"].strip().splitlines()[-1])
    if summ.get("planned") != 6 or summ.get("done") != 5 or summ.get("failed") != 1 or summ.get("status") != "done":
        raise AssertionError(f"summary {summ}")
    log = lib.fake_log(results)
    peak = lib.max_overlap(log)
    if peak != conc:
        raise AssertionError(f"peak concurrent jobs {peak}, want {conc}")
    if any(l["event"] == "slot_busy" for l in log):
        raise AssertionError("two jobs shared a SLOT at the same time")
    if wall >= 6 * 1.2:
        raise AssertionError(f"wall {wall:.1f} s: trials ran one at a time")
    for l in lines:
        e = lib.read_json(os.path.join(results, l["run"], "env.json"))
        if e["SLOT"] != l["slot"] or e["SLOT"] not in ("vm-01", "vm-02"):
            raise AssertionError(f"{l['run']}: SLOT {e['SLOT']} journal slot {l['slot']}")
        if (e["MODEL"], e["USER_MODEL"], e["JUDGE_MODEL"]) != (roles["policy"], roles["simulator"], roles["nl_judge"]):
            raise AssertionError(f"{l['run']}: roles not exported to the job: {e}")
    snap = lib.tree_snapshot(out)
    res2 = ctx.fleet(*args, env=env, expect_rc=0)
    if _journal(out) != lines or set(lib.fleet_dirs_of(results, tasks)) != set(l["run"] for l in lines):
        raise AssertionError("re-running a done fleet changed the journal or created directories")
    return f"6 trials in {wall:.1f} s, peak {peak} jobs, slots vm-01/vm-02, roles exported, re-run idle"


@check("R3_kill_resume", "done test 1 (restartable without duplicating or losing trials)")
def r3(ctx):
    tasks, trials, conc = ["0", "1"], 2, 2
    results = ctx.scratch("r3_results")
    rnd = random.Random(3)
    lib.write_run(results, "run_retail-0_0", "0", lib.KINDS()["done_pass"], rnd)  # pre-existing, not the fleet's
    pre = ["run_retail-0_0"]
    plan = {"default": {"kind": "done_pass", "sleep": 6}}
    out = os.path.join(ctx.tmp, "r3_fleet")
    env = ctx.env(results, ctx.fake(), plan)
    args = ["run", "--out", out, "--tasks", ",".join(tasks), "--trials", trials, "--concurrency", conc, "--job", ctx.fake(), "--json"]
    log = os.path.join(ctx.tmp, "r3.log")
    p = lib.fleet_popen(*args, env=env, log=log)
    try:
        started = lib.wait_until(lambda: p.poll() is not None or len([l for l in lib.fake_log(results) if l["event"] == "start"]) >= 2, 30)
        if not started or p.poll() is not None:
            raise AssertionError(f"no two jobs started within 30 s (runner rc={p.poll()})")
        time.sleep(1.0)
        p.send_signal(signal.SIGTERM)
        t0 = time.time()
        try:
            rc = p.wait(TH["stop_s"])
        except subprocess.TimeoutExpired:
            raise AssertionError(f"runner did not exit within {TH['stop_s']} s of SIGTERM")
        stop_s = time.time() - t0
    finally:
        if p.poll() is None:
            p.kill()
    flog = lib.fake_log(results)
    starts = {l["run"] for l in flog if l["event"] == "start"}
    ended = {l["run"] for l in flog if l["event"] in ("end", "aborted")}
    if starts - ended:
        raise AssertionError(f"jobs still running after the runner stopped: {sorted(starts - ended)}")
    if os.listdir(os.path.join(results, ".slots")):
        raise AssertionError("slot locks left behind")
    meta = lib.read_json(os.path.join(out, "fleet.json"))
    if meta["status"] != "stopped" or not meta["sessions"] or not meta["sessions"][-1].get("ended"):
        raise AssertionError(f"after SIGTERM: status {meta['status']}, sessions {meta['sessions']}")
    lines = _journal(out)
    inter = [l for l in lines if l.get("interrupted")]
    if not inter:
        raise AssertionError("no interrupted journal line after a mid-run SIGTERM")
    for l in inter:
        job = lib.read_json(os.path.join(results, l["run"], "job.json"))
        if job["status"] != "aborted" or l["status"] != "aborted":
            raise AssertionError(f"interrupted {l['run']} is {job['status']}/{l['status']}")
    n_inter = len(inter)
    t1 = time.time()
    res = ctx.fleet(*args, env=env, expect_rc=0, timeout=TH["resume_wait_s"])
    meta = lib.read_json(os.path.join(out, "fleet.json"))
    if meta["status"] != "done" or len(meta["sessions"]) < 2:
        raise AssertionError(f"after resume: status {meta['status']}, sessions {len(meta['sessions'])}")
    lines = _assert_plan_complete(out, tasks, trials, results, preexisting=pre)
    if any(l["run"] == "run_retail-0_0" for l in lines):
        raise AssertionError("pre-existing run_retail-0_0 was adopted by the fleet")
    if len([l for l in lines if l.get("interrupted")]) != n_inter:
        raise AssertionError("resume changed the interrupted lines")
    re_runs = len([l for l in lib.fake_log(results) if l["event"] == "start"]) - len(starts)
    missing = len(tasks) * trials - (len(starts) - n_inter)
    if re_runs != missing:
        raise AssertionError(f"resume started {re_runs} jobs; want exactly the {missing} missing (planned {len(tasks) * trials}, finished before stop {len(starts) - n_inter})")
    bad = ctx.fleet("run", "--out", out, "--tasks", "0,1", "--trials", 3, "--concurrency", 2, "--job", ctx.fake(), "--json", env=env, expect_rc=2)
    if set(lib.fleet_dirs_of(results, tasks)) != set(l["run"] for l in lines) | set(pre):
        raise AssertionError("a mismatched resume created directories")
    return f"stopped in {stop_s:.1f} s with {n_inter} interrupted, resumed to 4/4 unique trials in {time.time() - t1:.0f} s, mismatch refused"


@check("R4_sigkill_adopt", "§3 resume (orphans adopted, never duplicated)")
def r4(ctx):
    tasks, trials, conc = ["0", "7"], 2, 2
    results = ctx.scratch("r4_results")
    plan = {"default": {"kind": "done_fail", "sleep": 3}}
    out = os.path.join(ctx.tmp, "r4_fleet")
    env = ctx.env(results, ctx.fake(), plan)
    args = ["run", "--out", out, "--tasks", ",".join(tasks), "--trials", trials, "--concurrency", conc, "--job", ctx.fake(), "--json"]
    p = lib.fleet_popen(*args, env=env, log=os.path.join(ctx.tmp, "r4.log"))
    try:
        if not lib.wait_until(lambda: p.poll() is not None or len([l for l in lib.fake_log(results) if l["event"] == "start"]) >= 2, 30) or p.poll() is not None:
            raise AssertionError(f"no two jobs started within 30 s (runner rc={p.poll()})")
        time.sleep(0.5)
        p.kill()
        p.wait(10)
    finally:
        if p.poll() is None:
            p.kill()
    # the orphaned jobs finish on their own; wait for them (bounded), then plant one orphan directory without job.json
    ok = lib.wait_until(lambda: not ({l["run"] for l in lib.fake_log(results) if l["event"] == "start"}
                                     - {l["run"] for l in lib.fake_log(results) if l["event"] in ("end", "aborted")}), 30)
    if not ok:
        raise AssertionError("orphaned jobs did not finish within 30 s")
    orphan = "run_retail-7_9"
    os.makedirs(os.path.join(results, orphan))
    lib.write_json(os.path.join(results, orphan, "env.json"), {"SLOT": "vm-02"})
    journaled_before = {l["run"] for l in _journal(out)}
    finished = {l["run"] for l in lib.fake_log(results) if l["event"] == "end"}
    res = ctx.fleet(*args, env=env, expect_rc=0, timeout=TH["resume_wait_s"])
    lines = _assert_plan_complete(out, tasks, trials, results)
    runs = {l["run"]: l for l in lines}
    if orphan not in runs or not runs[orphan]["interrupted"]:
        raise AssertionError(f"orphan {orphan} not journaled as interrupted: {runs.get(orphan)}")
    oj = lib.read_json(os.path.join(results, orphan, "job.json"))
    if oj.get("status") != "aborted" or oj.get("stage") != "unknown":
        raise AssertionError(f"orphan job.json {oj}")
    adopted = finished - journaled_before
    for r in adopted:
        if r not in runs or runs[r]["interrupted"] or runs[r]["status"] != "done":
            raise AssertionError(f"finished-but-unjournaled {r} not adopted as it is: {runs.get(r)}")
    starts = [l["run"] for l in lib.fake_log(results) if l["event"] == "start"]
    if len(starts) != len(set(starts)) or len(starts) != len(tasks) * trials:
        raise AssertionError(f"jobs started: {len(starts)} (want exactly {len(tasks) * trials}, none twice)")
    return f"{len(adopted)} orphan trial(s) adopted, 1 directory without job.json journaled interrupted and re-run, 4 jobs total"


def _compare_matrix(ctx, got, fdir, prices=None, labels=None, parts=("m2", "m8", "m11", "m12", "m13", "m16", "trials", "warnings", "inputs")):
    want = lib.matrix_oracle(fdir, prices if prices is not None else ctx.prices, labels)
    want = {k: v for k, v in want.items() if k in parts}
    d = lib.diff(got, want)
    if d:
        raise AssertionError(f"{len(d)} differences from the oracle: " + "; ".join(d[:6]))
    if not isinstance(got.get("unverified"), list):
        raise AssertionError("matrix JSON has no `unverified` list")
    return want


def _unverified_paths(obj, path=""):
    out = []
    if obj == "unverified":
        return [path]
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _unverified_paths(v, f"{path}.{k}" if path else k)
    return out


@check("R5_busy_slot_isolation", "class A (a failing job never touches another job's resources)")
def r5(ctx):
    """Two fleets share one results root and are started with overlapping SLOT ranges (vm-01 twice): the second
    runner meets a busy slot. Whatever it does, the first fleet's in-flight trial must end `done` with its own
    directory intact, and the busy-slot trial must be journaled aborted at stage create with a cause, never done."""
    results = ctx.scratch("r5_results")
    plan = {"default": {"kind": "done_pass", "sleep": 5}}
    env = ctx.env(results, ctx.fake(), plan)
    out_a = os.path.join(ctx.tmp, "r5_a")
    out_b = os.path.join(ctx.tmp, "r5_b")
    pa = lib.fleet_popen("run", "--out", out_a, "--tasks", "0", "--trials", 1, "--concurrency", 1, "--job", ctx.fake(), "--json", env=env, log=os.path.join(ctx.tmp, "r5a.log"))
    try:
        if not lib.wait_until(lambda: pa.poll() is not None or any(l["event"] == "start" for l in lib.fake_log(results)), 30) or pa.poll() is not None:
            raise AssertionError(f"fleet A did not start a job (rc={pa.poll()})")
        run_a = [l for l in lib.fake_log(results) if l["event"] == "start"][0]["run"]
        snap_a = lib.tree_snapshot(os.path.join(results, run_a))
        # fleet B wants vm-01 too (concurrency 1 → the only slot it may use is busy) and must not break A
        rb = ctx.fleet("run", "--out", out_b, "--tasks", "1", "--trials", 1, "--concurrency", 1, "--job", ctx.fake(), "--json", env=env, expect_rc=(0, 1), timeout=120)
        rc_a = pa.wait(60)
    finally:
        if pa.poll() is None:
            pa.kill()
    ja = lib.read_json(os.path.join(results, run_a, "job.json"))
    if ja["status"] != "done" or rc_a != 0:
        raise AssertionError(f"fleet A's trial was disturbed by B's busy-slot attempt: status {ja['status']} rc {rc_a}")
    for rel, (n, sha) in snap_a.items():
        p = os.path.join(results, run_a, rel)
        if not os.path.exists(p) or lib.sha_file(p) != sha:
            raise AssertionError(f"B changed A's {rel}")
    lines_b = _journal(out_b)
    busy = [l for l in lines_b if lib.read_json(os.path.join(results, l["run"], "job.json")).get("error") == "slot busy"]
    for l in busy:
        if l["status"] != "aborted":
            raise AssertionError(f"busy-slot trial journaled as {l['status']}")
    g = ctx.matrix_json(out_b)["json"]
    by = {t["run"]: t for t in g["trials"]}
    for l in busy:
        if l.get("interrupted"):
            continue
        if by[l["run"]]["cause"] != "vm_create_failed" or by[l["run"]]["valid"]:
            raise AssertionError(f"busy-slot trial {l['run']}: cause {by[l['run']]['cause']}")
    if any(l["event"] == "slot_busy" and l["slot"] != "vm-01" for l in lib.fake_log(results)):
        raise AssertionError("a busy-slot refusal on a slot nobody held")
    return f"A's {run_a} done and byte-identical after B hit its slot; B: {len(busy)} busy-slot attempt(s) aborted at create, rc {rb['rc']}"


@check("R6_roles_reach_the_trial", "class B (declared roles = models in the llm.call rows)")
def r6(ctx):
    """The fake job records the roles it was given and writes rows under them. If the runner fails to pass a role,
    the rows carry the fake's defaults instead and the matrix must flag the trial, never count it."""
    roles = {"policy": "roles/policy-9b", "simulator": "roles/sim-27b", "nl_judge": "roles/judge"}
    out, results, env, res, args = _run_fleet(ctx, "r6", ["0", "1"], 2, 2, {"default": {"kind": "done_pass", "sleep": 0.2}}, roles=roles)
    g = ctx.matrix_json(out)["json"]
    _compare_matrix(ctx, g, out)
    for t in g["trials"]:
        if t["llm"] == "unverified":
            raise AssertionError(f"{t['run']}: no events")
        models = {m for m, b in t["llm"]["by_model"].items() if b["ok"]}
        if not models <= set(roles.values()) or roles["policy"] not in models:
            raise AssertionError(f"{t['run']}: rows under {sorted(models)}, declared {roles}")
        if t["cause"] is not None or not t["valid"]:
            raise AssertionError(f"{t['run']}: cause {t['cause']} on a trial whose rows match its roles")
    # the same artifacts with the declaration changed: every trial is role_mismatch, infra, invalid, M11 unverified
    meta = lib.read_json(os.path.join(out, "fleet.json"))
    meta["roles"] = {"policy": "declared/other-9b", "simulator": roles["simulator"], "nl_judge": roles["nl_judge"]}
    lib.write_json(os.path.join(out, "fleet.json"), meta)
    g = ctx.matrix_json(out)["json"]
    _compare_matrix(ctx, g, out)
    if any(t["cause"] != "role_mismatch" or t["cause_class"] != "infra" or t["valid"] or t["status"] != "done" for t in g["trials"]):
        raise AssertionError(f"declared policy absent from the rows: {[(t['cause'], t['valid']) for t in g['trials']]}")
    if g["m11"]["pass1"] != "unverified" or g["m11"]["causes"] != {"role_mismatch": 4} or g["m2"]["done"] != 4:
        raise AssertionError(f"M11 {g['m11']['pass1']} causes {g['m11']['causes']} M2 {g['m2']}")
    # a done trial with one extra row under an undeclared model is also a mismatch (synthetic shape)
    fdir, _, runs = lib.build_synthetic_fleet(ctx.tmp, "r6b", {"0": ["unattributed_model", "done_pass"]}, seed=6)
    g = ctx.matrix_json(fdir)["json"]
    _compare_matrix(ctx, g, fdir)
    by = {runs_k: t for t in g["trials"] for runs_k in [runs[t["run"]]]}
    if by["unattributed_model"]["cause"] != "role_mismatch" or by["done_pass"]["cause"] is not None:
        raise AssertionError(f"extra-model row: {by['unattributed_model']['cause']} / {by['done_pass']['cause']}")
    return "4 live trials: rows ⊆ declared roles and policy present; re-declared policy → 4 × role_mismatch (infra, M11 unverified); extra model row → role_mismatch"


@check("M10_label_rows_own_runs", "class A (M12 counts only evidence of the fleet's own trials)")
def m10(ctx):
    plan = {"0": ["done_pass", "done_pass"], "1": ["done_fail", "done_pass"]}
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, "m10", plan, seed=10)
    labels = os.path.join(ctx.tmp, "m10_labels")
    # 2 rows of this fleet + 3 of other runs (a POC 9 leftover, another fleet, a foreign domain)
    lib.write_labels(labels, [(1000, 100), (2000, 200), (5000, 500), (6000, 600), (7000, 700)],
                     runs=["run_retail-0_0", "run_retail-1_1", "run_retail-0_9", "run_retail-50_0", "run_airline-1_0"])
    g = ctx.matrix_json(fdir, labels=labels)["json"]
    _compare_matrix(ctx, g, fdir, labels=labels)
    lj = g["m12"]["label_judge"]
    if lj["calls"] != 2 or lj["prompt_tokens"] != 3000 or lj["completion_tokens"] != 300:
        raise AssertionError(f"label rows of other runs counted: {lj}")
    if not any("3 label rows" in w for w in g["warnings"]):
        raise AssertionError(f"foreign label rows not warned: {g['warnings']}")
    lib.write_labels(labels, [(5000, 500), (6000, 600)], runs=["run_retail-0_9", "run_retail-50_0"])
    g = ctx.matrix_json(fdir, labels=labels)["json"]
    _compare_matrix(ctx, g, fdir, labels=labels)
    lj = g["m12"]["label_judge"]
    if lj["calls"] != 0 or lj["usd_total"] != 0.0:
        raise AssertionError(f"label judge never ran over this fleet, yet: {lj}")
    return "2 own + 3 foreign rows → 2 calls, 3 warned; only foreign rows → 0 calls / $0.0"


@check("M11_gateway_evidence_causes", "class B (upstream error vs gateway timeout vs length, M11 never inflated)")
def m11(ctx):
    plan = {"0": ["failed_gateway_timeout", "done_pass", "done_pass", "done_pass"],
            "1": ["failed_5xx", "done_pass", "done_pass", "done_pass"],
            "5": ["failed_unreachable", "failed_5xx_legacy", "failed_length", "failed_length_then_timeout"],
            "7": ["done_length", "done_pass", "done_pass", "done_pass"]}
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, "m11", plan, seed=12)
    g = ctx.matrix_json(fdir)["json"]
    _compare_matrix(ctx, g, fdir)
    by = {runs[t["run"]]: t for t in g["trials"]}
    want = {"failed_gateway_timeout": ("gateway_timeout", "agent", True), "failed_5xx": ("upstream_5xx", "infra", False),
            "failed_unreachable": ("upstream_unreachable", "infra", False), "failed_5xx_legacy": ("gateway_5xx_unattributed", "agent", True),
            "failed_length": ("agent_length", "agent", True), "failed_length_then_timeout": ("gateway_timeout", "agent", True),
            "done_length": (None, None, True)}
    for k, (c, cls, valid) in want.items():
        t = by[k]
        if (t["cause"], t["cause_class"], t["valid"]) != (c, cls, valid):
            raise AssertionError(f"{k}: got {(t['cause'], t['cause_class'], t['valid'])}, want {(c, cls, valid)}")
    tk = {p["task"]: p for p in g["m11"]["tasks"]}
    if tk["0"]["n"] != 4 or tk["0"]["c"] != 3 or abs(tk["0"]["pass1"] - 0.75) > 1e-9:
        raise AssertionError(f"a gateway timeout must stay in n as a failure: {tk['0']}")
    if tk["1"]["n"] != 3 or tk["1"]["infra"] != 1 or abs(tk["1"]["pass1"] - 1.0) > 1e-9:
        raise AssertionError(f"a real upstream 5xx is excluded: {tk['1']}")
    if g["m11"]["infra_excluded"] != 2:
        raise AssertionError(f"infra_excluded {g['m11']['infra_excluded']} (want 2: one upstream_5xx, one unreachable)")
    return "timeout/legacy 5xx/length stay in n (pass^1 0.75), upstream 5xx and unreachable excluded, done+length no cause"


@check("R7_slot_ranges", "class C (two consumers on one host never share a slot)")
def r7(ctx):
    results = ctx.scratch("r7_results")
    plan = {"default": {"kind": "done_pass", "sleep": 1.5}}
    env = ctx.env(results, ctx.fake(), plan)
    out_a, out_b = os.path.join(ctx.tmp, "r7_a"), os.path.join(ctx.tmp, "r7_b")
    pa = lib.fleet_popen("run", "--out", out_a, "--tasks", "0,1", "--trials", 2, "--concurrency", 2, "--slots", "vm-03-vm-04", "--job", ctx.fake(), "--json", env=env, log=os.path.join(ctx.tmp, "r7a.log"))
    pb = lib.fleet_popen("run", "--out", out_b, "--tasks", "5,7", "--trials", 2, "--concurrency", 2, "--slots", "vm-05-vm-06", "--job", ctx.fake(), "--json", env=env, log=os.path.join(ctx.tmp, "r7b.log"))
    try:
        ra, rb = pa.wait(120), pb.wait(120)
    finally:
        for p in (pa, pb):
            if p.poll() is None:
                p.kill()
    if ra != 0 or rb != 0:
        raise AssertionError(f"fleets exited {ra}/{rb}")
    log = lib.fake_log(results)
    if any(l["event"] == "slot_busy" for l in log):
        raise AssertionError("a job met a busy slot although the ranges are disjoint")
    if lib.max_overlap(log) < 3:
        raise AssertionError(f"peak overlap {lib.max_overlap(log)}: the two fleets did not run side by side")
    for out, rng in ((out_a, {"vm-03", "vm-04"}), (out_b, {"vm-05", "vm-06"})):
        meta = lib.read_json(os.path.join(out, "fleet.json"))
        if set(meta.get("slots", [])) != rng:
            raise AssertionError(f"fleet.json slots {meta.get('slots')} ≠ {sorted(rng)}")
        for l in _journal(out):
            e = lib.read_json(os.path.join(results, l["run"], "env.json"))
            if e["SLOT"] not in rng or l["slot"] != e["SLOT"]:
                raise AssertionError(f"{l['run']} ran on {e['SLOT']} outside {sorted(rng)}")
    if {l["slot"] for l in log if l["event"] == "start"} & {"vm-01", "vm-02"}:
        raise AssertionError("vm-01/vm-02 were used although both ranges exclude them")
    ctx.fleet("run", "--out", os.path.join(ctx.tmp, "r7_c"), "--tasks", "0", "--trials", 1, "--concurrency", 3, "--slots", "vm-03-vm-04", "--job", ctx.fake(), "--json", env=env, expect_rc=2)
    ctx.fleet("run", "--out", out_a, "--tasks", "0,1", "--trials", 2, "--concurrency", 2, "--slots", "vm-07-vm-08", "--job", ctx.fake(), "--json", env=env, expect_rc=2)
    return "two fleets on vm-03..04 and vm-05..06 side by side, no busy slot, vm-01/02 untouched; range < concurrency and a changed range on resume refused"


@check("M12_one_simulation_per_trial", "class D (a trial is exactly one simulation; store = witness)")
def m12(ctx):
    plan = {"0": ["done_retried_sim", "done_pass", "done_pass", "done_pass"], "1": ["done_witness_tampered", "done_two_sims", "failed_retried_sim", "done_pass"]}
    # failed_retried_sim: a failed trial keeps τ²'s own cause (agent_agent_error); the witness rule is for done trials only
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, "m12", plan, seed=13)
    g = ctx.matrix_json(fdir)["json"]
    _compare_matrix(ctx, g, fdir)
    by = {runs[t["run"]]: t for t in g["trials"]}
    if by["failed_retried_sim"]["cause"] != "agent_agent_error" or not by["failed_retried_sim"]["valid"]:
        raise AssertionError(f"failed trial swallowed by the witness rule: {by['failed_retried_sim']['cause']}")
    for k in ("done_retried_sim", "done_witness_tampered", "done_two_sims"):
        t = by[k]
        if t["cause"] != "witness_mismatch" or t["cause_class"] != "infra" or t["valid"] or t["passed"] is not None or t["m8"]["status"] != "witness_mismatch":
            raise AssertionError(f"{k}: cause {t['cause']} class {t['cause_class']} valid {t['valid']} passed {t['passed']} m8 {t['m8']}")
    if by["done_retried_sim"]["status"] != "done" or by["done_pass"]["cause"] is not None:
        raise AssertionError("status must stay done for M2; a matching witness has no cause")
    tk = {p["task"]: p for p in g["m11"]["tasks"]}
    if tk["0"]["n"] != 3 or tk["0"]["infra"] != 1 or tk["1"]["n"] != 2 or g["m11"]["infra_excluded"] != 3:
        raise AssertionError(f"M11 {tk['0']} {tk['1']} excluded {g['m11']['infra_excluded']}")
    if g["m8"]["complete"] != 5 or g["m8"]["incomplete"] != 3 or g["m2"]["done"] != 7:
        raise AssertionError(f"M8 {g['m8']} M2 {g['m2']}")
    return "retried simulation, tampered usage, two simulations → witness_mismatch (infra, excluded, M8 not complete); failed+retried keeps τ²'s agent cause; M2 7/8"


def _overlap_per_window(log, runs):
    """Peak concurrent fake jobs among the given run ids."""
    return lib.max_overlap([l for l in log if l.get("run") in runs])


@check("R8_concurrency_cap_wide_slots", "class E (live jobs ≤ concurrency whatever the slot range; sweep levels)")
def r8(ctx):
    results = ctx.scratch("r8_results")
    plan = {"default": {"kind": "done_pass", "sleep": 1.0}}
    env = ctx.env(results, ctx.fake(), plan)
    out = os.path.join(ctx.tmp, "r8_fleet")
    # 12 slots offered, concurrency 3, 8 trials
    ctx.fleet("run", "--out", out, "--tasks", "0,1", "--trials", 4, "--concurrency", 3, "--slots", "vm-03-vm-14", "--job", ctx.fake(), "--json", env=env, expect_rc=0, timeout=120)
    log = lib.fake_log(results)
    peak = lib.max_overlap(log)
    if peak != 3:
        raise AssertionError(f"peak live jobs {peak} with 12 slots and --concurrency 3 (want exactly 3)")
    used = {l["slot"] for l in log if l["event"] == "start"}
    if not used <= {f"vm-{n:02d}" for n in range(3, 15)}:
        raise AssertionError(f"slots outside the range: {sorted(used)}")
    # resume path: kill mid-run and resume, cap still holds in the resumed session
    results2 = ctx.scratch("r8b_results")
    env2 = ctx.env(results2, ctx.fake(), {"default": {"kind": "done_pass", "sleep": 2.0}})
    out2 = os.path.join(ctx.tmp, "r8b_fleet")
    args = ["run", "--out", out2, "--tasks", "0,1,5", "--trials", 3, "--concurrency", 2, "--slots", "vm-03-vm-20", "--job", ctx.fake(), "--json"]
    p = lib.fleet_popen(*args, env=env2, log=os.path.join(ctx.tmp, "r8b.log"))
    try:
        if not lib.wait_until(lambda: p.poll() is not None or len([l for l in lib.fake_log(results2) if l["event"] == "end"]) >= 2, 60) or p.poll() is not None:
            raise AssertionError(f"runner ended early (rc={p.poll()})")
        p.send_signal(signal.SIGTERM)
        p.wait(TH["stop_s"])
    finally:
        if p.poll() is None:
            p.kill()
    before = {l["run"] for l in lib.fake_log(results2) if l["event"] == "start"}
    ctx.fleet(*args, env=env2, expect_rc=0, timeout=120)
    log2 = lib.fake_log(results2)
    resumed = {l["run"] for l in log2 if l["event"] == "start"} - before
    if len(resumed) < 3 or _overlap_per_window(log2, resumed) != 2 or lib.max_overlap(log2) > 2:
        raise AssertionError(f"resume: {len(resumed)} jobs, peak {_overlap_per_window(log2, resumed)} (want 2), overall peak {lib.max_overlap(log2)}")
    # sweep: each level's peak equals its level, with 10 slots offered
    results3 = ctx.scratch("r8c_results")
    env3 = ctx.env(results3, ctx.fake(), {"default": {"kind": "done_pass", "sleep": 0.8}})
    sd = os.path.join(ctx.tmp, "r8_sweep")
    res = ctx.fleet("sweep", "--out", sd, "--levels", "1,2,4", "--tasks", "0,1", "--trials", 2, "--slots", "vm-03-vm-12", "--job", ctx.fake(), "--json", env=env3, expect_rc=0, timeout=300)
    log3 = lib.fake_log(results3)
    for L in (1, 2, 4):
        d = os.path.join(sd, f"level_{L}")
        meta = lib.read_json(os.path.join(d, "fleet.json"))
        runs = {l["run"] for l in _journal(d)}
        pk = _overlap_per_window(log3, runs)
        if meta["concurrency"] != L or pk != L:
            raise AssertionError(f"level {L}: fleet.json concurrency {meta['concurrency']}, peak live jobs {pk} (want {L})")
    if lib.max_overlap(log3) > 4:
        raise AssertionError(f"sweep overall peak {lib.max_overlap(log3)} > 4: levels overlapped or a level overshot")
    return "run: 12 slots / C=3 → peak 3; resume: 18 slots / C=2 → peak 2; sweep 1,2,4 on 10 slots → peaks 1/2/4"


@check("M13_empty_reply_is_agent", "class F (τ² infrastructure_error caused by the agent's own reply stays in n)")
def m13(ctx):
    plan = {"0": ["failed_empty_reply", "done_pass", "done_pass", "done_pass"],
            "1": ["failed_empty_reply_after_5xx", "done_pass", "done_pass", "done_pass"],
            "5": ["failed_judge_length", "done_pass", "done_pass", "done_pass"],
            "7": ["failed_empty_reply", "failed_empty_reply", "done_pass", "done_pass"]}
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, "m13", plan, seed=14)
    g = ctx.matrix_json(fdir)["json"]
    _compare_matrix(ctx, g, fdir)
    by = {t["run"]: t for t in g["trials"]}
    for run, k in runs.items():
        want = {"failed_empty_reply": ("agent_empty_reply", "agent", True), "failed_empty_reply_after_5xx": ("upstream_5xx", "infra", False),
                "failed_judge_length": ("sim_infrastructure_error", "infra", False), "done_pass": (None, None, True)}[k]
        got = (by[run]["cause"], by[run]["cause_class"], by[run]["valid"])
        if got != want:
            raise AssertionError(f"{k}: {got}, want {want}")
    tk = {p["task"]: p for p in g["m11"]["tasks"]}
    if tk["0"]["n"] != 4 or abs(tk["0"]["pass1"] - 0.75) > 1e-9 or tk["7"]["n"] != 4 or abs(tk["7"]["pass1"] - 0.5) > 1e-9:
        raise AssertionError(f"empty replies must stay in n as failures: {tk['0']} {tk['7']}")
    if tk["1"]["n"] != 3 or tk["5"]["n"] != 3 or g["m11"]["infra_excluded"] != 2:
        raise AssertionError(f"upstream 5xx / judge length before τ²'s error are infra: {tk['1']} {tk['5']} excluded {g['m11']['infra_excluded']}")
    if g["m11"]["causes"].get("agent_empty_reply") != 3:
        raise AssertionError(f"causes {g['m11']['causes']}")
    return "3 empty replies → agent_empty_reply in n (pass^1 0.75 / 0.5); 5xx-then-τ²-error and judge-length-then-τ²-error → infra"


@check("M14_timeout_evidence", "class G (a timed-out trial leaves n only when the upstream stalled)")
def m14(ctx):
    plan = {"0": ["timeout_agent_looping", "done_pass", "done_pass", "done_pass"],
            "1": ["timeout_upstream_stalled", "done_pass", "done_pass", "done_pass"],
            "5": ["timeout", "timeout_no_close_row", "done_pass", "done_pass"],
            "7": ["timeout_agent_looping", "timeout_agent_looping", "done_pass", "done_pass"]}
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, "m14", plan, seed=15)
    g = ctx.matrix_json(fdir)["json"]
    _compare_matrix(ctx, g, fdir)
    by = {t["run"]: t for t in g["trials"]}
    want = {"timeout_agent_looping": ("agent_timeout", "agent", True), "timeout_upstream_stalled": ("harness_timeout", "infra", False),
            "timeout": ("harness_timeout", "infra", False), "timeout_no_close_row": ("agent_timeout", "agent", True), "done_pass": (None, None, True)}
    for run, k in runs.items():
        got = (by[run]["cause"], by[run]["cause_class"], by[run]["valid"])
        if got != want[k]:
            raise AssertionError(f"{k}: {got}, want {want[k]}")
    tk = {p["task"]: p for p in g["m11"]["tasks"]}
    if tk["0"]["n"] != 4 or abs(tk["0"]["pass1"] - 0.75) > 1e-9 or tk["7"]["n"] != 4 or abs(tk["7"]["pass1"] - 0.5) > 1e-9:
        raise AssertionError(f"looping agents must stay in n: {tk['0']} {tk['7']}")
    if tk["1"]["n"] != 3 or tk["5"]["n"] != 3 or g["m11"]["infra_excluded"] != 2:
        raise AssertionError(f"stalled upstream / no policy rows are infra: {tk['1']} {tk['5']} excluded {g['m11']['infra_excluded']}")
    return "healthy policy calls until the clock → agent_timeout in n (pass^1 0.75 / 0.5); ≥ 5 min without a successful policy call, or no policy row → harness_timeout excluded"


@check("M1_matrix_real_fixtures", "done test 2 (matrix from real run directories)")
def m1(ctx):
    fdir = os.path.join(ctx.tmp, "m1_fleet")
    lines = [lib.journal_line("0", "run_retail-0_22", "vm-01", "done", t0=lib.T0_MS, wall=21286),
             lib.journal_line("0", "run_retail-0_26", "vm-01", "aborted", t0=lib.T0_MS + 30_000, wall=54),
             lib.journal_line("0", "run_retail-0_27", "vm-01", "done", t0=lib.T0_MS + 31_000, wall=25151),
             lib.journal_line("1", "run_retail-1_5", "vm-02", "done", t0=lib.T0_MS + 1_000, wall=23210),
             lib.journal_line("7", "run_retail-7_4", "vm-02", "done", t0=lib.T0_MS + 60_000, wall=127891)]
    lib.write_fleet(fdir, ["0", "1", "7"], 3, 2, lib.FIX_RESULTS, lines, status="stopped", name="real",
                    sessions=[{"started": lib.ts_of(lib.T0_MS), "ended": lib.ts_of(lib.T0_MS + 200_000), "pid": 1}])
    snap = lib.tree_snapshot(lib.FIX_RESULTS)
    res = ctx.matrix_json(fdir)
    want = _compare_matrix(ctx, res["json"], fdir)
    if lib.tree_snapshot(lib.FIX_RESULTS) != snap:
        raise AssertionError("matrix wrote into the results root")
    g = res["json"]
    by = {t["run"]: t for t in g["trials"]}
    if by["run_retail-0_26"]["cause"] != "vm_create_failed" or by["run_retail-7_4"]["cause"] is not None:
        raise AssertionError("real causes wrong")
    if by["run_retail-7_4"]["llm"]["calls"] != 102 or by["run_retail-0_22"]["llm"]["calls"] != 14:
        raise AssertionError("llm.call counts from the real events differ from the gateway's 102/14")
    if g["m2"]["done"] != 4 or g["m2"]["trials"] != 5 or abs(g["m2"]["value"] - 0.8) > 1e-9:
        raise AssertionError(f"M2 {g['m2']}")
    if g["m8"]["unverified"] != 5 or g["m8"]["mean_ratio"] != "unverified":
        raise AssertionError(f"M8 of runs without completeness.json must be unverified, got {g['m8']}")
    text = ctx.fleet("matrix", "--fleet", fdir, "--prices", lib.PRICES, expect_rc=0)["out"]
    if "4/5" not in text and "4 / 5" not in text:
        raise AssertionError("text matrix does not show M2 as 4/5")
    if "unverified" not in text or "vm_create_failed" not in text:
        raise AssertionError("text matrix lacks `unverified` or the cause")
    return f"5 real trials: M2 4/5, 102+14+9+9 llm rows, causes and M11–M16 equal the oracle, M8 unverified ×5"


@check("M2_synthetic_all_kinds", "§5 one cause per non-done trial, M11 validity, M12 roles")
def m2(ctx):
    kinds = list(lib.KINDS())
    plan = {}
    rnd = random.Random(11)
    for i, k in enumerate(kinds):
        plan.setdefault(str(i % 8), []).append(k)
    plan["8"] = ["aborted_create", "failed_5xx", "no_job", "timeout"]          # all infra
    plan["9"] = ["done_pass", "done_pass", "failed_404"]                        # short of 4 valid
    plan["13"] = ["done_pass", "done_fail", "done_pass", "failed_ctx", "done_pass"]
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, "m2", plan, seed=11)
    res = ctx.matrix_json(fdir)
    g = res["json"]
    want = _compare_matrix(ctx, g, fdir)
    by = {t["run"]: t for t in g["trials"]}
    for run, k in runs.items():
        if by[run]["cause"] != lib.EXPECTED_CAUSE[k]:
            raise AssertionError(f"{k}: cause {by[run]['cause']}, want {lib.EXPECTED_CAUSE[k]}")
    nd = [t for t in g["trials"] if t["status"] != "done"]
    if any(t["cause"] is None for t in nd) or any(t["cause"] not in (None, "role_mismatch", "witness_mismatch") for t in g["trials"] if t["status"] == "done"):
        raise AssertionError("not exactly one cause per non-done trial (done trials: none, role_mismatch or witness_mismatch)")
    tk = {p["task"]: p for p in g["m11"]["tasks"]}
    if tk["8"]["n"] != 0 or "8" not in g["m11"]["tasks_all_infra"] or tk["8"]["pass4"] is not None:
        raise AssertionError(f"all-infra task: {tk['8']}")
    if tk["9"]["n"] != 2 or tk["9"]["pass4"] is not None or "9" not in g["m11"]["tasks_short_4"] or tk["9"]["pass1"] != 1.0:
        raise AssertionError(f"short task: {tk['9']}")
    if tk["13"]["n"] != 5 or tk["13"]["c"] != 3 or abs(tk["13"]["pass4"] - 0.0) > 1e-9 or abs(tk["13"]["avg4"] - 0.6) > 1e-9:
        raise AssertionError(f"task 13: {tk['13']}")
    un = g["m12"]["unattributed"]
    if un["models"] != ["mystery/model-9b"] or un["calls"] != 2:
        raise AssertionError(f"unattributed {un}")
    if not any("mystery/model-9b" in w for w in g["warnings"]):
        raise AssertionError("unattributed model not in warnings")
    lj = g["m12"]["label_judge"]
    if lj["outside_gateway"] is not True or lj["usd_total"] != "unverified":
        raise AssertionError(f"label judge without rows: {lj}")
    up = set(_unverified_paths(g))
    for p in ("m12.label_judge.usd_total",):
        if not any(u.startswith(p) for u in g["unverified"]):
            raise AssertionError(f"`unverified` list lacks {p}: {g['unverified'][:8]}")
    return f"{len(runs)} trials of {len(kinds)} shapes: every cause as expected, short/all-infra tasks reported, roles split"


@check("M3_passk_oracle", "M11 arithmetic (pass^1, pass^4, Avg@4, bootstrap interval)")
def m3(ctx):
    pool = ["done_pass", "done_pass", "done_fail", "failed_ctx", "failed_404", "aborted_create", "done_pass", "failed_agent_error"]
    worst = 0
    for seed in (1, 2, 3):
        rnd = random.Random(seed)
        plan = {str(t): [rnd.choice(pool) for _ in range(rnd.choice([4, 4, 4, 5, 3, 8]))] for t in rnd.sample(range(0, 60), 12)}
        fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, f"m3_{seed}", plan, seed=seed)
        g = ctx.matrix_json(fdir)["json"]
        want = lib.matrix_oracle(fdir, ctx.prices)
        d = lib.diff(g["m11"], want["m11"])
        if d:
            raise AssertionError(f"seed {seed}: " + "; ".join(d[:5]))
        for key in ("pass1", "pass4", "avg4"):
            ci = g["m11"][key + "_ci95"]
            v = g["m11"][key]
            if ci != "unverified" and not (ci[0] - 1e-9 <= v <= ci[1] + 1e-9):
                raise AssertionError(f"{key} {v} outside its interval {ci}")
        worst = max(worst, len(g["m11"]["tasks"]))
    return f"3 random fleets × 12 tasks: per-task and overall pass^1/pass^4/Avg@4 and ci95 exact (1e-9)"


@check("M4_unverified_everywhere", "statement (missing part → unverified, never 100 % or 0)")
def m4(ctx):
    plan = {"0": ["done_pass", "done_fail", "done_pass", "done_pass"], "1": ["done_pass", "failed_ctx", "done_fail", "done_pass"]}
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, "m4", plan, seed=4)
    empty = os.path.join(ctx.tmp, "m4_prices_empty.json")
    lib.write_json(empty, {})
    g = ctx.matrix_json(fdir, prices=empty)["json"]
    _compare_matrix(ctx, g, fdir, prices={})
    for role in ("policy", "simulator", "nl_judge"):
        if g["m12"][role]["usd_total"] != "unverified" or g["m12"][role]["usd_per_task"] != "unverified":
            raise AssertionError(f"{role} priced without prices: {g['m12'][role]}")
    if g["m16"]["value"] != "unverified" or g["m16"]["baselines"] != {}:
        raise AssertionError(f"M16 without host price: {g['m16']}")
    if g["m2"]["value"] == "unverified" or g["m11"]["pass1"] == "unverified":
        raise AssertionError("M2/M11 need no prices")
    two = os.path.join(ctx.tmp, "m4_prices_two_gpu.json")
    pp2 = json.loads(json.dumps(ctx.prices))
    pp2["hosts"]["gpu"]["count"] = 2
    lib.write_json(two, pp2)
    g1 = ctx.matrix_json(fdir)["json"]
    g2 = ctx.matrix_json(fdir, prices=two)["json"]
    _compare_matrix(ctx, g2, fdir, prices=pp2)
    gh = g2["m12"]["policy"]["gpu_hosts"]
    if gh["count"] != 2 or abs(gh["usd_per_h_total"] - 2 * ctx.prices["hosts"]["gpu"]["usd_per_h"]) > 1e-9 \
            or abs(g2["m12"]["policy"]["usd_total"] - 2 * g1["m12"]["policy"]["usd_total"]) > 1e-9:
        raise AssertionError(f"two GPU hosts must double the policy cost and be printed: {gh} {g2['m12']['policy']['usd_total']} vs {g1['m12']['policy']['usd_total']}")
    text = ctx.fleet("matrix", "--fleet", fdir, "--prices", two, expect_rc=0)["out"]
    if not re.search(r"\b2\b.{0,40}(host|GPU)|(host|GPU).{0,40}\b2\b", text, re.I):
        raise AssertionError("text matrix does not show the GPU host count next to the rate")
    partial = os.path.join(ctx.tmp, "m4_prices_partial.json")
    pp = json.loads(json.dumps(ctx.prices))
    del pp["models"][lib.ROLES["simulator"]]
    del pp["hosts"]["gpu"]
    pp["baselines"]["aws_t3_small_per_vm"].pop("boot_s")
    lib.write_json(partial, pp)
    g = ctx.matrix_json(fdir, prices=partial)["json"]
    _compare_matrix(ctx, g, fdir, prices=pp)
    if g["m12"]["simulator"]["usd_total"] != "unverified" or g["m12"]["nl_judge"]["usd_total"] == "unverified":
        raise AssertionError("missing one model price must only unverify that role")
    if g["m12"]["policy"]["usd_total"] != "unverified" or g["m12"]["policy"]["token_ceiling_usd_per_task"] == "unverified":
        raise AssertionError("policy without GPU price: gpu cost unverified, token ceiling still computed")
    if g["m16"]["baselines"]["aws_t3_small_per_vm"] != "unverified" or g["m16"]["baselines"]["aws_agentcore_v2"] == "unverified":
        raise AssertionError(f"baseline with a missing field: {g['m16']['baselines']}")
    # all-infra fleet: pass^1 is unverified, never 0; M13/M16 unverified
    fdir2, _, _ = lib.build_synthetic_fleet(ctx.tmp, "m4b", {"0": ["aborted_create", "timeout"], "1": ["no_job", "failed_5xx"]}, seed=5)
    g = ctx.matrix_json(fdir2)["json"]
    _compare_matrix(ctx, g, fdir2)
    if g["m11"]["pass1"] != "unverified" or g["m11"]["pass1_ci95"] != "unverified" or g["m13"]["median_s"] != "unverified" or g["m16"]["value"] != "unverified":
        raise AssertionError(f"all-infra fleet: pass1={g['m11']['pass1']} m13={g['m13']} m16={g['m16']['value']}")
    if g["m2"]["value"] != 0.0 or g["m2"]["trials"] != 4:
        raise AssertionError(f"M2 of an all-failed fleet is 0/4, got {g['m2']}")
    for role in ("policy", "simulator", "nl_judge"):
        if g["m12"][role]["usd_per_successful_task"] != "unverified":
            raise AssertionError("cost per successful task with pass^1 unverified must be unverified")
    # label judge rows
    labels = os.path.join(ctx.tmp, "m4_labels")
    lib.write_labels(labels, [(1200, 300), (900, 250), (1500, 100)], runs=["run_retail-0_0", "run_retail-0_1", "run_retail-1_2"])
    g = ctx.matrix_json(fdir, labels=labels)["json"]
    want = _compare_matrix(ctx, g, fdir, labels=labels)
    lj = g["m12"]["label_judge"]
    pr = ctx.prices["models"][lib.LABEL_MODEL]
    if lj["model"] != lib.LABEL_MODEL or abs(lj["usd_total"] - (3600 * pr["prompt_per_mtok"] + 650 * pr["completion_per_mtok"]) / 1e6) > 1e-9:
        raise AssertionError(f"label judge rows: {lj}")
    if lj["outside_gateway"] is not True or lj["source"] != "label rows":
        raise AssertionError("label judge must be flagged outside the gateway")
    text = ctx.fleet("matrix", "--fleet", fdir, "--prices", empty, expect_rc=0)["out"]
    if "unverified" not in text or re.search(r"\bnull\b|\bNone\b", text):
        raise AssertionError("text output with empty prices must say unverified, never null/None")
    return "empty, partial and full prices, all-infra fleet, label rows: every missing part reads unverified"


@check("M5_determinism", "done test 2 (same artifacts → byte-identical matrix)")
def m5(ctx):
    plan = {str(t): ["done_pass", "done_fail", "failed_404", "done_pass"] for t in (0, 1, 5, 7)}
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, "m5", plan, seed=55)
    outs, texts = [], []
    for i in range(3):
        env = lib.fleet_env(ctx.tmp, results, None, extra={"TMPDIR": ctx.scratch(f"m5_tmp{i}"), "HOME": ctx.scratch(f"m5_home{i}")})
        outs.append(ctx.matrix_json(fdir, env=env)["out"])
        texts.append(ctx.fleet("matrix", "--fleet", fdir, "--prices", lib.PRICES, env=env, expect_rc=0)["out"])
    copy = os.path.join(ctx.tmp, "m5_copy", "elsewhere", "fleet")
    shutil.copytree(fdir, copy)
    for p in lib.files_under(copy):
        os.utime(p, (1, 1))
    outs.append(ctx.matrix_json(copy)["out"])
    texts.append(ctx.fleet("matrix", "--fleet", copy, "--prices", lib.PRICES, expect_rc=0)["out"])
    if len(set(outs)) != 1 or len(set(texts)) != 1:
        raise AssertionError("matrix output differs between identical inputs (runs, fresh HOME/TMPDIR, a copy with other mtimes)")
    j = json.loads(outs[0])
    if ctx.tmp in outs[0] or time.strftime("%Y-%m-%dT") in outs[0]:
        raise AssertionError("matrix JSON carries a scratch path or today's timestamp")
    if j["inputs"].get("prices_sha256") != lib.sha_file(lib.PRICES):
        raise AssertionError("inputs.prices_sha256 is not the prices file's sha256")
    return "4 runs (3 fresh environments + a copy with mtime 1970) byte-identical, text and JSON"


@check("M6_cache_tamper", "§4 caches (content-bound derived state)")
def m6(ctx):
    plan = {str(t): ["done_pass", "done_fail", "done_pass", "failed_404"] for t in (0, 1, 5)}
    pristine = ctx.scratch("m6_pristine")
    fdir0, results0, runs = lib.build_synthetic_fleet(pristine, "m6", plan, seed=66)
    rnd = random.Random(66)
    findings = []

    def fresh(name):
        base = ctx.scratch(f"m6_{name}")
        shutil.copytree(fdir0, os.path.join(base, "fleet"))
        shutil.copytree(results0, os.path.join(base, "results"))
        meta = lib.read_json(os.path.join(base, "fleet", "fleet.json"))
        meta["results_root"] = os.path.join(base, "results")
        lib.write_json(os.path.join(base, "fleet", "fleet.json"), meta)
        env = lib.fleet_env(ctx.tmp, os.path.join(base, "results"), None,
                            extra={"TMPDIR": os.path.join(base, "tmp"), "HOME": os.path.join(base, "home"),
                                   "XDG_CACHE_HOME": os.path.join(base, "home", ".cache")})
        os.makedirs(env["TMPDIR"], exist_ok=True)
        os.makedirs(env["HOME"], exist_ok=True)
        return base, os.path.join(base, "fleet"), os.path.join(base, "results"), env

    # (a) same-size, same-mtime replacement of a source after a warm run
    base, fdir, results, env = fresh("a")
    g0 = ctx.matrix_json(fdir, env=env)["json"]
    run = "run_retail-0_0"
    lib.same_size_replace(os.path.join(results, run, "job.json"), '"reward": 1.0', '"reward": 0.0')
    g1 = ctx.matrix_json(fdir, env=env)["json"]
    _compare_matrix(ctx, g1, fdir, parts=("m2", "m11", "trials"))
    if g1["m11"]["pass1"] == g0["m11"]["pass1"]:
        raise AssertionError("a same-size same-mtime edit of job.json was not seen")
    findings.append("job.json same-size edit seen")
    ev = os.path.join(results, "run_retail-1_0", "events.jsonl")
    lib.same_size_replace(ev, '"status":200', '"status":502', count=1)
    g2 = ctx.matrix_json(fdir, env=env)["json"]
    _compare_matrix(ctx, g2, fdir, parts=("m12", "trials"))
    if g2["m12"]["policy"]["ok"] + g2["m12"]["simulator"]["ok"] >= g1["m12"]["policy"]["ok"] + g1["m12"]["simulator"]["ok"]:
        raise AssertionError("a same-size edit of events.jsonl was not seen")
    lib.same_size_replace(os.path.join(results, "run_retail-5_0", "completeness.json"), '"ratio": 1.0', '"ratio": 0.5')
    g3 = ctx.matrix_json(fdir, env=env)["json"]
    _compare_matrix(ctx, g3, fdir, parts=("m8",))
    findings.append("events/completeness same-size edits seen")
    # (b) tamper with everything the tool wrote (under the fleet dir, TMPDIR, HOME)
    for mode in ("truncate", "flip", "swap", "delete"):
        base, fdir, results, env = fresh(mode)
        before = set(lib.files_under(fdir, env["TMPDIR"], env["HOME"]))
        clean = ctx.matrix_json(fdir, env=env)["out"]
        written = sorted(set(lib.files_under(fdir, env["TMPDIR"], env["HOME"])) - before)
        if not written:
            findings.append(f"{mode}: nothing cached")
            continue
        lib.tamper_files(written, mode, rnd)
        res = ctx.fleet("matrix", "--fleet", fdir, "--prices", lib.PRICES, "--json", env=env, expect_rc=(0, 1))
        if res["rc"] == 0 and res["out"] != clean:
            raise AssertionError(f"after {mode} of {len(written)} cache files the matrix changed")
        if res["rc"] == 1 and "error" not in (res["out"] + res["err"]).lower():
            raise AssertionError(f"after {mode}: exit 1 without an error message")
        findings.append(f"{mode}: {len(written)} files → {'identical' if res['rc'] == 0 else 'refused'}")
    return "; ".join(findings)


@check("M7_duplicates_and_missing", "§4 journal with duplicated run ids and missing directories")
def m7(ctx):
    plan = {"0": ["done_pass", "done_fail"], "1": ["done_pass", "failed_ctx"]}
    extra = [lib.journal_line("0", "run_retail-0_1", "vm-01", "done"),                    # duplicate
             lib.journal_line("1", "run_retail-1_77", "vm-02", "done"),                   # no directory at all
             lib.journal_line("0", "run_retail-0_5", "vm-01", "aborted", interrupted=True)]  # interrupted: ignored
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, "m7", plan, seed=7, extra_lines=extra)
    g = ctx.matrix_json(fdir)["json"]
    _compare_matrix(ctx, g, fdir)
    if g["m2"]["trials"] != 5 or len(g["trials"]) != 5:
        raise AssertionError(f"trials counted {g['m2']['trials']} (want 5: 4 + the missing one, duplicate once, interrupted ignored)")
    by = {t["run"]: t for t in g["trials"]}
    if by["run_retail-1_77"]["status"] != "missing" or by["run_retail-1_77"]["cause"] != "no_job_json":
        raise AssertionError(f"missing directory: {by['run_retail-1_77']}")
    if not any("run_retail-0_1" in w for w in g["warnings"]) or not any("run_retail-1_77" in w for w in g["warnings"]):
        raise AssertionError(f"duplicate / missing directory not warned: {g['warnings']}")
    return "duplicate counted once (warned), missing directory = no_job_json, interrupted line ignored"


@check("M9_wrong_model_names", "§2 roles, §4 unattributed rows")
def m9(ctx):
    roles = {"policy": "same/model", "simulator": "same/model", "nl_judge": "judge/x"}
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, "m9", {"0": ["done_pass"]}, roles=roles)
    ctx.fleet("matrix", "--fleet", fdir, "--prices", lib.PRICES, "--json", expect_rc=2)
    env = ctx.env(ctx.scratch("m9_results"), ctx.fake())
    ctx.fleet("run", "--out", os.path.join(ctx.tmp, "m9_fleet2"), "--tasks", "0", "--trials", 1, "--concurrency", 1, "--job", ctx.fake(),
              "--policy", "a/b", "--simulator", "a/b", "--json", env=env, expect_rc=2)
    # rows under names that match no role: tokens never land on a role
    roles2 = {"policy": "p/9b", "simulator": "s/27b", "nl_judge": "j/flash"}
    fdir2, _, _ = lib.build_synthetic_fleet(ctx.tmp, "m9b", {"0": ["done_pass", "done_pass"]}, roles=roles2, seed=9)
    meta = lib.read_json(os.path.join(fdir2, "fleet.json"))
    meta["roles"] = {"policy": "other/p", "simulator": "other/s", "nl_judge": "other/j"}
    lib.write_json(os.path.join(fdir2, "fleet.json"), meta)
    g = ctx.matrix_json(fdir2)["json"]
    _compare_matrix(ctx, g, fdir2)
    if g["m12"]["simulator"]["calls"] != 0 or g["m12"]["simulator"]["usd_total"] != 0.0 or not g["m12"]["unattributed"]["calls"] \
            or g["m12"]["unattributed"]["usd_total"] != "unverified":
        raise AssertionError(f"rows of unknown models were attributed: {g['m12']['simulator']} / {g['m12']['unattributed']}")
    if set(g["m12"]["unattributed"]["models"]) != {"p/9b", "s/27b", "j/flash"}:
        raise AssertionError(f"unattributed models {g['m12']['unattributed']['models']}")
    return "non-distinct roles refused (matrix and run), unknown model names listed as unattributed with their tokens"


def _scale(ctx, name, n_trials, bound_s):
    rnd = random.Random(n_trials)
    split = lib.read_json(lib.SPLIT)
    tasks = sorted(split["train"], key=int) + sorted(split["held_out"], key=int)
    per = -(-n_trials // len(tasks))
    pool = ["done_pass"] * 5 + ["done_fail"] * 3 + ["failed_ctx", "failed_404", "aborted_create", "timeout", "no_events", "no_completeness"]
    plan, left = {}, n_trials
    for t in tasks:
        k = min(per, left)
        if k <= 0:
            break
        plan[t] = [rnd.choice(pool) for _ in range(k)]
        left -= k
    t0 = time.time()
    fdir, results, runs = lib.build_synthetic_fleet(ctx.tmp, name, plan, seed=n_trials, concurrency=32, trials_per_task=per)
    gen = time.time() - t0
    res = ctx.matrix_json(fdir, rss=True, timeout=bound_s * 3)
    if res["secs"] > bound_s:
        raise AssertionError(f"matrix over {n_trials} trials took {res['secs']:.1f} s > {bound_s} s")
    if res["maxrss_mib"] and res["maxrss_mib"] > TH["rss_mib"]:
        raise AssertionError(f"peak RSS {res['maxrss_mib']:.0f} MiB > {TH['rss_mib']}")
    g = res["json"]
    _compare_matrix(ctx, g, fdir, parts=("m2", "m11", "m12", "m13", "m16"))
    if g["m2"]["trials"] != n_trials:
        raise AssertionError(f"{g['m2']['trials']} trials counted")
    res2 = ctx.matrix_json(fdir, rss=True, timeout=bound_s * 3)
    if res2["out"] != res["out"]:
        raise AssertionError("second run differs")
    return f"{n_trials} trials (generated in {gen:.0f} s): matrix {res['secs']:.1f} s ≤ {bound_s} s, RSS {res['maxrss_mib'] or 0:.0f} MiB ≤ {TH['rss_mib']}, equal to the oracle"


@check("S1_scale_696", "POC 11 size (65×8 + 44×4)")
def s1(ctx):
    return _scale(ctx, "s1", 696, TH["matrix_696_s"])


@check("S2_scale_10k", "§4 scale (≈ 15 × POC 11)", scale=True)
def s2(ctx):
    return _scale(ctx, "s2", 10_000, TH["matrix_10k_s"])


@check("D1_m17_synthetic", "done test 3 / M17 (median host-side time, knee at +5 %)")
def d1(ctx):
    findings = []

    def run_m17(sd):
        res = ctx.fleet("m17", sd, "--json", expect_rc=0)
        try:
            g = json.loads(res["out"])
        except ValueError:
            raise AssertionError(f"m17 --json is not JSON: {lib.short(res['out'], 120)!r}")
        want = lib.m17_oracle(sd)
        d = lib.diff(g, want)
        if d:
            raise AssertionError(f"{os.path.basename(sd)}: " + "; ".join(d[:5]))
        res2 = ctx.fleet("m17", sd, "--json", expect_rc=0)
        if res2["out"] != res["out"]:
            raise AssertionError("m17 not deterministic")
        return g

    rnd = random.Random(17)
    # flat: every level within 2 %
    sd = ctx.scratch("d1_flat")
    for L in (1, 2, 4, 8):
        lib.plant_level(sd, L, [int(10_000 * rnd.uniform(0.98, 1.02)) for _ in range(7)], peak=600.0 * L)
    g = run_m17(sd)
    if g["m17"] != 8 or g["ceiling_reached"]:
        raise AssertionError(f"flat sweep: {g['m17']} {g['ceiling_reached']}")
    findings.append("flat → 8")
    # knee at 4
    sd = ctx.scratch("d1_knee")
    for L, hs in ((1, 10_000), (2, 10_200), (4, 10_450), (8, 12_000), (16, 20_000)):
        lib.plant_level(sd, L, [hs + rnd.randrange(-50, 50) for _ in range(9)])
    g = run_m17(sd)
    if g["m17"] != 4 or not g["ceiling_reached"] or [l["concurrency"] for l in g["levels"]] != [1, 2, 4, 8, 16]:
        raise AssertionError(f"knee sweep: {g['m17']} levels {[l['concurrency'] for l in g['levels']]}")
    findings.append("knee → 4, levels numeric order")
    # noise: outliers push the mean over +5 % but not the median
    sd = ctx.scratch("d1_noise")
    lib.plant_level(sd, 1, [10_000] * 10)
    lib.plant_level(sd, 2, [10_000] * 8 + [60_000, 90_000])
    lib.plant_level(sd, 4, [10_100] * 8 + [70_000, 95_000])
    g = run_m17(sd)
    if g["m17"] != 4 or g["ceiling_reached"]:
        raise AssertionError(f"noise sweep (mean trap): m17 {g['m17']}")
    findings.append("outliers ignored (median)")
    # raw wall rises 60 % (slow model) while host-side time is flat
    sd = ctx.scratch("d1_rawwall")
    lib.plant_level(sd, 1, [10_000] * 6, llm_ms=50_000)
    lib.plant_level(sd, 2, [10_000] * 6, llm_ms=80_000)
    lib.plant_level(sd, 4, [10_200] * 6, llm_ms=110_000)
    g = run_m17(sd)
    if g["m17"] != 4 or g["levels"][2]["wall_ms_median"] <= 1.5 * g["levels"][0]["wall_ms_median"]:
        raise AssertionError(f"raw-wall trap: m17 {g['m17']} (host-side flat, wall up 60 %)")
    findings.append("host-side not raw wall")
    # unverified pieces: missing peak, a level with no done trial, trials with unreadable events
    sd = ctx.scratch("d1_unver")
    lib.plant_level(sd, 1, [10_000] * 5, peak=None)
    lib.plant_level(sd, 2, [10_000] * 5, events=["truncate", "ok", "missing", "ok", "ok"])
    lib.plant_level(sd, 4, [10_000] * 5, statuses=["failed"] * 5)
    lib.plant_level(sd, 8, [10_000] * 5)
    g = run_m17(sd)
    lv = {l["concurrency"]: l for l in g["levels"]}
    if lv[1]["peak_guest_mem_mib"] != "unverified" or lv[2]["host_side_ms"]["n"] != 3 or lv[4]["host_side_ms"] != "unverified":
        raise AssertionError(f"unverified pieces: {lv[1]['peak_guest_mem_mib']} {lv[2]['host_side_ms']} {lv[4]['host_side_ms']}")
    if g["m17"] != 2 or g["ceiling_reached"]:
        raise AssertionError(f"m17 past an unverified level: {g['m17']} {g['ceiling_reached']}")
    findings.append("missing peak/level → unverified, never 0")
    # baseline level unverified → m17 unverified
    sd = ctx.scratch("d1_nobase")
    lib.plant_level(sd, 1, [10_000] * 3, events=["missing"] * 3)
    lib.plant_level(sd, 2, [10_000] * 3)
    g = run_m17(sd)
    if g["m17"] != "unverified":
        raise AssertionError(f"m17 with an unverified baseline: {g['m17']}")
    findings.append("no baseline → unverified")
    return "; ".join(findings)


def _route(ctx, table, model, slot, remote=False):
    if remote:
        p = ctx.ssh(f"cd {HOST_REPO} && sudo /opt/avos/gateway/bin/python gateway/gateway.py route --table {table} '{model}' {slot}", timeout=60)
        rc, out, err = p.returncode, p.stdout, p.stderr
    else:
        r = lib._run([sys.executable, os.path.join(lib.ROOT, "gateway", "gateway.py"), "route", "--table", table, model, slot])
        rc, out, err = r["rc"], r["out"], r["err"]
        if "No module named 'cedarpy'" in err:
            raise lib.SkipCheck("gateway.py needs cedarpy (host only); run with --host")
    if "Traceback" in err:
        raise AssertionError(f"traceback from gateway.py route: {lib.short(err.strip().splitlines()[-1], 160)}")
    try:
        j = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except ValueError:
        raise AssertionError(f"route printed no JSON: {lib.short(out, 120)!r}")
    return rc, j


def _route_cases(ctx, table_path, remote=False):
    rc, j = _route(ctx, table_path, "m/one", "vm-03", remote)
    if rc != 0 or j.get("upstream") != "http://a:1/v1":
        raise AssertionError(f"dict form: {rc} {j}")
    rc, j = _route(ctx, table_path, "m/single", "vm-04", remote)
    if rc != 0 or j.get("upstream") != "http://a:1/v1":
        raise AssertionError(f"list of one: {rc} {j}")
    for slot, want in (("vm-03", "http://b:2/v1"), ("vm-04", "http://a:1/v1"), ("vm-05", "http://b:2/v1"), ("vm-82", "http://a:1/v1"), ("vm-03-abcdef", "http://b:2/v1")):
        rc, j = _route(ctx, table_path, "m/two", slot, remote)
        if rc != 0 or j.get("upstream") != want:
            raise AssertionError(f"m/two on {slot}: {rc} {j} (want {want}: NN mod 2)")
    for slot, want in (("vm-03", "http://a:1/v1"), ("vm-04", "http://b:2/v1"), ("vm-05", "http://c:3/v1"), ("vm-06", "http://a:1/v1")):
        rc, j = _route(ctx, table_path, "m/three", slot, remote)
        if rc != 0 or j.get("upstream") != want:
            raise AssertionError(f"m/three on {slot}: {rc} {j} (want {want}: NN mod 3)")
    rc, j = _route(ctx, table_path, "m/none", "vm-03", remote)
    if rc != 1 or j.get("upstream") is not None or "no-route" not in json.dumps(j):
        raise AssertionError(f"unknown model: rc {rc} {j} (want exit 1, upstream null, no-route)")
    return "dict, list-of-one, two and three upstreams by NN mod n (slot and vm-NN-xxxxxx), unknown → no-route"


ROUTE_TABLE = {"m/one": "http://a:1/v1", "m/single": ["http://a:1/v1"], "m/two": ["http://a:1/v1", "http://b:2/v1"],
               "m/three": ["http://a:1/v1", "http://b:2/v1", "http://c:3/v1"]}


@check("G1_route_table_pure", "§8 (route = upstreams[model][NN mod n], pure entry point)")
def g1(ctx):
    t = os.path.join(ctx.tmp, "routes.json")
    lib.write_json(t, ROUTE_TABLE)
    return _route_cases(ctx, t)


@check("Z_fixtures_untouched", "fixtures read-only")
def z(ctx):
    drift = lib.fixture_drift()
    if drift:
        raise AssertionError(f"fixtures changed: {drift[:5]}")
    return f"{sum(1 for _ in lib.files_under(lib.FIX))} fixture files match MANIFEST.sha256"


# ====================================================================== host checks (avos-dev over ssh)

def _host_counts(ctx):
    cmd = ("echo taps=$(ip -br link | grep -c 'tap-vm'); echo rules=$(sudo iptables -S INPUT | grep -c -- '-i tap-vm'); "
           "echo jails=$(sudo ls /var/lib/avos/jail/firecracker 2>/dev/null | wc -l); "
           "echo entries=$(sudo /opt/spire/bin/spire-server entry show -socketPath /run/spire-server/api.sock 2>/dev/null | grep -c 'Entry ID'); "
           "echo fc=$(pgrep -c -x firecracker || true); echo runners=$(pgrep -fc 'fleet [rs](un|weep) --out' || true)")
    p = ctx.ssh(cmd, timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"ssh failed: {lib.short(p.stderr, 200)}")
    out = {}
    for line in p.stdout.split():
        k, v = line.split("=", 1)
        out[k] = int(v.strip() or 0)
    return out


def _host_wait(ctx, cmd, pred, timeout_s, poll_s=5):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        p = ctx.ssh(cmd, timeout=60)
        last = p.stdout
        if pred(p.stdout):
            return p.stdout
        time.sleep(poll_s)
    raise AssertionError(f"timed out after {timeout_s} s waiting on {cmd!r}; last: {lib.short(last, 120)!r}")


def _host_release(ctx, out):
    """Stop any runner on `out` and wait (bounded) until it is gone; the next check never inherits it."""
    ctx.ssh(f"sudo pkill -TERM -f 'fleet [rs](un|weep) --out {out}' 2>/dev/null || true", timeout=30)
    gone = lib.wait_until(lambda: int((ctx.ssh(f"pgrep -fc 'fleet [rs](un|weep) --out {out}' || true").stdout.strip() or 0)) == 0, TH["stop_s"] + 30, poll_s=3)
    if not gone:
        ctx.ssh(f"sudo pkill -KILL -f 'fleet [rs](un|weep) --out {out}' 2>/dev/null || true", timeout=30)


def _host_require_clean(ctx, cid):
    c = _host_counts(ctx)
    dirty = {k: c[k] for k in ("taps", "rules", "jails", "entries", "fc", "runners") if c[k] != ctx.baseline[k]}
    if dirty:
        raise AssertionError(f"blocked: host not clean before {cid} (an earlier check left {dirty}); not a {cid} result")


def _host_env():
    return f"sudo env TIMEOUT_S=300 MEM_MIB={HOST_MEM} python3 fleet/bin/fleet"


@check("H0_preflight", "class B (no stray state before)", host=True)
def h0(ctx):
    global HOST_REPO
    ctx.host_t0 = time.time()
    p = ctx.ssh(f"test -f {HOST_REPO}/fleet/bin/fleet && echo yes", timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"ssh {HOST}: {lib.short(p.stderr, 200)}")
    if "yes" not in p.stdout:
        raise lib.NotImplementedCLI("fleet/bin/fleet on the host (just push)")
    c = _host_counts(ctx)
    if c["runners"] or c["fc"] or c["jails"]:
        raise AssertionError(f"host busy before the suite: {c}")
    ctx.baseline = c
    HOST_REPO = ctx.ssh(f"cd {HOST_REPO} && pwd -P").stdout.strip() or HOST_REPO
    ctx.ssh(f"sudo rm -rf {HOST_TMP}; mkdir -p {HOST_TMP}; chmod 777 {HOST_TMP}")
    ctx.ssh(f"cat > {HOST_TMP}/prices.json", stdin=open(lib.PRICES).read())
    ctx.ssh(f"cat > {HOST_TMP}/lib.py", stdin=open(os.path.join(HERE, "lib.py")).read())
    return f"host idle, baseline {c}"


def _host_json(ctx, cmd, timeout=600):
    p = ctx.ssh(cmd, timeout=timeout)
    if "Traceback" in p.stderr or "Traceback" in p.stdout:
        raise AssertionError("traceback on the host: " + lib.short(p.stderr.strip().splitlines()[-1] if p.stderr.strip() else p.stdout, 200))
    try:
        return json.loads(p.stdout.strip().splitlines()[-1]), p
    except (ValueError, IndexError):
        raise AssertionError(f"no JSON on stdout: rc={p.returncode} {lib.short(p.stderr, 300)!r}")


def _host_oracle(ctx, what, arg, prices=None):
    code = (f"import sys, os, json; os.environ['AVOS_SUITE_ROOT'] = '{HOST_REPO}'; sys.path.insert(0, '{HOST_TMP}'); import lib; "
            + (f"print(json.dumps(lib.matrix_oracle('{arg}', json.load(open('{prices}')))))" if what == "matrix"
               else f"print(json.dumps(lib.m17_oracle('{arg}')))"))
    p = ctx.ssh(f"sudo python3 -c \"{code}\"", timeout=600)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AssertionError(f"oracle on host failed: {lib.short(p.stderr, 300)!r}")


@check("H1_kill_resume_live", "done test 1 on the real host (budget: 2 tasks × 2 trials at concurrency 2)", host=True)
def h1(ctx):
    _host_require_clean(ctx, "H1")
    out = f"{HOST_TMP}/fleet"
    try:
        return _h1(ctx, out)
    finally:
        _host_release(ctx, out)


def _h1(ctx, out):
    tasks = "0,1"
    base = f"cd {HOST_REPO} && {_host_env()} run --out {out} --tasks {tasks} --trials 2 --concurrency 2 --slots vm-03-vm-04 --mem-mib {HOST_MEM} --json"
    ctx.ssh(f"cd {HOST_REPO}; nohup setsid sh -c '{base} > {HOST_TMP}/run1.out 2> {HOST_TMP}/run1.err' < /dev/null > /dev/null 2>&1 &", timeout=60)
    _host_wait(ctx, f"sudo cat {out}/trials.jsonl 2>/dev/null | wc -l; pgrep -fc 'fleet [r]un --out {out}' || true",
               lambda s: int(s.split()[0] or 0) >= 1 or int(s.split()[1] or 0) == 0, TH["host_trial_wait_s"])
    if int(ctx.ssh(f"pgrep -fc 'fleet [r]un --out {out}' || true").stdout.split()[0] or 0) == 0:
        raise AssertionError("runner finished before the first journal line could be observed; nothing to interrupt")
    c = _host_counts(ctx)
    if c["jails"] < 1:
        raise AssertionError(f"no VM running while the fleet is mid-run: {c}")
    jails = ctx.ssh("sudo ls /var/lib/avos/jail/firecracker 2>/dev/null").stdout.split()
    if set(jails) & {"vm-01", "vm-02"} or not set(jails) <= {"vm-03", "vm-04"}:
        raise AssertionError(f"fleet on --slots vm-03-vm-04 uses jails {jails}: claim 1's vm-01/vm-02 are not free")
    ctx.ssh(f"sudo pkill -TERM -f 'fleet [r]un --out {out}'")
    _host_wait(ctx, f"pgrep -fc 'fleet [r]un --out {out}' || true", lambda s: int(s.strip() or 0) == 0, TH["stop_s"] + 30, poll_s=3)
    c = _host_counts(ctx)
    for k in ("taps", "rules", "jails", "entries", "fc"):
        if c[k] != ctx.baseline[k]:
            raise AssertionError(f"leftover after SIGTERM: {k}={c[k]} (baseline {ctx.baseline[k]})")
    meta = json.loads(ctx.ssh(f"sudo cat {out}/fleet.json").stdout)
    if meta["status"] != "stopped":
        raise AssertionError(f"status after SIGTERM: {meta['status']}")
    lines = [json.loads(l) for l in ctx.ssh(f"sudo cat {out}/trials.jsonl").stdout.splitlines() if l.strip()]
    n_inter = sum(1 for l in lines if l.get("interrupted"))
    summ, p = _host_json(ctx, base + f" 2> {HOST_TMP}/run2.err", timeout=TH["host_resume_s"])
    if summ.get("status") != "done":
        raise AssertionError(f"resume summary {summ}")
    lines = [json.loads(l) for l in ctx.ssh(f"sudo cat {out}/trials.jsonl").stdout.splitlines() if l.strip()]
    live = [l for l in lines if not l.get("interrupted")]
    runs = [l["run"] for l in live]
    per = {t: sum(1 for l in live if str(l["task"]) == t) for t in tasks.split(",")}
    if len(set(runs)) != 4 or len(runs) != 4 or any(v != 2 for v in per.values()):
        raise AssertionError(f"after resume: runs {runs} per task {per}")
    for l in lines:
        jp = ctx.ssh(f"sudo cat /var/lib/avos/results/{l['run']}/job.json").stdout
        if not jp.strip():
            raise AssertionError(f"{l['run']} has no job.json")
    c = _host_counts(ctx)
    for k in ("taps", "rules", "jails", "entries", "fc"):
        if c[k] != ctx.baseline[k]:
            raise AssertionError(f"leftover after the fleet: {k}={c[k]} (baseline {ctx.baseline[k]})")
    g, _ = _host_json(ctx, f"cd {HOST_REPO} && sudo python3 fleet/bin/fleet matrix --fleet {out} --prices {HOST_TMP}/prices.json --json")
    want = _host_oracle(ctx, "matrix", out, f"{HOST_TMP}/prices.json")
    d = lib.diff(g, {k: want[k] for k in ("m2", "m8", "m11", "m12", "m13", "m16", "trials")})
    if d:
        raise AssertionError("matrix ≠ oracle on the real run: " + "; ".join(d[:5]))
    nd = [t for t in g["trials"] if t["status"] != "done"]
    if any(t["cause"] is None for t in nd):
        raise AssertionError("a non-done trial without a cause")
    done = [t for t in g["trials"] if t["status"] == "done"]
    if not done:
        raise AssertionError(f"no trial finished: causes {[t['cause'] for t in nd]}")
    models_ok = set()
    for t in done:
        if t["llm"] != "unverified":
            models_ok |= {m for m, b in t["llm"]["by_model"].items() if b["ok"]}
    if len(models_ok) < 2:
        raise AssertionError(f"done trials reached only {sorted(models_ok)}: policy and simulator must be two models")
    if g["m8"]["unverified"] != 0:
        raise AssertionError(f"M8 unverified for {g['m8']['unverified']} live trials (completeness.json missing or unverified)")
    ev = ctx.ssh(f"sudo cat /var/lib/avos/results/{done[0]['run']}/events.jsonl").stdout
    rows = [json.loads(l) for l in ev.splitlines() if l.strip()]
    calls = [e for e in rows if e.get("kind") == "llm.call"]
    allows = [e for e in rows if e.get("kind") == "authz.decision" and e.get("action") == "llm.call" and e.get("decision") == "allow"]
    if len(allows) != len(calls):
        raise AssertionError(f"{len(allows)} llm.call allows vs {len(calls)} llm.call rows in {done[0]['run']}")
    bad = [e for e in calls if not all(k in e for k in ("upstream_status", "error", "finish_reason"))
           or (e.get("status") == 200 and (e.get("upstream_status") != 200 or e.get("finish_reason") is None))]
    if not calls or bad:
        raise AssertionError(f"{len(bad)}/{len(calls)} llm.call rows lack upstream_status/error/finish_reason evidence (§8)")
    return (f"stopped with {n_inter} interrupted, resumed to 4 unique trials ({len(done)} done, causes {sorted(t['cause'] for t in nd)}), "
            f"models with 200 rows {sorted(models_ok)}, no leftovers, matrix = oracle")


@check("H2_unrouted_model_live", "§8 (a model outside the route table is refused, not forwarded)", host=True)
def h2(ctx):
    _host_require_clean(ctx, "H2")
    out = f"{HOST_TMP}/unrouted"
    try:
        return _h2(ctx, out)
    finally:
        _host_release(ctx, out)


def _h2(ctx, out):
    model = "nobody/no-such-model-9b"
    cmd = (f"cd {HOST_REPO} && sudo env TIMEOUT_S=120 MEM_MIB={HOST_MEM} python3 fleet/bin/fleet run --out {out} --tasks 0 --trials 1 "
           f"--concurrency 1 --mem-mib {HOST_MEM} --policy {model} --json 2> {HOST_TMP}/unrouted.err")
    summ, p = _host_json(ctx, cmd, timeout=420)
    lines = [json.loads(l) for l in ctx.ssh(f"sudo cat {out}/trials.jsonl").stdout.splitlines() if l.strip()]
    live = [l for l in lines if not l.get("interrupted")]
    if len(live) != 1:
        raise AssertionError(f"journal {lines}")
    run = live[0]["run"]
    ev = ctx.ssh(f"sudo cat /var/lib/avos/results/{run}/events.jsonl").stdout
    rows = [json.loads(l) for l in ev.splitlines() if l.strip()]
    denies = [e for e in rows if e.get("kind") == "authz.decision" and e.get("decision") == "deny" and "no-route" in (e.get("policy_ids") or [])]
    if not denies:
        raise AssertionError("no authz.decision deny with policy_ids containing no-route")
    if not any(model in (e.get("resource") or "") for e in denies):
        raise AssertionError(f"the deny does not name the model: {denies[0].get('resource')}")
    forwarded = [e for e in rows if e.get("kind") == "llm.call" and e.get("model") == model and e.get("status") == 200]
    if forwarded:
        raise AssertionError(f"{len(forwarded)} calls of the unrouted model were forwarded with status 200")
    g, _ = _host_json(ctx, f"cd {HOST_REPO} && sudo python3 fleet/bin/fleet matrix --fleet {out} --prices {HOST_TMP}/prices.json --json")
    t = g["trials"][0]
    if t["cause"] != "model_unrouted" or t["cause_class"] != "infra" or t["valid"]:
        raise AssertionError(f"trial facts {t['status']} {t['cause']} {t['valid']}")
    keyish = re.compile(r"(sk-[A-Za-z0-9]{10,}|OPENAI_API_KEY=\S{8,}|Bearer [A-Za-z0-9._-]{16,})")
    blob = ctx.ssh(f"sudo cat /var/lib/avos/results/{run}/*.json {out}/*.json* {HOST_TMP}/unrouted.err 2>/dev/null").stdout
    if keyish.search(blob):
        raise AssertionError("a key-like string in the run directory, journal or log")
    c = _host_counts(ctx)
    for k in ("taps", "rules", "jails", "entries", "fc"):
        if c[k] != ctx.baseline[k]:
            raise AssertionError(f"leftover: {k}={c[k]} (baseline {ctx.baseline[k]})")
    return f"{run}: {len(denies)} no-route denies naming the model, 0 forwarded, cause model_unrouted, no key on disk"


@check("H3_sweep_live", "done test 3 on the real host (levels 1,2 × 2 trials)", host=True)
def h3(ctx):
    _host_require_clean(ctx, "H3")
    out = f"{HOST_TMP}/sweep"
    try:
        return _h3(ctx, out)
    finally:
        _host_release(ctx, out)


def _h3(ctx, out):
    cmd = (f"cd {HOST_REPO} && {_host_env()} sweep --out {out} --levels 1,2 --tasks 0,1 --trials 1 --mem-mib {HOST_MEM} --json "
           f"2> {HOST_TMP}/sweep.err")
    g, p = _host_json(ctx, cmd, timeout=TH["host_sweep_s"])
    g2, _ = _host_json(ctx, f"cd {HOST_REPO} && sudo python3 fleet/bin/fleet m17 {out} --json")
    if g2 != g:
        raise AssertionError("fleet m17 over the sweep directory differs from the sweep's own output")
    want = _host_oracle(ctx, "m17", out)
    d = lib.diff(g, want)
    if d:
        raise AssertionError("m17 ≠ oracle: " + "; ".join(d[:5]))
    lv = {l["concurrency"]: l for l in g["levels"]}
    if set(lv) != {1, 2}:
        raise AssertionError(f"levels {sorted(lv)}")
    for L, l in lv.items():
        if l["host_side_ms"] == "unverified" or l["host_side_ms"]["median"] <= 0:
            raise AssertionError(f"level {L}: host-side {l['host_side_ms']}")
        pk = l["peak_guest_mem_mib"]
        if pk == "unverified" or not (TH["peak_mib_min_per_vm"] * L <= pk <= TH["peak_mib_max_per_vm"] * L):
            raise AssertionError(f"level {L}: peak guest memory {pk} MiB not in [{TH['peak_mib_min_per_vm'] * L}, {TH['peak_mib_max_per_vm'] * L}]")
        if l["wall_ms_median"] <= l["host_side_ms"]["median"]:
            raise AssertionError(f"level {L}: wall {l['wall_ms_median']} ≤ host-side {l['host_side_ms']['median']} (no model time subtracted?)")
    c = _host_counts(ctx)
    for k in ("taps", "rules", "jails", "entries", "fc"):
        if c[k] != ctx.baseline[k]:
            raise AssertionError(f"leftover: {k}={c[k]} (baseline {ctx.baseline[k]})")
    return (f"levels 1,2: host-side medians {lv[1]['host_side_ms']['median']}/{lv[2]['host_side_ms']['median']} ms, "
            f"peak {lv[1]['peak_guest_mem_mib']}/{lv[2]['peak_guest_mem_mib']} MiB, m17={g['m17']}, = oracle, no leftovers")


@check("H4_matrix_poc9_real", "done test 2 over the POC 9 dry run (80 real runs + 3 real broken ones)", host=True)
def h4(ctx):
    _host_require_clean(ctx, "H4")
    dry = lib.read_json(os.path.join(lib.ROOT, "data", "splits", "dryrun_v1.json"))
    runs = list(dry["runs"]) + ["run_retail-0_26", "run_retail-2_1", "run_retail-2_0"]
    tasks = sorted(set(dry["tasks"]) | {"2"}, key=int)
    out = f"{HOST_TMP}/poc9"
    script = f"""
import json, os, sys, hashlib
os.environ['AVOS_SUITE_ROOT'] = '{HOST_REPO}'; sys.path.insert(0, '{HOST_TMP}'); import lib
runs = {json.dumps(runs)}; tasks = {json.dumps(tasks)}
lines = []
for r in runs:
    task = r.split('-', 1)[1].rsplit('_', 1)[0]
    d = '/var/lib/avos/results/' + r
    st = 'missing'
    try:
        st = json.load(open(d + '/job.json'))['status']
    except Exception:
        pass
    lines.append(lib.journal_line(task, r, 'vm-01', st if st != 'missing' else 'aborted', t0=lib.T0_MS))
lib.write_fleet('{out}', tasks, 4, 1, '/var/lib/avos/results', lines, status='stopped', name='poc9-dryrun', peak=None,
                sessions=[{{'started': '2026-10-22T20:13:00.000Z', 'ended': '2026-10-22T20:50:00.000Z', 'pid': 0}}])
print('ok')
"""
    p = ctx.ssh(f"cd {HOST_REPO} && sudo python3 -", stdin=script, timeout=120)
    if "ok" not in p.stdout:
        raise AssertionError(f"could not build the POC 9 fleet dir: {lib.short(p.stderr, 300)}")
    g, _ = _host_json(ctx, f"cd {HOST_REPO} && sudo python3 fleet/bin/fleet matrix --fleet {out} --prices {HOST_TMP}/prices.json --json", timeout=600)
    want = _host_oracle(ctx, "matrix", out, f"{HOST_TMP}/prices.json")
    d = lib.diff(g, {k: want[k] for k in ("m2", "m8", "m11", "m12", "m13", "m16", "trials")})
    if d:
        raise AssertionError("matrix ≠ oracle over the POC 9 runs: " + "; ".join(d[:5]))
    by = {t["run"]: t for t in g["trials"]}
    if by["run_retail-0_26"]["cause"] != "vm_create_failed" or by["run_retail-2_0"]["cause"] != "no_job_json" or by["run_retail-2_1"]["cause"] is None:
        raise AssertionError(f"real causes: {by['run_retail-0_26']['cause']} {by['run_retail-2_0']['cause']} {by['run_retail-2_1']['cause']}")
    wm = [t["run"] for t in g["trials"] if t["cause"] == "witness_mismatch"]
    if wm and set(wm) != {t["run"] for t in g["trials"] if t["status"] == "done" and t["cause"] == "witness_mismatch"}:
        raise AssertionError(f"witness_mismatch on non-done trials: {wm}")
    if g["m2"]["trials"] != len(runs) or g["m8"]["mean_ratio"] != "unverified":
        raise AssertionError(f"M2 trials {g['m2']['trials']}, M8 {g['m8']} (POC 9 runs have no completeness.json → unverified)")
    g2, _ = _host_json(ctx, f"cd {HOST_REPO} && sudo python3 fleet/bin/fleet matrix --fleet {out} --prices {HOST_TMP}/prices.json --json", timeout=600)
    if g2 != g:
        raise AssertionError("not deterministic on the host")
    return (f"{len(runs)} real runs: M2 {g['m2']['done']}/{g['m2']['trials']}, pass^1 {g['m11']['pass1']}, pass^4 {g['m11']['pass4']}, "
            f"causes {g['m11']['causes']}, = oracle, M8 unverified")


@check("H5_two_upstreams_live", "§8 (one trial = one upstream; slots split by NN mod n)", host=True)
def h5(ctx):
    ctx.ssh(f"cat > {HOST_TMP}/routes.json", stdin=json.dumps(ROUTE_TABLE))
    pure = _route_cases(ctx, f"{HOST_TMP}/routes.json", remote=True)
    out = f"{HOST_TMP}/fleet"
    lines = [json.loads(l) for l in ctx.ssh(f"sudo cat {out}/trials.jsonl 2>/dev/null").stdout.splitlines() if l.strip()]
    live = [l for l in lines if not l.get("interrupted")]
    if not live:
        raise AssertionError("blocked: H1's fleet directory has no trials to inspect")
    meta = json.loads(ctx.ssh(f"sudo cat {out}/fleet.json").stdout)
    policy = meta["roles"]["policy"]
    seen = {}
    for l in live:
        ev = ctx.ssh(f"sudo cat /var/lib/avos/results/{l['run']}/events.jsonl").stdout
        rows = [json.loads(x) for x in ev.splitlines() if x.strip()]
        pol = [e for e in rows if e.get("kind") == "llm.call" and e.get("model") == policy and e.get("status") == 200]
        if not pol:
            continue
        ups = {e.get("upstream") for e in pol}
        if len(ups) != 1 or None in ups:
            raise AssertionError(f"{l['run']}: policy rows name {sorted(map(str, ups))} upstreams (want exactly one)")
        p = ctx.ssh(f"cd {HOST_REPO} && sudo /opt/avos/gateway/bin/python gateway/gateway.py route '{policy}' {l['slot']}", timeout=60)
        want = json.loads(p.stdout.strip().splitlines()[-1]).get("upstream") if p.stdout.strip() else None
        if want != next(iter(ups)):
            raise AssertionError(f"{l['run']} on {l['slot']}: rows name {next(iter(ups))}, the live table routes to {want}")
        seen[l["slot"]] = next(iter(ups))
    if not seen:
        raise AssertionError("no trial with policy rows")
    return f"{pure}; live: {len(seen)} slots, each trial on one upstream equal to the table's route: {seen}"


@check("HZ_cleanup", "class B (no leftovers after)", host=True)
def hz(ctx):
    ctx.host_cleanup()
    c = _host_counts(ctx)
    for k in ("taps", "rules", "jails", "entries", "fc", "runners"):
        if c[k] != ctx.baseline[k]:
            raise AssertionError(f"leftover: {k}={c[k]} (baseline {ctx.baseline[k]})")
    p = ctx.ssh(f"test -e {HOST_TMP} && echo present || echo gone")
    if "gone" not in p.stdout:
        raise AssertionError(f"{HOST_TMP} still present")
    total = time.time() - ctx.host_t0
    if total > TH["host_total_s"]:
        raise AssertionError(f"live section took {total:.0f} s > {TH['host_total_s']} s budget")
    return f"host clean, live section {total:.0f} s ≤ {TH['host_total_s']} s"


# ====================================================================== selftest

def selftest():
    print("fixture manifest:", "ok" if not lib.fixture_drift() else f"DRIFT {lib.fixture_drift()[:3]}")
    if lib.fixture_drift():
        return 1
    lib.selftest()
    print("oracle: pass^k, bootstrap, causes (22 shapes), M8/M12/M17 hand cases ok")
    tmp = tempfile.mkdtemp(prefix="poc10-self-")
    try:
        job = lib.install_fake_job(tmp)
        results = os.path.join(tmp, "results")
        os.makedirs(results)
        plan = os.path.join(tmp, "plan.json")
        lib.write_json(plan, {"default": {"kind": "done_pass", "sleep": 0.1}, "tasks": {"0": ["failed_404", {"kind": "done_fail", "sleep": 2.5}]}})
        env = lib.fleet_env(tmp, results, job, plan, {"SLOT": "vm-03", "MODEL": "p/x", "USER_MODEL": "s/y", "JUDGE_MODEL": "j/z"})
        r = lib._run([sys.executable, job, "run", "tau2", "retail/0"], env=env)
        assert r["rc"] == 1 and os.path.exists(os.path.join(results, "run_retail-0_0", "job.json")), r
        job0 = lib.read_json(os.path.join(results, "run_retail-0_0", "job.json"))
        assert job0["status"] == "failed" and lib.read_json(os.path.join(results, "run_retail-0_0", "env.json"))["SLOT"] == "vm-03"
        rows = lib.read_events(os.path.join(results, "run_retail-0_0", "events.jsonl"))
        assert any(e.get("status") == 404 for e in rows) and any(e.get("model") == "p/x" for e in rows)
        # SIGTERM → aborted at stage harness, exit 143; a busy slot is refused
        p = subprocess.Popen([sys.executable, job, "run", "tau2", "retail/0"], env=dict(os.environ, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert lib.wait_until(lambda: os.path.exists(os.path.join(results, ".slots", "vm-03")), 5)
        r2 = lib._run([sys.executable, job, "run", "tau2", "retail/1"], env=env)
        assert r2["rc"] == 1 and lib.read_json(os.path.join(results, "run_retail-1_0", "job.json"))["stage"] == "create", r2
        p.send_signal(signal.SIGTERM)
        p.wait(5)
        j = lib.read_json(os.path.join(results, "run_retail-0_1", "job.json"))
        assert p.returncode == 143 and j["status"] == "aborted" and j["stage"] == "harness" and j["exit_code"] == 143, (p.returncode, j)
        assert not os.path.exists(os.path.join(results, ".slots", "vm-03"))
        r3 = lib._run([sys.executable, job, "run", "tau2", "retail/0"], env=env)
        assert r3["rc"] == 0 and json.loads(r3["out"].strip().splitlines()[-1])["status"] == "done"
        log = lib.fake_log(results)
        assert lib.max_overlap(log) == 1 and any(l["event"] == "slot_busy" for l in log)
        print("fake job: claims lowest free run id, honours SLOT, writes every file, aborts on SIGTERM (143), refuses a busy slot")
        # diff() catches what it should
        assert lib.diff({"a": 1.0, "b": [1, "x"]}, {"a": 1.0, "b": [1, "x"]}) == []
        assert lib.diff({"a": 1.0000001}, {"a": 1.0}) and lib.diff({"a": "unverified"}, {"a": 1.0}) and lib.diff({"a": 0}, {"a": "unverified"})
        assert lib.diff({"a": True}, {"a": 1}) and lib.diff({}, {"a": None})
        print("comparator: tolerance 1e-9, strings/bools/None exact, missing keys reported")
        # same_size_replace keeps size and mtime
        pth = os.path.join(tmp, "f.json")
        open(pth, "w").write('{"reward": 1.0}')
        st = os.stat(pth)
        lib.same_size_replace(pth, '"reward": 1.0', '"reward": 0.0')
        assert os.stat(pth).st_size == st.st_size and os.stat(pth).st_mtime_ns == st.st_mtime_ns
        print("tamper helpers: same-size same-mtime replacement verified")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("selftest ok")
    return 0


# ====================================================================== main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--host", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--write-manifest", action="store_true", help="(maintainer) regenerate fixtures/MANIFEST.sha256")
    a = ap.parse_args()
    if a.write_manifest:
        lib.write_fixture_manifest()
        print("manifest written")
        return 0
    if a.selftest:
        return selftest()
    drift = lib.fixture_drift()
    if drift:
        print(f"FAIL  FIXTURES  fixtures differ from MANIFEST.sha256: {drift[:5]}")
        return 1
    ctx = Ctx()
    results = []
    t_start = time.time()
    try:
        for cid, target, scale, host, fn in CHECKS:
            if a.only and not cid.startswith(a.only):
                continue
            if scale and a.quick:
                print(f"SKIP  {cid}  [{target}]  --quick")
                continue
            if host and not a.host:
                continue
            if host and ctx.baseline is None and cid != "H0_preflight":
                print(f"SKIP  {cid}  [{target}]  preflight did not pass")
                continue
            t0 = time.time()
            try:
                msg = fn(ctx)
                ok = True
            except lib.NotImplementedCLI as e:
                ok, msg = False, f"not implemented: {e}"
            except lib.SkipCheck as e:
                print(f"SKIP  {cid}  [{target}]  {e}")
                continue
            except AssertionError as e:
                ok, msg = False, str(e)
            except subprocess.TimeoutExpired as e:
                ok, msg = False, f"timeout after {e.timeout} s"
            except Exception as e:
                ok, msg = False, f"ERROR {type(e).__name__}: {lib.short(e, 200)} @ {traceback.format_exc().strip().splitlines()[-3].strip()}"
            results.append(ok)
            print(f"{'PASS' if ok else 'FAIL'}  {cid}  [{target}]  {msg}  ({time.time() - t0:.1f}s)", flush=True)
            if cid == "H0_preflight" and not ok:
                ctx.baseline = None
    finally:
        if a.host:
            try:
                ctx.host_cleanup()
            except Exception:
                pass
        ctx.ssh_close()
        if all(results):
            shutil.rmtree(ctx.tmp, ignore_errors=True)
        else:
            print(f"scratch kept at {ctx.tmp}")
    print(f"total {time.time() - t_start:.0f}s, {sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
