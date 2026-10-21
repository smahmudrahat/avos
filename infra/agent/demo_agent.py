"""Demo principal for POC 3, started only through the spawner shim (see agent.py).

Gets its X.509-SVID from the SPIRE Workload API (written to its private runtime dir), logs lines carrying its own
SPIFFE ID, spawns its children through the shim, sleeps, exits. (The shim measures M4 for every principal.)

  demo_agent.py [--sleep S] [--child NAME=SLEEP]... [--exit CODE]
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

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
args = ap.parse_args()

spiffe_id = fetch_svid()
ms = round((time.time() - T0) * 1000)


def log(msg):
    print(json.dumps({"ts": time.time(), "spiffe_id": spiffe_id, "msg": msg}), flush=True)


log(f"SVID in hand after {ms} ms")
for child in args.child:
    name, sleep = child.split("=", 1)
    p = shim({"op": "spawn", "name": name, "argv": [sys.executable, __file__, "--sleep", sleep]})
    log(f"spawned {name} as {p['id']} {p['spiffe_id']}")
time.sleep(args.sleep)
log(f"exiting with {args.exit}")
sys.exit(args.exit)
