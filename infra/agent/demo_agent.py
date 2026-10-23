"""Demo principal for POC 3 and 4, started only through the spawner shim (see agent.py).

Gets its X.509-SVID from the SPIRE Workload API (written to its private runtime dir), logs lines carrying its own
SPIFFE ID, spawns its children through the shim, calls the gateway's demo tool, tries to leave the VM, sleeps, exits.
(The shim measures M4 for every principal.)

  demo_agent.py [--refund CENTS]... [--then S] [--egress HOST] [--child NAME=SLEEP|NAME=ARGS]... [--sleep S] [--exit CODE]
                [--tick S] [--slow-llm MODEL]

--then S repeats the refunds after S seconds (past the SVID's 60 s TTL); a child's ARGS are its own demo_agent flags.
--tick S (POC 5) logs a numbered tick every S seconds for the --sleep period, with a gateway tool call every 10th
tick: the events whose loss or delay is M7. --slow-llm MODEL makes one long model call first (a halt lands mid-call).
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

T0 = float(os.environ["AVOS_T0"])
SVID_DIR = os.environ["AVOS_DIR"]


def shim(req):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(os.environ["AVOS_SPAWN"])
        s.sendall(json.dumps(req).encode() + b"\n")
        out = json.loads(s.makefile().readline())
    if "error" in out:
        raise SystemExit(out["error"])
    return out


def fetch_svid():
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
ap.add_argument("--tick", type=float, metavar="S")
ap.add_argument("--slow-llm", metavar="MODEL")
args = ap.parse_args()

spiffe_id = fetch_svid()
ms = round((time.time() - T0) * 1000)


def log(msg, **fields):
    print(json.dumps({"ts": time.time(), "spiffe_id": spiffe_id, "msg": msg, **fields}), flush=True)


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
    try:
        socket.create_connection((host, 443), timeout=3).close()
        log(f"egress {host} direct: CONNECTED")
    except OSError as e:
        log(f"egress {host} direct: blocked ({e})")
    c = http.client.HTTPConnection("127.0.0.1", 8090, timeout=10)
    c.set_tunnel(host, 443)
    try:
        c.connect()
        log(f"egress {host} via gateway: CONNECTED")
    except OSError as e:
        log(f"egress {host} via gateway: refused ({e})")


def slow_llm():
    req = urllib.request.Request(f"{FORWARDER}/v1/chat/completions", method="POST", data=json.dumps(
        {"model": args.slow_llm, "max_tokens": 1500,
         "messages": [{"role": "user", "content": "Count from 1 to 400, one number per line."}]}).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        out = json.load(urllib.request.urlopen(req, timeout=120))
        log(f"llm: 200 ({(time.time() - t0) * 1000:.0f} ms, {out.get('usage', {}).get('completion_tokens')} tokens)")
    except urllib.error.HTTPError as e:
        log(f"llm: {e.code} {json.load(e)}")
    except OSError as e:
        log(f"llm: error {e}")


def ticks():
    end, seq = time.time() + args.sleep, 0
    while time.time() < end:
        seq += 1
        extra = {}
        if seq % 10 == 0:
            req = urllib.request.Request(f"{FORWARDER}/tools/issue_refund", method="POST",
                                         data=json.dumps({"order_id": "#W2378156", "amount_cents": 100}).encode())
            try:
                urllib.request.urlopen(req, timeout=10)
                extra["gw"] = 200
            except urllib.error.HTTPError as e:
                extra["gw"] = e.code
                extra["policy"] = json.load(e).get("policy_ids")
            except OSError as e:
                extra["gw"] = f"error {e}"
        log("tick", tick=seq, **extra)
        time.sleep(args.tick)


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
if args.slow_llm:
    slow_llm()
if args.tick:
    ticks()
else:
    time.sleep(args.sleep)
log(f"exiting with {args.exit}")
sys.exit(args.exit)
