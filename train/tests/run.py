#!/usr/bin/env python3
"""POC 12 acceptance suite (train/CONTRACT.md). Black box: only train/bin/train, fleet/bin/fleet compare, the raw
fixtures and (with --host) a GPU host over ssh are touched.

  python3 train/tests/run.py --quick       local checks (no GPU; ~3 min with warm uv caches)
  python3 train/tests/run.py --selftest    proves the suite's own oracles on the real fixtures
  python3 train/tests/run.py --host GPU    adds the live checks (fit / merge / identity serving) on host alias GPU
        [--model-path /models/Qwen3.5-9B] [--steps 2] [--port 8011] [--host-repo ~/poc] [--host-tmp /tmp/p12-tests]
  python3 train/tests/run.py --only P      only checks whose id starts with P

Exit 0 only when every executed check passes. One line per check:
  PASS|FAIL|SKIP  <check id>  [<target>]  <measured vs threshold>
"""
import argparse
import contextlib
import http.server
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hostproc
import lib

HOSTPROC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hostproc.py")

CHECKS = []
TH = {"select_s": 300, "tok_oracle_s": 900, "vllm_ready_s": 900, "fit_s": 1800, "merge_s": 1200, "replay_max_tokens": 64,
      "replay_prompts": 6, "price": 1.861, "tol": 1e-9}
K2_DELTA = {"10": +2, "12": +1, "14": +3, "20": +1, "21": +1, "25": +2, "40": +1, "54": +1, "11": -1, "13": -2, "22": -1}
K2_SEED = 11


def check(cid, target, host=False):
    def deco(fn):
        CHECKS.append((cid, target, host, fn))
        return fn
    return deco


class Ctx:
    def __init__(self, args):
        self.args = args
        self.tmp = tempfile.mkdtemp(prefix="poc12-tests-")
        self.items = lib.load_items()
        self.rows = lib.load_rows()
        self.row_by_run = {r["run"]: r for r in self.rows}
        self.cache = {}
        self.ssh_ctl = None
        self.ssh_dir = None

    def scratch(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    def tok_dir(self):
        if "tok" not in self.cache:
            self.cache["tok"] = lib.unpack_tokenizer(os.path.join(self.tmp, "tok"))
        return self.cache["tok"]

    def oracle(self):
        """Expected ids for every policy turn of the 12 fixture items, keyed by content key."""
        if "oracle" not in self.cache:
            self.cache["oracle"] = lib.tok_oracle(self.scratch("oracle"), self.tok_dir(), self.items, timeout=TH["tok_oracle_s"])
        return self.cache["oracle"]

    def item(self, run):
        return json.loads(json.dumps(next(it for it in self.items if it["run"] == run)))

    def kept_items(self):
        return [it for it in self.items if self.row_by_run[it["run"]]["final"]["kept"]]

    def real_pair(self):
        return [(self.items, self.rows)]

    def train(self, *args, env=None, expect_rc=None, timeout=None):
        res = lib.train(*args, env=env, timeout=timeout or TH["select_s"])
        if not lib.no_traceback(res):
            raise AssertionError(f"traceback (train {' '.join(map(str, args[:2]))}): {lib.short(res['err'], 300)}")
        if expect_rc is not None and res["rc"] not in (expect_rc if isinstance(expect_rc, tuple) else (expect_rc,)):
            raise AssertionError(f"train {' '.join(map(str, args[:3]))}: rc={res['rc']} (want {expect_rc}) stderr={lib.short(res['err'], 300)!r} stdout={lib.short(res['out'], 200)!r}")
        return res

    def fleet(self, *args, expect_rc=None):
        res = lib.fleet(*args)
        if not lib.no_traceback(res):
            raise AssertionError(f"traceback (fleet {' '.join(map(str, args[:2]))}): {lib.short(res['err'], 300)}")
        if expect_rc is not None and res["rc"] != expect_rc:
            raise AssertionError(f"fleet {' '.join(map(str, args[:3]))}: rc={res['rc']} (want {expect_rc}) stderr={lib.short(res['err'], 300)!r}")
        return res

    def select(self, tag, pairs, extra=(), expect_rc=0, env=None, split=None, fleets=None, policy=lib.SERVED):
        """pairs: [(export_dir, labels_dir)]; fleets default to the fleet.json beside each export (None = omit --fleet). Returns (res, out_dir)."""
        out = os.path.join(self.tmp, f"sel_{tag}", "data")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        args = ["select", "--out", out]
        for ex, lb in pairs:
            args += ["--pair", ex, lb]
        for fl in ([lib.fleet_path(ex) for ex, _ in pairs] if fleets is None else fleets):
            args += ["--fleet", fl]
        if policy is not None:
            args += ["--policy", policy]
        args += ["--tokenizer", self.tok_dir(), "--json"] + list(extra)
        if split:
            args += ["--split", split]
        res = self.train(*args, env=env or lib.train_env(self.tmp, tag), expect_rc=expect_rc)
        return res, out

    def pair_from(self, name, items, rows, **kw):
        return lib.make_pair(self.tmp, name, items, rows, **kw)

    # --- ssh (host checks)
    def ssh_base(self):
        if self.ssh_ctl is None:
            d = tempfile.mkdtemp(prefix="p12-", dir="/tmp")
            self.ssh_dir, self.ssh_ctl = d, os.path.join(d, "cm-%C")
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self.ssh_ctl}", "-o", "ControlPersist=600", self.args.host]

    def ssh(self, cmd, timeout=900, check=True):
        p = subprocess.run(self.ssh_base() + [cmd], capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise AssertionError(f"ssh `{lib.short(cmd, 120)}` rc={p.returncode}: {lib.short(p.stderr or p.stdout, 400)}")
        return p

    def ssh_json(self, cmd, timeout=900):
        p = self.ssh(cmd, timeout=timeout)
        try:
            return json.loads(p.stdout)  # a whole JSON document (pretty-printed files included)
        except ValueError:
            pass
        try:
            return json.loads(p.stdout.strip().splitlines()[-1])  # a command whose last line is the JSON
        except (ValueError, IndexError):
            raise AssertionError(f"ssh `{lib.short(cmd, 100)}`: stdout is not JSON (whole or last line): {lib.short(p.stdout, 200)!r}")

    def scp_to(self, local, remote):
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", f"ControlPath={self.ssh_ctl}", "-r", local, f"{self.args.host}:{remote}"], check=True, timeout=600)

    def scp_from(self, remote, local):
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", f"ControlPath={self.ssh_ctl}", f"{self.args.host}:{remote}", local], check=True, timeout=600)

    def host_cleanup(self):
        """Stop every session the suite started (one pid file each), remove the host scratch; returns the FAIL lines printed."""
        failed = 0
        if self.args.host and not self.cache.get("host_cleaned"):
            self.cache["host_cleaned"] = True  # pid files exist only where hostproc.py was shipped, so the loop is safe before any server
            t = self.args.host_tmp
            try:
                p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=no", "-o", "ControlPath=none", self.args.host,
                                    f"for p in {t}/*.pid; do [ -f \"$p\" ] && echo \"$p\" && python3 {t}/hostproc.py stop --pid-file \"$p\" --port {self.args.port}; done"],
                                   capture_output=True, text=True, timeout=300)
                lines = p.stdout.strip().splitlines()
                for pidf, rep in zip(lines[::2], lines[1::2]):
                    left = _leftover_text(rep, pidf)
                    if left:
                        failed += 1
                        print(f"FAIL host_cleanup  {left}")
                if failed == 0:
                    subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=no", "-o", "ControlPath=none", self.args.host, f"rm -rf {t}"],
                                   capture_output=True, timeout=120)
                else:
                    print(f"host scratch kept at {self.args.host}:{t}")
            except Exception as e:
                failed += 1
                print(f"FAIL host_cleanup  suite error {type(e).__name__}: {lib.short(e, 300)}")
        if self.ssh_ctl:
            subprocess.run(["ssh", "-o", f"ControlPath={self.ssh_ctl}", "-O", "exit", self.args.host], capture_output=True)
            shutil.rmtree(self.ssh_dir, ignore_errors=True)
            self.ssh_ctl = None
        return failed


# ====================================================================== local checks

@check("A0_cli_surface", "CONTRACT §1 exit codes")
def a0(ctx):
    ctx.train(expect_rc=2)
    ctx.train("bogus", expect_rc=2)
    ctx.train("select", expect_rc=2)
    ctx.train("select", "--out", os.path.join(ctx.tmp, "a0_out"), "--tokenizer", ctx.tok_dir(), expect_rc=2)
    common = ["--fleet", lib.FLEET, "--policy", lib.SERVED, "--tokenizer", ctx.tok_dir()]
    ctx.train("select", "--out", os.path.join(ctx.tmp, "a0_out"), "--pair", os.path.join(ctx.tmp, "nope"), lib.LABELS, *common, expect_rc=2)
    ctx.train("select", "--out", os.path.join(ctx.tmp, "a0_out"), "--pair", lib.EXPORT, lib.LABELS, "--fleet", lib.FLEET, "--policy", lib.SERVED, "--tokenizer", os.path.join(ctx.tmp, "nope"), expect_rc=2)
    # torn trajectories (half a line)
    ex, lb = ctx.pair_from("a0_torn", ctx.items, ctx.rows)
    p = os.path.join(ex, "trajectories.jsonl")
    b = open(p, "rb").read()
    open(p, "wb").write(b[: len(b) - 40])
    ctx.train("select", "--out", os.path.join(ctx.tmp, "a0_out2"), "--pair", ex, lb, *common, expect_rc=2)
    # a label row without `final`
    rows = json.loads(json.dumps(ctx.rows))
    del rows[0]["final"]
    ex, lb = ctx.pair_from("a0_nofinal", ctx.items, rows)
    ctx.train("select", "--out", os.path.join(ctx.tmp, "a0_out3"), "--pair", ex, lb, *common, expect_rc=2)
    ctx.train("card", expect_rc=2)
    ctx.train("card", "--fit", ctx.scratch("a0_emptyfit"), expect_rc=2)
    ctx.train("serve-cmd", expect_rc=2)
    ctx.train("replay", "--url", "http://127.0.0.1:1/v1", expect_rc=2)
    ctx.fleet("compare", expect_rc=2)
    ctx.fleet("compare", "--a", lib.MATRIX_HELDOUT, "--b", os.path.join(ctx.tmp, "nope.json"), "--json", expect_rc=2)
    bad = os.path.join(ctx.tmp, "a0_bad.json")
    open(bad, "w").write("{not json")
    ctx.fleet("compare", "--a", bad, "--b", lib.MATRIX_HELDOUT, "--json", expect_rc=2)
    for d in ("a0_out", "a0_out2", "a0_out3"):
        if os.path.exists(os.path.join(ctx.tmp, d)):
            raise AssertionError(f"a refused select created {d}")
    return "15 usage errors exit 2, nothing created, no traceback"


def _check_examples(ctx, out, expected_items, sources):
    """examples.jsonl against the tokenizer oracle: exact ids, exact mask, usage parity, order, hash."""
    orc = ctx.oracle()
    exs = lib.read_jsonl(os.path.join(out, "examples.jsonl"))
    man = lib.read_json(os.path.join(out, "manifest.json"))
    want = []
    for it in expected_items:
        key = lib.content_sha(it)
        for k in lib.policy_turns(it):
            want.append((it["item_id"], k, key, sources[it["item_id"]], it))
    want.sort(key=lambda w: (w[0], w[1]))
    if len(exs) != len(want):
        raise AssertionError(f"{len(exs)} examples, want {len(want)}")
    im_end = orc["_im_end_id"]
    for ex, (iid, k, key, src, it) in zip(exs, want):
        if (ex.get("item_id"), ex.get("turn_index")) != (iid, k):
            raise AssertionError(f"examples order/content: got ({ex.get('item_id', '')[:12]}, {ex.get('turn_index')}) want ({iid[:12]}, {k})")
        o = orc[key][str(k)]
        u = it["messages"][k]["usage"]
        P, C = len(o["prompt"]), len(o["completion"])
        if (P, C) != (u["prompt_tokens"], u["completion_tokens"]):
            raise AssertionError(f"oracle itself off for {it['run']}#{k}: {P},{C} vs usage {u}")
        if ex.get("prompt_tokens") != P or ex.get("completion_tokens") != C:
            raise AssertionError(f"{it['run']}#{k}: prompt_tokens/completion_tokens {ex.get('prompt_tokens')}/{ex.get('completion_tokens')} != gateway {P}/{C}")
        ids = ex.get("input_ids")
        if ids != o["prompt"] + o["completion"]:
            where = next((i for i, (a, b) in enumerate(zip(ids or [], o["prompt"] + o["completion"])) if a != b), min(len(ids or []), P + C))
            raise AssertionError(f"{it['run']}#{k}: input_ids differ from the served rendering at position {where} (len {len(ids or [])} vs {P + C})")
        if ex.get("loss_mask") != [0] * P + [1] * C:
            raise AssertionError(f"{it['run']}#{k}: loss_mask is not exactly [0]*{P} + [1]*{C}")
        if ids[-1] != im_end:
            raise AssertionError(f"{it['run']}#{k}: completion does not end with <|im_end|> ({ids[-1]})")
        if ex.get("source") != src or ex.get("run") != it["run"] or ex.get("task_id") != it["task_id"]:
            raise AssertionError(f"{it['run']}#{k}: source/run/task_id wrong: {ex.get('source')} {ex.get('run')} {ex.get('task_id')}")
    if man.get("examples_sha256") != lib.sha_file(os.path.join(out, "examples.jsonl")):
        raise AssertionError("manifest.examples_sha256 != sha256(examples.jsonl)")
    c = man.get("counts", {})
    if c.get("examples") != len(want) or c.get("runs") != len(expected_items) or c.get("tasks") != len({it["task_id"] for it in expected_items}):
        raise AssertionError(f"counts {c} vs {len(want)} examples / {len(expected_items)} runs")
    pt = sum(len(orc[w[2]][str(w[1])]["prompt"]) for w in want)
    ct = sum(len(orc[w[2]][str(w[1])]["completion"]) for w in want)
    if (c.get("prompt_tokens"), c.get("completion_tokens"), c.get("tokens")) != (pt, ct, pt + ct):
        raise AssertionError(f"counts tokens {c} vs {pt}/{ct}")
    sel = man.get("selected", [])
    if [s.get("item_id") for s in sel] != sorted({w[0] for w in want}):
        raise AssertionError("manifest.selected is not the sorted selected item_ids")
    return exs, man


@check("S1_select_real", "done tests 1–2 on real POC 11 items")
def s1(ctx):
    before = lib.tree_sha(lib.FIX)
    res, out = ctx.select("s1", [(lib.EXPORT, lib.LABELS)])
    if lib.tree_sha(lib.FIX) != before:
        raise AssertionError("select wrote under the fixtures (export/labels/tokenizer are read-only)")
    kept = ctx.kept_items()
    exs, man = _check_examples(ctx, out, kept, {it["item_id"]: 0 for it in kept})
    try:
        if json.loads(res["out"]) != man:
            raise AssertionError("--json stdout differs from manifest.json")
    except ValueError:
        raise AssertionError(f"--json stdout is not JSON: {lib.short(res['out'], 120)!r}")
    if man.get("format") != "avos-train-data/1":
        raise AssertionError(f"format {man.get('format')}")
    tk = man.get("tokenizer", {})
    if tk.get("tokenizer_sha256") != lib.sha_file(os.path.join(ctx.tok_dir(), "tokenizer.json")) or tk.get("chat_template_sha256") != lib.sha_file(os.path.join(ctx.tok_dir(), "chat_template.jinja")):
        raise AssertionError("manifest.tokenizer hashes are not the given tokenizer's")
    if tk.get("im_end_id") != ctx.oracle()["_im_end_id"]:
        raise AssertionError(f"im_end_id {tk.get('im_end_id')}")
    if man.get("split_file", {}).get("sha256") != lib.sha_file(lib.SPLIT):
        raise AssertionError("manifest.split_file.sha256 wrong")
    rej = {(r.get("run"), r.get("reason")) for r in man.get("rejected", [])}
    want_rej = {(it["run"], "not_kept") for it in ctx.items if not ctx.row_by_run[it["run"]]["final"]["kept"]}
    if rej != want_rej:
        raise AssertionError(f"rejected {sorted(rej)} want {sorted(want_rej)}")
    src = man.get("sources", [{}])[0]
    if (src.get("items"), src.get("kept"), src.get("selected"), src.get("rejected")) != (12, 8, 8, 4) or src.get("dataset_sha256") != lib.read_json(os.path.join(lib.EXPORT, "manifest.json"))["dataset_sha256"] or src.get("labels_sha256") != lib.sha_file(os.path.join(lib.LABELS, "labels.jsonl")):
        raise AssertionError(f"sources[0] {src}")
    ctx.cache["s1_examples_sha"] = man["examples_sha256"]
    human = [it for it in kept if ctx.row_by_run[it["run"]]["final"]["rule"] == "human"]
    return f"8/12 runs selected ({len(human)} human-kept incl. a reward-0 one), {len(exs)} policy turns token-identical to the served rendering, mask exact, 4 not_kept"


def _refusal(ctx, tag, pairs, reason, extra=(), split=None, policy=lib.SERVED):
    res, out = ctx.select(tag, pairs, extra=extra, expect_rc=1, split=split, policy=policy)
    text = res["err"] + res["out"]
    if reason not in text:
        raise AssertionError(f"{tag}: exit 1 but `{reason}` not named: {lib.short(text, 300)!r}")
    if os.path.exists(out):
        raise AssertionError(f"{tag}: refused select created {out}")


@check("S2_leak_refusals", "done test 1 (held-out / unsplit / mismatched / flagged never enter)")
def s2(ctx):
    base = ctx.item("run_retail-17_11")
    row = ctx.row_by_run["run_retail-17_11"]
    others = [it for it in ctx.items if it["run"] != "run_retail-17_11"]
    others_rows = [r for r in ctx.rows if r["run"] != "run_retail-17_11"]
    # (a) a held-out run labelled kept, row and item both say held_out
    ho = lib.set_task(lib.clone_item(base, 901), "10", "held_out")
    ex, lb = ctx.pair_from("s2_heldout", others + [ho], others_rows + [lib.row_for(ho, row, kept=True)])
    _refusal(ctx, "s2a", [(ex, lb)], "kept_not_train")
    # (b) the same run with item and row both claiming "train": the split file is the authority
    ho2 = lib.set_task(lib.clone_item(base, 902), "10", "train")
    ex, lb = ctx.pair_from("s2_heldout_lying", others + [ho2], others_rows + [lib.row_for(ho2, row, kept=True)])
    _refusal(ctx, "s2b", [(ex, lb)], "kept_not_train")
    # (c) an unsplit (excluded by the split file) run labelled kept
    un = lib.set_task(lib.clone_item(base, 903), "64", None)
    ex, lb = ctx.pair_from("s2_unsplit", others + [un], others_rows + [lib.row_for(un, row, kept=True)])
    _refusal(ctx, "s2c", [(ex, lb)], "kept_not_train")
    # (d) a label row whose content_sha256 does not match: one character of one assistant content changed
    ed = json.loads(json.dumps(base))
    k = lib.policy_turns(ed)[-1]
    m = ed["messages"][k]
    if m.get("content"):
        m["content"] = ("X" if m["content"][0] != "X" else "Y") + m["content"][1:]
    else:
        m["content"] = "X"
    lib.rehash(ed)
    r = lib.row_for(ed, row, kept=True)
    r["content_sha256"] = row["content_sha256"]  # the label was given for the original conversation
    ex, lb = ctx.pair_from("s2_content", others + [ed], others_rows + [r])
    _refusal(ctx, "s2d", [(ex, lb)], "content_sha256_mismatch")
    # (e) a witness-disagreeing item labelled kept
    wd = json.loads(json.dumps(base))
    wd["witness"]["agrees"] = False
    wd["witness"]["problems"] = ["message 3: output_sha256 differs"]
    lib.rehash(wd)
    ex, lb = ctx.pair_from("s2_witness", others + [wd], others_rows + [lib.row_for(wd, row, kept=True)])
    _refusal(ctx, "s2e", [(ex, lb)], "kept_witness_disagrees")
    # (f) an item without a label row, (g) a row for a run the export does not hold
    ex, lb = ctx.pair_from("s2_nolabel", ctx.items, others_rows)
    _refusal(ctx, "s2f", [(ex, lb)], "label_missing")
    ghost = lib.row_for(lib.clone_item(base, 904), row, kept=False)
    ex, lb = ctx.pair_from("s2_ghost", ctx.items, ctx.rows + [ghost])
    _refusal(ctx, "s2g", [(ex, lb)], "label_run_unknown")
    # (h) export manifest whose dataset hash is not the items'
    ex, lb = ctx.pair_from("s2_dsha", ctx.items, ctx.rows, manifest_patch={"dataset_sha256": "00" * 32})
    _refusal(ctx, "s2h", [(ex, lb)], "dataset_sha256_mismatch")
    # (i) a different split file than the export was built with
    sp = lib.read_json(lib.SPLIT)
    sp["train"] = sp["train"] + ["10"]
    other_split = os.path.join(ctx.tmp, "s2_split.json")
    lib.write_json(other_split, sp)
    _refusal(ctx, "s2i", [(lib.EXPORT, lib.LABELS)], "split_mismatch", split=other_split)
    # (j) held-out item not kept: a rejection, never in the data
    ho3 = lib.set_task(lib.clone_item(base, 905), "10", "held_out")
    ex, lb = ctx.pair_from("s2_heldout_dropped", others + [ho3], others_rows + [lib.row_for(ho3, row, kept=False)])
    res, out = ctx.select("s2j", [(ex, lb)], expect_rc=0)
    man = lib.read_json(os.path.join(out, "manifest.json"))
    if any(e["run"] == ho3["run"] for e in lib.read_jsonl(os.path.join(out, "examples.jsonl"))) or man["counts"]["runs"] != 7:
        raise AssertionError("a held-out run entered the data")
    if (ho3["run"], "not_kept") not in {(r["run"], r["reason"]) for r in man["rejected"]}:
        raise AssertionError("held-out run not listed as rejected")
    # (k) an excluded task
    res, out = ctx.select("s2k", [(lib.EXPORT, lib.LABELS)], extra=["--exclude-task", "17,57"], expect_rc=0)
    man = lib.read_json(os.path.join(out, "manifest.json"))
    exs = lib.read_jsonl(os.path.join(out, "examples.jsonl"))
    if any(e["task_id"] in ("17", "57") for e in exs) or man["excluded_tasks"] != ["17", "57"]:
        raise AssertionError("excluded task present")
    want = {(it["run"], "excluded_task") for it in ctx.kept_items() if it["task_id"] in ("17", "57")}
    if not want <= {(r["run"], r["reason"]) for r in man["rejected"]}:
        raise AssertionError(f"excluded runs not rejected as excluded_task: {man['rejected']}")
    keep = [it for it in ctx.kept_items() if it["task_id"] not in ("17", "57")]
    _check_examples(ctx, out, keep, {it["item_id"]: 0 for it in keep})
    return "9 refusals exit 1 naming the reason with no output dir; a dropped held-out run and 4 excluded runs are rejected, never present"


@check("S3_two_pairs", "statement (two train fleets: T 0 and T 0.7)")
def s3(ctx):
    kept = ctx.kept_items()
    second = [lib.clone_item(it, 200 + i) for i, it in enumerate(kept)]
    rows2 = [lib.row_for(c, ctx.row_by_run[o["run"]]) for c, o in zip(second, kept)]
    ex2, lb2 = ctx.pair_from("s3_second", second, rows2)
    res, out = ctx.select("s3", [(lib.EXPORT, lib.LABELS), (ex2, lb2)])
    sources = {it["item_id"]: 0 for it in kept}
    sources.update({it["item_id"]: 1 for it in second})
    exs, man = _check_examples(ctx, out, kept + second, sources)
    if len(man["sources"]) != 2 or man["sources"][1]["selected"] != 8 or man["counts"]["runs"] != 16:
        raise AssertionError(f"sources/counts {man['sources']} {man['counts']}")
    # the same item in both pairs is a duplicate, refused
    ex3, lb3 = ctx.pair_from("s3_dup", kept[:2], [ctx.row_by_run[it["run"]] for it in kept[:2]])
    _refusal(ctx, "s3dup", [(lib.EXPORT, lib.LABELS), (ex3, lb3)], "duplicate_item")
    return "16 runs from 2 pairs, sources 0/1 per example, 8+8; the same item twice → duplicate_item"


@check("S4_determinism_caches", "CONTRACT §2 determinism and content-bound caches")
def s4(ctx):
    env1, env2 = lib.train_env(ctx.tmp, "s4a"), lib.train_env(ctx.tmp, "s4b")
    _, out1 = ctx.select("s4a", [(lib.EXPORT, lib.LABELS)], env=env1)
    for p in env1.values():
        shutil.rmtree(p, ignore_errors=True)
        os.makedirs(p, exist_ok=True)
    _, out2 = ctx.select("s4b", [(lib.EXPORT, lib.LABELS)], env=env2)
    t1, t2 = lib.tree_sha(out1), lib.tree_sha(out2)
    if t1 != t2:
        raise AssertionError(f"data dirs differ across re-runs: {sorted(k for k in set(t1) | set(t2) if t1.get(k) != t2.get(k))}")
    if set(t1) != {"examples.jsonl", "manifest.json"}:
        raise AssertionError(f"unexpected files in the data dir: {sorted(t1)}")
    # a private copy of the sources, run once (caches warm), then tamper. The copy lives at other paths, which the
    # manifest records "as given": examples.jsonl byte-identical, manifest identical but for those path strings
    ex, lb = ctx.pair_from("s4_src", ctx.items, ctx.rows, fleet_patch={"fleet": "p11-train"})  # a true copy keeps the fleet's name
    env = lib.train_env(ctx.tmp, "s4c")
    _, out3 = ctx.select("s4c", [(ex, lb)], env=env)
    if lib.sha_file(os.path.join(out3, "examples.jsonl")) != t1["examples.jsonl"]:
        raise AssertionError("copied sources give a different examples.jsonl")
    m1, m3 = (lib.read_json(os.path.join(o, "manifest.json")) for o in (out1, out3))
    for m in (m1, m3):
        for s in m.get("sources", []):
            for k in ("export", "labels", "fleet"):
                s.pop(k, None)
    if m1 != m3:
        raise AssertionError(f"copied sources give a different manifest beyond sources[].export/labels/fleet: {lib.short(lib.diff(m3, m1), 300)}")
    # (1) every cache file damaged
    n = lib.tamper_tree(env["AVOS_TRAIN_CACHE"]) + lib.tamper_tree(env["TMPDIR"]) + lib.tamper_tree(env["HOME"])
    res, out4 = ctx.select("s4d", [(ex, lb)], env=env, expect_rc=(0, 1))
    if res["rc"] == 0 and lib.tree_sha(out4) != lib.tree_sha(out3):
        raise AssertionError("after cache damage: exit 0 with a different data dir")
    # (2) labels.jsonl replaced by a same-size, same-mtime variant: 17_11 kept→false, 73_74 false→true
    p = os.path.join(lb, "labels.jsonl")
    rows = lib.read_jsonl(p)
    for r in rows:
        if r["run"] == "run_retail-17_11":
            r["final"]["kept"], r["final"]["label"] = False, "fail"
        if r["run"] == "run_retail-73_74":
            r["final"]["kept"], r["final"]["label"] = True, "pass"
    new = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    old = open(p, "rb").read()
    if len(new) != len(old):
        # pad/trim whitespace inside the last line's JSON is not allowed; adjust by a trailing space before the newline of the last row
        delta = len(old) - len(new)
        if delta < 0:
            raise AssertionError("same-size swap impossible")
        new = new[:-1] + b" " * delta + b"\n"
    lib.same_size_replace(p, new)
    res, out5 = ctx.select("s4e", [(ex, lb)], env=env, expect_rc=0)
    man = lib.read_json(os.path.join(out5, "manifest.json"))
    runs = {s["run"] for s in man["selected"]}
    if "run_retail-17_11" in runs or "run_retail-73_74" not in runs:
        raise AssertionError(f"same-size/mtime label swap not noticed: selected {sorted(runs)}")
    want = [it for it in ctx.items if (it["run"] in runs)]
    _check_examples(ctx, out5, want, {it["item_id"]: 0 for it in want})
    # (3) trajectories edited in place (one usage count), caches warm → refused
    tp = os.path.join(ex, "trajectories.jsonl")
    items = lib.read_jsonl(tp)
    for it in items:
        if it["run"] == "run_retail-73_74":
            k = lib.policy_turns(it)[0]
            it["messages"][k]["usage"]["prompt_tokens"] += 1
            lib.rehash(it)
            rows_now = lib.read_jsonl(p)
            for r in rows_now:
                if r["run"] == it["run"]:
                    r["item_id"] = it["item_id"]
            lib.write_jsonl(p, rows_now)
    lib.write_jsonl(tp, items)
    man_p = os.path.join(ex, "manifest.json")
    man_ex = lib.read_json(man_p)
    man_ex["dataset_sha256"] = lib.dataset_sha(items)
    for mi, it in zip(man_ex["items"], items):
        mi["item_id"] = it["item_id"]
    lib.write_json(man_p, man_ex)
    _refusal(ctx, "s4f", [(ex, lb)], "token_parity")
    return f"byte-identical across 2 runs, copied sources identical but for the as-given paths; {n} cache files damaged → still correct or exit 1; same-size/mtime label swap changes the set; edited usage → token_parity"


@check("S5_parity_refusals", "done test 2 (gateway counts are the authority)")
def s5(ctx):
    base = ctx.item("run_retail-17_10")
    row = ctx.row_by_run["run_retail-17_10"]
    others = [it for it in ctx.items if it["run"] != base["run"]]
    others_rows = [r for r in ctx.rows if r["run"] != base["run"]]
    # completion count off by one
    it = json.loads(json.dumps(base))
    k = lib.policy_turns(it)[1]
    it["messages"][k]["usage"]["completion_tokens"] -= 1
    lib.rehash(it)
    ex, lb = ctx.pair_from("s5_comp", others + [it], others_rows + [lib.row_for(it, row)])
    _refusal(ctx, "s5a", [(ex, lb)], "token_parity")
    # usage removed from a policy turn
    it = json.loads(json.dumps(base))
    del it["messages"][k]["usage"]
    lib.rehash(it)
    ex, lb = ctx.pair_from("s5_nousage", others + [it], others_rows + [lib.row_for(it, row)])
    _refusal(ctx, "s5b", [(ex, lb)], "usage_missing")
    # (c) a tool argument string re-serialised (indent, key order) with the label re-keyed to it: arguments are parsed
    # to objects before the template, so the rendering, the counts and the ids are the original's → selected, not refused
    it = lib.reserialise_args(base)
    ex, lb = ctx.pair_from("s5_args", others + [it], others_rows + [lib.row_for(it, row)])
    res, out = ctx.select("s5c", [(ex, lb)], expect_rc=0)
    exs = [e for e in lib.read_jsonl(os.path.join(out, "examples.jsonl")) if e["run"] == it["run"]]
    orc = ctx.oracle()[lib.content_sha(base)]
    if [e["turn_index"] for e in exs] != lib.policy_turns(base) or any(e["input_ids"] != orc[str(e["turn_index"])]["prompt"] + orc[str(e["turn_index"])]["completion"] for e in exs):
        raise AssertionError("re-serialised arguments (same object) do not give the original's ids")
    # (d) an argument *value* changed (label re-keyed, counts untouched): the rendering no longer matches → token_parity
    it = lib.mutate_arg_value(base)
    ex, lb = ctx.pair_from("s5_value", others + [it], others_rows + [lib.row_for(it, row)])
    _refusal(ctx, "s5d", [(ex, lb)], "token_parity")
    return "completion −1 → token_parity; usage missing → usage_missing; re-serialised argument string → selected with the original ids; a changed argument value → token_parity"


@check("S6_policy_role", "the trained model is the one that produced the runs (--fleet / --policy)")
def s6(ctx):
    # (a) --fleet omitted, (b) fewer --fleet than --pair: usage, nothing created
    res, out = ctx.select("s6a", [(lib.EXPORT, lib.LABELS)], fleets=[], expect_rc=2)
    kept = ctx.kept_items()
    second = [lib.clone_item(it, 300 + i) for i, it in enumerate(kept[:2])]
    ex2, lb2 = ctx.pair_from("s6_second", second, [lib.row_for(c, ctx.row_by_run[o["run"]]) for c, o in zip(second, kept[:2])])
    res, out2 = ctx.select("s6b", [(lib.EXPORT, lib.LABELS), (ex2, lb2)], fleets=[lib.FLEET], expect_rc=2)
    for o in (out, out2):
        if os.path.exists(o):
            raise AssertionError("usage error created the output dir")
    # (c) a fleet whose policy role is another served model (the incident: the fleet's default role) → policy_mismatch
    ex3, lb3 = ctx.pair_from("s6_think", second, [lib.row_for(c, ctx.row_by_run[o["run"]]) for c, o in zip(second, kept[:2])], fleet_patch={"policy": "policy-qwen3.5-9b-think"})
    res, out3 = ctx.select("s6c", [(lib.EXPORT, lib.LABELS), (ex3, lb3)], expect_rc=1)
    text = res["err"] + res["out"]
    if "policy_mismatch" not in text or "s6_think" not in text or "policy-qwen3.5-9b-think" not in text or os.path.exists(out3):
        raise AssertionError(f"wrong-policy fleet in pair 1: want policy_mismatch naming fleet s6_think and its role, no dir: {lib.short(text, 300)!r}")
    # (d) the right fleets but another model to be trained → policy_mismatch on pair 0
    _refusal(ctx, "s6d", [(lib.EXPORT, lib.LABELS)], "policy_mismatch", policy="policy-qwen3.5-9b-think")
    # (e) a fleet built on another split file → split_mismatch
    ex5, lb5 = ctx.pair_from("s6_split", second, [lib.row_for(c, ctx.row_by_run[o["run"]]) for c, o in zip(second, kept[:2])], fleet_patch={"split": {"path": "x", "sha256": "00" * 32, "part": "train"}})
    _refusal(ctx, "s6e", [(lib.EXPORT, lib.LABELS), (ex5, lb5)], "split_mismatch")
    # (f) the good case records what was checked
    res, out6 = ctx.select("s6f", [(lib.EXPORT, lib.LABELS), (ex2, lb2)], expect_rc=0)
    man = lib.read_json(os.path.join(out6, "manifest.json"))
    fl = lib.read_json(lib.FLEET)
    if man.get("policy") != lib.SERVED or [s.get("fleet_name") for s in man["sources"]] != ["p11-train", "s6_second"] or any(s.get("fleet_policy") != lib.SERVED for s in man["sources"]) or man["sources"][0].get("fleet") != lib.FLEET:
        raise AssertionError(f"manifest policy/sources: {man.get('policy')} {[{k: s.get(k) for k in ('fleet', 'fleet_name', 'fleet_policy')} for s in man['sources']]}")
    if fl["roles"]["policy"] != lib.SERVED:
        raise AssertionError("fixture fleet.json is not the served policy")
    return "--fleet missing or short → exit 2; a fleet whose policy role is the -think model, or --policy naming another model → policy_mismatch naming the fleet; foreign split → split_mismatch; manifest records policy + fleet_name/fleet_policy per source"


@check("C1_card_arithmetic", "done test 3 (the record)")
def c1(ctx):
    d, fit, losses = lib.make_fitdir(ctx.tmp, "c1_fit", steps=7, wall_s=1234.5, gpu_count=2, price=1.861, runs=178)
    res = ctx.train("card", "--fit", d, "--json", expect_rc=0)
    card = json.loads(res["out"])
    gpu_h = 1234.5 * 2 / 3600
    want = {"format": "avos-train-card/1", "note": "LoRA · Qwen/Qwen3.5-9B · 178 passed runs", "base_model": "Qwen/Qwen3.5-9B",
            "data": fit["data"], "hyperparameters": fit["hyperparameters"], "steps": 7, "epochs": fit["epochs"],
            "loss": {"per_step": losses, "first": losses[0], "last": losses[-1], "min": min(losses)},
            "wall_s": 1234.5, "gpu_count": 2, "gpu_hours": gpu_h, "gpu_usd_per_h": 1.861, "cost_usd": gpu_h * 1.861}
    errs = lib.diff(card, want)
    if errs:
        raise AssertionError("card: " + "; ".join(errs[:5]))
    res2 = ctx.train("card", "--fit", d, "--json", expect_rc=0)
    if res2["out"] != res["out"]:
        raise AssertionError("card --json not byte-identical across runs")
    txt = ctx.train("card", "--fit", d, expect_rc=0)["out"]
    first = txt.strip().splitlines()[0] if txt.strip() else ""
    if first.strip() != want["note"]:
        raise AssertionError(f"CARD text first line {first!r} != note")
    for tokn in ("steps", "epochs", "lr", "rank", "alpha", "178", "ab" * 32, "1234.5", "gpu_hours", "cost_usd", f"{gpu_h * 1.861:.4f}"[:5]):
        if tokn not in txt:
            raise AssertionError(f"CARD text lacks {tokn!r}")
    if os.path.exists(os.path.join(d, "card.json")) or os.path.exists(os.path.join(d, "CARD.md")):
        raise AssertionError("card wrote into FITDIR (it is pure)")
    # steps 0: identity run
    d0, _, _ = lib.make_fitdir(ctx.tmp, "c1_fit0", steps=0, wall_s=61.0, gpu_count=1, price=1.861, runs=8)
    c0 = json.loads(ctx.train("card", "--fit", d0, "--json", expect_rc=0)["out"])
    if c0["steps"] != 0 or c0["loss"]["per_step"] != [] or c0["loss"]["first"] is not None or abs(c0["cost_usd"] - 61.0 / 3600 * 1.861) > TH["tol"] or c0["note"] != "LoRA · Qwen/Qwen3.5-9B · 8 passed runs":
        raise AssertionError(f"steps-0 card: {lib.short(c0, 200)}")
    # broken logs: short, out of sequence, non-finite
    d1, _, _ = lib.make_fitdir(ctx.tmp, "c1_short", steps=5, wall_s=10, gpu_count=1, price=1.0, runs=1, log_lines=[{"step": i + 1, "loss": 1.0, "lr": 1e-4, "tokens": 1, "elapsed_s": 1.0} for i in range(4)])
    ctx.train("card", "--fit", d1, "--json", expect_rc=1)
    d2, _, _ = lib.make_fitdir(ctx.tmp, "c1_seq", steps=2, wall_s=10, gpu_count=1, price=1.0, runs=1, log_lines=[{"step": 2, "loss": 1.0, "lr": 1e-4, "tokens": 1, "elapsed_s": 1.0}, {"step": 1, "loss": 1.0, "lr": 1e-4, "tokens": 1, "elapsed_s": 2.0}])
    ctx.train("card", "--fit", d2, "--json", expect_rc=1)
    d3, _, _ = lib.make_fitdir(ctx.tmp, "c1_nan", steps=1, wall_s=10, gpu_count=1, price=1.0, runs=1)
    with open(os.path.join(d3, "log.jsonl"), "w") as f:
        f.write('{"step": 1, "loss": NaN, "lr": 0.0001, "tokens": 1, "elapsed_s": 1.0}\n')
    ctx.train("card", "--fit", d3, "--json", expect_rc=1)
    return f"gpu_hours {gpu_h:.6f} × $1.861 = ${gpu_h * 1.861:.4f} within 1e-9, note verbatim, steps-0 nulls, 3 broken logs → exit 1, pure"


@check("V1_serve_cmd", "done test 4 (the frozen stack)")
def v1(ctx):
    a = json.loads(ctx.train("serve-cmd", "--model", "/m/base", "--port", 8011, "--json", expect_rc=0)["out"])["argv"]
    b = json.loads(ctx.train("serve-cmd", "--model", "/m/merged", "--port", 8011, "--json", expect_rc=0)["out"])["argv"]
    if len(a) != len(b) or [i for i in range(len(a)) if a[i] != b[i]] != [a.index("/m/base")]:
        raise AssertionError(f"argv for two models differs in more than the model path: {a} vs {b}")
    s = " ".join(a)
    for frag in ("--served-model-name policy-qwen3.5-9b", "--tool-call-parser qwen3_coder", "--enable-auto-tool-choice", "--max-model-len 32768",
                 "--enable-prefix-caching", "--seed 300", "--port 8011"):
        if frag not in s:
            raise AssertionError(f"argv lacks {frag!r}: {s}")
    i = a.index("--default-chat-template-kwargs")
    if json.loads(a[i + 1]).get("enable_thinking") is not False:
        raise AssertionError(f"chat template kwargs {a[i + 1]!r}")
    c = json.loads(ctx.train("serve-cmd", "--model", "/m/base", "--port", 9000, "--json", expect_rc=0)["out"])["argv"]
    if "9000" not in c or "8011" in c:
        raise AssertionError("--port not honoured")
    return "argv carries the 8 frozen flags, differs only in the model path, honours --port"


class _FakeVLLM(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": lib.SERVED, "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        _FakeVLLM.seen.append((self.path, req))
        if "FAIL500" in str(req.get("prompt", "")):
            body = json.dumps({"error": {"message": "boom"}}).encode()
            self.send_response(500)
        else:
            text = "ok-" + lib.sha_bytes(str(req.get("prompt", "")).encode())[:8] + "<|im_end|>"
            body = json.dumps({"id": "cmpl-1", "object": "text_completion", "model": lib.SERVED,
                               "choices": [{"index": 0, "text": text, "finish_reason": "stop", "logprobs": None}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@check("P1_replay_format", "CONTRACT §7 (replay sends the pinned request, records the answer)")
def p1(ctx):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeVLLM)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        reqs = [{"index": i, "prompt": f"<|im_start|>user\nhello {i}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n", "max_tokens": 64} for i in range(3)]
        rp = os.path.join(ctx.tmp, "p1_requests.jsonl")
        lib.write_jsonl(rp, reqs)
        out1, out2 = os.path.join(ctx.tmp, "p1_out1.jsonl"), os.path.join(ctx.tmp, "p1_out2.jsonl")
        _FakeVLLM.seen.clear()
        res = ctx.train("replay", "--url", f"http://127.0.0.1:{port}/v1", "--requests", rp, "--out", out1, "--json", expect_rc=0)
        summ = json.loads(res["out"])
        if (summ.get("requests"), summ.get("ok"), summ.get("failed")) != (3, 3, 0):
            raise AssertionError(f"summary {summ}")
        rows = lib.read_jsonl(out1)
        if [r.get("index") for r in rows] != [0, 1, 2]:
            raise AssertionError(f"result order {rows}")
        for r, (path, req) in zip(rows, _FakeVLLM.seen):
            if not path.endswith("/completions") or path.endswith("/chat/completions"):
                raise AssertionError(f"replay posted to {path}, want /v1/completions")
            want = {"model": lib.SERVED, "temperature": 0, "seed": 300, "skip_special_tokens": False, "max_tokens": 64}
            for k, v in want.items():
                if req.get(k) != v:
                    raise AssertionError(f"request {k}={req.get(k)!r}, want {v!r}")
            if req.get("prompt") != reqs[r["index"]]["prompt"]:
                raise AssertionError("prompt not sent verbatim")
            exp = "ok-" + lib.sha_bytes(req["prompt"].encode())[:8] + "<|im_end|>"
            if (r.get("text"), r.get("completion_tokens"), r.get("finish_reason"), r.get("status")) != (exp, 5, "stop", 200):
                raise AssertionError(f"result row {r}")
        ctx.train("replay", "--url", f"http://127.0.0.1:{port}/v1", "--requests", rp, "--out", out2, "--json", expect_rc=0)
        if open(out1, "rb").read() != open(out2, "rb").read():
            raise AssertionError("two replays of identical answers are not byte-identical")
        reqs.append({"index": 3, "prompt": "FAIL500", "max_tokens": 8})
        lib.write_jsonl(rp, reqs)
        out3 = os.path.join(ctx.tmp, "p1_out3.jsonl")
        res = ctx.train("replay", "--url", f"http://127.0.0.1:{port}/v1", "--requests", rp, "--out", out3, "--json", expect_rc=1)
        rows = lib.read_jsonl(out3)
        if len(rows) != 4 or rows[3].get("status") != 500 or "error" not in rows[3]:
            raise AssertionError(f"500 not recorded: {rows[-1:]}")
    finally:
        srv.shutdown()
    return "3 pinned requests verbatim (T 0, seed 300, special tokens kept), rows in order, byte-identical twice, a 500 → status 500 + exit 1"


def _compare(ctx, a, b, extra=(), expect_rc=0):
    res = ctx.fleet("compare", "--a", a, "--b", b, "--json", *extra, expect_rc=expect_rc)
    if expect_rc == 0:
        try:
            res["json"] = json.loads(res["out"])
        except ValueError:
            raise AssertionError(f"compare --json stdout is not one JSON object: {lib.short(res['out'], 200)!r}")
    return res


@check("K1_compare_self", "done test 5 (a fleet against itself: exactly 0)")
def k1(ctx):
    res = _compare(ctx, lib.MATRIX_HELDOUT, lib.MATRIX_HELDOUT)
    j = res["json"]
    a = lib.read_json(lib.MATRIX_HELDOUT)
    want = lib.compare_oracle(a, a)
    errs = lib.diff(j, want)
    if errs:
        raise AssertionError("; ".join(errs[:6]))
    for k in ("pass1", "pass4"):
        if j[k]["diff"] != 0.0 or j[k]["ci95"] != [0.0, 0.0] or j[k]["tasks"] != 44:
            raise AssertionError(f"{k}: {j[k]}")
    if any(v != 0 for v in j["causes"]["diff"].values()) or j["m12"]["policy_usd_per_task"]["diff"] != 0.0 or j["m13"]["median_s"]["diff"] != 0.0:
        raise AssertionError("non-zero difference against itself")
    if len(j["paired_tasks"]) != 44 or j["a_only"] or j["b_only"]:
        raise AssertionError("pairing wrong")
    res2 = _compare(ctx, lib.MATRIX_HELDOUT, lib.MATRIX_HELDOUT)
    if res2["out"] != res["out"]:
        raise AssertionError("compare not byte-identical across runs")
    txt = ctx.fleet("compare", "--a", lib.MATRIX_HELDOUT, "--b", lib.MATRIX_HELDOUT, expect_rc=0)["out"]
    if "pass^4" not in txt or "pass^1" not in txt:
        raise AssertionError("text mode lacks pass^1 / pass^4 blocks")
    return "44 tasks paired, pass^1/pass^4 diff 0.0 with ci [0.0, 0.0], every m12/m13/cause diff 0, byte-identical, text has pass^k"


@check("K2_compare_known_diff", "done test 5 (paired differences, pinned bootstrap, M12/M13/causes)")
def k2(ctx):
    a = lib.read_json(lib.MATRIX_HELDOUT)
    # 8 tasks improve, 3 regress, median latency −12 s, one new agent cause, 3 trials/task recorded
    b = lib.synthetic_matrix(a, c_delta=K2_DELTA, median_delta=-12.0, add_cause=("agent_timeout", 3, "agent"), drop_tasks=("113", "110"), trials_per_task=3)
    bp = os.path.join(ctx.tmp, "k2_b.json")
    lib.write_json(bp, b)
    res = _compare(ctx, lib.MATRIX_HELDOUT, bp)
    want = lib.compare_oracle(a, b)
    errs = lib.diff(res["json"], want)
    if errs:
        raise AssertionError("; ".join(errs[:6]))
    j = res["json"]
    if j["a_only"] != ["110", "113"] or j["b_only"] or j["pass1"]["tasks"] != 42:
        raise AssertionError(f"pairing {j['a_only']} {j['b_only']} {j['pass1']['tasks']}")
    if not any("trials_per_task" in w for w in j.get("warnings", [])):
        raise AssertionError(f"no warning about trials_per_task 4 vs 3: {j.get('warnings')}")
    if j["causes"]["diff"].get("agent_timeout") != 3 or j["by_class"]["agent"]["diff"] != 3:
        raise AssertionError(f"causes {j['causes']} {j['by_class']}")
    if abs(j["m13"]["median_s"]["diff"] + 12.0) > TH["tol"]:
        raise AssertionError(f"m13 diff {j['m13']}")
    # another seed: still the pinned formula; --out writes the same JSON. The paired diffs are multiples of 1/4, so
    # many seeds tie on the percentiles (7 does); seed 11 differs from 300 on both pass^1 bounds (proven in selftest)
    outp = os.path.join(ctx.tmp, "k2_out.json")
    res7 = _compare(ctx, lib.MATRIX_HELDOUT, bp, extra=["--seed", str(K2_SEED), "--out", outp])
    want7 = lib.compare_oracle(a, b, seed=K2_SEED)
    errs = lib.diff(res7["json"], want7)
    if errs:
        raise AssertionError(f"seed {K2_SEED}: " + "; ".join(errs[:4]))
    if res7["json"]["pass1"]["ci95"] == j["pass1"]["ci95"] or res7["json"]["bootstrap"]["seed"] != K2_SEED:
        raise AssertionError("seed had no effect on the interval")
    if lib.read_json(outp) != res7["json"]:
        raise AssertionError("--out file differs from stdout")
    # train vs held-out baseline (real matrices, same split file, different part) → refused
    _compare(ctx, lib.MATRIX_HELDOUT, lib.MATRIX_TRAIN, expect_rc=1)
    return f"pass^1 diff {j['pass1']['diff']:+.4f} ci {j['pass1']['ci95']}, pass^4 diff {j['pass4']['diff']:+.4f}: equals the oracle; 42 paired, 2 a_only, warning on trials, m13 −12 s, cause +3; seed changes the interval; train-vs-held-out refused"


@check("K3_compare_refusals", "CONTRACT §8 (tampered or incomparable matrices are never compared)")
def k3(ctx):
    a = lib.read_json(lib.MATRIX_HELDOUT)
    t = json.loads(json.dumps(a))
    t["m11"]["tasks"][3]["pass4"] = 0.5  # parts say otherwise
    tp = os.path.join(ctx.tmp, "k3_tampered.json")
    lib.write_json(tp, t)
    res = _compare(ctx, lib.MATRIX_HELDOUT, tp, expect_rc=1)
    if "pass4" not in (res["err"] + res["out"]) and a["m11"]["tasks"][3]["task"] not in (res["err"] + res["out"]):
        raise AssertionError(f"tamper refusal does not name the field/task: {lib.short(res['err'], 200)!r}")
    sp = os.path.join(ctx.tmp, "k3_sim.json")
    lib.write_json(sp, lib.synthetic_matrix(a, simulator="google/gemma-3-12b-it"))
    _compare(ctx, lib.MATRIX_HELDOUT, sp, expect_rc=1)
    pp = os.path.join(ctx.tmp, "k3_part.json")
    lib.write_json(pp, lib.synthetic_matrix(a, part="train"))
    _compare(ctx, lib.MATRIX_HELDOUT, pp, expect_rc=1)
    np_ = os.path.join(ctx.tmp, "k3_nopair.json")
    lib.write_json(np_, lib.synthetic_matrix(a, drop_tasks=tuple(x["task"] for x in a["m11"]["tasks"])))
    _compare(ctx, lib.MATRIX_HELDOUT, np_, expect_rc=1)
    m = json.loads(json.dumps(a))
    del m["m11"]["tasks"]
    mp = os.path.join(ctx.tmp, "k3_nom11.json")
    lib.write_json(mp, m)
    _compare(ctx, lib.MATRIX_HELDOUT, mp, expect_rc=2)
    # a changed policy is fine
    okp = os.path.join(ctx.tmp, "k3_ok.json")
    lib.write_json(okp, lib.synthetic_matrix(a))
    _compare(ctx, lib.MATRIX_HELDOUT, okp, expect_rc=0)
    return "tampered pass4, other simulator, other split part, no paired task → exit 1; no m11.tasks → exit 2; a new policy compares"


# ====================================================================== host checks (GPU host over ssh)

def _ht(ctx, *parts):
    return "/".join([ctx.args.host_tmp] + list(parts))


def _train_remote(ctx):
    return f"cd {ctx.args.host_repo} && train/bin/train"


@check("H1_select_on_host", "CONTRACT §2 (the same data on the training host)", host=True)
def h1(ctx):
    ctx.ssh(f"mkdir -p {ctx.args.host_tmp} && test -d {ctx.args.host_repo} && test -x {ctx.args.host_repo}/train/bin/train && test -f {ctx.args.model_path}/config.json && test -f {ctx.args.model_path}/tokenizer.json")
    ctx.scp_to(lib.FIX, _ht(ctx, "fixtures"))
    if "s1_examples_sha" not in ctx.cache:
        s1(ctx)
    j = ctx.ssh_json(f"{_train_remote(ctx)} select --out {_ht(ctx, 'data')} --pair {_ht(ctx, 'fixtures/p11/export')} {_ht(ctx, 'fixtures/p11/labels')} "
                     f"--fleet {_ht(ctx, 'fixtures/p11/fleet.json')} --policy {lib.SERVED} --tokenizer {ctx.args.model_path} --json", timeout=TH["select_s"])
    if j.get("examples_sha256") != ctx.cache["s1_examples_sha"]:
        raise AssertionError(f"host examples_sha256 {j.get('examples_sha256', '')[:12]} != local {ctx.cache['s1_examples_sha'][:12]} (model tokenizer vs fixture tokenizer, or host-dependent output)")
    if j.get("counts", {}).get("runs") != 8:
        raise AssertionError(f"host counts {j.get('counts')}")
    return "host select byte-equals the local one (examples_sha256), with the model directory as tokenizer"


def _card_consistent(ctx, fitdir, steps):
    fit = ctx.ssh_json(f"cat {fitdir}/fit.json")
    card = ctx.ssh_json(f"cat {fitdir}/card.json")
    recomputed = ctx.ssh_json(f"{_train_remote(ctx)} card --fit {fitdir} --json")
    if card != recomputed:
        raise AssertionError("card.json != train card recomputation")
    nlog = int(ctx.ssh(f"wc -l < {fitdir}/log.jsonl").stdout.strip() or 0)
    if fit.get("steps") != steps or nlog != steps or card.get("steps") != steps:
        raise AssertionError(f"steps: fit {fit.get('steps')} log {nlog} card {card.get('steps')} want {steps}")
    gpu_h = fit["wall_s"] * fit["gpu"]["count"] / 3600
    if abs(card["gpu_hours"] - gpu_h) > 1e-6 or abs(card["cost_usd"] - gpu_h * TH["price"]) > 1e-6 or card["gpu_usd_per_h"] != TH["price"]:
        raise AssertionError(f"card cost: {card['gpu_hours']} {card['cost_usd']} vs {gpu_h} × {TH['price']}")
    if card.get("note") != f"LoRA · {lib.BASE_MODEL_NAME} · 8 passed runs":
        raise AssertionError(f"note {card.get('note')!r}")
    if fit["data"]["examples_sha256"] != ctx.cache["s1_examples_sha"] or fit["data"]["runs"] != 8:
        raise AssertionError("fit.json data hash/runs not the data dir's")
    ctx.ssh(f"test -f {fitdir}/adapter/adapter_config.json && test -f {fitdir}/adapter/adapter_model.safetensors && test -f {fitdir}/CARD.md")
    return fit, card


@check("H2_fit_identity", "done test 4 (an adapter that changes nothing) + done test 3", host=True)
def h2(ctx):
    if "s1_examples_sha" not in ctx.cache:
        h1(ctx)
    ctx.ssh(f"{_train_remote(ctx)} fit --data {_ht(ctx, 'data')} --model {ctx.args.model_path} --out {_ht(ctx, 'fit0')} --gpu-usd-per-h {TH['price']} --steps 0 --json", timeout=TH["fit_s"])
    fit, card = _card_consistent(ctx, _ht(ctx, "fit0"), 0)
    if fit["adapter"].get("b_all_zero") is not True:
        raise AssertionError("steps 0 adapter does not declare b_all_zero")
    return f"steps 0: identity adapter, wall {fit['wall_s']:.1f} s, card ${card['cost_usd']:.4f} = {card['gpu_hours']:.5f} GPU-h × ${TH['price']}"


@check("H3_fit_steps", "done test 3 (a real record of a tiny run)", host=True)
def h3(ctx):
    if "s1_examples_sha" not in ctx.cache:
        h1(ctx)
    n = ctx.args.steps
    ctx.ssh(f"{_train_remote(ctx)} fit --data {_ht(ctx, 'data')} --model {ctx.args.model_path} --out {_ht(ctx, 'fitN')} --gpu-usd-per-h {TH['price']} --steps {n} --json", timeout=TH["fit_s"])
    fit, card = _card_consistent(ctx, _ht(ctx, "fitN"), n)
    log = [json.loads(l) for l in ctx.ssh(f"cat {_ht(ctx, 'fitN')}/log.jsonl").stdout.splitlines() if l.strip()]
    if [l["step"] for l in log] != list(range(1, n + 1)) or not all(isinstance(l["loss"], (int, float)) and math.isfinite(l["loss"]) and l["loss"] > 0 for l in log):
        raise AssertionError(f"log {log}")
    if not all(l["tokens"] > 0 for l in log):
        raise AssertionError("a step reports 0 loss tokens")
    if fit["adapter"].get("b_all_zero") is not False or fit["wall_s"] <= 0 or fit["epochs"] <= 0:
        raise AssertionError(f"fit facts {fit['adapter']} {fit['wall_s']} {fit['epochs']}")
    return f"{n} steps, losses {[round(l['loss'], 3) for l in log]}, {fit['wall_s']:.0f} s, ${card['cost_usd']:.4f}"


@check("H4_merge", "done test 4 (merged checkpoint on the frozen stack)", host=True)
def h4(ctx):
    for name in ("fit0", "fitN"):
        if ctx.ssh(f"test -f {_ht(ctx, name)}/fit.json", check=False).returncode != 0:
            (h2 if name == "fit0" else h3)(ctx)
    for name, ck in (("fit0", "ckpt_identity"), ("fitN", "ckpt_trained")):
        ctx.ssh(f"{_train_remote(ctx)} merge --model {ctx.args.model_path} --adapter {_ht(ctx, name)}/adapter --out {_ht(ctx, ck)} --json", timeout=TH["merge_s"])
        for f in ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja", "generation_config.json", "vocab.json", "merges.txt"):
            if ctx.ssh(f"test -f {ctx.args.model_path}/{f}", check=False).returncode == 0:
                ctx.ssh(f"cmp -s {ctx.args.model_path}/{f} {_ht(ctx, ck)}/{f}")
        ctx.ssh(f"test -f {_ht(ctx, ck)}/config.json && ls {_ht(ctx, ck)}/*.safetensors >/dev/null && ! test -e {_ht(ctx, ck)}/adapter_config.json")
        base_cfg = ctx.ssh_json(f"cat {ctx.args.model_path}/config.json")
        cfg = ctx.ssh_json(f"cat {_ht(ctx, ck)}/config.json")
        if cfg.get("architectures") != base_cfg.get("architectures"):
            raise AssertionError(f"{ck}: architectures {cfg.get('architectures')} vs base {base_cfg.get('architectures')}")
        mj = ctx.ssh_json(f"cat {_ht(ctx, ck)}/merge.json")
        if mj.get("format") != "avos-train-merge/1" or mj.get("b_all_zero") is not (name == "fit0"):
            raise AssertionError(f"{ck}: merge.json {mj}")
        sha = ctx.ssh(f"sha256sum {_ht(ctx, ck)}/tokenizer.json | cut -d' ' -f1").stdout.strip()
        if mj.get("tokenizer_sha256") != sha:
            raise AssertionError(f"{ck}: tokenizer_sha256 wrong")
    return "identity and trained merges: tokenizer/template/generation config byte-identical to the base, no adapter files, merge.json consistent"


def _hostproc(ctx):
    if "hostproc" not in ctx.cache:
        ctx.ssh(f"mkdir -p {ctx.args.host_tmp}", timeout=60)
        ctx.scp_to(HOSTPROC, _ht(ctx, "hostproc.py"))
        ctx.cache["hostproc"] = True
    return f"python3 {_ht(ctx, 'hostproc.py')}"


def _serve(ctx, model_dir, tag):
    argv = json.loads(ctx.ssh(f"{_train_remote(ctx)} serve-cmd --model {model_dir} --port {ctx.args.port} --json", timeout=120).stdout.strip().splitlines()[-1])["argv"]
    cmd = " ".join(subprocess.list2cmdline([a]) if " " not in a and '"' not in a else "'" + a.replace("'", "'\\''") + "'" for a in argv)
    pidf, logf = _ht(ctx, tag + ".pid"), _ht(ctx, tag + ".log")
    # hostproc start forks, setsids, redirects 0/1/2 and execs: nothing in the ssh session is left holding sshd's pipes
    ctx.ssh(f"cd {ctx.args.host_repo} && {_hostproc(ctx)} start --pid-file {pidf} --log {logf} -- {cmd}", timeout=60)
    t0 = time.monotonic()
    while True:
        left = TH["vllm_ready_s"] - (time.monotonic() - t0)
        if left <= 0:
            raise AssertionError(f"vLLM for {tag} not ready within {TH['vllm_ready_s']} s")
        to = max(5, min(30, left))
        p = ctx.ssh(f"curl -s -m 5 http://127.0.0.1:{ctx.args.port}/v1/models", check=False, timeout=to)
        if p.returncode == 0 and lib.SERVED in p.stdout:
            return time.monotonic() - t0
        if ctx.ssh(f"{_hostproc(ctx)} alive --pid-file {pidf}", check=False, timeout=to).returncode != 0:
            raise AssertionError(f"vLLM for {tag} died: {lib.short(ctx.ssh(f'tail -c 1500 {logf}', check=False, timeout=to).stdout, 600)}")
        time.sleep(5)


def _stop(ctx, pidf):
    """Kill the whole session the pid file names, then the port; returns the FAIL text for anything left ('' when clean)."""
    p = ctx.ssh(f"{_hostproc(ctx)} stop --pid-file {pidf} --port {ctx.args.port}", check=False, timeout=120)
    return _leftover_text(p.stdout, pidf)


def _leftover_text(stdout, pidf):
    try:
        j = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return f"cleanup of {pidf}: hostproc stop gave no report: {lib.short(stdout, 200)!r}"
    if j.get("error"):
        return f"cleanup of {pidf}: {j['error']}"
    parts = [f"pid {l['pid']} ({l['comm']}{', holds GPU memory' if l['gpu'] else ''}) survived SIGKILL" for l in j["leftover"]]
    if j["port_open"]:
        parts.append("port still open")
    return f"cleanup of {pidf}: " + "; ".join(parts) if parts else ""


@contextlib.contextmanager
def _served(ctx, model_dir, tag):
    try:
        yield lambda: _serve(ctx, model_dir, tag)
    finally:
        left = _stop(ctx, _ht(ctx, tag + ".pid"))
    if left:  # reached only when the body did not raise: the body's failure wins, else the leftover is the FAIL
        raise AssertionError(left)


def _replay(ctx, tag):
    out = _ht(ctx, f"replay_{tag}.jsonl")
    ctx.ssh(f"{_train_remote(ctx)} replay --url http://127.0.0.1:{ctx.args.port}/v1 --requests {_ht(ctx, 'requests.jsonl')} --out {out} --json", timeout=600)
    local = os.path.join(ctx.tmp, f"replay_{tag}.jsonl")
    ctx.scp_from(out, local)
    return lib.read_jsonl(local)


@check("H5_identity_serving", "done test 4 (zero-B merge is token-identical to the base on replayed requests)", host=True)
def h5(ctx):
    for ck in ("ckpt_identity", "ckpt_trained"):
        if ctx.ssh(f"test -f {_ht(ctx, ck)}/merge.json", check=False).returncode != 0:
            h4(ctx)
            break
    orc = ctx.oracle()
    reqs, i = [], 0
    for it in ctx.kept_items():
        for k in lib.policy_turns(it)[:1] + lib.policy_turns(it)[-1:]:
            if i >= TH["replay_prompts"]:
                break
            reqs.append({"index": i, "prompt": orc[lib.content_sha(it)][str(k)]["prompt_text"], "max_tokens": TH["replay_max_tokens"]})
            i += 1
    rp = os.path.join(ctx.tmp, "requests.jsonl")
    lib.write_jsonl(rp, reqs)
    ctx.scp_to(rp, _ht(ctx, "requests.jsonl"))
    timings = {}
    with _served(ctx, ctx.args.model_path, "base") as up:
        timings["base"] = up()
        base = _replay(ctx, "base")
    with _served(ctx, _ht(ctx, "ckpt_identity"), "identity") as up:
        timings["identity"] = up()
        ident = _replay(ctx, "identity")
    if len(base) != len(reqs) or len(ident) != len(reqs):
        raise AssertionError(f"replay rows {len(base)} / {len(ident)} vs {len(reqs)} requests")
    bad = [(b["index"], b.get("text", "")[:40], d.get("text", "")[:40]) for b, d in zip(base, ident)
           if (b.get("text"), b.get("completion_tokens"), b.get("finish_reason"), b.get("status")) != (d.get("text"), d.get("completion_tokens"), d.get("finish_reason"), d.get("status")) or b.get("status") != 200]
    if bad:
        raise AssertionError(f"identity merge differs from the base on {len(bad)}/{len(reqs)} prompts: {bad[:2]}")
    if not all(b.get("completion_tokens", 0) > 0 for b in base):
        raise AssertionError("empty completions: the check proves nothing")
    with _served(ctx, _ht(ctx, "ckpt_trained"), "trained") as up:
        timings["trained"] = up()
        tr = _replay(ctx, "trained")
    if len(tr) != len(reqs) or any(r.get("status") != 200 for r in tr):
        raise AssertionError(f"trained merge did not answer every request: {[r.get('status') for r in tr]}")
    diffs = sum(1 for b, t in zip(base, tr) if b.get("text") != t.get("text"))
    return f"{len(reqs)} prompts: identity merge token-identical to base ({sum(b['completion_tokens'] for b in base)} completion tokens); trained merge serves ({diffs} replies differ); vLLM ready in {', '.join(f'{k} {v:.0f}s' for k, v in timings.items())}"


# ====================================================================== selftest

def selftest(ctx):
    n = lib.check_fixture_manifest()
    print(f"ok   fixtures: {n} hashed files match MANIFEST.sha256")
    for it in ctx.items:
        assert lib.item_id(it) == it["item_id"], it["run"]
        assert lib.content_sha(it) == ctx.row_by_run[it["run"]]["content_sha256"], it["run"]
    print(f"ok   content keys and item_ids recomputed for {len(ctx.items)} real items / label rows")
    kinds = {ctx.row_by_run[it["run"]]["final"]["rule"] for it in ctx.items}
    assert kinds >= {"reward", "human", "veto", "tau2_384"}, kinds
    st = lib.select_oracle(ctx.real_pair())
    assert st[0] == "ok" and len(st[1]) == 8 and len(st[2]) == 4 and {r[2] for r in st[2]} == {"not_kept"}, st
    base = ctx.item("run_retail-17_11")
    row = ctx.row_by_run["run_retail-17_11"]
    ho = lib.set_task(lib.clone_item(base, 901), "10", "held_out")
    assert lib.select_oracle([(ctx.items + [ho], ctx.rows + [lib.row_for(ho, row, kept=True)])])[:2] == ("refuse", "kept_not_train")
    ho2 = lib.set_task(lib.clone_item(base, 902), "10", "train")
    assert lib.select_oracle([(ctx.items + [ho2], ctx.rows + [lib.row_for(ho2, row, kept=True)])])[:2] == ("refuse", "kept_not_train")
    wd = json.loads(json.dumps(base))
    wd["witness"]["agrees"] = False
    lib.rehash(wd)
    assert lib.select_oracle([([it for it in ctx.items if it["run"] != base["run"]] + [wd], [r for r in ctx.rows if r["run"] != base["run"]] + [lib.row_for(wd, row, kept=True)])])[:2] == ("refuse", "kept_witness_disagrees")
    assert lib.select_oracle([(ctx.items, ctx.rows)], exclude=("17",))[2].count(("run_retail-17_11", 0, "excluded_task")) == 1
    r2 = lib.row_for(base, row)
    r2["content_sha256"] = "00" * 32
    assert lib.select_oracle([(ctx.items, [r for r in ctx.rows if r["run"] != base["run"]] + [r2])])[:2] == ("refuse", "content_sha256_mismatch")
    print("ok   select oracle: 8 selected / 4 not_kept on the real pair; held-out, lying split, witness, excluded, content refusals")
    t0 = time.monotonic()
    orc = ctx.oracle()
    turns = 0
    for it in ctx.items:
        for k in lib.policy_turns(it):
            o = orc[lib.content_sha(it)][str(k)]
            u = it["messages"][k]["usage"]
            assert len(o["prompt"]) == u["prompt_tokens"] and len(o["completion"]) == u["completion_tokens"], (it["run"], k, len(o["prompt"]), len(o["completion"]), u)
            assert o["completion"][-1] == orc["_im_end_id"]
            assert o["prompt_text"].endswith("<think>\n\n</think>\n\n")
            turns += 1
    print(f"ok   tokenizer oracle: {turns} policy turns of 12 real items reproduce the gateway's prompt/completion counts exactly, end in <|im_end|> ({time.monotonic() - t0:.0f} s)")
    # the examples checker on a golden data dir written straight from the oracle in the CONTRACT §2 format
    kept = ctx.kept_items()
    gold = ctx.scratch("self_gold")
    rows_out = []
    for it in sorted(kept, key=lambda x: x["item_id"]):
        for k in lib.policy_turns(it):
            o = orc[lib.content_sha(it)][str(k)]
            rows_out.append({"item_id": it["item_id"], "run": it["run"], "task_id": it["task_id"], "source": 0, "turn_index": k,
                             "prompt_tokens": len(o["prompt"]), "completion_tokens": len(o["completion"]),
                             "input_ids": o["prompt"] + o["completion"], "loss_mask": [0] * len(o["prompt"]) + [1] * len(o["completion"])})
    with open(os.path.join(gold, "examples.jsonl"), "w") as f:
        for r in rows_out:
            f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
    pt, ct = sum(r["prompt_tokens"] for r in rows_out), sum(r["completion_tokens"] for r in rows_out)
    lib.write_json(os.path.join(gold, "manifest.json"), {"examples_sha256": lib.sha_file(os.path.join(gold, "examples.jsonl")),
                   "counts": {"runs": 8, "tasks": len({it["task_id"] for it in kept}), "examples": len(rows_out), "prompt_tokens": pt, "completion_tokens": ct, "tokens": pt + ct},
                   "selected": [{"item_id": i} for i in sorted({r["item_id"] for r in rows_out})]})
    _check_examples(ctx, gold, kept, {it["item_id"]: 0 for it in kept})
    bad = lib.read_jsonl(os.path.join(gold, "examples.jsonl"))
    bad[5]["loss_mask"][bad[5]["prompt_tokens"] - 1] = 1  # one prompt token takes loss
    with open(os.path.join(gold, "examples.jsonl"), "w") as f:
        for r in bad:
            f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
    try:
        _check_examples(ctx, gold, kept, {it["item_id"]: 0 for it in kept})
        raise RuntimeError("checker accepted a mask that covers a prompt token")
    except AssertionError as e:
        assert "loss_mask" in str(e), e
    print(f"ok   examples checker: golden dir of {len(rows_out)} examples accepted; one prompt token under loss → rejected")
    # the S4 same-size label swap and the S5 argument re-serialisation really change what the oracle says
    p = os.path.join(ctx.scratch("self_swap"), "labels.jsonl")
    lib.write_jsonl(p, sorted(ctx.rows, key=lambda r: r["run"]))
    rows = lib.read_jsonl(p)
    for r in rows:
        if r["run"] == "run_retail-17_11":
            r["final"]["kept"], r["final"]["label"] = False, "fail"
        if r["run"] == "run_retail-73_74":
            r["final"]["kept"], r["final"]["label"] = True, "pass"
    new = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    assert len(new) == os.path.getsize(p), (len(new), os.path.getsize(p))
    lib.same_size_replace(p, new)
    st = lib.select_oracle([(ctx.items, lib.read_jsonl(p))])
    assert st[0] == "ok" and {s[1] for s in st[1]} == ({it["run"] for it in kept} - {"run_retail-17_11"}) | {"run_retail-73_74"}
    base17 = ctx.item("run_retail-17_10")
    rs, mv = lib.reserialise_args(base17), lib.mutate_arg_value(base17)
    assert len({lib.content_sha(base17), lib.content_sha(rs), lib.content_sha(mv)}) == 3
    o2 = lib.tok_oracle(ctx.scratch("self_oracle2"), ctx.tok_dir(), [rs, mv], timeout=TH["tok_oracle_s"])
    same = o2[lib.content_sha(rs)]
    for k in lib.policy_turns(base17):
        assert same[str(k)]["prompt"] == orc[lib.content_sha(base17)][str(k)]["prompt"] and same[str(k)]["completion"] == orc[lib.content_sha(base17)][str(k)]["completion"], k
    off = [k for k in lib.policy_turns(mv) if (len(o2[lib.content_sha(mv)][str(k)]["prompt"]), len(o2[lib.content_sha(mv)][str(k)]["completion"])) != tuple(o2[lib.content_sha(mv)][str(k)]["usage"])]
    assert off, "a changed argument value left every count equal: S5d would prove nothing"
    print(f"ok   same-size label swap moves 17_11 out and 73_74 in; re-serialised arguments render the same ids (new content key); a changed value breaks parity on {len(off)} turns")
    fl = lib.read_json(lib.FLEET)
    assert fl["roles"]["policy"] == lib.SERVED and fl["split"]["sha256"] == lib.sha_file(lib.SPLIT) and fl["fleet"] == "p11-train"
    wrong = json.loads(json.dumps(fl))
    wrong["roles"]["policy"] = "policy-qwen3.5-9b-think"
    assert lib.select_oracle([(ctx.items, ctx.rows, wrong)])[:2] == ("refuse", "policy_mismatch")
    assert lib.select_oracle([(ctx.items, ctx.rows, fl)], policy="other")[:2] == ("refuse", "policy_mismatch")
    print("ok   fixture fleet.json: p11-train, policy role = the served name, split hash = the split file; another role or --policy → policy_mismatch")
    assert lib.pass_k(4, 4, 4) == 1.0 and lib.pass_k(4, 3, 4) == 0.0 and lib.pass_k(8, 7, 4) == 0.5 and lib.pass_k(3, 2, 4) is None and abs(lib.pass_k(8, 5, 4) - 5 / 70) < 1e-12
    a = lib.read_json(lib.MATRIX_HELDOUT)
    vals = [t["pass1"] for t in a["m11"]["tasks"]]
    assert lib.bootstrap(vals) == a["m11"]["pass1_ci95"], (lib.bootstrap(vals), a["m11"]["pass1_ci95"])
    vals4 = [t["pass4"] for t in a["m11"]["tasks"]]
    assert lib.bootstrap(vals4) == a["m11"]["pass4_ci95"]
    for t in a["m11"]["tasks"]:
        assert abs(lib.pass_k(t["n"], t["c"], 4) - t["pass4"]) < 1e-12 and abs(lib.pass_k(t["n"], t["c"], 1) - t["pass1"]) < 1e-12
    print("ok   pass^k and the pinned bootstrap reproduce the real POC 11 held-out matrix (pass^1 and pass^4 intervals exactly)")
    self_ = lib.compare_oracle(a, a)
    assert self_["pass1"]["diff"] == 0.0 and self_["pass4"]["ci95"] == [0.0, 0.0] and self_["pass1"]["tasks"] == 44
    b = lib.synthetic_matrix(a, c_delta={"10": +2, "11": -1}, drop_tasks=("113",))
    c = lib.compare_oracle(a, b)
    d10 = next(t for t in c["tasks"] if t["task"] == "10")
    assert d10["d_pass1"] == 0.5 and d10["b"]["c"] == 2 and c["pass1"]["tasks"] == 43 and c["a_only"] == ["113"]
    assert abs(c["pass1"]["diff"] - (0.5 - 0.25) / 43) < 1e-12
    lo, hi = c["pass1"]["ci95"]
    assert lo <= c["pass1"]["diff"] <= hi
    bk = lib.synthetic_matrix(a, c_delta=K2_DELTA, drop_tasks=("113", "110"))
    c300, c11 = lib.compare_oracle(a, bk, seed=300), lib.compare_oracle(a, bk, seed=K2_SEED)
    assert c300["pass1"]["ci95"] != c11["pass1"]["ci95"], (c300["pass1"]["ci95"], c11["pass1"]["ci95"])
    assert lib.compare_oracle(a, bk, seed=7)["pass1"]["ci95"] == c300["pass1"]["ci95"]  # the tie the solver reported
    empty = lib.synthetic_matrix(a, drop_tasks=tuple(x["task"] for x in a["m11"]["tasks"]))
    assert empty["m11"]["tasks"] == [] and empty["m11"]["pass1"] == "unverified"
    assert lib.diff({"x": 1.0, "y": [1, {"z": "a"}]}, {"x": 1.0 + 1e-10, "y": [1, {"z": "a"}]}) == []
    assert lib.diff({"x": 1.0}, {"x": 1.1}) and lib.diff({"x": "unverified"}, {"x": 0.0})
    print(f"ok   compare oracle: self → zeros; +2/−1 on two tasks → paired diff (0.5 − 0.25)/43 with the interval around it; K2 seed {K2_SEED} ≠ seed 300 (7 ties); every task dropped → no crash; diff() catches 1e-1 and 'unverified'")
    d, fit, losses = lib.make_fitdir(ctx.tmp, "self_fit", steps=3, wall_s=100.0, gpu_count=2, price=2.0, runs=5)
    assert len(lib.read_jsonl(os.path.join(d, "log.jsonl"))) == 3 and fit["gpu"]["count"] == 2
    print("ok   fit directory synthesis (3 steps, 2 GPUs)")
    p = os.path.join(ctx.tmp, "self_same.txt")
    open(p, "wb").write(b"abcdef")
    st0 = os.stat(p)
    lib.same_size_replace(p, b"abcdeX")
    st1 = os.stat(p)
    assert open(p, "rb").read() == b"abcdeX" and st0.st_mtime_ns == st1.st_mtime_ns and st0.st_size == st1.st_size
    print("ok   same-size, same-mtime replacement")

    # the H5 server lifecycle: start returns at once with 0/1/2 detached; stop kills the whole session (a child that
    # ignores SIGTERM and holds the port, like vLLM's EngineCore), reports nothing left, and the port is closed
    d = ctx.scratch("hostproc")
    pidf, logf = os.path.join(d, "fake.pid"), os.path.join(d, "fake.log")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    child = ("import os, signal, socket, time, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True)\n"
             "if os.fork() == 0:\n"
             "    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', %d)); s.listen(1)\n"
             "    time.sleep(600)\n"
             "time.sleep(600)") % port
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, HOSTPROC, "start", "--pid-file", pidf, "--log", logf, "--", sys.executable, "-c", child],
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0 and time.monotonic() - t0 < 5, (p.returncode, p.stderr)
    leader = int(open(pidf).read())
    assert int(p.stdout.strip()) == leader and os.getsid(leader) == leader, "pid file is not the session leader"
    for _ in range(50):
        if hostproc.port_open(port) and os.path.getsize(logf):
            break
        time.sleep(0.1)
    assert hostproc.port_open(port) and open(logf).read().startswith("up"), "fake server not up / log not captured"
    assert subprocess.run([sys.executable, HOSTPROC, "alive", "--pid-file", pidf]).returncode == 0
    members = hostproc.session_pids(leader)
    assert len(members) == 2, members
    os.kill(leader, signal.SIGTERM)
    time.sleep(0.5)
    assert hostproc.session_pids(leader), "fake should ignore SIGTERM (so the stop path is exercised, not the easy case)"
    p = subprocess.run([sys.executable, HOSTPROC, "stop", "--pid-file", pidf, "--port", str(port), "--term-wait", "2"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and _leftover_text(p.stdout, pidf) == "", (p.stdout, p.stderr)
    assert not hostproc.session_pids(leader) and not hostproc.port_open(port)
    assert subprocess.run([sys.executable, HOSTPROC, "alive", "--pid-file", pidf]).returncode != 0
    assert _leftover_text(json.dumps({"leftover": [{"pid": 7, "comm": "x", "gpu": True}], "port_open": True}), "f") == "cleanup of f: pid 7 (x, holds GPU memory) survived SIGKILL; port still open"
    p = subprocess.run([sys.executable, HOSTPROC, "stop", "--pid-file", pidf, "--port", "8000"], capture_output=True, text=True, timeout=10)
    assert p.returncode == 2 and "refusing" in _leftover_text(p.stdout, pidf)
    print(f"ok   hostproc: start detached in {time.monotonic() - t0:.1f} s total, 2-process session (TERM-immune) stopped clean, port closed, port 8000 refused")
    return True


# ====================================================================== main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="local checks only (the default when no --host is given)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--host", metavar="ALIAS", help="GPU host ssh alias for the live checks")
    ap.add_argument("--model-path", default="/models/Qwen3.5-9B", help="base model directory on the host")
    ap.add_argument("--steps", type=int, default=2, help="optimizer steps for the tiny live fit")
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--host-repo", default="~/poc")
    ap.add_argument("--host-tmp", default="/tmp/p12-tests")
    ap.add_argument("--only", default="")
    ap.add_argument("--keep", action="store_true", help="keep the local scratch directory")
    args = ap.parse_args()
    if args.port == 8000:
        ap.error("--port 8000 is the host's production vLLM; the suite never starts or kills anything there")
    ctx = Ctx(args)
    print(f"scratch {ctx.tmp}")
    failed = 0
    try:
        if args.selftest:
            try:
                selftest(ctx)
                print("PASS selftest")
            except Exception as e:
                failed += 1
                print(f"FAIL selftest: {e}")
                traceback.print_exc()
            return failed
        try:
            lib.check_fixture_manifest()
        except AssertionError as e:
            print(f"FAIL fixtures  {e}")
            return 1
        failed = _run_checks(ctx, args)
        failed += ctx.host_cleanup()
        return failed
    finally:
        ctx.host_cleanup()
        if failed == 0 and not args.keep:
            shutil.rmtree(ctx.tmp, ignore_errors=True)
        else:
            print(f"scratch kept at {ctx.tmp}")


def _run_checks(ctx, args):
    failed = 0
    for cid, target, host, fn in CHECKS:
        if args.only and not cid.startswith(args.only):
            continue
        if host and not args.host:
            print(f"SKIP {cid}  [{target}]  needs --host")
            continue
        t0 = time.monotonic()
        try:
            msg = fn(ctx)
            print(f"PASS {cid}  [{target}]  {msg}  ({time.monotonic() - t0:.0f} s)")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {cid}  [{target}]  {e}  ({time.monotonic() - t0:.0f} s)")
        except Exception as e:
            failed += 1
            print(f"FAIL {cid}  [{target}]  suite error {type(e).__name__}: {lib.short(e, 300)}  ({time.monotonic() - t0:.0f} s)")
            traceback.print_exc()
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
