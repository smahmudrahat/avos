#!/usr/bin/env python3
"""POC 8 acceptance suite (retrain/CONTRACT.md). Black box: only retrain/bin/retrain, the POC 6/7 tools as
oracles, and raw fixtures are touched.

  python3 retrain/tests/run.py              full local run (includes the 700-run scale checks)
  python3 retrain/tests/run.py --quick      skips the scale checks
  python3 retrain/tests/run.py --selftest   proves the suite's own oracles catch what they claim
  python3 retrain/tests/run.py --host       adds the live checks on avos-dev over ssh (repo pushed to ~/poc)
  python3 retrain/tests/run.py --only P     only checks whose id starts with P

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
import lib

CHECKS = []
TH = {"shortcut_share": 0.9, "export_s_per_run": 0.2, "export_s_fixed": 5.0, "rss_mib": 512, "scale_factor": 3.0,
      "view_s": 2.0, "n_scale_runs": 700, "n_noise_vms": 2000, "n_small_runs": 20}
UV = ["uv", "run", "--isolated", "--no-project", "--python", "3.12", "--with", "trl", "--with", "transformers",
      "--with", "datasets", "--with", "torch", "--with", "accelerate", "python", "-I"]
HOST = "avos-dev"
HOST_REPO = "~/poc"
HOST_TMP = "/tmp/retrain-tests"


def check(cid, target, scale=False, host=False):
    def deco(fn):
        CHECKS.append((cid, target, scale, host, fn))
        return fn
    return deco


class Ctx:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="poc8-tests-")
        self.cache = {}
        self._env = None
        self.snap0 = None
        self.ssh_ctl = None

    @property
    def env(self):
        if self._env is None:
            e = lib.Env(self.tmp)
            lib.clone_run(e, "run_retail-0_13", "run_retail-10_0", "vm-01-aa0010", task_id="10")
            lib.clone_run(e, "run_retail-0_13", "run_retail-0_80", "vm-01-aa0080")
            lib.clone_run(e, "run_retail-0_11", "run_retail-0_90", "vm-01-aa0090", reward_info=None)
            lib.clone_run(e, "run_retail-0_12", "run_retail-0_91", "vm-01-aa0091", tamper_usage=True)
            lib.clone_run(e, "run_retail-0_11", "run_retail-0_92", "vm-01-aa0092", counters="truncate")
            lib.clone_run(e, "run_retail-0_11", "run_retail-0_93", "vm-01-aa0093", counters="none")
            lib.clone_run(e, "run_retail-0_14", "run_retail-0_94", "vm-01-aa0094", stale_copy=True)
            os.remove(os.path.join(e.results, "run_retail-0_15", "events.jsonl"))
            for name, mut in (("201", lib.mut_reply_text), ("202", lib.mut_user_text), ("203", lib.mut_tool_result), ("204", lib.mut_tool_args),
                              ("205", lib.mut_tool_id), ("206", lib.mut_policy), ("207", None), ("208", lib.mut_swap_raw_ids)):
                lib.clone_run(e, "run_retail-0_16", f"run_retail-0_{name}", f"vm-01-bb{name}", mutate=mut)
            lib.clone_run(e, "run_retail-0_16", "run_retail-0_209", "vm-01-bb209")
            self.judge_id = lib.inject_call(e, "vm-01-bb209", "deepseek-ai/DeepSeek-V4-Flash-0731")
            lib.clone_run(e, "run_retail-0_16", "run_retail-0_210", "vm-01-bb210")
            lib.inject_call(e, "vm-01-bb210", "Qwen/Qwen3-30B-A3B-Instruct-2507", response_id="chatcmpl-leftover-0001")
            self.snap0 = e.roots_snapshot()
            self._env = e
        return self._env

    def export(self, key, *args, env=None, expect_rc=None, **kw):
        """Cached export; returns dict(dir, res, items, manifest, sft, stats)."""
        if key in self.cache:
            return self.cache[key]
        out = self.env.out(key)
        res = lib.retrain("export", "--out", out, "--split", lib.SPLIT_REL, *args, env=env or self.env.env(), **kw)
        d = {"dir": out, "res": res, "items": [], "manifest": None, "sft": [], "stats": None}
        if not lib.no_traceback(res):
            raise AssertionError(f"traceback on stderr: {lib.short(res['err'], 300)}")
        if expect_rc is not None and res["rc"] != expect_rc:
            raise AssertionError(f"export {key}: rc={res['rc']} (want {expect_rc}) stderr={lib.short(res['err'], 300)!r}")
        if os.path.exists(os.path.join(out, "manifest.json")):
            d["manifest"] = json.load(open(os.path.join(out, "manifest.json")))
            d["items"] = lib.read_jsonl(os.path.join(out, "trajectories.jsonl"))
            d["sft"] = lib.read_jsonl(os.path.join(out, "sft.jsonl"))
            d["stats"] = json.load(open(os.path.join(out, "stats.json")))
        elif expect_rc in (0, 1):
            raise AssertionError(f"export {key}: rc={res['rc']} but no manifest.json written; stderr={lib.short(res['err'], 300)!r}")
        self.cache[key] = d
        return d

    def base(self):
        return self.export("base", *sum((["--run", r] for r in lib.STORE_RUNS), []), expect_rc=0)

    def everything(self):
        return self.export("all", "--all", expect_rc=1)

    def tree(self):
        return self.export("tree", "--run", lib.TREE_VM, expect_rc=0)

    def item(self, exp, run, principal=None):
        for it in exp["items"]:
            if it["run"] == run and (principal is None and it["principal"].endswith("/agent/tau2") or it["principal"] == principal):
                return it
        for it in exp["items"]:
            if it["run"] == run and principal is None and it["principal"].count("/agent/") == 1 and "/sub/" not in it["principal"]:
                return it
        raise AssertionError(f"no item for {run} {principal or '(agent)'} in {exp['dir']}")

    def view(self, *args, env=None):
        res = lib.retrain("view", *args, env=env or self.env.env())
        if not lib.no_traceback(res):
            raise AssertionError(f"view traceback: {lib.short(res['err'], 300)}")
        return res

    def ssh_base(self):
        if self.ssh_ctl is None:
            d = tempfile.mkdtemp(prefix="p8-", dir="/tmp")
            self.ssh_dir, self.ssh_ctl = d, os.path.join(d, "cm-%C")
            if len(os.path.join(d, "cm-" + "0" * 40)) >= 100:
                raise AssertionError(f"ssh ControlPath would exceed the socket limit: {d}")
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self.ssh_ctl}", "-o", "ControlPersist=300", HOST]

    def ssh(self, cmd, timeout=600, stdin=None):
        p = subprocess.run(self.ssh_base() + [cmd], capture_output=True, text=True, timeout=timeout, input=stdin)
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
                        f"sudo rm -rf {HOST_TMP} /tmp/poc8-tests-* /tmp/poc8-selftest-*"], capture_output=True, text=True, timeout=120)


@check("A0_cli_surface", "CONTRACT §1/§3: exit codes, no tracebacks, `runs` lists every run with its evidence")
def a0(ctx):
    e = ctx.env
    r = lib.retrain(env=e.env())
    probs = []
    if r["rc"] != 2 or not lib.no_traceback(r):
        probs.append(f"no args: rc={r['rc']}")
    r = lib.retrain("nonsense", env=e.env())
    if r["rc"] != 2 or not lib.no_traceback(r):
        probs.append(f"unknown subcommand: rc={r['rc']}")
    r = lib.retrain("runs", env=e.env())
    if r["rc"] != 0:
        probs.append(f"runs rc={r['rc']} {lib.short(r['err'])}")
    else:
        try:
            rows = json.loads(r["out"])
        except ValueError:
            rows = None
        want = sorted(n for n in os.listdir(e.results) if n.startswith("run_"))
        got = sorted(x.get("run") for x in rows) if isinstance(rows, list) else None
        if got != want:
            probs.append(f"runs: want {len(want)} runs, got {got and len(got)}; missing {sorted(set(want) - set(got or []))[:4]}")
        else:
            by = {x["run"]: x for x in rows}
            for run, ev in (("run_retail-0_12", "store"), ("run_retail-0_10", "ledger"), ("run_retail-8_1", "witness"), ("run_retail-0_9", "ledger")):
                if by[run].get("evidence") != ev:
                    probs.append(f"{run}: evidence {by[run].get('evidence')!r} want {ev!r}")
            if by["run_retail-8_1"].get("exportable") is not False or by["run_retail-0_12"].get("exportable") is not True:
                probs.append("exportable flags wrong for witness-only / store runs")
            for k in ("harness", "task", "vm_id", "witness", "reason"):
                if k not in by["run_retail-0_12"]:
                    probs.append(f"runs entries lack {k}")
    return not probs, "; ".join(probs) or "no-args and unknown subcommand exit 2; runs lists 20 runs with evidence store/ledger/witness"


def _viewer_check(ctx, run, html=False):
    e = ctx.env
    sim = e.sim(run)
    res = ctx.view("--run", run, *(["--html"] if html else []))
    if res["rc"] != 0:
        return [f"rc={res['rc']} {lib.short(res['err'])}"]
    text = lib.strip_html(res["out"]) if html else res["out"]
    probs = []
    if html and "<" not in res["out"]:
        probs.append("--html output has no markup")
    for tok in (f"run={run}", "principal=spiffe://avoslab.com/vm/", "task=retail/0", "evidence=store", "trace=complete"):
        if tok not in text:
            probs.append(f"header lacks {tok!r}")
    needles = lib.viewer_needles(sim, lib.system_prompt_for(sim), None)
    miss = lib.in_order(text, needles)
    if miss is not None:
        probs.append(f"missing or out of order: {lib.short(miss, 80)!r}")
    evs = e.store_events(e.vm_of_run(run))
    ag = e.job(run)["agent_spiffe_id"]
    pairs, _ = lib.match_witness(sim["messages"], lib.agent_calls(evs, ag))
    n_policy = sum(1 for i, _ in pairs if sim["messages"][i]["role"] == "assistant")
    if text.count("[policy]") != n_policy:
        probs.append(f"[policy] markers: {text.count('[policy]')} want {n_policy} (assistant turns matched to a stored llm.call)")
    if "[env]" not in text:
        probs.append("no [env] marker (greeting, user simulator, tool results are environment)")
    ids = {x for x in re.findall(r"event=(\S+)", text)}
    store_ids = {x["event_id"] for x in evs}
    if not ids or not ids <= store_ids:
        probs.append(f"event= ids not all from the store: {sorted(ids - store_ids)[:2]}")
    if "WITNESS DISAGREES" in text:
        probs.append("WITNESS DISAGREES shown on a run whose witness agrees")
    return probs


@check("D1_viewer_judgeable", "done test 1: system prompt, every turn in order, every tool call with arguments and result, outcome with reward breakdown, on a failed and a passed run")
def d1(ctx):
    probs = []
    for run in ("run_retail-0_12", "run_retail-0_13"):
        probs += [f"{run}: {p}" for p in _viewer_check(ctx, run)]
    return not probs, "; ".join(probs[:4]) or "both runs: header, outcome (db_match, 5 action checks), system prompt, 16/16 messages with ids and arguments in order, [policy]/[env] markers, event ids from the store"


@check("D1b_viewer_html", "done test 1: the static-HTML form carries the same content")
def d1b(ctx):
    probs = _viewer_check(ctx, "run_retail-0_13", html=True)
    return not probs, "; ".join(probs[:3]) or "HTML view of run_retail-0_13 carries every needle in order"


@check("D1c_viewer_errors", "CONTRACT §10: unknown run or principal → exit 1, empty stdout, no traceback")
def d1c(ctx):
    r1 = ctx.view("--run", "run_retail-99_9")
    r2 = ctx.view("--run", "run_retail-0_12", "--principal", "spiffe://avoslab.com/vm/vm-01-e1a4cf/agent/nobody")
    probs = []
    for n, r in (("unknown run", r1), ("unknown principal", r2)):
        if r["rc"] != 1 or r["out"].strip():
            probs.append(f"{n}: rc={r['rc']} stdout={lib.short(r['out'], 60)!r}")
    return not probs, "; ".join(probs) or "both exit 1 with nothing on stdout"


@check("D1d_viewer_tree", "statement: per-principal conversations; a parent references its children; a sub-agent's view is its own log")
def d1d(ctx):
    e = ctx.env
    vm = lib.TREE_VM
    base = f"spiffe://avoslab.com/vm/{vm}/agent/support"
    evs = e.store_events(vm)
    r = ctx.view("--run", vm, "--principal", base)
    probs = []
    if r["rc"] != 0:
        return False, f"view support rc={r['rc']} {lib.short(r['err'])}"
    t = r["out"]
    if "children=3" not in t:
        probs.append("support view lacks children=3")
    for b in ("B1", "B2", "B3"):
        if not re.search(rf"child {re.escape(base)}/sub/{b} item=[0-9a-f]{{64}} passed=null", t):
            probs.append(f"no child line for {b} with an item id")
    tool_ids = [x["event_id"] for x in evs if x["kind"] == "tool.call" and x["principal"] == base]
    if lib.in_order(t, [f"tool_call {i} issue_refund " for i in tool_ids]) is not None:
        probs.append("support view lacks its 10 gateway tool calls (ids = tool.call event ids) in order")
    if '"refund_id"' not in t:
        probs.append("tool results not shown")
    r2 = ctx.view("--run", vm, "--principal", base + "/sub/B2")
    if r2["rc"] != 0:
        probs.append(f"view B2 rc={r2['rc']}")
    else:
        t2 = r2["out"]
        b2_calls = [x["event_id"] for x in evs if x["kind"] == "llm.call" and x["principal"] == base + "/sub/B2"]
        if not all(i in t2 for i in b2_calls):
            probs.append("B2 view lacks its own llm.call event ids")
        if any(i in t2 for i in tool_ids):
            probs.append("B2 view shows the parent's tool calls")
        if "[policy]" in t2:
            probs.append("B2 (no content captured) shows [policy] turns")
        if "evidence=store" not in t2 or "trace=complete" not in t2:
            probs.append("B2 header lacks evidence/trace")
    r3 = ctx.view("--run", vm)
    if r3["rc"] != 0 or f"principal={base}" not in r3["out"]:
        probs.append("default principal of a tree VM is not its first agent (support)")
    return not probs, "; ".join(probs[:4]) or "support: 10 tool calls with results and 3 child lines; B2: only its own events, no [policy]"


@check("D2_trl_dry_tokenization", "done test 2 / claim 3: TRL SFTTrainer accepts trajectories.jsonl and sft.jsonl; labels != -100 only on policy turns, never the user simulator or the fixed greeting")
def d2(ctx):
    b = ctx.base()
    if shutil.which("uv") is None:
        return False, "uv not on PATH: cannot run the isolated TRL environment"
    rep_path = os.path.join(ctx.tmp, "trl_base.json")
    env = dict(os.environ, PYTHONNOUSERSITE="1")
    p = subprocess.run(UV + [os.path.join(HERE, "trl_check.py"), b["dir"], rep_path], capture_output=True, text=True,
                       timeout=1800, env=env)
    if not os.path.exists(rep_path):
        return False, f"trl_check produced no report (rc={p.returncode}): {lib.short(p.stderr[-800:], 400)}"
    rep = json.load(open(rep_path))
    probs = list(rep.get("errors") or [])
    tj = rep.get("trajectories") or {}
    if tj.get("tokenized") != len(b["items"]):
        probs.append(f"trajectories.jsonl: {tj.get('tokenized')} tokenized of {len(b['items'])} ({tj.get('error', '')[:200]})")
    policy = {}
    by_id = {it["item_id"]: it for it in b["items"]}
    for it in b["items"]:
        if it["witness"]["agrees"] is False:
            continue
        for i, m in enumerate(it["messages"]):
            if m.get("train"):
                policy[(it["item_id"], i)] = m
    got = {(x["item_id"], x["turn_index"]) for x in rep.get("sft", {}).get("examples", [])}
    if got != set(policy):
        probs.append(f"sft examples {len(got)} vs policy turns {len(policy)}: missing {sorted(set(policy) - got)[:2]} extra {sorted(got - set(policy))[:2]}")
    n_bad = 0
    for x in rep.get("sft", {}).get("examples", []):
        m = policy.get((x["item_id"], x["turn_index"]))
        if m is None:
            continue
        it = by_id[x["item_id"]]
        tr = x["trained"]
        want = (m.get("content") or "")[:40] if m.get("content") else None
        if want is None and m.get("tool_calls"):
            want = m["tool_calls"][0]["function"]["name"]
        bad = []
        if want and want not in tr:
            bad.append(f"its own text {want[:30]!r} not trained")
        if lib.GREETING in tr:
            bad.append("greeting trained")
        if "<|im_start|>system" in tr or "<policy>" in tr:
            bad.append("system prompt trained")
        own = (m.get("content") or "") + "".join(t["function"]["arguments"] for t in m.get("tool_calls") or [])
        for um in it["messages"][:x["turn_index"]]:
            snip = um.get("content")[:24] if isinstance(um.get("content"), str) and len(um["content"]) >= 24 else None
            if um["role"] in ("user", "tool") and snip and snip in tr and snip not in own:
                bad.append(f"environment text {snip!r} trained")
                break
        if x["n_trained"] == 0 or x["n_trained"] >= x["n_tokens"]:
            bad.append(f"{x['n_trained']}/{x['n_tokens']} tokens trained")
        if bad:
            n_bad += 1
            if n_bad <= 2:
                probs.append(f"{x['item_id'][:8]}@{x['turn_index']}: " + ", ".join(bad))
    if n_bad > 2:
        probs.append(f"... {n_bad} bad examples in total")
    ok = not probs
    return ok, "; ".join(probs[:4]) or f"trl {rep['versions'].get('trl')}: {tj.get('tokenized')} trajectories accepted; {len(got)} prompt-completion examples, each trains exactly its policy turn (no greeting, no simulator, no system prompt)"


@check("D2b_axolotl_shape", "done test 2: Axolotl chat_template needs roles in {system,user,assistant,tool}, a boolean per-message train field (message_field_training) and a tools column")
def d2b(ctx):
    b = ctx.base()
    probs = []
    for it in b["items"]:
        for i, m in enumerate(it["messages"]):
            if m["role"] not in lib.ROLES or not isinstance(m.get("train"), bool):
                probs.append(f"{it['run']} message {i}: role {m['role']!r} train {m.get('train')!r}")
                break
            if m.get("content") == lib.GREETING and m.get("train"):
                probs.append(f"{it['run']}: the fixed greeting is marked train")
            if m["role"] != "assistant" and m.get("train"):
                probs.append(f"{it['run']} message {i}: {m['role']} marked train")
        if not isinstance(it.get("tools"), list) or not it["tools"]:
            probs.append(f"{it['run']}: tools column empty")
        sim = ctx.env.sim(it["run"])
        n_pol = sum(1 for m in it["messages"] if m.get("train"))
        evs = ctx.env.store_events(it["vm_id"])
        pairs, _ = lib.match_witness(sim["messages"], lib.agent_calls(evs, it["principal"]))
        want = sum(1 for i, _ in pairs if sim["messages"][i]["role"] == "assistant")
        if n_pol != want:
            probs.append(f"{it['run']}: {n_pol} train turns, want {want} (assistant turns with a stored llm.call)")
    return not probs, "; ".join(probs[:4]) or f"{len(b['items'])} items: roles valid, train is a bool on every message, true exactly on the policy's assistant turns, tools present"


@check("D3_tau2_roundtrip", "done test 3: item messages (system dropped) equal the SimulationRun's ordered messages, tool-call ids, arguments and usage on five real runs")
def d3(ctx):
    b = ctx.base()
    probs = []
    for run in lib.STORE_RUNS:
        it = ctx.item(b, run)
        sim = ctx.env.sim(run)
        d = lib.first_diff(lib.tau2_norm(sim), lib.item_norm(it))
        if d:
            probs.append(f"{run}: {lib.short(d, 200)}")
            continue
        ids_w = [t["id"] for m in sim["messages"] for t in (m.get("tool_calls") or [])]
        ids_i = [t["id"] for m in it["messages"] for t in (m.get("tool_calls") or [])]
        if ids_w != ids_i:
            probs.append(f"{run}: tool-call ids differ")
    return not probs, "; ".join(probs[:3]) or f"5 runs: {sum(len(ctx.env.sim(r)['messages']) for r in lib.STORE_RUNS)} messages and every tool-call id identical in order"


@check("D3b_system_prompt", "done test 1/3: messages[0] is the system prompt τ²'s pinned llm_agent built for the run's policy, verbatim")
def d3b(ctx):
    b = ctx.base()
    probs = []
    for run in lib.STORE_RUNS:
        it = ctx.item(b, run)
        m0 = it["messages"][0]
        want = lib.system_prompt_for(ctx.env.sim(run))
        if m0["role"] != "system":
            probs.append(f"{run}: messages[0] is {m0['role']}")
        elif m0.get("content") != want:
            probs.append(f"{run}: system prompt differs ({len(m0.get('content') or '')} vs {len(want)} chars; policy text {'present' if ctx.env.sim(run)['policy'] in (m0.get('content') or '') else 'absent'})")
        elif m0.get("train") or m0.get("event_id") is not None:
            probs.append(f"{run}: system message must be train=false, event_id=null")
    return not probs, "; ".join(probs[:3]) or "5 runs: system prompt == SYSTEM_PROMPT.format(domain_policy, AGENT_INSTRUCTION) of the pinned τ²"


@check("D3c_tools_column", "statement: a tools column of JSON-schema function definitions matching the pinned τ² retail tools and every call made")
def d3c(ctx):
    b = ctx.base()
    want = {t["function"]["name"]: t for t in json.load(open(lib.TOOLS_FIX))}
    probs = []
    for it in b["items"]:
        got = {t["function"]["name"]: t for t in it["tools"]}
        if set(got) != set(want):
            probs.append(f"{it['run']}: tool names differ: missing {sorted(set(want) - set(got))[:3]} extra {sorted(set(got) - set(want))[:3]}")
            continue
        diff = [n for n in want if got[n]["function"].get("parameters") != want[n]["function"].get("parameters")]
        if diff:
            probs.append(f"{it['run']}: parameters differ for {diff[:3]}")
        probs += [f"{it['run']}: {x}" for x in lib.tools_cover_calls(it)]
    return not probs, "; ".join(probs[:3]) or "16 retail tools with the pinned parameters; every call's arguments covered"


@check("D4_manifest_deterministic", "done test 4: byte-identical trajectories/sft/manifest/stats across re-runs, after caches are removed, with another --out, and from copied roots")
def d4(ctx):
    e = ctx.env
    a = ctx.everything()
    files = ("trajectories.jsonl", "sft.jsonl", "manifest.json", "stats.json")
    probs = []
    for f in files:
        if not os.path.exists(os.path.join(a["dir"], f)):
            return False, f"--all export wrote no {f}"
    e.clear_caches()
    b = ctx.export("all2", "--all", expect_rc=1)
    for f in files:
        if open(os.path.join(a["dir"], f), "rb").read() != open(os.path.join(b["dir"], f), "rb").read():
            probs.append(f"{f} differs between two runs (caches cleared, other --out)")
    copy = os.path.join(ctx.tmp, "rootscopy")
    shutil.copytree(e.dir, copy, ignore=shutil.ignore_patterns("out", "cache", "tmp", "home"))
    env2 = e.env(AVOS_TRACE_ROOT=os.path.join(copy, "store"), AVOS_RESULTS_ROOT=os.path.join(copy, "results"),
                 AVOS_LEDGER=os.path.join(copy, "ledger.jsonl"))
    c = ctx.export("all3", "--all", env=env2, expect_rc=1)
    for f in ("trajectories.jsonl", "manifest.json"):
        if open(os.path.join(a["dir"], f), "rb").read() != open(os.path.join(c["dir"], f), "rb").read():
            probs.append(f"{f} depends on where the roots live")
    return not probs, "; ".join(probs) or f"4 files byte-identical over 3 exports ({len(a['items'])} items, {len(a['sft'])} sft examples)"


@check("D4b_manifest_hashes_counts", "done test 4 / M14 / demo counts: every item's hash and split listed, item_id is the canonical SHA-256, counts and M14 equal the oracle's")
def d4b(ctx):
    a = ctx.everything()
    m, items = a["manifest"], a["items"]
    probs = []
    if m.get("format") != "avos-retrain-manifest/1":
        probs.append(f"format {m.get('format')!r}")
    ids = [it["item_id"] for it in items]
    if [x.get("item_id") for x in m.get("items", [])] != ids:
        probs.append("manifest.items are not the file's items in order")
    bad = [(it["run"], p) for it in items for p in lib.item_problems(it)[:1]]
    if bad:
        probs.append(f"{len(bad)} items malformed, e.g. {bad[:2]}")
    import hashlib
    if m.get("dataset_sha256") != hashlib.sha256("\n".join(ids).encode()).hexdigest():
        probs.append("dataset_sha256 is not SHA-256 of the item ids joined by newline")
    if (m.get("split_file") or {}).get("sha256") != lib.sha_file(lib.SPLIT) or m["split_file"].get("path") != lib.SPLIT_REL:
        probs.append("split_file path/sha256 not recorded as given")
    for x, it in zip(m.get("items", []), items):
        if x.get("split") != it["split"] or x.get("passed") != it["outcome"]["passed"] or x.get("trace_complete") != it["trace_complete"]["status"]:
            probs.append(f"manifest entry for {it['run']} disagrees with the item")
            break
    agents = [it for it in items if it["principal"].count("/agent/") == 1 and "/sub/" not in it["principal"]]
    want = {"items": len(items), "runs_exported": len({it["run"] for it in items}),
            "passed": sum(1 for i in agents if i["outcome"]["passed"] is True),
            "failed": sum(1 for i in agents if i["outcome"]["passed"] is False),
            "unknown": sum(1 for i in agents if i["outcome"]["passed"] is None),
            "train": sum(1 for i in agents if i["split"] == "train"), "held_out": sum(1 for i in agents if i["split"] == "held_out"),
            "unsplit": sum(1 for i in agents if i["split"] is None),
            "flagged": sum(1 for i in items if i["witness"]["agrees"] is False), "sft_examples": len(a["sft"])}
    got = m.get("counts") or {}
    for k, v in want.items():
        if got.get(k) != v:
            probs.append(f"counts.{k}={got.get(k)} want {v}")
    n_runs = sum(1 for n in os.listdir(ctx.env.results) if n.startswith("run_"))
    if got.get("runs_requested") != n_runs:
        probs.append(f"counts.runs_requested={got.get('runs_requested')} want {n_runs}")
    if m.get("m14") != lib.m14_oracle(agents) and not (m.get("m14") and abs((m["m14"].get("kept_run_fraction") or 0) - (lib.m14_oracle(agents)["kept_run_fraction"] or 0)) < 1e-9
                                                        and m["m14"].get("train_runs") == lib.m14_oracle(agents)["train_runs"] and m["m14"].get("train_passed") == lib.m14_oracle(agents)["train_passed"]):
        probs.append(f"m14 {m.get('m14')} want {lib.m14_oracle(agents)}")
    text = open(os.path.join(a["dir"], "manifest.json")).read() + open(os.path.join(a["dir"], "trajectories.jsonl")).read()
    if ctx.env.dir in text:
        probs.append("absolute paths of the roots leak into manifest/items")
    if re.search(r'"(created|generated|timestamp|exported_at)"', open(os.path.join(a["dir"], "manifest.json")).read()):
        probs.append("manifest carries a wall-clock field")
    return not probs, "; ".join(probs[:4]) or f"{len(ids)} item hashes listed, counts {json.dumps({k: got.get(k) for k in ('passed', 'failed', 'unknown', 'train', 'held_out')})}, M14 {m['m14']['train_passed']}/{m['m14']['train_runs']}"


@check("L1_lineage_store_ids", "statement (POC 7 debt): every item traces to stored events by the store's own ids, never re-minted; every stored llm.call of the principal is referenced")
def l1(ctx):
    e, b = ctx.env, ctx.base()
    store_ids = e.all_store_ids()
    probs = []
    for run in lib.STORE_RUNS:
        it = ctx.item(b, run)
        evs = e.store_events(it["vm_id"])
        used = {m.get("event_id") for m in it["messages"] if m.get("event_id")}
        lin = set(it["lineage"]["events"])
        foreign = (used | lin) - set(store_ids)
        if foreign:
            probs.append(f"{run}: {len(foreign)} ids not in the store, e.g. {sorted(foreign)[:1]}")
        calls = {x["event_id"] for x in evs if x["kind"] == "llm.call" and x["principal"] == it["principal"]}
        if not calls <= used:
            probs.append(f"{run}: {len(calls - used)} llm.calls not referenced by any message")
        if not calls <= lin:
            probs.append(f"{run}: lineage.events misses llm.calls")
        spawn = next((x["event_id"] for x in evs if x["kind"] == "agent.spawn" and x["principal"] == it["principal"]), None)
        so = next((x["event_id"] for x in evs if x["kind"] == "session.open"), None)
        if it["lineage"].get("spawn") != spawn or it["lineage"].get("session_open") != so:
            probs.append(f"{run}: lineage.spawn/session_open wrong")
        rd = os.path.join(e.results, run)
        if os.path.exists(os.path.join(rd, "events.jsonl")):
            out = os.path.join(ctx.tmp, f"ingest-{run}.jsonl")
            r = lib._run([sys.executable, os.path.join(lib.TRACE_BIN, "ingest"), "--run-dir", rd, "-o", out], env={"AVOS_TRACE_CONTENT": "1"})
            if os.path.exists(out):
                reminted = {x["event_id"] for x in lib.read_jsonl(out)} - set(store_ids)
                if (used | lin) & reminted:
                    probs.append(f"{run}: uses ids re-minted by ingest --run-dir")
        for i, m in enumerate(it["messages"]):
            if m.get("usage") and not m.get("event_id"):
                probs.append(f"{run}: message {i} has usage but no event_id")
                break
            if m.get("event_id") and store_ids.get(m["event_id"]) and m.get("ts") != next(x["ts"] for x in evs if x["event_id"] == m["event_id"]):
                probs.append(f"{run}: message {i} ts is not the stored call's host ts")
                break
    return not probs, "; ".join(probs[:4]) or "5 runs: every message/lineage id exists in the store, every llm.call referenced, none re-minted"


@check("L2_copy_not_authoritative", "CONTRACT §2: the run-dir events.jsonl copy is ignored; the store decides (copy removed; stale copy from another run)")
def l2(ctx):
    e = ctx.env
    probs = []
    x = ctx.export("l2", "--run", "run_retail-0_15", "--run", "run_retail-0_94", expect_rc=0)
    it = ctx.item(x, "run_retail-0_15")
    if it["evidence"] != "store" or not it["lineage"]["events"]:
        probs.append("run_retail-0_15 (copy removed): not exported from the store")
    it94 = ctx.item(x, "run_retail-0_94")
    ids14 = {ev["event_id"] for ev in e.store_events(e.vm_of_run("run_retail-0_14"))}
    ids94 = {ev["event_id"] for ev in e.store_events("vm-01-aa0094")}
    used = {m.get("event_id") for m in it94["messages"] if m.get("event_id")} | set(it94["lineage"]["events"])
    if it94["vm_id"] != "vm-01-aa0094" or used & ids14 or not used <= ids94:
        probs.append("run_retail-0_94 (stale copy of run_retail-0_14 next to it): lineage follows the copy, not the store")
    return not probs, "; ".join(probs) or "copy removed → exported from the store; stale copy → ids are the store's for the right VM"


@check("P1_tree_per_principal", "statement: per-principal trajectories for a real POC 7 tree; parent lists children's items; store-only content is null and untrained")
def p1(ctx):
    e, t = ctx.env, ctx.tree()
    vm = lib.TREE_VM
    base = f"spiffe://avoslab.com/vm/{vm}"
    evs = e.store_events(vm)
    probs = []
    got = {it["principal"] for it in t["items"]}
    want = {base, f"{base}/agent/support"} | {f"{base}/agent/support/sub/{b}" for b in ("B1", "B2", "B3")}
    if got != want:
        return False, f"items for {sorted(p.replace(base, '') or '<vm>' for p in got)} want {sorted(p.replace(base, '') or '<vm>' for p in want)}"
    by = {it["principal"]: it for it in t["items"]}
    sup = by[f"{base}/agent/support"]
    ch = {c["principal"]: c for c in sup["children"]}
    if set(ch) != {f"{base}/agent/support/sub/{b}" for b in ("B1", "B2", "B3")}:
        probs.append("support.children are not B1,B2,B3")
    elif any(ch[p]["item_id"] != by[p]["item_id"] for p in ch):
        probs.append("children item_ids do not match the children's items")
    if any(by[p]["parent"] != f"{base}/agent/support" for p in ch) or sup["parent"] != base or by[base]["parent"] is not None:
        probs.append("parent fields wrong")
    tcs = [x for x in evs if x["kind"] == "tool.call" and x["principal"] == sup["principal"]]
    trs = {x["call_ref"]: x for x in evs if x["kind"] == "tool.result" and x["principal"] == sup["principal"]}
    calls = [m for m in sup["messages"] if m.get("tool_calls")]
    ids = [c["id"] for m in calls for c in m["tool_calls"]]
    if ids != [x["event_id"] for x in tcs]:
        probs.append(f"support: tool_calls ids {len(ids)} vs 10 gateway tool.call events in order")
    else:
        for m, x in zip(calls, tcs):
            c = m["tool_calls"][0]
            if c["function"]["name"] != x["tool"] or json.loads(c["function"]["arguments"]) != x["args"] or m.get("event_id") != x["event_id"]:
                probs.append("support: a tool call's name/arguments/event_id differ from the store")
                break
        tools_msgs = {m["tool_call_id"]: m for m in sup["messages"] if m["role"] == "tool"}
        for x in tcs:
            r = trs[x["event_id"]]
            tm = tools_msgs.get(x["event_id"])
            if tm is None or json.loads(tm["content"]) != r["result"] or tm.get("event_id") != r["event_id"]:
                probs.append("support: tool results missing or not the stored result")
                break
    for p, it in by.items():
        n_calls = sum(1 for x in evs if x["kind"] == "llm.call" and x["principal"] == p)
        n_null = sum(1 for m in it["messages"] if m["role"] == "assistant" and m.get("content") is None and not m.get("tool_calls"))
        if n_null != n_calls:
            probs.append(f"{p.replace(base, '') or '<vm>'}: {n_null} content-less assistant messages for {n_calls} llm.calls")
        if any(m.get("train") for m in it["messages"]):
            probs.append(f"{p.replace(base, '')}: train=true without content")
        if it["outcome"]["passed"] is not None or it["outcome"]["reward"] is not None:
            probs.append("outcome not null for a store-only principal")
        if it["evidence"] != "store" or it["witness"]["present"] or it["trace_complete"]["status"] != "complete":
            probs.append(f"{p.replace(base, '')}: evidence/witness/trace fields wrong ({it['evidence']}, {it['witness']['present']}, {it['trace_complete']})")
        pt = sum(x["prompt_tokens"] for x in evs if x["kind"] == "llm.call" and x["principal"] == p)
        if it["cost"]["prompt_tokens"] != pt or it["cost"]["llm_calls"] != n_calls:
            probs.append(f"{p.replace(base, '')}: cost tokens/calls differ from the store")
    if t["sft"]:
        probs.append("sft.jsonl has examples without any content")
    if t["manifest"]["counts"]["items"] != 5:
        probs.append("manifest counts wrong")
    return not probs, "; ".join(probs[:4]) or "5 items (VM, support, B1, B2, B3); support: 10 gateway tool calls + results with store ids, 3 children with item ids; nothing trained, outcome null"


@check("S1_split_user_disjoint", "claim 3 / statement: held-out tasks never tagged train; train/held_out/null from the user-disjoint split")
def s1(ctx):
    a = ctx.everything()
    probs = []
    for it in a["items"]:
        want = lib.split_of(it["task_id"]) if it["task_id"] else None
        if it["split"] != want:
            probs.append(f"{it['run']}: split {it['split']!r} want {want!r}")
        if it["task_id"] and lib.split_of(it["task_id"]) == "held_out" and it["split"] == "train":
            probs.append(f"{it['run']}: HELD-OUT TASK TAGGED TRAIN")
    it10 = ctx.item(a, "run_retail-10_0")
    if it10["split"] != "held_out" or it10["task_id"] != "10":
        probs.append(f"run_retail-10_0: split {it10['split']} task_id {it10['task_id']}")
    if ctx.item(a, "run_coding-slugify_2")["split"] is not None:
        probs.append("coding run has a τ² split")
    alt = os.path.join(ctx.tmp, "alt_split.json")
    s = json.load(open(lib.SPLIT))
    s["train"] = [x for x in s["train"] if x != "0"]
    s["held_out"] = s["held_out"] + ["0"]
    json.dump(s, open(alt, "w"))
    res = lib.retrain("export", "--out", ctx.env.out("alt"), "--split", alt, "--run", "run_retail-0_12", env=ctx.env.env())
    if res["rc"] != 0:
        probs.append(f"export with another split rc={res['rc']}")
    else:
        it = lib.read_jsonl(os.path.join(ctx.env.outs, "alt", "trajectories.jsonl"))[0]
        if it["split"] != "held_out":
            probs.append("--split FILE not honoured (task 0 held out there)")
    return not probs, "; ".join(probs[:4]) or f"{len(a['items'])} items split per the user-disjoint file; held-out task 10 never train; --split honoured"


@check("S2_outcome_cost_time", "statement: per trajectory outcome (reward, breakdown, termination), cost and tokens from the gateway, timestamps from the store")
def s2(ctx):
    e, b = ctx.env, ctx.base()
    probs = []
    for run in lib.STORE_RUNS:
        it, sim, job = ctx.item(b, run), e.sim(run), e.job(run)
        o = it["outcome"]
        ri = sim["reward_info"]
        if o["reward"] != ri["reward"] or o["passed"] != (ri["reward"] == 1.0) or o["reward_breakdown"] != ri or o["termination_reason"] != sim["termination_reason"] or o["source"] != "tau2":
            probs.append(f"{run}: outcome differs from reward_info ({o['reward']}, {o['passed']}, {o['termination_reason']})")
        evs = e.store_events(it["vm_id"])
        calls = [x for x in evs if x["kind"] == "llm.call" and x["principal"] == it["principal"]]
        pairs, _ = lib.match_witness(sim["messages"], lib.agent_calls(evs, it["principal"]))
        want = {"prompt_tokens": sum(x["prompt_tokens"] for x in calls), "completion_tokens": sum(x["completion_tokens"] for x in calls),
                "llm_calls": len(calls), "policy_calls": sum(1 for i, _ in pairs if sim["messages"][i]["role"] == "assistant"),
                "environment_calls": sum(1 for i, _ in pairs if sim["messages"][i]["role"] == "user")}
        if any(it["cost"].get(k) != v for k, v in want.items()):
            probs.append(f"{run}: cost {json.dumps({k: it['cost'].get(k) for k in want})} want {json.dumps(want)}")
        if it["cost"]["usd"] is not None and not any("cost_usd" in x for x in calls):
            probs.append(f"{run}: cost.usd invented (no priced call in the store)")
        sp = next(x for x in evs if x["kind"] == "agent.spawn" and x["principal"] == it["principal"])
        ex = next((x for x in evs if x["kind"] == "agent.exit" and x["principal"] == it["principal"]), None)
        sc = next((x for x in evs if x["kind"] == "session.close"), None)
        ts = it["timestamps"]
        if ts["start"] != sp["ts"] or (ex and ts["end"] != ex["ts"]) or (sc and ts["wall_ms"] != sc["wall_ms"]):
            probs.append(f"{run}: timestamps {ts} want start {sp['ts']} end {ex and ex['ts']} wall {sc and sc['wall_ms']}")
        if it["task"] != job["task"] or it["harness"] != "tau2" or it["vm_id"] != job["vm_id"] or it["host"] != evs[0]["host"]:
            probs.append(f"{run}: task/harness/vm_id/host wrong")
        if it["witness"]["sha256"] != lib.sha_file(os.path.join(e.results, run, "result.json")) or it["witness"]["agrees"] is not True:
            probs.append(f"{run}: witness sha256/agrees wrong")
    return not probs, "; ".join(probs[:4]) or "5 runs: reward/breakdown/termination from the witness, tokens and calls (policy vs simulator) from the gateway rows, timestamps from spawn/exit/session.close"


@check("S3_m8_carried", "M8: trace_complete equals avos completeness; incomplete/unverified runs are never presented as complete")
def s3(ctx):
    e, a = ctx.env, ctx.everything()
    probs = []
    for run in lib.STORE_RUNS + ["run_retail-0_92", "run_retail-0_93", "run_retail-10_0"]:
        it = ctx.item(a, run)
        c = json.loads(lib.avos("completeness", "--run", run, env={"AVOS_TRACE_ROOT": e.store})["out"])
        if it["trace_complete"]["status"] != c["status"] or it["trace_complete"]["ratio"] != c["ratio"]:
            probs.append(f"{run}: item says {it['trace_complete']} avos says {c['status']}/{c['ratio']}")
    if json.loads(lib.avos("completeness", "--run", "run_retail-0_92", env={"AVOS_TRACE_ROOT": e.store})["out"])["status"] != "incomplete":
        probs.append("fixture: run_retail-0_92 should read incomplete (counters truncated)")
    for run in ("run_retail-0_10", "run_retail-0_9", "run_coding-slugify_2"):
        it = ctx.item(a, run)
        if it["trace_complete"] != {"status": "unverified", "ratio": None} or it["evidence"] != "ledger":
            probs.append(f"{run}: legacy run reads {it['trace_complete']} evidence {it['evidence']}")
    for x in a["manifest"]["items"]:
        it = next(i for i in a["items"] if i["item_id"] == x["item_id"])
        if x["trace_complete"] != it["trace_complete"]["status"]:
            probs.append("manifest trace_complete differs from the item")
            break
    return not probs, "; ".join(probs[:4]) or "store runs match avos (complete / incomplete / unverified); legacy runs unverified"


@check("V1_stats_match_oracle", "statement: tool-at-turn-index, turn-count histogram, tool-call share, exact duplicates equal values computed independently")
def v1(ctx):
    a = ctx.everything()
    want = lib.stats_oracle(a["items"])
    probs = lib.stats_diff(want, a["stats"])
    r = lib.retrain("stats", "--dataset", a["dir"], env=ctx.env.env())
    if r["rc"] != 0:
        probs.append(f"stats --dataset rc={r['rc']}")
    else:
        try:
            if json.loads(r["out"]) != a["stats"]:
                probs.append("stats --dataset differs from stats.json")
        except ValueError:
            probs.append("stats --dataset is not JSON")
    return not probs, "; ".join(probs[:4]) or f"{want['items']} items: hist {want['policy_turns_hist']}, tool-call share {want['tool_call_share']:.3f}, top (tool, index) share {want['shortcut']['share']:.2f}, {want['exact_duplicates']['items']} duplicates"


@check("V2_planted_shortcut_flagged", "statement (published failure): 98 % of items calling one tool at one index must be flagged; planted duplicates found")
def v2(ctx):
    d = os.path.join(ctx.tmp, "planted")
    items = lib.planted_dataset(d)
    r = lib.retrain("stats", "--dataset", d, env=ctx.env.env())
    if r["rc"] != 0:
        return False, f"stats --dataset rc={r['rc']} {lib.short(r['err'])}"
    got = json.loads(r["out"])
    want = lib.stats_oracle(items)
    probs = lib.stats_diff(want, got)
    sc = got.get("shortcut") or {}
    if not (sc.get("tool") == "find_user_id_by_email" and str(sc.get("index")) == "3" and sc.get("flag") is True and abs(sc.get("share", 0) - 0.98) < 1e-9):
        probs.append(f"shortcut not flagged as find_user_id_by_email@3 share 0.98: {sc}")
    dup = (got.get("exact_duplicates") or {}).get("groups") or []
    if dup != [sorted(items[i]["item_id"] for i in (0, 1, 2))]:
        probs.append(f"duplicates: {dup and [len(g) for g in dup]} want one group of 3")
    d2 = os.path.join(ctx.tmp, "diverse")
    items2 = lib.planted_dataset(d2, shortcut_share=0.3, seed=5)
    r2 = lib.retrain("stats", "--dataset", d2, env=ctx.env.env())
    sc2 = (json.loads(r2["out"]) if r2["rc"] == 0 else {}).get("shortcut") or {}
    if sc2.get("flag") is not False:
        probs.append(f"diverse set flagged: {sc2}")
    return not probs, "; ".join(probs[:3]) or "planted 98 % auth-at-index-3 flagged (share 0.98), 3 duplicates grouped; 30 % set not flagged"


@check("V3_duplicate_real_run", "statement: exact-duplicate detection on a real conversation exported twice under different run ids")
def v3(ctx):
    a = ctx.everything()
    i1, i2 = ctx.item(a, "run_retail-0_13")["item_id"], ctx.item(a, "run_retail-0_80")["item_id"]
    groups = (a["stats"].get("exact_duplicates") or {}).get("groups") or []
    ok = any(i1 in g and i2 in g for g in groups) and i1 != i2
    return ok, "run_retail-0_13 and its clone run_retail-0_80 share a duplicate group (distinct item ids)" if ok else f"not grouped: {[len(g) for g in groups]}"


@check("H2_legacy_runs", "statement: POC 1–5 runs (ledger rows + result.json) export as evidence=ledger, unverified, with the witness's conversation; ids never claimed to be the store's")
def h2(ctx):
    e, a = ctx.env, ctx.everything()
    store_ids = set(e.all_store_ids())
    probs = []
    for run in ("run_retail-0_10", "run_retail-0_9"):
        it = ctx.item(a, run)
        sim = e.sim(run)
        if it["evidence"] != "ledger" or it["trace_complete"]["status"] != "unverified":
            probs.append(f"{run}: evidence {it['evidence']} trace {it['trace_complete']}")
        d = lib.first_diff(lib.tau2_norm(sim), lib.item_norm(it))
        if d:
            probs.append(f"{run}: messages differ: {lib.short(d, 120)}")
        if set(it["lineage"]["events"]) & store_ids:
            probs.append(f"{run}: claims store ids")
        if it["outcome"]["passed"] != lib.passed_of_sim(sim):
            probs.append(f"{run}: passed {it['outcome']['passed']}")
        if it["messages"][0]["role"] != "system":
            probs.append(f"{run}: no system prompt")
    return not probs, "; ".join(probs[:3]) or "run_retail-0_10 and run_retail-0_9: ledger evidence, unverified, conversation and outcome intact"


@check("H3_reward_info_null", "statement: 4 real runs have reward_info null; outcome unknown, never passed/failed, counted in M14's denominator only")
def h3(ctx):
    a = ctx.everything()
    it = ctx.item(a, "run_retail-0_90")
    probs = []
    o = it["outcome"]
    if o["reward"] is not None or o["passed"] is not None or o["reward_breakdown"] is not None:
        probs.append(f"outcome {json.dumps(o)[:120]}")
    m = a["manifest"]
    if m["counts"]["unknown"] < 1:
        probs.append("counts.unknown does not count it")
    agents = [i for i in a["items"] if i["principal"].endswith("/agent/tau2") or i["harness"] != "tau2"]
    if m["m14"]["train_runs"] != lib.m14_oracle(agents)["train_runs"]:
        probs.append("m14.train_runs excludes the unknown run")
    r = ctx.view("--run", "run_retail-0_90")
    if r["rc"] != 0 or "reward=unknown" not in r["out"] or "unknown" not in r["out"]:
        probs.append("view does not say reward=unknown")
    return not probs, "; ".join(probs) or "reward/passed/breakdown null, counted as unknown, in the M14 denominator, viewer says reward=unknown"


@check("H4_witness_tampered", "docs/trace.md §4: a transcript that disagrees with the host rows is reported, never silently merged: flagged, no training examples, exit 1, banner in the viewer")
def h4(ctx):
    e = ctx.env
    x = ctx.export("h4", "--run", "run_retail-0_91", expect_rc=1)
    it = ctx.item(x, "run_retail-0_91")
    probs = []
    if it["witness"]["agrees"] is not False or not it["witness"]["problems"]:
        probs.append(f"witness {it['witness']}")
    if x["sft"]:
        probs.append(f"{len(x['sft'])} sft examples produced from a disputed transcript")
    if x["manifest"]["counts"].get("flagged") != 1 or x["manifest"]["items"][0].get("sft_examples") != 0 or x["manifest"]["items"][0].get("witness_agrees") is not False:
        probs.append("manifest does not flag it")
    if not it["messages"]:
        probs.append("item has no messages (it must still be inspectable)")
    r = ctx.view("--run", "run_retail-0_91")
    if r["rc"] != 0 or "WITNESS DISAGREES" not in r["out"]:
        probs.append("viewer shows no WITNESS DISAGREES banner")
    elif r["out"].index("WITNESS DISAGREES") > r["out"].index("#0 "):
        probs.append("banner after the messages")
    a = ctx.everything()
    if ctx.item(a, "run_retail-0_91")["witness"]["agrees"] is not False:
        probs.append("--all export merges it silently")
    return not probs, "; ".join(probs) or "agrees=false with problems, 0 sft examples, flagged=1, exit 1, banner before the messages"


@check("H5_unknown_run", "CONTRACT §6/§10: an unknown run id is skipped with reason unknown_run (exit 1), the viewer exits 1")
def h5(ctx):
    x = ctx.export("h5", "--run", "run_retail-99_9", "--run", "run_retail-0_12")
    probs = []
    if x["res"]["rc"] != 1:
        probs.append(f"rc={x['res']['rc']}")
    if x["manifest"] is None:
        return False, f"no manifest written (rc={x['res']['rc']} {lib.short(x['res']['err'])})"
    sk = {s["run"]: s["reason"] for s in x["manifest"].get("skipped", [])}
    if sk.get("run_retail-99_9") != "unknown_run":
        probs.append(f"skipped={sk}")
    if [i["run"] for i in x["items"]] != ["run_retail-0_12"]:
        probs.append("the known run was not exported alongside")
    return not probs, "; ".join(probs) or "skipped with reason unknown_run, exit 1, the known run still exported"


@check("H6_witness_only", "CONTRACT §3: a run with no host rows is skipped (no_host_rows) unless --include-witness-only, then evidence=witness, unverified, no event ids")
def h6(ctx):
    x = ctx.export("h6a", "--run", "run_retail-8_1")
    probs = []
    if x["res"]["rc"] != 1 or x["items"] or {s["run"]: s["reason"] for s in (x["manifest"] or {}).get("skipped", [])}.get("run_retail-8_1") != "no_host_rows":
        probs.append(f"default: rc={x['res']['rc']} items={len(x['items'])} skipped={(x['manifest'] or {}).get('skipped')}")
    y = ctx.export("h6b", "--run", "run_retail-8_1", "--include-witness-only", expect_rc=0)
    if not y["items"]:
        probs.append("--include-witness-only exported nothing")
    else:
        it = y["items"][0]
        sim = ctx.env.sim("run_retail-8_1")
        if it["evidence"] != "witness" or it["trace_complete"]["status"] != "unverified" or it["lineage"]["events"] or any(m.get("event_id") for m in it["messages"]):
            probs.append(f"evidence {it['evidence']} trace {it['trace_complete']} events {len(it['lineage']['events'])}")
        if lib.first_diff(lib.tau2_norm(sim), lib.item_norm(it)) or it["outcome"]["passed"] is not True:
            probs.append("conversation/outcome not the witness's")
        if any(m.get("train") for m in it["messages"]):
            probs.append("train=true without any stored call vouching for it")
    return not probs, "; ".join(probs) or "skipped no_host_rows by default; with the flag: evidence=witness, unverified, no ids, nothing trained"


@check("H7_mini_swe_agent", "statement: coding runs export with outcome from tests_passed and the harness trajectory; call-count disagreement is reported")
def h7(ctx):
    e, a = ctx.env, ctx.everything()
    it = ctx.item(a, "run_coding-slugify_2")
    w = e.witness("run_coding-slugify_2")
    probs = []
    if it["harness"] != "mini-swe-agent" or it["evidence"] != "ledger" or it["outcome"]["passed"] is not True or it["outcome"]["source"] != "mini-swe-agent":
        probs.append(f"harness {it['harness']} evidence {it['evidence']} passed {it['outcome']['passed']} source {it['outcome']['source']}")
    tm = [m for m in w["trajectory"]["messages"] if m.get("role") in lib.CHAT_ROLES]
    got = [(m["role"], m.get("content")) for m in it["messages"]]
    want = [(m["role"], m.get("content")) for m in tm]
    if got != want:
        probs.append(f"messages differ from trajectory.messages ({len(got)} vs {len(want)})")
    n_ast = sum(1 for m in tm if m["role"] == "assistant")
    n_calls = sum(1 for l in open(e.ledger) if "vm-01-fc4298" in l and '"kind":"llm.call"' in l.replace(" ", ""))
    if n_calls == 0:
        n_calls = sum(1 for l in open(e.ledger) if "vm-01-fc4298" in l and json.loads(l).get("kind") == "llm.call")
    agrees = n_ast == n_calls
    if it["witness"]["agrees"] is not agrees:
        probs.append(f"witness.agrees={it['witness']['agrees']} but {n_ast} assistant messages vs {n_calls} ledger llm.calls")
    if not agrees and any(m.get("train") for m in it["messages"]):
        probs.append("trains on turns the host cannot match")
    return not probs, "; ".join(probs[:3]) or f"ledger evidence, tests_passed → passed, {len(tm)} trajectory messages verbatim, {n_ast} assistant vs {n_calls} calls → agrees={agrees}"


def _native(ctx):
    return ctx.export("native", *sum((["--run", r] for r in lib.NATIVE_RUNS), []), expect_rc=0)


@check("A1_native_join_by_response_id", "trace/CONTRACT.md §5 / CONTRACT §4: on native runs every model-produced message names the call whose response_id is its raw_data.id; join=response_id, all seven checks listed, agrees")
def a1(ctx):
    e, x = ctx.env, _native(ctx)
    probs = []
    for run in lib.NATIVE_RUNS:
        it, res = ctx.item(x, run), e.witness(run)
        sim = res["simulations"][0]
        calls = lib.agent_calls(e.store_events(it["vm_id"]), it["principal"])
        by_id = {c["response_id"]: c["event_id"] for c in calls}
        w = it["witness"]
        if w["agrees"] is not True or w.get("join") != "response_id" or set(w.get("checked") or []) != {"tokens", "role", "system", "tools", "output", "messages"}:
            probs.append(f"{run}: witness {json.dumps({k: w.get(k) for k in ('agrees', 'join', 'checked')})}")
        wm = [m for m in sim["messages"] if m.get("usage")]
        im = [m for m in it["messages"] if m.get("usage")]
        if len(wm) != len(im) or any(by_id.get((a.get("raw_data") or {}).get("id")) != b.get("event_id") for a, b in zip(wm, im)):
            probs.append(f"{run}: message event_ids are not the calls joined by response_id")
        pairs, oprobs, join, other = lib.witness_verdict(res, calls, it["tools"], it["messages"][0]["content"])
        if oprobs:
            probs.append(f"{run}: suite oracle disagrees: {oprobs[0]}")
        if not any(m.get("train") for m in it["messages"]) or not [s for s in x["sft"] if s["item_id"] == it["item_id"]]:
            probs.append(f"{run}: nothing trained on a clean native run")
    return not probs, "; ".join(probs[:3]) or f"4 native runs: joined by response_id, 7 checks, {len(x['sft'])} sft rows"


def _tamper_case(ctx, name, mutate, src="run_retail-0_16", tools_env=None):
    e = ctx.env
    run = f"run_retail-0_{name}"
    env = e.env(**(tools_env or {}))
    x = ctx.export(f"tamper-{name}", "--run", run, env=env)
    if x["manifest"] is None:
        return [f"{name}: rc={x['res']['rc']} no manifest ({lib.short(x['res']['err'], 120)})"]
    it = ctx.item(x, run)
    probs = []
    if x["res"]["rc"] != 1:
        probs.append(f"{name}: rc={x['res']['rc']} (want 1)")
    if it["witness"]["agrees"] is not False or not it["witness"]["problems"]:
        probs.append(f"{name}: agrees={it['witness']['agrees']} problems={it['witness']['problems'][:1]}")
    if any(m.get("train") for m in it["messages"]) or [s for s in x["sft"] if s["item_id"] == it["item_id"]]:
        probs.append(f"{name}: still trained")
    if not it["messages"]:
        probs.append(f"{name}: item not inspectable")
    return probs


@check("A2_tampered_text_same_tokens", "class A: witness reply text, a simulator turn and a tool result altered with token counts unchanged → agrees=false, nothing trained")
def a2(ctx):
    probs = _tamper_case(ctx, "201", lib.mut_reply_text) + _tamper_case(ctx, "202", lib.mut_user_text) + _tamper_case(ctx, "203", lib.mut_tool_result)
    return not probs, "; ".join(probs[:3]) or "reply text, simulator text, tool result: each flagged by the host's output/messages hashes"


@check("A3_tampered_tool_call_same_tokens", "class A: a tool call's arguments or id altered (token counts unchanged) → agrees=false, nothing trained")
def a3(ctx):
    probs = _tamper_case(ctx, "204", lib.mut_tool_args) + _tamper_case(ctx, "205", lib.mut_tool_id)
    return not probs, "; ".join(probs[:3]) or "arguments and id: each flagged"


@check("A4_tampered_prompt_and_tools", "class A: the policy text (system prompt) or the tool schemas differ from what the host hashed → agrees=false, nothing trained")
def a4(ctx):
    alt = os.path.join(ctx.tmp, "alt_tools.json")
    tools = json.load(open(lib.TOOLS_FIX))
    tools[3]["function"]["description"] += " (edited)"
    json.dump(tools, open(alt, "w"))
    probs = _tamper_case(ctx, "206", lib.mut_policy) + _tamper_case(ctx, "207", None, tools_env={"AVOS_TOOL_SCHEMAS": alt})
    return not probs, "; ".join(probs[:3]) or "edited policy and edited tool schema: each flagged by system_sha256 / tools_sha256"


@check("A5_join_not_by_order", "class A: two assistant replies with swapped raw_data.id (same order, same texts) → the join by response_id fails; never silently joined by position")
def a5(ctx):
    probs = _tamper_case(ctx, "208", lib.mut_swap_raw_ids)
    return not probs, "; ".join(probs[:3]) or "swapped response ids flagged"


@check("J1_judge_call_other_model", "CONTRACT §4 (τ² NL-assertion judge): an extra call to another model under the agent's identity is listed in other_calls, kept in cost and lineage, and the run still agrees and trains")
def j1(ctx):
    e = ctx.env
    run, jid = "run_retail-0_209", ctx.judge_id
    c = json.loads(lib.avos("completeness", "--run", run, env={"AVOS_TRACE_ROOT": e.store})["out"])
    x = ctx.export("judge-other", "--run", run, expect_rc=0)
    it = ctx.item(x, run)
    probs = []
    if c["status"] != "complete":
        probs.append(f"fixture: avos says {c['status']}")
    if it["witness"]["agrees"] is not True or not any(m.get("train") for m in it["messages"]):
        probs.append(f"agrees={it['witness']['agrees']} problems={it['witness']['problems'][:1]}")
    oc = it["witness"].get("other_calls") or []
    if not all(isinstance(o, dict) for o in oc) or [o.get("event_id") for o in oc] != [jid] or oc[0].get("model") != "deepseek-ai/DeepSeek-V4-Flash-0731":
        probs.append(f"other_calls must be [{{event_id, model, prompt_tokens, completion_tokens}}], got {lib.short(json.dumps(oc), 120)}")
    if jid not in it["lineage"]["events"] or it["cost"]["llm_calls"] != 15 or it["cost"]["prompt_tokens"] < 1234:
        probs.append("judge call missing from lineage or cost")
    if any(m.get("event_id") == jid for m in it["messages"]):
        probs.append("judge call merged as a message")
    return not probs, "; ".join(probs[:3]) or "judge call on another model: other_calls, lineage and cost carry it; run agrees and trains"


@check("J2_leftover_call_same_model", "CONTRACT §4: an extra call to the agent's own model that no witness message explains → flagged, nothing trained")
def j2(ctx):
    run = "run_retail-0_210"
    x = ctx.export("judge-same", "--run", run)
    it = ctx.item(x, run) if x["items"] else None
    ok = it is not None and x["res"]["rc"] == 1 and it["witness"]["agrees"] is False and not any(m.get("train") for m in it["messages"]) and not x["sft"]
    return ok, "leftover call on the agent's model flagged, nothing trained" if ok else f"rc={x['res']['rc']} witness={it and it['witness']['agrees']} sft={len(x['sft'])}"


@check("X1_witness_replaced_same_size_mtime", "CONTRACT §11: after a warm export the witness changes content with the same size, mtime and inode; the next export and view follow the file")
def x1(ctx):
    e = ctx.env
    ctx.base()
    run, path = "run_retail-0_13", os.path.join(e.results, "run_retail-0_13", "result.json")
    orig, st = open(path, "rb").read(), os.stat(path)
    try:
        lib.same_size_replace(path, "19122", "19123")
        y = lib.retrain("export", "--out", e.out("x1"), "--split", lib.SPLIT_REL, "--run", run, env=e.env())
        probs = []
        if y["rc"] != 0:
            probs.append(f"rc={y['rc']} {lib.short(y['err'])}")
        else:
            it = lib.read_jsonl(os.path.join(e.outs, "x1", "trajectories.jsonl"))[0]
            text = json.dumps(it, ensure_ascii=False)
            if "19123" not in text or "19122" in text:
                probs.append("export still carries the old content")
            if it["item_id"] == ctx.item(ctx.base(), run)["item_id"]:
                probs.append("item_id unchanged although the witness changed")
        v = ctx.view("--run", run)
        if v["rc"] == 0 and ("19123" not in v["out"] or "19122" in v["out"]):
            probs.append("view shows the old content")
        elif v["rc"] != 0:
            probs.append(f"view rc={v['rc']}")
    finally:
        open(path, "wb").write(orig)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return not probs, "; ".join(probs) or "export and view follow the new bytes (zip 19122→19123), item_id changes"


@check("X2_store_edited_in_place", "CONTRACT §11: a store row edited in place (same size, mtime) breaks the chain and the witness match; never the old answer with exit 0")
def x2(ctx):
    e = ctx.env
    run = "run_retail-0_14"
    before = ctx.item(ctx.base(), run)
    path = os.path.join(e.store, "vm", e.vm_of_run(run), "gateway.jsonl")
    orig, st = open(path, "rb").read(), os.stat(path)
    first = next(x for x in e.store_events(e.vm_of_run(run)) if x["kind"] == "llm.call")
    old = f'"prompt_tokens": {first["prompt_tokens"]}'
    new = f'"prompt_tokens": {first["prompt_tokens"] + 1}'
    try:
        lib.same_size_replace(path, old, new, count=1)
        y = lib.retrain("export", "--out", e.out("x2"), "--split", lib.SPLIT_REL, "--run", run, env=e.env())
        out = os.path.join(e.outs, "x2", "trajectories.jsonl")
        after = lib.read_jsonl(out)[0] if os.path.exists(out) else None
        stale = y["rc"] == 0 and after is not None and after["item_id"] == before["item_id"]
        silent = y["rc"] == 0 and after is not None and after["witness"]["agrees"] is not False
        if not lib.no_traceback(y):
            return False, "traceback"
    finally:
        open(path, "wb").write(orig)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    ok = not stale and not silent
    return ok, ("stale answer served with exit 0" if stale else "edited row merged silently with exit 0" if silent else
                f"rc={y['rc']}: {'witness flagged' if after and after['witness']['agrees'] is False else 'refused'} ({lib.short(y['err'], 100)})")


@check("X3_cache_tampered", "CONTRACT §11: every cache the tool wrote (cache dir, TMPDIR, HOME) truncated, bit-flipped or swapped; the next export is identical or refused, never wrong")
def x3(ctx):
    e = ctx.env
    clean = ctx.base()
    ref = {f: open(os.path.join(clean["dir"], f), "rb").read() for f in ("trajectories.jsonl", "manifest.json")}
    probs, n_files = [], 0
    rnd = random.Random(1)
    for mode in ("truncate", "flip", "swap", "delete"):
        lib.retrain("export", "--out", e.out("warm"), "--split", lib.SPLIT_REL, *sum((["--run", r] for r in lib.STORE_RUNS), []), env=e.env())
        if mode == "delete":
            e.clear_caches()
        else:
            n_files += sum(lib.tamper_dir(d, mode, rnd) for d in e.cache_dirs())
        out = e.out(f"x3-{mode}")
        y = lib.retrain("export", "--out", out, "--split", lib.SPLIT_REL, *sum((["--run", r] for r in lib.STORE_RUNS), []), env=e.env())
        if not lib.no_traceback(y):
            probs.append(f"{mode}: traceback")
        elif y["rc"] == 0:
            for f, want in ref.items():
                if open(os.path.join(out, f), "rb").read() != want:
                    probs.append(f"{mode}: {f} differs from the clean export")
                    break
        elif os.path.exists(os.path.join(out, "manifest.json")) and open(os.path.join(out, "manifest.json"), "rb").read() != ref["manifest.json"]:
            probs.append(f"{mode}: rc={y['rc']} but a different manifest was written")
    e.clear_caches()
    return not probs, "; ".join(probs[:3]) or f"{n_files} cached files damaged over 3 modes + deletion: every export identical to the clean one or refused"


@check("R1_redaction_hook", "statement: a content redaction hook applied to every content field of trajectories, sft and the viewer; recorded in the manifest")
def r1(ctx):
    e = ctx.env
    x = ctx.export("red", *sum((["--run", r] for r in lib.STORE_RUNS), []), "--redact", "redact_hook:digits", expect_rc=0)
    probs = []
    for it in x["items"]:
        for where, s in lib.content_strings(it):
            if lib.DIGIT.search(s):
                probs.append(f"{it['run']} {where} keeps a digit: {lib.short(s, 50)!r}")
                break
        if it.get("redact") != "redact_hook:digits":
            probs.append(f"{it['run']}: item.redact={it.get('redact')!r}")
    for ex in x["sft"]:
        bad = [w for w, s in lib.content_strings(ex) if lib.DIGIT.search(s)]
        if bad:
            probs.append(f"sft example keeps digits in {bad[0]}")
            break
    if x["manifest"].get("redact") != "redact_hook:digits":
        probs.append("manifest.redact not recorded")
    base = ctx.base()
    if any(a["item_id"] == b["item_id"] for a in x["items"] for b in base["items"]):
        probs.append("redacted item has the same item_id as the unredacted one")
    sim = e.sim("run_retail-0_13")
    v = ctx.view("--run", "run_retail-0_13", "--redact", "redact_hook:digits")
    if v["rc"] != 0:
        probs.append(f"view --redact rc={v['rc']}")
    else:
        originals = [m["content"] for m in sim["messages"] if isinstance(m.get("content"), str) and lib.DIGIT.search(m["content"]) and len(m["content"]) > 12]
        leaked = [o for o in originals if o in v["out"]]
        if leaked:
            probs.append(f"view --redact shows unredacted content: {lib.short(leaked[0], 50)!r}")
        import redact_hook
        if not any(redact_hook.digits(o, "content") in v["out"] for o in originals[:5]):
            probs.append("view --redact does not show the redacted content")
    return not probs, "; ".join(probs[:3]) or f"{len(x['items'])} items + {len(x['sft'])} sft examples + view: no digit left in any content field; ids differ; hook recorded"


@check("SC1_export_700_runs", "CONTRACT §12: export of 700 synthetic runs (--runs-file) (POC 11/12 dataset size) ≤ 145 s, peak RSS ≤ 512 MiB", scale=True)
def sc1(ctx):
    e = lib.Env(ctx.tmp, "scale")
    runs = lib.gen_scale(e, TH["n_scale_runs"], 0)
    rf = os.path.join(e.dir, "runs.txt")
    open(rf, "w").write("\n".join(runs) + "\n")
    r = lib.retrain("export", "--out", e.out("big"), "--split", lib.SPLIT_REL, "--runs-file", rf, env=e.env(), rss=True, timeout=1800)
    bar = TH["export_s_per_run"] * TH["n_scale_runs"] + TH["export_s_fixed"]
    m = json.load(open(os.path.join(e.outs, "big", "manifest.json"))) if os.path.exists(os.path.join(e.outs, "big", "manifest.json")) else None
    n_items = (m or {}).get("counts", {}).get("items")
    ok = r["rc"] == 0 and r["secs"] <= bar and (r["maxrss_mib"] or 0) <= TH["rss_mib"] and n_items == len(runs)
    return ok, f"rc={r['rc']} {r['secs']:.1f}s (bar {bar:.0f}s), peak RSS {r['maxrss_mib'] and round(r['maxrss_mib'])} MiB (bar {TH['rss_mib']}), items {n_items}/{len(runs)}"


@check("SC2_store_size_independence", "CONTRACT §12 / M9: the same 20 runs exported from a store with 2,000 unrelated VMs take ≤ 3× the small store; view ≤ 2 s there", scale=True)
def sc2(ctx):
    small, big = lib.Env(ctx.tmp, "small"), lib.Env(ctx.tmp, "big")
    runs = lib.gen_scale(small, TH["n_small_runs"], 0)
    lib.gen_scale(big, TH["n_small_runs"], TH["n_noise_vms"])
    args = sum((["--run", r] for r in runs), [])

    def t(env, key):
        best = None
        for i in range(2):
            r = lib.retrain("export", "--out", env.out(f"{key}{i}"), "--split", lib.SPLIT_REL, *args, env=env.env(), timeout=1800)
            if r["rc"] != 0:
                raise AssertionError(f"export rc={r['rc']} {lib.short(r['err'])}")
            best = r["secs"] if best is None else min(best, r["secs"])
        return best
    ts, tb = t(small, "s"), t(big, "b")
    v = lib.retrain("view", "--run", runs[0], env=big.env())
    v = lib.retrain("view", "--run", runs[0], env=big.env())
    ratio = tb / max(ts, 0.05)
    ok = ratio <= TH["scale_factor"] and v["rc"] == 0 and v["secs"] <= TH["view_s"]
    return ok, f"20 runs: small store {ts:.2f}s, +2000 VMs {tb:.2f}s (×{ratio:.2f}, bar ×{TH['scale_factor']}); view {v['secs']:.2f}s (bar {TH['view_s']}s)"


@check("Z_roots_untouched", "collect/CONTRACT.md §2 / CONTRACT §6: after every command the store, results and ledger are byte-identical; nothing was written under them")
def z(ctx):
    e = ctx.env
    now = e.roots_snapshot()
    probs = []
    for k in ("store", "results"):
        a, b = ctx.snap0[k], now[k]
        if a != b:
            changed = sorted(set(a) ^ set(b) | {p for p in a if p in b and a[p] != b[p]})
            probs.append(f"{k}: {len(changed)} files differ, e.g. {changed[:3]}")
    if ctx.snap0["ledger"] != now["ledger"]:
        probs.append("ledger changed")
    return not probs, "; ".join(probs) or f"{len(now['store'])} store files, {len(now['results'])} result files, ledger: unchanged"


def _host_retrain_present(ctx):
    p = ctx.ssh(f"test -f {HOST_REPO}/retrain/bin/retrain && echo yes")
    if p.returncode != 0 and not p.stdout:
        raise AssertionError(f"ssh {HOST} failed: {lib.short(p.stderr, 200)}")
    if "yes" not in p.stdout:
        raise lib.NotImplementedCLI(f"{HOST}:{HOST_REPO}/retrain/bin/retrain (push the repo first)")


def _hexport(ctx, out, *args):
    envs = (f"AVOS_RETRAIN_CACHE={HOST_TMP}/cache TMPDIR={HOST_TMP}/tmp HOME={HOST_TMP}/home "
            f"AVOS_TOOL_SCHEMAS={HOST_TMP}/tau2_retail_tools.json")
    cmd = (f"mkdir -p {HOST_TMP}/cache {HOST_TMP}/tmp {HOST_TMP}/home && cd {HOST_REPO} && "
           f"sudo env {envs} python3 retrain/bin/retrain export --out {out} --split {lib.SPLIT_REL} {' '.join(args)}; echo rc=$?")
    t0 = time.perf_counter()
    p = ctx.ssh(cmd, timeout=1800)
    rc = int(re.findall(r"rc=(\d+)", p.stdout)[-1]) if re.findall(r"rc=(\d+)", p.stdout) else -1
    return rc, p, time.perf_counter() - t0


def _hread(ctx, path):
    return ctx.ssh(f"sudo cat {path}", timeout=300).stdout


@check("HOST1_live_export_lineage_m8", "host: export the five POC 7 runs from the live store; ids exist there; M8 equals avos; the store is only appended to", host=True)
def host1(ctx):
    _host_retrain_present(ctx)
    ctx.ssh(f"mkdir -p {HOST_TMP}", stdin=None)
    ctx.ssh(f"cat > {HOST_TMP}/tau2_retail_tools.json", stdin=open(lib.TOOLS_FIX).read())
    before = ctx.ssh("sudo find /var/lib/avos/trace -type f -printf '%s %p\\n'").stdout.split("\n")
    sizes = {l.split(" ", 1)[1]: int(l.split(" ", 1)[0]) for l in before if l.strip()}
    heads = {}
    for path, n in list(sizes.items()):
        heads[path] = ctx.ssh(f"sudo head -c {n} '{path}' | sha256sum").stdout.split()[0]
    rc, p, secs = _hexport(ctx, f"{HOST_TMP}/ds5", *sum((["--run", r] for r in lib.STORE_RUNS), []))
    probs = []
    if rc != 0:
        probs.append(f"rc={rc} {lib.short(p.stdout + p.stderr, 300)}")
        return False, "; ".join(probs)
    items = [json.loads(l) for l in _hread(ctx, f"{HOST_TMP}/ds5/trajectories.jsonl").splitlines() if l.strip()]
    ctx.cache["host_items"] = items
    for it in items:
        vm = it["vm_id"]
        live = {}
        for n in ("gateway", "job", "registrar", "vm"):
            for l in _hread(ctx, f"/var/lib/avos/trace/vm/{vm}/{n}.jsonl").splitlines():
                if l.strip():
                    ev = json.loads(l)
                    live[ev["event_id"]] = ev
        used = {m.get("event_id") for m in it["messages"] if m.get("event_id")} | set(it["lineage"]["events"])
        if not used or not used <= set(live):
            probs.append(f"{it['run']}: ids not in the live store ({len(used - set(live))})")
        calls = {k for k, ev in live.items() if ev["kind"] == "llm.call" and ev["principal"] == it["principal"]}
        if not calls <= used:
            probs.append(f"{it['run']}: {len(calls - used)} live llm.calls unreferenced")
        c = json.loads(ctx.ssh(f"cd {HOST_REPO} && sudo python3 collect/bin/avos completeness --run {it['run']}").stdout)
        if it["trace_complete"]["status"] != c["status"] or it["trace_complete"]["ratio"] != c["ratio"]:
            probs.append(f"{it['run']}: trace_complete {it['trace_complete']} vs avos {c['status']}")
        if it["witness"]["agrees"] is not True:
            probs.append(f"{it['run']}: witness.agrees={it['witness']['agrees']}")
    after = ctx.ssh("sudo find /var/lib/avos/trace -type f -printf '%s %p\\n'").stdout.split("\n")
    sizes2 = {l.split(" ", 1)[1]: int(l.split(" ", 1)[0]) for l in after if l.strip()}
    for path, n in sizes.items():
        if sizes2.get(path, -1) < n or ctx.ssh(f"sudo head -c {n} '{path}' | sha256sum").stdout.split()[0] != heads[path]:
            probs.append(f"store file rewritten: {path}")
            break
    return not probs, "; ".join(probs[:4]) or f"{len(items)} items in {secs:.1f}s: every id in the live store, every llm.call referenced, M8 = avos, {len(sizes)} store files untouched"


@check("HOST2_tau2_roundtrip_pinned", "done test 3 on the pinned τ² (~/tau2-bench/.venv): SimulationRun → export → SimulationRun, identical ordered messages and tool-call ids", host=True)
def host2(ctx):
    _host_retrain_present(ctx)
    if "host_items" not in ctx.cache:
        raise AssertionError("HOST1 did not export")
    ctx.ssh(f"cat > {HOST_TMP}/tau2_roundtrip.py", stdin=open(os.path.join(HERE, "tau2_roundtrip.py")).read())
    ctx.ssh(f"sudo cp {HOST_TMP}/ds5/trajectories.jsonl {HOST_TMP}/traj.jsonl && sudo chmod a+r {HOST_TMP}/traj.jsonl")
    probs, n = [], 0
    for it in ctx.cache["host_items"]:
        p = ctx.ssh(f"cd ~/tau2-bench && .venv/bin/python {HOST_TMP}/tau2_roundtrip.py /var/lib/avos/results/{it['run']}/result.json "
                    f"{HOST_TMP}/traj.jsonl {it['run']} {it['principal']} 2>/dev/null | tail -1", timeout=600)
        try:
            rep = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            probs.append(f"{it['run']}: no verdict ({lib.short(p.stderr, 120)})")
            continue
        if not rep["ok"]:
            probs.append(f"{it['run']}: {lib.short(rep['problems'][0], 200)}")
        n += rep["n"]
    return not probs, "; ".join(probs[:3]) or f"5 runs, {n} messages: τ² validates the rebuilt runs and they equal the originals"


@check("HOST3_all_runs_counts_determinism", "host: export --all over the 57 real results twice; byte-identical manifest; passed/failed/unknown equal the result files", host=True)
def host3(ctx):
    _host_retrain_present(ctx)
    rc1, p1, s1 = _hexport(ctx, f"{HOST_TMP}/all1", "--all")
    rc2, p2, s2 = _hexport(ctx, f"{HOST_TMP}/all2", "--all")
    probs = []
    if rc1 not in (0, 1) or rc2 != rc1:
        probs.append(f"rc {rc1}/{rc2} {lib.short(p1.stdout + p1.stderr, 200)}")
    same = ctx.ssh(f"sudo cmp {HOST_TMP}/all1/manifest.json {HOST_TMP}/all2/manifest.json && sudo cmp {HOST_TMP}/all1/trajectories.jsonl {HOST_TMP}/all2/trajectories.jsonl && echo same").stdout
    if "same" not in same:
        probs.append("two --all exports differ")
    m = json.loads(_hread(ctx, f"{HOST_TMP}/all1/manifest.json") or "{}")
    summary = ctx.ssh("python3 - <<'EOF'\nimport json,glob,os\nout={}\nfor d in sorted(glob.glob('/var/lib/avos/results/run_*')):\n"
                      "    r=os.path.basename(d)\n    try:\n        R=json.load(open(d+'/result.json'))\n    except Exception:\n        out[r]='unreadable'; continue\n"
                      "    if 'simulations' in R:\n        ri=R['simulations'][0].get('reward_info')\n        out[r]=None if not ri else (ri.get('reward')==1.0)\n"
                      "    else:\n        out[r]=bool(R.get('tests_passed'))\nprint(json.dumps(out))\nEOF").stdout
    truth = json.loads(summary)
    exported = {x["run"] for x in m.get("items", [])}
    agents = {}
    for x in m.get("items", []):
        if x["principal"].count("/agent/") == 1 and "/sub/" not in x["principal"]:
            agents[x["run"]] = x["passed"]
    wrong = [r for r, v in agents.items() if truth.get(r) != v]
    if wrong:
        probs.append(f"passed disagrees with result.json for {wrong[:3]}")
    if m.get("counts", {}).get("runs_requested") != len(truth):
        probs.append(f"runs_requested {m.get('counts', {}).get('runs_requested')} vs {len(truth)} result dirs")
    bad_reasons = [s for s in m.get("skipped", []) if s["reason"] not in ("unknown_run", "no_host_rows", "witness_unreadable", "store_unreadable")]
    if bad_reasons:
        probs.append(f"skipped with free-text reasons: {bad_reasons[:2]}")
    c = m.get("counts", {})
    return not probs, "; ".join(probs[:3]) or (f"{len(truth)} runs: {c.get('runs_exported')} exported ({c.get('passed')} passed / {c.get('failed')} failed / "
                                              f"{c.get('unknown')} unknown), {len(m.get('skipped', []))} skipped, M14 {m.get('m14')}, identical twice ({s1:.0f}s, {s2:.0f}s)")


@check("HOST4_view_latency", "host / M9: `view` of a real run on the live store answers in ≤ 2 s", host=True)
def host4(ctx):
    _host_retrain_present(ctx)
    cmd = f"cd {HOST_REPO} && sudo env AVOS_TOOL_SCHEMAS={HOST_TMP}/tau2_retail_tools.json AVOS_RETRAIN_CACHE={HOST_TMP}/cache TMPDIR={HOST_TMP}/tmp python3 retrain/bin/retrain view --run run_retail-0_12 > /dev/null; echo rc=$?"
    ctx.ssh(cmd)
    t0 = time.perf_counter()
    p = ctx.ssh(cmd)
    secs = time.perf_counter() - t0
    ok = "rc=0" in p.stdout and secs <= TH["view_s"] + 0.5
    return ok, f"{secs:.2f}s incl. ssh (bar {TH['view_s']}s + 0.5 ssh), {p.stdout.strip()}"


@check("HOST0_no_stray_loops", "class B: before the live checks, no leftover wait loop or earlier suite process is running on the host", host=True)
def host0(ctx):
    p = ctx.ssh("pgrep -af 'while pgrep|tests/run.py|retrain/bin/retrain|tau2_roundtrip' | grep -v 'pgrep -af' || true")
    if p.returncode not in (0, 1):
        raise AssertionError(f"ssh failed: {lib.short(p.stderr, 200)}")
    stray = [l for l in p.stdout.splitlines() if l.strip()]
    return not stray, "nothing running" if not stray else f"{len(stray)} stray process(es) on {HOST}, e.g. {lib.short(stray[0], 140)!r}"


@check("HOSTZ_no_leftover_processes", "class B: after cleanup the suite left no process or scratch dir of its own on the host nor an ssh master on the laptop", host=True)
def hostz(ctx):
    ctl = ctx.ssh_ctl
    ctx.host_cleanup()
    probs = []
    if ctl and subprocess.run(["pgrep", "-f", f"ControlPath={ctl}"], capture_output=True).returncode == 0:
        probs.append("ssh control master still alive on the laptop")
    one = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=no", "-o", "ControlPath=none", HOST,
           "pgrep -af 'retrain/bin/retrain|tau2_roundtrip|while pgrep|retrain-tests' | grep -v 'pgrep -af' || true; "
           f"ls -d {HOST_TMP} /tmp/poc8-tests-* /tmp/poc8-selftest-* 2>/dev/null | sed 's/^/scratch left: /'"]
    p = subprocess.run(one, capture_output=True, text=True, timeout=60)
    left = [l for l in p.stdout.splitlines() if l.strip()]
    if left:
        probs.append(f"{len(left)} process(es)/scratch dir(s) left on {HOST}: {lib.short(left[0], 120)!r}")
    if subprocess.run(["pgrep", "-f", "retrain/tests/trl_check.py|tau2_roundtrip.py"], capture_output=True).returncode == 0:
        probs.append("suite helper still running on the laptop")
    return not probs, "; ".join(probs) or "no suite process on the host, no ssh master on the laptop"


def run(args):
    ctx = Ctx()
    n_fail = n_pass = 0
    print(f"# POC 8 acceptance suite  ({'quick' if args.quick else 'full'}{', host' if args.host else ''})  scratch={ctx.tmp}")
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
            except Exception as e:
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
        if n_fail == 0:
            shutil.rmtree(ctx.tmp, ignore_errors=True)
    print(f"# {n_pass} passed, {n_fail} failed")
    return 0 if n_fail == 0 else 1


def selftest(args):
    results = []

    def ok(name, cond, note=""):
        results.append(bool(cond))
        print(f"{'PASS' if cond else 'FAIL'} selftest.{name:<40} {note}")

    tmp = tempfile.mkdtemp(prefix="poc8-selftest-")
    ok("fixtures_frozen", lib.fixture_drift() == [], str(lib.fixture_drift()[:3]))
    e = lib.Env(tmp)
    for vm in ("vm-01-e1a4cf", lib.TREE_VM):
        v, note = lib.validate_store(e, vm)
        ok(f"fixture_valid_{vm}", v, str(note) if not v else "validate+verify ok")
    lib.clone_run(e, "run_retail-0_13", "run_retail-10_0", "vm-01-aa0010", task_id="10")
    lib.clone_run(e, "run_retail-0_11", "run_retail-0_92", "vm-01-aa0092", counters="truncate")
    lib.clone_run(e, "run_retail-0_11", "run_retail-0_93", "vm-01-aa0093", counters="none")
    lib.clone_run(e, "run_retail-0_12", "run_retail-0_91", "vm-01-aa0091", tamper_usage=True)
    v, note = lib.validate_store(e, "vm-01-aa0010")
    ok("clone_valid", v, str(note) if not v else "cloned VM passes validate+verify")
    st = {r: json.loads(lib.avos("completeness", "--run", r, env={"AVOS_TRACE_ROOT": e.store})["out"])["status"]
          for r in ("run_retail-10_0", "run_retail-0_92", "run_retail-0_93")}
    ok("clone_m8", st == {"run_retail-10_0": "complete", "run_retail-0_92": "incomplete", "run_retail-0_93": "unverified"}, str(st))
    ok("clone_task", e.job("run_retail-10_0")["task"] == "retail/10" and e.sim("run_retail-10_0")["task_id"] == "10")
    ok("store_ids_distinct", not set(x["event_id"] for x in e.store_events("vm-01-aa0010")) & set(x["event_id"] for x in e.store_events("vm-01-a42e93")))
    ag = e.job("run_retail-0_12")["agent_spiffe_id"]
    pairs, probs = lib.match_witness(e.sim("run_retail-0_12")["messages"], lib.agent_calls(e.store_events("vm-01-e1a4cf"), ag))
    ok("witness_agrees_real", len(pairs) == 11 and not probs, f"{len(pairs)} pairs, {probs}")
    ag91 = e.job("run_retail-0_91")["agent_spiffe_id"]
    pairs, probs = lib.match_witness(e.sim("run_retail-0_91")["messages"], lib.agent_calls(e.store_events("vm-01-aa0091"), ag91))
    ok("witness_tamper_detected", bool(probs), f"{probs[:1]}")
    sim9 = e.sim("run_retail-0_9")
    ok("greeting_is_first", sim9["messages"][0]["content"] == lib.GREETING and not sim9["messages"][0].get("usage"))
    for run in lib.NATIVE_RUNS:
        res = e.witness(run)
        vm = e.vm_of_run(run)
        calls = lib.agent_calls(e.store_events(vm), e.job(run)["agent_spiffe_id"])
        tools = json.load(open(lib.TOOLS_FIX))
        pairs, probs, join, other = lib.witness_verdict(res, calls, tools, lib.system_prompt_for(res["simulations"][0]))
        ok(f"hash_oracle_{run}", not probs and join == "response_id" and len(pairs) == len(calls) and not other, f"{len(pairs)} joined, {probs[:1]}")
    res = e.witness("run_retail-0_16")
    vm = e.vm_of_run("run_retail-0_16")
    calls = lib.agent_calls(e.store_events(vm), e.job("run_retail-0_16")["agent_spiffe_id"])
    tools = json.load(open(lib.TOOLS_FIX))
    for name, mut in (("reply", lib.mut_reply_text), ("user", lib.mut_user_text), ("args", lib.mut_tool_args), ("id", lib.mut_tool_id),
                      ("policy", lib.mut_policy), ("swap", lib.mut_swap_raw_ids), ("result", lib.mut_tool_result)):
        r2 = json.loads(json.dumps(res))
        sim2 = r2["simulations"][0]
        mut(sim2)
        same_tokens = [m["usage"] for m in sim2["messages"] if m.get("usage")] == [m["usage"] for m in res["simulations"][0]["messages"] if m.get("usage")]
        _, probs, _, _ = lib.witness_verdict(r2, calls, tools, lib.system_prompt_for(sim2))
        ok(f"mutator_{name}_caught", same_tokens and bool(probs), f"{probs[:1]}")
    t2 = json.loads(json.dumps(tools))
    t2[3]["function"]["description"] += " (edited)"
    ok("mutator_tools_caught", bool(lib.witness_verdict(res, calls, t2, lib.system_prompt_for(res["simulations"][0]))[1]))
    lib.clone_run(e, "run_retail-0_16", "run_retail-0_209", "vm-01-bb209")
    jid = lib.inject_call(e, "vm-01-bb209", "deepseek-ai/DeepSeek-V4-Flash-0731")
    v, note = lib.validate_store(e, "vm-01-bb209")
    cj = json.loads(lib.avos("completeness", "--run", "run_retail-0_209", env={"AVOS_TRACE_ROOT": e.store})["out"])
    calls9 = lib.agent_calls(e.store_events("vm-01-bb209"), e.job("run_retail-0_209")["agent_spiffe_id"])
    pairs, probs, join, other = lib.witness_verdict(e.witness("run_retail-0_209"), calls9, tools, lib.system_prompt_for(e.sim("run_retail-0_209")))
    ok("inject_call_valid", v and cj["status"] == "complete" and [o["event_id"] for o in other] == [jid] and not probs, f"{note if not v else ''} M8 {cj['status']} other {len(other)} {probs[:1]}")
    lib.clone_run(e, "run_retail-0_16", "run_retail-0_210", "vm-01-bb210")
    lib.inject_call(e, "vm-01-bb210", "Qwen/Qwen3-30B-A3B-Instruct-2507", response_id="chatcmpl-leftover-0001")
    calls10 = lib.agent_calls(e.store_events("vm-01-bb210"), e.job("run_retail-0_210")["agent_spiffe_id"])
    ok("inject_same_model_leftover", bool(lib.witness_verdict(e.witness("run_retail-0_210"), calls10, tools, lib.system_prompt_for(e.sim("run_retail-0_210")))[1]))
    sim = e.sim("run_retail-0_13")
    fake = {"messages": [{"role": "system", "content": "x"}] + json.loads(json.dumps(
        [({"role": "tool", "tool_call_id": m["id"], "content": m["content"]} if m["role"] == "tool" else
          {"role": m["role"], "content": m["content"], **({"tool_calls": [{"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])}} for t in m["tool_calls"]]} if m.get("tool_calls") else {}),
           **({"usage": {"prompt_tokens": m["usage"]["prompt_tokens"], "completion_tokens": m["usage"]["completion_tokens"]}} if m.get("usage") else {})})
         for m in sim["messages"]]))}
    ok("roundtrip_equal", lib.first_diff(lib.tau2_norm(sim), lib.item_norm(fake)) is None)
    bad = json.loads(json.dumps(fake))
    tc = next(m for m in bad["messages"] if m.get("tool_calls"))
    tc["tool_calls"][0]["id"] = "chatcmpl-tool-ffff"
    ok("roundtrip_catches_id", lib.first_diff(lib.tau2_norm(sim), lib.item_norm(bad)) is not None)
    bad2 = json.loads(json.dumps(fake))
    bad2["messages"][3], bad2["messages"][4] = bad2["messages"][4], bad2["messages"][3]
    ok("roundtrip_catches_order", lib.first_diff(lib.tau2_norm(sim), lib.item_norm(bad2)) is not None)
    sp = lib.system_prompt_for(sim)
    ok("system_prompt", sp.startswith("<instructions>") and sim["policy"] in sp and len(sp) > 6000, f"{len(sp)} chars")
    d = os.path.join(tmp, "planted")
    items = lib.planted_dataset(d)
    s = lib.stats_oracle(items)
    ok("stats_shortcut", s["shortcut"]["tool"] == "find_user_id_by_email" and s["shortcut"]["index"] == 3 and abs(s["shortcut"]["share"] - 0.98) < 1e-9 and s["shortcut"]["flag"], str(s["shortcut"]))
    ok("stats_duplicates", s["exact_duplicates"]["items"] == 3 and len(s["exact_duplicates"]["groups"]) == 1)
    s2 = lib.stats_oracle(lib.planted_dataset(os.path.join(tmp, "div"), shortcut_share=0.3, seed=5))
    ok("stats_diverse_unflagged", s2["shortcut"]["flag"] is False, str(s2["shortcut"]))
    ok("stats_diff_catches", lib.stats_diff(s, {**s, "tool_call_share": s["tool_call_share"] + 0.01}) and not lib.stats_diff(s, json.loads(json.dumps(s))))
    ok("item_ids_canonical", all(lib.canon_item_id(i) == i["item_id"] for i in items))
    ok("split_oracle", lib.split_of("10") == "held_out" and lib.split_of("0") == "train" and lib.split_of("64") is None)
    ok("passed_oracle", lib.passed_of_sim(e.sim("run_retail-0_13")) is True and lib.passed_of_sim(e.sim("run_retail-0_12")) is False and lib.passed_of_sim(e.sim("run_retail-2_1")) is None)
    p = os.path.join(tmp, "f.txt")
    open(p, "w").write("hello world 19122\n")
    st0 = os.stat(p)
    lib.same_size_replace(p, "19122", "19123")
    st1 = os.stat(p)
    ok("same_size_replace", open(p).read() == "hello world 19123\n" and st0.st_size == st1.st_size and st0.st_mtime_ns == st1.st_mtime_ns and st0.st_ino == st1.st_ino)
    snap = {p: (os.path.getsize(p), lib.sha_file(p))}
    open(p, "a").write("more\n")
    ok("prefix_accepts_append", lib.prefix_broken(snap) == [])
    open(p, "w").write("rewritten\n")
    ok("prefix_catches_rewrite", lib.prefix_broken(snap) == [p])
    t0 = lib.tree_snapshot(e.results)
    open(os.path.join(e.results, "run_retail-0_12", "job.json"), "a").write(" ")
    ok("tree_snapshot_detects", lib.tree_snapshot(e.results) != t0)
    good = {"item_id": "", "run": "r", "principal": "p", "parent": None, "children": [], "vm_id": "v", "host": "h", "harness": "tau2", "task": "retail/0",
            "task_id": "0", "evidence": "store", "messages": [{"role": "system", "content": "x", "train": False, "event_id": None},
                                                             {"role": "assistant", "content": None, "train": True, "event_id": "e",
                                                              "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
                                                             {"role": "tool", "tool_call_id": "c", "content": "ok", "train": False, "event_id": None}],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object", "properties": {}}}}],
            "outcome": {"reward": 1.0, "passed": True, "termination_reason": "user_stop", "reward_breakdown": {}, "source": "tau2"},
            "cost": {"usd": None, "prompt_tokens": 1, "completion_tokens": 1, "llm_calls": 1, "policy_calls": 1, "environment_calls": 0},
            "timestamps": {"start": None, "end": None, "wall_ms": None}, "trace_complete": {"status": "complete", "ratio": 1.0},
            "witness": {"present": True, "agrees": True, "problems": [], "sha256": None},
            "lineage": {"session_open": None, "session_close": None, "spawn": None, "exit": None, "label": None, "events": ["e"]},
            "split": "train", "redact": None}
    good["item_id"] = lib.canon_item_id(good)
    ok("item_schema_accepts", lib.item_problems(good) == [], str(lib.item_problems(good)[:2]))
    b1 = json.loads(json.dumps(good))
    del b1["messages"][1]["train"]
    b2 = json.loads(json.dumps(good))
    b2["item_id"] = "0" * 64
    b3 = json.loads(json.dumps(good))
    b3["messages"][1]["tool_calls"][0]["function"]["arguments"] = {"a": 1}
    ok("item_schema_rejects", lib.item_problems(b1) and lib.item_problems(b2) and lib.item_problems(b3))
    ok("tools_cover_calls", lib.tools_cover_calls(good) == [] and lib.tools_cover_calls({**good, "messages": [{"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "g", "arguments": "{}"}}]}]}))
    import redact_hook
    r = redact_hook.digits('{"zip": "19122", "n": 5}', "arguments")
    ok("redact_hook", not lib.DIGIT.search(r) and json.loads(r)["zip"] == "#####" and redact_hook.digits("a1b2", "content") == "a#b#", r)
    sim = e.sim("run_retail-0_12")
    needles = lib.viewer_needles(sim, lib.system_prompt_for(sim), None)
    text = "\n".join(needles)
    ok("viewer_needles_pass", lib.in_order(text, needles) is None, f"{len(needles)} needles")
    sw = list(needles)
    i = next(k for k, n in enumerate(sw) if n.startswith("tool_call "))
    sw[i], sw[i + 1] = sw[i + 1], sw[i]
    ok("viewer_needles_catch_order", lib.in_order("\n".join(sw), needles) is not None)
    ok("strip_html", lib.strip_html("<pre>a &lt; b</pre>") == "a < b")
    e2 = lib.Env(tmp, "scale")
    n_base = len(os.listdir(os.path.join(e2.store, "vm")))
    runs = lib.gen_scale(e2, 6, 3)
    v, note = lib.validate_store(e2, e2.vm_of_run(runs[0]))
    ok("gen_scale_valid", v and len(runs) == 6 and len(os.listdir(os.path.join(e2.store, "vm"))) == n_base + 6 + 3, str(note) if not v else f"{len(os.listdir(os.path.join(e2.store, 'vm')))} VMs")
    if shutil.which("uv") is None:
        print("SKIP selftest.trl_harness                         uv not on PATH")
    else:
        md = os.path.join(tmp, "mini")
        os.makedirs(md)
        tools = [{"type": "function", "function": {"name": "get_order_details", "description": "get", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}}]
        msgs = [{"role": "system", "content": "You are retail.", "train": False, "event_id": None},
                {"role": "assistant", "content": lib.GREETING, "train": False, "event_id": None},
                {"role": "user", "content": "I want to exchange order 123 please.", "train": False, "event_id": "e1"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "chatcmpl-tool-1", "type": "function", "function": {"name": "get_order_details", "arguments": "{\"order_id\": \"#W1\"}"}}], "train": True, "event_id": "e2"},
                {"role": "tool", "tool_call_id": "chatcmpl-tool-1", "content": "{\"status\": \"delivered\"}", "train": False, "event_id": None},
                {"role": "assistant", "content": "Your order is delivered.", "train": True, "event_id": "e3"}]
        lib.write_jsonl(os.path.join(md, "trajectories.jsonl"), [{"item_id": "a" * 64, "messages": msgs, "tools": tools}])
        lib.write_jsonl(os.path.join(md, "sft.jsonl"), [{"item_id": "a" * 64, "turn_index": 3, "prompt": msgs[:3], "completion": [msgs[3]], "tools": tools},
                                                       {"item_id": "a" * 64, "turn_index": 5, "prompt": msgs[:5], "completion": [msgs[5]], "tools": tools}])
        rep_path = os.path.join(md, "rep.json")
        p = subprocess.run(UV + [os.path.join(HERE, "trl_check.py"), md, rep_path], capture_output=True, text=True, timeout=1800,
                           env=dict(os.environ, PYTHONNOUSERSITE="1"))
        rep = json.load(open(rep_path)) if os.path.exists(rep_path) else {"errors": [lib.short(p.stderr[-400:], 300)]}
        ex = {x["turn_index"]: x["trained"] for x in rep.get("sft", {}).get("examples", [])}
        ok("trl_harness", not rep.get("errors") and rep.get("trajectories", {}).get("tokenized") == 1 and set(ex) == {3, 5}
           and "get_order_details" in ex.get(3, "") and "Your order is delivered." in ex.get(5, "")
           and all(lib.GREETING not in t and "exchange order" not in t for t in ex.values()),
           f"trl {rep.get('versions', {}).get('trl')}: {rep.get('errors') or 'labels only on the two policy turns'}")
    n_fail = results.count(False)
    print(f"# selftest: {len(results)} items, {n_fail} failed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="skip the scale checks")
    ap.add_argument("--selftest", action="store_true", help="validate the suite's own oracles on the fixtures")
    ap.add_argument("--host", action="store_true", help="also run the live checks on avos-dev over ssh")
    ap.add_argument("--only", help="run only checks whose id starts with this prefix")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    sys.exit(selftest(a) if a.selftest else run(a))
