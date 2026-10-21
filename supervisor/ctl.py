#!/usr/bin/env python3
"""Control table (POC 5). Runs on the host as root. Stops, resumes, kills or cuts off any principal or VM by name,
without touching its siblings. The name is resolved on this host; nothing central is consulted.

  ctl.py ls                         live VMs and principals, with their control state
  ctl.py halt NAME                  freeze the branch (agent: its cgroup; VM: Firecracker pause); prints a receipt
  ctl.py resume NAME                thaw it
  ctl.py kill NAME                  SIGKILL the branch (agent only); its exits are ledgered as usual
  ctl.py cut NAME | uncut NAME      keep it running, but the gateway refuses everything it (or its children) sends
  ctl.py stats TICKS.jsonl [--tick S]   M6 over halt rows, M7 over the siblings' ticks

NAME is an ag_xxxx id, a SPIFFE ID, a vm_id (vm-01-abc123) or a slot (vm-01). Every command writes one `halt` or
`resume` row (receipt hr_xxxx, who, when, nodes under it, ms) to events.jsonl, and the branch to cut.json, which the
gateway reads before it forwards a request and before it returns a response.
"""
import getpass
import http.client
import json
import os
import re
import secrets
import socket
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "identity"))
import registrar  # noqa: E402  (the ledger, principals() and emit() live there)

CUT = f"{registrar.ROOT}/cut.json"


def cut_table():
    try:
        return json.load(open(CUT))
    except FileNotFoundError:
        return {}


def write_cut(table):
    tmp = CUT + ".tmp"
    json.dump(table, open(tmp, "w"))
    os.replace(tmp, CUT)  # atomic: the gateway never sees a half-written table


def vms():
    """Live VMs: vm_id -> {slot, spiffe_id, ip}."""
    out = {}
    for slot in os.listdir(registrar.JAIL):
        try:
            ident = registrar.identity(slot)
        except FileNotFoundError:
            continue
        n = int(slot.split("-")[1])
        out[ident["VM_ID"]] = {"slot": slot, "spiffe_id": ident["VM_SPIFFE_ID"], "ip": f"172.16.{n}.2"}
    return out


def resolve(name):
    """-> (target dict with spiffe_id, vm, kind) for an ag_ id, SPIFFE ID, vm_id or slot."""
    live = {k: v for k, v in registrar.principals().items() if not v["exited_at"]}
    for vm_id, vm in vms().items():
        if name in (vm_id, vm["slot"], vm["spiffe_id"]):
            return {"spiffe_id": vm["spiffe_id"], "vm_id": vm_id, "vm": vm, "kind": "vm"}
    for p in live.values():
        if name in (p["id"], p["spiffe_id"]):
            return {"spiffe_id": p["spiffe_id"], "vm_id": p["vm_id"], "vm": vms()[p["vm_id"]], "kind": "principal"}
    sys.exit(f"{name}: not a live VM or principal (see `ctl.py ls`)")


def guest(t, path, body):
    c = http.client.HTTPConnection(t["vm"]["ip"], 80, timeout=10)
    c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    out = json.load(r)
    if r.status != 200:
        sys.exit(f"guest {path}: {out.get('error')}")
    return out


def firecracker(t, state):
    # The VM's own API socket inside its jail; pausing the VMM freezes every vCPU at once.
    s = socket.socket(socket.AF_UNIX)
    s.connect(f"{registrar.JAIL}/{t['vm']['slot']}/root/fc.sock")
    body = json.dumps({"state": state}).encode()
    s.sendall(b"PATCH /vm HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
              % (len(body), body))
    head = s.recv(4096).decode()
    s.close()
    if " 204 " not in head.split("\r\n")[0]:
        sys.exit(f"firecracker: {head.splitlines()[0]}")


def nodes_under(t):
    return [p["spiffe_id"] for p in registrar.principals().values()
            if not p["exited_at"] and (p["spiffe_id"] == t["spiffe_id"] or p["spiffe_id"].startswith(t["spiffe_id"] + "/"))]


def receipt(kind, t, action, ms, nodes, **extra):
    r = f"hr_{secrets.token_hex(2)}"
    registrar.emit(kind, receipt=r, action=action, by=os.environ.get("SUDO_USER") or getpass.getuser(),
                   target=t["spiffe_id"], vm_id=t["vm_id"], nodes=nodes, ms=ms, **extra)
    return r


def live_count(exclude):
    return sum(1 for p in registrar.principals().values() if not p["exited_at"] and p["spiffe_id"] not in exclude)


def control(cmd, name):
    t0 = time.time()  # M6 counts from the command, name resolution included
    t = resolve(name)
    table = cut_table()
    if cmd == "halt":
        if t["kind"] == "vm":
            firecracker(t, "Paused")
            out, action = {"nodes": nodes_under(t)}, "vm-pause"
        else:
            out, action = guest(t, "/halt", {"spiffe_id": t["spiffe_id"]}), "freeze"
        ms = round((time.time() - t0) * 1000, 1)
        r = receipt("halt", t, action, ms, out["nodes"], freeze_ms=out.get("freeze_ms"))
        table[t["spiffe_id"]] = {"receipt": r, "state": "halted"}
    elif cmd == "resume":
        if t["kind"] == "vm":
            firecracker(t, "Resumed")
            out, action = {"nodes": nodes_under(t)}, "vm-resume"
        else:
            out, action = guest(t, "/resume", {"spiffe_id": t["spiffe_id"]}), "thaw"
        ms = round((time.time() - t0) * 1000, 1)
        r = receipt("resume", t, action, ms, out["nodes"])
        if table.get(t["spiffe_id"], {}).get("state") == "halted":
            del table[t["spiffe_id"]]
    elif cmd == "kill":
        if t["kind"] == "vm":
            sys.exit("kill a VM with `infra/vm destroy`")
        out = guest(t, "/kill", {"spiffe_id": t["spiffe_id"]})
        ms = round((time.time() - t0) * 1000, 1)
        r = receipt("halt", t, "kill", ms, out["nodes"])
        table[t["spiffe_id"]] = {"receipt": r, "state": "killed"}  # drops any answer still in flight
    elif cmd == "cut":
        out = {"nodes": nodes_under(t)}
        r = receipt("halt", t, "net-cut", 0, out["nodes"])
        table[t["spiffe_id"]] = {"receipt": r, "state": "cut"}
    elif cmd == "uncut":
        out = {"nodes": nodes_under(t)}
        r = receipt("resume", t, "net-uncut", 0, out["nodes"])
        table.pop(t["spiffe_id"], None)
    write_cut(table)
    ms = round((time.time() - t0) * 1000, 1)
    print(f"{r}  {cmd} {t['spiffe_id']}  {len(out['nodes'])} node(s) under it  {ms} ms end to end"
          + (f" (frozen after {out['freeze_ms']} ms in the guest)" if out.get("freeze_ms") is not None else ""))
    print(f"{live_count(out['nodes'])} agents still running")


def ls():
    table = cut_table()

    def state(spiffe_id):
        for k, v in table.items():
            if spiffe_id == k or spiffe_id.startswith(k + "/"):
                return f"{v['state']} {v['receipt']}" + ("" if spiffe_id == k else " (via parent)")
        return "live"

    live = [p for p in registrar.principals().values() if not p["exited_at"]]
    for vm_id, vm in sorted(vms().items()):
        print(f"{vm_id:<16} {vm['spiffe_id']:<72} {state(vm['spiffe_id'])}")
        for p in sorted(live, key=lambda p: p["ts"]):
            if p["vm_id"] == vm_id:
                print(f"  {p['id']:<14} {p['spiffe_id']:<72} {state(p['spiffe_id'])}")


def stats(ticks_path, tick_s):
    """M6 over this run's halts, M7 over the ticks of principals that were never named by a control.
    A late tick counts as disruption only inside a control window (1 s around a halt or resume); stalls outside
    any window are host jitter and are reported separately."""
    from datetime import datetime
    ticks = {}
    for line in open(ticks_path):
        try:
            t = json.loads(line)
        except ValueError:
            continue
        if t.get("msg") == "tick":
            ticks.setdefault(t["spiffe_id"], []).append(t)
    vm_ids = {re.search(r"/vm/([^/]+)", sid)[1] for sid in ticks}
    rows = [r for r in registrar.ledger() if r["kind"] in ("halt", "resume") and r.get("vm_id") in vm_ids]
    stops = [r["ms"] for r in rows if r["action"] in ("freeze", "vm-pause")]
    if stops:
        print(f"M6 stop_latency_ms: median {statistics.median(stops):.0f}, worst {max(stops):.0f} (n={len(stops)})")
    named = [r["target"] for r in rows]
    controls = [(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp(), r["action"]) for r in rows]
    siblings = {sid: ts for sid, ts in ticks.items() if not any(sid == n or sid.startswith(n + "/") for n in named)}
    lost = failed = 0
    late = []  # (gap s, ts, principal, nearest control)
    for sid, ts in siblings.items():
        ts.sort(key=lambda t: t["tick"])
        lost += ts[-1]["tick"] - len(ts)  # a gap in seq = a tick that was never written
        failed += sum(1 for t in ts if isinstance(t.get("gw"), str))
        for a, b in zip(ts, ts[1:]):
            if b["ts"] - a["ts"] > tick_s + 0.1:
                near = min(controls, key=lambda c: abs(c[0] - b["ts"]), default=(float("inf"), "-"))
                late.append((b["ts"] - a["ts"], b["ts"], sid, near))
    during = [x for x in late if abs(x[3][0] - x[1]) <= 1]
    print(f"M7 sibling_disruption: {lost + len(during) + failed} (lost {lost}, delayed during a control {len(during)}, "
          f"failed calls {failed}) over {len(siblings)} never-named principals, {sum(map(len, siblings.values()))} ticks")
    for gap, at, sid, near in sorted(late, reverse=True)[:5]:
        print(f"  {'DURING' if abs(near[0] - at) <= 1 else 'jitter'}: +{gap * 1000:.0f} ms on {sid.split('/vm/')[1]}"
              f" ({near[1]} {near[0] - at:+.2f} s away)")


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "ls":
        ls()
    elif cmd in ("halt", "resume", "kill", "cut", "uncut") and len(args) == 1:
        control(cmd, args[0])
    elif cmd == "stats" and args:
        stats(args[0], float(args[args.index("--tick") + 1]) if "--tick" in args else 0.05)
    else:
        sys.exit(__doc__)
