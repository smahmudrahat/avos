"""Independent oracles, fakes and synthetic artifacts for the POC 13 acceptance suite (eval/CONTRACT.md).

Nothing here knows how the solver works. The fakes stand in for the two upstreams the gateway talks to (an
Anthropic Messages endpoint and an OpenAI-compatible one) and record exactly what reached them; the oracles compute
the expected answer from raw files by the contract's definitions; the builders write fleets, run directories,
matrices and datasets the way infra/job, fleet matrix and retrain export leave them (reusing the sibling suites'
builders where they exist). Python 3.10+ standard library only.
"""
import hashlib
import http.client
import http.server
import importlib.util
import json
import math
import os
import random
import ssl
import statistics
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("AVOS_SUITE_ROOT") or os.path.abspath(os.path.join(HERE, "..", ".."))
FIX = os.path.join(HERE, "fixtures")
FIX_MANIFEST = os.path.join(FIX, "MANIFEST.sha256")
PRICES = os.path.join(FIX, "prices.json")
FLEET = os.path.join(ROOT, "fleet", "bin", "fleet")
TRAIN = os.path.join(ROOT, "train", "bin", "train")
LABEL = os.path.join(ROOT, "label", "bin", "label")
GW_LOCAL = os.path.join(HERE, "gw_local.py")
SPLIT = os.path.join(ROOT, "data", "splits", "retail_user_disjoint_v1.json")

CLOSED = "claude-opus-5-5"
PINNED = {"effort": "medium", "max_tokens": 16000, "cache": True}
ANTHROPIC_VERSION = "2023-06-01"
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens", "thinking_tokens")
FINISH = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length", "refusal": "content_filter"}
OPEN_ROLES = {"policy": "policy-qwen3.5-9b", "simulator": "google/gemma-3-27b-it", "nl_judge": "deepseek-ai/DeepSeek-V4-Flash-0731"}
CLOSED_ROLES = {**OPEN_ROLES, "policy": CLOSED}
TODAY_ROW_KEYS = {"event_id", "kind", "ts", "writer", "seq", "host", "principal", "vm_id", "prev", "hash", "authz_ref", "model",
                  "status", "ms", "prompt_tokens", "completion_tokens", "upstream", "upstream_status", "error", "finish_reason",
                  "dropped", "response_id", "system_sha256", "tools_sha256", "messages_sha256", "output_sha256", "attempt"}
VM_ID = "vm-01-abcdef"
SPIFFE_AGENT = f"spiffe://avoslab.com/vm/{VM_ID}/agent/tau2"
REAL_MATRICES = {"p11-heldout": "data/poc11/p11-heldout/matrix.json", "p12-heldout-base": "data/poc12/p12-heldout-base/matrix.json",
                 "p12-heldout": "data/poc12/p12-heldout/matrix.json", "p12-heldout-r2": "data/poc12/p12-heldout-r2/matrix.json"}
HASHED_REPO_FILES = [*REAL_MATRICES.values(), "data/poc12/pooled.json", "data/poc12/prices.json", "data/poc11/prices.json",
                     "data/splits/retail_user_disjoint_v1.json", "data/poc12/p12-heldout/fleet.json",
                     "fleet/tests/fixtures/results/run_retail-0_22/events.jsonl", "label/tests/fixtures/expected_poc8.json"]
# The served checkpoints a vLLM policy upstream reports in GET /v1/models `root` (CONTRACT §2.1): the retrained merge and the base.
ROOT_RETRAINED = "/opt/avos/ckpt/p13-retrained-merged"
ROOT_BASE = "/opt/avos/models/Qwen3.5-9B"


class NotImplementedCLI(Exception):
    """A contract path is missing: the check reports FAIL 'not implemented: <path>'."""


class SkipCheck(Exception):
    """The check cannot run here (SKIP with the reason)."""


# ---------------------------------------------------------------- sibling suites (read-only reuse of their builders)

def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def fleet_lib():
    return _load("p10lib", "fleet/tests/lib.py")


def train_lib():
    return _load("p12lib", "train/tests/lib.py")


def label_lib():
    return _load("p9lib", "label/tests/lib.py")


def trace_lib():
    return _load("lib_trace", "trace/lib_trace.py")


# ---------------------------------------------------------------- small helpers

def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_json(p, obj, indent=1):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=indent, sort_keys=True, ensure_ascii=False) + "\n")


def read_jsonl(p):
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def write_jsonl(p, rows):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def short(s, n=200):
    s = str(s).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def no_traceback(res):
    return "Traceback (most recent call last)" not in (res["err"] + res["out"])


def same_size_replace(path, old, new, count=1):
    """Different content, same size, same mtime: what a stale cache keyed on (size, mtime) cannot see."""
    if len(old) != len(new):
        raise AssertionError(f"same_size_replace needs equal lengths: {old!r} {new!r}")
    st = os.stat(path)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if old not in text:
        raise AssertionError(f"{old!r} not in {path}")
    text = text.replace(old, new, count)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    if os.stat(path).st_size != st.st_size or os.stat(path).st_mtime_ns != st.st_mtime_ns:
        raise AssertionError("same_size_replace changed size or mtime")


def run(argv, env=None, timeout=600, cwd=None, stdin=None):
    e = dict(os.environ)
    e.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if env:
        e.update(env)
    t0 = time.perf_counter()
    p = subprocess.run(argv, capture_output=True, text=True, env=e, timeout=timeout, cwd=cwd or ROOT, input=stdin)
    return {"rc": p.returncode, "out": p.stdout, "err": p.stderr, "secs": time.perf_counter() - t0}


def cli(path, *args, env=None, timeout=600):
    if not os.path.exists(path):
        raise NotImplementedCLI(os.path.relpath(path, ROOT))
    return run([sys.executable, path, *map(str, args)], env=env, timeout=timeout)


def private_env(tmp, name, extra=None):
    d = os.path.join(tmp, "env", name)
    e = {"TMPDIR": os.path.join(d, "tmp"), "HOME": os.path.join(d, "home"), "XDG_CACHE_HOME": os.path.join(d, "home", ".cache"),
         "XDG_DATA_HOME": os.path.join(d, "home", ".local"), "AVOS_TRACE_ROOT": os.path.join(d, "trace"),
         "AVOS_LABEL_ROOT": os.path.join(d, "labels"), "AVOS_LABEL_CACHE": os.path.join(d, "label-cache"),
         "PYTHONDONTWRITEBYTECODE": "1"}
    for v in e.values():
        if v.startswith(d):
            os.makedirs(v, exist_ok=True)
    e.update(extra or {})
    return e


# ---------------------------------------------------------------- fixtures

def fixture_drift():
    """Files whose SHA-256 differs from MANIFEST.sha256 (repo files and fixtures/)."""
    if not os.path.exists(FIX_MANIFEST):
        return ["MANIFEST.sha256 missing"]
    bad = []
    for line in open(FIX_MANIFEST):
        if not line.strip():
            continue
        want, rel = line.split(None, 1)
        rel = rel.strip()
        p = os.path.join(ROOT, rel) if not rel.startswith("fixtures/") else os.path.join(HERE, rel)
        if not os.path.exists(p):
            bad.append(f"{rel}: missing")
        elif sha_file(p) != want:
            bad.append(f"{rel}: changed")
    return bad


def write_fixture_manifest():
    lines = []
    for d, _, fs in os.walk(FIX):
        for n in sorted(fs):
            if n == "MANIFEST.sha256":
                continue
            p = os.path.join(d, n)
            lines.append(f"{sha_file(p)}  fixtures/{os.path.relpath(p, FIX)}")
    for rel in HASHED_REPO_FILES:
        lines.append(f"{sha_file(os.path.join(ROOT, rel))}  {rel}")
    with open(FIX_MANIFEST, "w") as f:
        f.write("\n".join(sorted(lines)) + "\n")


# ---------------------------------------------------------------- test PKI (openssl CLI)

def make_certs(d):
    """A CA, a server certificate (localhost, the gateway's SPIFFE id) and a client certificate carrying the agent's
    SPIFFE id as a URI SAN, the way the forwarder presents an SVID."""
    os.makedirs(d, exist_ok=True)

    def ossl(*args):
        p = subprocess.run(["openssl", *args], capture_output=True, text=True)
        if p.returncode:
            raise AssertionError(f"openssl {' '.join(args[:3])}: {short(p.stderr)}")

    def leaf(name, sans):
        cnf = os.path.join(d, f"{name}.cnf")
        with open(cnf, "w") as f:
            f.write("[req]\ndistinguished_name=dn\nreq_extensions=ext\nprompt=no\n[dn]\nCN=" + name +
                    "\n[ext]\nsubjectAltName=" + ",".join(sans) + "\nbasicConstraints=CA:FALSE\n"
                    "keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth,clientAuth\n")
        ossl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", os.path.join(d, f"{name}.key"),
             "-out", os.path.join(d, f"{name}.csr"), "-config", cnf)
        ossl("x509", "-req", "-in", os.path.join(d, f"{name}.csr"), "-CA", os.path.join(d, "ca.pem"), "-CAkey",
             os.path.join(d, "ca.key"), "-CAcreateserial", "-out", os.path.join(d, f"{name}.pem"), "-days", "2",
             "-extfile", cnf, "-extensions", "ext")

    ossl("req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", os.path.join(d, "ca.key"),
         "-out", os.path.join(d, "ca.pem"), "-days", "2", "-subj", "/CN=p13-test-ca")
    leaf("server", ["DNS:localhost", "IP:127.0.0.1", "URI:spiffe://avoslab.com/gateway"])
    leaf("client", [f"URI:{SPIFFE_AGENT}"])
    return d


# ---------------------------------------------------------------- fake upstreams

class FakeUpstream:
    """An HTTP server that records every request it gets and answers from a script. Each script entry is
    (status, headers, body bytes | object); the last entry repeats. `requests` holds {path, method, headers, body}."""

    def __init__(self):
        self.script, self.requests, self.lock = [], [], threading.Lock()
        self.require = None     # (header, value): answer 401 unless the request carries exactly that, as the real keyed upstreams do
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.do_POST("GET")

            def do_POST(self, method="POST"):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                with fake.lock:
                    fake.requests.append({"path": self.path, "method": method, "headers": {k.lower(): v for k, v in self.headers.items()},
                                          "body": body, "t": time.time()})
                    if fake.require and self.headers.get(fake.require[0]) != fake.require[1]:
                        entry = (401, {}, {"error": {"message": "Unauthorized", "type": "authentication_error"}})
                    else:
                        entry = fake.script.pop(0) if len(fake.script) > 1 else (fake.script[0] if fake.script else (404, {}, {"error": "unscripted"}))
                status, headers, out = entry
                data = out if isinstance(out, bytes) else json.dumps(out).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "content-type" not in {k.lower() for k in headers}:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def log_message(self, *_):
                pass

        class Srv(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, addr):   # a client dropping its keep-alive connection is not news
                pass

        self.srv = Srv(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def set(self, *entries):
        with self.lock:
            self.script = list(entries)
            self.requests = []

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def vllm_models(served_id, root):
    """A vLLM GET /v1/models answer: each served model with its `root`, the checkpoint path it was started on."""
    return {"object": "list", "data": [{"id": served_id, "object": "model", "created": 1761300000, "owned_by": "vllm", "root": root,
                                        "parent": None, "max_model_len": 32768, "permission": []}]}


def hosted_models(*ids):
    """A hosted OpenAI-compatible provider's GET /v1/models (many ids, no `root`: the weights are the provider's)."""
    return {"object": "list", "data": [{"id": i, "object": "model", "created": 1761300000, "owned_by": "system"} for i in ids]}


def msg(content, stop="end_turn", usage=None, mid="msg_01TestResponseId0001", model=CLOSED):
    """An Anthropic Messages response body."""
    u = {"input_tokens": 120, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 500, "output_tokens": 45,
         "output_tokens_details": {"thinking_tokens": 20}}
    if usage is not None:
        u = usage
    return {"id": mid, "type": "message", "role": "assistant", "model": model, "content": content, "stop_reason": stop,
            "stop_sequence": None, "usage": u}


def text(t):
    return {"type": "text", "text": t}


def tool_use(i, name, inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def thinking(t):
    return {"type": "thinking", "thinking": t, "signature": "sig_test"}


def usage_sum(u):
    """prompt_tokens as the contract defines it for a Messages reply (absent cache fields count 0)."""
    return (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)


# ---------------------------------------------------------------- the gateway under test, on loopback

class Gateway:
    """gw_local.py serve as a subprocess: real Handler, scratch store, route table, env file, test mTLS."""

    def __init__(self, tmp, name, routes, env_vars, certs, vm_id=VM_ID, slot=1):
        self.dir = os.path.join(tmp, "gw", name)
        os.makedirs(self.dir, exist_ok=True)
        self.store = os.path.join(self.dir, "store")
        self.routes = os.path.join(self.dir, "gateway.routes.json")
        self.envfile = os.path.join(self.dir, "gateway.env")
        self.jail = os.path.join(self.dir, "jail")
        self.certs = certs
        self.vm_id = vm_id
        write_json(self.routes, routes)
        with open(self.envfile, "w") as f:
            f.write("".join(f"{k}={v}\n" for k, v in env_vars.items()))
        os.chmod(self.envfile, 0o600)
        self.log = open(os.path.join(self.dir, "gateway.log"), "w+")
        self.proc = subprocess.Popen([sys.executable, GW_LOCAL, "serve", "--port", "0", "--certs", certs, "--store", self.store,
                                      "--routes", self.routes, "--env", self.envfile, "--jail", self.jail, "--vm-id", vm_id,
                                      "--slot", str(slot)], stdout=subprocess.PIPE, stderr=self.log, text=True,
                                     env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "AVOS_SUITE_ROOT": ROOT})
        line = ""
        t0 = time.time()
        while time.time() - t0 < 30:
            line = self.proc.stdout.readline()
            if line.startswith("ready") or self.proc.poll() is not None:
                break
            if not line:
                time.sleep(0.05)
        if not line.startswith("ready"):
            self.log.seek(0)
            err = self.log.read()
            self.stop()
            raise AssertionError(f"local gateway did not start: {short(err.strip().splitlines()[-1] if err.strip() else line, 300)}")
        parts = line.split()
        self.port = int(parts[1])
        self.cedarpy = parts[2].split("=", 1)[1] if len(parts) > 2 else "?"
        self.ctx = ssl.create_default_context(cafile=os.path.join(certs, "ca.pem"))
        self.ctx.load_cert_chain(os.path.join(certs, "client.pem"), os.path.join(certs, "client.key"))

    def call(self, body, path="/v1/chat/completions", headers=None, attempt=1, timeout=120):
        """One request as the forwarder relays it (mTLS, X-Avos-Attempt). Returns (status, headers, body bytes)."""
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        c = http.client.HTTPSConnection("localhost", self.port, context=self.ctx, timeout=timeout)
        h = {"Content-Type": "application/json", "X-Avos-Attempt": str(attempt), **(headers or {})}
        c.request("POST", path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        hs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hs, out

    def rows(self, kind="llm.call"):
        p = os.path.join(self.store, "vm", self.vm_id, "gateway.jsonl")
        return [r for r in (read_jsonl(p) if os.path.exists(p) else []) if kind is None or r["kind"] == kind]

    def everything_written(self):
        """Every byte the gateway wrote where a key could leak: the store and its own stdout/stderr."""
        blobs = []
        for d, _, fs in os.walk(self.store):
            for n in fs:
                blobs.append(open(os.path.join(d, n), "rb").read())
        self.log.flush()
        blobs.append(open(self.log.name, "rb").read())
        return b"\n".join(blobs)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


def anthropic_route(url, key_name="ANTHROPIC_API_KEY", **pinned):
    return {"base_url": url, "key": key_name, "kind": "anthropic", **{**PINNED, **pinned}}


def tau2_request(messages, tools=None, model=CLOSED, **extra):
    """What LiteLLM's openai/ provider sends for τ²'s llm_agent: the model without its provider prefix, the
    messages, the OpenAI tool schemas, and the sampling fields the closed model refuses."""
    body = {"model": model, "messages": messages, "temperature": 0.0, **extra}
    if tools is not None:
        body["tools"] = tools
    return body


TOOLS = [{"type": "function", "function": {"name": "get_order_details", "description": "Look up an order.",
                                           "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                                                          "required": ["order_id"]}}},
         {"type": "function", "function": {"name": "transfer_to_human_agents", "description": "Escalate.",
                                           "parameters": {"type": "object", "properties": {"summary": {"type": "string"}},
                                                          "required": ["summary"]}}}]
SYSTEM = "<instructions>\nYou are a customer service agent for a retail store.\n</instructions>"


def expect_hashes(sent, reply_message):
    """The §5 hashes on the OpenAI-shaped request and reply, exactly as for an open model."""
    T = trace_lib()
    msgs = sent.get("messages") or []
    system = next((m.get("content") for m in msgs if m.get("role") == "system"), None)
    return {"system_sha256": None if system is None else T.content_sha(system),
            "tools_sha256": T.content_sha(sent["tools"]) if sent.get("tools") else None,
            "messages_sha256": [T.content_sha(T.chat_form(m)) for m in msgs],
            "output_sha256": None if reply_message is None else T.content_sha(T.chat_form(reply_message, "assistant"))}


# ---------------------------------------------------------------- closed-policy fleets (fleet matrix inputs)

def closed_fleet(tmp, name, plan, seed=7, trials_per_task=None, policy_route=None, rows_patch=None, prices_models=None):
    """A fleet directory + run directories for a closed policy: fleet/tests' builder for everything infra/job and
    the open gateway write, then every policy row gains the §2 fields. plan: {task: [kind, ...]} with fleet/tests'
    kind names. rows_patch(run, i, row) may alter policy rows (to plant the refusal cases)."""
    L = fleet_lib()
    rnd = random.Random(seed)
    fdir, results, runs = L.build_synthetic_fleet(tmp, name, plan, roles=CLOSED_ROLES, seed=seed, trials_per_task=trials_per_task)
    meta = read_json(os.path.join(fdir, "fleet.json"))
    meta["policy_route"] = policy_route or {"kind": "anthropic", "upstream": "https://api.anthropic.com", **PINNED}
    write_json(os.path.join(fdir, "fleet.json"), meta)
    for run in runs:
        p = os.path.join(results, run, "events.jsonl")
        if not os.path.exists(p):
            continue
        rows = read_jsonl(p)
        for i, r in enumerate(rows):
            if r.get("kind") == "llm.call" and r.get("model") == CLOSED:
                r["route_kind"], r["effort"], r["max_tokens"], r["cache"] = "anthropic", PINNED["effort"], PINNED["max_tokens"], True
                r["dropped_fields"] = ["temperature"]
                if r["status"] == 200:
                    pt, ct = r["prompt_tokens"], r["completion_tokens"]
                    cr = rnd.randrange(0, pt // 2 + 1)
                    cw = rnd.randrange(0, (pt - cr) // 4 + 1)
                    th = rnd.randrange(0, ct + 1)
                    r["usage"] = {"input_tokens": pt - cr - cw, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
                                  "output_tokens": ct, "thinking_tokens": th}
                    r["stop_reason"] = "end_turn"
                    r["response_id"] = f"msg_{run}_{i}"
                else:
                    r["usage"] = dict.fromkeys(USAGE_KEYS)
                    r["stop_reason"] = None
                if rows_patch:
                    rows_patch(run, i, r)
        write_jsonl(p, rows)
    return fdir, results, runs


def closed_prices(path, cache=True, listed_on="2026-10-24"):
    p = read_json(PRICES)
    if not cache:
        for k in ("cache_write_per_mtok", "cache_read_per_mtok"):
            p["models"][CLOSED].pop(k, None)
    p["models"][CLOSED]["listed_on"] = listed_on
    write_json(path, p)
    return p


def closed_m12_oracle(fleet_dir, prices):
    """M12 of a token-priced policy from the rows and the prices alone (CONTRACT §3)."""
    meta = read_json(os.path.join(fleet_dir, "fleet.json"))
    lines = read_jsonl(os.path.join(fleet_dir, "trials.jsonl"))
    seen, trials = set(), []
    for ln in lines:
        if ln.get("interrupted") or ln["run"] in seen:
            continue
        seen.add(ln["run"])
        trials.append(ln)
    model = meta["roles"]["policy"]
    pm = (prices.get("models") or {}).get(model) or {}
    have_cache = all(isinstance(pm.get(k), (int, float)) for k in ("prompt_per_mtok", "completion_per_mtok", "cache_write_per_mtok", "cache_read_per_mtok"))
    have_plain = all(isinstance(pm.get(k), (int, float)) for k in ("prompt_per_mtok", "completion_per_mtok"))
    per_trial, calls, ok, agg, thinking_missing = {}, 0, 0, dict.fromkeys(USAGE_KEYS, 0), 0
    pt_sum = ct_sum = 0
    for t in trials:
        p = os.path.join(meta["results_root"], t["run"], "events.jsonl")
        usd = 0.0
        if os.path.exists(p):
            for r in read_jsonl(p):
                if r.get("kind") != "llm.call" or r.get("model") != model:
                    continue
                calls += 1
                if r.get("status") != 200:
                    continue
                ok += 1
                u = r["usage"]
                for k in USAGE_KEYS:
                    if k == "thinking_tokens" and u.get(k) is None:     # CONTRACT §2: thinking may be absent; never billed apart
                        thinking_missing += 1
                        continue
                    agg[k] += u[k]
                pt_sum += r["prompt_tokens"]
                ct_sum += r["completion_tokens"]
                if have_cache:
                    usd += (u["input_tokens"] * pm["prompt_per_mtok"] + u["cache_creation_input_tokens"] * pm["cache_write_per_mtok"]
                            + u["cache_read_input_tokens"] * pm["cache_read_per_mtok"] + u["output_tokens"] * pm["completion_per_mtok"]) / 1e6
        per_trial[t["run"]] = usd if have_cache else "unverified"
    n = len(trials)
    total = sum(v for v in per_trial.values()) if have_cache else "unverified"
    ceiling = ((pt_sum * pm["prompt_per_mtok"] + ct_sum * pm["completion_per_mtok"]) / 1e6 / n) if have_plain and n else "unverified"
    return {"model": model, "basis": "tokens", "calls": calls, "ok": ok, "usage": agg, "prompt_tokens": pt_sum, "completion_tokens": ct_sum,
            "usd_total": total, "usd_per_task": total / n if have_cache and n else "unverified",
            "uncached_ceiling_usd_per_task": ceiling, "per_trial_usd": per_trial, "trials": n,
            "thinking_tokens_missing": thinking_missing}


def open_m12_recompute(matrix, prices):
    """Hosted roles and the policy's token ceiling from a matrix's trials[] parts (fleet/CONTRACT.md §4)."""
    roles = matrix["inputs"]["roles"]
    ntr = matrix["m2"]["trials"]
    agg = {}
    for t in matrix["trials"]:
        if t["llm"] == "unverified":
            continue
        for m, b in t["llm"]["by_model"].items():
            a = agg.setdefault(m, {"calls": 0, "ok": 0, "prompt_tokens": 0, "completion_tokens": 0})
            for k in a:
                a[k] += b[k]
    out = {}
    for role in ("policy", "simulator", "nl_judge"):
        m = roles[role]
        a = agg.get(m, {"calls": 0, "ok": 0, "prompt_tokens": 0, "completion_tokens": 0})
        pm = prices["models"].get(m)
        tok = (a["prompt_tokens"] * pm["prompt_per_mtok"] + a["completion_tokens"] * pm["completion_per_mtok"]) / 1e6 if pm else "unverified"
        if role == "policy":
            out[role] = {**a, "token_ceiling_usd_per_task": tok / ntr if tok != "unverified" else tok, "basis": "gpu_hours"}
        else:
            out[role] = {**a, "usd_total": tok, "usd_per_task": tok / ntr if tok != "unverified" else tok}
    return out


# ---------------------------------------------------------------- M15 oracle

def pass_k(c, n, k):
    return math.comb(c, k) / math.comb(n, k) if n >= k else None


def percentile_ci(vals, seed=300, B=2000):
    """fleet/CONTRACT.md §4's bootstrap, applied to any per-task statistic: means[50], means[1949]."""
    rng = random.Random(seed)
    T = len(vals)
    if T < 2:
        return "unverified"
    means = sorted(statistics.fmean(vals[rng.randrange(T)] for _ in range(T)) for _ in range(B))
    return [means[50], means[1949]]


def ratio_ci(num_vals, den_vals, seed=300, B=2000):
    """Resample tasks (paired), recompute mean(num)/mean(den); undefined resamples are counted, never inf."""
    rng = random.Random(seed)
    T = len(num_vals)
    ratios, undefined = [], 0
    for _ in range(B):
        idx = [rng.randrange(T) for _ in range(T)]
        d = statistics.fmean(den_vals[i] for i in idx)
        if d == 0:
            undefined += 1
            continue
        ratios.append(statistics.fmean(num_vals[i] for i in idx) / d)
    ratios.sort()
    return ([ratios[50], ratios[1949]] if undefined == 0 else None), undefined


def session_facts(matrix):
    """What M15 reads from one matrix: per task (n, c, trials), policy cost per trial, roles, split."""
    tasks = {t["task"]: t for t in matrix["m11"]["tasks"]}
    m12p = matrix["m12"]["policy"]
    ntr = matrix["m2"]["trials"]
    per_trial = {}
    for t in matrix["trials"]:
        b = (t.get("llm") or {}).get("by_model", {}) if t.get("llm") != "unverified" else {}
        bp = b.get(matrix["inputs"]["roles"]["policy"]) or {}
        if m12p.get("basis") == "tokens" and isinstance(bp.get("usd"), (int, float)):
            per_trial[t["run"]] = (t["task"], bp["usd"])
        elif m12p.get("basis") == "gpu_hours" and isinstance(m12p.get("usd_total"), (int, float)) and ntr:
            per_trial[t["run"]] = (t["task"], m12p["usd_total"] / ntr)
        else:
            per_trial[t["run"]] = (t["task"], None)
    return {"fleet": matrix["inputs"]["fleet"], "policy": matrix["inputs"]["roles"]["policy"], "simulator": matrix["inputs"]["roles"]["simulator"],
            "split": matrix["inputs"]["split"]["sha256"], "part": matrix["inputs"]["split"]["part"], "tasks": tasks, "per_trial": per_trial,
            "pass1": matrix["m11"]["pass1"], "pass4": matrix["m11"]["pass4"], "usd_per_task": m12p.get("usd_per_task"), "trials": ntr}


def m15_oracle(retrained, closed, seed=300, bars=(0.75, 5.0)):
    """Pooled pass^k per arm, X, Y, the pass^4 difference, their intervals and the claim bars (CONTRACT §5)."""
    arms = {"retrained": [session_facts(m) for m in retrained], "closed": [session_facts(m) for m in closed]}
    ids = {t for s in arms["retrained"] for t in s["tasks"]}
    task_ids = sorted(ids, key=int) if all(t.isdigit() for t in ids) else sorted(ids)
    out = {"tasks": task_ids, "arms": {}}
    pooled = {}
    for arm, sessions in arms.items():
        nc = {t: [0, 0, 0] for t in task_ids}
        cost = {t: [0.0, 0] for t in task_ids}
        for s in sessions:
            for t in task_ids:
                nc[t][0] += s["tasks"][t]["n"]
                nc[t][1] += s["tasks"][t]["c"]
                nc[t][2] += s["tasks"][t]["trials"]
            for run, (t, usd) in s["per_trial"].items():
                if usd is not None:
                    cost[t][0] += usd
                    cost[t][1] += 1
        p1 = {t: pass_k(nc[t][1], nc[t][0], 1) for t in task_ids}
        p4 = {t: pass_k(nc[t][1], nc[t][0], 4) for t in task_ids}
        short4 = [t for t in task_ids if p4[t] is None]
        per_task_cost = {t: (cost[t][0] / cost[t][1] if cost[t][1] else None) for t in task_ids}
        pooled[arm] = {"nc": nc, "p1": p1, "p4": p4, "short4": short4, "cost": per_task_cost}
        p1v = [p1[t] for t in task_ids if p1[t] is not None]
        p4v = [p4[t] for t in task_ids if p4[t] is not None]
        cv = [per_task_cost[t] for t in task_ids if per_task_cost[t] is not None]
        out["arms"][arm] = {"sessions": [{"fleet": s["fleet"], "policy": s["policy"], "pass1": s["pass1"], "pass4": s["pass4"],
                                          "usd_per_task": s["usd_per_task"], "trials": s["trials"]} for s in sessions],
                            "pooled": {"pass1": statistics.fmean(p1v) if p1v else "unverified", "pass1_ci95": percentile_ci(p1v, seed) if p1v else "unverified",
                                       "pass4": statistics.fmean(p4v) if p4v else "unverified", "pass4_ci95": percentile_ci(p4v, seed) if p4v else "unverified",
                                       "usd_per_task": statistics.fmean(cv) if cv else "unverified",
                                       "trials": sum(v[2] for v in nc.values())},
                            "tasks_short_4": short4, "per_task": {t: {"n": nc[t][0], "c": nc[t][1]} for t in task_ids}}
        pp = out["arms"][arm]["pooled"]
        pp["usd_per_successful_task"] = pp["usd_per_task"] / pp["pass1"] if isinstance(pp["usd_per_task"], float) and isinstance(pp["pass1"], float) and pp["pass1"] > 0 else "unverified"
    both4 = [t for t in task_ids if pooled["retrained"]["p4"][t] is not None and pooled["closed"]["p4"][t] is not None]
    r4 = [pooled["retrained"]["p4"][t] for t in both4]
    c4 = [pooled["closed"]["p4"][t] for t in both4]
    d4 = [a - b for a, b in zip(r4, c4)]
    out["diff_pass4"] = {"value": statistics.fmean(d4) if d4 else "unverified", "ci95": percentile_ci(d4, seed) if d4 else "unverified", "tasks": len(both4)}

    def ratio(num, den, bar):
        if not num or not den:
            return {"value": None, "ci95": None, "verdict": "unresolved", "reason": "no_tasks", "bar": bar, "tasks": 0}
        mn, md = statistics.fmean(num), statistics.fmean(den)
        if md == 0:
            return {"value": None, "ci95": None, "verdict": "unresolved", "reason": "zero_denominator", "bar": bar, "tasks": len(num)}
        ci, undefined = ratio_ci(num, den, seed)
        v = mn / md
        r = {"value": v, "ci95": ci, "bar": bar, "tasks": len(num), "undefined_resamples": undefined}
        if ci is None:
            r.update(verdict="unresolved", reason="ratio_undefined_in_bootstrap")
        else:
            r["verdict"] = "pass" if ci[0] >= bar else ("fail" if ci[1] < bar else "unresolved")
        return r

    out["x"] = ratio(r4, c4, bars[0])
    both_cost = [t for t in task_ids if pooled["retrained"]["cost"][t] is not None and pooled["closed"]["cost"][t] is not None]
    if not both_cost:
        out["y"] = {"value": None, "ci95": None, "verdict": "unresolved", "reason": "cost_unverified", "bar": bars[1], "tasks": 0}
    else:
        out["y"] = ratio([pooled["closed"]["cost"][t] for t in both_cost], [pooled["retrained"]["cost"][t] for t in both_cost], bars[1])
    return out


CLOSED_ROUTE = {"kind": "anthropic", "upstream": "https://api.anthropic.com", **PINNED}


def open_route(root, upstreams=("http://10.0.1.21:8000/v1",), policy=None):
    """inputs.policy_route of an open-policy fleet started under CONTRACT §2.1: every upstream's served root recorded."""
    return {"kind": "openai", "upstream": list(upstreams) if len(upstreams) > 1 else upstreams[0],
            "served": [{"upstream": u, "id": policy or OPEN_ROLES["policy"], "root": root} for u in upstreams]}


def synthetic_matrix(base, fleet, policy, nc, basis="tokens", usd_per_trial=None, simulator=None, split_sha=None, drop_tasks=(), pass_patch=None,
                     policy_route="default"):
    """A fleet-matrix JSON of the real shape with chosen (n, c) per task; pass fields recomputed from the parts.
    For a token-priced policy every trial carries by_model[policy].usd and usage. policy_route: what `fleet matrix`
    copies into inputs.policy_route ("default": the pinned anthropic route for basis tokens, else the base's; None removes it)."""
    m = json.loads(json.dumps(base))
    m["inputs"]["fleet"] = fleet
    m["inputs"]["roles"]["policy"] = policy
    if policy_route == "default":
        policy_route = dict(CLOSED_ROUTE) if basis == "tokens" else m["inputs"].get("policy_route")
    if policy_route is None:
        m["inputs"].pop("policy_route", None)
    else:
        m["inputs"]["policy_route"] = policy_route
    if simulator:
        m["inputs"]["roles"]["simulator"] = simulator
    if split_sha:
        m["inputs"]["split"]["sha256"] = split_sha
    tasks = [t for t in m["m11"]["tasks"] if t["task"] not in drop_tasks]
    trials = []
    rnd = random.Random(11)
    for t in tasks:
        n, c = nc.get(t["task"], (t["n"], t["c"]))
        t["n"], t["c"], t["trials"], t["infra"] = n, c, n, 0
        t["pass1"], t["pass4"] = pass_k(c, n, 1), pass_k(c, n, 4)
        t["avg4"] = c / n if n else None
        for i in range(n):
            run = f"run_retail-{t['task']}_{i}"
            bm = {policy: {"calls": 10, "ok": 10, "prompt_tokens": 50000 + rnd.randrange(1000), "completion_tokens": 2000 + rnd.randrange(100), "ms": 60000}}
            if basis == "tokens":
                pt = bm[policy]["prompt_tokens"]
                bm[policy]["usage"] = {"input_tokens": pt // 2, "cache_creation_input_tokens": pt // 4, "cache_read_input_tokens": pt - pt // 2 - pt // 4,
                                       "output_tokens": bm[policy]["completion_tokens"], "thinking_tokens": 800}
                bm[policy]["usd"] = usd_per_trial if usd_per_trial is not None else round(0.4 + rnd.random() * 0.4, 6)
            trials.append({"run": run, "task": t["task"], "slot": "vm-01", "status": "done", "reward": 1.0 if i < c else 0.0,
                           "termination_reason": "user_stop", "wall_ms": 100000, "cause": None, "cause_class": None, "valid": True,
                           "passed": i < c, "llm": {"calls": 10, "ms": 60000, "by_model": bm}, "host_side_ms": 40000,
                           "m8": {"status": "complete", "ratio": 1.0}})
    m["m11"]["tasks"] = tasks
    for k in (1, 4):
        vals = [t[f"pass{k}"] for t in tasks if t[f"pass{k}"] is not None]
        m["m11"][f"pass{k}"] = statistics.fmean(vals) if vals else "unverified"
        m["m11"][f"pass{k}_ci95"] = percentile_ci(vals) if vals else "unverified"
    m["trials"] = sorted(trials, key=lambda t: t["run"])
    m["m2"] = {"done": len(trials), "trials": len(trials), "value": 1.0}
    pol = m["m12"]["policy"]
    if basis == "tokens":
        total = sum(t["llm"]["by_model"][policy]["usd"] for t in trials)
        pol.update(basis="tokens", usd_total=total, usd_per_task=total / len(trials), model=policy)
        pol["usd_per_successful_task"] = pol["usd_per_task"] / m["m11"]["pass1"] if isinstance(m["m11"]["pass1"], float) and m["m11"]["pass1"] else "unverified"
        pol.pop("gpu_hosts", None)
    else:
        pol.update(basis="gpu_hours", model=policy)
        if usd_per_trial is not None:
            pol["usd_total"] = usd_per_trial * len(trials)
            pol["usd_per_task"] = usd_per_trial
    if pass_patch:
        pass_patch(m)
    return m


# ---------------------------------------------------------------- datasets for train select / label (closed items)

def closed_dataset(tmp, name, mark="items", fleet_policy=None):
    """A retrain-export + labels + fleet.json triple built from train/tests' 12 real items, every item with
    policy_model = the closed model (mark="items"), or an open export under a fleet whose policy role is closed
    (mark="fleet"). Returns (export_dir, labels_dir, fleet_json, items)."""
    T = train_lib()
    items = T.load_items()
    rows = {r["run"]: r for r in T.load_rows()}
    out_items, out_rows = [], []
    for it in items:
        it = json.loads(json.dumps(it))
        if mark == "items":
            it["policy_model"] = CLOSED
        else:
            it["policy_model"] = OPEN_ROLES["policy"]
        T.rehash(it)
        out_items.append(it)
        out_rows.append(T.row_for(it, rows[it["run"]]))
    ex, lb = T.make_pair(tmp, name, out_items, out_rows, fleet_patch={"policy": fleet_policy or (CLOSED if mark == "fleet" else OPEN_ROLES["policy"])})
    write_jsonl(os.path.join(ex, "sft.jsonl"), [])
    with open(os.path.join(ex, "stats.json"), "w") as f:
        f.write("{}\n")
    return ex, lb, T.fleet_path(ex), out_items


def add_closed_run(env, new_run="run_retail-0_70", new_vm="vm-01-aa0070", src_run="run_retail-0_13"):
    """One run cloned (POC 8 suite builder) from a real one into a label/tests fixture environment, with every gateway
    call, model decision and witness model renamed to the closed model, as a closed-policy trial leaves it."""
    L8 = label_lib().L
    L8.clone_run(env, src_run, new_run, new_vm)
    gw = os.path.join(env.store, "vm", new_vm, "gateway.jsonl")
    rows = read_jsonl(gw)
    old = {r["model"] for r in rows if r.get("kind") == "llm.call"}
    for r in rows:
        if r.get("kind") == "llm.call":
            r["model"] = CLOSED
        elif r.get("kind") == "authz.decision" and str(r.get("resource", "")).startswith("Model::"):
            r["resource"] = f'Model::"{CLOSED}"'
    L8._rechain([(gw, rows)])
    write_jsonl(gw, rows)
    rp = os.path.join(env.results, new_run, "result.json")
    text_ = open(rp, encoding="utf-8").read()
    for m in old:
        text_ = text_.replace(m, CLOSED)
    with open(rp, "w", encoding="utf-8") as f:
        f.write(text_)
    return new_run, new_vm


def diff(got, want, path="", tol=1e-9, out=None):
    """Structural differences between two JSON values (numbers within tol)."""
    out = [] if out is None else out
    if isinstance(want, dict):
        if not isinstance(got, dict):
            out.append(f"{path or '.'}: {short(got, 60)!r} is not an object")
            return out
        for k, v in want.items():
            if k not in got:
                out.append(f"{path}.{k}: missing")
            else:
                diff(got[k], v, f"{path}.{k}", tol, out)
    elif isinstance(want, list):
        if not isinstance(got, list) or len(got) != len(want):
            out.append(f"{path}: list {short(got, 80)!r} != {short(want, 80)!r}")
        else:
            for i, (g, w) in enumerate(zip(got, want)):
                diff(g, w, f"{path}[{i}]", tol, out)
    elif isinstance(want, bool) or want is None or isinstance(want, str):
        if got != want:
            out.append(f"{path}: {short(got, 80)!r} != {short(want, 80)!r}")
    elif isinstance(want, (int, float)):
        if not isinstance(got, (int, float)) or isinstance(got, bool) or abs(got - want) > tol * max(1.0, abs(want)):
            out.append(f"{path}: {short(got, 40)!r} != {want!r}")
    return out
