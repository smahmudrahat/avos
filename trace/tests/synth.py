"""Synthetic fixture generators for the POC 6 suite. All deterministic (seeded).

Events written here follow trace/CONTRACT.md exactly, including the pinned hash chain, so the
suite's own understanding of the contract is exercised by `run.py --selftest`.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

TD = "spiffe://avoslab.com"
MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
PRICE = {"prompt_per_mtok": 0.10, "completion_per_mtok": 0.30, "currency": "USD"}


class Stream:
    """Builds contract events: per-writer seq, ids derived from (writer, seq), chain at the end."""

    def __init__(self, host):
        self.host = host
        self.seq = {}
        self.rows = []

    def ev(self, kind, ts, writer, principal, vm_id, **f):
        w = f"{self.host}/{writer}"
        s = self.seq[w] = self.seq.get(w, 0) + 1
        r = {"event_id": f"{self.host}.{writer}.{s:010d}", "kind": kind, "ts": ts if isinstance(ts, str) else lib.ms_ts(ts),
             "writer": w, "seq": s, "host": self.host, "principal": principal, "vm_id": vm_id}
        r.update(f)
        self.rows.append(r)
        return r

    def done(self, sort=False):
        rows = sorted(self.rows, key=lib.order_key) if sort else self.rows
        return lib.chain(rows)


def cost(pt, ct, price=PRICE):
    if pt is None or ct is None:
        return None
    return round((pt * price["prompt_per_mtok"] + ct * price["completion_per_mtok"]) / 1e6, 9)


def small_run(host="h1", vm_id="vm-01-aaaaaa", t0=1_792_000_000_000, run="run_retail-0_1", with_sub=True):
    """A VM boots, a session opens, an agent (and a sub-agent) run, calls happen, a halt/resume, exit."""
    S = Stream(host)
    vm = f"{TD}/vm/{vm_id}"
    ag = f"{vm}/agent/tau2"
    sub = f"{ag}/sub/search"
    t = t0
    S.ev("vm.boot", t, "vm", vm, vm_id, vm="vm-01", boot_ms=4000, disk_ms=2000, runtime="firecracker", mem_mib=2048)
    S.ev("session.open", t + 10, "job", vm, vm_id, run=run, harness="tau2", task="retail/0")
    S.ev("agent.spawn", t + 400, "registrar", ag, vm_id, spiffe_id=ag, parent=vm, name="tau2", agent_id="ag_0001", uid=20001)
    d = S.ev("authz.decision", t + 1000, "gateway", ag, vm_id, action="llm.call", resource=f'Model::"{MODEL}"', args={},
             decision="allow", policy_ids=["llm-all"], errors=[])
    S.ev("llm.call", t + 1800, "gateway", ag, vm_id, run=run, authz_ref=d["event_id"], model=MODEL, status=200, ms=800,
         prompt_tokens=428, completion_tokens=86, dropped=False, role="assistant", price=PRICE, cost_usd=cost(428, 86))
    if with_sub:
        S.ev("agent.spawn", t + 2000, "registrar", sub, vm_id, spiffe_id=sub, parent=ag, name="search", agent_id="ag_0002", uid=20002)
    caller = sub if with_sub else ag
    d2 = S.ev("authz.decision", t + 2500, "gateway", caller, vm_id, action="tool.call", resource='Tool::"issue_refund"',
              args={"order_id": "#W1", "amount_cents": 15000}, decision="allow", policy_ids=["refund-200"], errors=[])
    tc = S.ev("tool.call", t + 2510, "gateway", caller, vm_id, run=run, authz_ref=d2["event_id"], via="gateway",
              tool="issue_refund", args={"order_id": "#W1", "amount_cents": 15000})
    S.ev("tool.result", t + 2520, "gateway", caller, vm_id, run=run, call_ref=tc["event_id"], result={"refund_id": "rf_1"}, error=False)
    S.ev("authz.decision", t + 2600, "gateway", caller, vm_id, action="tool.call", resource='Tool::"issue_refund"',
         args={"order_id": "#W1", "amount_cents": 48000}, decision="deny", policy_ids=["refund-200"], errors=[])
    d4 = S.ev("authz.decision", t + 2900, "gateway", caller, vm_id, action="llm.call", resource=f'Model::"{MODEL}"', args={},
              decision="allow", policy_ids=["llm-all"], errors=[])
    h = S.ev("halt", t + 3000, "ctl", caller, vm_id, receipt="hr_0a1b", action="freeze", by="ubuntu", target=caller,
             nodes=[caller], ms=7.3, freeze_ms=0.1)
    S.ev("authz.decision", t + 3100, "gateway", caller, vm_id, action="llm.call", resource=f'Model::"{MODEL}"', args={},
         decision="deny", policy_ids=["halted", "hr_0a1b"], errors=[])
    S.ev("llm.call", t + 3101, "gateway", caller, vm_id, run=run, authz_ref=d4["event_id"], model=MODEL, status=200, ms=900,
         prompt_tokens=500, completion_tokens=10, dropped=True, receipt=h["receipt"], role="assistant", price=PRICE,
         cost_usd=cost(500, 10))
    S.ev("resume", t + 4000, "ctl", caller, vm_id, receipt="hr_0a1c", action="thaw", by="ubuntu", target=caller, nodes=[caller], ms=6.0)
    d3 = S.ev("authz.decision", t + 4100, "gateway", caller, vm_id, action="net.connect", resource='Host::"example.com:443"',
              args={}, decision="allow", policy_ids=["net-example"], errors=[])
    S.ev("net.connect", t + 4110, "gateway", caller, vm_id, run=run, authz_ref=d3["event_id"], dest="example.com:443")
    S.ev("fs.write", t + 4200, "guest", caller, vm_id, run=run, path="/work/out.txt", bytes=120)
    if with_sub:
        S.ev("agent.exit", t + 5000, "registrar", sub, vm_id, spiffe_id=sub, exit_reason="exit 0", agent_id="ag_0002")
    S.ev("label", t + 5500, "job", ag, vm_id, run=run, labels={"reward": 1.0, "termination_reason": "user_stop"}, source="tau2")
    S.ev("agent.exit", t + 6000, "registrar", ag, vm_id, spiffe_id=ag, exit_reason="exit 0", agent_id="ag_0001")
    S.ev("session.close", t + 6100, "job", vm, vm_id, run=run, status="done", wall_ms=6100)
    S.ev("vm.exit", t + 6200, "vm", vm, vm_id, vm="vm-01")
    return S


def golden(outdir):
    """good.jsonl plus one deliberately broken file per validator error code."""
    os.makedirs(outdir, exist_ok=True)
    good = small_run().done()
    lib.write_jsonl(os.path.join(outdir, "good.jsonl"), good)
    idx = {r["event_id"]: i for i, r in enumerate(good)}
    tc = next(i for i, r in enumerate(good) if r["kind"] == "tool.call")
    tr = next(i for i, r in enumerate(good) if r["kind"] == "tool.result")
    halt = next(i for i, r in enumerate(good) if r["kind"] == "halt")
    llm0 = next(i for i, r in enumerate(good) if r["kind"] == "llm.call")
    llm_drop = next(i for i, r in enumerate(good) if r["kind"] == "llm.call" and r["dropped"])
    spawn_sub = next(i for i, r in enumerate(good) if r["kind"] == "agent.spawn" and "/sub/" in r["principal"])
    exit_sub = next(i for i, r in enumerate(good) if r["kind"] == "agent.exit" and "/sub/" in r["principal"])

    def variant(name, mutate, rechain=True):
        rows = [json.loads(json.dumps(r)) for r in good]
        bad_i = mutate(rows)
        if rechain:
            lib.chain(rows)
        lib.write_jsonl(os.path.join(outdir, f"bad_{name}.jsonl"), rows)
        return name, rows[bad_i]["event_id"] if bad_i is not None else None

    cases = {}

    def reg(name, code, mutate, rechain=True):
        n, eid = variant(name, mutate, rechain)
        cases[n] = {"code": code, "event_id": eid}

    reg("missing_principal", "E_SCHEMA", lambda R: (R[llm0].pop("principal"), llm0)[1])
    reg("toolcall_no_authz", "E_AUTHZ_MISSING", lambda R: (R[tc].pop("authz_ref"), tc)[1])
    reg("unknown_kind", "E_KIND", lambda R: (R[llm0].__setitem__("kind", "llm.cal"), llm0)[1])
    reg("timestamp", "E_TS", lambda R: (R[llm0].__setitem__("ts", "2026-10-21 18:45:32"), llm0)[1])
    reg("dup_id", "E_DUP_ID", lambda R: (R[tr].__setitem__("event_id", R[tc]["event_id"]), tr)[1])
    reg("dangling_ref", "E_REF", lambda R: (R[tr].__setitem__("call_ref", "h1.gateway.9999999999"), tr)[1])
    reg("unknown_parent", "E_REF", lambda R: (R[spawn_sub].__setitem__("parent", f"{TD}/vm/vm-01-aaaaaa/agent/ghost"), spawn_sub)[1])
    reg("unknown_principal", "E_PRINCIPAL_UNKNOWN",
        lambda R: (R[llm0].__setitem__("principal", f"{TD}/vm/vm-01-aaaaaa/agent/nobody"), llm0)[1])
    reg("dead_principal", "E_PRINCIPAL_DEAD", lambda R: _move_exit(R, spawn_sub, exit_sub))
    reg("halt_no_receipt", "E_RECEIPT", lambda R: (R[halt].pop("receipt"), halt)[1])
    reg("dropped_no_receipt", "E_RECEIPT", lambda R: (R[llm_drop].pop("receipt"), llm_drop)[1])
    reg("llm_no_tokens", "E_TOKENS", lambda R: (R[llm0].__setitem__("prompt_tokens", None), llm0)[1])
    reg("authz_denied", "E_AUTHZ_DENIED", lambda R: (R[idx[R[tc]["authz_ref"]]].__setitem__("decision", "deny"), tc)[1])
    reg("ts_order", "E_TS_ORDER", lambda R: (R[tr].__setitem__("ts", lib.ms_ts(lib.ts_ms(R[tc]["ts"]) - 5000)), tr)[1])
    reg("seq_order", "E_SEQ_ORDER", lambda R: _swap_lines(R, tc + 1, tc + 2), rechain=False)
    reg("chain", "E_CHAIN_HASH", lambda R: (R[tc]["args"].__setitem__("amount_cents", 100), tc)[1], rechain=False)
    json.dump(cases, open(os.path.join(outdir, "bad_cases.json"), "w"), indent=1, sort_keys=True)
    return cases


def _swap_lines(R, i, j):
    """Two events of one writer, given the same ts (so only seq order is wrong), chained in seq order, then
    swapped in the file. The lower-seq event now comes second: E_SEQ_ORDER without E_TS_ORDER."""
    assert R[i]["writer"] == R[j]["writer"]
    R[j]["ts"] = R[i]["ts"]
    lib.chain(R)
    R[i], R[j] = R[j], R[i]
    return j


def _move_exit(R, spawn_sub, exit_sub):
    """Make the sub-agent exit right after it spawns, so its later tool.call is by a dead principal."""
    R[exit_sub]["ts"] = lib.ms_ts(lib.ts_ms(R[spawn_sub]["ts"]) + 1)
    ex = R.pop(exit_sub)
    R.insert(spawn_sub + 1, ex)
    s = 0
    for r in R:
        if r["writer"].endswith("/registrar"):
            s += 1
            r["seq"] = s
            r["event_id"] = f"{r['host']}.registrar.{s:010d}"
    return next(i for i, r in enumerate(R) if r["kind"] == "tool.call")


def mini_run(outdir):
    """A hand-sized SimulationRun with its legacy ledger half and the expected chat projection."""
    os.makedirs(outdir, exist_ok=True)
    vm_id, run = "vm-01-m1n1aa", "run_retail-7_0"
    vm = f"{TD}/vm/{vm_id}"
    ag = f"{vm}/agent/tau2"
    g0 = "2026-10-21T21:00:00"
    msgs = [
        {"role": "assistant", "content": "Hi! How can I help you today?", "tool_calls": None, "turn_idx": 0, "timestamp": g0 + ".100000", "usage": None},
        {"role": "user", "content": "Cancel order #W9 please.", "tool_calls": None, "turn_idx": 1, "timestamp": g0 + ".900000", "usage": {"completion_tokens": 9, "prompt_tokens": 400}},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "chatcmpl-tool-0001", "name": "get_order_details", "arguments": {"order_id": "#W9"}, "requestor": "assistant"}], "turn_idx": 2, "timestamp": "2026-10-21T21:00:01.700000", "usage": {"completion_tokens": 20, "prompt_tokens": 4000}},
        {"id": "chatcmpl-tool-0001", "role": "tool", "content": "{\"order_id\": \"#W9\", \"status\": \"pending\"}", "requestor": "assistant", "error": False, "turn_idx": 3, "timestamp": "2026-10-21T21:00:01.701000"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "chatcmpl-tool-0002", "name": "cancel_pending_order", "arguments": {"order_id": "#W9", "reason": "no longer needed"}, "requestor": "assistant"}], "turn_idx": 4, "timestamp": "2026-10-21T21:00:02.600000", "usage": {"completion_tokens": 25, "prompt_tokens": 4100}},
        {"id": "chatcmpl-tool-0002", "role": "tool", "content": "{\"order_id\": \"#W9\", \"status\": \"cancelled\"}", "requestor": "assistant", "error": False, "turn_idx": 5, "timestamp": "2026-10-21T21:00:02.601000"},
        {"role": "assistant", "content": "Done, order #W9 is cancelled.", "tool_calls": None, "turn_idx": 6, "timestamp": "2026-10-21T21:00:03.500000", "usage": {"completion_tokens": 12, "prompt_tokens": 4200}},
        {"role": "user", "content": "Thanks. ###STOP###", "tool_calls": None, "turn_idx": 7, "timestamp": "2026-10-21T21:00:04.200000", "usage": {"completion_tokens": 6, "prompt_tokens": 450}},
    ]
    sim = {"id": "mini", "task_id": "7", "start_time": "2026-10-21T21:00:00.100000", "end_time": "2026-10-21T21:00:04.200000",
           "duration": 4.1, "termination_reason": "user_stop", "agent_cost": 0.0, "user_cost": 0.0,
           "reward_info": {"reward": 1.0, "db_check": {"db_match": True, "db_reward": 1.0}}, "messages": msgs, "trial": 0, "seed": 1}
    json.dump({"timestamp": "2026-10-21T21:00:00", "info": {"num_trials": 1}, "tasks": [{"id": "7"}], "simulations": [sim]},
              open(os.path.join(outdir, "result.json"), "w"), indent=1)
    json.dump({"run": run, "vm": "vm-01", "vm_id": vm_id, "vm_spiffe_id": vm, "agent_spiffe_id": ag, "harness": "tau2",
               "task": "retail/7", "status": "done", "http": "200", "wall_ms": 9000, "reward": 1.0,
               "termination_reason": "user_stop"}, open(os.path.join(outdir, "job.json"), "w"), indent=1)
    rows = legacy_ledger_for(sim, run, vm_id, host="ip-10-0-0-1", boot_at_ms=lib.ts_ms("2026-10-21T20:59:55.000Z"))
    lib.write_jsonl(os.path.join(outdir, "events.jsonl"), rows)
    json.dump(expected_chat(sim, run, ag), open(os.path.join(outdir, "expected_chat.json"), "w"), indent=1)


def expected_chat(sim, run, principal):
    """The chat projection the contract requires (OpenAI Chat Completions message format)."""
    out = []
    for m in sim["messages"]:
        if m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["id"], "content": m["content"]})
        else:
            o = {"role": m["role"], "content": m["content"]}
            if m.get("tool_calls"):
                o["tool_calls"] = [{"id": t["id"], "type": "function",
                                    "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])}}
                                   for t in m["tool_calls"]]
            if m.get("usage"):
                o["usage"] = {"prompt_tokens": m["usage"]["prompt_tokens"], "completion_tokens": m["usage"]["completion_tokens"]}
            out.append(o)
    us = lib.tau2_usage_seq(sim)
    return {"run": run, "principal": principal, "messages": out, "reward": sim["reward_info"]["reward"],
            "termination_reason": sim["termination_reason"],
            "usage": {"prompt_tokens": sum(p for p, _ in us), "completion_tokens": sum(c for _, c in us)}}


def guest_ms(ts):
    """tau2 message timestamp (naive ISO with microseconds, UTC) -> epoch ms."""
    return lib.ts_ms(ts[:23] + "Z")


def legacy_ledger_for(sim, run, vm_id, host, boot_at_ms, skew_ms=300):
    """What the POC 1-5 host would have recorded for this run: the exact shapes of today's emitters
    (infra/vm, infra/job, identity/registrar, gateway/gateway.py). One authz.decision + llm.call per
    harness message that carries usage (agent and user-simulator turns both go through the gateway)."""
    vm = f"{TD}/vm/{vm_id}"
    ag = f"{vm}/agent/tau2"
    rows = []
    t = boot_at_ms
    rows.append({"kind": "vm.boot", "ts": lib.ms_ts(t), "vm": "vm-01", "vm_id": vm_id, "spiffe_id": vm, "ip": "172.16.1.2",
                 "boot_ms": 4500, "disk_ms": 2000, "runtime": "firecracker", "host": host, "mem_mib": 2048})
    rows.append({"kind": "session.open", "ts": lib.ms_ts(t + 8), "run": run, "vm": "vm-01", "vm_id": vm_id, "vm_spiffe_id": vm,
                 "harness": "tau2", "task": "retail/" + str(sim.get("task_id", "0"))})
    rows.append({"kind": "agent.spawn", "ts": lib.ms_ts(t + 380), "id": "ag_m1n1", "spiffe_id": ag, "parent": vm, "name": "tau2",
                 "vm_id": vm_id, "uid": 20001, "entry_id": "00000000-0000-0000-0000-000000000001"})
    last = t + 380
    for m in sim["messages"]:
        if not m.get("usage"):
            continue
        end = guest_ms(m["timestamp"]) + skew_ms
        ms = max(50, min(end - last - 10, 3000))
        rows.append({"kind": "authz.decision", "ts": lib.ms_ts(end - ms), "principal": ag, "vm_id": vm_id, "action": "llm.call",
                     "resource": f'Model::"{MODEL}"', "args": {}, "decision": "allow", "policy_ids": ["llm-all"], "errors": []})
        rows.append({"kind": "llm.call", "ts": lib.ms_ts(end), "principal": ag, "vm_id": vm_id, "model": MODEL, "status": 200,
                     "ms": ms, "prompt_tokens": m["usage"]["prompt_tokens"], "completion_tokens": m["usage"]["completion_tokens"],
                     "dropped": False})
        last = end
    rows.append({"kind": "agent.exit", "ts": lib.ms_ts(last + 800), "id": "ag_m1n1", "spiffe_id": ag, "vm_id": vm_id,
                 "exit_reason": "exit 0", "svid_ms": 900})
    rows.append({"kind": "vm.exit", "ts": lib.ms_ts(last + 2500), "vm": "vm-01", "vm_id": vm_id, "spiffe_id": vm})
    rows.append({"kind": "session.close", "ts": lib.ms_ts(last + 2580), "run": run, "vm": "vm-01", "vm_id": vm_id, "vm_spiffe_id": vm,
                 "agent_spiffe_id": ag, "status": "done", "wall_ms": last + 2580 - t})
    return rows


def legacy_run_dir(src_dir, out_dir, vm_id, host="ip-172-31-44-187"):
    """A data/poc2 run (harness only) + a legacy ledger the tests synthesize in today's emitter shapes."""
    os.makedirs(out_dir, exist_ok=True)
    sim = lib.tau2_sim(os.path.join(src_dir, "result.json"))
    job = json.load(open(os.path.join(src_dir, "job.json")))
    vm = f"{TD}/vm/{vm_id}"
    job.update({"vm_id": vm_id, "vm_spiffe_id": vm, "agent_spiffe_id": f"{vm}/agent/tau2"})
    json.dump(job, open(os.path.join(out_dir, "job.json"), "w"), indent=1)
    json.dump(json.load(open(os.path.join(src_dir, "result.json"))), open(os.path.join(out_dir, "result.json"), "w"))
    boot = guest_ms(sim["start_time"]) - 6000
    lib.write_jsonl(os.path.join(out_dir, "events.jsonl"), legacy_ledger_for(sim, job["run"], vm_id, host, boot))
    return job


def skewed_run_dir(src_dir, out_dir, skew_s=3600):
    """Same run, guest clock off by skew_s: every harness timestamp shifted; host ledger untouched."""
    os.makedirs(out_dir, exist_ok=True)
    j = json.load(open(os.path.join(src_dir, "result.json")))
    import datetime as dt

    def shift(s):
        d = dt.datetime.strptime(s[:26], "%Y-%m-%dT%H:%M:%S.%f") if "." in s else dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        return (d + dt.timedelta(seconds=skew_s)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    for sim in j["simulations"]:
        for k in ("start_time", "end_time", "timestamp"):
            if sim.get(k):
                sim[k] = shift(sim[k])
        for m in sim["messages"]:
            m["timestamp"] = shift(m["timestamp"])
    json.dump(j, open(os.path.join(out_dir, "result.json"), "w"))
    for n in ("job.json", "events.jsonl"):
        open(os.path.join(out_dir, n), "w").write(open(os.path.join(src_dir, n)).read())


def completeness_fixture(path, n=10_000, writers=4, drop=None, seed=7):
    """n events over `writers` contiguous streams; optionally drop one line (index in file order)."""
    rnd = random.Random(seed)
    S = Stream("h1")
    vm_id = "vm-02-c0c0c0"
    vm = f"{TD}/vm/{vm_id}"
    ag = f"{vm}/agent/w"
    t = 1_792_100_000_000
    S.ev("vm.boot", t, "vm", vm, vm_id, vm="vm-02", boot_ms=4000, disk_ms=1000, runtime="firecracker", mem_mib=2048)
    S.ev("agent.spawn", t + 500, "registrar", ag, vm_id, spiffe_id=ag, parent=vm, name="w", agent_id="ag_c0c0", uid=20001)
    names = [f"w{i}" for i in range(writers)]
    t += 600
    for i in range(n - 2):
        t += rnd.randint(1, 5)
        S.ev("authz.decision", t, names[i % writers], ag, vm_id, action="tool.call", resource='Tool::"refund_limit"', args={},
             decision="deny", policy_ids=["default-deny"], errors=[])
    rows = S.done()
    dropped = None
    if drop is not None:
        dropped = rows.pop(drop)
    lib.write_jsonl(path, rows)
    return dropped


def multiwriter_fixture(path):
    """Two hosts, two VMs; the file is written writer by writer (not time-sorted); host B's harness clock is skewed.
    Returns the principal to audit and the expected ordered event ids."""
    S = Stream("hA")
    T = Stream("hB")
    rows = []
    t = 1_792_200_000_000
    for S_, vm_id, dt_ in ((S, "vm-01-a0a0a0", 0), (T, "vm-01-b0b0b0", 7)):
        vm = f"{TD}/vm/{vm_id}"
        ag = f"{vm}/agent/tau2"
        S_.ev("vm.boot", t + dt_, "vm", vm, vm_id, vm="vm-01", boot_ms=3000, disk_ms=1000, runtime="firecracker", mem_mib=2048)
        S_.ev("agent.spawn", t + dt_ + 300, "registrar", ag, vm_id, spiffe_id=ag, parent=vm, name="tau2", agent_id="ag_" + vm_id[-4:], uid=20001)
        for i in range(30):
            base = t + dt_ + 1000 + i * 100
            d = S_.ev("authz.decision", base, "gateway", ag, vm_id, action="llm.call", resource=f'Model::"{MODEL}"', args={},
                      decision="allow", policy_ids=["llm-all"], errors=[])
            S_.ev("llm.call", base + 50, "gateway", ag, vm_id, authz_ref=d["event_id"], model=MODEL, status=200, ms=50,
                  prompt_tokens=100 + i, completion_tokens=10, dropped=False, price=PRICE, cost_usd=cost(100 + i, 10),
                  guest_ts=lib.ms_ts(base + 50 + (3_600_000 if dt_ else -250)))
            if i % 10 == 5:
                S_.ev("halt", base + 60, "ctl", ag, vm_id, receipt=f"hr_{i:04x}", action="freeze", by="ubuntu", target=ag, nodes=[ag], ms=5.0)
                S_.ev("resume", base + 80, "ctl", ag, vm_id, receipt=f"hr_{i + 1:04x}", action="thaw", by="ubuntu", target=ag, nodes=[ag], ms=4.0)
        S_.ev("agent.exit", t + dt_ + 9000, "registrar", ag, vm_id, spiffe_id=ag, exit_reason="exit 0", agent_id="ag_" + vm_id[-4:])
        S_.ev("vm.exit", t + dt_ + 9500, "vm", vm, vm_id, vm="vm-01")
    for S_ in (S, T):
        byw = {}
        for r in S_.rows:
            byw.setdefault(r["writer"], []).append(r)
        for w in sorted(byw):
            rows.extend(byw[w])
    lib.chain(rows)
    lib.write_jsonl(path, rows)
    ag_b = f"{TD}/vm/vm-01-b0b0b0/agent/tau2"
    exp = sorted([r for r in rows if r["principal"] == ag_b], key=lib.order_key)
    return ag_b, [r["event_id"] for r in exp]


def economics_fixture(path):
    """One run with mixed llm.calls (one failed, one unpriced model) and the exact expected cost."""
    S = Stream("h9")
    vm_id, run = "vm-03-ec0ec0", "run_retail-3_2"
    vm = f"{TD}/vm/{vm_id}"
    ag = f"{vm}/agent/tau2"
    t = 1_792_300_000_000
    S.ev("vm.boot", t, "vm", vm, vm_id, vm="vm-03", boot_ms=5000, disk_ms=1000, runtime="firecracker", mem_mib=2048)
    S.ev("session.open", t + 10, "job", vm, vm_id, run=run, harness="tau2", task="retail/3")
    S.ev("agent.spawn", t + 300, "registrar", ag, vm_id, spiffe_id=ag, parent=vm, name="tau2", agent_id="ag_ec00", uid=20001)
    calls = [(428, 86, 200, MODEL, PRICE), (4981, 24, 200, MODEL, PRICE), (None, None, 502, MODEL, PRICE),
             (7000, 300, 200, "other/model", None), (546, 43, 200, MODEL, PRICE)]
    exp_cost, pt_sum, ct_sum, priced = 0.0, 0, 0, 0
    for i, (pt, ct, st, model, price) in enumerate(calls):
        base = t + 1000 + i * 1000
        d = S.ev("authz.decision", base, "gateway", ag, vm_id, action="llm.call", resource=f'Model::"{model}"', args={},
                 decision="allow", policy_ids=["llm-all"], errors=[])
        c = cost(pt, ct, price) if price else None
        S.ev("llm.call", base + 500, "gateway", ag, vm_id, run=run, authz_ref=d["event_id"], model=model, status=st, ms=500,
             prompt_tokens=pt, completion_tokens=ct, dropped=False, price=price, cost_usd=c)
        if c is not None:
            exp_cost += c
            priced += 1
        pt_sum += pt or 0
        ct_sum += ct or 0
    S.ev("label", t + 7000, "job", ag, vm_id, run=run, labels={"reward": 0.0, "termination_reason": "max_steps"}, source="tau2")
    S.ev("agent.exit", t + 7100, "registrar", ag, vm_id, spiffe_id=ag, exit_reason="exit 0", agent_id="ag_ec00")
    S.ev("session.close", t + 7200, "job", vm, vm_id, run=run, status="done", wall_ms=7200)
    S.ev("vm.exit", t + 7300, "vm", vm, vm_id, vm="vm-03")
    lib.write_jsonl(path, S.done())
    return {"run": run, "vm_id": vm_id, "host": "h9", "llm_calls": len(calls), "priced_calls": priced,
            "prompt_tokens": pt_sum, "completion_tokens": ct_sum, "cost_usd": round(exp_cost, 9), "wall_ms": 7200}


def serialized_size(rows):
    return sum(len(json.dumps(r, separators=(",", ":")).encode()) + 1 for r in rows)


def same_size(rows_a, rows_b, pad_key="pad"):
    """Pad the smaller trace (an extra string field on its first event; unknown fields are allowed by the
    contract) so both serialize to the same byte count. Both are re-chained. Returns (rows_a, rows_b)."""
    for rows in (rows_a, rows_b):
        rows[0][pad_key] = "x" * 16
        lib.chain(rows)
    a, b = serialized_size(rows_a), serialized_size(rows_b)
    small = rows_a if a < b else rows_b
    small[0][pad_key] += "x" * abs(a - b)
    lib.chain(small)
    assert serialized_size(rows_a) == serialized_size(rows_b), (serialized_size(rows_a), serialized_size(rows_b))
    return rows_a, rows_b


def rewrite_in_place(path, rows):
    """Overwrite the file's content keeping path, inode, byte size and mtime (and atime)."""
    st = os.stat(path)
    with open(path, "r+") as f:
        f.truncate(0)
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(path).st_size == st.st_size, (os.stat(path).st_size, st.st_size)
    assert os.stat(path).st_mtime_ns == st.st_mtime_ns


def swap_principals_text(rows, a, b):
    """A consistent renaming a<->b over the whole trace (same lengths), re-chained: a valid, different trace of
    identical byte size in which principal a now owns what b owned."""
    assert len(a) == len(b)
    txt = json.dumps(rows)
    txt = txt.replace(a, "\u0000").replace(b, a).replace("\u0000", b)
    return lib.chain(json.loads(txt))


def move_pair(rows, src, dst, nth=3):
    """Move the nth (authz.decision, llm.call) pair of principal src to principal dst (same-length ids and
    vm_ids), keeping validity; re-chained. Returns the moved event ids."""
    src_vm = next(r["vm_id"] for r in rows if r["principal"] == src)
    dst_vm = next(r["vm_id"] for r in rows if r["principal"] == dst)
    assert len(src_vm) == len(dst_vm)
    k, moved = 0, []
    for i, r in enumerate(rows):
        if r["principal"] == src and r["kind"] == "authz.decision" and r["action"] == "llm.call":
            k += 1
            if k == nth:
                call = next(x for x in rows[i + 1:] if x.get("authz_ref") == r["event_id"])
                for x in (r, call):
                    x["principal"], x["vm_id"] = dst, dst_vm
                    moved.append(x["event_id"])
                break
    assert len(moved) == 2
    return lib.chain(rows)


def economics_variant(path, shift):
    """Economics fixture with every token count changed (same run, same shape); expected cost returned."""
    S = Stream("h9")
    vm_id, run = "vm-03-ec0ec0", "run_retail-3_2"
    vm = f"{TD}/vm/{vm_id}"
    ag = f"{vm}/agent/tau2"
    t = 1_792_300_000_000
    S.ev("vm.boot", t, "vm", vm, vm_id, vm="vm-03", boot_ms=5000, disk_ms=1000, runtime="firecracker", mem_mib=2048)
    S.ev("session.open", t + 10, "job", vm, vm_id, run=run, harness="tau2", task="retail/3")
    S.ev("agent.spawn", t + 300, "registrar", ag, vm_id, spiffe_id=ag, parent=vm, name="tau2", agent_id="ag_ec00", uid=20001)
    exp = 0.0
    pts = cts = 0
    for i, (pt, ct) in enumerate(((428, 86), (4981, 24), (546, 43), (1209, 11))):
        pt, ct = pt + shift, ct + shift
        base = t + 1000 + i * 1000
        d = S.ev("authz.decision", base, "gateway", ag, vm_id, action="llm.call", resource=f'Model::"{MODEL}"', args={},
                 decision="allow", policy_ids=["llm-all"], errors=[])
        S.ev("llm.call", base + 500, "gateway", ag, vm_id, run=run, authz_ref=d["event_id"], model=MODEL, status=200, ms=500,
             prompt_tokens=pt, completion_tokens=ct, dropped=False, price=PRICE, cost_usd=cost(pt, ct))
        exp += cost(pt, ct)
        pts += pt
        cts += ct
    S.ev("agent.exit", t + 7100, "registrar", ag, vm_id, spiffe_id=ag, exit_reason="exit 0", agent_id="ag_ec00")
    S.ev("session.close", t + 7200, "job", vm, vm_id, run=run, status="done", wall_ms=7200)
    S.ev("vm.exit", t + 7300, "vm", vm, vm_id, vm="vm-03")
    rows = S.done()
    want = {"run": run, "vm_id": vm_id, "host": "h9", "llm_calls": 4, "priced_calls": 4, "prompt_tokens": pts,
            "completion_tokens": cts, "cost_usd": round(exp, 9), "wall_ms": 7200}
    if path:
        lib.write_jsonl(path, rows)
    return rows, want


def big_fixture(path, n, hosts=4, seed=11):
    """n contract events whose kind mix, principal shapes and sizes follow the real POC 4-5 ledger
    (authz.decision ~73%, tool.call ~10%, llm.call ~7%, lifecycle and control the rest), spread over
    `hosts` hosts x 3 writers each. Streams are written per host in time order (as a host log would be).
    Returns a principal with a moderate number of events and its expected audit output (ids in order)."""
    rnd = random.Random(seed)
    t0 = 1_792_400_000_000
    per_host = n // hosts
    heads, seqs = {}, {}
    sample_p, sample_rows = None, []
    with open(path, "w") as out:
        for h in range(hosts):
            host = f"host{h:02d}"
            t = t0
            vm_n = 0
            left = per_host
            while left > 0:
                vm_n += 1
                vm_id = f"vm-{vm_n % 32 + 1:02d}-{rnd.randrange(16**6):06x}"
                vm = f"{TD}/vm/{vm_id}"
                agents = [f"{vm}/agent/{nm}" for nm in ("support", "refund-manager")[: rnd.choice((1, 2))]]
                if rnd.random() < 0.4:
                    agents.append(f"{agents[0]}/sub/search")
                run = f"run_retail-{rnd.randrange(114)}_{rnd.randrange(8)}"
                rows = []
                seq_at_start = dict(seqs)

                def ev(kind, ts, writer, principal, **fx):
                    w = f"{host}/{writer}"
                    s = seqs[w] = seqs.get(w, 0) + 1
                    r = {"event_id": f"{host}.{writer}.{s:010d}", "kind": kind, "ts": lib.ms_ts(ts), "writer": w, "seq": s,
                         "host": host, "principal": principal, "vm_id": vm_id}
                    r.update(fx)
                    rows.append(r)
                    return r
                ev("vm.boot", t, "vm", vm, vm="vm-%02d" % (vm_n % 32 + 1), boot_ms=rnd.randint(2500, 6500), disk_ms=2000,
                   runtime="firecracker", mem_mib=2048)
                ev("session.open", t + 8, "job", vm, run=run, harness="tau2", task="retail/0")
                for i, a in enumerate(agents):
                    parent = a.rsplit("/sub/", 1)[0] if "/sub/" in a else vm
                    ev("agent.spawn", t + 300 + i * 50, "registrar", a, spiffe_id=a, parent=parent, name=a.rsplit("/", 1)[1],
                       agent_id=f"ag_{rnd.randrange(65536):04x}", uid=20001 + i)
                t += 500
                n_calls = rnd.randint(20, 90)
                for _ in range(n_calls):
                    a = rnd.choice(agents)
                    t += rnd.randint(1, 60)
                    x = rnd.random()
                    if x < 0.80:
                        amt = rnd.choice((100, 15000, 48000, 120000))
                        allow = amt <= 20000 and rnd.random() < 0.3
                        d = ev("authz.decision", t, "gateway", a, action="tool.call", resource='Tool::"issue_refund"',
                               args={"order_id": f"#W{rnd.randrange(10**7)}", "amount_cents": amt},
                               decision="allow" if allow else "deny", policy_ids=["refund-200" if allow else "default-deny"], errors=[])
                        if allow:
                            tc = ev("tool.call", t + 1, "gateway", a, run=run, authz_ref=d["event_id"], via="gateway",
                                    tool="issue_refund", args=d["args"])
                            ev("tool.result", t + 2, "gateway", a, run=run, call_ref=tc["event_id"],
                               result={"refund_id": f"rf_{rnd.randrange(16**6):06x}", **d["args"]}, error=False)
                    elif x < 0.97:
                        d = ev("authz.decision", t, "gateway", a, action="llm.call", resource=f'Model::"{MODEL}"', args={},
                               decision="allow", policy_ids=["llm-all"], errors=[])
                        pt, ct = rnd.randint(400, 9500), rnd.randint(2, 300)
                        dur = rnd.randint(400, 2600)
                        t += dur
                        ev("llm.call", t, "gateway", a, run=run, authz_ref=d["event_id"], model=MODEL, status=200, ms=dur - 10,
                           prompt_tokens=pt, completion_tokens=ct, dropped=False, role="assistant", price=PRICE, cost_usd=cost(pt, ct))
                    else:
                        r = f"hr_{rnd.randrange(65536):04x}"
                        ev("halt", t, "ctl", a, receipt=r, action="freeze", by="ubuntu", target=a, nodes=[a], ms=7.0, freeze_ms=0.1)
                        ev("resume", t + 1000, "ctl", a, receipt=f"hr_{rnd.randrange(65536):04x}", action="thaw", by="ubuntu",
                           target=a, nodes=[a], ms=6.0)
                        t += 1000
                t += 500
                for i, a in enumerate(reversed(agents)):
                    ev("agent.exit", t + i * 20, "registrar", a, spiffe_id=a, exit_reason="exit 0", agent_id="ag_0000")
                ev("label", t + 200, "job", agents[0], run=run, labels={"reward": float(rnd.random() < 0.6), "termination_reason": "user_stop"}, source="tau2")
                ev("session.close", t + 300, "job", vm, run=run, status="done", wall_ms=t + 300 - (t0 if vm_n == 1 else 0))
                ev("vm.exit", t + 400, "vm", vm, vm="vm-01")
                t += 1000
                rows.sort(key=lambda r: (r["ts"], r["writer"], r["seq"]))
                remap = {}
                for r in rows:
                    w = r["writer"]
                    seq_at_start[w] = seq_at_start.get(w, 0) + 1
                    new_id = f"{host}.{w.split('/', 1)[1]}.{seq_at_start[w]:010d}"
                    remap[r["event_id"]] = new_id
                    r["seq"], r["event_id"] = seq_at_start[w], new_id
                for r in rows:
                    for fld in ("authz_ref", "call_ref"):
                        if fld in r:
                            r[fld] = remap[r[fld]]
                    w = r["writer"]
                    r["prev"] = heads.get(w, lib.GENESIS)
                    r["hash"] = lib.ev_hash(r)
                    heads[w] = r["hash"]
                    out.write(json.dumps(r, separators=(",", ":")) + "\n")
                left -= len(rows)
                if sample_p is None and h == hosts - 1 and vm_n >= 2 and len(agents) > 1:
                    sample_p = agents[1]
                    sample_rows = [r for r in rows if r["principal"] == sample_p]
    sample_rows.sort(key=lib.order_key)
    return sample_p, [r["event_id"] for r in sample_rows]


if __name__ == "__main__":
    if sys.argv[1:] == ["golden"]:
        golden(os.path.join(lib.FIX, "golden"))
        mini_run(os.path.join(lib.FIX, "golden", "mini_run"))
        print("golden fixtures written")
