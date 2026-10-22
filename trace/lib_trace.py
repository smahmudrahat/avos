"""Shared pieces of the trace CLIs (POC 6): canonical JSON and the hash chain, event ids, timestamps, the schema
table, streaming readers and the CLI wrapper. Contract: trace/CONTRACT.md. Design: docs/trace.md.
"""
import calendar
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "schema.json")
GENESIS = "0" * 64
ENVELOPE = {"event_id", "kind", "ts", "writer", "seq", "host", "principal", "vm_id", "prev", "hash"}
TS_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z\Z")
ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
RECEIPT_RE = re.compile(r"hr_[0-9a-f]{4}\Z")
VM_OF = re.compile(r"spiffe://[^/]+/vm/([^/]+)")


# ----------------------------------------------------------------------------- chain

def canon(ev):
    """The pinned canonical form: the event without `hash`, keys sorted, compact, ASCII-escaped."""
    d = dict(ev)
    d.pop("hash", None)
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def ev_hash(ev):
    return hashlib.sha256(canon(ev).encode()).hexdigest()


def sha(obj):
    """Content hash kept when content capture is off."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dumps(ev):
    # One line as stored: the emitters' own json.dumps style, UTF-8 rather than \u escapes.
    return json.dumps(ev, ensure_ascii=False) + "\n"


# ----------------------------------------------------------------------------- time and ids

_DAY = {}


def ts_ms(ts):
    """Contract timestamp -> epoch ms (no strptime per call: the day is cached)."""
    d = _DAY.get(ts[:10])
    if d is None:
        d = _DAY[ts[:10]] = calendar.timegm(time.strptime(ts[:10], "%Y-%m-%d")) * 1000
    return d + int(ts[11:13]) * 3_600_000 + int(ts[14:16]) * 60_000 + int(ts[17:19]) * 1000 + int(ts[20:23])


def ms_ts(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + f".{ms % 1000:03d}Z"


def guest_ts(s):
    """τ² message timestamp (naive ISO, microseconds) -> contract format, kept as guest_ts only."""
    return s[:23] + "Z" if len(s) >= 23 else s[:19] + ".000Z"


def mint_id(ts, writer, seq):
    """Event id: 12 hex of the host-clock ms, 12 hex of SHA-256(writer), 10 hex of the writer's seq (36 chars).
    Sorts by time; unique without coordination because seq never repeats within a writer and the writer
    name starts with the host."""
    return f"{ts_ms(ts):012x}-{hashlib.sha256(writer.encode()).hexdigest()[:12]}-{seq:010x}"


def vm_of(principal):
    m = VM_OF.match(principal or "")
    return m[1] if m else None


# ----------------------------------------------------------------------------- schema

_TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "object": (dict,),
          "array": (list,), "null": (type(None),)}


def _field(name, spec, required):
    types = spec.get("type")
    allowed = None if types is None else tuple(t for x in ([types] if isinstance(types, str) else types) for t in _TYPES[x])
    return name, allowed, set(spec["enum"]) if "enum" in spec else None, required


def schema_table():
    """schema.json -> {kind: [(field, python types, enum, required)]}: the envelope plus that kind's fields.
    The validator checks presence, type and enum from here; patterns and cross-event rules are its own codes."""
    s = json.load(open(SCHEMA))
    common = [_field(k, v, k in s["required"]) for k, v in s["properties"].items() if k != "kind"]
    out = {}
    for kind, d in s["$defs"]["kinds"].items():
        req = set(d.get("required", []))
        out[kind] = common + [_field(k, v, k in req) for k, v in d.get("properties", {}).items()]
    return out


def type_ok(v, allowed):
    t = type(v)
    return t in allowed and not (t is bool and bool not in allowed)


# ----------------------------------------------------------------------------- reading

def lines(path):
    """(line number, raw line) for every non-blank line, streamed."""
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                yield n, line


def events(path):
    """(line number, event or None when the line is not a JSON object), streamed."""
    for n, line in lines(path):
        try:
            ev = json.loads(line)
        except ValueError:
            ev = None
        yield n, ev if isinstance(ev, dict) else None


def principal_table(paths):
    """{principal: [birth ts, death ts | None, parent | None]} from vm.boot / agent.spawn / *.exit rows. Only
    those lines are parsed. VM life starts at vm.boot.ts - boot_ms (the create request)."""
    L = {}
    for path in paths:
        for _, line in lines(path):
            if '"vm.boot"' not in line and '"agent.spawn"' not in line and '.exit"' not in line:
                continue
            try:
                r = json.loads(line)
                k, p = r["kind"], r["principal"]
                if k == "vm.boot":
                    L.setdefault(p, [None, None, None])[0] = ms_ts(ts_ms(r["ts"]) - int(r.get("boot_ms") or 0))
                elif k == "agent.spawn":
                    e = L.setdefault(p, [None, None, None])
                    e[0], e[2] = r["ts"], r.get("parent")
                elif k in ("vm.exit", "agent.exit"):
                    L.setdefault(p, [None, None, None])[1] = r["ts"]
            except (ValueError, KeyError, TypeError):
                continue  # reported by the second pass
    return L


def fingerprint(path):
    """Identity and content of a file, for derived indexes: device, inode, size, mtime and SHA-256 of its first and
    last 64 KB. Size and mtime alone matched a different file at the same path (a stale index answered nothing)."""
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(65536))
        f.seek(max(0, st.st_size - 65536))
        h.update(f.read(65536))
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, h.hexdigest()]


# ----------------------------------------------------------------------------- projections

GATED = {"llm.call", "tool.call", "net.connect"}


def authority(ev, by_id):
    """What an event acted under (§8): the decision a gated call names, the receipt of a control, or nothing."""
    k = ev["kind"]
    if k in GATED:
        if k == "tool.call" and ev.get("via") == "harness":
            return {"via": "harness"}
        d = by_id.get(ev.get("authz_ref"), {})
        a = {"authz_ref": ev.get("authz_ref"), "decision": d.get("decision"), "policy_ids": d.get("policy_ids")}
        if ev.get("receipt"):
            a["receipt"] = ev["receipt"]
        return a
    if k == "authz.decision":
        return {"decision": ev["decision"], "policy_ids": ev["policy_ids"]}
    if k in ("halt", "resume"):
        return {"receipt": ev["receipt"], "by": ev["by"]}
    return {}


LEGACY_DROP = {"event_id", "writer", "seq", "host", "prev", "hash", "authority", "authz_ref", "call_ref"}


def legacy(ev):
    """A contract event in the shape of a POC 1-6 ledger row, for `ingest` and the reports written for those rows
    (registrar who/stats, ctl stats, gateway stats)."""
    k = ev["kind"]
    r = {f: v for f, v in ev.items() if f not in LEGACY_DROP}
    p = r.pop("principal")
    if k in ("vm.boot", "vm.exit"):
        r["spiffe_id"] = p
    elif k.startswith("session."):
        r["vm_spiffe_id"] = p
    elif k not in ("agent.spawn", "agent.exit", "halt", "resume"):
        r["principal"] = p
    if k == "net.connect":
        r["host"] = r.pop("dest", None)
    if "agent_id" in r:
        r["id"] = r.pop("agent_id")
    return r


# ----------------------------------------------------------------------------- CLI

def cli(main):
    """Exit 2 with a one-line message (never a traceback) on unreadable input; argparse already exits 2 on usage."""
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as e:
        print(f"{os.path.basename(sys.argv[0])}: {e}", file=sys.stderr)
        sys.exit(2)


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
