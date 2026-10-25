#!/usr/bin/env python3
"""Serve the real gateway request path on loopback for the POC 13 suite (eval/CONTRACT.md §1.6).

Loads gateway/gateway.py as a module, points its file-system constants at a scratch directory, gives it a route
table, an env file and an mTLS context made of test certificates, and serves `Handler` on 127.0.0.1. The only
seam: on loopback the client address is not 172.16.NN.2, so the slot and VM id the real `setup` derives from the
address and the jail are given on the command line instead (`--slot`, `--vm-id`). Everything the suite asserts
(routing, translation, the upstream request, the reply, the llm.call row, the hashes) runs in the gateway's code.

When `cedarpy` is not installed (a laptop), a stub that allows `llm.call` and denies everything else stands in,
exactly what gateway/policies/avos.cedar (`llm-all`) decides for model calls; the suite prints which one ran.

  gw_local.py serve --port P --certs DIR --store DIR --routes FILE --env FILE --jail DIR --vm-id ID --slot N
  gw_local.py route --table FILE MODEL SLOT        gateway.py route, with the same cedarpy fallback
"""
import argparse
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("AVOS_SUITE_ROOT") or os.path.abspath(os.path.join(HERE, "..", ".."))
GATEWAY = os.path.join(ROOT, "gateway", "gateway.py")


def cedarpy_stub():
    import enum

    class Decision(enum.Enum):
        Allow = "allow"
        Deny = "deny"

    class _Diag:
        def __init__(self, reasons):
            self.reasons, self.errors = reasons, []

    class _Result:
        def __init__(self, allow, reasons):
            self.decision = Decision.Allow if allow else Decision.Deny
            self.diagnostics = _Diag(reasons)

    m = types.ModuleType("cedarpy")
    m.Decision = Decision
    m.policies_to_json_str = lambda text: '{"staticPolicies": {}}'
    m.is_authorized = lambda req, policies, entities: _Result(req.get("action") == 'Action::"llm.call"', ["llm-all"]) \
        if req.get("action") == 'Action::"llm.call"' else _Result(False, [])
    m.__avos_stub__ = True
    return m


def load_gateway(store):
    os.environ["AVOS_TRACE_ROOT"] = store
    try:
        import cedarpy  # noqa: F401
        which = "real"
    except ImportError:
        sys.modules["cedarpy"] = cedarpy_stub()
        which = "stub"
    spec = importlib.util.spec_from_file_location("gateway", GATEWAY)
    gw = importlib.util.module_from_spec(spec)
    sys.modules["gateway"] = gw
    spec.loader.exec_module(gw)
    return gw, which


def serve(a):
    import ssl
    gw, which = load_gateway(a.store)
    os.makedirs(a.jail, exist_ok=True)
    gw.JAIL = a.jail
    gw.CUT = os.path.join(a.jail, "cut.json")
    gw.ROUTES = a.routes
    gw.UPSTREAM = gw.env_file(a.env)
    gw.ROUTE_TABLE = gw.load_routes(gw.UPSTREAM, a.routes)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_cert_chain(os.path.join(a.certs, "server.pem"), os.path.join(a.certs, "server.key"))
    ctx.load_verify_locations(os.path.join(a.certs, "ca.pem"))
    gw.CTX = ctx

    class LocalHandler(gw.Handler):
        def setup(self):
            super().setup()
            self.slot, self.vm_id = a.slot, a.vm_id

    srv = gw.Server(("127.0.0.1", a.port), LocalHandler)
    print(f"ready {srv.server_address[1]} cedarpy={which}", flush=True)
    srv.serve_forever()


def route(a):
    gw, which = load_gateway(a.store or os.path.join(os.environ.get("TMPDIR", "/tmp"), "gw-local-store"))
    print(f"cedarpy={which}", file=sys.stderr, flush=True)
    sys.exit(gw.route_cli(["--table", a.table, a.model, a.slot]))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--certs", required=True)
    s.add_argument("--store", required=True)
    s.add_argument("--routes", required=True)
    s.add_argument("--env", required=True)
    s.add_argument("--jail", required=True)
    s.add_argument("--vm-id", required=True)
    s.add_argument("--slot", type=int, default=1)
    r = sub.add_parser("route")
    r.add_argument("--table", required=True)
    r.add_argument("--store")
    r.add_argument("model")
    r.add_argument("slot")
    a = ap.parse_args()
    (serve if a.cmd == "serve" else route)(a)


if __name__ == "__main__":
    main()
