#!/opt/avos/gateway/bin/python
"""Authorization gateway (POC 4). Runs on the host as root; the only way out of every microVM.

Each VM's egress forwarder (infra/agent/agent.py) relays a principal's connection here over mTLS with that
principal's own X.509 SVID. The gateway names the caller from the certificate (and checks it belongs to the VM the
connection comes from), decides with Cedar (policies/*.cedar), attaches upstream credentials only to allowed calls,
and writes one authz.decision row per decision to /var/lib/avos/events.jsonl.

  POST /v1/...            llm.call     Model::<model>      forwarded to the upstream model API with its key
  POST /tools/<name>      tool.call    Tool::<name>        demo tools served here (issue_refund)
  CONNECT host:port       net.connect  Host::<host:port>   tunnelled if allowed (no host is, by default)

Kill switch (POC 5): supervisor/ctl.py writes the halted, killed or cut branches to /var/lib/avos/cut.json
({spiffe prefix: {receipt, state}}). A request from such a branch is denied before Cedar, and a response for a
branch that was halted or cut while its call was in flight is dropped instead of delivered.

  gateway.py serve        mTLS on :8443
  gateway.py check        assert the policy's expected decisions (no host needed)
  gateway.py stats [VM]...   M5 authz_attributable_denials, llm.call totals
"""
import glob
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cedarpy

ROOT = "/var/lib/avos"
EVENTS = f"{ROOT}/events.jsonl"
JAIL = f"{ROOT}/jail/firecracker"
HERE = os.path.dirname(os.path.abspath(__file__))
SPIRE = ["/opt/spire/bin/spire-server"]
SOCK = ["-socketPath", "/run/spire-server/api.sock"]
GATEWAY_ID = "spiffe://avoslab.com/gateway"
CERT_DIR = "/run/avos-gateway"
NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}"
SPIFFE = re.compile(rf"spiffe://avoslab\.com/vm/(vm-\d+-[0-9a-f]{{6}})((?:/agent/{NAME})(?:/sub/{NAME})*)?")
HOSTPORT = re.compile(r"[A-Za-z0-9.-]{1,253}:\d{1,5}")


def load_policies():
    text = "\n".join(open(f).read() for f in sorted(glob.glob(f"{HERE}/policies/*.cedar")))
    # Cedar names policies policy0, policy1, ...; map them to their @id so every decision names its rule.
    ids = {k: p["annotations"].get("id", k) for k, p in json.loads(cedarpy.policies_to_json_str(text))["staticPolicies"].items()}
    return text, ids


POLICIES, POLICY_IDS = load_policies()
CUT = f"{ROOT}/cut.json"
cut = {"mtime": None, "table": {}}


def cut_for(spiffe_id):
    """The kill-switch entry covering this principal (itself or an ancestor), or None. Re-read when the file changes."""
    try:
        mtime = os.stat(CUT).st_mtime_ns
        if mtime != cut["mtime"]:
            cut.update(mtime=mtime, table=json.load(open(CUT)))
    except FileNotFoundError:
        cut.update(mtime=None, table={})
    return next((v for k, v in cut["table"].items() if spiffe_id == k or spiffe_id.startswith(k + "/")), None)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def emit(kind, **fields):
    with open(EVENTS, "a") as f:
        f.write(json.dumps({"kind": kind, "ts": now(), **fields}) + "\n")


def principal(spiffe_id):
    """Cedar entity for a SPIFFE ID: kind vm|agent|sub, its own name, and the top-level agent's name."""
    m = SPIFFE.fullmatch(spiffe_id)
    if not m:
        raise PermissionError(f"not an Avos principal: {spiffe_id}")
    path = (m[2] or "").split("/")[1:]  # ["agent", "A", "sub", "B"]
    kind, name, root = (path[-2], path[-1], path[1]) if path else ("vm", m[1], "")
    return m[1], {"uid": {"type": "Agent", "id": spiffe_id}, "attrs": {"kind": kind, "name": name, "root": root},
                  "parents": []}


def decide(spiffe_id, action, resource, args, vm_id=None):
    """Cedar decision for one call, logged as authz.decision. Returns (allowed, policy ids)."""
    _, entity = principal(spiffe_id)
    r = cedarpy.is_authorized({"principal": f"Agent::{json.dumps(spiffe_id)}", "action": f"Action::{json.dumps(action)}",
                               "resource": resource, "context": args}, POLICIES, [entity])
    allowed = r.decision == cedarpy.Decision.Allow
    # No matching permit means Cedar's default deny; name it, so a denial always carries a rule.
    ids = [POLICY_IDS.get(p, p) for p in r.diagnostics.reasons] or ([] if allowed else ["default-deny"])
    emit("authz.decision", principal=spiffe_id, vm_id=vm_id, action=action, resource=resource, args=args,
         decision="allow" if allowed else "deny", policy_ids=ids, errors=[str(e) for e in r.diagnostics.errors])
    return allowed, ids


def env_file(path):
    return dict(line.split("=", 1) for line in open(path).read().splitlines() if "=" in line)


def mint():
    # The gateway's own server SVID, minted by the host's SPIRE Server; guests verify it with the trust bundle.
    os.makedirs(CERT_DIR, mode=0o700, exist_ok=True)
    subprocess.run([*SPIRE, "x509", "mint", "-spiffeID", GATEWAY_ID, "-ttl", "2h", "-write", CERT_DIR, *SOCK],
                   check=True, capture_output=True)
    bundle = subprocess.run([*SPIRE, "bundle", "show", *SOCK], check=True, capture_output=True, text=True).stdout
    CTX.load_cert_chain(f"{CERT_DIR}/svid.pem", f"{CERT_DIR}/key.pem")
    CTX.load_verify_locations(cadata=bundle)


def remint():
    while True:
        time.sleep(3600)
        mint()


def pipe(a, b):
    """Copy bytes both ways until either side closes."""
    def copy(src, dst):
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
    t = threading.Thread(target=copy, args=(b, a), daemon=True)
    t.start()
    copy(a, b)
    t.join()


def issue_refund(args):
    # Demo tool (stands in for a payment API the gateway holds credentials for).
    return {"refund_id": f"rf_{secrets.token_hex(3)}", "order_id": args["order_id"], "amount_cents": args["amount_cents"]}


TOOLS = {"issue_refund": (issue_refund, {"order_id": str, "amount_cents": int})}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: one TLS handshake per forwarded connection, not per call

    def setup(self):
        super().setup()
        self.connection.do_handshake()  # deferred out of accept(); a bad or expired SVID fails here
        san = dict((k, v) for k, v in self.connection.getpeercert().get("subjectAltName", ()) if k == "URI")
        self.spiffe_id = san.get("URI", "")
        # vm create pins each tap to its guest IP, so the source IP names the VM; its principals hold SVIDs under it.
        m = re.fullmatch(r"172\.16\.(\d+)\.2", self.client_address[0])
        try:
            self.vm_id = json.load(open(f"{JAIL}/vm-{int(m[1]):02d}/identity.json"))["VM_ID"] if m else None
        except FileNotFoundError:
            self.vm_id = None

    def allowed(self, action, resource, args):
        try:
            vm_id, _ = principal(self.spiffe_id)
            if vm_id != self.vm_id:
                emit("authz.decision", principal=self.spiffe_id, vm_id=self.vm_id, action=action, resource=resource,
                     args=args, decision="deny", policy_ids=["vm-binding"], errors=[])
                return self.deny(f"{self.spiffe_id} does not belong to the calling VM ({self.vm_id})", ["vm-binding"], args)
            if c := cut_for(self.spiffe_id):
                return self.cut_off(action, resource, args, c)
            ok, ids = decide(self.spiffe_id, action, resource, args, vm_id)
            return ok or self.deny(f"{action} on {resource} not permitted", ids, args)
        except PermissionError as e:
            return self.deny(str(e), [], args)

    def cut_off(self, action, resource, args, c):
        ids = [c["state"], c["receipt"]]
        emit("authz.decision", principal=self.spiffe_id, vm_id=self.vm_id, action=action, resource=resource,
             args=args, decision="deny", policy_ids=ids, errors=[])
        return self.deny(f"{c['state']} by {c['receipt']}", ids, args)

    def still_allowed(self, action, resource, args):
        # A halt or cut that landed while the call was upstream: the answer is not delivered (nor is it kept).
        c = cut_for(self.spiffe_id)
        return not c or self.cut_off(action, resource, args, c)

    def deny(self, why, ids, args):
        self.reply(403, {"error": why, "principal": self.spiffe_id, "policy_ids": ids, "args": args})
        return False

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_POST(self):
        if self.path.startswith("/v1/"):
            return self.llm(self.body())
        m = re.fullmatch(r"/tools/([a-z_]+)", self.path)
        if not m or m[1] not in TOOLS:
            return self.reply(404, {"error": f"no tool at {self.path}"})
        fn, types = TOOLS[m[1]]
        try:
            args = json.loads(self.body())
            if set(args) != set(types) or not all(type(args[k]) is t for k, t in types.items()):
                raise ValueError(f"arguments must be {', '.join(f'{k}: {t.__name__}' for k, t in types.items())}")
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        resource = f"Tool::{json.dumps(m[1])}"
        if self.allowed("tool.call", resource, args):
            result = fn(args)
            emit("tool.call", principal=self.spiffe_id, vm_id=self.vm_id, tool=m[1], args=args, result=result)
            if self.still_allowed("tool.call", resource, args):
                self.reply(200, result)

    def llm(self, body):
        try:
            model = json.loads(body)["model"]
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {"error": "body must be JSON with a model"})
        resource = f"Model::{json.dumps(model)}"
        if not self.allowed("llm.call", resource, {}):
            return
        t0 = time.time()
        # Limit: the upstream answer is read whole, so a streamed reply reaches the agent in one piece.
        req = urllib.request.Request(UPSTREAM["OPENAI_BASE_URL"].rstrip("/") + self.path[3:], data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {UPSTREAM['OPENAI_API_KEY']}"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                code, out, ctype = r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            code, out, ctype = e.code, e.read(), "application/json"
        except OSError as e:
            code, out, ctype = 502, json.dumps({"error": f"upstream: {e}"}).encode(), "application/json"
        try:
            usage = json.loads(out).get("usage") or {}
        except (ValueError, AttributeError):
            usage = {}
        delivered = self.still_allowed("llm.call", resource, {})
        emit("llm.call", principal=self.spiffe_id, vm_id=self.vm_id, model=model, status=code,
             ms=round((time.time() - t0) * 1000), prompt_tokens=usage.get("prompt_tokens"),
             completion_tokens=usage.get("completion_tokens"), dropped=not delivered)
        if delivered:
            self.reply(code, out, ctype)

    def do_CONNECT(self):
        if not HOSTPORT.fullmatch(self.path):
            return self.reply(400, {"error": "CONNECT host:port"})
        if not self.allowed("net.connect", f"Host::{json.dumps(self.path)}", {}):
            return
        host, port = self.path.rsplit(":", 1)
        try:
            up = socket.create_connection((host, int(port)), timeout=10)
        except OSError as e:
            return self.reply(502, {"error": str(e)})
        emit("net.connect", principal=self.spiffe_id, vm_id=self.vm_id, host=self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()
        up.settimeout(None)
        # Limit: a halt or cut after this point does not close the tunnel (data/poc5/README.md, tech debt).
        pipe(self.connection, up)
        self.close_connection = True

    def reply(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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
        print(f"{addr[0]}: {sys.exc_info()[1]!r}", flush=True)  # e.g. an expired or foreign SVID at handshake


def stats(*vm_ids):
    rows = [json.loads(line) for line in open(EVENTS)]
    rows = [r for r in rows if not vm_ids or r.get("vm_id") in vm_ids]
    denials = [r for r in rows if r["kind"] == "authz.decision" and r["decision"] == "deny"]
    ok = [r for r in denials if r.get("principal") and r.get("policy_ids") and "args" in r]
    decisions = sum(r["kind"] == "authz.decision" for r in rows)
    print(f"decisions: {decisions}, denials: {len(denials)}")
    if denials:
        print(f"M5 authz_attributable_denials: {len(ok)}/{len(denials)} = {100 * len(ok) / len(denials):.0f}%")
    calls = [r for r in rows if r["kind"] == "llm.call"]
    tokens = sum((r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0) for r in calls)
    print(f"llm.call: {len(calls)} calls, {tokens} tokens")


def check():
    vm = "spiffe://avoslab.com/vm/vm-01-abcdef"
    refund = 'Tool::"issue_refund"'
    cases = [  # principal, action, resource, context, expected decision, expected policy
        (f"{vm}/agent/support", "tool.call", refund, 15000, True, "refund-support"),
        (f"{vm}/agent/support", "tool.call", refund, 48000, False, "default-deny"),
        (f"{vm}/agent/refund-manager", "tool.call", refund, 48000, True, "refund-manager"),
        (f"{vm}/agent/refund-manager", "tool.call", refund, 150000, False, "default-deny"),
        (f"{vm}/agent/refund-manager/sub/refund-manager", "tool.call", refund, 100, False, "default-deny"),
        (f"{vm}/agent/tau2", "llm.call", 'Model::"m"', None, True, "llm-all"),
        (vm, "llm.call", 'Model::"m"', None, True, "llm-all"),
        (f"{vm}/agent/support", "net.connect", 'Host::"pypi.org:443"', None, False, "default-deny"),
    ]
    global emit
    emit = lambda *_, **__: None  # noqa: E731 (no ledger off-host)
    for p, action, resource, cents, want, rule in cases:
        args = {"order_id": "#W1", "amount_cents": cents} if cents is not None else {}
        got, ids = decide(p, action, resource, args)
        assert (got, ids) == (want, [rule]), (p, action, cents, got, ids)
    for bad in ("spiffe://avoslab.com/vm/vm-01-abcdef/sub/x", "spiffe://evil.com/vm/vm-01-abcdef/agent/a"):
        try:
            principal(bad)
            raise AssertionError(bad)
        except PermissionError:
            pass
    global CUT
    CUT = f"/tmp/avos-cut-check-{os.getpid()}.json"
    json.dump({f"{vm}/agent/support": {"receipt": "hr_test", "state": "cut"}}, open(CUT, "w"))
    assert cut_for(f"{vm}/agent/support/sub/x")["receipt"] == "hr_test" and cut_for(f"{vm}/agent/supporter") is None
    os.remove(CUT)
    print(f"policy check: {len(cases)} decisions as expected; kill switch matches by path prefix")


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "serve":
        UPSTREAM = env_file(f"{ROOT}/gateway.env")  # model API key: on the host only, never in a VM
        CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        CTX.verify_mode = ssl.CERT_REQUIRED
        mint()
        threading.Thread(target=remint, daemon=True).start()
        Server(("0.0.0.0", 8443), Handler).serve_forever()
    elif cmd == "check":
        check()
    elif cmd == "stats":
        stats(*args)
    else:
        sys.exit(__doc__)
