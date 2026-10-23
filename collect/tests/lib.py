"""Oracles for the POC 7 acceptance suite: ground truth from the agents' own logs, the timeline/M8/count
checkers, the one-day event generator for M9, and small parsers. Pure functions, stdlib only, no host access, so
`run.py --selftest` can prove each one on fixtures. Nothing here reads the solver's code."""
import hashlib
import json
import random
import re
import datetime as _dt

TD = "spiffe://avoslab.com"
MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
GENESIS = "0" * 64
KINDS7 = ("authz.decision", "llm.call", "tool.call", "agent.spawn", "agent.exit", "halt", "resume")
RECEIPT = re.compile(r"hr_[0-9a-f]{4}")


def ms_ts(ms):
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ms % 1000)


def ts_ms(ts):
    d = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_dt.timezone.utc)
    return int(d.timestamp() * 1000)


def ev_hash(ev):
    e = {k: v for k, v in ev.items() if k != "hash"}
    return hashlib.sha256(json.dumps(e, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def order_key(e):
    return (e["ts"], e["writer"], e["seq"])


def is_sorted(events):
    ks = [order_key(e) for e in events]
    return all(a <= b for a, b in zip(ks, ks[1:]))


def ground_truth(records):
    """records: the JSONL records one guest_agent.py principal wrote. Returns what the agent saw."""
    gt = {"attempts": 0, "answered": 0, "exc": 0, "tool_ok": 0, "llm_ok": 0, "denied": 0, "calls": [],
          "exited": any(r.get("k") == "exit" for r in records), "spawned": [], "latency_ms": [], "uid": None}
    for r in records:
        k = r.get("k")
        if k == "meta":
            gt["uid"] = r.get("uid")
        elif k == "spawned":
            gt["spawned"].append(r)
        elif k == "call":
            gt["attempts"] += 1
            st = r.get("st")
            if isinstance(st, int) and (r["kind"] == "llm" or st in (200, 403)):
                gt["answered"] += 1
                gt["latency_ms"].append((r["t1"] - r["t0"]) * 1000)
                if st == 403:
                    gt["denied"] += 1
                elif r["kind"] == "tool":
                    gt["tool_ok"] += 1
                else:
                    gt["llm_ok"] += 1
            else:
                gt["exc"] += 1
            gt["calls"].append(r)
    gt["mine"] = {"authz.decision": gt["attempts"], "tool.call": gt["tool_ok"], "llm.call": gt["llm_ok"],
                  "agent.exit": 1 if gt["exited"] else 0}
    return gt


def call_shapes(r):
    """Shapes one answered call must leave in the store: its decision, then the call row if it was allowed."""
    st = r.get("st")
    if not isinstance(st, int) or (r["kind"] == "tool" and st not in (200, 403)):
        return [("unanswered",)]
    if r["kind"] == "tool":
        key, action = r["args"]["amount_cents"], "tool.call"
    else:
        key, action = r["args"]["model"], "llm.call"
    if st == 403:
        return [("decision", action, key, "deny")]
    return [("decision", action, key, "allow"), ("tool", key) if r["kind"] == "tool" else ("llm",)]


def expected_shapes(records, controls):
    """The whole timeline a principal must show: spawn, each call, the controls issued while it was idle, exit.
    controls: [("halt", receipt), ("resume", receipt)] issued between the idle_start and idle_end markers."""
    out = [("spawn",)]
    for r in records:
        k = r.get("k")
        if k == "call":
            out += call_shapes(r)
        elif k == "phase" and r.get("p") == "idle_start":
            out += list(controls)
        elif k == "exit":
            out.append(("exit",))
    return out


def model_of(resource):
    m = re.match(r'Model::"(.*)"$', resource or "")
    return m.group(1) if m else resource


def shape_of(e):
    k = e["kind"]
    if k == "agent.spawn":
        return ("spawn",)
    if k == "agent.exit":
        return ("exit",)
    if k == "authz.decision":
        key = e.get("args", {}).get("amount_cents") if e.get("action") == "tool.call" else model_of(e.get("resource"))
        return ("decision", e.get("action"), key, e.get("decision"))
    if k == "llm.call":
        return ("llm",)
    if k == "tool.call":
        return ("tool", e.get("args", {}).get("amount_cents"))
    if k == "tool.result":
        return None
    if k in ("halt", "resume"):
        return (k, e.get("receipt"))
    return ("other", k)


def policy_ids_from_body(body):
    """policy ids the agent saw in a 403 body, if the body is JSON and names them; else None."""
    try:
        j = json.loads(body)
    except (ValueError, TypeError):
        return None
    if isinstance(j, dict):
        for key in ("policy_ids", "policies", "policy"):
            v = j.get(key)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                return sorted(v)
            if isinstance(v, str):
                return [v]
        for v in j.values():
            if isinstance(v, dict):
                r = policy_ids_from_body(json.dumps(v))
                if r:
                    return r
    return None


def check_timeline(events, principal, records, controls):
    """events: the solver's `avos trace --principal P` lines. Returns a list of problems (empty = pass)."""
    p = []
    ids = [e.get("event_id") for e in events]
    if len(set(ids)) != len(ids):
        p.append("duplicate event_id")
    for e in events:
        for f in ("event_id", "kind", "ts", "writer", "seq", "host", "principal", "vm_id", "prev", "hash", "authority"):
            if f not in e:
                p.append("missing field %s on %s" % (f, e.get("event_id")))
                break
    if not is_sorted(events):
        p.append("not in (ts, writer, seq) order")
    for e in events:
        if e.get("principal") != principal and not (e.get("kind") in ("halt", "resume") and principal in e.get("nodes", [])):
            p.append("foreign event %s %s principal=%s" % (e.get("kind"), e.get("event_id"), e.get("principal")))
    got = [s for s in (shape_of(e) for e in events) if s is not None]
    want = expected_shapes(records, controls)
    if any(c[1] is None for c in controls):
        got = [(s[0], None) if s[0] in ("halt", "resume") else s for s in got]
    if got != want:
        i = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
        p.append("timeline differs at %d: got %s want %s (got %d shapes, want %d)" %
                 (i, got[i] if i < len(got) else None, want[i] if i < len(want) else None, len(got), len(want)))
    last_allow = {}
    denies = [r for r in records if r.get("k") == "call" and r.get("st") == 403]
    di = 0
    for e in events:
        a = e.get("authority")
        if not isinstance(a, dict):
            p.append("authority missing on %s" % e.get("event_id"))
            continue
        k = e["kind"]
        if k == "authz.decision":
            if a.get("decision") != e.get("decision") or a.get("policy_ids") != e.get("policy_ids"):
                p.append("authority of decision %s does not copy decision/policy_ids" % e["event_id"])
            if e.get("decision") == "allow":
                last_allow[e.get("action")] = e["event_id"]
            else:
                if not e.get("policy_ids"):
                    p.append("deny %s without policy_ids" % e["event_id"])
                if di < len(denies):
                    seen = policy_ids_from_body(denies[di].get("b", ""))
                    if seen is not None and seen != sorted(e.get("policy_ids") or []):
                        p.append("deny %s policy_ids %s != what the agent saw %s" % (e["event_id"], e.get("policy_ids"), seen))
                    di += 1
        elif k in ("llm.call", "tool.call"):
            if a.get("decision") != "allow" or not a.get("policy_ids"):
                p.append("%s %s authority is not an allow with policy ids" % (k, e["event_id"]))
            if a.get("authz_ref") != last_allow.get(k) or e.get("authz_ref") != last_allow.get(k):
                p.append("%s %s authz_ref %s != preceding allow %s" % (k, e["event_id"], a.get("authz_ref"), last_allow.get(k)))
            last_allow.pop(k, None)
        elif k in ("halt", "resume"):
            r = e.get("receipt")
            if not (isinstance(r, str) and RECEIPT.fullmatch(r)) or a.get("receipt") != r or not a.get("by"):
                p.append("%s %s receipt/by missing in event or authority" % (k, e["event_id"]))
            if principal not in e.get("nodes", []) or e.get("target") != principal:
                p.append("%s %s target/nodes do not name %s" % (k, e["event_id"], principal))
    return p


def check_disjoint(outputs):
    """outputs: {principal: events}. No event id of one principal's answer may appear in another's, except a
    halt/resume whose nodes cover both."""
    p = []
    owner = {}
    for pr, evs in outputs.items():
        for e in evs:
            if e.get("kind") in ("halt", "resume"):
                continue
            if e["event_id"] in owner and owner[e["event_id"]] != pr:
                p.append("event %s in answers of %s and %s" % (e["event_id"], owner[e["event_id"]], pr))
            owner[e["event_id"]] = pr
    return p


def count_kinds(events):
    c = {}
    for e in events:
        c[e["kind"]] = c.get(e["kind"], 0) + 1
    return c


def lost_events(gt, stored):
    """How many events the agent saw happen that the store does not have (0 = nothing lost for this principal)."""
    c = count_kinds(stored)
    lost = max(0, gt["attempts"] - c.get("authz.decision", 0))
    lost += max(0, gt["tool_ok"] + gt["llm_ok"] - c.get("tool.call", 0) - c.get("llm.call", 0))
    lost += max(0, (1 if gt["exited"] else 0) - c.get("agent.exit", 0))
    lost += 0 if c.get("agent.spawn", 0) else 1
    return lost


def m8_ratio(stored, happened, n_gaps):
    """Contract §4: per kind min over its sources / max over its sources, summed; the decision term also counts the
    guest's requests; one per seq gap in the denominator. 1.0 exactly when everything agrees and there are no gaps."""
    num = den = 0
    for k in KINDS7:
        src = [stored[k], happened[k]] + ([happened["guest_requests"]] if k == "authz.decision" else [])
        num += min(src)
        den += max(src)
    den += n_gaps
    return 1.0 if den == 0 else num / den


def check_m8(rec, lost, clean, mine=None):
    """rec: the solver's completeness record. lost: events we know were lost (from the agents' own logs).
    clean: this run had no injection and no unanswered call, so it must read complete and 100 %.
    mine: per-kind counts we observed for the run (compared when the record claims complete)."""
    p = []
    if not isinstance(rec, dict):
        return ["not a JSON object"]
    st, ratio = rec.get("status"), rec.get("ratio")
    stored, happened = rec.get("stored"), rec.get("happened")
    if st not in ("complete", "incomplete", "unverified"):
        p.append("status %r" % (st,))
    if not (isinstance(stored, dict) and all(isinstance(stored.get(k), int) for k in KINDS7)):
        p.append("stored lacks integer counts for %s" % (KINDS7,))
    if not (isinstance(happened, dict) and all(k in happened for k in KINDS7 + ("guest_requests",))):
        p.append("happened lacks keys %s + guest_requests" % (KINDS7,))
    if not isinstance(rec.get("gaps"), list):
        p.append("gaps missing")
    if p:
        return p
    nulls = [k for k, v in happened.items() if v is None]
    eq = all(stored[k] == happened[k] for k in KINDS7) and stored["authz.decision"] == happened["guest_requests"]
    if not nulls and ratio is not None:
        want = m8_ratio(stored, happened, len(rec["gaps"]))
        if not isinstance(ratio, (int, float)) or abs(float(ratio) - want) > 1e-9:
            p.append("ratio %r != contract formula %.9f" % (ratio, want))
    if st == "complete":
        if nulls:
            p.append("complete with null counters %s" % nulls)
        if not eq:
            p.append("complete but stored != happened")
        if ratio != 1.0 or rec["gaps"]:
            p.append("complete but ratio=%r gaps=%d" % (ratio, len(rec["gaps"])))
        if mine:
            bad = {k: (stored.get(k), v) for k, v in mine.items() if stored.get(k) != v}
            if bad:
                p.append("complete but stored != observed: %s" % bad)
    elif st == "incomplete":
        if nulls:
            p.append("incomplete with null counters (must be unverified)")
        if eq and not rec["gaps"]:
            p.append("incomplete but every counter matches")
        if ratio is None or ratio == 1.0:
            p.append("incomplete with ratio %r" % (ratio,))
    else:
        if not nulls:
            p.append("unverified with every counter present")
        if ratio is not None:
            p.append("unverified with ratio %r" % (ratio,))
    if clean and st != "complete":
        p.append("clean run reads %s" % st)
    if lost > 0 and (st == "complete" or ratio == 1.0):
        p.append("%d events lost but reads %s ratio=%r (silent loss)" % (lost, st, ratio))
    return p


def parse_check_lines(text):
    """`PASS|FAIL <id> ... measured=... bar=...` lines -> {id: (ok, line)}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"(PASS|FAIL)\s+(\S+)\s", line)
        if m:
            out[m.group(2)] = (m.group(1) == "PASS", line)
    return out


def parse_claim1_gateway(line):
    """check 7 line -> {calls, unanswered, p50, p99} (None if it does not parse)."""
    m = re.search(r"calls=(\d+)\s+agents=\d+\s+unanswered=(\d+)\s+p50=(\d+)ms\s+p99=(\d+)ms", line)
    return {"calls": int(m.group(1)), "unanswered": int(m.group(2)), "p50": int(m.group(3)), "p99": int(m.group(4))} if m else None


def percentile(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def snapshot(paths):
    """{path: (size, sha256)} of each file now (streamed; files may be large)."""
    snap = {}
    for p in paths:
        h, n = hashlib.sha256(), 0
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
                n += len(chunk)
        snap[p] = (n, h.hexdigest())
    return snap


def prefix_broken(snap):
    """Append-only: every file in an earlier snapshot still exists and its first `size` bytes hash the same.
    Returns the paths that violate this."""
    bad = []
    for p, (n, sha) in snap.items():
        try:
            h, left = hashlib.sha256(), n
            with open(p, "rb") as f:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    h.update(chunk)
                    left -= len(chunk)
            if left > 0 or h.hexdigest() != sha:
                bad.append(p)
        except OSError:
            bad.append(p)
    return bad


def gen_day(path, n, day="2026-09-01", seed=5, big_share=0.03, host="hday"):
    """Write ~n POC 6-valid events of the real kind mix on one UTC day to `path` (chained, one host, 5 writers):
    thousands of short VM lives with 1-3 principals each, plus one long-lived `support` agent that owns ~big_share
    of the day (the largest principal) and one agent with 3 events (the smallest). Returns a dict with per-principal
    event counts (so the caller can pick small/median/largest) and the generated vm_ids."""
    rnd = random.Random(seed)
    t_day = ts_ms(day + "T00:00:00.000Z")
    span = int(86400_000 * 0.95)
    seqs, heads = {}, {}
    counts = {}
    vm_ids = []

    def mk(rows, kind, ts, writer, principal, vm_id, **f):
        w = host + "/" + writer
        r = {"event_id": None, "kind": kind, "ts": ms_ts(ts), "writer": w, "seq": None, "host": host,
             "principal": principal, "vm_id": vm_id}
        r.update(f)
        rows.append(r)
        return r

    def flush(rows, out):
        rows.sort(key=lambda r: (r["ts"], r["writer"]))
        remap = {}
        for r in rows:
            w = r["writer"]
            seqs[w] = seqs.get(w, 0) + 1
            r["seq"] = seqs[w]
            new_id = "%s.%s.%010d" % (host, w.split("/", 1)[1], r["seq"])
            if r["event_id"] is not None:
                remap[r["event_id"]] = new_id
            r["event_id"] = new_id
        for r in rows:
            for f in ("authz_ref", "call_ref"):
                if f in r:
                    r[f] = remap[r[f]]
            r["prev"] = heads.get(r["writer"], GENESIS)
            r["hash"] = ev_hash(r)
            heads[r["writer"]] = r["hash"]
            counts[r["principal"]] = counts.get(r["principal"], 0) + 1
            if r["kind"] in ("halt", "resume"):
                for nd in r["nodes"]:
                    if nd != r["principal"]:
                        counts[nd] = counts.get(nd, 0) + 1
            out.write(json.dumps(r, separators=(",", ":")) + "\n")
        rows.clear()

    tmp_id = [0]

    def tid():
        tmp_id[0] += 1
        return "tmp%d" % tmp_id[0]

    def calls(rows, a, vm_id, t, k, run=None):
        """k gated actions of principal a starting at t; returns the time after them."""
        for _ in range(k):
            t += rnd.randint(1, 40)
            x = rnd.random()
            extra = {"run": run} if run else {}
            if x < 0.80:
                amt = rnd.choice((100, 15000, 48000, 120000))
                allow = amt <= 20000 and a.endswith("/agent/support") and rnd.random() < 0.5
                d = mk(rows, "authz.decision", t, "gateway", a, vm_id, action="tool.call", resource='Tool::"issue_refund"',
                       args={"order_id": "#W%d" % rnd.randrange(10 ** 7), "amount_cents": amt},
                       decision="allow" if allow else "deny", policy_ids=["refund-support" if allow else "default-deny"], errors=[])
                d["event_id"] = tid()
                if allow:
                    tc = mk(rows, "tool.call", t + 1, "gateway", a, vm_id, authz_ref=d["event_id"], via="gateway",
                            tool="issue_refund", args=d["args"], **extra)
                    tc["event_id"] = tid()
                    mk(rows, "tool.result", t + 2, "gateway", a, vm_id, call_ref=tc["event_id"],
                       result={"refund_id": "rf_%06x" % rnd.randrange(16 ** 6), **d["args"]}, error=False, **extra)
            elif x < 0.97:
                d = mk(rows, "authz.decision", t, "gateway", a, vm_id, action="llm.call", resource='Model::"%s"' % MODEL,
                       args={}, decision="allow", policy_ids=["llm-all"], errors=[])
                d["event_id"] = tid()
                dur = rnd.randint(100, 600)
                t += dur
                mk(rows, "llm.call", t, "gateway", a, vm_id, authz_ref=d["event_id"], model=MODEL, status=200, ms=dur - 5,
                   prompt_tokens=rnd.randint(300, 9000), completion_tokens=rnd.randint(2, 300), dropped=False, **extra)
            else:
                r1, r2 = "hr_%04x" % rnd.randrange(65536), "hr_%04x" % rnd.randrange(65536)
                mk(rows, "halt", t, "ctl", a, vm_id, receipt=r1, action="freeze", by="ubuntu", target=a, nodes=[a], ms=6.0)
                mk(rows, "resume", t + 300, "ctl", a, vm_id, receipt=r2, action="thaw", by="ubuntu", target=a, nodes=[a], ms=5.0)
                t += 300
        return t

    with open(path, "w") as out:
        rows = []
        big_vm_id = "vm-07-b16b16"
        big_vm = "%s/vm/%s" % (TD, big_vm_id)
        big = big_vm + "/agent/support"
        t = t_day + 1000
        mk(rows, "vm.boot", t, "vm", big_vm, big_vm_id, vm="vm-07", boot_ms=3000, disk_ms=400, runtime="firecracker", mem_mib=2048)
        mk(rows, "agent.spawn", t + 300, "registrar", big, big_vm_id, spiffe_id=big, parent=big_vm, name="support", agent_id="ag_b16b", uid=20001)
        flush(rows, out)
        vm_ids.append(big_vm_id)
        small_vm_id = "vm-03-5ma111"
        small_vm = "%s/vm/%s" % (TD, small_vm_id)
        small = small_vm + "/agent/probe"
        t = t_day + 5000
        mk(rows, "vm.boot", t, "vm", small_vm, small_vm_id, vm="vm-03", boot_ms=2800, disk_ms=400, runtime="firecracker", mem_mib=2048)
        mk(rows, "agent.spawn", t + 300, "registrar", small, small_vm_id, spiffe_id=small, parent=small_vm, name="probe", agent_id="ag_5ma1", uid=20001)
        mk(rows, "authz.decision", t + 500, "gateway", small, small_vm_id, action="tool.call", resource='Tool::"issue_refund"',
           args={"order_id": "#W1", "amount_cents": 99999}, decision="deny", policy_ids=["default-deny"], errors=[])
        mk(rows, "agent.exit", t + 700, "registrar", small, small_vm_id, spiffe_id=small, exit_reason="exit 0", agent_id="ag_5ma1")
        mk(rows, "vm.exit", t + 900, "vm", small_vm, small_vm_id, vm="vm-03")
        flush(rows, out)
        vm_ids.append(small_vm_id)

        per_block_est = 2.2 * 35 * 1.3 + 9
        n_blocks = max(1, int((n * (1 - big_share)) / per_block_est))
        big_per_block = max(1, int(n * big_share / n_blocks / 1.3))
        step = (span - 10_000) // n_blocks
        t = t_day + 10_000
        vm_n = 0
        while sum(counts.values()) < n * 0.998 and t < t_day + span - 2_000:
            vm_n += 1
            vm_id = "vm-%02d-%06x" % (vm_n % 32 + 1, rnd.randrange(16 ** 6))
            vm = "%s/vm/%s" % (TD, vm_id)
            vm_ids.append(vm_id)
            names = ["support", "refund-manager", "planner"][: rnd.choice((1, 2, 2, 3))]
            agents = ["%s/agent/%s" % (vm, nm) for nm in names]
            if rnd.random() < 0.4:
                agents.append(agents[0] + "/sub/search")
            run = "run_retail-%d_%d" % (rnd.randrange(114), rnd.randrange(8))
            tb = t
            mk(rows, "vm.boot", tb, "vm", vm, vm_id, vm=vm_id[:5], boot_ms=rnd.randint(2500, 6500), disk_ms=400, runtime="firecracker", mem_mib=2048)
            mk(rows, "session.open", tb + 8, "job", vm, vm_id, run=run, harness="tau2", task="retail/0")
            for i, a in enumerate(agents):
                parent = a.rsplit("/sub/", 1)[0] if "/sub/" in a else vm
                mk(rows, "agent.spawn", tb + 300 + i * 40, "registrar", a, vm_id, spiffe_id=a, parent=parent,
                   name=a.rsplit("/", 1)[1], agent_id="ag_%04x" % rnd.randrange(65536), uid=20001 + i)
            ends = [calls(rows, a, vm_id, tb + 500 + i * 40, rnd.randint(10, 60), run) for i, a in enumerate(agents)]
            ends.append(calls(rows, big, big_vm_id, tb + rnd.randint(0, 200), big_per_block))
            tt = max(ends)
            if any("/sub/" in a for a in agents) and rnd.random() < 0.3:
                par = agents[0]
                sb = next(a for a in agents if "/sub/" in a)
                mk(rows, "halt", tt + 10, "ctl", par, vm_id, receipt="hr_%04x" % rnd.randrange(65536), action="freeze", by="ubuntu",
                   target=par, nodes=[par, sb], ms=7.0)
                mk(rows, "resume", tt + 300, "ctl", par, vm_id, receipt="hr_%04x" % rnd.randrange(65536), action="thaw", by="ubuntu",
                   target=par, nodes=[par, sb], ms=6.0)
                tt += 300
            for i, a in enumerate(reversed(agents)):
                mk(rows, "agent.exit", tt + 100 + i * 20, "registrar", a, vm_id, spiffe_id=a, exit_reason="exit 0", agent_id="ag_0000")
            mk(rows, "label", tt + 300, "job", agents[0], vm_id, run=run, labels={"reward": float(rnd.random() < 0.6), "termination_reason": "user_stop"}, source="tau2")
            mk(rows, "session.close", tt + 350, "job", vm, vm_id, run=run, status="done", wall_ms=tt + 350 - tb)
            mk(rows, "vm.exit", tt + 400, "vm", vm, vm_id, vm=vm_id[:5])
            flush(rows, out)
            t = max(t + step, tt + 500)
        t = max(t_day + span, t + 1000)
        mk(rows, "agent.exit", t, "registrar", big, big_vm_id, spiffe_id=big, exit_reason="exit 0", agent_id="ag_b16b")
        mk(rows, "vm.exit", t + 200, "vm", big_vm, big_vm_id, vm="vm-07")
        flush(rows, out)
    written = sum(1 for _ in open(path))
    return {"n": written, "counts": counts, "vm_ids": vm_ids, "day": day, "largest": big, "smallest": small}


def pick_principals(counts):
    agents = sorted(((c, p) for p, c in counts.items() if "/agent/" in p), key=lambda x: (x[0], x[1]))
    return {"small": agents[0][1], "median": agents[len(agents) // 2][1], "largest": agents[-1][1]}


def mutate_day(src, dst):
    """A different valid day of exactly the same byte size: every digit in every order_id is rotated (+1 mod 10),
    the per-writer chain is recomputed (hashes are fixed-width). Ids, ts, writers and seqs are unchanged, so only
    the content (and every hash) differs: an answer served from a cache built on `src` is detectable."""
    heads = {}
    rot = str.maketrans("0123456789", "1234567890")
    with open(src) as f, open(dst, "w") as out:
        for line in f:
            r = json.loads(line)
            for holder in (r.get("args"), r.get("result")):
                if isinstance(holder, dict) and isinstance(holder.get("order_id"), str):
                    holder["order_id"] = holder["order_id"].translate(rot)
            r["prev"] = heads.get(r["writer"], GENESIS)
            r["hash"] = ev_hash(r)
            heads[r["writer"]] = r["hash"]
            out.write(json.dumps(r, separators=(",", ":")) + "\n")


def authority_oracle(r, by_id):
    k = r["kind"]
    if k in ("llm.call", "tool.call", "net.connect"):
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


def audit_oracle_stream(paths, principals, day=None):
    """Expected audit output for several principals from one or more files (a store = their concatenation), in one
    streaming pass: {principal: [(event_id, authority, hash)]} in (ts, writer, seq) order; `day` keeps that UTC day."""
    if isinstance(paths, str):
        paths = [paths]
    want = {p: [] for p in principals}
    pend = {p: [] for p in principals}
    decisions = {}
    for path in paths:
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                if r["kind"] == "authz.decision" and r["principal"] in want:
                    decisions[r["event_id"]] = r
                if day and not r["ts"].startswith(day):
                    continue
                for p in principals:
                    if r["principal"] == p or (r["kind"] in ("halt", "resume") and p in r.get("nodes", [])):
                        pend[p].append(r)
    for p in principals:
        pend[p].sort(key=order_key)
        want[p] = [(r["event_id"], authority_oracle(r, decisions), r["hash"]) for r in pend[p]]
    return want


def authority_matches(expected, got):
    return isinstance(got, dict) and all(got.get(k) == v for k, v in expected.items())


def compare_audit(want, got):
    """want: [(id, authority, hash)], got: solver NDJSON rows. The rows must be the stored rows: same ids in order,
    authority as the oracle, and the same `hash` (a row served from a cache of another file has another hash).
    Returns a problem string or None."""
    wid = [w[0] for w in want]
    gid = [g.get("event_id") for g in got]
    if gid != wid:
        i = next((i for i, (a, b) in enumerate(zip(gid, wid)) if a != b), min(len(gid), len(want)))
        return "ids differ at %d (got %d, want %d)" % (i, len(gid), len(want))
    for w, g in zip(want, got):
        eid, auth, h = w[0], w[1], (w[2] if len(w) > 2 else None)
        if not authority_matches(auth, g.get("authority")):
            return "authority of %s: want %s got %s" % (eid, auth, g.get("authority"))
        if h is not None and g.get("hash") != h:
            return "hash of %s differs: the row is not the stored row" % eid
    return None
