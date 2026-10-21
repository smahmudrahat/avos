#!/usr/bin/env python3
"""Identity registrar (POC 3). Runs on the host as root, next to the host's SPIRE Server.

Nothing is registered in advance: the spawner shim inside a VM asks for an entry when it starts a principal, and the
entry is deleted when the principal dies. SPIRE keeps no lineage, so this file also keeps the birth/death ledger
(rows in /var/lib/avos/events.jsonl, next to vm.boot): agent.spawn, agent.exit, vm.exit.

  registrar.py serve                       HTTPS on :8082 for the shims; the caller is its VM's own SVID (mTLS),
                                           which only the guest's root can read, and must match its source IP
  registrar.py vm-up vm-NN GW OUT.json     mint a unique VM name + join token; write the guest's identity config
  registrar.py vm-down vm-NN               record deaths, delete the VM's entries, evict its SPIRE agent
  registrar.py who [--all]                 principals as a tree; --all includes destroyed VMs
  registrar.py stats [VM_ID]              M3 identity_coverage, M4 identity_issue_ms over exited principals
"""
import json
import os
import re
import secrets
import ssl
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = "/var/lib/avos"
EVENTS = f"{ROOT}/events.jsonl"
JAIL = f"{ROOT}/jail/firecracker"
TD = "spiffe://avoslab.com"
SPIRE = "/opt/spire/bin/spire-server"
SOCK = "/run/spire-server/api.sock"
SVID_TTL_S = 60  # a copied SVID is useless after this; the SPIRE agent rotates live ones at half-life
VM_UID = 20000  # the guest's holder of the VM's own SVID (infra/agent/agent.py)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
CERT_DIR = "/run/avos-registrar"
lock = threading.Lock()  # spawns and exits are serialized (IDs must stay unique); TLS handshakes are not


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def spire(*args, check=True):
    return subprocess.run([SPIRE, *args, "-socketPath", SOCK], check=check, capture_output=True, text=True).stdout


def emit(kind, **fields):
    row = {"kind": kind, "ts": now(), **fields}
    with open(EVENTS, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def ledger():
    try:
        return [json.loads(line) for line in open(EVENTS)]
    except FileNotFoundError:
        return []


def principals():
    # Limit: rescans the whole ledger per call; an index (or POC 7's store) when it gets slow.
    # Only rows with a spiffe_id are principals (infra/job writes job-level agent.exit rows without one).
    ps = {}
    for r in ledger():
        if r["kind"] == "agent.spawn" and "spiffe_id" in r:
            ps[r["spiffe_id"]] = {**r, "exited_at": None}
        elif r["kind"] == "agent.exit" and r.get("spiffe_id") in ps:
            ps[r["spiffe_id"]] |= {"exited_at": r["ts"], "exit_reason": r["exit_reason"], "svid_ms": r.get("svid_ms")}
    return ps


def identity(slot):
    return json.load(open(f"{JAIL}/{slot}/identity.json"))


def vm_up(slot, gw, out):
    used = {r.get("vm_id") for r in ledger() if r["kind"] == "vm.boot"}
    while (vm_id := f"{slot}-{secrets.token_hex(3)}") in used:
        pass
    vm_spiffe = f"{TD}/vm/{vm_id}"
    # The token's -spiffeID makes the VM's SPIRE agent known by the VM's own name; workload entries hang off it.
    token = spire("token", "generate", "-spiffeID", vm_spiffe, "-ttl", "600").split()[-1]
    # That alias only names the SPIRE agent. This entry gives the VM a credential of its own for the same ID.
    entry = spire("entry", "create", "-spiffeID", vm_spiffe, "-parentID", vm_spiffe, "-selector", f"unix:uid:{VM_UID}",
                  "-output", "json")
    ident = {"VM_ID": vm_id, "VM_SPIFFE_ID": vm_spiffe, "VM_ENTRY_ID": json.loads(entry)["results"][0]["entry"]["id"],
             "JOIN_TOKEN": token, "SPIRE_BUNDLE": spire("bundle", "show"), "SPIRE_SERVER": gw, "REGISTRAR": f"https://{gw}:8082"}
    with os.fdopen(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(ident, f)
    print(vm_id)


def end(p, reason, svid_ms=None):
    spire("entry", "delete", "-entryID", p["entry_id"], check=False)
    return emit("agent.exit", id=p["id"], spiffe_id=p["spiffe_id"], vm_id=p["vm_id"], exit_reason=reason, svid_ms=svid_ms)


def vm_down(slot):
    ident = identity(slot)
    for p in principals().values():
        if p["vm_id"] == ident["VM_ID"] and not p["exited_at"]:
            end(p, "vm destroyed")
    spire("entry", "delete", "-entryID", ident["VM_ENTRY_ID"], check=False)
    agent = f"{TD}/spire/agent/join_token/{ident['JOIN_TOKEN']}"
    # The join token's -spiffeID alias is an entry of its own (parent: the SPIRE agent); it outlives the eviction.
    alias = json.loads(spire("entry", "show", "-parentID", agent, "-output", "json", check=False) or "{}")
    for e in alias.get("entries") or []:
        spire("entry", "delete", "-entryID", e["id"], check=False)
    spire("agent", "evict", "-spiffeID", agent, check=False)
    emit("vm.exit", vm=slot, vm_id=ident["VM_ID"], spiffe_id=ident["VM_SPIFFE_ID"])


def spawn(vm, body):
    name, parent, uid = body["name"], body.get("parent"), int(body["uid"])
    if not NAME.fullmatch(name):
        raise ValueError(f"bad name {name!r}")
    ps = principals()
    if parent:
        pp = ps.get(parent)
        if not pp or pp["vm_id"] != vm["VM_ID"] or pp["exited_at"]:
            raise PermissionError(f"parent {parent} is not live in {vm['VM_ID']}")
        spiffe_id = f"{parent}/sub/{name}"
    else:
        spiffe_id = f"{vm['VM_SPIFFE_ID']}/agent/{name}"
    if spiffe_id in ps:
        raise ValueError(f"{spiffe_id} was already issued; IDs are never reused")
    out = spire("entry", "create", "-spiffeID", spiffe_id, "-parentID", vm["VM_SPIFFE_ID"],
                "-selector", f"unix:uid:{uid}", "-x509SVIDTTL", str(SVID_TTL_S), "-output", "json")
    entry_id = json.loads(out)["results"][0]["entry"]["id"]
    ids = {p["id"] for p in ps.values()}
    while (pid := f"ag_{secrets.token_hex(2)}") in ids:  # short display ID for the agent tree
        pass
    return emit("agent.spawn", id=pid, spiffe_id=spiffe_id, parent=parent or vm["VM_SPIFFE_ID"], name=name,
                vm_id=vm["VM_ID"], uid=uid, entry_id=entry_id)


def exit_(vm, body):
    p = principals().get(body["spiffe_id"])
    if not p or p["vm_id"] != vm["VM_ID"] or p["exited_at"]:
        raise PermissionError(f"{body['spiffe_id']} is not live in {vm['VM_ID']}")
    return end(p, body["reason"], body.get("svid_ms"))


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.do_handshake()  # deferred out of accept(), so a stalled client blocks no one

    def do_POST(self):
        try:
            # vm create pins each tap to its guest IP (iptables), so the source IP names the VM; the client SVID
            # proves the caller is that VM's root (agents in the VM can reach this port, but hold no VM SVID).
            m = re.fullmatch(r"172\.16\.(\d+)\.2", self.client_address[0])
            if not m:
                raise PermissionError("not a guest")
            vm = identity(f"vm-{int(m[1]):02d}")
            san = dict(self.connection.getpeercert().get("subjectAltName", ()))
            if san.get("URI") != vm["VM_SPIFFE_ID"]:
                raise PermissionError(f"caller is {san.get('URI')}, not {vm['VM_SPIFFE_ID']}")
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                out = {"/spawn": spawn, "/exit": exit_}[self.path](vm, body)
            self.reply(200, out)
        except (KeyError, ValueError, PermissionError, OSError, subprocess.CalledProcessError) as e:
            self.reply(400, {"error": f"{type(e).__name__}: {e}"})

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        s, addr = self.socket.accept()
        return CTX.wrap_socket(s, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, addr):
        print(f"{addr[0]}: {sys.exc_info()[1]!r}", flush=True)


def mint():
    # The registrar's own server SVID; guests verify it with the trust bundle. Re-minted hourly (TTL 2 h).
    os.makedirs(CERT_DIR, mode=0o700, exist_ok=True)
    spire("x509", "mint", "-spiffeID", "spiffe://avoslab.com/registrar", "-ttl", "2h", "-write", CERT_DIR)
    CTX.load_cert_chain(f"{CERT_DIR}/svid.pem", f"{CERT_DIR}/key.pem")
    CTX.load_verify_locations(cadata=spire("bundle", "show"))


def remint():
    while True:
        time.sleep(3600)
        mint()


def serve():
    mint()
    threading.Thread(target=remint, daemon=True).start()
    Server(("0.0.0.0", 8082), Handler).serve_forever()


def who(show_all):
    rows = ledger()
    vms = {r["vm_id"]: dict(r) for r in rows if r["kind"] == "vm.boot" and "vm_id" in r}
    for r in rows:
        if r["kind"] == "vm.exit" and r["vm_id"] in vms:
            vms[r["vm_id"]]["exited_at"] = r["ts"]
    kids = defaultdict(list)
    for p in principals().values():
        kids[p["parent"]].append(p)

    def life(x):
        return f"died {x['exited_at']} ({x['exit_reason']})" if x.get("exit_reason") else \
            f"died {x['exited_at']}" if x.get("exited_at") else "live"

    def walk(parent, depth):
        for p in sorted(kids[parent], key=lambda p: p["ts"]):
            print(f"{'   ' * depth}└─ {p['id']}  {p['spiffe_id']}  born {p['ts']}  {life(p)}")
            walk(p["spiffe_id"], depth + 1)

    for v in vms.values():
        if show_all or not v.get("exited_at"):
            print(f"{v['vm_id']}  {v['spiffe_id']}  born {v['ts']}  {life(v)}")
            walk(v["spiffe_id"], 1)


def stats(vm_id=None):
    done = [p for p in principals().values()
            if p["exited_at"] and p["exit_reason"] != "vm destroyed" and vm_id in (None, p["vm_id"])]
    ms = sorted(p["svid_ms"] for p in done if p["svid_ms"] is not None)
    if not done:
        sys.exit("no exited principals yet")
    print(f"M3 identity_coverage: {len(ms)}/{len(done)} = {100 * len(ms) / len(done):.0f}%")
    if ms:
        print(f"M4 identity_issue_ms: median {statistics.median(ms):.0f}, worst {ms[-1]}, best {ms[0]} (n={len(ms)})")


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "serve":
        CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        CTX.verify_mode = ssl.CERT_REQUIRED
        serve()
    elif cmd == "vm-up" and len(args) == 3:
        vm_up(*args)
    elif cmd == "vm-down" and len(args) == 1:
        vm_down(*args)
    elif cmd == "who":
        who("--all" in args)
    elif cmd == "stats" and len(args) <= 1:
        stats(*args)
    else:
        sys.exit(__doc__)
