"""Demo principal for POC 3 and 4, started only through the spawner shim (see agent.py).

Gets its X.509-SVID from the SPIRE Workload API (written to its private runtime dir), logs lines carrying its own
SPIFFE ID, spawns its children through the shim, calls the gateway's demo tool, tries to leave the VM, sleeps, exits.
(The shim measures M4 for every principal.)

  demo_agent.py [--refund CENTS]... [--then S] [--egress HOST] [--child NAME=SLEEP|NAME=ARGS]... [--sleep S] [--exit CODE]

--then S repeats the refunds after S seconds (past the SVID's 60 s TTL); a child's ARGS are its own demo_agent flags.
"""
import argparse
import json
import http.client
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

T0 = float(os.environ["AVOS_T0"])  # the shim's clock just before it started this process
SVID_DIR = os.environ["AVOS_DIR"]  # /run/avos/<id>, mode 0700, owned by this principal's uid


def shim(req):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(os.environ["AVOS_SPAWN"])
        s.sendall(json.dumps(req).encode() + b"\n")
        out = json.loads(s.makefile().readline())
    if "error" in out:
        raise SystemExit(out["error"])
    return out


def fetch_svid():
    # The SPIRE agent syncs entries from the server every 1 s, so the first tries may find nothing.
    while time.time() - T0 < 30:
        r = subprocess.run(["/opt/spire/bin/spire-agent", "api", "fetch", "x509", "-socketPath", "/run/spire/agent.sock",
                            "-write", SVID_DIR], capture_output=True, text=True)
        if r.returncode == 0:
            return re.search(r"SPIFFE ID:\s*(\S+)", r.stdout)[1]
        time.sleep(0.05)
    raise SystemExit("no SVID within 30 s")


ap = argparse.ArgumentParser()
ap.add_argument("--sleep", type=float, default=0)
ap.add_argument("--child", action="append", default=[], metavar="NAME=SLEEP")
ap.add_argument("--exit", type=int, default=0)
ap.add_argument("--refund", type=int, action="append", default=[], metavar="CENTS")
ap.add_argument("--then", type=float, metavar="S")
ap.add_argument("--egress", metavar="HOST")
args = ap.parse_args()

spiffe_id = fetch_svid()
ms = round((time.time() - T0) * 1000)


def log(msg):
    print(json.dumps({"ts": time.time(), "spiffe_id": spiffe_id, "msg": msg}), flush=True)


FORWARDER = "http://127.0.0.1:8090"


def refunds():
    for cents in args.refund:
        req = urllib.request.Request(f"{FORWARDER}/tools/issue_refund", method="POST",
                                     data=json.dumps({"order_id": "#W2378156", "amount_cents": cents}).encode())
        t0 = time.time()
        try:
            out = json.load(urllib.request.urlopen(req, timeout=10))
            log(f"refund {cents}: allowed {out['refund_id']} ({(time.time() - t0) * 1000:.0f} ms)")
        except urllib.error.HTTPError as e:
            log(f"refund {cents}: refused {e.code} {json.load(e)}")


def egress(host):
    try:  # straight out of the VM
        socket.create_connection((host, 443), timeout=3).close()
        log(f"egress {host} direct: CONNECTED")
    except OSError as e:
        log(f"egress {host} direct: blocked ({e})")
    c = http.client.HTTPConnection("127.0.0.1", 8090, timeout=10)  # through the gateway, as an HTTPS proxy
    c.set_tunnel(host, 443)
    try:
        c.connect()
        log(f"egress {host} via gateway: CONNECTED")
    except OSError as e:
        log(f"egress {host} via gateway: refused ({e})")


log(f"SVID in hand after {ms} ms")
for child in args.child:
    name, rest = child.split("=", 1)
    argv = ["--sleep", rest] if re.fullmatch(r"[\d.]+", rest) else shlex.split(rest)
    p = shim({"op": "spawn", "name": name, "argv": [sys.executable, __file__, *argv]})
    log(f"spawned {name} as {p['id']} {p['spiffe_id']}")
refunds()
if args.egress:
    egress(args.egress)
if args.then:
    time.sleep(args.then)
    log(f"after {args.then:.0f} s (SVID rotated):")
    refunds()
time.sleep(args.sleep)
log(f"exiting with {args.exit}")
sys.exit(args.exit)
