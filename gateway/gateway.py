#!/opt/avos/gateway/bin/python
"""Authorization gateway (POC 4). Runs on the host as root; the only way out of every microVM.

Each VM's egress forwarder (infra/agent/agent.py) relays a principal's connection here over mTLS with that
principal's own X.509 SVID. The gateway names the caller from the certificate (and checks it belongs to the VM the
connection comes from), decides with Cedar (policies/*.cedar), attaches upstream credentials only to allowed calls,
and writes one authz.decision event per request to the trace store (collect/store.py), in the stream of the caller's
VM. Calls name the decision that allowed them (authz_ref); every row carries the forwarder's X-Avos-Attempt number.
A model call also records hashes of what the model was shown and answered, never the text (seen(), docs/trace.md §5).

  POST /v1/...            llm.call     Model::<model>      forwarded to the upstream model API with its key
  POST /tools/<name>      tool.call    Tool::<name>        demo tools served here (issue_refund)
  CONNECT host:port       net.connect  Host::<host:port>   tunnelled if allowed (no host is, by default)

Kill switch (POC 5): supervisor/ctl.py writes the halted, killed or cut branches to /var/lib/avos/cut.json
({spiffe prefix: {receipt, state}}). A request from such a branch is denied before Cedar, and a response for a
branch that was halted or cut while its call was in flight is dropped instead of delivered; the call's event
carries the receipt that dropped it.

  gateway.py serve        mTLS on :8443
  gateway.py check        assert the policy's expected decisions (no host needed)
  gateway.py stats [VM]...   M5 authz_attributable_denials, llm.call totals
"""
import glob
import json
import os
import re
import secrets
import selectors
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cedarpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "collect"))
import store

ROOT = "/var/lib/avos"
JAIL = f"{ROOT}/jail/firecracker"
SPIRE = ["/opt/spire/bin/spire-server"]
SOCK = ["-socketPath", "/run/spire-server/api.sock"]
GATEWAY_ID = "spiffe://avoslab.com/gateway"
CERT_DIR = "/run/avos-gateway"
NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}"
SPIFFE = re.compile(rf"spiffe://avoslab\.com/vm/(vm-\d+-[0-9a-f]{{6}})((?:/agent/{NAME})(?:/sub/{NAME})*)?")
HOSTPORT = re.compile(r"[A-Za-z0-9.-]{1,253}:\d{1,5}")


def load_policies():
    text = "\n".join(open(f).read() for f in sorted(glob.glob(f"{HERE}/policies/*.cedar")))
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


def principal(spiffe_id):
    """Cedar entity for a SPIFFE ID: kind vm|agent|sub, its own name, and the top-level agent's name."""
    m = SPIFFE.fullmatch(spiffe_id)
    if not m:
        raise PermissionError(f"not an Avos principal: {spiffe_id}")
    path = (m[2] or "").split("/")[1:]
    kind, name, root = (path[-2], path[-1], path[1]) if path else ("vm", m[1], "")
    return m[1], {"uid": {"type": "Agent", "id": spiffe_id}, "attrs": {"kind": kind, "name": name, "root": root},
                  "parents": []}


def decide(spiffe_id, action, resource, args):
    """Cedar decision for one call. Returns (allowed, policy ids, errors)."""
    _, entity = principal(spiffe_id)
    r = cedarpy.is_authorized({"principal": f"Agent::{json.dumps(spiffe_id)}", "action": f"Action::{json.dumps(action)}",
                               "resource": resource, "context": args}, POLICIES, [entity])
    allowed = r.decision == cedarpy.Decision.Allow
    ids = [POLICY_IDS.get(p, p) for p in r.diagnostics.reasons] or ([] if allowed else ["default-deny"])
    return allowed, ids, [str(e) for e in r.diagnostics.errors]


def env_file(path):
    return dict(line.split("=", 1) for line in open(path).read().splitlines() if "=" in line)


def mint():
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
    """Copy bytes both ways until either side closes, from this one thread. A TLS socket must never be read by one
    thread while another writes it (OpenSSL objects are not thread-safe): with a thread per direction, a TLS 1.3
    session ticket arriving during the write lost the request about once in 2,600 calls, and the caller hung."""
    peer = {a: b, b: a}
    sel = selectors.DefaultSelector()
    try:
        for s in peer:
            s.setblocking(False)
            sel.register(s, selectors.EVENT_READ)
        while True:
            ready = [s for s in peer if isinstance(s, ssl.SSLSocket) and s.pending()] or \
                [k.fileobj for k, _ in sel.select()]
            for src in ready:
                try:
                    data = src.recv(65536)
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                    continue
                if not data:
                    return
                send_all(peer[src], data)
    except OSError:
        pass
    finally:
        sel.close()
        for s in peer:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def send_all(s, data):
    view = memoryview(data)
    while view:
        try:
            view = view[s.send(view):]
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
            with selectors.DefaultSelector() as w:
                w.register(s, selectors.EVENT_READ | selectors.EVENT_WRITE)
                if not w.select(30):
                    raise TimeoutError("peer stopped reading for 30 s")


def issue_refund(args):
    return {"refund_id": f"rf_{secrets.token_hex(3)}", "order_id": args["order_id"], "amount_cents": args["amount_cents"]}


TOOLS = {"issue_refund": (issue_refund, {"order_id": str, "amount_cents": int})}


def seen(sent, answer):
    """What the model was shown and what it answered, as hashes only (no text is kept on the host): the upstream
    response id, the system prompt, the tool schemas sent (null: none were), each request message in order, and the
    reply. The form is lib_trace's chat_form + content_sha, which a reader of the harness transcript recomputes."""
    T = store.T
    try:
        msgs = sent.get("messages") or []
        system = next((m.get("content") for m in msgs if m.get("role") == "system"), None)
        reply = answer["choices"][0]["message"] if isinstance(answer, dict) and answer.get("choices") else None
        return {"response_id": answer.get("id") if isinstance(answer, dict) else None,
                "system_sha256": None if system is None else T.content_sha(system),
                "tools_sha256": T.content_sha(sent["tools"]) if sent.get("tools") else None,
                "messages_sha256": [T.content_sha(T.chat_form(m)) for m in msgs],
                "output_sha256": None if reply is None else T.content_sha(T.chat_form(reply, "assistant"))}
    except (AttributeError, TypeError, KeyError, IndexError):
        return {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.do_handshake()
        san = dict((k, v) for k, v in self.connection.getpeercert().get("subjectAltName", ()) if k == "URI")
        self.spiffe_id = san.get("URI", "")
        m = re.fullmatch(r"172\.16\.(\d+)\.2", self.client_address[0])
        try:
            self.vm_id = json.load(open(f"{JAIL}/vm-{int(m[1]):02d}/identity.json"))["VM_ID"] if m else None
        except FileNotFoundError:
            self.vm_id = None

    def parse_request(self):
        """Count every request before anything is decided or written (M8 "happened"), in its principal's VM."""
        if not super().parse_request():
            return False
        n = self.headers.get("X-Avos-Attempt", "")
        self.attempt = int(n) if n.isdigit() else None
        self.home = store.vm_of(self.spiffe_id)
        store.count(self.home, "a")
        return True

    def emit(self, kind, **fields):
        if self.attempt is not None:
            fields["attempt"] = self.attempt
        return store.emit("gateway", kind, self.spiffe_id, self.home, **fields)

    def allowed(self, action, resource, args):
        """The allow decision event (calls name it in authz_ref), or None once a deny has been answered."""
        try:
            vm_id, _ = principal(self.spiffe_id)
            if vm_id != self.vm_id:
                self.emit("authz.decision", action=action, resource=resource, args=args, decision="deny",
                          policy_ids=["vm-binding"], errors=[], via_vm=self.vm_id)
                return self.deny(f"{self.spiffe_id} does not belong to the calling VM ({self.vm_id})", ["vm-binding"], args)
            if c := cut_for(self.spiffe_id):
                ids = [c["state"], c["receipt"]]
                self.emit("authz.decision", action=action, resource=resource, args=args, decision="deny",
                          policy_ids=ids, errors=[])
                return self.deny(f"{c['state']} by {c['receipt']}", ids, args)
            ok, ids, errors = decide(self.spiffe_id, action, resource, args)
            d = self.emit("authz.decision", action=action, resource=resource, args=args,
                          decision="allow" if ok else "deny", policy_ids=ids, errors=errors)
            return d if ok else self.deny(f"{action} on {resource} not permitted", ids, args)
        except PermissionError as e:
            return self.deny(str(e), [], args)

    def refuse(self, code, action, why, args):
        """A request no policy can be asked about (no such tool, bad body): still one decision, so every request
        the gateway received has its row."""
        if self.home:
            self.emit("authz.decision", action=action, resource=f"Path::{json.dumps(self.path)}", args=args,
                      decision="deny", policy_ids=["malformed-request"], errors=[why])
        self.reply(code, {"error": why, "policy_ids": ["malformed-request"]})

    def deny(self, why, ids, args):
        self.reply(403, {"error": why, "principal": self.spiffe_id, "policy_ids": ids, "args": args})
        return None

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_POST(self):
        if self.path.startswith("/v1/"):
            return self.llm(self.body())
        m = re.fullmatch(r"/tools/([a-z_]+)", self.path)
        if not m or m[1] not in TOOLS:
            self.body()
            return self.refuse(404, "tool.call", f"no tool at {self.path}", {})
        fn, types = TOOLS[m[1]]
        try:
            args = json.loads(self.body())
            if set(args) != set(types) or not all(type(args[k]) is t for k, t in types.items()):
                raise ValueError(f"arguments must be {', '.join(f'{k}: {t.__name__}' for k, t in types.items())}")
        except (ValueError, TypeError) as e:
            return self.refuse(400, "tool.call", str(e), {})
        if d := self.allowed("tool.call", f"Tool::{json.dumps(m[1])}", args):
            result = fn(args)
            store.count(self.home, "t")
            call = self.emit("tool.call", via="gateway", tool=m[1], args=args, authz_ref=d["event_id"])
            c = cut_for(self.spiffe_id)
            self.emit("tool.result", call_ref=call["event_id"], result=result,
                      **({"dropped": True, "receipt": c["receipt"]} if c else {}))
            if c:
                return self.deny(f"{c['state']} by {c['receipt']}", [c["state"], c["receipt"]], args)
            self.reply(200, result)

    def llm(self, body):
        try:
            sent = json.loads(body)
            model = sent["model"]
        except (ValueError, KeyError, TypeError):
            return self.refuse(400, "llm.call", "body must be JSON with a model", {})
        if not (d := self.allowed("llm.call", f"Model::{json.dumps(model)}", {})):
            return
        t0 = time.time()
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
            answer = json.loads(out)
        except ValueError:
            answer = None
        usage = (answer.get("usage") if isinstance(answer, dict) else None) or {}
        c = cut_for(self.spiffe_id)
        store.count(self.home, "l")
        self.emit("llm.call", authz_ref=d["event_id"], model=model, status=code, ms=round((time.time() - t0) * 1000),
                  prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                  dropped=bool(c), **({"receipt": c["receipt"]} if c else {}), **seen(sent, answer))
        if c:
            return self.deny(f"{c['state']} by {c['receipt']}", [c["state"], c["receipt"]], {})
        self.reply(code, out, ctype)

    def do_GET(self):
        self.close_connection = True
        self.refuse(405, "tool.call", f"{self.command} is not served; POST or CONNECT", {})

    do_HEAD = do_PUT = do_PATCH = do_DELETE = do_GET

    def do_CONNECT(self):
        if not HOSTPORT.fullmatch(self.path):
            return self.refuse(400, "net.connect", "CONNECT host:port", {})
        if not (d := self.allowed("net.connect", f"Host::{json.dumps(self.path)}", {})):
            return
        host, port = self.path.rsplit(":", 1)
        try:
            up = socket.create_connection((host, int(port)), timeout=10)
        except OSError as e:
            return self.reply(502, {"error": str(e)})
        self.emit("net.connect", authz_ref=d["event_id"], dest=self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()
        up.settimeout(None)
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
        print(f"{addr[0]}: {sys.exc_info()[1]!r}", flush=True)


def stats(*vm_ids):
    rows = list(store.ledger(vm_ids))
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
    cases = [
        (f"{vm}/agent/support", "tool.call", refund, 15000, True, "refund-support"),
        (f"{vm}/agent/support", "tool.call", refund, 48000, False, "default-deny"),
        (f"{vm}/agent/refund-manager", "tool.call", refund, 48000, True, "refund-manager"),
        (f"{vm}/agent/refund-manager", "tool.call", refund, 150000, False, "default-deny"),
        (f"{vm}/agent/refund-manager/sub/refund-manager", "tool.call", refund, 100, False, "default-deny"),
        (f"{vm}/agent/tau2", "llm.call", 'Model::"m"', None, True, "llm-all"),
        (vm, "llm.call", 'Model::"m"', None, True, "llm-all"),
        (f"{vm}/agent/support", "net.connect", 'Host::"pypi.org:443"', None, False, "default-deny"),
    ]
    for p, action, resource, cents, want, rule in cases:
        args = {"order_id": "#W1", "amount_cents": cents} if cents is not None else {}
        got, ids, _ = decide(p, action, resource, args)
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
        UPSTREAM = env_file(f"{ROOT}/gateway.env")
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
