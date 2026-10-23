#!/usr/bin/env python3
"""POC 6 acceptance suite. Black box: only trace/CONTRACT.md paths are exercised.

  python3 trace/tests/run.py            full run (includes the 1M-event scale checks; < 10 min on a laptop)
  python3 trace/tests/run.py --quick    skips the 1M-event checks
  python3 trace/tests/run.py --selftest proves the suite's own oracles catch what they claim, on the golden fixtures
  python3 trace/tests/run.py --only P   run only checks whose id starts with P (debugging aid)

Exit 0 only when every executed check passes. One line per check:
  PASS|FAIL|SKIP  <check id>  [<claim/metric>]  <measured vs threshold>
"""
import argparse
import concurrent.futures
import json
import os
import shutil
import statistics
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lib
import synth

CHECKS = []
VERBOSE = False

TH = {
    "size_max_b": 4096, "size_median_b": 768,
    "validate_evps": 50_000, "verify_evps": 40_000, "completeness_evps": 40_000,
    "audit_cold_s": 20.0, "audit_warm_s": 1.0, "audit_scale_factor": 3.0,
    "rss_mib": 512,
    "n_big": 1_000_000, "n_small": 100_000,
}


def check(cid, claim, scale=False):
    def deco(fn):
        CHECKS.append((cid, claim, scale, fn))
        return fn
    return deco


class Ctx:
    """Lazy, cached black-box artefacts (ingested traces, synthetic files) in one temp dir."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="poc6-tests-")
        self.cache = {}

    def path(self, *p):
        return os.path.join(self.tmp, *p)

    def ingest(self, key, *args, env=None):
        if key in self.cache:
            return self.cache[key]
        out = self.path(key + ".jsonl")
        res = lib.run_cli("ingest", *args, "-o", out, env=env)
        if res["rc"] != 0 or not os.path.exists(out):
            raise AssertionError(f"ingest rc={res['rc']}: {(res['err'] or res['out'])[:300]!r}")
        self.cache[key] = out
        return out

    def real(self):
        return self.ingest("real", "--ledger", os.path.join(lib.FIX, "real_events.jsonl"),
                           env={"AVOS_TRACE_CONTENT": "0"})

    def real_rows(self):
        if "real_rows" not in self.cache:
            self.cache["real_rows"] = lib.read_jsonl(self.real())
        return self.cache["real_rows"]

    def legacy_rows(self):
        if "legacy_rows" not in self.cache:
            self.cache["legacy_rows"] = lib.read_jsonl(os.path.join(lib.FIX, "real_events.jsonl"))
        return self.cache["legacy_rows"]

    def run_trace(self, rundir, content=1, prices=None, tag=None):
        key = f"run-{tag or os.path.basename(rundir)}-c{content}-p{bool(prices)}"
        args = ["--run-dir", rundir]
        if prices:
            args += ["--prices", prices]
        return self.ingest(key, *args, env={"AVOS_TRACE_CONTENT": str(content)})

    def rows(self, path):
        k = "rows:" + path
        if k not in self.cache:
            self.cache[k] = lib.read_jsonl(path)
        return self.cache[k]

    def fixture(self, key, maker):
        if key not in self.cache:
            self.cache[key] = maker()
        return self.cache[key]


REAL_RUN = os.path.join(lib.FIX, "run_retail-0_9")
MINI_RUN = os.path.join(lib.FIX, "golden", "mini_run")
GOOD = os.path.join(lib.FIX, "golden", "good.jsonl")
PRICES = os.path.join(lib.FIX, "prices.json")
BAD_CASES = json.load(open(os.path.join(lib.FIX, "golden", "bad_cases.json")))


@check("V01_schema_file", "audit/M8: machine-readable schema")
def v01(ctx):
    if not os.path.exists(lib.SCHEMA):
        raise lib.NotImplementedCLI("trace/schema.json")
    s = json.load(open(lib.SCHEMA))
    txt = json.dumps(s)
    missing = sorted(k for k in lib.KINDS if f'"{k}"' not in txt)
    return isinstance(s, dict) and "$schema" in s and not missing, f"$schema={'$schema' in s} kinds_missing={missing} (want none)"


@check("V02_validate_good", "audit/M8: validator accepts a valid trace")
def v02(ctx):
    res = lib.run_cli("validate", GOOD)
    j = lib.cli_json(res)
    n = sum(1 for _ in open(GOOD))
    return res["rc"] == 0 and j.get("ok") is True and j.get("events") == n, f"rc={res['rc']} ok={j.get('ok')} events={j.get('events')} (want 0/true/{n})"


def _mk_bad(name, case):
    code, eid = case["code"], case["event_id"]

    def fn(ctx):
        res = lib.run_cli("validate", os.path.join(lib.FIX, "golden", f"bad_{name}.jsonl"))
        j = lib.cli_json(res)
        errs = j.get("errors") or []
        hit = any(e.get("code") == code and e.get("event_id") == eid for e in errs)
        got = sorted({f"{e.get('code')}@{e.get('event_id')}" for e in errs})[:6]
        return res["rc"] == 1 and j.get("ok") is False and hit, f"rc={res['rc']} got={got} (want rc 1, {code}@{eid})"
    return fn


for _i, (_name, _case) in enumerate(sorted((k, v) for k, v in BAD_CASES.items() if k != "chain")):
    check(f"V{_i + 3:02d}_reject_{_name}", f"audit: validator rejects {_case['code']}")(_mk_bad(_name, _case))


@check("V20_validate_real_ingest", "audit/M8: ingested real ledger (POC 1-5) validates")
def v20(ctx):
    res = lib.run_cli("validate", ctx.real())
    j = lib.cli_json(res)
    return res["rc"] == 0 and j.get("ok") is True, f"rc={res['rc']} ok={j.get('ok')} errors={(j.get('errors') or [])[:3]}"


@check("V21_validate_real_run_content_on", "retraining: real run trace validates with content captured")
def v21(ctx):
    res = lib.run_cli("validate", ctx.run_trace(REAL_RUN, 1))
    j = lib.cli_json(res)
    return res["rc"] == 0 and j.get("ok") is True, f"rc={res['rc']} errors={(j.get('errors') or [])[:3]}"


@check("V22_validate_real_run_content_off", "audit: real run trace validates without content")
def v22(ctx):
    res = lib.run_cli("validate", ctx.run_trace(REAL_RUN, 0))
    j = lib.cli_json(res)
    return res["rc"] == 0 and j.get("ok") is True, f"rc={res['rc']} errors={(j.get('errors') or [])[:3]}"


@check("V23_cli_usage_exit_code", "contract: missing input -> exit 2, no traceback")
def v23(ctx):
    res = lib.run_cli("validate", ctx.path("does-not-exist.jsonl"))
    return res["rc"] == 2 and "Traceback" not in res["err"], f"rc={res['rc']} (want 2) traceback={'Traceback' in res['err']}"


@check("L01_ingest_preserves_rows", "M8: one trace event per real ledger row, per kind")
def l01(ctx):
    from collections import Counter
    cin = Counter(r["kind"] for r in ctx.legacy_rows())
    cout = Counter(r["kind"] for r in ctx.real_rows())
    bad = {k: (cin[k], cout.get(k, 0)) for k in cin if cout.get(k, 0) != cin[k]}
    extra = {k for k in cout if k not in cin} - {"tool.result"}
    ok = not bad and not extra and cout.get("tool.result", 0) >= cin["tool.call"]
    return ok, f"in={sum(cin.values())} out={sum(cout.values())} mismatched={bad} extra_kinds={sorted(extra)}"


@check("L02_principals_alive", "M3/audit: every event's principal is in the ledger and alive at its ts")
def l02(ctx):
    rows = ctx.real_rows()
    L = lib.ledger(rows)
    unknown = dead = 0
    for r in rows:
        a = lib.alive_for(L, r)
        if a is None:
            unknown += 1
        elif not a:
            dead += 1
    legacy_p = {p for p in map(lib.legacy_principal, ctx.legacy_rows()) if p}
    missing = sorted(legacy_p - set(L))[:3]
    return unknown == 0 and dead == 0 and not missing, f"unknown={unknown} dead={dead} principals={len(L)} legacy_not_in_ledger={missing} (want 0/0/none)"


@check("L03_toolcall_links_allow", "M5/audit: every gateway tool.call links to the authz.decision that allowed it")
def l03(ctx):
    rows = ctx.real_rows()
    by_id = {r["event_id"]: r for r in rows}
    used, bad, n = set(), [], 0
    for r in rows:
        if r["kind"] != "tool.call":
            continue
        n += 1
        d = by_id.get(r.get("authz_ref"))
        why = None
        if r.get("via") != "gateway":
            why = "via!=gateway"
        elif not d or d["kind"] != "authz.decision":
            why = "dangling authz_ref"
        elif d["decision"] != "allow" or d["principal"] != r["principal"] or d["action"] != "tool.call":
            why = "decision does not allow this principal/action"
        elif d["resource"] != "Tool::" + json.dumps(r["tool"]) or d["args"] != r["args"]:
            why = "decision is for other tool/args"
        elif d["ts"] > r["ts"]:
            why = "decision after call"
        elif r["authz_ref"] in used:
            why = "decision reused by two calls"
        used.add(r.get("authz_ref"))
        if why:
            bad.append((r["event_id"], why))
    want = sum(1 for r in ctx.legacy_rows() if r["kind"] == "tool.call")
    return n == want and not bad, f"tool.calls={n} (want {want}) bad={bad[:3]}"


@check("L04_llmcall_links_allow", "M5/audit: every llm.call links to its allow decision and keeps usage")
def l04(ctx):
    rows = ctx.real_rows()
    by_id = {r["event_id"]: r for r in rows}
    calls = [r for r in rows if r["kind"] == "llm.call"]
    legacy = [r for r in ctx.legacy_rows() if r["kind"] == "llm.call"]
    bad = []
    for r in calls:
        d = by_id.get(r.get("authz_ref"))
        if not d or d["kind"] != "authz.decision" or d["decision"] != "allow" or d["principal"] != r["principal"] \
                or d["action"] != "llm.call" or d["resource"] != "Model::" + json.dumps(r["model"]) or d["ts"] > r["ts"]:
            bad.append(r["event_id"])
    seq_ok = [(r["prompt_tokens"], r["completion_tokens"], r["status"], r["ms"], r["model"], bool(r.get("dropped"))) for r in calls] == \
             [(r["prompt_tokens"], r["completion_tokens"], r["status"], r["ms"], r["model"], bool(r.get("dropped"))) for r in legacy]
    return not bad and seq_ok and len(calls) == len(legacy), f"llm.calls={len(calls)} (want {len(legacy)}) bad_links={bad[:3]} usage_preserved={seq_ok}"


@check("L05_receipts_preserved", "claim 1: halt/resume receipts, nodes and dropped-call receipts survive ingest")
def l05(ctx):
    rows, legacy = ctx.real_rows(), ctx.legacy_rows()
    hr_in = {r["receipt"]: r for r in legacy if r["kind"] in ("halt", "resume")}
    hr_out = {r["receipt"]: r for r in rows if r["kind"] in ("halt", "resume")}
    bad = [k for k in hr_in if k not in hr_out or set(hr_out[k]["nodes"]) != set(hr_in[k]["nodes"]) | {hr_in[k]["target"]}
           or hr_out[k]["target"] != hr_in[k]["target"]
           or hr_out[k]["principal"] != hr_in[k]["target"] or hr_out[k]["action"] != hr_in[k]["action"] or hr_out[k]["by"] != hr_in[k]["by"]]
    fmt = [k for k in hr_out if not lib.RECEIPT_RE.match(k)]
    _, want_receipts = lib.pair_llm_calls(legacy)
    legacy_llm = [i for i, r in enumerate(legacy) if r["kind"] == "llm.call"]
    out_llm = [r for r in rows if r["kind"] == "llm.call"]
    drop_bad = []
    for pos, li in enumerate(legacy_llm):
        if li in want_receipts and (pos >= len(out_llm) or out_llm[pos].get("receipt") != want_receipts[li] or not out_llm[pos].get("dropped")):
            drop_bad.append((legacy[li]["ts"], want_receipts[li], out_llm[pos].get("receipt") if pos < len(out_llm) else None))
    ok = set(hr_in) == set(hr_out) and not bad and not fmt and not drop_bad and len(want_receipts) > 0
    return ok, f"receipts={len(hr_out)} (want {len(hr_in)}) mismatched={bad[:3]} bad_format={fmt[:3]} dropped_calls={len(want_receipts)} dropped_bad={drop_bad[:2]}"


@check("L06_lineage_to_vm", "M3/audit: parent chain of every agent and sub-agent reaches its VM")
def l06(ctx):
    rows = ctx.real_rows()
    L = lib.ledger(rows)
    agents = [r["principal"] for r in rows if r["kind"] == "agent.spawn"]
    bad = []
    for p in agents:
        res = lib.run_cli("query", "lineage", "--principal", p, ctx.real())
        got = lib.cli_json(res)
        want = lib.lineage(L, p)
        if res["rc"] != 0 or got != want or "/agent/" in want[-1] or not want[-1].startswith("spiffe://"):
            bad.append((p[-40:], got))
    return bool(agents) and not bad, f"principals={len(agents)} sub_agents={sum('/sub/' in p for p in agents)} bad={bad[:2]}"


@check("L07_ids_unique_across_hosts", "scale: event ids minted concurrently on 8 hosts never collide")
def l07(ctx):
    src = os.path.join(lib.FIX, "real_events.jsonl")
    outs = [ctx.path(f"host{i:02d}.jsonl") for i in range(8)]

    def one(i):
        return lib.run_cli("ingest", "--ledger", src, "--host", f"host{i:02d}", "-o", outs[i], env={"AVOS_TRACE_CONTENT": "0"})
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, range(8)))
    if any(r["rc"] != 0 for r in res):
        raise AssertionError("ingest failed: " + next(r["err"][:200] for r in res if r["rc"] != 0))
    ids, n, badfmt = set(), 0, 0
    for o in outs:
        for r in lib.read_jsonl(o):
            n += 1
            ids.add(r["event_id"])
            badfmt += not lib.ID_RE.match(r["event_id"])
    return len(ids) == n and badfmt == 0 and n >= 8 * 5516, f"ids={n} unique={len(ids)} bad_format={badfmt} (want 0 collisions)"


@check("L08_event_size_budget", "scale: per-event size without content capture")
def l08(ctx):
    sizes = [len(l.encode()) for l in open(ctx.real()) if l.strip()]
    mx, med = max(sizes), statistics.median(sizes)
    return mx <= TH["size_max_b"] and med <= TH["size_median_b"], f"max={mx}B (<= {TH['size_max_b']}) median={med:.0f}B (<= {TH['size_median_b']})"


@check("C01_completeness_intact", "M8: counters report 100% on an intact 10,000-event trace")
def c01(ctx):
    p = ctx.path("c10k.jsonl")
    synth.completeness_fixture(p)
    j = lib.cli_json(lib.run_cli("completeness", p))
    s, e, g = j.get("avos_trace_events_stored_total"), j.get("avos_trace_events_expected_total"), j.get("gaps")
    return (s, e, g) == (10_000, 10_000, []) and abs(float(j.get("avos_trace_completeness_ratio", 0)) - 1.0) < 1e-12, f"stored={s} expected={e} gaps={g} (want 10000/10000/[])"


@check("C02_completeness_one_dropped", "M8: a single dropped event in 10,000 is detected and named")
def c02(ctx):
    p = ctx.path("c10k-drop.jsonl")
    dropped = synth.completeness_fixture(p, drop=6_543)
    j = lib.cli_json(lib.run_cli("completeness", p))
    s, e, g = j.get("avos_trace_events_stored_total"), j.get("avos_trace_events_expected_total"), j.get("gaps") or []
    want = {"writer": dropped["writer"], "seq": dropped["seq"]}
    ok = (s, e) == (9_999, 10_000) and len(g) == 1 and all(g[0].get(k) == v for k, v in want.items()) \
        and abs(float(j.get("avos_trace_completeness_ratio", 0)) - 0.9999) < 1e-9
    return ok, f"stored={s} expected={e} gaps={g} (want 9999/10000/[{want}])"


@check("C03_completeness_real_run", "M8: real run: trace events == harness ground truth (14 model calls, 5 tool calls)")
def c03(ctx):
    from collections import Counter
    rows = ctx.rows(ctx.run_trace(REAL_RUN, 1))
    sim = lib.tau2_sim(os.path.join(REAL_RUN, "result.json"))
    want = {"llm.call": len(lib.tau2_usage_seq(sim)), "tool.call": len(lib.tau2_tool_calls(sim)),
            "tool.result": sum(m["role"] == "tool" for m in sim["messages"]), "label": 1, "session.open": 1, "session.close": 1,
            "agent.spawn": 1, "agent.exit": 1, "vm.boot": 1, "vm.exit": 1}
    c = Counter(r["kind"] for r in rows)
    got = {k: c.get(k, 0) for k in want}
    stored, happened = sum(got.values()), sum(want.values())
    j = lib.cli_json(lib.run_cli("completeness", ctx.run_trace(REAL_RUN, 1)))
    ok = got == want and j.get("gaps") == []
    return ok, f"stored/happened={stored}/{happened} per_kind_mismatch={ {k: (want[k], got[k]) for k in want if want[k] != got[k]} } cli_gaps={j.get('gaps')}"


def _chat_check(ctx, rundir, tag=None):
    sim = lib.tau2_sim(os.path.join(rundir, "result.json"))
    job = json.load(open(os.path.join(rundir, "job.json")))
    trace = ctx.run_trace(rundir, 1, tag=tag)
    res = lib.run_cli("project", "chat", "--run", job["run"], trace)
    chat = lib.cli_json(res)
    want, got = lib.tau2_norm_messages(sim), lib.chat_norm_messages(chat)
    diff = lib.first_diff(want, got)
    us = lib.tau2_usage_seq(sim)
    tot = {"prompt_tokens": sum(p for p, _ in us), "completion_tokens": sum(c for _, c in us)}
    meta_ok = chat.get("run") == job["run"] and chat.get("principal") == job["agent_spiffe_id"] \
        and chat.get("reward") == sim["reward_info"]["reward"] and chat.get("termination_reason") == sim["termination_reason"] \
        and chat.get("usage") == tot
    return res["rc"] == 0 and diff is None and meta_ok, \
        f"messages={len(got)}/{len(want)} diff={diff} reward={chat.get('reward')}/{sim['reward_info']['reward']} term={chat.get('termination_reason')} usage_ok={chat.get('usage') == tot} principal_ok={chat.get('principal') == job['agent_spiffe_id']}"


@check("P01_chat_mini_golden", "retraining: hand-written mini run projects to its expected chat")
def p01(ctx):
    exp = json.load(open(os.path.join(MINI_RUN, "expected_chat.json")))
    ok, m = _chat_check(ctx, MINI_RUN)
    res = lib.run_cli("project", "chat", "--run", exp["run"], ctx.run_trace(MINI_RUN, 1))
    chat = lib.cli_json(res)
    same = lib.chat_norm_messages(chat) == lib.chat_norm_messages(exp)
    return ok and same, m + f" equals_golden={same}"


@check("P02_chat_real_run", "retraining/claim 3: real tau2 run (both halves) reconstructed exactly once and in order")
def p02(ctx):
    return _chat_check(ctx, REAL_RUN)


def _mk_poc2(name, vm_id):
    def fn(ctx):
        d = ctx.fixture("poc2:" + name, lambda: (synth.legacy_run_dir(os.path.join(lib.FIX, name), ctx.path("poc2-" + name), vm_id), ctx.path("poc2-" + name))[1])
        return _chat_check(ctx, d)
    return fn


for _i, (_n, _v) in enumerate((("run_retail-0_2", "vm-01-0a0a02"), ("run_retail-2_3", "vm-01-0a0a23"), ("run_retail-8_1", "vm-01-0a0a81"))):
    check(f"P0{_i + 3}_chat_poc2_{_n}", "retraining: data/poc2 run (reward %s) reconstructed" % ("0.0" if "2_3" in _n else "1.0"))(_mk_poc2(_n, _v))


@check("P06_llm_and_tool_events_once_in_order", "retraining/M8: trace llm.call usage sequence == harness; tool events match")
def p06(ctx):
    rows = sorted(ctx.rows(ctx.run_trace(REAL_RUN, 1)), key=lib.order_key)
    sim = lib.tau2_sim(os.path.join(REAL_RUN, "result.json"))
    legacy = [r for r in lib.read_jsonl(os.path.join(REAL_RUN, "events.jsonl")) if r["kind"] == "llm.call"]
    calls = [r for r in rows if r["kind"] == "llm.call"]
    usage = [(r.get("prompt_tokens"), r.get("completion_tokens")) for r in calls]
    host_ts = [r["ts"] for r in calls] == [r["ts"] for r in legacy]
    tcs = [r for r in rows if r["kind"] == "tool.call"]
    trs = [r for r in rows if r["kind"] == "tool.result"]
    want_tc = [(t["id"], t["name"], json.dumps(t["arguments"], sort_keys=True)) for t in lib.tau2_tool_calls(sim)]
    got_tc = [(r.get("call_id"), r.get("tool"), json.dumps(r.get("args"), sort_keys=True)) for r in tcs]
    by_id = {r["event_id"]: r for r in rows}
    tr_ok = [by_id.get(r["call_ref"], {}).get("call_id") for r in trs] == [m["id"] for m in sim["messages"] if m["role"] == "tool"]
    via = all(r.get("via") == "harness" for r in tcs)
    ok = usage == lib.tau2_usage_seq(sim) and host_ts and got_tc == want_tc and tr_ok and via
    return ok, f"usage_seq_ok={usage == lib.tau2_usage_seq(sim)} host_ts_anchored={host_ts} tool_calls={len(got_tc)}/{len(want_tc)} tool_match={got_tc == want_tc} results_ok={tr_ok} via_harness={via}"


@check("P07_content_capture_switch", "privacy/audit: AVOS_TRACE_CONTENT=0 stores no prompt/completion/argument text; =1 does")
def p07(ctx):
    sim = lib.tau2_sim(os.path.join(REAL_RUN, "result.json"))
    needles = {m["content"][:24] for m in sim["messages"] if isinstance(m.get("content"), str) and len(m["content"]) >= 24}
    needles |= {json.dumps(t["arguments"])[1:25] for t in lib.tau2_tool_calls(sim)}
    off = open(ctx.run_trace(REAL_RUN, 0)).read()
    on = open(ctx.run_trace(REAL_RUN, 1)).read()
    leak = sorted(n for n in needles if n in off)
    present = sum(n in on for n in needles)
    sizes = [len(l.encode()) for l in off.splitlines() if l.strip()]
    ok = not leak and present == len(needles) and max(sizes) <= TH["size_max_b"]
    return ok, f"leaked_off={leak[:2]} present_on={present}/{len(needles)} max_size_off={max(sizes)}B (<= {TH['size_max_b']})"


@check("P08_label_event", "retraining: the run's result is a label event (reward, termination_reason)")
def p08(ctx):
    rows = ctx.rows(ctx.run_trace(REAL_RUN, 1))
    job = json.load(open(os.path.join(REAL_RUN, "job.json")))
    labels = [r for r in rows if r["kind"] == "label" and r.get("run") == job["run"]]
    ok = len(labels) == 1 and labels[0]["labels"].get("reward") == job["reward"] \
        and labels[0]["labels"].get("termination_reason") == job["termination_reason"] and labels[0]["principal"] == job["agent_spiffe_id"]
    return ok, f"labels={len(labels)} got={labels[0]['labels'] if labels else None} (want reward={job['reward']} termination_reason={job['termination_reason']})"


def _audit(ctx, trace, rows, principal):
    res = lib.run_cli("query", "audit", "--principal", principal, trace)
    got = lib.cli_ndjson(res)
    want = lib.audit_oracle(rows, principal)
    ids_ok = [g.get("event_id") for g in got] == [w for w, _ in want]
    auth_bad = [g.get("event_id") for g, (_, a) in zip(got, want) if not lib.authority_matches(a, g.get("authority"))]
    return res["rc"] == 0 and ids_ok and not auth_bad, f"events={len(got)}/{len(want)} order_ok={ids_ok} authority_bad={auth_bad[:3]}"


@check("A01_audit_golden_subagent", "M9/audit: all events by principal X in order, each with its authority")
def a01(ctx):
    rows = lib.read_jsonl(GOOD)
    sub = next(r["principal"] for r in rows if "/sub/" in r["principal"])
    return _audit(ctx, GOOD, rows, sub)


@check("A02_audit_real_principals", "M9/audit: real principals (halted agents, a VM, a sub-agent)")
def a02(ctx):
    rows = ctx.real_rows()
    ps = [r["principal"] for r in rows if r["kind"] == "llm.call" and r.get("dropped")]
    ps.append(next(r["principal"] for r in rows if r["kind"] == "vm.boot" and any(x["kind"] == "llm.call" and x["principal"] == r["principal"] for x in rows)))
    ps.append(next(r["principal"] for r in rows if r["kind"] == "agent.spawn" and "/sub/" in r["principal"]))
    results = [_audit(ctx, ctx.real(), rows, p) for p in dict.fromkeys(ps)]
    bad = [m for ok, m in results if not ok]
    return not bad, f"principals={len(results)} failures={bad[:2]}"


@check("A03_audit_sorts_across_writers", "clock/M9: events of one principal from several writers sort by host (ts, writer, seq), not file order or guest clock")
def a03(ctx):
    p = ctx.path("multiwriter.jsonl")
    principal, want = synth.multiwriter_fixture(p)
    res = lib.run_cli("query", "audit", "--principal", principal, p)
    got = [g.get("event_id") for g in lib.cli_ndjson(res)]
    file_order = [r["event_id"] for r in lib.read_jsonl(p) if r["principal"] == principal]
    return res["rc"] == 0 and got == want, f"events={len(got)}/{len(want)} sorted_ok={got == want} file_order_differs={file_order != want}"


@check("T01_verify_intact", "trust: verifier accepts an intact chain and reports per-writer heads")
def t01(ctx):
    rows = lib.read_jsonl(GOOD)
    heads = {}
    for r in rows:
        heads[r["writer"]] = r["hash"]
    j = lib.cli_json(lib.run_cli("verify", GOOD))
    got = {w: (v or {}).get("head") for w, v in (j.get("writers") or {}).items()}
    return j.get("ok") is True and got == heads, f"ok={j.get('ok')} heads_match={got == heads}"


@check("T02_verify_modified_event", "trust: a field changed after the fact is detected and the event named")
def t02(ctx):
    case = BAD_CASES["chain"]
    res = lib.run_cli("verify", os.path.join(lib.FIX, "golden", "bad_chain.jsonl"))
    j = lib.cli_json(res)
    errs = j.get("errors") or []
    hit = any(e.get("event_id") == case["event_id"] and e.get("code") == "E_CHAIN_HASH" for e in errs)
    return res["rc"] == 1 and j.get("ok") is False and hit, f"rc={res['rc']} ok={j.get('ok')} errors={[(e.get('code'), e.get('event_id')) for e in errs][:3]} (want E_CHAIN_HASH@{case['event_id']})"


@check("T03_verify_deleted_event", "trust: an event deleted from the middle of the real trace is detected at its successor")
def t03(ctx):
    rows = ctx.real_rows()
    k = len(rows) // 2
    victim, succ = rows[k], next(r for r in rows[k + 1:] if r["writer"] == rows[k]["writer"])
    p = ctx.path("real-deleted.jsonl")
    lib.write_jsonl(p, rows[:k] + rows[k + 1:])
    res = lib.run_cli("verify", p)
    j = lib.cli_json(res)
    errs = j.get("errors") or []
    hit = any(e.get("event_id") == succ["event_id"] and e.get("code") in ("E_CHAIN_PREV", "E_CHAIN_GAP") for e in errs)
    return res["rc"] == 1 and hit, f"deleted={victim['event_id']} errors={[(e.get('code'), e.get('event_id')) for e in errs][:3]} (want E_CHAIN_PREV|E_CHAIN_GAP@{succ['event_id']})"


@check("T04_verify_rehashed_event", "trust: an attacker who re-hashes the edited event is caught at the next link")
def t04(ctx):
    rows = [dict(r) for r in ctx.real_rows()]
    k = next(i for i, r in enumerate(rows) if r["kind"] == "tool.call" and i > len(rows) // 3)
    rows[k]["args"] = dict(rows[k]["args"], amount_cents=1)
    rows[k]["hash"] = lib.ev_hash(rows[k])
    succ = next(r for r in rows[k + 1:] if r["writer"] == rows[k]["writer"])
    p = ctx.path("real-rehashed.jsonl")
    lib.write_jsonl(p, rows)
    res = lib.run_cli("verify", p)
    j = lib.cli_json(res)
    errs = j.get("errors") or []
    hit = any(e.get("event_id") == succ["event_id"] and e.get("code") == "E_CHAIN_PREV" for e in errs)
    return res["rc"] == 1 and hit, f"edited={rows[k]['event_id']} errors={[(e.get('code'), e.get('event_id')) for e in errs][:3]} (want E_CHAIN_PREV@{succ['event_id']})"


@check("T05_chain_is_pinned_algorithm", "trust: the real trace's chain recomputes with the contract's SHA-256/canonical-JSON rule")
def t05(ctx):
    r = lib.chain_check(ctx.real_rows())
    return r is None, f"first_break={r} (want none)"


@check("T06_verify_real", "trust: verifier accepts the ingested real trace")
def t06(ctx):
    res = lib.run_cli("verify", ctx.real())
    j = lib.cli_json(res)
    return res["rc"] == 0 and j.get("ok") is True and j.get("events") == len(ctx.real_rows()), f"rc={res['rc']} ok={j.get('ok')} events={j.get('events')}"


@check("K01_dropped_answer_visible", "claim 1/M7: an answer dropped after a halt is marked dropped and joined to the halt receipt")
def k01(ctx):
    rows = ctx.real_rows()
    halts = {r["receipt"]: r for r in rows if r["kind"] == "halt"}
    dropped = [r for r in rows if r["kind"] == "llm.call" and r.get("dropped")]
    bad = []
    for r in dropped:
        h = halts.get(r.get("receipt"))
        if not h or not r["principal"].startswith(h["target"]) or h["ts"] > r["ts"]:
            bad.append((r["event_id"], r.get("receipt")))
    audit_ok = True
    for r in dropped[:2]:
        got = lib.cli_ndjson(lib.run_cli("query", "audit", "--principal", r["principal"], ctx.real()))
        g = next((x for x in got if x.get("event_id") == r["event_id"]), {})
        audit_ok &= (g.get("authority") or {}).get("receipt") == r["receipt"]
    return len(dropped) >= 4 and not bad and audit_ok, f"dropped={len(dropped)} (want >= 4) unjoined={bad[:3]} audit_shows_receipt={audit_ok}"


@check("K02_halt_joins_branch", "claim 1: every halt/resume names a receipt and the nodes under its target, all known principals")
def k02(ctx):
    rows = ctx.real_rows()
    L = lib.ledger(rows)
    hr = [r for r in rows if r["kind"] in ("halt", "resume")]
    bad = [r["receipt"] for r in hr if r["target"] != r["principal"] or r["target"] not in r["nodes"]
           or any(not n.startswith(r["target"]) or n not in L for n in r["nodes"])]
    dup = len(hr) - len({r["receipt"] for r in hr})
    return bool(hr) and not bad and dup == 0, f"controls={len(hr)} bad_join={bad[:3]} duplicate_receipts={dup}"


@check("E01_cost_synthetic_run", "claim 2/M12: per-run cost from the trace alone (tokens x price, wall, host)")
def e01(ctx):
    p = ctx.path("econ.jsonl")
    want = synth.economics_fixture(p)
    got = lib.cli_json(lib.run_cli("query", "cost", "--run", want["run"], p))
    d = lib.cost_matches(want, got)
    return d is None, f"diff={d} got_cost={got.get('cost_usd')} want={want['cost_usd']}"


@check("E02_cost_real_run", "claim 2/M12/M16: real run priced from llm.call price fields; wall and host from the trace")
def e02(ctx):
    trace = ctx.run_trace(REAL_RUN, 1, prices=PRICES, tag="priced")
    rows = ctx.rows(trace)
    prices = json.load(open(PRICES))
    bad = []
    for r in rows:
        if r["kind"] != "llm.call":
            continue
        pr = prices.get(r["model"])
        want = round((r["prompt_tokens"] * pr["prompt_per_mtok"] + r["completion_tokens"] * pr["completion_per_mtok"]) / 1e6, 9)
        if not r.get("price") or r["price"].get("prompt_per_mtok") != pr["prompt_per_mtok"] or r.get("cost_usd") is None or abs(r["cost_usd"] - want) > 1e-9:
            bad.append(r["event_id"])
    want = lib.cost_oracle(rows, "run_retail-0_9")
    got = lib.cli_json(lib.run_cli("query", "cost", "--run", "run_retail-0_9", trace))
    d = lib.cost_matches(want, got)
    ok = not bad and d is None and want["wall_ms"] == 30692 and want["host"] == "ip-172-31-44-187" and want["priced_calls"] == 14
    return ok, f"unpriced_or_wrong={bad[:3]} diff={d} cost={got.get('cost_usd')} wall_ms={got.get('wall_ms')} host={got.get('host')}"


@check("E03_cost_reads_only_the_trace", "claim 2: doubling tokens in a copied trace (no other files present) doubles the answer")
def e03(ctx):
    rows = [dict(r) for r in ctx.rows(ctx.run_trace(REAL_RUN, 1, prices=PRICES, tag="priced"))]
    for r in rows:
        if r["kind"] == "llm.call":
            r["prompt_tokens"] *= 2
            r["completion_tokens"] *= 2
            r["cost_usd"] = round(r["cost_usd"] * 2, 9)
    lib.chain(rows)
    d = ctx.path("alone")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "t.jsonl")
    lib.write_jsonl(p, rows)
    want = lib.cost_oracle(rows, "run_retail-0_9")
    got = lib.cli_json(lib.run_cli("query", "cost", "--run", "run_retail-0_9", "t.jsonl", cwd=d))
    diff = lib.cost_matches(want, got)
    return diff is None, f"diff={diff} cost={got.get('cost_usd')} (want {want['cost_usd']})"


@check("Q01_guest_clock_skew", "clock: guest clock +1 h; events stay host-anchored, inside the session, in causal order; projection unchanged")
def q01(ctx):
    d = ctx.path("skewed-run")
    synth.skewed_run_dir(REAL_RUN, d)
    ok_chat, m = _chat_check(ctx, d, tag="skewed")
    rows = sorted(ctx.rows(ctx.run_trace(d, 1, tag="skewed")), key=lib.order_key)
    run = "run_retail-0_9"
    so = next(r for r in rows if r["kind"] == "session.open")
    sc = next(r for r in rows if r["kind"] == "session.close")
    inside = all(so["ts"] <= r["ts"] <= sc["ts"] for r in rows if r.get("run") == run and r["kind"] not in ("session.open", "session.close"))
    legacy_llm = [r["ts"] for r in lib.read_jsonl(os.path.join(REAL_RUN, "events.jsonl")) if r["kind"] == "llm.call"]
    anchored = [r["ts"] for r in rows if r["kind"] == "llm.call"] == legacy_llm
    causal, last_llm = True, None
    seq = [r for r in rows if r["kind"] in ("llm.call", "tool.call", "tool.result")]
    for i, r in enumerate(seq):
        if r["kind"] == "llm.call":
            last_llm = r
        elif last_llm is None or r["ts"] < last_llm["ts"]:
            causal = False
    valid = lib.cli_json(lib.run_cli("validate", ctx.run_trace(d, 1, tag="skewed"))).get("ok") is True
    return ok_chat and inside and anchored and causal and valid, f"projection_ok={ok_chat} inside_session={inside} llm_ts_host_anchored={anchored} tool_events_causal={causal} validates={valid}"


def _audit_ids(res):
    return [g.get("event_id") for g in lib.cli_ndjson(res)]


def _refused(res):
    """A refusal is acceptable: exit 1 and an error on stdout (JSON with "error") or stderr; never a silent empty."""
    if res["rc"] != 1:
        return False
    try:
        return bool(json.loads(res["out"]).get("error")) or bool(res["err"].strip())
    except ValueError:
        return bool(res["err"].strip())


def _audit_ok_or_refused(res, want):
    if _refused(res):
        return True, "refused(rc=1)"
    got = _audit_ids(res) if res["rc"] == 0 else None
    return res["rc"] == 0 and got == want, f"rc={res['rc']} got={len(got) if got is not None else None}/{len(want)} correct={got == want}"


@check("X01_audit_file_replaced_same_size_mtime", "M9/audit: after a warm query, the file's content is replaced (same path, inode, size, mtime); the answer follows the content")
def x01(ctx):
    p = ctx.path("stale-a.jsonl")
    principal, want_a = synth.multiwriter_fixture(p)
    other = principal.replace("b0b0b0", "a0a0a0")
    lib.run_cli("query", "audit", "--principal", principal, p)
    r0 = lib.run_cli("query", "audit", "--principal", principal, p)
    warm_ok = _audit_ids(r0) == want_a
    rows_b = synth.swap_principals_text(lib.read_jsonl(p), "a0a0a0", "b0b0b0")
    synth.rewrite_in_place(p, rows_b)
    want_b = [e for e, _ in lib.audit_oracle(rows_b, principal)]
    assert want_b != want_a and len(want_b) == len(want_a)
    r1 = lib.run_cli("query", "audit", "--principal", principal, p)
    ok, m = _audit_ok_or_refused(r1, want_b)
    return warm_ok and ok, f"warm_before={warm_ok} after_replace: {m} (want the new content's {len(want_b)} events, or rc 1 with an error)"


@check("X02_audit_two_files_same_name_same_tmpdir", "M9/audit: two different traces with the same basename, queried alternately under one TMPDIR, each answer belongs to its file")
def x02(ctx):
    d1, d2 = ctx.path("same-name", "one"), ctx.path("same-name", "two")
    os.makedirs(d1), os.makedirs(d2)
    p1, p2 = os.path.join(d1, "t.jsonl"), os.path.join(d2, "t.jsonl")
    principal, want1 = synth.multiwriter_fixture(p1)
    rows2 = synth.swap_principals_text(lib.read_jsonl(p1), "a0a0a0", "b0b0b0")
    lib.write_jsonl(p2, rows2)
    st = os.stat(p1)
    os.utime(p2, ns=(st.st_atime_ns, st.st_mtime_ns))
    want2 = [e for e, _ in lib.audit_oracle(rows2, principal)]
    seq = [(p1, want1), (p2, want2), (p1, want1), (p2, want2), (p1, want1)]
    outs = [_audit_ok_or_refused(lib.run_cli("query", "audit", "--principal", principal, f), w) for f, w in seq]
    return all(o for o, _ in outs), " ; ".join(m for _, m in outs)


@check("X03_audit_inplace_edit_same_size_mtime", "M9/audit: two events move to another principal in place (size and mtime unchanged); both principals' answers follow")
def x03(ctx):
    p = ctx.path("stale-c.jsonl")
    pb, _ = synth.multiwriter_fixture(p)
    pa = pb.replace("b0b0b0", "a0a0a0")
    for q in (pa, pb, pa, pb):
        lib.run_cli("query", "audit", "--principal", q, p)
    rows = synth.move_pair(lib.read_jsonl(p), pa, pb)
    synth.rewrite_in_place(p, rows)
    want_a = [e for e, _ in lib.audit_oracle(rows, pa)]
    want_b = [e for e, _ in lib.audit_oracle(rows, pb)]
    oa, ma = _audit_ok_or_refused(lib.run_cli("query", "audit", "--principal", pa, p), want_a)
    ob, mb = _audit_ok_or_refused(lib.run_cli("query", "audit", "--principal", pb, p), want_b)
    return oa and ob, f"A: {ma} ; B: {mb}"


@check("X04_cost_file_replaced_same_size_mtime", "claim 2/M12: cost after the file is replaced in place by a same-size, same-mtime trace with other token counts")
def x04(ctx):
    p = ctx.path("stale-e.jsonl")
    rows_a, want_a = synth.economics_variant(None, 0)
    rows_b, want_b = synth.economics_variant(None, 7)
    synth.same_size(rows_a, rows_b)
    lib.write_jsonl(p, rows_a)
    lib.run_cli("query", "cost", "--run", want_a["run"], p)
    g0 = lib.cli_json(lib.run_cli("query", "cost", "--run", want_a["run"], p))
    before = lib.cost_matches(want_a, g0) is None
    synth.rewrite_in_place(p, rows_b)
    r1 = lib.run_cli("query", "cost", "--run", want_b["run"], p)
    if _refused(r1):
        return before, f"before={before} after=refused(rc=1)"
    g1 = lib.cli_json(r1) if r1["rc"] == 0 else {}
    d = lib.cost_matches(want_b, g1)
    return before and r1["rc"] == 0 and d is None, f"before={before} after: rc={r1['rc']} diff={d} cost={g1.get('cost_usd')} (want {want_b['cost_usd']})"


def _snapshot(dirs):
    """{path: (size, mtime_ns, sha256)} for every regular file under dirs (recursive)."""
    import hashlib
    out = {}
    for d in dirs:
        for root, _, files in os.walk(d):
            for n in files:
                fp = os.path.join(root, n)
                try:
                    st = os.stat(fp)
                    with open(fp, "rb") as f:
                        h = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    continue
                out[fp] = (st.st_size, st.st_mtime_ns, h)
    return out


def _new_or_changed(before, after, ignore=()):
    return sorted(p for p, v in after.items() if p not in ignore and before.get(p) != v)


def _flip(path, where):
    with open(path, "r+b") as f:
        data = bytearray(f.read())
        if not data:
            return
        n = min(64, len(data))
        start = {"start": 0, "middle": max(0, len(data) // 2 - n // 2), "end": len(data) - n}[where]
        for i in range(start, start + n):
            data[i] ^= 0xFF
        f.seek(0)
        f.write(data)
        f.truncate()


def _truncate(path, frac):
    with open(path, "r+b") as f:
        f.truncate(int(os.path.getsize(path) * frac))


def _swap(a, b):
    with open(a, "rb") as f:
        da = f.read()
    with open(b, "rb") as f:
        db = f.read()
    with open(a, "wb") as f:
        f.write(db)
    with open(b, "wb") as f:
        f.write(da)


@check("X05_audit_cache_tampered", "M9/audit: whatever the tool cached (under its TMPDIR/HOME or next to the input) is truncated, byte-flipped or swapped with another file's cache; the next answer is correct or refused")
def x05(ctx):
    import shutil as sh
    T = ctx.path("x05", "tmp")
    D1, D2 = ctx.path("x05", "in1"), ctx.path("x05", "in2")
    for d in (T, D1, D2):
        os.makedirs(d)
    p1, p2 = os.path.join(D1, "t.jsonl"), os.path.join(D2, "t.jsonl")
    principal, want1 = synth.multiwriter_fixture(p1)
    rows2 = synth.swap_principals_text(lib.read_jsonl(p1), "a0a0a0", "b0b0b0")
    lib.write_jsonl(p2, rows2)
    st = os.stat(p1)
    os.utime(p2, ns=(st.st_atime_ns, st.st_mtime_ns))
    want2 = [e for e, _ in lib.audit_oracle(rows2, principal)]
    env = {k: T for k in ("TMPDIR", "TMP", "TEMP", "HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")}

    def q(path):
        return lib.run_cli("query", "audit", "--principal", principal, path, env=env)
    before = _snapshot([T, D1, D2])
    q(p1), q(p1)
    after1 = _snapshot([T, D1, D2])
    cache1 = _new_or_changed(before, after1, ignore=(p1, p2))
    q(p2), q(p2)
    after2 = _snapshot([T, D1, D2])
    cache2 = _new_or_changed(after1, after2, ignore=(p1, p2))
    if not cache1 and not cache2:
        return True, "no side files written under TMPDIR/HOME or next to the input: nothing to tamper (cache-free design)"
    keep = ctx.path("x05", "pristine")
    os.makedirs(keep)
    pristine = {c: os.path.join(keep, f"{i}.bin") for i, c in enumerate(cache1 + cache2)}
    for c, k in pristine.items():
        sh.copy2(c, k)

    def restore():
        for c, k in pristine.items():
            os.makedirs(os.path.dirname(c), exist_ok=True)
            sh.copy2(k, c)
    results = []
    for name, act in (("flip_start", lambda: [_flip(c, "start") for c in cache1]),
                      ("flip_middle", lambda: [_flip(c, "middle") for c in cache1]),
                      ("flip_end", lambda: [_flip(c, "end") for c in cache1]),
                      ("truncate_half", lambda: [_truncate(c, 0.5) for c in cache1]),
                      ("truncate_zero", lambda: [_truncate(c, 0.0) for c in cache1]),
                      ("delete", lambda: [os.unlink(c) for c in cache1])):
        if not cache1:
            break
        restore()
        act()
        ok, m = _audit_ok_or_refused(q(p1), want1)
        results.append((name, ok, m))
    if cache1 and cache2 and len(cache1) == len(cache2):
        restore()
        for a, b in zip(cache1, cache2):
            _swap(a, b)
        ok, m = _audit_ok_or_refused(q(p1), want1)
        results.append(("swap_with_other_input", ok, m))
        ok, m = _audit_ok_or_refused(q(p2), want2)
        results.append(("swap_other_side", ok, m))
    return all(ok for _, ok, _ in results), f"cache_files={len(cache1)}+{len(cache2)} " + " ; ".join(f"{n}:{'ok' if ok else 'FAIL'}({m})" for n, ok, m in results)


def _big(ctx):
    def mk():
        p = ctx.path("big.jsonl")
        import time
        t0 = time.perf_counter()
        principal, want = synth.big_fixture(p, TH["n_big"])
        n = sum(1 for _ in open(p))
        return {"path": p, "principal": principal, "want": want, "n": n, "gen_s": time.perf_counter() - t0}
    return ctx.fixture("big", mk)


def _small(ctx):
    def mk():
        p = ctx.path("small.jsonl")
        principal, want = synth.big_fixture(p, TH["n_small"], seed=12)
        return {"path": p, "principal": principal, "want": want, "n": sum(1 for _ in open(p))}
    return ctx.fixture("small", mk)


@check("S01_validate_1M_throughput_memory", "scale/M8: validator streams 1M events at >= 50k ev/s on one core, <= 512 MiB", scale=True)
def s01(ctx):
    b = _big(ctx)
    res = lib.run_cli("validate", b["path"], rss=True)
    j = lib.cli_json(res)
    evps = b["n"] / res["secs"]
    ok = res["rc"] == 0 and j.get("ok") is True and evps >= TH["validate_evps"] and (res["maxrss_mib"] or 1e9) <= TH["rss_mib"]
    return ok, f"events={b['n']} ok={j.get('ok')} {evps:,.0f} ev/s (>= {TH['validate_evps']:,}) rss={res['maxrss_mib']:.0f} MiB (<= {TH['rss_mib']}) [gen {b['gen_s']:.0f}s]"


@check("S02_verify_1M_throughput_memory", "scale/trust: verifier streams 1M events at >= 40k ev/s, <= 512 MiB", scale=True)
def s02(ctx):
    b = _big(ctx)
    res = lib.run_cli("verify", b["path"], rss=True)
    j = lib.cli_json(res)
    evps = b["n"] / res["secs"]
    ok = res["rc"] == 0 and j.get("ok") is True and evps >= TH["verify_evps"] and (res["maxrss_mib"] or 1e9) <= TH["rss_mib"]
    return ok, f"ok={j.get('ok')} {evps:,.0f} ev/s (>= {TH['verify_evps']:,}) rss={res['maxrss_mib']:.0f} MiB (<= {TH['rss_mib']})"


@check("S03_audit_1M_cold", "M9: first per-principal query over 1M events (index build allowed) <= 20 s and correct", scale=True)
def s03(ctx):
    b = _big(ctx)
    res = lib.run_cli("query", "audit", "--principal", b["principal"], b["path"], rss=True)
    got = [g.get("event_id") for g in lib.cli_ndjson(res)]
    ok = res["rc"] == 0 and got == b["want"] and res["secs"] <= TH["audit_cold_s"]
    ctx.cache["cold_s"] = res["secs"]
    return ok, f"{res['secs']:.2f}s (<= {TH['audit_cold_s']}) events={len(got)}/{len(b['want'])} correct={got == b['want']} rss={res['maxrss_mib']:.0f} MiB"


@check("S04_audit_1M_warm_scales", "M9: repeat query <= 1.0 s, <= 512 MiB, and <= 3x the same query over 100k events", scale=True)
def s04(ctx):
    b, s = _big(ctx), _small(ctx)
    lib.run_cli("query", "audit", "--principal", s["principal"], s["path"])
    rs = lib.run_cli("query", "audit", "--principal", s["principal"], s["path"])
    rb = lib.run_cli("query", "audit", "--principal", b["principal"], b["path"], rss=True)
    got = [g.get("event_id") for g in lib.cli_ndjson(rb)]
    ratio = rb["secs"] / max(rs["secs"], 0.05)
    ok = rb["rc"] == 0 and got == b["want"] and rb["secs"] <= TH["audit_warm_s"] and ratio <= TH["audit_scale_factor"] \
        and (rb["maxrss_mib"] or 1e9) <= TH["rss_mib"]
    return ok, f"warm_1M={rb['secs']:.2f}s (<= {TH['audit_warm_s']}) warm_100k={rs['secs']:.2f}s ratio={ratio:.1f} (<= {TH['audit_scale_factor']}) rss={rb['maxrss_mib']:.0f} MiB correct={got == b['want']}"


@check("S05_completeness_1M", "M8/scale: completeness counters over 1M events, streaming", scale=True)
def s05(ctx):
    b = _big(ctx)
    res = lib.run_cli("completeness", b["path"], rss=True)
    j = lib.cli_json(res)
    evps = b["n"] / res["secs"]
    ok = j.get("avos_trace_events_stored_total") == b["n"] and j.get("gaps") == [] and evps >= TH["completeness_evps"] \
        and (res["maxrss_mib"] or 1e9) <= TH["rss_mib"]
    return ok, f"stored={j.get('avos_trace_events_stored_total')}/{b['n']} gaps={len(j.get('gaps') or [])} {evps:,.0f} ev/s (>= {TH['completeness_evps']:,}) rss={res['maxrss_mib']:.0f} MiB"


def run(args):
    ctx = Ctx()
    failed = 0
    print(f"# POC 6 acceptance suite  ({'quick' if args.quick else 'full'})  scratch={ctx.tmp}")
    try:
        for cid, claim, scale, fn in CHECKS:
            if args.only and not cid.startswith(args.only):
                continue
            if scale and args.quick:
                print(f"SKIP {cid:<40} [{claim}] --quick")
                continue
            try:
                ok, measured = fn(ctx)
                status = "PASS" if ok else "FAIL"
            except lib.NotImplementedCLI as e:
                ok, status, measured = False, "FAIL", f"not implemented: {e}"
            except AssertionError as e:
                ok, status, measured = False, "FAIL", str(e)[:300]
            except Exception as e:
                ok, status, measured = False, "FAIL", f"error: {type(e).__name__}: {str(e)[:200]}"
                if VERBOSE:
                    traceback.print_exc()
            failed += not ok
            print(f"{status} {cid:<40} [{claim}] {measured}", flush=True)
    finally:
        if not args.keep:
            shutil.rmtree(ctx.tmp, ignore_errors=True)
    print(f"# {len(CHECKS)} checks, {failed} failed")
    return 1 if failed else 0


def selftest():
    """Prove the suite's oracles catch what they claim, using only the fixtures (no solver needed)."""
    import copy
    import random
    results = []

    def ok(name, cond, note=""):
        results.append(cond)
        print(f"{'PASS' if cond else 'FAIL'} selftest.{name:<36} {note}")

    tmp = tempfile.mkdtemp(prefix="poc6-selftest-")
    try:
        synth.golden(os.path.join(tmp, "g"))
        synth.mini_run(os.path.join(tmp, "g", "mini_run"))
        same = all(open(os.path.join(tmp, "g", f)).read() == open(os.path.join(lib.FIX, "golden", f)).read()
                   for f in os.listdir(os.path.join(lib.FIX, "golden")) if f.endswith(".jsonl") or f.endswith(".json"))
        ok("golden_reproducible", same)
        good = lib.read_jsonl(GOOD)
        ok("lint_accepts_good", lib.lint(good) == [], f"errors={lib.lint(good)[:2]}")
        ok("chain_accepts_good", lib.chain_check(good) is None)
        for name, case in sorted(BAD_CASES.items()):
            rows = lib.read_jsonl(os.path.join(lib.FIX, "golden", f"bad_{name}.jsonl"))
            if name == "chain":
                r = lib.chain_check(rows)
                ok(f"chain_catches_{name}", r is not None and rows[r[0]]["event_id"] == case["event_id"] and r[1] == "hash", f"{r}")
            else:
                codes = lib.lint_codes(rows)
                ok(f"lint_catches_{name}", (case["event_id"], case["code"]) in codes, f"{case['code']}@{case['event_id']}")
        rows = copy.deepcopy(good)
        del rows[10]
        r = lib.chain_check(rows)
        ok("chain_catches_deletion", r is not None and r[1] == "prev")
        rows = copy.deepcopy(good)
        rows[10]["ts"] = "2030-01-01T00:00:00.000Z"
        rows[10]["hash"] = lib.ev_hash(rows[10])
        r = lib.chain_check(rows)
        ok("chain_catches_rehash", r is not None and r[1] == "prev" and r[0] > 10)
        sim = lib.tau2_sim(os.path.join(MINI_RUN, "result.json"))
        exp = json.load(open(os.path.join(MINI_RUN, "expected_chat.json")))
        ok("chat_oracle_matches_golden", lib.first_diff(lib.tau2_norm_messages(sim), lib.chat_norm_messages(exp)) is None)

        def mutated(f):
            c = copy.deepcopy(exp)
            f(c)
            return lib.first_diff(lib.tau2_norm_messages(sim), lib.chat_norm_messages(c)) is not None
        ok("chat_catches_dropped_tool_msg", mutated(lambda c: c["messages"].pop(3)))
        ok("chat_catches_reorder", mutated(lambda c: c["messages"].__setitem__(slice(1, 3), c["messages"][1:3][::-1])))
        ok("chat_catches_usage_change", mutated(lambda c: c["messages"][2]["usage"].__setitem__("prompt_tokens", 1)))
        ok("chat_catches_argument_change", mutated(lambda c: c["messages"][2]["tool_calls"][0]["function"].__setitem__("arguments", '{"order_id": "#W8"}')))
        ok("chat_catches_duplicate_call", mutated(lambda c: c["messages"].insert(4, c["messages"][4])))
        ok("chat_catches_tool_id_change", mutated(lambda c: c["messages"][3].__setitem__("tool_call_id", "x")))
        rsim = lib.tau2_sim(os.path.join(REAL_RUN, "result.json"))
        gw = [(r["prompt_tokens"], r["completion_tokens"]) for r in lib.read_jsonl(os.path.join(REAL_RUN, "events.jsonl")) if r["kind"] == "llm.call"]
        ok("real_run_usage_joins_gateway", gw == lib.tau2_usage_seq(rsim), f"{len(gw)} calls")
        legacy = lib.read_jsonl(os.path.join(lib.FIX, "real_events.jsonl"))
        L = lib.ledger(legacy, legacy=True)
        unknown = dead = 0
        for r in legacy:
            p = lib.legacy_principal(r)
            if not p:
                continue
            a = lib.alive_for(L, r, p)
            unknown += a is None
            dead += a is False
        ok("real_ledger_liveness_rule", unknown == 0 and dead == 0, f"unknown={unknown} dead={dead} (vm birth = vm.boot.ts - boot_ms; llm.call start = ts - ms)")
        pairs = lib.pair_tool_calls(legacy)
        ok("real_ledger_toolcalls_pair", len(pairs) == 527 and all(v is not None for v in pairs.values()), f"{len(pairs)} paired")
        lp, rc = lib.pair_llm_calls(legacy)
        ok("real_ledger_llmcalls_pair", len(lp) == 375 and all(v is not None for v in lp.values()) and len(rc) == 4 and all(rc.values()), f"{len(lp)} paired, {len(rc)} dropped with receipt")
        orphan = [p for p in L if L[p][2] and L[p][2] not in L]
        ok("real_ledger_lineage_closed", not orphan, f"orphans={orphan[:2]}")
        p = os.path.join(tmp, "c.jsonl")
        dropped = synth.completeness_fixture(p, drop=6_543)
        c = lib.completeness_oracle(lib.read_jsonl(p))
        ok("completeness_oracle_finds_gap", c["stored"] == 9999 and c["expected"] == 10000 and c["gaps"] == [{"writer": dropped["writer"], "seq": dropped["seq"]}], f"{c['gaps']}")
        synth.completeness_fixture(p)
        c = lib.completeness_oracle(lib.read_jsonl(p))
        ok("completeness_oracle_intact", c["stored"] == c["expected"] == 10000 and not c["gaps"])
        ok("completeness_fixture_valid", lib.lint(lib.read_jsonl(p)) == [] and lib.chain_check(lib.read_jsonl(p)) is None)
        p = os.path.join(tmp, "mw.jsonl")
        principal, want = synth.multiwriter_fixture(p)
        rows = lib.read_jsonl(p)
        ok("multiwriter_fixture_valid", lib.lint(rows) == [] and lib.chain_check(rows) is None, f"lint={lib.lint(rows)[:2]}")
        ok("audit_oracle_sorts", [e for e, _ in lib.audit_oracle(rows, principal)] == want and [r["event_id"] for r in rows if r["principal"] == principal] != want)
        a = dict(lib.audit_oracle(rows, principal))
        halt_id = next(r["event_id"] for r in rows if r["kind"] == "halt" and r["principal"] == principal)
        ok("audit_oracle_authority", a[halt_id].get("receipt", "").startswith("hr_") and not lib.authority_matches(a[halt_id], {"receipt": "hr_bad"}))
        p = os.path.join(tmp, "e.jsonl")
        want = synth.economics_fixture(p)
        rows = lib.read_jsonl(p)
        ok("economics_fixture_valid", lib.lint(rows) == [], f"{lib.lint(rows)[:2]}")
        got = lib.cost_oracle(rows, want["run"])
        ok("cost_oracle_matches_generator", lib.cost_matches(want, got) is None, f"{lib.cost_matches(want, got)}")
        for r in rows:
            if r["kind"] == "llm.call" and r.get("prompt_tokens"):
                r["prompt_tokens"] *= 2
        ok("cost_oracle_moves_with_tokens", lib.cost_matches(want, lib.cost_oracle(rows, want["run"])) is not None)
        p = os.path.join(tmp, "big.jsonl")
        principal, want = synth.big_fixture(p, 20_000, seed=3)
        rows = lib.read_jsonl(p)
        ok("big_fixture_valid", lib.lint(rows) == [] and lib.chain_check(rows) is None and len(want) > 5, f"lint={lib.lint(rows)[:2]} sample_events={len(want)}")
        from collections import Counter
        mix = Counter(r["kind"] for r in rows)
        ok("big_fixture_mimics_real_mix", 0.6 < mix["authz.decision"] / len(rows) < 0.85 and mix["llm.call"] > 0 and mix["halt"] > 0, f"{dict(mix.most_common(4))}")
        p = os.path.join(tmp, "st.jsonl")
        principal, want = synth.multiwriter_fixture(p)
        st = os.stat(p)
        rows_b = synth.swap_principals_text(lib.read_jsonl(p), "a0a0a0", "b0b0b0")
        synth.rewrite_in_place(p, rows_b)
        st2 = os.stat(p)
        want_b = [e for e, _ in lib.audit_oracle(rows_b, principal)]
        ok("stale_swap_same_size_mtime_valid", st.st_size == st2.st_size and st.st_mtime_ns == st2.st_mtime_ns and st.st_ino == st2.st_ino
           and lib.lint(rows_b) == [] and lib.chain_check(rows_b) is None and want_b != want and len(want_b) == len(want), f"size={st2.st_size} lint={lib.lint(rows_b)[:1]}")
        pa = principal.replace("b0b0b0", "a0a0a0")
        rows_c = synth.move_pair(lib.read_jsonl(p), pa, principal)
        wa, wb = lib.audit_oracle(rows_c, pa), lib.audit_oracle(rows_c, principal)
        ok("stale_move_pair_valid", lib.lint(rows_c) == [] and synth.serialized_size(rows_c) == synth.serialized_size(rows_b)
           and len(wb) == len(want_b) + 2 and len(wa) == len(want) - 2, f"lint={lib.lint(rows_c)[:1]}")
        ra, wa_ = synth.economics_variant(None, 0)
        rb, wb_ = synth.economics_variant(None, 7)
        synth.same_size(ra, rb)
        ok("stale_cost_same_size_valid", synth.serialized_size(ra) == synth.serialized_size(rb) and lib.lint(ra) == [] and lib.lint(rb) == []
           and lib.chain_check(rb) is None and lib.cost_matches(wa_, lib.cost_oracle(ra, wa_["run"])) is None
           and lib.cost_matches(wb_, lib.cost_oracle(rb, wb_["run"])) is None and wa_["cost_usd"] != wb_["cost_usd"])
        ok("stale_refusal_detector", _refused({"rc": 1, "out": '{"error":"stale index"}', "err": ""}) and not _refused({"rc": 0, "out": "", "err": ""})
           and not _refused({"rc": 1, "out": "", "err": ""}))
        cd = os.path.join(tmp, "cache")
        os.makedirs(os.path.join(cd, "sub"))
        f1, f2, f3 = (os.path.join(cd, n) for n in ("a.idx", "sub/b.idx", "c.idx"))
        open(f1, "wb").write(b"A" * 300)
        open(f2, "wb").write(b"B" * 300)
        snap0 = _snapshot([cd])
        open(f3, "wb").write(b"C" * 300)
        open(f2, "r+b").write(b"Z")
        os.utime(f2, ns=(snap0[f2][1], snap0[f2][1]))
        changed = _new_or_changed(snap0, _snapshot([cd]))
        ok("cache_diff_sees_new_and_changed", changed == sorted([f2, f3]), f"{[os.path.basename(c) for c in changed]}")
        _flip(f1, "middle")
        _truncate(f3, 0.5)
        _swap(f1, f2)
        ok("cache_tamper_functions", os.path.getsize(f3) == 150 and open(f2, "rb").read()[:10] == b"AAAAAAAAAA" and open(f2, "rb").read().count(0xBE) == 64
           and open(f1, "rb").read()[:1] == b"Z", f"sizes={os.path.getsize(f1)},{os.path.getsize(f2)},{os.path.getsize(f3)}")
        d = os.path.join(tmp, "skew")
        synth.skewed_run_dir(REAL_RUN, d)
        s2 = lib.tau2_sim(os.path.join(d, "result.json"))
        ok("skew_fixture_shifts_guest_clock", synth.guest_ms(s2["messages"][0]["timestamp"]) - synth.guest_ms(rsim["messages"][0]["timestamp"]) == 3_600_000
           and open(os.path.join(d, "events.jsonl")).read() == open(os.path.join(REAL_RUN, "events.jsonl")).read())
        d = os.path.join(tmp, "poc2")
        synth.legacy_run_dir(os.path.join(lib.FIX, "run_retail-2_3"), d, "vm-01-0a0a23")
        s3 = lib.tau2_sim(os.path.join(d, "result.json"))
        lg = lib.read_jsonl(os.path.join(d, "events.jsonl"))
        ok("poc2_legacy_ledger_joins_harness", [(r["prompt_tokens"], r["completion_tokens"]) for r in lg if r["kind"] == "llm.call"] == lib.tau2_usage_seq(s3)
           and all(a["ts"] <= b["ts"] for a, b in zip(lg, lg[1:])), f"{len(lg)} rows")
        random.seed(0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n_fail = results.count(False)
    print(f"# selftest: {len(results)} items, {n_fail} failed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="skip the 1M-event scale checks")
    ap.add_argument("--selftest", action="store_true", help="validate the suite's own oracles on the golden fixtures")
    ap.add_argument("--only", help="run only checks whose id starts with this prefix")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    ap.add_argument("-v", "--verbose", action="store_true", help="print tracebacks for unexpected errors")
    a = ap.parse_args()
    VERBOSE = a.verbose
    sys.exit(selftest() if a.selftest else run(a))
