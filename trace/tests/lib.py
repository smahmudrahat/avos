"""Shared helpers and independent oracles for the POC 6 acceptance suite.

Nothing here knows how the solver works. Every function either reads files, runs a CLI
from trace/CONTRACT.md as a black box, or computes an *expected* answer from raw data.
Python 3.10+ standard library only.
"""
import hashlib
import json
import os
import re
import resource
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
TRACE = os.path.join(ROOT, "trace")
BIN = os.path.join(TRACE, "bin")
FIX = os.path.join(HERE, "fixtures")
SCHEMA = os.path.join(TRACE, "schema.json")

KINDS = {"vm.boot", "vm.exit", "session.open", "session.close", "agent.spawn", "agent.exit", "llm.call",
         "tool.call", "tool.result", "fs.write", "net.connect", "authz.decision", "halt", "resume", "label"}
TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
RECEIPT_RE = re.compile(r"^hr_[0-9a-f]{4}$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
GENESIS = "0" * 64


class NotImplementedCLI(Exception):
    """A contract path is missing: the check reports FAIL 'not implemented: <path>'."""


def read_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def ts_ms(ts):
    """Strict contract timestamp -> epoch milliseconds (3.10-safe; no fromisoformat on 'Z')."""
    if not TS_RE.match(ts):
        raise ValueError(f"bad ts {ts!r}")
    import calendar
    t = time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    return calendar.timegm(t) * 1000 + int(ts[20:23])


def ms_ts(ms):
    ms = int(round(ms))
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + f".{ms % 1000:03d}Z"


def order_key(ev):
    """Total order fixed by the contract: host ts, then writer, then seq."""
    return (ev["ts"], ev["writer"], ev["seq"])


def canon(ev):
    d = {k: v for k, v in ev.items() if k != "hash"}
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def ev_hash(ev):
    return hashlib.sha256(canon(ev).encode()).hexdigest()


def chain(rows):
    """Fill prev/hash in place for a list of contract events, per writer stream, in list order."""
    heads = {}
    for r in rows:
        r.pop("hash", None)
        r["prev"] = heads.get(r["writer"], GENESIS)
        r["hash"] = ev_hash(r)
        heads[r["writer"]] = r["hash"]
    return rows


def chain_check(rows):
    """Independent chain verifier. Returns the first problem as (index, code) or None."""
    heads = {}
    for i, r in enumerate(rows):
        w = r.get("writer")
        if r.get("prev") != heads.get(w, GENESIS):
            return i, "prev"
        if r.get("hash") != ev_hash(r):
            return i, "hash"
        heads[w] = r["hash"]
    return None


def cli_path(name):
    return os.path.join(BIN, name)


def run_cli(name, *args, stdin=None, env=None, cwd=None, timeout=900, rss=False):
    """Run trace/bin/<name> as a black box. Returns dict(rc, out, err, secs, maxrss_mib)."""
    path = cli_path(name)
    if not os.path.exists(path):
        raise NotImplementedCLI(os.path.relpath(path, ROOT))
    cmd = [sys.executable, path, *map(str, args)]
    e = dict(os.environ)
    e.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if env:
        e.update(env)
    stats = None
    if rss:
        stats = os.path.join(cwd or HERE, f".rss.{os.getpid()}.{time.time_ns()}.json")
        wrapper = ("import json,resource,subprocess,sys;r=subprocess.run(sys.argv[2:]);"
                   "json.dump({'maxrss':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss},open(sys.argv[1],'w'));"
                   "sys.exit(r.returncode)")
        cmd = [sys.executable, "-c", wrapper, stats, *cmd]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=e, cwd=cwd, timeout=timeout)
    secs = time.perf_counter() - t0
    res = {"rc": p.returncode, "out": p.stdout, "err": p.stderr, "secs": secs, "maxrss_mib": None}
    if stats and os.path.exists(stats):
        raw = json.load(open(stats))["maxrss"]
        os.unlink(stats)
        res["maxrss_mib"] = raw / 2**20 if sys.platform == "darwin" else raw / 1024
    return res


def cli_json(res):
    try:
        return json.loads(res["out"])
    except ValueError:
        raise AssertionError(f"stdout is not JSON (rc={res['rc']}): {res['out'][:200]!r} stderr={res['err'][:200]!r}")


def cli_ndjson(res):
    try:
        return [json.loads(l) for l in res["out"].splitlines() if l.strip()]
    except ValueError:
        raise AssertionError(f"stdout is not NDJSON: {res['out'][:200]!r}")


def legacy_principal(r):
    """Principal of a POC 1-5 ledger row, as the tests understand it."""
    k = r["kind"]
    if k in ("vm.boot", "vm.exit", "agent.spawn", "agent.exit"):
        return r.get("spiffe_id")
    if k in ("session.open", "session.close"):
        return r.get("vm_spiffe_id")
    if k in ("halt", "resume"):
        return r.get("target")
    return r.get("principal")


def ledger(events, legacy=False):
    """Birth/death table {principal: [birth_ms, death_ms|None, parent|None]} from events.

    Contract rule: a VM is alive from vm.boot.ts - boot_ms (the create request) to vm.exit.ts;
    an agent from agent.spawn.ts to agent.exit.ts. Open-ended if no exit."""
    L = {}
    for r in events:
        k = r["kind"]
        p = legacy_principal(r) if legacy else r.get("principal")
        if k == "vm.boot" and p:
            L.setdefault(p, [None, None, None])[0] = ts_ms(r["ts"]) - int(r.get("boot_ms") or 0)
        elif k == "agent.spawn" and p:
            e = L.setdefault(p, [None, None, None])
            e[0] = ts_ms(r["ts"])
            e[2] = r.get("parent")
        elif k in ("vm.exit", "agent.exit") and p:
            L.setdefault(p, [None, None, None])[1] = ts_ms(r["ts"])
    return L


def alive(L, p, ts):
    e = L.get(p)
    if not e or e[0] is None:
        return None
    t = ts_ms(ts)
    return e[0] <= t and (e[1] is None or t <= e[1])


LIVENESS_EXEMPT = {"vm.boot", "vm.exit", "agent.exit", "session.close", "label"}


def liveness_exempt(r):
    """vm.boot creates the principal; exit/close/label are written after it ended; a control-plane deny
    that carries a receipt (an in-flight answer refused after halt/kill/cut) is evidence, not an action."""
    if r["kind"] in LIVENESS_EXEMPT:
        return True
    return r["kind"] == "authz.decision" and r.get("decision") == "deny" \
        and any(isinstance(x, str) and x.startswith("hr_") for x in r.get("policy_ids", []))


def start_ts(r):
    """When the event began, host clock: an llm.call row is written at completion, so its start is ts - ms."""
    if r["kind"] == "llm.call" and isinstance(r.get("ms"), (int, float)) and TS_RE.match(r.get("ts", "")):
        return ms_ts(ts_ms(r["ts"]) - int(r["ms"]))
    return r.get("ts")


def alive_for(L, r, principal=None):
    """None = principal unknown; True/False = alive at the event's start (or exempt)."""
    p = principal or r.get("principal")
    a = alive(L, p, start_ts(r))
    if a is None:
        return None
    return True if liveness_exempt(r) else a


def lineage(L, p):
    out = [p]
    while True:
        e = L.get(out[-1])
        if not e or not e[2]:
            return out
        out.append(e[2])


def pair_tool_calls(rows, legacy=True):
    """FIFO pairing of allow decisions to tool.calls: {tool.call index: decision index}. Oracle for authz_ref."""
    pending, out = {}, {}
    for i, r in enumerate(rows):
        if r["kind"] == "authz.decision" and r["action"] == "tool.call" and r["decision"] == "allow":
            key = (r["principal"], r["resource"], json.dumps(r["args"], sort_keys=True))
            pending.setdefault(key, []).append(i)
        elif r["kind"] == "tool.call" and (legacy or r.get("via") == "gateway"):
            key = (r["principal"], "Tool::" + json.dumps(r["tool"]), json.dumps(r["args"], sort_keys=True))
            q = pending.get(key) or []
            out[i] = q.pop(0) if q else None
    return out


def pair_llm_calls(rows):
    """FIFO pairing of allow decisions to llm.calls, and the receipt a dropped call must carry."""
    pending, last_deny, out, receipts = {}, {}, {}, {}
    for i, r in enumerate(rows):
        if r["kind"] == "authz.decision" and r["action"] == "llm.call":
            if r["decision"] == "allow":
                pending.setdefault((r["principal"], r["resource"]), []).append(i)
            else:
                last_deny[r["principal"]] = r
        elif r["kind"] == "llm.call":
            q = pending.get((r["principal"], "Model::" + json.dumps(r["model"]))) or []
            out[i] = q.pop(0) if q else None
            if r.get("dropped"):
                d = last_deny.get(r["principal"])
                receipts[i] = next((x for x in (d or {}).get("policy_ids", []) if x.startswith("hr_")), None)
    return out, receipts


def tau2_sim(result_path):
    j = json.load(open(result_path))
    return j["simulations"][0]


def tau2_norm_messages(sim):
    """SimulationRun messages -> comparable tuples. Built from the harness file only."""
    out = []
    for m in sim["messages"]:
        role = m["role"]
        if role == "tool":
            out.append(("tool", m.get("id"), m.get("content"), bool(m.get("error"))))
        else:
            tcs = tuple((t["id"], t["name"], json.dumps(t["arguments"], sort_keys=True))
                        for t in (m.get("tool_calls") or [])) or None
            u = m.get("usage")
            usage = (u["prompt_tokens"], u["completion_tokens"]) if u else None
            out.append((role, m.get("content"), tcs, usage))
    return out


def chat_norm_messages(chat):
    """Projection output (OpenAI Chat Completions message format) -> the same tuples."""
    out = []
    for m in chat["messages"]:
        role = m["role"]
        if role == "tool":
            out.append(("tool", m.get("tool_call_id"), m.get("content"), bool(m.get("error", False))))
        else:
            tcs = []
            for t in m.get("tool_calls") or []:
                fn = t.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    args = json.loads(args)
                tcs.append((t.get("id"), fn.get("name"), json.dumps(args, sort_keys=True)))
            u = m.get("usage")
            usage = (u["prompt_tokens"], u["completion_tokens"]) if u else None
            out.append((role, m.get("content"), tuple(tcs) or None, usage))
    return out


def tau2_usage_seq(sim):
    return [(m["usage"]["prompt_tokens"], m["usage"]["completion_tokens"]) for m in sim["messages"] if m.get("usage")]


def tau2_tool_calls(sim):
    return [t for m in sim["messages"] if m["role"] == "assistant" for t in (m.get("tool_calls") or [])]


def first_diff(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"index {i}: expected {x!r}, got {y!r}"
    if len(a) != len(b):
        return f"length: expected {len(a)}, got {len(b)}"
    return None


def percentile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def cpu_seconds():
    return resource.getrusage(resource.RUSAGE_SELF).ru_utime


COMMON = {"event_id": str, "kind": str, "ts": str, "writer": str, "seq": int, "host": str, "principal": str,
          "vm_id": str, "prev": str, "hash": str}
PER_KIND = {
    "vm.boot": {"boot_ms": int, "vm": str}, "vm.exit": {},
    "session.open": {"run": str, "harness": str, "task": str}, "session.close": {"run": str, "status": str, "wall_ms": int},
    "agent.spawn": {"spiffe_id": str, "parent": str, "name": str}, "agent.exit": {"spiffe_id": str, "exit_reason": str},
    "llm.call": {"authz_ref": str, "model": str, "status": int, "ms": int, "dropped": bool},
    "tool.call": {"via": str, "tool": str}, "tool.result": {"call_ref": str}, "fs.write": {"path": str},
    "net.connect": {"authz_ref": str, "dest": str},
    "authz.decision": {"action": str, "resource": str, "args": dict, "decision": str, "policy_ids": list},
    "halt": {"receipt": str, "action": str, "by": str, "target": str, "nodes": list},
    "resume": {"receipt": str, "action": str, "by": str, "target": str, "nodes": list},
    "label": {"run": str, "labels": dict},
}
AUTHZ_ACTION = {"llm.call": "llm.call", "tool.call": "tool.call", "net.connect": "net.connect"}


def lint(rows):
    """Apply the contract's validation rules independently. Returns [(index, code, message)].
    This is the suite's oracle; it is deliberately simple and complete rather than fast."""
    errs = []
    E = lambda i, c, m: errs.append((i, c, m))
    seen_ids, last_ts, last_seq, by_id = {}, {}, {}, {}
    L = ledger(rows)
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            E(i, "E_JSON", "not an object")
            continue
        k = r.get("kind")
        bad_schema = False
        for f, t in COMMON.items():
            if f not in r:
                E(i, "E_SCHEMA", f"missing {f}")
                bad_schema = True
            elif not isinstance(r[f], t) or (t is int and isinstance(r[f], bool)):
                E(i, "E_SCHEMA", f"{f} must be {t.__name__}")
                bad_schema = True
        if k not in KINDS:
            E(i, "E_KIND", f"unknown kind {k!r}")
            continue
        for f, t in PER_KIND[k].items():
            if f not in r or not isinstance(r[f], t) or (t is int and isinstance(r[f], bool)):
                E(i, "E_SCHEMA", f"{k} needs {f}: {t.__name__}")
        if bad_schema:
            continue
        eid = r["event_id"]
        if not ID_RE.match(eid):
            E(i, "E_SCHEMA", "event_id charset/length")
        if eid in seen_ids:
            E(i, "E_DUP_ID", f"event_id {eid} already used at line {seen_ids[eid] + 1}")
        seen_ids.setdefault(eid, i)
        by_id[eid] = r
        if not TS_RE.match(r["ts"]):
            E(i, "E_TS", f"ts {r['ts']!r} is not YYYY-MM-DDTHH:MM:SS.mmmZ")
            continue
        w = r["writer"]
        if w in last_ts and r["ts"] < last_ts[w]:
            E(i, "E_TS_ORDER", f"ts {r['ts']} precedes {last_ts[w]} earlier in stream {w}")
        last_ts[w] = r["ts"]
        if w in last_seq and r["seq"] <= last_seq[w]:
            E(i, "E_SEQ_ORDER", f"seq {r['seq']} not after seq {last_seq[w]} earlier in stream {w}")
        last_seq[w] = r["seq"]
        p = r["principal"]
        if k == "agent.spawn":
            if r.get("parent") not in L:
                E(i, "E_REF", f"parent {r.get('parent')} not in ledger")
        else:
            a = alive_for(L, r)
            if a is None:
                E(i, "E_PRINCIPAL_UNKNOWN", f"principal {p} has no vm.boot/agent.spawn")
            elif not a:
                E(i, "E_PRINCIPAL_DEAD", f"principal {p} not alive at {start_ts(r)}")
        if k in ("halt", "resume"):
            if not RECEIPT_RE.match(str(r.get("receipt", ""))):
                E(i, "E_RECEIPT", f"{k} without hr_xxxx receipt")
            if r.get("target") != p or p not in r.get("nodes", []):
                E(i, "E_SCHEMA", "halt/resume target must equal principal and be in nodes")
        if k == "llm.call":
            if r["status"] == 200 and not all(isinstance(r.get(f), int) and not isinstance(r.get(f), bool)
                                              for f in ("prompt_tokens", "completion_tokens")):
                E(i, "E_TOKENS", "llm.call with status 200 needs integer prompt_tokens and completion_tokens")
            if r["dropped"] and not RECEIPT_RE.match(str(r.get("receipt", ""))):
                E(i, "E_RECEIPT", "dropped llm.call without the halt/cut receipt")
        if k == "authz.decision":
            if r["decision"] not in ("allow", "deny"):
                E(i, "E_SCHEMA", "decision must be allow|deny")
            if r["decision"] == "deny" and not r["policy_ids"]:
                E(i, "E_SCHEMA", "deny without policy_ids")
        if k in AUTHZ_ACTION and not (k == "tool.call" and r.get("via") == "harness"):
            ref = r.get("authz_ref")
            if not ref:
                E(i, "E_AUTHZ_MISSING", f"{k} without authz_ref")
            else:
                d = by_id.get(ref)
                if not d or d.get("kind") != "authz.decision":
                    E(i, "E_REF", f"authz_ref {ref} is not an earlier authz.decision")
                elif d["decision"] != "allow" or d["principal"] != p or d["action"] != AUTHZ_ACTION[k] or d["ts"] > r["ts"]:
                    E(i, "E_AUTHZ_DENIED", f"authz_ref {ref} did not allow this call for this principal")
        if k == "tool.call" and r.get("via") not in ("gateway", "harness"):
            E(i, "E_SCHEMA", "tool.call via must be gateway|harness")
        if k == "tool.result":
            c = by_id.get(r["call_ref"])
            if not c or c.get("kind") != "tool.call":
                E(i, "E_REF", f"call_ref {r['call_ref']} is not an earlier tool.call")
    return errs


def lint_codes(rows):
    return {(rows[i].get("event_id") if isinstance(rows[i], dict) else None, c) for i, c, _ in lint(rows)}


def authority_oracle(r, by_id):
    k = r["kind"]
    if k in AUTHZ_ACTION:
        if k == "tool.call" and r.get("via") == "harness":
            return {"via": "harness"}
        d = by_id.get(r.get("authz_ref")) or {}
        a = {"authz_ref": r.get("authz_ref"), "decision": d.get("decision"), "policy_ids": d.get("policy_ids")}
        if r.get("receipt"):
            a["receipt"] = r["receipt"]
        return a
    if k == "authz.decision":
        return {"decision": r["decision"], "policy_ids": r["policy_ids"]}
    if k in ("halt", "resume"):
        return {"receipt": r["receipt"], "by": r["by"]}
    return {}


def audit_oracle(rows, principal):
    by_id = {r["event_id"]: r for r in rows}
    sel = [r for r in rows if r["principal"] == principal
           or (r["kind"] in ("halt", "resume") and principal in r.get("nodes", []))]
    sel.sort(key=order_key)
    return [(r["event_id"], authority_oracle(r, by_id)) for r in sel]


def authority_matches(expected, got):
    return isinstance(got, dict) and all(got.get(k) == v for k, v in expected.items())


def cost_oracle(rows, run):
    """Per-run economics from the trace alone: tokens x price, wall time, host attribution."""
    calls = [r for r in rows if r["kind"] == "llm.call" and r.get("run") == run]
    close = next((r for r in rows if r["kind"] == "session.close" and r.get("run") == run), None)
    vm_id = close["vm_id"] if close else (calls[0]["vm_id"] if calls else None)
    boot = next((r for r in rows if r["kind"] == "vm.boot" and r["vm_id"] == vm_id), None)
    usd, priced = 0.0, 0
    for c in calls:
        p = c.get("price")
        if p and isinstance(c.get("prompt_tokens"), int) and isinstance(c.get("completion_tokens"), int):
            usd += (c["prompt_tokens"] * p["prompt_per_mtok"] + c["completion_tokens"] * p["completion_per_mtok"]) / 1e6
            priced += 1
    return {"run": run, "vm_id": vm_id, "host": boot["host"] if boot else None, "llm_calls": len(calls), "priced_calls": priced,
            "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in calls),
            "completion_tokens": sum(c.get("completion_tokens") or 0 for c in calls),
            "cost_usd": round(usd, 9), "wall_ms": close["wall_ms"] if close else None}


def cost_matches(expected, got):
    for k, v in expected.items():
        g = got.get(k)
        if k == "cost_usd":
            if g is None or abs(float(g) - v) > 1e-9:
                return f"cost_usd expected {v} got {g}"
        elif g != v:
            return f"{k} expected {v!r} got {g!r}"
    return None


def completeness_oracle(rows):
    """Per-writer contiguity: expected = last_seq - first_seq + 1."""
    w = {}
    for r in rows:
        s = w.setdefault(r["writer"], {"stored": 0, "seqs": set()})
        s["stored"] += 1
        s["seqs"].add(r["seq"])
    stored = sum(s["stored"] for s in w.values())
    expected = sum(max(s["seqs"]) - min(s["seqs"]) + 1 for s in w.values())
    gaps = [{"writer": k, "seq": q} for k, s in sorted(w.items()) for q in range(min(s["seqs"]), max(s["seqs"]) + 1) if q not in s["seqs"]]
    return {"stored": stored, "expected": expected, "gaps": gaps}
