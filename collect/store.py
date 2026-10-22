"""The host's trace store (POC 7): every host-side emitter writes POC 6 contract events here as they happen.

One stream per (host, component, VM): writer `<host>/<component>/<vm_id>`, file `<root>/vm/<vm_id>/<component>.jsonl`
(`<root>/host/<component>.jsonl` for an event with no VM). Each append mints the id, `seq`, host `ts` and chain link
under an exclusive lock on the file, so the processes that share a stream (registrar server and `vm-down`, every
`ctl` run) keep one gap-free chain, and writes the whole line with one O_APPEND write: a writer killed at any point
leaves whole lines. No fsync on the request path; the kernel holds the line once write() returns.

M8's "happened" side lives beside the store, not in it: `<root>/counters/<vm_id>` gets one byte per counted act,
appended just before the act is written (a = request received by the gateway, l / t = model / tool call forwarded,
s / e = spawn / exit processed by the registrar, h / r = halt / resume issued by ctl). Its size survives restarts.

  python3 collect/store.py emit COMPONENT KIND VM_ID PRINCIPAL 'JSON-FIELDS'    (for infra/vm and infra/job)

Root: $AVOS_TRACE_ROOT, default /var/lib/avos/trace. Interface: collect/CONTRACT.md. Design: docs/architecture.md §7.
"""
import fcntl
import json
import os
import re
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trace"))
import lib_trace as T  # noqa: E402

DEFAULT_ROOT = "/var/lib/avos/trace"
ROOT = os.environ.get("AVOS_TRACE_ROOT", DEFAULT_ROOT)
LEGACY = "/var/lib/avos/events.jsonl"  # the POC 1-6 ledger, read-only history since POC 7
HOST = socket.gethostname()
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # a vm_id or run id that is safe as a file name


def vm_of(principal):
    vm = T.vm_of(principal)
    return vm if vm and SAFE.fullmatch(vm) else None


def vm_dir(vm_id):
    return f"{ROOT}/vm/{vm_id}" if vm_id else f"{ROOT}/host"


class Stream:
    """One writer's append-only file. The in-process state is a cache; the file is the truth (another process may
    have appended since), so it is re-read from the file's tail whenever the size is not the one we left."""

    def __init__(self, component, vm_id):
        self.writer = f"{HOST}/{component}" + (f"/{vm_id}" if vm_id else "")
        self.path = f"{vm_dir(vm_id)}/{component}.jsonl"
        self.lock = threading.Lock()
        self.size, self.seq, self.head, self.last_ms = -1, 0, T.GENESIS, 0

    def tail(self, size):
        self.seq, self.head, self.last_ms = 0, T.GENESIS, 0
        if not size:
            return
        with open(self.path, "rb") as f:
            pos, buf = size, b""
            while True:
                step = min(65536, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                i = buf.rstrip(b"\n").rfind(b"\n")
                if i >= 0 or pos == 0:
                    break
        ev = json.loads(buf.rstrip(b"\n")[i + 1:])
        self.seq, self.head, self.last_ms = ev["seq"], ev["hash"], T.ts_ms(ev["ts"])

    def append(self, kind, principal, vm_id, fields):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                size = os.fstat(fd).st_size
                if size != self.size:
                    self.tail(size)
                ms = max(int(time.time() * 1000), self.last_ms)  # ts never goes back within a writer
                ts, seq = T.ms_ts(ms), self.seq + 1
                ev = {"event_id": T.mint_id(ts, self.writer, seq), "kind": kind, "ts": ts, "writer": self.writer,
                      "seq": seq, "host": HOST, "principal": principal, "vm_id": vm_id or "", **fields, "prev": self.head}
                ev["hash"] = T.ev_hash(ev)
                line = T.dumps(ev).encode()
                if os.write(fd, line) != len(line):
                    raise OSError(f"short write to {self.path}")
                self.size, self.seq, self.head, self.last_ms = size + len(line), seq, ev["hash"], ms
                return ev
            finally:
                os.close(fd)  # also releases the lock


_streams, _streams_lock = {}, threading.Lock()


def emit(component, kind, principal, vm_id, **fields):
    """Append one event to the stream of (component, vm_id) and return it (its event_id is what others reference)."""
    with _streams_lock:
        s = _streams.get((component, vm_id)) or _streams.setdefault((component, vm_id), Stream(component, vm_id))
    ev = s.append(kind, principal, vm_id, fields)
    if kind == "session.open":  # run id -> VM, so `avos trace --run run_*` needs no scan
        os.makedirs(f"{ROOT}/runs", exist_ok=True)
        open(f"{ROOT}/runs/{fields['run']}", "w").write(vm_id)
    return ev


# ----------------------------------------------------------------------------- M8 counters

def count(vm_id, what):
    """One byte = one counted act, appended before the act is written."""
    if not vm_id:
        return
    os.makedirs(f"{ROOT}/counters", exist_ok=True)
    fd = os.open(f"{ROOT}/counters/{vm_id}", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
    try:
        os.write(fd, what.encode())
    finally:
        os.close(fd)


def counters(vm_id):
    """{byte: count} for one VM, or None if this store never counted for it (imported or unknown)."""
    try:
        data = open(f"{ROOT}/counters/{vm_id}", "rb").read()
    except FileNotFoundError:
        return None
    return {c: data.count(c.encode()) for c in "altsehr"}


# ----------------------------------------------------------------------------- reading

def files():
    """Every stream file, in a fixed order. Each writer lives in exactly one file, so any order is one valid trace."""
    out = []
    for sub in ("host", "import"):
        d = f"{ROOT}/{sub}"
        out += sorted(f"{d}/{n}" for n in (os.listdir(d) if os.path.isdir(d) else []) if n.endswith(".jsonl"))
    base = f"{ROOT}/vm"
    for vm in sorted(os.listdir(base) if os.path.isdir(base) else []):
        out += vm_files(vm)
    return out


def vm_files(vm_id):
    d = vm_dir(vm_id)
    return sorted(f"{d}/{n}" for n in (os.listdir(d) if os.path.isdir(d) else []) if n.endswith(".jsonl"))


def vm_events(vm_id):
    for path in vm_files(vm_id):
        with open(path, encoding="utf-8") as f:
            for line in f:
                yield json.loads(line)


def ledger(vm_ids=None):
    """Every event as a POC 1-6 ledger row: the frozen legacy ledger, then the store. For reports only."""
    if ROOT == DEFAULT_ROOT and os.path.exists(LEGACY):
        with open(LEGACY) as f:
            for line in f:
                r = json.loads(line)
                if not vm_ids or r.get("vm_id") in vm_ids:
                    yield r
    for path in [p for v in vm_ids for p in vm_files(v)] if vm_ids else files():
        with open(path, encoding="utf-8") as f:
            for line in f:
                yield T.legacy(json.loads(line))


def main():
    if len(sys.argv) != 7 or sys.argv[1] != "emit":
        sys.exit("usage: store.py emit COMPONENT KIND VM_ID PRINCIPAL 'JSON-FIELDS'")
    _, _, component, kind, vm_id, principal, fields = sys.argv
    emit(component, kind, principal, vm_id, **json.loads(fields))


if __name__ == "__main__":
    main()
