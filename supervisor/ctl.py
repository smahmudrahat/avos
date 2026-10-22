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
`resume` event (receipt hr_xxxx, who, when, nodes under it, ms) to the target VM's ctl stream in the trace store
(collect/store.py), and the branch to cut.json, which the gateway reads before it forwards a request and before it
returns a response. The command is counted (M8) before it acts, so a control that acted but left no event shows.
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "identity"), os.path.join(HERE, "..", "collect")]
import registrar  # noqa: E402  (the per-host index of principals lives there)
import store  # noqa: E402

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
    live_vms = vms()
    for vm_id, vm in live_vms.items():
        if name in (vm_id, vm["slot"], vm["spiffe_id"]):
            return {"spiffe_id": vm["spiffe_id"], "vm_id": vm_id, "vm": vm, "kind": "vm"}
    p = registrar.by_id(name) if name.startswith("ag_") else registrar.get(name)  # the index, not the ledger
    if p and not p["exited_at"] and p["vm_id"] in live_vms:
        return {"spiffe_id": p["spiffe_id"], "vm_id": p["vm_id"], "vm": live_vms[p["vm_id"]], "kind": "principal"}
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
    # The target itself first (a VM is not a ledger principal, so it would otherwise be missing), then its branch.
    return [t["spiffe_id"]] + [p["spiffe_id"] for p in registrar.live(t["vm_id"])
                               if p["spiffe_id"].startswith(t["spiffe_id"] + "/")]


def receipt(kind, t, action, ms, nodes, **extra):
    r = f"hr_{secrets.token_hex(2)}"
    store.emit("ctl", kind, t["spiffe_id"], t["vm_id"], receipt=r, action=action,
               by=os.environ.get("SUDO_USER") or getpass.getuser(), target=t["spiffe_id"], nodes=nodes, ms=ms, **extra)
    return r


def live_count(exclude):
    return sum(1 for p in registrar.live() if p["spiffe_id"] not in exclude)


def control(cmd, name):
    t0 = time.time()  # M6 counts from the command, name resolution included
    t = resolve(name)
    store.count(t["vm_id"], "r" if cmd in ("resume", "uncut") else "h")  # issued; its event follows the act
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

    live = registrar.live()
    for vm_id, vm in sorted(vms().items()):
        print(f"{vm_id:<16} {vm['spiffe_id']:<72} {state(vm['spiffe_id'])}")
        for p in sorted(live, key=lambda p: p["ts"]):
            if p["vm_id"] == vm_id:
                print(f"  {p['id']:<14} {p['spiffe_id']:<72} {state(p['spiffe_id'])}")


def stats(ticks_path, tick_s):
    """M6 over this run's halts, M7 over the ticks of principals that were never named by a control.
    A failed sibling call is a lost event whenever it happens. A late tick counts only inside a control window (1 s
    around a halt or resume): `just poc5-baseline` shows late ticks at the same low rate with no control at all
    (host scheduling on a small host), and none inside a window, so outside one they are reported, not counted."""
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
    rows = [r for r in (store.ledger(vm_ids) if vm_ids else ()) if r["kind"] in ("halt", "resume")]
    stops = [r["ms"] for r in rows if r["action"] in ("freeze", "vm-pause")]
    if stops:
        print(f"M6 stop_latency_ms: median {statistics.median(stops):.0f}, worst {max(stops):.0f} (n={len(stops)})")
    named = [r["target"] for r in rows]
    controls = [(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp(), r["action"]) for r in rows]
    siblings = {sid: ts for sid, ts in ticks.items() if not any(sid == n or sid.startswith(n + "/") for n in named)}
    lost = 0
    late, failed = [], []  # (gap s or 0, ts, principal, nearest control); the same 1 s window applies to both
    near_ticks = near_calls = 0  # denominators: how much of the run lay inside a control window

    def nearest(at):
        return min(controls, key=lambda c: abs(c[0] - at), default=(float("inf"), "-"))

    def inside(at):
        return abs(nearest(at)[0] - at) <= 1

    for sid, ts in siblings.items():
        ts.sort(key=lambda t: t["tick"])
        lost += ts[-1]["tick"] - len(ts)  # a gap in seq = a tick that was never written
        failed += [(0, t["ts"], sid, nearest(t["ts"])) for t in ts if isinstance(t.get("gw"), str)]
        late += [(b["ts"] - a["ts"], b["ts"], sid, nearest(b["ts"])) for a, b in zip(ts, ts[1:])
                 if b["ts"] - a["ts"] > tick_s + 0.1]
        near_ticks += sum(inside(t["ts"]) for t in ts)
        near_calls += sum(inside(t["ts"]) for t in ts if "gw" in t)
    during = [x for x in late if abs(x[3][0] - x[1]) <= 1] + failed
    n_ticks = sum(map(len, siblings.values()))
    n_calls = sum("gw" in t for ts in siblings.values() for t in ts)
    print(f"M7 sibling_disruption: {lost + len(during)} (lost {lost}, failed calls {len(failed)}, late during a control "
          f"{len(during) - len(failed)}; late outside any control: {len(late) - len(during) + len(failed)}) over "
          f"{len(siblings)} never-named principals, {n_ticks} ticks")
    # The window is only fair if anomalies outside it are as common as anomalies inside it (and as in a run with no
    # controls at all, `just poc5-baseline`); these counts let the rates be compared.
    out = {"principals": len(siblings), "ticks": n_ticks, "ticks_in_window": near_ticks, "lost": lost,
           "late_in": sum(1 for x in late if abs(x[3][0] - x[1]) <= 1), "late_out": sum(1 for x in late if abs(x[3][0] - x[1]) > 1),
           "calls": n_calls, "calls_in_window": near_calls,
           "failed_in": sum(1 for x in failed if abs(x[3][0] - x[1]) <= 1), "failed_out": sum(1 for x in failed if abs(x[3][0] - x[1]) > 1),
           "worst_gap_ms": round(max((x[0] for x in late), default=0) * 1000), "controls": len(controls)}
    print("M7 counts: " + json.dumps(out))
    for gap, at, sid, near in sorted(late + failed, key=lambda x: -x[0])[:5]:
        what = f"+{gap * 1000:.0f} ms" if gap else "failed gateway call"
        print(f"  {'COUNTED' if not gap or abs(near[0] - at) <= 1 else 'jitter'}: {what} on {sid.split('/vm/')[1]}"
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
