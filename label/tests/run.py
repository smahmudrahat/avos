#!/usr/bin/env python3
"""POC 9 acceptance suite (label/CONTRACT.md). Black box: only label/bin/label, POC 8's retrain as the dataset
producer, a stub judge the suite controls, and raw fixtures are touched.

  python3 label/tests/run.py              full local run (includes the 10,000-item scale checks and POC 8's suite)
  python3 label/tests/run.py --quick      skips the scale checks and runs only part of POC 8's suite
  python3 label/tests/run.py --selftest   proves the suite's own oracles and the stub judge do what they claim
  python3 label/tests/run.py --host       adds the live checks on avos-dev over ssh (repo pushed to ~/poc)
  python3 label/tests/run.py --only P     only checks whose id starts with P

Exit 0 only when every executed check passes. One line per check:
  PASS|FAIL|SKIP  <check id>  [<target>]  <measured vs threshold>
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lib  # noqa: E402

CHECKS = []
TH = {"level0_s": 60.0, "level01_s": 900.0, "rerun_s": 120.0, "rss_mib": 512, "root_mib": 50, "n_scale": 10000,
      "attempts": 4, "gold_min": 50, "host_wait_s": 5400, "judge_runs_host": 4, "verbatim": lib.VERBATIM}
HOST = "avos-dev"
HOST_REPO = "~/poc"
HOST_TMP = "/tmp/p9-tests"
DRYRUN_LOG = "/tmp/p9-dryrun.log"
DRYRUN_TASKS = "0 7 8 15 37 38 39 47 52 56 60 61 67 73 75 92 96 98 99 104".split()
EXPECTED = json.load(open(os.path.join(lib.FIX, "expected_poc8.json")))
RUBRIC_ALT = os.path.join(lib.FIX, "rubric_alt.json")


def check(cid, target, scale=False, host=False):
    def deco(fn):
        CHECKS.append((cid, target, scale, host, fn))
        return fn
    return deco


# =============================================================================== context

class Ctx:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="poc9-tests-")
        self._env = None
        self._base = None
        self.judge = None
        self.cache = {}
        self.ssh_ctl = None
        self.snap0 = None
        self.transcripts = []  # (text, where) seen on stdout/stderr, scanned for the key

    @property
    def env(self):
        if self._env is None:
            self._env = lib.build_env(self.tmp)
            self.snap0 = self._env.roots_snapshot()
        return self._env

    @property
    def base(self):
        """The POC 8 dataset over the environment (18 runs, one item each)."""
        if self._base is None:
            self._base = lib.export_dataset(self.env, "base")
            self._base["snap"] = lib.tree_snapshot(self._base["dir"])
        return self._base

    def item(self, run, ds=None):
        it = lib.harness_item((ds or self.base)["items"], run)
        if it is None:
            raise AssertionError(f"no harness item for {run}")
        return it

    def stub(self):
        if self.judge is None:
            self.judge = lib.Judge().start()
        return self.judge

    def roots(self, name, judge=True, key=lib.DUMMY_KEY, base_url=None):
        r = lib.Roots(self.tmp, name)
        if judge:
            r.judge_env(base_url or self.stub().base_url, key)
        return r

    def label(self, *args, env, expect_rc=None, timeout=900, rss=False):
        res = lib.label(*args, env=env, timeout=timeout, rss=rss)
        self.transcripts.append((res["out"] + res["err"], " ".join(map(str, args[:2]))))
        if not lib.no_traceback(res):
            raise AssertionError(f"traceback on stderr ({args[0]}): {lib.short(res['err'].strip().splitlines()[-1], 200)}")
        if expect_rc is not None and res["rc"] not in (expect_rc if isinstance(expect_rc, tuple) else (expect_rc,)):
            raise AssertionError(f"label {' '.join(map(str, args[:3]))}: rc={res['rc']} (want {expect_rc}) stderr={lib.short(res['err'], 300)!r}")
        return res

    def run(self, r, ds, *args, level="0,1", out=None, env_extra=None, expect_rc=None, timeout=900, rss=False):
        out = out or r.out()
        env = r.env(**(env_extra or {}))
        res = self.label("run", "--dataset", ds["dir"], "--out", out, "--level", level, *args, env=env, expect_rc=expect_rc, timeout=timeout, rss=rss)
        d = lib.read_out(out)
        d["res"] = res
        if res["rc"] in (0, 1) and lib.missing_outputs(out):
            raise AssertionError(f"rc={res['rc']} but outputs missing: {lib.missing_outputs(out)}")
        return d

    # -- the warm level 0,1 run over the base dataset with the adversarial stub (shared by several checks)
    def judged(self):
        if "judged" in self.cache:
            return self.cache["judged"]
        ds = self.base
        j = self.stub()
        runs = lib.runs_of(ds["items"])
        items = {r: self.item(r) for r in runs}
        eligible = [r for r in runs if lib.judged_oracle(items[r])]
        # controlled verdicts: two inconsistent items, one that never answers validly, one slow, the rest by hash
        inc = [items[r]["item_id"] for r in ("run_retail-0_13", "run_retail-0_17")]
        inv = [items["run_retail-0_14"]["item_id"]]
        slow = {items["run_retail-0_11"]["item_id"]: 3}
        verdict = {items["run_retail-0_16"]["item_id"]: "veto", items["run_retail-0_12"]["item_id"]: "pass",
                   items["run_retail-0_15"]["item_id"]: "pass", items["run_retail-0_18"]["item_id"]: "pass",
                   items["run_retail-1_4"]["item_id"]: "veto"}
        must = [s for r in ("run_retail-0_16", "run_retail-1_4") for s in lib.content_strings(items[r])[:40]]
        j.reset(verdict=verdict, inconsistent=inc, always_invalid=inv, delay=slow, faults=True, drift=False,
                quote_reasons=True, keep_bodies=True, must_contain=must, status=None, default="hash")
        r = self.roots("judged")
        d = self.run(r, ds, expect_rc=1)  # one item stays unknown (judge_invalid)
        d.update({"roots": r, "eligible": eligible, "items": items, "inconsistent": inc, "invalid": inv})
        self.cache["judged"] = d
        return d

    # -- the 200-run synthetic dataset with a stub verdict map (gold set, kappa)
    def synth(self):
        if "synth" in self.cache:
            return self.cache["synth"]
        items = lib.synthetic_items(self.base["items"], 200, seed=7)
        ds_dir = lib.write_dataset(os.path.join(self.tmp, "ds_synth"), items, self.base["manifest"])
        ds = {"dir": ds_dir, "items": items, "manifest": json.load(open(os.path.join(ds_dir, "manifest.json")))}
        rnd = random.Random(21)
        verdict = {}
        for it in items:  # judge agrees with the reward 70 % of the time
            agree = rnd.random() < 0.7
            p = it["outcome"]["passed"]
            verdict[it["item_id"]] = ("pass" if p else "veto") if agree else ("veto" if p else "pass")
        self.stub().reset(verdict=verdict, inconsistent=[], always_invalid=[], delay={}, faults=False, drift=False,
                          quote_reasons=False, keep_bodies=False, must_contain=[], status=None, default="hash")
        r = self.roots("synth")
        d = self.run(r, ds, "--gold-n", "60", "--seed", "3", expect_rc=0)
        root0 = os.path.join(self.tmp, "synth_root0")  # as built: later checks copy this, never the live root
        shutil.copytree(r.root, root0)
        d.update({"roots": r, "ds": ds, "verdict": verdict, "root0": root0})
        self.cache["synth"] = d
        return d

    # -- ssh (host mode)
    def ssh_base(self):
        if self.ssh_ctl is None:  # a Unix socket path must stay < 104 bytes: never under macOS's long TMPDIR
            d = tempfile.mkdtemp(prefix="p9-", dir="/tmp")
            self.ssh_dir, self.ssh_ctl = d, os.path.join(d, "cm-%C")
            if len(os.path.join(d, "cm-" + "0" * 40)) >= 100:
                raise AssertionError(f"ssh ControlPath would exceed the socket limit: {d}")
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
        """Remove the suite's own scratch on the host (one-shot ssh, no control master); idempotent."""
        self.ssh_close()
        subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=no", "-o", "ControlPath=none", HOST,
                        f"sudo rm -rf {HOST_TMP} /tmp/poc9-tests-* /tmp/poc9-selftest-*"], capture_output=True, text=True, timeout=120)

    def close(self):
        if self.judge:
            self.judge.stop()
            self.judge = None


def row_by_run(d):
    return {r["run"]: r for r in d["rows"]}


# =============================================================================== A: command surface

@check("A0_cli_surface", "CONTRACT.md §1: exit codes, no tracebacks, usage errors are 2, unknown run is 1 with empty stdout")
def a0(ctx):
    r = ctx.roots("a0", judge=False)
    ds = ctx.base
    probs = []
    for args, want in ((), 2), (("nonsense",), 2), (("run", "--out", r.out()), 2), (("run", "--dataset", ds["dir"], "--out", r.out(), "--level", "3"), 2), \
                      (("run", "--dataset", os.path.join(ctx.tmp, "nope"), "--out", r.out(), "--level", "0"), 2), \
                      (("view", "--dataset", ds["dir"], "--run", "run_retail-99_99"), 1), \
                      (("human", "--dataset", ds["dir"], "--run", "run_retail-0_13", "--verdict", "maybe", "--labeller", "t"), 2), \
                      (("human", "--dataset", ds["dir"], "--run", "run_retail-99_99", "--verdict", "pass", "--labeller", "t"), 1), \
                      (("gold", "--out", os.path.join(ctx.tmp, "nope"), "--next"), 2):
        res = ctx.label(*args, env=r.env())
        if res["rc"] != want:
            probs.append(f"{' '.join(args[:2]) or 'no args'}: rc={res['rc']} want {want}")
        if want == 1 and args[0] == "view" and res["out"].strip():
            probs.append("view of an unknown run printed something")
    # level 1 without a readable env file: exit 2, nothing half-written
    env = r.env(AVOS_JUDGE_ENV=os.path.join(ctx.tmp, "no.env"))
    out = r.out()
    res = ctx.label("run", "--dataset", ds["dir"], "--out", out, "--level", "0,1", env=env)
    if res["rc"] != 2:
        probs.append(f"level 1 with a missing env file: rc={res['rc']} want 2")
    if lib.root_rows(r.root):
        probs.append("rows were written to the label root although no judge was reachable")
    return not probs, "; ".join(probs) or "usage 2, unknown run 1 (empty stdout), missing judge env 2; no tracebacks"


# =============================================================================== L: level 0 on the dataset

@check("L0_rows_match_oracle", "done test 1 / statement level 0: one row per run with reward, termination, structural status and the final label")
def l0(ctx):
    ds = ctx.base
    r = ctx.roots("l0", judge=False)
    d = ctx.run(r, ds, level="0", expect_rc=0)
    runs = lib.runs_of(ds["items"])
    probs = []
    got = [x["run"] for x in d["rows"]]
    if got != runs:
        probs.append(f"rows {len(got)} vs runs {len(runs)}; missing {sorted(set(runs) - set(got))[:3]}; order sorted: {got == sorted(got)}")
        return False, "; ".join(probs)
    kept = 0
    for row in d["rows"]:
        it = ctx.item(row["run"])
        want = lib.level0_oracle(it)
        for k in ("reward", "passed", "termination_reason", "trace_complete", "witness_agrees", "checks", "structural_ok", "reward_informative", "status"):
            if row.get("level0", {}).get(k) != want[k]:
                probs.append(f"{row['run']}.level0.{k} = {row.get('level0', {}).get(k)!r} want {want[k]!r}")
        if sorted(row.get("level0", {}).get("reasons") or []) != sorted(want["reasons"]):
            probs.append(f"{row['run']}.level0.reasons {row.get('level0', {}).get('reasons')} want {want['reasons']}")
        wf = lib.final_oracle(want, None, None)
        if row.get("final") != wf:
            probs.append(f"{row['run']}.final {row.get('final')} want {wf}")
        if row.get("level1") is not None or row.get("level2") is not None:
            probs.append(f"{row['run']}: level1/level2 not null in a level-0 run")
        if row.get("item_id") != it["item_id"] or row.get("task_id") != it["task_id"] or row.get("split") != it["split"] or row.get("content_sha256") != lib.content_sha(it):
            probs.append(f"{row['run']}: item_id/content_sha256/task_id/split differ from the item")
        if row.get("stratum") != lib.stratum_oracle(want, None):
            probs.append(f"{row['run']}.stratum {row.get('stratum')} want {lib.stratum_oracle(want, None)}")
        kept += bool(wf["kept"])
    s = d["summary"] or {}
    c = s.get("counts", {})
    want_t = lib.tiles(len(runs), ds["manifest"]["counts"]["runs_exported"], kept)
    if s.get("tiles") != want_t:
        probs.append(f"tiles {s.get('tiles')} want {want_t}")
    if (c.get("labelled"), c.get("kept"), c.get("dropped")) != (len(runs), kept, len(runs) - kept):
        probs.append(f"counts labelled/kept/dropped {c.get('labelled')}/{c.get('kept')}/{c.get('dropped')} want {len(runs)}/{kept}/{len(runs) - kept}")
    if s.get("levels") != [0] or s.get("exit") != 0:
        probs.append(f"summary.levels {s.get('levels')} exit {s.get('exit')}")
    # the normal empty-NL-assertion shape: a passed, complete, agreeing run is clean and kept
    row13 = row_by_run(d).get("run_retail-0_13", {})
    if row13.get("level0", {}).get("status") != "clean" or not row13.get("final", {}).get("kept"):
        probs.append(f"run_retail-0_13 (nl_assertions [] with NL_ASSERTION basis) not clean/kept: {row13.get('level0', {}).get('reasons')}")
    return not probs, "; ".join(probs[:4]) or f"{len(runs)} rows, {kept} kept; level 0 fields, final labels, strata and tiles equal the oracle"


@check("L0b_never_clean", "statement: flagged / incomplete / unverified / null reward / infra or context errors / τ² #384 are never clean or kept")
def l0b(ctx):
    r = ctx.roots("l0b", judge=False)
    d = ctx.run(r, ctx.base, level="0", expect_rc=0)
    by = row_by_run(d)
    want = {"run_retail-0_91": ("level0", "witness_agrees"), "run_retail-0_92": ("level0", "trace_complete"),
            "run_retail-0_93": ("level0", "trace_complete"), "run_retail-0_90": ("level0", "reward_known"),
            "run_retail-0_95": ("level0", "no_context_overflow"), "run_retail-50_0": ("tau2_384", "tau2_384"),
            "run_coding-slugify_2": ("level0", "witness_agrees"), "run_retail-0_10": ("level0", "trace_complete")}
    probs = []
    for run, (rule, reason) in want.items():
        row = by.get(run)
        if not row:
            probs.append(f"{run}: no row")
            continue
        l0, f = row.get("level0", {}), row.get("final", {})
        if l0.get("status") != "not_clean" or reason not in (l0.get("reasons") or []):
            probs.append(f"{run}: status {l0.get('status')} reasons {l0.get('reasons')} (want {reason})")
        if f.get("kept") is not False or f.get("label") != "fail" or f.get("rule") != rule:
            probs.append(f"{run}: final {f} want fail/{rule}")
    return not probs, "; ".join(probs[:4]) or f"{len(want)} runs each not clean for the right reason, none kept"


@check("L0c_item_hash", "CONTRACT.md §3: an item whose content no longer matches its item_id is not clean and the run exits 1")
def l0c(ctx):
    ds_dir = os.path.join(ctx.tmp, "ds_edited")
    shutil.rmtree(ds_dir, ignore_errors=True)
    shutil.copytree(ctx.base["dir"], ds_dir)
    p = os.path.join(ds_dir, "trajectories.jsonl")
    it = ctx.item("run_retail-0_13")
    lines = open(p, encoding="utf-8").read().split("\n")
    k = next(i for i, l in enumerate(lines) if '"run": "run_retail-0_13"' in l)
    old = None
    for needle in lib.content_strings(it):
        for i in range(0, max(0, len(needle) - 16)):
            w = needle[i:i + 16]
            if w.isascii() and w.isprintable() and "\\" not in w and '"' not in w and lines[k].count(w) == 1:
                old = w
                break
        if old:
            break
    if old is None:
        return False, "suite error: no usable 16-char window in run_retail-0_13"
    st = os.stat(p)
    new = old[:-1] + ("X" if old[-1] != "X" else "Y")
    lines[k] = lines[k].replace(old, new, 1)
    with open(p, "r+b") as f:  # in place, same size, same inode; mtime restored
        f.seek(0)
        f.write("\n".join(lines).encode("utf-8"))
        f.truncate()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    ds = {"dir": ds_dir, "items": lib.read_jsonl(p), "manifest": ctx.base["manifest"]}
    r = ctx.roots("l0c", judge=False)
    d = ctx.run(r, ds, level="0", expect_rc=1)
    bad = [x for x in d["rows"] if x["level0"]["checks"].get("item_hash") is False]
    ok = len(bad) == 1 and bad[0]["run"] == "run_retail-0_13" and bad[0]["final"]["kept"] is False and "item_hash" in bad[0]["level0"]["reasons"]
    return ok, f"edited run_retail-0_13 in place: {[x['run'] for x in bad]} flagged item_hash, kept={bad and bad[0]['final']['kept']}, rc {d['res']['rc']}"


# =============================================================================== J: the judge

@check("J1_judge_protocol", "done test 1 / CONTRACT.md §3: judge rows follow the stub's answers, ≤ 4 attempts, both framings, reasons without transcript text")
def j1(ctx):
    d = ctx.judged()
    j = ctx.stub()
    by = row_by_run(d)
    probs = []
    judged = [r for r, row in by.items() if row.get("level1")]
    if sorted(judged) != sorted(d["eligible"]):
        probs.append(f"judged runs {sorted(set(judged) ^ set(d['eligible']))} differ from the oracle (witness agrees and a policy turn)")
    for run in d["eligible"]:
        row, it = by[run], d["items"][run]
        l1 = row.get("level1") or {}
        calls = j.calls_for(it["item_id"])
        if not calls:
            probs.append(f"{run}: the judge was never asked")
            continue
        bodies = list(j.bodies_by_item.get(it["item_id"], {}))
        if len(bodies) != 2:
            probs.append(f"{run}: {len(bodies)} distinct request bodies (want 2 framings)")
        per_body = {b: sum(1 for c in calls if c["body_sha"] == b) for b in bodies}
        if any(n > TH["attempts"] for n in per_body.values()):
            probs.append(f"{run}: more than {TH['attempts']} attempts on one framing: {per_body}")
        for c in calls:
            if c["auth"] != f"Bearer {lib.DUMMY_KEY}":
                probs.append(f"{run}: Authorization header {str(c['auth'])[:12]!r}")
                break
            if c["temperature"] != 0 or not isinstance(c["seed"], int) or c["model"] != "openai/gpt-oss-120b":
                probs.append(f"{run}: temperature {c['temperature']} seed {c['seed']!r} model {c['model']}")
                break
        # transcript present in every request
        for c in calls:
            miss = [s for s in lib.content_strings(it) if s not in (c["text"] or "")]
            if miss:
                probs.append(f"{run}: {len(miss)} transcript strings missing from the judge request, e.g. {lib.short(miss[0], 40)!r}")
                break
        want_v, served = j.expected_item_verdict(it["item_id"])
        if it["item_id"] in d["invalid"]:
            if l1.get("verdict") != "unknown":
                probs.append(f"{run}: always-invalid answers but verdict {l1.get('verdict')}")
            fr = l1.get("framings") or {}
            if any((fr.get(k) or {}).get("error") != "judge_invalid" for k in ("a", "b")):
                probs.append(f"{run}: framings without error judge_invalid: {fr}")
            continue
        if l1.get("verdict") != want_v:
            probs.append(f"{run}: verdict {l1.get('verdict')} but the stub served {served}")
        fr = l1.get("framings") or {}
        got_answers = sorted(json.dumps((fr.get(k) or {}).get("answers"), sort_keys=True) for k in ("a", "b"))
        want_answers = sorted(json.dumps(j.served[(it["item_id"], b)]["answers"], sort_keys=True) for b in bodies if (it["item_id"], b) in j.served)
        if got_answers != want_answers:
            probs.append(f"{run}: stored answers differ from the answers served")
        want_c = (len(set(served)) == 1) if len(served) == 2 else None
        if l1.get("consistent") != want_c:
            probs.append(f"{run}: consistent {l1.get('consistent')} want {want_c} (served {served})")
        reasons = " ".join(l1.get("reasons") or [])
        if "STUBREASON" not in reasons:
            probs.append(f"{run}: the judge's reasons are not carried")
        if lib.verbatim_leaks(reasons, [it]):
            probs.append(f"{run}: reasons carry ≥ {TH['verbatim']} verbatim transcript characters")
        if (row["final"]["rule"] == "veto") != (want_v == "veto" and row["level0"]["status"] == "clean" and row["level0"]["passed"] is True):
            probs.append(f"{run}: final {row['final']} with judge {want_v} and level0 {row['level0']['status']}/{row['level0']['passed']}")
    s = d["summary"] or {}
    cons = [by[r]["level1"]["consistent"] for r in d["eligible"] if by[r].get("level1") and by[r]["level1"].get("consistent") is not None]
    want_cons = {"n": len(cons), "agree": sum(cons), "rate": (sum(cons) / len(cons)) if cons else None}
    got_cons = (s.get("judge") or {}).get("consistency") or {}
    if got_cons.get("n") != want_cons["n"] or got_cons.get("agree") != want_cons["agree"] or \
            (want_cons["rate"] is not None and abs((got_cons.get("rate") or -1) - want_cons["rate"]) > 1e-9):
        probs.append(f"consistency {got_cons} want {want_cons}")
    if want_cons["agree"] != want_cons["n"] - 2:
        probs.append(f"the two planted inconsistent items are not both inconsistent: {want_cons}")
    if (s.get("judge") or {}).get("calls_made") != j.n_calls:
        probs.append(f"summary.judge.calls_made {(s.get('judge') or {}).get('calls_made')} vs {j.n_calls} HTTP attempts seen")
    if c := s.get("counts", {}):
        if c.get("judged") != len(d["eligible"]) or c.get("judge_unknown") != 1:
            probs.append(f"counts.judged {c.get('judged')} / judge_unknown {c.get('judge_unknown')} want {len(d['eligible'])} / 1")
    if d["res"]["rc"] != 1 or s.get("exit") != 1:
        probs.append(f"rc {d['res']['rc']} summary.exit {s.get('exit')} want 1 (one item unknown)")
    return not probs, "; ".join(probs[:4]) or (f"{len(d['eligible'])} runs judged in {j.n_calls} attempts, verdicts = stub's, consistency "
                                             f"{want_cons['agree']}/{want_cons['n']}, 1 unknown, rc 1")


@check("J1b_storage_rows", "CONTRACT.md §4: level-1 rows are canonical, attributable (model, rubric, framing) and free of transcript text")
def j1b(ctx):
    d = ctx.judged()
    rows = lib.root_rows(d["roots"].root)
    probs = []
    if not rows:
        return False, "no rows under AVOS_LABEL_ROOT"
    bad = [(f, i, lib.row_problems(row) if row else ["not a JSON object"]) for f, i, row, _ in rows if not row or lib.row_problems(row)]
    if bad:
        probs.append(f"{len(bad)} invalid rows, e.g. {bad[0]}")
    l1 = [row for _, _, row, _ in rows if row and row.get("level") == 1]
    items = {it["item_id"]: lib.content_sha(it) for it in d["items"].values()}
    if any(items.get(row["item_id"]) != row["content_sha256"] for row in l1):
        probs.append("a row's content_sha256/item_id pair does not match the dataset")
    keys = {(row["run"], row["content_sha256"], row["framing"]) for row in l1}
    if len(keys) != 2 * len(d["eligible"]) or len(l1) != len(keys):
        probs.append(f"{len(keys)} distinct (run, content, framing) keys in {len(l1)} rows for {len(d['eligible'])} judged runs")
    if any(row["model"] != "openai/gpt-oss-120b" or not re.fullmatch(r"[0-9a-f]{64}", str(row["rubric_sha256"])) for row in l1):
        probs.append("rows without model / rubric sha")
    unk = [row for row in l1 if row["verdict"] == "unknown"]
    if len(unk) != 2 or any(row["error"] != "judge_invalid" for row in unk):
        probs.append(f"{len(unk)} unknown rows (want 2 with error judge_invalid)")
    text = lib.text_of_files(lib.files_under(d["roots"].root, *d["roots"].cache_dirs()))
    leaks = lib.verbatim_leaks(text, list(d["items"].values()))
    if leaks:
        probs.append(f"transcript text of {leaks[:3]} stored under the label root or caches")
    if lib.dummy_key_in(text):
        probs.append("the judge key is stored under the root or caches")
    return not probs, "; ".join(probs[:4]) or f"{len(l1)} level-1 rows canonical, keyed by (item, model, rubric, framing), no transcript text"


@check("J2_rerun_identical_no_calls", "done test 3: a re-run yields byte-identical outputs and asks the judge nothing")
def j2(ctx):
    d = ctx.judged()
    j = ctx.stub()
    r = d["roots"]
    before = lib.out_bytes(d["dir"])
    n0 = j.n_calls
    r.clear_caches()
    d2 = ctx.run(r, ctx.base, expect_rc=1)
    probs = []
    diff = [n for n, b in lib.out_bytes(d2["dir"]).items() if b != before[n]]
    if diff:
        probs.append(f"re-run differs in {diff}")
    if j.n_calls != n0:
        probs.append(f"re-run made {j.n_calls - n0} judge requests")
    jd = (d2["summary"] or {}).get("judge", {})
    if jd.get("calls_made") != 0 or jd.get("calls_reused") != 2 * len(d["eligible"]):
        probs.append(f"summary.judge calls_made {jd.get('calls_made')} calls_reused {jd.get('calls_reused')} want 0 / {2 * len(d['eligible'])}")
    # from a copy of the dataset and root elsewhere
    cp = os.path.join(ctx.tmp, "copy")
    shutil.rmtree(cp, ignore_errors=True)
    shutil.copytree(ctx.base["dir"], os.path.join(cp, "ds"))
    r2 = ctx.roots("j2copy")
    shutil.rmtree(r2.root)
    shutil.copytree(r.root, r2.root)
    d3 = ctx.run(r2, {"dir": os.path.join(cp, "ds")}, expect_rc=1)
    diff = [n for n, b in lib.out_bytes(d3["dir"]).items() if b != before[n]]
    if diff:
        probs.append(f"run from copied dataset+root differs in {diff}")
    if j.n_calls != n0:
        probs.append(f"copied run made {j.n_calls - n0} judge requests")
    return not probs, "; ".join(probs) or "5 output files byte-identical twice (caches emptied; dataset and root copied), 0 judge requests"


@check("J3_changed_rubric_or_model_reasks", "done test 3 (never silently reuse): a changed rubric or model asks the judge again and records the new key")
def j3(ctx):
    d = ctx.judged()
    j = ctx.stub()
    r = d["roots"]
    alt_text = open(RUBRIC_ALT, encoding="utf-8").read()
    j.set(must_contain=[alt_text.strip()[:200]], always_invalid=[], inconsistent=[], delay={})
    n0 = j.n_calls
    probs = []
    d2 = ctx.run(r, ctx.base, "--rubric", RUBRIC_ALT, expect_rc=0)
    made = j.n_calls - n0
    if made < 2 * len(d["eligible"]):
        probs.append(f"--rubric: only {made} requests for {2 * len(d['eligible'])} framings")
    calls = j.calls_for()[n0:]
    if calls and not all(c["contains"].get(alt_text.strip()[:200]) for c in calls):
        probs.append("the rubric file's text is not in every judge request")
    sha = lib.sha_file(RUBRIC_ALT)
    if (d2["summary"] or {}).get("rubric_sha256") != sha or any((x.get("level1") or {}).get("rubric_sha256") not in (None, sha) for x in d2["rows"]):
        probs.append(f"rubric_sha256 is not the file's sha {sha[:8]}")
    n1 = j.n_calls
    d3 = ctx.run(r, ctx.base, "--rubric", RUBRIC_ALT, expect_rc=0)
    if j.n_calls != n1 or lib.out_bytes(d3["dir"]) != lib.out_bytes(d2["dir"]):
        probs.append("second --rubric run asked again or differs")
    d4 = ctx.run(r, ctx.base, "--rubric", RUBRIC_ALT, env_extra={"AVOS_JUDGE_MODEL": "openai/gpt-oss-20b"}, expect_rc=0)
    if j.n_calls - n1 < 2 * len(d["eligible"]):
        probs.append(f"changed model: only {j.n_calls - n1} requests")
    if any(c["model"] != "openai/gpt-oss-20b" for c in j.calls_for()[n1:]):
        probs.append("changed model not sent as `model`")
    if (d4["summary"] or {}).get("model") != "openai/gpt-oss-20b":
        probs.append("summary.model is not the changed model")
    n2 = j.n_calls
    j.set(must_contain=[])
    d5 = ctx.run(r, ctx.base, expect_rc=1)  # the original key: everything stored, incl. the unknown rows
    if j.n_calls != n2:
        probs.append(f"back to the original rubric/model: {j.n_calls - n2} requests (want 0)")
    if lib.out_bytes(d5["dir"]) != lib.out_bytes(d["dir"]):
        probs.append("back to the original key: outputs differ from the first run")
    return not probs, "; ".join(probs) or f"rubric change → {made} new requests with the rubric text; model change → new requests; original key → 0 requests, identical"


@check("J4_no_repeat_without_storage", "statement (temperature 0 does not repeat): with the store emptied the judge is asked again and its new answers are what is recorded")
def j4(ctx):
    d = ctx.judged()
    j = ctx.stub()
    r = ctx.roots("j4")
    j.reset(verdict={}, inconsistent=[], always_invalid=[], delay={}, faults=False, drift=True, quote_reasons=False,
            keep_bodies=False, must_contain=[], default="hash")
    d1 = ctx.run(r, ctx.base, expect_rc=0)
    n1 = j.n_calls
    r.clear_root()
    d2 = ctx.run(r, ctx.base, expect_rc=0)
    probs = []
    if j.n_calls - n1 != 2 * len(d["eligible"]):
        probs.append(f"emptied root: {j.n_calls - n1} requests (want {2 * len(d['eligible'])})")
    by1, by2 = row_by_run(d1), row_by_run(d2)
    changed = [run for run in d["eligible"] if (by1[run].get("level1") or {}).get("verdict") != (by2[run].get("level1") or {}).get("verdict")]
    if not changed:
        probs.append("a drifting judge produced identical labels: the answers were not taken from the judge")
    for run in d["eligible"]:
        want, _ = j.expected_item_verdict(d["items"][run]["item_id"])
        if (by2[run].get("level1") or {}).get("verdict") != want:
            probs.append(f"{run}: recorded {by2[run].get('level1', {}).get('verdict')} but the judge's latest answers give {want}")
            break
    j.set(drift=False)
    return not probs, "; ".join(probs) or f"{len(changed)}/{len(d['eligible'])} verdicts changed when the judge drifted; rows equal the judge's newest answers"


@check("J5_judge_unavailable_or_rejected", "CONTRACT.md §3: unreachable judge → unknown + exit 1 and no row stored; 401 → exit 2; level 0 still written")
def j5(ctx):
    d = ctx.judged()
    probs = []
    # closed port
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = ctx.roots("j5", base_url=f"http://127.0.0.1:{port}/v1")
    d1 = ctx.run(r, ctx.base, expect_rc=1, timeout=1200)
    by = row_by_run(d1)
    for run in d["eligible"]:
        l1 = by[run].get("level1") or {}
        if l1.get("verdict") != "unknown":
            probs.append(f"{run}: verdict {l1.get('verdict')} with no judge")
            break
        fr = l1.get("framings") or {}
        if any((fr.get(k) or {}).get("error") != "judge_unavailable" for k in ("a", "b")):
            probs.append(f"{run}: errors {[(fr.get(k) or {}).get('error') for k in 'ab']} want judge_unavailable")
            break
    if lib.root_rows(r.root):
        probs.append("rows stored for an unreachable judge (a later run could never retry)")
    if (d1["summary"] or {}).get("counts", {}).get("labelled") != len(ctx.base["items"]):
        probs.append("level 0 rows missing when the judge is down")
    # the same root now reaches the judge: it asks (nothing was stored)
    j = ctx.stub()
    j.set(status=None, drift=False, faults=False)
    r.judge_env(j.base_url)
    n0 = j.n_calls
    ctx.run(r, ctx.base, expect_rc=0)
    if j.n_calls - n0 < 2 * len(d["eligible"]):
        probs.append(f"after the judge came back only {j.n_calls - n0} requests were made")
    # credentials rejected
    j.set(status=401)
    r2 = ctx.roots("j5b")
    res = ctx.label("run", "--dataset", ctx.base["dir"], "--out", r2.out(), "--level", "0,1", env=r2.env())
    j.set(status=None)
    if res["rc"] != 2:
        probs.append(f"401 from the judge: rc={res['rc']} want 2")
    if lib.root_rows(r2.root):
        probs.append("rows stored after 401")
    return not probs, "; ".join(probs) or "closed port → unknown/judge_unavailable, rc 1, no rows, level 0 complete; judge back → asked; 401 → rc 2"


@check("J6_secret_hygiene", "statement (never print the key): the judge key appears in no output, row, cache, stdout or stderr")
def j6(ctx):
    d = ctx.judged()
    paths = lib.files_under(d["roots"].dir)
    hits = [p for p in paths if not p.endswith("judge.env") and lib.dummy_key_in(open(p, "rb").read().decode("utf-8", "replace"))]
    outs = [w for t, w in ctx.transcripts if lib.dummy_key_in(t)]
    ok = not hits and not outs
    return ok, "key nowhere" if ok else f"key found in files {hits[:2]} / outputs of {outs[:2]}"


# =============================================================================== G: blind view, gold set, human labels, M10

@check("G1_blind_view", "done test 2 / CONTRACT.md §7: the view shows the whole transcript and leaks no outcome, verdict, flag or label, for every run")
def g1(ctx):
    d = ctx.judged()
    r = d["roots"]
    probs = []
    n = 0
    for run in lib.runs_of(ctx.base["items"]):
        it = ctx.item(run)
        if not it["messages"]:
            continue
        for html in (False, True):
            res = ctx.label("view", "--dataset", ctx.base["dir"], "--run", run, *(["--html"] if html else []), env=r.env())
            if res["rc"] != 0:
                probs.append(f"{run}{' html' if html else ''}: rc={res['rc']} {lib.short(res['err'], 80)}")
                continue
            text = lib.strip_html(res["out"]) if html else res["out"]
            if html and "<pre" not in res["out"]:
                probs.append(f"{run}: --html output is not HTML")
            miss = lib.view_has_transcript(text, it)
            if miss:
                probs.append(f"{run}: {len(miss)} message strings missing from the view, e.g. {miss[0]!r}")
            for needle in (f"run={run}", f"item={it['item_id']}", "tools="):
                if needle not in text:
                    probs.append(f"{run}: header lacks {needle[:12]}")
            leaks, _ = lib.blind_leaks(text, it)
            if leaks:
                probs.append(f"{run}: outcome words in the blind view: {leaks[:4]}")
            n += 1
    # AVOS_LABEL_DATASET default
    res = ctx.label("view", "--run", "run_retail-0_13", env=r.env(AVOS_LABEL_DATASET=ctx.base["dir"]))
    if res["rc"] != 0:
        probs.append("AVOS_LABEL_DATASET is not honoured by view")
    return not probs, "; ".join(probs[:4]) or f"{n} views (text and HTML) carry every message and no outcome/verdict/label word"


@check("G2_gold_selection", "statement (50–100, stratified, deterministic, resumable): gold.json equals the §8 algorithm on the rows; seeds and n change it; adding labels does not")
def g2(ctx):
    d = ctx.synth()
    probs = []
    strata = {"disagree": [], "passed": [], "failed": []}
    for row in d["rows"]:
        if row.get("stratum"):
            strata[row["stratum"]].append(row["run"])
    # strata recomputed from the stub's verdict map and the oracle
    for row in d["rows"]:
        it = lib.harness_item(d["ds"]["items"], row["run"])
        l0 = lib.level0_oracle(it)
        want = lib.stratum_oracle(l0, d["verdict"][it["item_id"]])
        if row.get("stratum") != want:
            probs.append(f"{row['run']}: stratum {row.get('stratum')} want {want}")
            break
    g = d["gold"] or {}
    want = lib.gold_oracle(strata, 60, 3)
    if g.get("runs") != want["runs"] or g.get("picked") != want["picked"] or g.get("short") is not False or g.get("n") != 60:
        probs.append(f"gold set differs from the §8 algorithm: {len(g.get('runs') or [])} runs, picked sizes {({k: len(v) for k, v in (g.get('picked') or {}).items()})} want {({k: len(v) for k, v in want['picked'].items()})}")
    if {k: sorted(v) for k, v in (g.get("strata") or {}).items()} != {k: sorted(v) for k, v in strata.items()}:
        probs.append("gold.strata are not the eligible runs by stratum")
    if g.get("labelled") != [] or g.get("left") != want["runs"]:
        probs.append("labelled/left wrong before any human label")
    # another seed and n: different, still the algorithm
    r = d["roots"]
    d2 = ctx.run(r, d["ds"], "--gold-n", "80", "--seed", "4", expect_rc=0)
    want2 = lib.gold_oracle(strata, 80, 4)
    if (d2["gold"] or {}).get("runs") != want2["runs"]:
        probs.append("seed 4 / n 80: gold set differs from the algorithm")
    if (d2["gold"] or {}).get("runs") == g.get("runs"):
        probs.append("seed 4 / n 80 gave the same set as seed 3 / n 60")
    d3 = ctx.run(r, d["ds"], "--gold-n", "7", "--seed", "3", expect_rc=0)  # clamped to 50
    if (d3["gold"] or {}).get("n") != 50 or len((d3["gold"] or {}).get("runs") or []) != 50:
        probs.append(f"--gold-n 7 not clamped to 50: n={(d3['gold'] or {}).get('n')}")
    # resumable: label three, --next is left[0], --status counts
    env = r.env(AVOS_LABEL_DATASET=d["ds"]["dir"])
    for run in want["runs"][:3]:
        ctx.label("human", "--run", run, "--verdict", "pass", "--labeller", "tester", env=env, expect_rc=0)
    d4 = ctx.run(r, d["ds"], "--gold-n", "60", "--seed", "3", expect_rc=0)
    g4 = d4["gold"] or {}
    if g4.get("runs") != want["runs"] or sorted(g4.get("labelled") or []) != want["runs"][:3] or g4.get("left") != want["runs"][3:]:
        probs.append("after 3 human labels the set changed or labelled/left are wrong")
    nxt = ctx.label("gold", "--out", d4["dir"], "--next", env=env, expect_rc=0)
    if nxt["out"].strip() != want["runs"][3]:
        probs.append(f"gold --next printed {nxt['out'].strip()!r} want {want['runs'][3]}")
    st = ctx.label("gold", "--out", d4["dir"], "--status", env=env, expect_rc=0)
    try:
        sj = json.loads(st["out"])
    except ValueError:
        sj = {}
    if (sj.get("selected"), sj.get("labelled"), sj.get("left")) != (60, 3, 57):
        probs.append(f"gold --status {sj} want selected 60 labelled 3 left 57")
    # small dataset: fewer than 50 eligible → short, all picked
    jd = ctx.judged()
    gj = jd["gold"] or {}
    elig = sorted(row["run"] for row in jd["rows"] if row.get("stratum"))
    if gj.get("short") is not True or gj.get("runs") != elig:
        probs.append(f"18-run dataset: short={gj.get('short')} runs {len(gj.get('runs') or [])} want all {len(elig)} eligible")
    return not probs, "; ".join(probs[:4]) or f"200 runs: strata {({k: len(v) for k, v in strata.items()})}, gold 60 = algorithm; seed/n change it; 3 labels → next/status right; 18-run set short"


def _plan_human(ctx, d, runs, rnd_seed=5):
    """Human verdicts for the gold runs: agree with the judge 80 % of the time; returns {run: verdict}."""
    rnd = random.Random(rnd_seed)
    by = row_by_run(d)
    plan = {}
    for run in runs:
        jv = (by[run].get("level1") or {}).get("verdict")
        base = "pass" if jv == "pass" else "fail"
        plan[run] = base if rnd.random() < 0.8 else ("fail" if base == "pass" else "pass")
    return plan


@check("G3_m10_kappa", "done test 2 / M10: kappa(judge, human) and kappa(reward, human) with SE and CI follow the formula from the rows; < 50 labels reads unverified")
def g3(ctx):
    d = ctx.synth()
    r = d["roots"]
    env = r.env(AVOS_LABEL_DATASET=d["ds"]["dir"])
    gold = (d["gold"] or {}).get("runs") or []
    if len(gold) != 60:
        return False, f"gold set has {len(gold)} runs"
    plan = _plan_human(ctx, d, gold)
    # 20 labels: unverified, values null
    for run in gold[:20]:
        ctx.label("human", "--run", run, "--verdict", plan[run], "--labeller", "tester", env=env, expect_rc=0)
    d1 = ctx.run(r, d["ds"], "--gold-n", "60", "--seed", "3", expect_rc=0)
    probs = []
    m = (d1["summary"] or {}).get("m10") or {}
    for k in ("kappa_judge_human", "kappa_reward_human"):
        got = m.get(k) or {}
        want = lib.kappa_from_rows(d1["rows"], set(gold), "judge" if k.startswith("kappa_judge") else "reward")
        if got.get("status") != "unverified" or got.get("value") is not None or got.get("ci95") is not None or not str(got.get("reason", "")).startswith("n="):
            probs.append(f"{k} with 20 labels: {got.get('status')} value {got.get('value')} reason {got.get('reason')!r} (want unverified, null)")
        if lib.kappa_diff(want, got):
            probs.append(f"{k} (20 labels) parts differ: {lib.kappa_diff(want, got)[:2]}")
    if not re.search(r"M10 kappa\(judge, human\): unverified \(n=\d+ < 50\)", d1["card"] or ""):
        probs.append("CARD.md does not read 'M10 kappa(judge, human): unverified (n=… < 50)'")
    # the remaining 40, one relabel and a second labeller
    for run in gold[20:]:
        ctx.label("human", "--run", run, "--verdict", plan[run], "--labeller", "tester", env=env, expect_rc=0)
    flip = gold[25]
    other = "fail" if plan[flip] == "pass" else "pass"
    time.sleep(1.1)  # a later second: the newest row must win even with second-resolution timestamps
    ctx.label("human", "--run", flip, "--verdict", other, "--labeller", "second", "--note", "relabelled", env=env, expect_rc=0)
    plan[flip] = other
    d2 = ctx.run(r, d["ds"], "--gold-n", "60", "--seed", "3", expect_rc=0)
    by2 = row_by_run(d2)
    m = (d2["summary"] or {}).get("m10") or {}
    for k in ("kappa_judge_human", "kappa_reward_human"):
        got = m.get(k) or {}
        want = lib.kappa_from_rows(d2["rows"], set(gold), "judge" if k.startswith("kappa_judge") else "reward")
        if lib.kappa_diff(want, got):
            probs.append(f"{k}: {lib.kappa_diff(want, got)[:3]}")
        if want["status"] != "measured":
            probs.append(f"{k}: the oracle itself is unverified ({want['reason']}); planted data wrong")
        else:
            name = "kappa(judge, human)" if k == "kappa_judge_human" else "kappa(reward, human)"
            line = f"M10 {name} = {want['value']:.3f} (95% CI {want['ci95'][0]:.3f}–{want['ci95'][1]:.3f}, n={want['n']})"
            if line not in (d2["card"] or ""):
                probs.append(f"CARD.md lacks the line for {k} ({want['value']:.3f}, n={want['n']})")
    if (m.get("gold") or {}).get("labelled") != 60 or (m.get("gold") or {}).get("selected") != 60 or (m.get("gold") or {}).get("required") != 50:
        probs.append(f"m10.gold {m.get('gold')}")
    # level 2 rows: current = newest, history kept, attribution
    l2 = by2[flip].get("level2") or {}
    if l2.get("verdict") != other or l2.get("labeller") != "human:second" or l2.get("history") != 2:
        probs.append(f"relabelled run: level2 {l2} want verdict {other} by human:second, history 2")
    rows = [row for _, _, row, _ in lib.root_rows(r.root) if row and row.get("level") == 2 and row["run"] == flip]
    if len(rows) != 2 or {x["labeller"] for x in rows} != {"human:tester", "human:second"} or not all(not lib.row_problems(x) for x in rows):
        probs.append(f"storage holds {len(rows)} level-2 rows for the relabelled run (want both, canonical)")
    # the disagreement rule with humans: settled vetoes, reward-fail stays fail, counts by rule
    for run in gold:
        row = by2[run]
        want = lib.final_oracle(row["level0"], (row.get("level1") or {}).get("verdict"), plan[run])
        if row["final"] != want:
            probs.append(f"{run}: final {row['final']} want {want} (human {plan[run]}, reward {row['level0']['passed']}, judge {(row.get('level1') or {}).get('verdict')})")
            break
    settled = [run for run in gold if by2[run]["final"]["rule"] == "human" and (by2[run].get("level1") or {}).get("verdict") == "veto" and by2[run]["level0"]["passed"] is True]
    if not settled:
        probs.append("no reward-pass/judge-veto run settled by a human label (done test 4)")
    rf = [run for run in gold if by2[run]["level0"]["passed"] is False and plan[run] == "pass"]
    if rf and any(by2[run]["final"]["label"] != "fail" for run in rf):
        probs.append("a human 'pass' promoted a reward-failed run")
    c = (d2["summary"] or {}).get("counts") or {}
    want_rule = dict(sorted(__import__("collections").Counter(row["final"]["rule"] for row in d2["rows"]).items()))
    if dict(sorted((c.get("by_rule") or {}).items())) != want_rule or c.get("human_labelled") != 60:
        probs.append(f"counts.by_rule {c.get('by_rule')} want {want_rule}; human_labelled {c.get('human_labelled')}")
    kept = sum(1 for row in d2["rows"] if row["final"]["kept"])
    if (d2["summary"] or {}).get("tiles") != lib.tiles(len(d2["rows"]), 200, kept):
        probs.append(f"tiles {(d2['summary'] or {}).get('tiles')} want {lib.tiles(len(d2['rows']), 200, kept)}")
    if (d2["manifest"] or {}).get("m10") != m:
        probs.append("manifest.m10 differs from summary.m10")
    kj = m.get("kappa_judge_human", {})
    return not probs, "; ".join(probs[:4]) or (f"60 gold labels: kappa(judge,human) {kj.get('value', 0):.3f} ± {1.96 * kj.get('se', 0):.3f} (n={kj.get('n')}), "
                                             f"kappa(reward,human) {m.get('kappa_reward_human', {}).get('value', 0):.3f}; 20 labels → unverified; {len(settled)} vetoes settled")


@check("G4_stale_label_reported", "CONTRACT.md §3: a human label whose item changed is reported, never applied; exit 1")
def g4(ctx):
    e = ctx.env
    # a human label on run_retail-0_13, then the witness of that run changes (a word in a reply) → new item_id
    r = ctx.roots("g4", judge=False)
    ds0 = ctx.base
    env = r.env(AVOS_LABEL_DATASET=ds0["dir"])
    it0 = ctx.item("run_retail-0_13")
    ctx.label("human", "--run", "run_retail-0_13", "--verdict", "fail", "--labeller", "tester", env=env, expect_rc=0)
    d0 = ctx.run(r, ds0, level="0", expect_rc=0)
    if row_by_run(d0)["run_retail-0_13"]["final"] != {"label": "fail", "rule": "human", "kept": False}:
        return False, f"before the change the human label is not applied: {row_by_run(d0)['run_retail-0_13']['final']}"
    L = lib.L
    # re-export after editing the witness text (token counts unchanged): POC 8 flags it, item_id changes
    p = os.path.join(e.results, "run_retail-0_13", "result.json")
    backup = open(p, "rb").read()
    try:
        sim = json.load(open(p))
        L.mut_reply_text(sim["simulations"][0])
        json.dump(sim, open(p, "w"))
        ds1 = lib.export_dataset(e, "g4")
    finally:
        open(p, "wb").write(backup)
    it1 = lib.harness_item(ds1["items"], "run_retail-0_13")
    if it1["item_id"] == it0["item_id"]:
        return False, "suite error: the re-export did not change the item"
    d1 = ctx.run(r, ds1, level="0", expect_rc=1)
    row = row_by_run(d1)["run_retail-0_13"]
    s = d1["summary"] or {}
    stale = [x for x in s.get("stale_labels") or [] if x.get("run") == "run_retail-0_13"]
    probs = []
    if row.get("level2") is not None or row["final"]["rule"] == "human":
        probs.append(f"stale label applied: level2 {row.get('level2')} final {row['final']}")
    if not stale or stale[0].get("labelled_content_sha256") != lib.content_sha(it0) or stale[0].get("current_content_sha256") != lib.content_sha(it1) \
            or stale[0].get("labelled_item_id") != it0["item_id"] or stale[0].get("current_item_id") != it1["item_id"] or stale[0].get("labeller") != "human:tester":
        probs.append(f"summary.stale_labels {stale} lacks the old/current content keys, item ids or the labeller")
    if s.get("exit") != 1:
        probs.append(f"summary.exit {s.get('exit')}")
    # the user relabels the new content: exit 0 again, the stale row still listed
    ctx.label("human", "--dataset", ds1["dir"], "--run", "run_retail-0_13", "--verdict", "fail", "--labeller", "tester", env=r.env(), expect_rc=0)
    d2 = ctx.run(r, ds1, level="0", expect_rc=0)
    s2 = d2["summary"] or {}
    if not [x for x in s2.get("stale_labels") or [] if x.get("run") == "run_retail-0_13"] or s2.get("exit") != 0 \
            or (row_by_run(d2)["run_retail-0_13"].get("level2") or {}).get("verdict") != "fail":
        probs.append(f"after relabelling the new content: exit {s2.get('exit')}, stale listed {len(s2.get('stale_labels') or [])}, level2 {row_by_run(d2)['run_retail-0_13'].get('level2')}")
    return not probs, "; ".join(probs) or "old human label listed as stale with both keys and not applied (rc 1); after relabelling the new content rc 0 and still listed"


@check("G5_key_is_content", "ruling (label key): a re-export changing only evidence/cost/host keeps human labels applied and judge answers reused; a one-word edit makes them stale")
def g5(ctx):
    d = ctx.judged()
    j = ctx.stub()
    j.set(faults=False, always_invalid=[], inconsistent=[], delay={}, drift=False, must_contain=[])
    r = ctx.roots("g5")
    shutil.rmtree(r.root)
    shutil.copytree(d["roots"].root, r.root)
    env = r.env(AVOS_LABEL_DATASET=ctx.base["dir"])
    runs = ["run_retail-0_13", "run_retail-0_12", "run_retail-0_16"]
    for run, v in zip(runs, ("fail", "pass", "pass")):
        ctx.label("human", "--run", run, "--verdict", v, "--labeller", "tester", env=env, expect_rc=0)
    d0 = ctx.run(r, ctx.base, expect_rc=1)
    # the same runs with non-content fields changed, item_id recomputed (POC 8 would emit this after an import / pricing / another host)
    items = json.loads(json.dumps(ctx.base["items"]))
    for it in items:
        it["host"] = "another-host"
        it["cost"]["usd"] = 0.0123
        if it["evidence"] == "ledger":
            it["evidence"] = "store"
        it["trace_complete"]["ratio"] = 0.999 if it["trace_complete"]["ratio"] is not None else None
        it["lineage"]["events"] = it["lineage"]["events"] + ["f" * 64]
        it["item_id"] = lib.L.canon_item_id(it)
    ds1 = {"dir": lib.write_dataset(os.path.join(ctx.tmp, "ds_noncontent"), items, ctx.base["manifest"]), "items": items}
    n0 = j.n_calls
    d1 = ctx.run(r, ds1, expect_rc=1)
    probs = []
    by0, by1 = row_by_run(d0), row_by_run(d1)
    if j.n_calls != n0:
        probs.append(f"non-content re-export: {j.n_calls - n0} judge requests (answers not reused)")
    for run in runs:
        if (by1[run].get("level2") or {}).get("verdict") != (by0[run].get("level2") or {}).get("verdict") or by1[run]["final"] != by0[run]["final"]:
            probs.append(f"{run}: human label lost after a non-content change: {by1[run].get('level2')} final {by1[run]['final']}")
    if (d1["summary"] or {}).get("stale_labels"):
        probs.append(f"stale_labels reported for a non-content change: {len(d1['summary']['stale_labels'])}")
    if any(by1[run]["item_id"] == by0[run]["item_id"] for run in runs):
        probs.append("suite error: item_id did not change")
    # one word in one reply of run_retail-0_13: stale, reported, not applied; the other two labels stay
    items2 = json.loads(json.dumps(items))
    it = lib.harness_item(items2, "run_retail-0_13")
    m = next(m for m in it["messages"] if m["role"] == "assistant" and isinstance(m.get("content"), str) and len(m["content"]) > 20)
    m["content"] = m["content"].replace(" ", " indeed ", 1)
    it["item_id"] = lib.L.canon_item_id(it)
    ds2 = {"dir": lib.write_dataset(os.path.join(ctx.tmp, "ds_oneword"), items2, ctx.base["manifest"]), "items": items2}
    d2 = ctx.run(r, ds2, expect_rc=1)
    by2 = row_by_run(d2)
    s = d2["summary"] or {}
    stale = [x["run"] for x in s.get("stale_labels") or []]
    if stale != ["run_retail-0_13"] or by2["run_retail-0_13"].get("level2") is not None or by2["run_retail-0_13"]["final"]["rule"] == "human":
        probs.append(f"one-word edit: stale {stale}, level2 {by2['run_retail-0_13'].get('level2')}, final {by2['run_retail-0_13']['final']}")
    for run in runs[1:]:
        if (by2[run].get("level2") or {}).get("verdict") != (by0[run].get("level2") or {}).get("verdict"):
            probs.append(f"{run}: label lost although its content did not change")
    if j.n_calls - n0 != 2:
        probs.append(f"one-word edit: {j.n_calls - n0} judge requests (want 2: only the edited run, both framings)")
    return not probs, "; ".join(probs[:4]) or "evidence/cost/host/lineage change: 3 labels kept, 0 requests, no stale; one-word edit: that run stale and reported (rc 1), 2 requests, others kept"


# =============================================================================== M: manifest, card, POC 8 untouched

@check("M1_manifest_card", "done test 3 / dataset card: the manifest is POC 8's plus labels and M10; the card carries the tiles, M10 lines and the rule")
def m1(ctx):
    d = ctx.judged()
    m, base = d["manifest"] or {}, ctx.base["manifest"]
    probs = []
    if m.get("format") != "avos-retrain-manifest/1+labels":
        probs.append(f"format {m.get('format')!r}")
    for k, v in base.items():
        if k != "format" and m.get(k) != v:
            probs.append(f"manifest.{k} no longer equals the dataset manifest's")
    extra = set(m) - set(base) - {"labels", "m10", "judge"}
    if extra:
        probs.append(f"unexpected manifest keys {sorted(extra)}")
    lab = m.get("labels") or {}
    by = row_by_run(d)
    want_items = [{"item_id": x["item_id"], "run": x["run"], **({"label": by[x["run"]]["final"]["label"], "rule": by[x["run"]]["final"]["rule"], "kept": by[x["run"]]["final"]["kept"]}
                                                                if x["run"] in by and by[x["run"]]["item_id"] == x["item_id"] else {"label": None, "rule": None, "kept": None})}
                  for x in base["items"]]
    if lab.get("items") != want_items:
        probs.append("manifest.labels.items is not (item_id, run, label, rule, kept) in dataset order")
    kept = sum(1 for x in d["rows"] if x["final"]["kept"])
    if lab.get("counts") != {"labelled": len(d["rows"]), "kept": kept, "dropped": len(d["rows"]) - kept}:
        probs.append(f"manifest.labels.counts {lab.get('counts')}")
    if lab.get("label_set_sha256") != lib.sha_file(os.path.join(d["dir"], "labels.jsonl")):
        probs.append("label_set_sha256 is not the sha of labels.jsonl")
    if lab.get("rule") != lib.RULE_SENTENCE:
        probs.append("manifest.labels.rule is not the statement's sentence")
    if m.get("m10") != (d["summary"] or {}).get("m10") or (m.get("judge") or {}).get("model") != "openai/gpt-oss-120b":
        probs.append("manifest.m10 / judge differ from the summary")
    card = d["card"] or ""
    s = d["summary"] or {}
    for needle in (s.get("tiles", {}).get("labelled", "\0"), s.get("tiles", {}).get("kept", "\0"), lib.RULE_SENTENCE, "openai/gpt-oss-120b",
                   "M10 kappa(judge, human): unverified", "M10 kappa(reward, human): unverified", str(s.get("rubric_sha256"))):
        if needle not in card:
            probs.append(f"CARD.md lacks {lib.short(needle, 50)!r}")
    cons = (s.get("judge") or {}).get("consistency") or {}
    if cons.get("rate") is not None and f"{cons['rate']:.3f}" not in card:
        probs.append("CARD.md lacks the consistency rate")
    for k in ("summary", "manifest"):
        txt = json.dumps(d[k])
        if ctx.tmp in txt or re.search(r'"20\d\d-\d\d-\d\dT', txt.replace('"ts"', "")) and k == "summary":
            probs.append(f"{k} carries an absolute path or wall-clock")
    return not probs, "; ".join(probs[:4]) or "manifest = dataset manifest + labels/m10/judge; card has tiles, M10 lines, model, rubric, consistency, rule"


@check("M2_poc8_untouched", "CONTRACT.md §13: the dataset directory is never written; POC 8's export is byte-identical to the frozen pre-POC 9 value")
def m2(ctx):
    ds = ctx.base
    probs = []
    if lib.tree_snapshot(ds["dir"]) != ds["snap"]:
        probs.append("files under the dataset directory changed")
    if ds["manifest"]["dataset_sha256"] != EXPECTED["dataset_sha256"] or ds["manifest"]["counts"] != EXPECTED["counts"]:
        probs.append(f"POC 8 export over the fixture environment changed: dataset_sha256 {ds['manifest']['dataset_sha256'][:12]} vs frozen {EXPECTED['dataset_sha256'][:12]}, counts {ds['manifest']['counts']}")
    again = lib.export_dataset(ctx.env, "m2")
    for n in ("trajectories.jsonl", "sft.jsonl", "manifest.json", "stats.json"):
        if open(os.path.join(again["dir"], n), "rb").read() != open(os.path.join(ds["dir"], n), "rb").read():
            probs.append(f"POC 8 {n} differs between two exports")
    return not probs, "; ".join(probs) or f"dataset untouched; export dataset_sha256 {EXPECTED['dataset_sha256'][:12]}… as frozen; re-export identical"


@check("M3_poc8_suite", "CONTRACT.md §13: POC 8's own suite still passes (quick: A0 and D4; full: --quick)")
def m3(ctx, quick_only=None):
    runs = [["--only", "A0"], ["--only", "D4"]] if ctx.quick else [["--quick"]]
    probs = []
    for extra in runs:
        p = subprocess.run([sys.executable, os.path.join(lib.RETRAIN_TESTS, "run.py"), *extra], capture_output=True, text=True, timeout=3600, cwd=lib.ROOT)
        fails = [l for l in p.stdout.splitlines() if l.startswith("FAIL")]
        if p.returncode != 0 or fails:
            probs.append(f"retrain/tests/run.py {' '.join(extra)}: rc {p.returncode}, {len(fails)} FAIL, e.g. {lib.short(fails[0] if fails else p.stderr, 160)}")
    if not ctx.quick:
        p = subprocess.run([sys.executable, os.path.join(lib.ROOT, "collect", "tests", "run.py"), "--selftest"], capture_output=True, text=True, timeout=1800, cwd=lib.ROOT)
        if p.returncode != 0:
            probs.append(f"collect selftest rc {p.returncode}")
    return not probs, "; ".join(probs) or f"POC 8 suite {' / '.join(' '.join(x) for x in runs)} passes" + ("" if ctx.quick else "; collect selftest passes")


# =============================================================================== X: tampering, merge, transcripts

@check("X1_row_replaced_same_size", "CONTRACT.md §4/§11: a label row edited in place (same size, mtime, inode) is invalid: reported, never applied, exit 1")
def x1(ctx):
    d = ctx.synth()
    r = ctx.roots("x1")
    shutil.rmtree(r.root)
    shutil.copytree(d["root0"], r.root)
    env = r.env(AVOS_LABEL_DATASET=d["ds"]["dir"])
    run = (d["gold"] or {}).get("runs", ["run_retail-0_10000"])[0]
    it = lib.harness_item(d["ds"]["items"], run)
    v0 = "pass" if it["outcome"]["passed"] else "fail"
    ctx.label("human", "--run", run, "--verdict", v0, "--labeller", "tester", env=env, expect_rc=0)
    d1 = ctx.run(r, d["ds"], "--gold-n", "60", "--seed", "3", expect_rc=0)
    before = row_by_run(d1)[run]
    # flip the verdict inside the stored row, same length
    files = [f for f in os.listdir(r.root) if f.endswith(".jsonl")]
    hit = None
    for f in files:
        p = os.path.join(r.root, f)
        txt = open(p, "rb").read()
        if f'"verdict": "{v0}"'.encode() in txt or f'"verdict":"{v0}"'.encode() in txt:
            if b'"level": 2' in txt or b'"level":2' in txt:
                hit = p
                break
    if hit is None:
        return False, "suite error: could not find the stored level-2 row"
    v1 = "fail" if v0 == "pass" else "pass"
    txt = open(hit, "rb").read()
    old = f'"verdict": "{v0}"' if f'"verdict": "{v0}"'.encode() in txt else f'"verdict":"{v0}"'
    lib.same_size_replace(hit, old, old.replace(v0, v1), count=1)
    d2 = ctx.run(r, d["ds"], "--gold-n", "60", "--seed", "3", expect_rc=1)
    after = row_by_run(d2)[run]
    s = d2["summary"] or {}
    probs = []
    if after.get("level2") and after["level2"].get("verdict") == v1:
        probs.append("the edited verdict was applied")
    if not s.get("invalid_rows"):
        probs.append("summary.invalid_rows is empty")
    if after.get("level2") is not None and after["level2"] == before.get("level2"):
        probs.append("the invalid row is still presented as the current label")
    if s.get("exit") != 1:
        probs.append(f"summary.exit {s.get('exit')}")
    return not probs, "; ".join(probs) or f"edited row ({v0}→{v1}, same size/mtime) reported in invalid_rows, not applied, rc 1"


@check("X2_root_tampered", "CONTRACT.md §4/§11: truncated / bit-flipped / swapped label files: identical answer or exit 1 naming invalid rows, never a traceback")
def x2(ctx):
    d = ctx.judged()
    j = ctx.stub()
    j.set(faults=False, always_invalid=[], inconsistent=[], delay={}, drift=False, must_contain=[])
    clean, clean_rc = lib.out_bytes(d["dir"]), d["res"]["rc"]
    probs = []
    rnd = random.Random(9)
    for mode in ("swap", "flip", "truncate"):
        r = ctx.roots(f"x2{mode}")
        shutil.rmtree(r.root)
        shutil.copytree(d["roots"].root, r.root)
        n = lib.tamper_dir(r.root, mode, rnd)
        n0 = j.n_calls
        res = ctx.label("run", "--dataset", ctx.base["dir"], "--out", r.out(), "--level", "0,1", env=r.env())
        out = lib.read_out(r.outs + "/o1")
        s = out["summary"] or {}
        if res["rc"] not in (0, 1):
            probs.append(f"{mode}: rc {res['rc']} {lib.short(res['err'], 100)}")
            continue
        if mode == "swap" and (res["rc"] != clean_rc or lib.out_bytes(r.outs + "/o1") != clean):
            probs.append(f"swap of {n} files (same rows, other names) changed the answer: rc {res['rc']}")
        if mode == "flip" and (res["rc"] != 1 or not s.get("invalid_rows")):
            probs.append(f"flip: rc {res['rc']}, invalid_rows {len(s.get('invalid_rows') or [])} (a damaged row must be reported)")
        if mode == "truncate" and (res["rc"] == 0 and lib.out_bytes(r.outs + "/o1") != clean and not s.get("invalid_rows")) and j.n_calls == n0:
            probs.append("truncate: rows were lost yet nothing was reported and the judge was not asked again")
        if len(out["rows"]) != len(ctx.base["items"]):
            probs.append(f"{mode}: {len(out['rows'])} rows")
        if s.get("judge", {}).get("calls_made", 0) != j.n_calls - n0:
            probs.append(f"{mode}: calls_made {s.get('judge', {}).get('calls_made')} vs {j.n_calls - n0} requests seen")
    return not probs, "; ".join(probs) or "swap → identical; flip/truncate → identical or rc 1 with invalid_rows (lost rows re-asked)"


@check("X3_cache_tampered", "CONTRACT.md §11: a tampered, truncated, swapped or deleted cache never gives a stale or empty answer with exit 0")
def x3(ctx):
    d = ctx.judged()
    r = d["roots"]
    j = ctx.stub()
    j.set(faults=False, always_invalid=[], inconsistent=[], delay={}, drift=False, must_contain=[])
    clean = lib.out_bytes(d["dir"])
    probs = []
    rnd = random.Random(4)
    n_files = sum(len(lib.files_under(x)) for x in r.cache_dirs())
    for mode in ("truncate", "flip", "swap", "delete"):
        ctx.run(r, ctx.base, expect_rc=1)  # warm
        if mode == "delete":
            r.clear_caches()
        else:
            for x in r.cache_dirs():
                lib.tamper_dir(x, mode, rnd)
        res = ctx.label("run", "--dataset", ctx.base["dir"], "--out", r.out(f"x3{mode}"), "--level", "0,1", env=r.env())
        got = lib.out_bytes(os.path.join(r.outs, f"x3{mode}"))
        if res["rc"] == 1 and got == clean:
            continue
        if res["rc"] in (0, 1) and got != clean:
            probs.append(f"{mode}: rc {res['rc']} with outputs that differ from the clean run")
        elif res["rc"] not in (0, 1, 2):
            probs.append(f"{mode}: rc {res['rc']}")
    r.clear_caches()
    return not probs, "; ".join(probs) or f"{n_files} cache files tampered four ways: outputs byte-identical to the clean run every time"


@check("MG1_merge_order_independent", "statement (two hosts / two labellers): merging roots A←B and B←A gives identical labels")
def mg1(ctx):
    d = ctx.synth()
    gold = (d["gold"] or {}).get("runs") or []
    ra, rb = ctx.roots("mgA"), ctx.roots("mgB")
    for r in (ra, rb):
        shutil.rmtree(r.root)
        shutil.copytree(d["root0"], r.root)
    ea, eb = ra.env(AVOS_LABEL_DATASET=d["ds"]["dir"]), rb.env(AVOS_LABEL_DATASET=d["ds"]["dir"])
    for run in gold[:5]:
        ctx.label("human", "--run", run, "--verdict", "pass", "--labeller", "alice", env=ea, expect_rc=0)
    for run in gold[5:10]:
        ctx.label("human", "--run", run, "--verdict", "fail", "--labeller", "bob", env=eb, expect_rc=0)
    time.sleep(1.1)
    ctx.label("human", "--run", gold[2], "--verdict", "fail", "--labeller", "bob", env=eb, expect_rc=0)  # bob relabels alice's run later
    ab, ba = ctx.roots("mgAB"), ctx.roots("mgBA")
    for r in (ab, ba):
        shutil.rmtree(r.root)
    shutil.copytree(ra.root, ab.root)
    shutil.copytree(rb.root, ba.root)
    probs = []
    m1_ = ctx.label("merge", "--from", rb.root, env=ab.env(), expect_rc=0)
    m2_ = ctx.label("merge", "--from", ra.root, env=ba.env(), expect_rc=0)
    try:
        j1_, j2_ = json.loads(m1_["out"]), json.loads(m2_["out"])
    except ValueError:
        return False, f"merge did not print JSON: {lib.short(m1_['out'], 80)!r}"
    if j1_.get("added") != 6 or j2_.get("added") != 5:
        probs.append(f"merge added {j1_.get('added')} / {j2_.get('added')} (want 6 / 5: bob's 6 rows into A, alice's 5 into B)")
    again = ctx.label("merge", "--from", rb.root, env=ab.env(), expect_rc=0)
    if json.loads(again["out"]).get("added") != 0:
        probs.append("merging the same root twice added rows again")
    d_ab = ctx.run(ab, d["ds"], "--gold-n", "60", "--seed", "3", expect_rc=0)
    d_ba = ctx.run(ba, d["ds"], "--gold-n", "60", "--seed", "3", expect_rc=0)
    if lib.out_bytes(d_ab["dir"]) != lib.out_bytes(d_ba["dir"]):
        probs.append("A←B and B←A outputs differ")
    l2 = row_by_run(d_ab)[gold[2]].get("level2") or {}
    if l2.get("labeller") != "human:bob" or l2.get("history") != 2 or l2.get("verdict") != "fail":
        probs.append(f"relabel across roots: {l2} want bob's later fail with history 2")
    n_h = sum(1 for x in d_ab["rows"] if x.get("level2"))
    if n_h != 10:
        probs.append(f"{n_h} human-labelled runs after merge (want 10)")
    return not probs, "; ".join(probs) or "6/5 rows merged each way, idempotent, byte-identical outputs, later relabel wins with history 2"


@check("T1_no_transcript_text_anywhere", "statement (labels must not become a copy of the transcripts): no 60-char transcript run in root, caches or outputs")
def t1(ctx):
    d = ctx.judged()
    s = ctx.synth()
    probs = []
    for name, dd in (("judged", d), ("synth", s)):
        items = list(d["items"].values()) if name == "judged" else s["ds"]["items"]
        paths = [p for p in lib.files_under(dd["roots"].root, *dd["roots"].cache_dirs(), dd["dir"]) if not p.endswith("judge.env")]
        text = lib.text_of_files(paths)
        leaks = lib.verbatim_leaks(text, items)
        if leaks:
            probs.append(f"{name}: transcript text of {leaks[:3]} found under root/caches/out ({len(paths)} files)")
    size = sum(os.path.getsize(p) for p in lib.files_under(d["roots"].root))
    return not probs, "; ".join(probs) or f"no ≥ {TH['verbatim']}-char transcript run anywhere; judged root {size / 1024:.0f} KiB"


# =============================================================================== SC: scale

@check("SC1_scale_10k", "CONTRACT.md §12: 10,000 items: level 0 ≤ 60 s, level 0,1 ≤ 900 s, re-run ≤ 120 s, RSS ≤ 512 MiB, root ≤ 50 MiB", scale=True)
def sc1(ctx):
    items = lib.synthetic_items(ctx.base["items"], TH["n_scale"], seed=11)
    ds_dir = lib.write_dataset(os.path.join(ctx.tmp, "ds_scale"), items, ctx.base["manifest"])
    ds = {"dir": ds_dir}
    j = ctx.stub()
    j.reset(verdict={}, inconsistent=[], always_invalid=[], delay={}, faults=False, drift=False, quote_reasons=False,
            keep_bodies=False, must_contain=[], status=None, default="hash")
    r = ctx.roots("scale")
    probs = []
    d0 = ctx.run(r, ds, level="0", expect_rc=0, timeout=1800, rss=True)
    t0, m0 = d0["res"]["secs"], d0["res"]["maxrss_mib"]
    if len(d0["rows"]) != TH["n_scale"]:
        probs.append(f"level 0: {len(d0['rows'])} rows")
    if t0 > TH["level0_s"] or (m0 or 0) > TH["rss_mib"]:
        probs.append(f"level 0: {t0:.0f}s / {m0:.0f} MiB")
    d1 = ctx.run(r, ds, level="0,1", expect_rc=0, timeout=3600, rss=True)
    t1_, m1_ = d1["res"]["secs"], d1["res"]["maxrss_mib"]
    if (d1["summary"] or {}).get("counts", {}).get("judged") != TH["n_scale"] or j.n_calls < 2 * TH["n_scale"]:
        probs.append(f"level 1: judged {(d1['summary'] or {}).get('counts', {}).get('judged')} with {j.n_calls} requests")
    if t1_ > TH["level01_s"] or (m1_ or 0) > TH["rss_mib"]:
        probs.append(f"level 0,1: {t1_:.0f}s / {m1_:.0f} MiB")
    n1 = j.n_calls
    d2 = ctx.run(r, ds, level="0,1", expect_rc=0, timeout=1800, rss=True)
    t2, m2_ = d2["res"]["secs"], d2["res"]["maxrss_mib"]
    if j.n_calls != n1 or lib.out_bytes(d2["dir"]) != lib.out_bytes(d1["dir"]):
        probs.append(f"re-run made {j.n_calls - n1} requests or differs")
    if t2 > TH["rerun_s"] or (m2_ or 0) > TH["rss_mib"]:
        probs.append(f"re-run: {t2:.0f}s / {m2_:.0f} MiB")
    size = sum(os.path.getsize(p) for p in lib.files_under(r.root)) / 2**20
    if size > TH["root_mib"]:
        probs.append(f"label root {size:.0f} MiB")
    return not probs, "; ".join(probs) or (f"level 0 {t0:.1f}s/{m0:.0f} MiB; level 0,1 {t1_:.0f}s/{m1_:.0f} MiB ({j.n_calls} requests); "
                                         f"re-run {t2:.1f}s/{m2_:.0f} MiB, 0 requests; root {size:.1f} MiB")


# =============================================================================== Z: roots

@check("Z_roots_untouched", "collect §2 / retrain §2: after every check the store, results, ledger and dataset are byte-identical to the start")
def z(ctx):
    e = ctx.env
    probs = []
    if e.roots_snapshot() != ctx.snap0:
        probs.append("store/results/ledger changed")
    if lib.tree_snapshot(ctx.base["dir"]) != ctx.base["snap"]:
        probs.append("the base dataset changed")
    return not probs, "; ".join(probs) or "store, results, ledger and dataset byte-identical"


# =============================================================================== HOST

def _host_label_present(ctx):
    p = ctx.ssh(f"test -f {HOST_REPO}/label/bin/label && echo yes")
    if "yes" not in p.stdout:
        raise lib.NotImplementedCLI(f"label/bin/label on {HOST}:{HOST_REPO} (push the repo)")


def _host_wait_done(ctx):
    t0 = time.time()
    while time.time() - t0 < TH["host_wait_s"]:
        p = ctx.ssh(f"tail -1 {DRYRUN_LOG} 2>/dev/null")
        if p.stdout.strip() == "DONE":
            return True
        time.sleep(30)
    return False


def _host_dryrun_runs(ctx):
    """Run ids of the dry-run set: results created after the log's first line, on the 20 tasks; cached on the host."""
    cmd = f"""sudo python3 - <<'PY'
import json, os, datetime
log = open("{DRYRUN_LOG}").read().splitlines()
t0 = datetime.datetime.strptime(log[0].split()[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
tasks = set("{' '.join(DRYRUN_TASKS)}".split())
runs = []
for r in sorted(os.listdir("/var/lib/avos/results")):
    d = os.path.join("/var/lib/avos/results", r)
    if not r.startswith("run_retail-") or not os.path.exists(os.path.join(d, "result.json")):
        continue
    task = r[len("run_retail-"):].split("_")[0]
    if task in tasks and os.path.getmtime(os.path.join(d, "result.json")) >= t0 - 60:
        runs.append(r)
print(json.dumps(runs))
PY"""
    p = ctx.ssh(cmd)
    return json.loads(p.stdout.strip() or "[]")


def _host_setup(ctx):
    if "host" in ctx.cache:
        return ctx.cache["host"]
    _host_label_present(ctx)
    if not _host_wait_done(ctx):
        raise AssertionError(f"{DRYRUN_LOG} did not end with DONE within {TH['host_wait_s']} s")
    runs = _host_dryrun_runs(ctx)
    if not 60 <= len(runs) <= 80:
        raise AssertionError(f"{len(runs)} dry-run results found (want 80, accept ≥ 60): {runs[:3]}…")
    ctx.ssh(f"sudo rm -rf {HOST_TMP}; mkdir -p {HOST_TMP}; chmod 777 {HOST_TMP}")
    ctx.ssh(f"cat > {HOST_TMP}/runs.txt", stdin="\n".join(runs) + "\n")
    p = ctx.ssh(f"cd {HOST_REPO} && sudo python3 retrain/bin/retrain export --out {HOST_TMP}/ds --runs-file {HOST_TMP}/runs.txt > {HOST_TMP}/export.json; echo rc=$?; "
                f"sudo cat {HOST_TMP}/ds/manifest.json | python3 -c 'import json,sys; m=json.load(sys.stdin); print(json.dumps(m[\"counts\"]))'")
    h = {"runs": runs, "export": p.stdout}
    # the truth per run from result.json (reward, termination) — summary only, no transcript leaves the host
    cmd = f"""sudo python3 - <<'PY'
import json, os
out = {{}}
for r in open("{HOST_TMP}/runs.txt").read().split():
    res = json.load(open(os.path.join("/var/lib/avos/results", r, "result.json")))
    s = res["simulations"][0]
    ri = s.get("reward_info") or {{}}
    out[r] = {{"reward": ri.get("reward"), "termination_reason": s.get("termination_reason"),
              "nl_empty": ri.get("nl_assertions") == [] and "NL_ASSERTION" in (ri.get("reward_basis") or [])}}
print(json.dumps(out))
PY"""
    h["truth"] = json.loads(ctx.ssh(cmd).stdout.strip() or "{}")
    ctx.cache["host"] = h
    return h


def _host_env(extra=""):
    return (f"sudo env AVOS_LABEL_ROOT={HOST_TMP}/labels AVOS_LABEL_CACHE={HOST_TMP}/cache TMPDIR={HOST_TMP}/tmp HOME={HOST_TMP}/home "
            f"AVOS_LABEL_DATASET={HOST_TMP}/ds {extra} python3 label/bin/label")


@check("HOST0_no_stray_loops", "class B: before the live checks, no leftover wait loop or earlier suite process is running on the host", host=True)
def host0(ctx):
    p = ctx.ssh("pgrep -af 'while pgrep|tests/run.py|label/bin/label' | grep -v 'pgrep -af' || true")
    if p.returncode not in (0, 1):
        raise AssertionError(f"ssh failed: {lib.short(p.stderr, 200)}")
    stray = [l for l in p.stdout.splitlines() if l.strip()]
    return not stray, "nothing running" if not stray else f"{len(stray)} stray process(es) on {HOST}, e.g. {lib.short(stray[0], 140)!r}"


@check("HOST1_dryrun_level0", "done test 1 (real data): level 0 over the 80 dry-run runs: one row each, reward/termination = result.json, flagged never clean", host=True)
def host1(ctx):
    h = _host_setup(ctx)
    ctx.ssh(f"mkdir -p {HOST_TMP}/tmp {HOST_TMP}/home")
    p = ctx.ssh(f"cd {HOST_REPO} && {_host_env()} run --dataset {HOST_TMP}/ds --out {HOST_TMP}/l0 --level 0 > {HOST_TMP}/l0.out 2> {HOST_TMP}/l0.err; echo rc=$?; "
                f"sudo cat {HOST_TMP}/l0/labels.jsonl; echo ===; sudo cat {HOST_TMP}/l0/summary.json")
    if "Traceback" in p.stdout + p.stderr:
        return False, "traceback on the host"
    body, _, summ = p.stdout.partition("===")
    rows = [json.loads(l) for l in body.splitlines() if l.startswith("{")]
    probs = []
    by = {r["run"]: r for r in rows}
    if sorted(by) != sorted(h["runs"]):
        probs.append(f"{len(rows)} rows for {len(h['runs'])} runs")
    for run, t in h["truth"].items():
        row = by.get(run)
        if not row:
            continue
        if row["level0"]["reward"] != t["reward"] or row["level0"]["termination_reason"] != t["termination_reason"]:
            probs.append(f"{run}: reward/termination {row['level0']['reward']}/{row['level0']['termination_reason']} vs result.json {t}")
            break
        if row["level0"]["status"] == "clean" and (row["level0"]["witness_agrees"] is not True or row["level0"]["trace_complete"] != "complete"):
            probs.append(f"{run}: clean without an agreeing witness and a complete trace")
        if t["nl_empty"] and row["level0"]["structural_ok"] and t["reward"] is not None and row["level0"]["status"] != "clean":
            probs.append(f"{run}: empty NL assertions treated as a problem: {row['level0']['reasons']}")
    try:
        s = json.loads(summ)
    except ValueError:
        s = {}
    kept = sum(1 for r in rows if r["final"]["kept"])
    if s.get("tiles") != lib.tiles(len(rows), len(h["runs"]), kept):
        probs.append(f"tiles {s.get('tiles')}")
    clean = sum(1 for r in rows if r["level0"]["status"] == "clean")
    passed = sum(1 for r in rows if r["level0"]["passed"] is True)
    return not probs, "; ".join(probs[:3]) or f"{len(rows)} rows: {passed} passed by reward, {clean} clean, kept {kept}; {s.get('tiles', {}).get('kept')}"


@check("HOST2_real_judge_budgeted", f"done test 1/3 (real judge, ≤ $1): {TH['judge_runs_host']} runs through the real model, valid rows, then a re-run with 0 requests", host=True)
def host2(ctx):
    h = _host_setup(ctx)
    rows_l0 = None
    p = ctx.ssh(f"sudo cat {HOST_TMP}/l0/labels.jsonl 2>/dev/null")
    rows_l0 = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
    cand = [r["run"] for r in rows_l0 if r["level0"]["status"] == "clean"] or h["runs"]
    pick = sorted(cand)[:TH["judge_runs_host"] // 2] + sorted(cand)[-(TH["judge_runs_host"] - TH["judge_runs_host"] // 2):]
    pick = list(dict.fromkeys(pick))
    args = " ".join(f"--run {r}" for r in pick)
    t0 = time.perf_counter()
    p = ctx.ssh(f"cd {HOST_REPO} && {_host_env()} run --dataset {HOST_TMP}/ds --out {HOST_TMP}/j1 --level 0,1 {args} > {HOST_TMP}/j1.out 2> {HOST_TMP}/j1.err; echo rc=$?; "
                f"sudo cat {HOST_TMP}/j1/summary.json; echo ===; sudo cat {HOST_TMP}/j1/labels.jsonl; echo ===; sudo cat {HOST_TMP}/labels/*.jsonl", timeout=1800)
    secs = time.perf_counter() - t0
    if "Traceback" in p.stdout:
        return False, "traceback on the host"
    first, _, rest = p.stdout.partition("===")
    body, _, stored = rest.partition("===")
    rc = first.splitlines()[0] if first else ""
    try:
        s = json.loads(first.split("\n", 1)[1])
    except (ValueError, IndexError):
        return False, f"no summary: {lib.short(first, 200)}"
    rows = [json.loads(l) for l in body.splitlines() if l.startswith("{")]
    srows = [json.loads(l) for l in stored.splitlines() if l.startswith("{")]
    probs = []
    if len(rows) != len(pick):
        probs.append(f"{len(rows)} rows for --run ×{len(pick)}")
    judged = [r for r in rows if r.get("level1")]
    known = [r for r in judged if r["level1"]["verdict"] in ("pass", "veto")]
    if len(known) < max(1, len(judged) - 1):
        probs.append(f"only {len(known)}/{len(judged)} runs got a verdict from the real judge (rc {rc})")
    for r in known:
        if not any("STUB" not in x and len(x) > 10 for x in r["level1"]["reasons"]):
            probs.append(f"{r['run']}: no reasons from the judge")
            break
    toks = sum((x.get("usage") or {}).get("prompt_tokens", 0) + (x.get("usage") or {}).get("completion_tokens", 0) for x in srows if x.get("level") == 1)
    calls = (s.get("judge") or {}).get("calls_made")
    if calls is None or calls > TH["attempts"] * 2 * len(pick):
        probs.append(f"calls_made {calls}")
    bad = [lib.row_problems(x) for x in srows if lib.row_problems(x)]
    if bad:
        probs.append(f"invalid stored rows: {bad[0]}")
    if lib.dummy_key_in(p.stdout) or re.search(r"OPENAI_API_KEY=\S{8,}", p.stdout):
        probs.append("a key-like string on stdout")
    # re-run: 0 requests, identical
    p2 = ctx.ssh(f"cd {HOST_REPO} && {_host_env()} run --dataset {HOST_TMP}/ds --out {HOST_TMP}/j2 --level 0,1 {args} > /dev/null 2>&1; echo rc=$?; "
                 f"for f in labels.jsonl manifest.json gold.json CARD.md; do sudo cmp -s {HOST_TMP}/j1/$f {HOST_TMP}/j2/$f && echo same $f || echo DIFF $f; done; "
                 f"sudo python3 -c 'import json; a,b=[json.load(open(f\"{HOST_TMP}/\"+d+\"/summary.json\")) for d in (\"j1\",\"j2\")]; [x[\"judge\"].pop(k) for x in (a,b) for k in (\"calls_made\",\"calls_reused\")]; print(\"same summary.json\" if a==b else \"DIFF summary.json\")'; "
                 f"sudo python3 -c 'import json; print(json.load(open(\"{HOST_TMP}/j2/summary.json\"))[\"judge\"])'", timeout=600)
    if "DIFF" in p2.stdout or "'calls_made': 0" not in p2.stdout.replace('"', "'"):
        probs.append(f"re-run differs or made requests: {lib.short(p2.stdout, 160)}")
    cons = (s.get("judge") or {}).get("consistency")
    return not probs, "; ".join(probs[:3]) or (f"{len(known)}/{len(judged)} runs judged by the real model in {calls} requests, ≈{toks / 1000:.0f} K tokens, {secs:.0f}s; "
                                             f"consistency {cons}; re-run identical with 0 requests")


@check("HOST3_blind_view_all", "done test 2: the blind view of every dry-run run carries the transcript and no outcome word", host=True)
def host3(ctx):
    h = _host_setup(ctx)
    # the view and the item's own strings are compared on the host (no transcript leaves it); only verdicts come back
    leak_re = lib.LEAK.pattern
    cmd = f"""cd {HOST_REPO} && sudo python3 - <<'PY'
import json, re, subprocess, os
LEAK = re.compile({leak_re!r}, re.I)
items = {{}}
for l in open("{HOST_TMP}/ds/trajectories.jsonl"):
    it = json.loads(l)
    if it["principal"].count("/agent/") == 1 and "/sub/" not in it["principal"]:
        items[it["run"]] = it
env = dict(os.environ, AVOS_LABEL_ROOT="{HOST_TMP}/labels", AVOS_LABEL_CACHE="{HOST_TMP}/cache", TMPDIR="{HOST_TMP}/tmp", HOME="{HOST_TMP}/home")
res = {{"n": 0, "problems": []}}
for run, it in sorted(items.items()):
    p = subprocess.run(["python3", "label/bin/label", "view", "--dataset", "{HOST_TMP}/ds", "--run", run], capture_output=True, text=True, env=env)
    if p.returncode != 0 or "Traceback" in p.stderr:
        res["problems"].append(f"{{run}}: rc {{p.returncode}}"); continue
    text = p.stdout
    strings = []
    for m in it["messages"]:
        if isinstance(m.get("content"), str) and m["content"]: strings.append(m["content"])
        for t in m.get("tool_calls") or []:
            a = t.get("function", {{}}).get("arguments")
            if isinstance(a, str) and a: strings.append(a)
    miss = [s for s in strings if s not in text]
    if miss: res["problems"].append(f"{{run}}: {{len(miss)}} message strings missing")
    rest = text
    for s in sorted(strings, key=len, reverse=True): rest = rest.replace(s, " ")
    for t in it.get("tools") or []: rest = rest.replace(t["function"]["name"], " ")
    words = sorted({{m.group(0).lower() for m in LEAK.finditer(rest)}})
    if words: res["problems"].append(f"{{run}}: {{words[:4]}}")
    res["n"] += 1
print(json.dumps(res))
PY"""
    p = ctx.ssh(cmd, timeout=1800)
    try:
        res = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, f"host script failed: {lib.short(p.stderr or p.stdout, 200)}"
    return not res["problems"], "; ".join(res["problems"][:4]) or f"{res['n']} blind views: full transcript, no outcome word"


@check("HOST4_gold_and_human_loop", "done test 2/4 (the user's path): gold set over the dry-run rows, label a few blind, next/status, kappa reads unverified honestly", host=True)
def host4(ctx):
    h = _host_setup(ctx)
    # level 0 only over all 80 runs: a real judge pass over 80 runs is the user's spend, not the suite's (HOST2 is
    # the budgeted judge check). The gold algorithm and the human loop are the same path either way.
    p = ctx.ssh(f"cd {HOST_REPO} && {_host_env()} run --dataset {HOST_TMP}/ds --out {HOST_TMP}/g --level 0 > /dev/null 2>&1; echo rc=$?; "
                f"sudo cat {HOST_TMP}/g/gold.json", timeout=1800)
    try:
        g = json.loads(p.stdout.split("\n", 1)[1])
    except (ValueError, IndexError):
        return False, f"no gold.json: {lib.short(p.stdout, 200)}"
    probs = []
    p2 = ctx.ssh(f"sudo python3 -c 'import json; s=json.load(open(\"{HOST_TMP}/g/summary.json\")); print(json.dumps({{\"judge\": s[\"judge\"], \"m10\": s[\"m10\"], \"counts\": s[\"counts\"]}}))'")
    try:
        s = json.loads(p2.stdout)
    except ValueError:
        return False, f"no summary: {lib.short(p2.stdout, 200)}"
    if s["judge"]["calls_made"] != 0:
        probs.append(f"a level-0 run made {s['judge']['calls_made']} judge requests")
    strata = g.get("strata") or {}
    rows_p = ctx.ssh(f"sudo cat {HOST_TMP}/g/labels.jsonl")
    rows = [json.loads(l) for l in rows_p.stdout.splitlines() if l.startswith("{")]
    want_strata = {"disagree": [], "passed": [], "failed": []}
    for r in rows:
        if r.get("stratum"):
            want_strata[r["stratum"]].append(r["run"])
    if {k: sorted(v) for k, v in strata.items()} != want_strata:
        probs.append("gold.strata differ from the rows")
    want = lib.gold_oracle(want_strata, 60, 0)
    if g.get("runs") != want["runs"] or g.get("short") != want["short"]:
        probs.append(f"gold set differs from the algorithm ({len(g.get('runs') or [])} vs {len(want['runs'])})")
    # label two blind, then next/status
    env = _host_env()
    for run, v in zip(want["runs"][:2], ("pass", "fail")):
        q = ctx.ssh(f"cd {HOST_REPO} && {env} human --run {run} --verdict {v} --labeller tester > /dev/null; echo rc=$?")
        if "rc=0" not in q.stdout:
            probs.append(f"human label failed: {lib.short(q.stdout + q.stderr, 100)}")
    q = ctx.ssh(f"cd {HOST_REPO} && {env} run --dataset {HOST_TMP}/ds --out {HOST_TMP}/g2 --level 0 > /dev/null 2>&1; {env} gold --out {HOST_TMP}/g2 --next; {env} gold --out {HOST_TMP}/g2 --status; "
                f"sudo python3 -c 'import json; s=json.load(open(\"{HOST_TMP}/g2/summary.json\")); print(json.dumps(s[\"m10\"]))'", timeout=1800)
    lines = [l for l in q.stdout.splitlines() if l.strip()]
    if len(lines) < 3 or lines[0].strip() != want["runs"][2]:
        probs.append(f"gold --next printed {lines[:1]} want {want['runs'][2]}")
    try:
        st, m10 = json.loads(lines[1]), json.loads(lines[2])
    except (ValueError, IndexError):
        st, m10 = {}, {}
    if st.get("labelled") != 2 or st.get("left") != len(want["runs"]) - 2:
        probs.append(f"gold --status {st}")
    for k in ("kappa_judge_human", "kappa_reward_human"):
        if (m10.get(k) or {}).get("status") != "unverified" or (m10.get(k) or {}).get("value") is not None:
            probs.append(f"{k} with 2 labels is not unverified/null: {m10.get(k)}")
    if (m10.get("kappa_reward_human") or {}).get("n") != 2:
        probs.append(f"kappa_reward_human.n {(m10.get('kappa_reward_human') or {}).get('n')} want 2 (both labels count as parts)")
    return not probs, "; ".join(probs[:3]) or f"gold {len(g.get('runs') or [])} runs {({k: len(v) for k, v in strata.items()})}, 2 labelled, next/status right, M10 unverified (n=2), 0 judge requests"


@check("HOSTZ_no_leftover_processes", "class B: after cleanup the suite left no process or scratch dir on the host nor an ssh master on the laptop", host=True)
def hostz(ctx):
    ctl = ctx.ssh_ctl
    ctx.host_cleanup()
    probs = []
    if ctl and subprocess.run(["pgrep", "-f", f"ControlPath={ctl}"], capture_output=True).returncode == 0:
        probs.append("ssh control master still alive on the laptop")
    one = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=no", "-o", "ControlPath=none", HOST,
           "pgrep -af 'label/bin/label|while pgrep|p9-tests' | grep -v 'pgrep -af' || true; "
           f"ls -d {HOST_TMP} /tmp/poc9-tests-* 2>/dev/null | sed 's/^/scratch left: /'"]
    p = subprocess.run(one, capture_output=True, text=True, timeout=60)
    left = [l for l in p.stdout.splitlines() if l.strip()]
    if left:
        probs.append(f"{len(left)} process(es)/scratch dir(s) left on {HOST}: {lib.short(left[0], 120)!r}")
    return not probs, "; ".join(probs) or "no suite process on the host, no ssh master on the laptop"


# =============================================================================== runner

def run(args):
    ctx = Ctx()
    ctx.quick = args.quick
    n_fail = n_pass = 0
    print(f"# POC 9 acceptance suite  ({'quick' if args.quick else 'full'}{', host' if args.host else ''})  scratch={ctx.tmp}")
    drift = lib.fixture_drift()
    if drift:
        print(f"FAIL FIXTURES                             fixtures/ differ from MANIFEST.sha256: {drift[:5]} — the suite's inputs were changed; restore them")
        return 1
    try:
        for cid, target, scale, host, fn in CHECKS:
            if args.only and not cid.startswith(args.only):
                continue
            if scale and args.quick:
                print(f"SKIP {cid:<36} [{target}] --quick")
                continue
            if host and not args.host:
                print(f"SKIP {cid:<36} [{target}] --host not given")
                continue
            try:
                ok, note = fn(ctx)
            except lib.NotImplementedCLI as e:
                ok, note = False, f"not implemented: {e}"
            except AssertionError as e:
                ok, note = False, f"{lib.short(e, 400)}"
            except Exception as e:  # noqa: BLE001
                ok, note = False, f"suite error {type(e).__name__}: {lib.short(e, 200)}"
                if args.verbose:
                    traceback.print_exc()
            n_pass += ok
            n_fail += not ok
            print(f"{'PASS' if ok else 'FAIL'} {cid:<36} [{lib.short(target, 110)}] {note}")
            sys.stdout.flush()
    finally:
        if args.host:
            ctx.host_cleanup()
        ctx.ssh_close()
        ctx.close()
        if n_fail == 0:
            shutil.rmtree(ctx.tmp, ignore_errors=True)  # kept on failure for inspection (printed above)
    print(f"# {n_pass} passed, {n_fail} failed")
    return 0 if n_fail == 0 else 1


# =============================================================================== selftest

def selftest(args):
    import urllib.error
    import urllib.request
    results = []

    def ok(name, cond, note=""):
        results.append(bool(cond))
        print(f"{'PASS' if cond else 'FAIL'} selftest.{name:<40} {note}")

    tmp = tempfile.mkdtemp(prefix="poc9-selftest-")
    ok("fixtures_frozen", lib.fixture_drift() == [], str(lib.fixture_drift()[:3]))
    # the environment and POC 8's export over it
    e = lib.build_env(tmp)
    ds = lib.export_dataset(e, "base")
    ok("poc8_export_frozen", ds["manifest"]["dataset_sha256"] == EXPECTED["dataset_sha256"] and ds["manifest"]["counts"] == EXPECTED["counts"],
       f"{ds['manifest']['dataset_sha256'][:12]} counts {ds['manifest']['counts']}")
    runs = lib.runs_of(ds["items"])
    ok("one_item_per_run", len(ds["items"]) == len(runs) == 18 and all(lib.harness_item(ds["items"], r) for r in runs), f"{len(ds['items'])} items / {len(runs)} runs")
    # level 0 oracle on the planted cases
    l0 = {r: lib.level0_oracle(lib.harness_item(ds["items"], r)) for r in runs}
    clean = sorted(r for r in runs if l0[r]["status"] == "clean")
    ok("level0_clean_set", clean == ["run_retail-0_11", "run_retail-0_12", "run_retail-0_13", "run_retail-0_14", "run_retail-0_15", "run_retail-0_16",
                                    "run_retail-0_17", "run_retail-0_18", "run_retail-1_4"], str(clean))
    ok("level0_reasons", l0["run_retail-50_0"]["reasons"] == ["tau2_384"] and l0["run_retail-0_95"]["reasons"] == ["no_context_overflow"]
       and l0["run_retail-0_91"]["reasons"] == ["witness_agrees"] and l0["run_retail-0_92"]["reasons"] == ["trace_complete"]
       and l0["run_retail-0_90"]["reasons"] == ["reward_known"] and l0["run_retail-0_93"]["reasons"] == ["trace_complete"],
       str({r: l0[r]["reasons"] for r in ("run_retail-50_0", "run_retail-0_95", "run_retail-0_91", "run_retail-0_92", "run_retail-0_90", "run_retail-0_93")}))
    judged = sorted(r for r in runs if lib.judged_oracle(lib.harness_item(ds["items"], r)))
    keyed = {(r, lib.content_sha(lib.harness_item(ds["items"], r))) for r in runs}
    shared = len(runs) - len({lib.content_sha(lib.harness_item(ds["items"], r)) for r in runs})
    ok("fixture_keys_distinct", len(keyed) == len(runs) and shared == 5, f"{len(keyed)} (run, content) keys; {shared} runs share a transcript with another run")
    ok("judged_set", len(judged) == 16 and "run_retail-0_91" not in judged and "run_coding-slugify_2" not in judged, f"{len(judged)} judged")
    # final rule table
    f = lib.final_oracle
    c = l0["run_retail-0_13"]
    ok("final_rule", f(c, None, None) == {"label": "pass", "rule": "reward", "kept": True} and f(c, "veto", None)["rule"] == "veto"
       and f(c, "veto", "pass") == {"label": "pass", "rule": "human", "kept": True} and f(c, "pass", "fail")["rule"] == "human"
       and f(l0["run_retail-0_12"], "pass", "pass") == {"label": "fail", "rule": "reward", "kept": False}
       and f(l0["run_retail-50_0"], "pass", None)["rule"] == "tau2_384" and f(l0["run_retail-50_0"], "pass", "pass")["rule"] == "human"
       and f(l0["run_retail-0_92"], "pass", "pass")["rule"] == "level0")
    ok("stratum_rule", lib.stratum_oracle(c, "veto") == "disagree" and lib.stratum_oracle(c, "pass") == "passed" and lib.stratum_oracle(c, None) == "passed"
       and lib.stratum_oracle(l0["run_retail-0_12"], "pass") == "disagree" and lib.stratum_oracle(l0["run_retail-50_0"], "pass") == "disagree"
       and lib.stratum_oracle(l0["run_retail-0_91"], "pass") is None and lib.stratum_oracle(l0["run_retail-0_90"], None) is None)
    cs = lib.content_sha(it0 := lib.harness_item(ds["items"], "run_retail-0_13"))
    it_nc = json.loads(json.dumps(it0)); it_nc["host"] = "x"; it_nc["cost"]["usd"] = 1.0; it_nc["evidence"] = "ledger"; it_nc["lineage"]["events"].append("f" * 64)
    it_c = json.loads(json.dumps(it0)); it_c["messages"][3]["content"] = (it_c["messages"][3].get("content") or "") + "!"
    ok("content_sha_key", lib.content_sha(it_nc) == cs and lib.content_sha(it_c) != cs and lib.L.canon_item_id(it_nc) != it0["item_id"]
       and lib.content_sha(lib.harness_item(ds["items"], "run_retail-50_0")) != cs)  # same transcript, other task
    # kappa: hand-checked table, perfect agreement, degenerate, small n
    k = lib.kappa(20, 5, 10, 15)
    po, pe = 35 / 50, (25 * 30 + 25 * 20) / 2500
    ok("kappa_formula", abs(k["value"] - (po - pe) / (1 - pe)) < 1e-12 and k["status"] == "measured" and abs(k["se"] - (po * (1 - po) / (50 * (1 - pe) ** 2)) ** 0.5) < 1e-12
       and abs(k["ci95"][1] - k["ci95"][0] - 2 * 1.96 * k["se"]) < 1e-12, f"κ={k['value']:.4f} se={k['se']:.4f}")
    ok("kappa_edges", lib.kappa(30, 0, 0, 30)["value"] == 1.0 and lib.kappa(50, 0, 0, 0)["status"] == "unverified" and lib.kappa(50, 0, 0, 0)["reason"] == "pe=1"
       and lib.kappa(10, 2, 3, 5)["status"] == "unverified" and lib.kappa(10, 2, 3, 5)["value"] is None and lib.kappa(10, 2, 3, 5)["reason"] == "n=20 < 50")
    ok("kappa_diff_catches", lib.kappa_diff(k, {**k, "value": k["value"] + 1e-6}) and not lib.kappa_diff(k, json.loads(json.dumps(k))))
    # gold algorithm: deterministic, quotas, fill, clamp
    st = {"disagree": [f"d{i}" for i in range(10)], "passed": [f"p{i}" for i in range(100)], "failed": [f"f{i}" for i in range(30)]}
    g = lib.gold_oracle(st, 60, 0)
    ok("gold_quota_fill", len(g["runs"]) == 60 and {s: len(v) for s, v in g["picked"].items()} == {"disagree": 10, "passed": 30, "failed": 20} and not g["short"]
       and g == lib.gold_oracle(st, 60, 0) and g != lib.gold_oracle(st, 60, 1) and lib.gold_oracle(st, 7, 0)["n"] == 50 and lib.gold_oracle(st, 500, 0)["n"] == 100)
    gs = lib.gold_oracle({"disagree": ["a"], "passed": ["b", "c"], "failed": []}, 60, 0)
    ok("gold_short", gs["n"] == 60 and gs["runs"] == ["a", "b", "c"] and gs["short"] is True and sorted(gs["picked"]["passed"]) == ["b", "c"])
    # label row canon and problems
    row = {"level": 2, "content_sha256": "c" * 64, "item_id": "a" * 64, "run": "r", "labeller": "human:t", "ts": "2026-10-22T00:00:00Z", "verdict": "pass", "note": None}
    row["label_id"] = lib.canon_label_id(row)
    bad = dict(row, verdict="fail")
    ok("row_canon", lib.row_problems(row) == [] and lib.row_problems(bad) == ["label_id is not the canonical hash"], str(lib.row_problems(bad)))
    r1 = {"level": 1, "content_sha256": "c" * 64, "item_id": "a" * 64, "run": "r", "labeller": "model:m", "ts": "t", "verdict": "pass", "model": "m", "rubric_sha256": "b" * 64, "framing": "a",
          "answers": {"policy_followed": True, "right_tools": True, "hallucinated_facts": False, "unsafe_action": True}, "reasons": [], "error": None, "attempts": 1, "usage": None}
    r1["label_id"] = lib.canon_label_id(r1)
    ok("row_verdict_from_answers", "does not follow" in " ".join(lib.row_problems(r1)) and lib.verdict_of(r1["answers"]) == "veto")
    # verbatim leak oracle
    it = lib.harness_item(ds["items"], "run_retail-0_13")
    s = next(x for x in lib.content_strings(it) if len(x) > 100)
    ok("verbatim_leak", lib.verbatim_leaks("xx " + s[10:75] + " yy", [it]) == ["run_retail-0_13"] and lib.verbatim_leaks(s[10:60], [it]) == [] and lib.verbatim_leaks("nothing here", [it]) == [])
    # blind-view oracle on POC 8's own (non-blind) view: must leak; on a hand-built blind text: must not
    v = lib.retrain("view", "--run", "run_retail-0_13", env=e.env())
    leaks, _ = lib.blind_leaks(v["out"], it)
    ok("blind_catches_poc8_view", v["rc"] == 0 and {"reward", "passed"} <= set(leaks), str(leaks[:5]))
    blind = "\n".join([f"run=run_retail-0_13 principal={it['principal']} task={it['task']} item={it['item_id']}", "tools=" + ",".join(t["function"]["name"] for t in it["tools"])]
                      + [x for m in it["messages"] for x in ([f"#{m['role']} [{'policy' if m.get('train') else 'env'}]"] + ([m["content"]] if isinstance(m.get("content"), str) else [])
                                                               + [f"tool_call {t['id']} {t['function']['name']} {t['function']['arguments']}" for t in m.get("tool_calls") or []])])
    leaks2, rest = lib.blind_leaks(blind, it)
    ok("blind_accepts_clean_view", leaks2 == [] and lib.view_has_transcript(blind, it) == [], f"{leaks2[:4]} {lib.short(rest, 0)}")
    ok("transcript_check_catches", lib.view_has_transcript(blind.replace(s, ""), it) != [])
    # stub judge: deterministic, adversarial, counts
    j = lib.Judge().start()
    try:
        j.reset(faults=True, inconsistent=["item2"], always_invalid=["item4"], must_contain=["RUBRIC-TEXT"], quote_reasons=True)

        def call(item, framing, key=lib.DUMMY_KEY):
            long = "transcript sentence number %d about the order and the exchange of the delivered items; " * 3
            body = json.dumps({"model": "m", "temperature": 0, "seed": 300, "user": item,
                               "messages": [{"role": "system", "content": "RUBRIC-TEXT " + framing}, {"role": "user", "content": (long % (1, 2, 3)) + item}]}).encode()
            req = urllib.request.Request(j.base_url + "/chat/completions", data=body, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    raw = r.read()
                try:
                    d = json.loads(raw)
                    ch = d["choices"][0]
                    txt = ch["message"]["content"]
                    if txt.startswith("```"):
                        txt = txt.strip("`").split("\n", 1)[1].rsplit("```", 1)[0]
                    a = json.loads(txt)
                    valid = ch["finish_reason"] == "stop" and all(isinstance(a.get(k), bool) for k in lib.ANSWER_KEYS)
                    return 200, valid, a
                except (ValueError, KeyError, IndexError):
                    return 200, False, None
            except urllib.error.HTTPError as ex:
                return ex.code, False, None

        got = {}
        for item in ("item1", "item2", "item3", "item4"):
            for fr in "ab":
                for k in range(4):
                    code, valid, a = call(item, fr)
                    if valid:
                        got[(item, fr)] = a
                        break
        ok("stub_valid_within_3", all((i, fr) in got for i in ("item1", "item2", "item3") for fr in "ab") and ("item4", "a") not in got,
           f"{sorted(got)}")
        kinds = __import__("collections").Counter(c["kind"] for c in j.calls_for() if c["item"] != "item4")
        ok("stub_faults_seen", len(kinds) >= 2 and kinds["valid"] >= 6, str(dict(kinds)))
        v2, served = j.expected_item_verdict("item2")
        ok("stub_inconsistent", served == ["pass", "veto"] and v2 == "veto" and lib.verdict_of(got[("item2", "a")]) == "pass" and lib.verdict_of(got[("item2", "b")]) == "veto", str(served))
        ok("stub_deterministic", got[("item1", "a")] == call("item1", "a")[2] and all(c["contains"]["RUBRIC-TEXT"] for c in j.calls_for()))
        reasons = " ".join(got[("item1", "a")]["reasons"])
        ok("stub_quotes_transcript", "STUBREASON-item1" in reasons and "transcript sentence number 2" in reasons and len(reasons) > 120)
        j.set(drift=True)
        d1, d2 = call("item3", "a")[2], call("item3", "a")[2]
        ok("stub_drift", lib.verdict_of(d1) != lib.verdict_of(d2))
        j.set(drift=False, status=401)
        ok("stub_401", call("item1", "a")[0] == 401)
        j.set(status=None)
        # throughput for the scale bar
        j.reset(faults=False, quote_reasons=False, keep_bodies=False, must_contain=[])
        import concurrent.futures
        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda i: call(f"x{i}", "a"), range(1000)))
        rps = 1000 / (time.perf_counter() - t0)
        ok("stub_throughput", rps > 2 * TH["n_scale"] * 2 / TH["level01_s"], f"{rps:.0f} req/s (scale bar needs ≥ {2 * TH['n_scale'] * 2 / TH['level01_s']:.0f})")
    finally:
        j.stop()
    # synthetic items are canonical and distinct
    syn = lib.synthetic_items(ds["items"], 50, seed=7)
    ok("synthetic_items", len({x["item_id"] for x in syn}) == 50 and all(lib.L.canon_item_id(x) == x["item_id"] for x in syn)
       and len({x["run"] for x in syn}) == 50 and 10 < sum(1 for x in syn if x["outcome"]["passed"]) < 40)
    m = lib.manifest_for(syn, ds["manifest"])
    ok("synthetic_manifest", m["counts"]["items"] == 50 and m["counts"]["passed"] + m["counts"]["failed"] == 50 and len(m["dataset_sha256"]) == 64)
    ok("tiles", lib.tiles(80, 80, 31) == {"labelled": "80 / 80 runs labelled", "kept": "kept 31 passed runs → training set · dropped 49 failed"})
    ok("same_size_replace", (lambda p: (open(p, "w").write('{"verdict": "pass"}\n'), lib.same_size_replace(p, '"pass"', '"fail"'), open(p).read() == '{"verdict": "fail"}\n')[-1])(os.path.join(tmp, "f.jsonl")))
    n_fail = results.count(False)
    print(f"# selftest: {len(results)} items, {n_fail} failed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="skip the scale checks and most of POC 8's suite")
    ap.add_argument("--selftest", action="store_true", help="validate the suite's own oracles and stub judge")
    ap.add_argument("--host", action="store_true", help="also run the live checks on avos-dev over ssh")
    ap.add_argument("--only", help="run only checks whose id starts with this prefix")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    sys.exit(selftest(a) if a.selftest else run(a))
