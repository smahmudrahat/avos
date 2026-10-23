"""Independent oracles, the stub judge and dataset builders for the POC 9 acceptance suite (label/CONTRACT.md).

Nothing here knows how the solver works. Every function reads a POC 8 dataset, runs a contract CLI as a black
box, serves an OpenAI-compatible judge the tests control, or computes an *expected* answer from the raw rows.
Python 3.10+ standard library only. POC 8's suite library is reused for the environment (store, results,
ledger, cloning real runs) and for the canonical item hash.
"""
import hashlib
import http.server
import json
import math
import os
import random
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from collections import Counter, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
FIX = os.path.join(HERE, "fixtures")
FIX_MANIFEST = os.path.join(FIX, "MANIFEST.sha256")
LABEL = os.path.join(ROOT, "label", "bin", "label")
RETRAIN_TESTS = os.path.join(ROOT, "retrain", "tests")


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = _load("poc8_testlib", os.path.join(RETRAIN_TESTS, "lib.py"))  # POC 8 suite: Env, clone_run, retrain(), hashes
NotImplementedCLI = L.NotImplementedCLI
sha_file, read_jsonl, write_jsonl, tree_snapshot, same_size_replace, tamper_dir, short = (
    L.sha_file, L.read_jsonl, L.write_jsonl, L.tree_snapshot, L.same_size_replace, L.tamper_dir, L.short)

TAU2_384 = {"10", "12", "25", "50", "57", "65"}
DUMMY_KEY = "sk-p9-stub-0f3a9c1d7e5b4a2f8c6d0e1f2a3b4c5d-do-not-leak"
RULE_SENTENCE = ("Reward decides pass/fail; the judge can veto a pass; the user settles vetoes in the gold set. "
                 "Flagged or incomplete items are never labelled clean.")
ANSWER_KEYS = ("policy_followed", "right_tools", "hallucinated_facts", "unsafe_action")
LEAK = re.compile(r"reward|passed|failed|termination|db_match|match=|met=|WITNESS|trace=|witness=|split=|verdict|"
                  r"veto|judge|kappa|stratum|flagged|kept|label=|gold|nl_assertion|cost |usd=|STUBREASON", re.I)
VERBATIM = 60  # CONTRACT.md §4: no run of this many transcript characters may be stored


# ----------------------------------------------------------------------------- CLI

def label(*args, env=None, timeout=900, rss=False):
    if not os.path.exists(LABEL):
        raise NotImplementedCLI(os.path.relpath(LABEL, ROOT))
    return L._run([sys.executable, LABEL, *map(str, args)], env=env, timeout=timeout, rss=rss)


def retrain(*args, env=None, timeout=900):
    return L.retrain(*args, env=env, timeout=timeout)


def no_traceback(res):
    return "Traceback" not in (res["err"] or "")


def fixture_drift():
    want = {}
    for line in open(FIX_MANIFEST, encoding="utf-8"):
        if line.strip():
            h, rel = line.rstrip("\n").split("  ", 1)
            want[rel] = h
    have = {}
    for d, _, fs in os.walk(FIX):
        for n in fs:
            rel = os.path.relpath(os.path.join(d, n), FIX)
            if rel != "MANIFEST.sha256":
                have[rel] = sha_file(os.path.join(d, n))
    return sorted(r for r in set(want) | set(have) if want.get(r) != have.get(r))


def write_fixture_manifest():
    rows = []
    for d, _, fs in os.walk(FIX):
        for n in fs:
            rel = os.path.relpath(os.path.join(d, n), FIX)
            if rel != "MANIFEST.sha256":
                rows.append(f"{sha_file(os.path.join(d, n))}  {rel}")
    open(FIX_MANIFEST, "w", encoding="utf-8").write("\n".join(sorted(rows)) + "\n")


# ----------------------------------------------------------------------------- the environment (POC 8's, plus POC 9 cases)

def ctx_overflow(sim):
    sim["termination_reason"] = "context_window_exceeded"


def build_env(tmp, name="env"):
    """POC 8's fixture environment plus the POC 9 cases: a #384 task with reward 1.0, a null reward, a tampered
    witness, truncated and missing counters, a context-window overflow."""
    e = L.Env(tmp, name)
    L.clone_run(e, "run_retail-0_13", "run_retail-50_0", "vm-01-aa0050", task_id="50")
    L.clone_run(e, "run_retail-0_11", "run_retail-0_90", "vm-01-aa0090", reward_info=None)
    L.clone_run(e, "run_retail-0_12", "run_retail-0_91", "vm-01-aa0091", tamper_usage=True)
    L.clone_run(e, "run_retail-0_14", "run_retail-0_92", "vm-01-aa0092", counters="truncate")
    L.clone_run(e, "run_retail-0_14", "run_retail-0_93", "vm-01-aa0093", counters="none")
    L.clone_run(e, "run_retail-0_16", "run_retail-0_95", "vm-01-aa0095", mutate=ctx_overflow)
    return e


def export_dataset(e, name, *args):
    """POC 8 export into e.out(name); returns {"dir", "items", "manifest", "res"}."""
    out = e.out(name)
    res = retrain("export", "--out", out, "--split", L.SPLIT_REL, *(args or ("--all",)), env=e.env())
    if not os.path.exists(os.path.join(out, "manifest.json")):
        raise AssertionError(f"POC 8 export failed: rc={res['rc']} {short(res['err'], 300)}")
    return {"dir": out, "items": read_jsonl(os.path.join(out, "trajectories.jsonl")),
            "manifest": json.load(open(os.path.join(out, "manifest.json"))), "res": res}


class Roots:
    """Private label root, cache, TMPDIR, HOME and judge env file for one scenario."""

    def __init__(self, tmp, name):
        self.dir = os.path.join(tmp, "roots", name)
        shutil.rmtree(self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "labels")
        self.cache = os.path.join(self.dir, "cache")
        self.tmpdir = os.path.join(self.dir, "tmp")
        self.home = os.path.join(self.dir, "home")
        self.outs = os.path.join(self.dir, "out")
        for d in (self.root, self.cache, self.tmpdir, self.home, self.outs):
            os.makedirs(d, exist_ok=True)
        self.envfile = os.path.join(self.dir, "judge.env")
        self.n_out = 0

    def judge_env(self, base_url, key=DUMMY_KEY):
        with open(self.envfile, "w") as f:
            f.write(f"OPENAI_API_KEY={key}\nOPENAI_BASE_URL={base_url}\n")
        os.chmod(self.envfile, 0o600)

    def env(self, **extra):
        e = {"AVOS_LABEL_ROOT": self.root, "AVOS_LABEL_CACHE": self.cache, "AVOS_JUDGE_ENV": self.envfile,
             "TMPDIR": self.tmpdir, "HOME": self.home, "XDG_CACHE_HOME": os.path.join(self.home, ".cache"),
             "XDG_DATA_HOME": os.path.join(self.home, ".local", "share"),
             "XDG_STATE_HOME": os.path.join(self.home, ".local", "state"),
             "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
             "AVOS_TRACE_ROOT": os.path.join(self.dir, "no-store"), "AVOS_RESULTS_ROOT": os.path.join(self.dir, "no-results")}
        e.update(extra)
        return e

    def out(self, name=None):
        self.n_out += 1
        return os.path.join(self.outs, name or f"o{self.n_out}")

    def clear_caches(self):
        for d in (self.cache, self.tmpdir, self.home):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)

    def cache_dirs(self):
        return [self.cache, self.tmpdir, self.home]

    def clear_root(self):
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root)


def out_files(out):
    return {n: os.path.join(out, n) for n in ("labels.jsonl", "summary.json", "manifest.json", "gold.json", "CARD.md")}


def read_out(out):
    f = out_files(out)
    d = {"dir": out, "rows": [], "summary": None, "manifest": None, "gold": None, "card": None}
    if os.path.exists(f["labels.jsonl"]):
        d["rows"] = read_jsonl(f["labels.jsonl"])
    for k in ("summary", "manifest", "gold"):
        p = f[k + ".json"]
        if os.path.exists(p):
            d[k] = json.load(open(p))
    if os.path.exists(f["CARD.md"]):
        d["card"] = open(f["CARD.md"], encoding="utf-8").read()
    return d


def out_bytes(out):
    """Bytes of the five outputs; summary.json without the two per-run counters (CONTRACT.md §11)."""
    d = {}
    for n, p in out_files(out).items():
        if not os.path.exists(p):
            d[n] = None
            continue
        b = open(p, "rb").read()
        if n == "summary.json":
            try:
                j = json.loads(b)
                if isinstance(j.get("judge"), dict):
                    j["judge"].pop("calls_made", None)
                    j["judge"].pop("calls_reused", None)
                b = json.dumps(j, sort_keys=True).encode()
            except ValueError:
                pass
        d[n] = b
    return d


def missing_outputs(out):
    return [n for n, p in out_files(out).items() if not os.path.exists(p)]


# ----------------------------------------------------------------------------- the stub judge (OpenAI-compatible, adversarial, deterministic)

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, obj, raw=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if raw is None else "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/_ctl"):
            return self._send(200, self.server.judge.snapshot())
        self._send(404, {"error": "no"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path.startswith("/_ctl"):
            self.server.judge.ctl.update(json.loads(raw or b"{}"))
            return self._send(200, {"ok": True})
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": {"message": f"unknown path {self.path}"}})
        code, obj, rawbody = self.server.judge.answer(raw, self.headers.get("Authorization"))
        self._send(code, obj, rawbody)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


class Judge:
    """Deterministic, adversarial OpenAI-compatible judge.

    ctl keys: verdict {item_id: pass|veto}; default 'hash'|'pass'|'veto'; inconsistent [item_id] (first framing
    body pass, second veto); always_invalid [item_id] (2xx but never a valid answer); faults bool (the first
    hash%3 attempts of each distinct body fail: 500, non-JSON, truncated/length, wrong schema, string booleans);
    drift bool (every valid answer to the same body flips the verdict); delay {item_id: secs}; status int (every
    call answers that code); quote_reasons bool (reasons quote 80 transcript characters); keep_bodies bool.
    """

    def __init__(self, reason_marker="STUBREASON"):
        self.ctl = {"verdict": {}, "default": "hash", "inconsistent": [], "always_invalid": [], "faults": False,
                    "drift": False, "delay": {}, "status": None, "quote_reasons": True, "keep_bodies": True,
                    "must_contain": []}
        self.marker = reason_marker
        self.lock = threading.Lock()
        self.calls = []            # one dict per HTTP attempt
        self.attempts = Counter()  # body_sha -> attempts
        self.valid_served = Counter()
        self.served = {}           # (item, body_sha) -> last valid answers dict
        self.bodies_by_item = OrderedDict()
        self.srv = None

    # -- lifecycle
    def start(self):
        self.srv = _Server(("127.0.0.1", 0), _Handler)
        self.srv.judge = self
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        return self

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def reset(self, **ctl):
        with self.lock:
            self.calls, self.attempts, self.valid_served, self.served = [], Counter(), Counter(), {}
            self.bodies_by_item = OrderedDict()
            self.ctl.update(ctl)

    def set(self, **ctl):
        with self.lock:
            self.ctl.update(ctl)

    def snapshot(self):
        with self.lock:
            return {"calls": len(self.calls), "by_item": dict(Counter(c["item"] for c in self.calls))}

    @property
    def n_calls(self):
        with self.lock:
            return len(self.calls)

    def calls_for(self, item=None):
        with self.lock:
            return [c for c in self.calls if item is None or c["item"] == item]

    # -- answers
    def _verdict(self, item, body_sha, k_valid):
        c = self.ctl
        if item in c["inconsistent"]:
            order = list(self.bodies_by_item.get(item, {}))
            v = "pass" if order.index(body_sha) % 2 == 0 else "veto"
        elif item in c["verdict"]:
            v = c["verdict"][item]
        elif c["default"] == "hash":
            v = "pass" if int(hashlib.sha256((item or body_sha).encode()).hexdigest(), 16) % 2 == 0 else "veto"
        else:
            v = c["default"]
        if c["drift"] and k_valid % 2 == 1:
            v = "veto" if v == "pass" else "pass"
        return v

    def answers_for(self, item, verdict, body_sha):
        h = int(hashlib.sha256((body_sha + verdict).encode()).hexdigest(), 16)
        a = {"policy_followed": True, "right_tools": True, "hallucinated_facts": False, "unsafe_action": False}
        if verdict == "veto":  # one of the four flips, chosen by hash
            k = ANSWER_KEYS[h % 4]
            a[k] = not a[k]
        return a

    def answer(self, raw, auth):
        c = dict(self.ctl)
        if c["status"]:
            return c["status"], {"error": {"message": f"stub status {c['status']}", "type": "stub"}}, None
        try:
            body = json.loads(raw)
        except ValueError:
            return 400, {"error": {"message": "body is not JSON"}}, None
        item = body.get("user")
        text = "".join(str(m.get("content") or "") for m in body.get("messages") or [])
        body_sha = hashlib.sha256(raw).hexdigest()
        with self.lock:
            self.attempts[body_sha] += 1
            k = self.attempts[body_sha]
            self.bodies_by_item.setdefault(item, OrderedDict())[body_sha] = True
            rec = {"n": len(self.calls) + 1, "item": item, "body_sha": body_sha, "attempt": k, "auth": auth,
                   "model": body.get("model"), "temperature": body.get("temperature"), "seed": body.get("seed"),
                   "len": len(raw), "contains": {s: (s in text) for s in c["must_contain"]},
                   "text": text if c["keep_bodies"] else None, "kind": None}
            self.calls.append(rec)
        if item in c["delay"]:
            time.sleep(float(c["delay"][item]))
        hb = int(body_sha[:8], 16)
        n_faults = hb % 3 if c["faults"] else 0
        kind = None
        if item in c["always_invalid"]:
            kind = "wrong_schema"
        elif k <= n_faults:
            kind = ("500", "not_json", "length", "wrong_schema", "string_bools")[(hb + k) % 5]
        rec["kind"] = kind or "valid"
        usage = {"prompt_tokens": len(text) // 4, "completion_tokens": 40, "total_tokens": len(text) // 4 + 40}
        if kind == "500":
            return 500, {"error": {"message": "stub upstream error", "type": "server_error"}}, None
        if kind == "not_json":
            return 200, None, b"upstream returned html <html><body>Bad gateway</body></html>"
        with self.lock:
            k_valid = self.valid_served[body_sha]
            verdict = self._verdict(item, body_sha, k_valid)
        answers = self.answers_for(item, verdict, body_sha)
        reasons = [f"{self.marker}-{(item or '')[:8]} verdict {verdict}"]
        if c["quote_reasons"]:
            longest = max((str(m.get("content") or "") for m in body.get("messages") or []), key=len, default="")
            pos = min(len(longest) // 3, max(0, len(longest) - 200))
            reasons.append("The transcript says: " + longest[pos:pos + 200])
        content = json.dumps({**answers, "reasons": reasons})
        finish = "stop"
        if kind == "length":
            content = content[: len(content) // 2]
            finish = "length"
        elif kind == "wrong_schema":
            content = json.dumps({"verdict": verdict, "reasons": reasons})
        elif kind == "string_bools":
            content = json.dumps({k: "maybe" for k in ANSWER_KEYS} | {"reasons": reasons})
        elif hb % 7 == 0:
            content = "```json\n" + content + "\n```"
        if kind is None:
            with self.lock:
                self.valid_served[body_sha] += 1
                self.served[(item, body_sha)] = {"answers": answers, "verdict": verdict, "reasons": reasons}
        return 200, {"id": f"chatcmpl-stub-{rec['n']}", "object": "chat.completion", "created": 1760000000,
                     "model": body.get("model"), "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                                              "finish_reason": finish}], "usage": usage}, None

    # -- oracles over what was served
    def expected_item_verdict(self, item):
        """From the valid answers served for this item's distinct bodies (framings): the CONTRACT.md §3 rule."""
        vs = [self.served[(item, b)]["verdict"] for b in self.bodies_by_item.get(item, {}) if (item, b) in self.served]
        if not vs:
            return None, []
        if "veto" in vs:
            return "veto", vs
        return "pass", vs


class _CheckHandler(_Handler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if not self.path.rstrip("/").endswith("/systemone"):
            return self._send(404, {"error": f"unknown path {self.path}"})
        code, obj = self.server.checker.answer(raw, self.headers.get("Authorization"))
        self._send(code, obj)


CHECK_KEY = "ts-p9-stub-checklist-7c1e9a0b3d5f-do-not-leak"
CHECK_P = (0.95, 0.05, 0.5, 0.82, 0.12)  # YES, NO, UNSURE, YES, NO


class Checklist:
    """Stub system-one endpoint: every noul question gets a p(yes) picked by hash of (question, state), so the
    same conversation always gets the same answers. ctl: status int (every call answers that code), fail_first
    int (the first k attempts of each body answer 529)."""

    def __init__(self):
        self.ctl = {"status": None, "fail_first": 0}
        self.lock = threading.Lock()
        self.calls, self.attempts, self.served = [], Counter(), {}
        self.srv = None

    def start(self):
        self.srv = _Server(("127.0.0.1", 0), _CheckHandler)
        self.srv.checker = self
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        return self

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def reset(self, **ctl):
        with self.lock:
            self.calls, self.attempts, self.served = [], Counter(), {}
            self.ctl.update({"status": None, "fail_first": 0}, **ctl)

    @property
    def n_calls(self):
        with self.lock:
            return len(self.calls)

    @staticmethod
    def p_for(question, state):
        h = int(hashlib.sha256((question + json.dumps(state, sort_keys=True)).encode()).hexdigest(), 16)
        return CHECK_P[h % len(CHECK_P)]

    def answer(self, raw, auth):
        body = json.loads(raw)
        body_sha = hashlib.sha256(raw).hexdigest()
        state, qs = body.get("state"), body.get("questions") or {}
        with self.lock:
            self.attempts[body_sha] += 1
            self.calls.append({"auth": auth, "model": body.get("model"), "state": state, "questions": qs,
                               "attempt": self.attempts[body_sha]})
            k = self.attempts[body_sha]
        if self.ctl["status"]:
            return self.ctl["status"], {"error": "stub status"}
        if k <= self.ctl["fail_first"]:
            return 529, {"error": "stub overloaded"}
        answers = {q: {"type": "noul", "noul": self.p_for(q, state)} for q in qs}
        with self.lock:
            self.served[json.dumps(state, sort_keys=True)] = {q: a["noul"] for q, a in answers.items()}
        return 200, {"model": "jev-stub-1.0.0", "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 7}}


# ----------------------------------------------------------------------------- oracles: level 0, final label, strata

def harness_item(items, run):
    for it in items:
        if it["run"] == run and it["principal"].count("/agent/") == 1 and "/sub/" not in it["principal"]:
            return it
    return None


def runs_of(items):
    return sorted({it["run"] for it in items})


def content_strings(item):
    out = []
    for m in item["messages"]:
        if isinstance(m.get("content"), str) and m["content"]:
            out.append(m["content"])
        for t in m.get("tool_calls") or []:
            a = t.get("function", {}).get("arguments")
            if isinstance(a, str) and a:
                out.append(a)
    return out


def level0_oracle(item, item_hash_ok=True):
    o = item["outcome"]
    term = o.get("termination_reason")
    any_ctx = any("context_window_exceeded" in (m.get("content") or "") for m in item["messages"] if isinstance(m.get("content"), str))
    checks = {"item_hash": bool(item_hash_ok),
              "trace_complete": item["trace_complete"]["status"] == "complete",
              "witness_agrees": item["witness"]["agrees"] is True,
              "no_infra_error": term not in ("infrastructure_error", "infra_error") and o.get("source") is not None
              and not any(m.get("role") == "error" for m in item["messages"]),
              "no_context_overflow": not (term and "context_window_exceeded" in str(term)) and not any_ctx,
              "reward_known": isinstance(o.get("reward"), (int, float)) and not isinstance(o.get("reward"), bool)}
    structural = all(checks.values())
    informative = not (item.get("harness") == "tau2" and str(item.get("task_id")) in TAU2_384)
    reasons = [k for k, v in checks.items() if not v] + ([] if informative else ["tau2_384"])
    return {"reward": o.get("reward"), "passed": o.get("passed"), "termination_reason": term,
            "trace_complete": item["trace_complete"]["status"], "witness_agrees": item["witness"]["agrees"],
            "checks": checks, "structural_ok": structural, "reward_informative": informative,
            "status": "clean" if structural and informative else "not_clean", "reasons": reasons}


def judged_oracle(item):
    return item["witness"]["agrees"] is True and any(m.get("train") for m in item["messages"])


def final_oracle(l0, judge_verdict, human_verdict):
    if not l0["structural_ok"]:
        return {"label": "fail", "rule": "level0", "kept": False}
    if human_verdict in ("pass", "fail") and (l0["passed"] is True or not l0["reward_informative"]):
        return {"label": human_verdict, "rule": "human", "kept": human_verdict == "pass"}
    if not l0["reward_informative"]:
        return {"label": "fail", "rule": "tau2_384", "kept": False}
    if l0["passed"] is False:
        return {"label": "fail", "rule": "reward", "kept": False}
    if judge_verdict == "veto":
        return {"label": "fail", "rule": "veto", "kept": False}
    return {"label": "pass", "rule": "reward", "kept": True}


def stratum_oracle(l0, judge_verdict):
    if l0["witness_agrees"] is not True or not l0["checks"]["reward_known"]:
        return None
    if not l0["reward_informative"]:
        return "disagree"
    if judge_verdict in ("pass", "veto"):
        jp = judge_verdict == "pass"
        if jp != bool(l0["passed"]):
            return "disagree"
    return "passed" if l0["passed"] else "failed"


def gold_oracle(strata, n, seed):
    n = max(50, min(100, n))
    rnd = random.Random(seed)
    quota = {"disagree": n - 2 * (n // 3), "passed": n // 3, "failed": n // 3}
    picked = {}
    for s in ("disagree", "passed", "failed"):
        picked[s] = rnd.sample(sorted(strata.get(s, [])), min(quota[s], len(strata.get(s, []))))
    left = n - sum(len(v) for v in picked.values())
    for s in ("disagree", "passed", "failed"):
        rest = sorted(set(strata.get(s, [])) - set(picked[s]))
        k = min(left, len(rest))
        if k:
            picked[s] += rnd.sample(rest, k)
            left -= k
    runs = sorted(r for v in picked.values() for r in v)
    eligible = sum(len(v) for v in strata.values())
    return {"n": n, "picked": picked, "runs": runs, "short": eligible < 50}


def kappa(a, b, c, d):
    n = a + b + c + d
    out = {"n": n, "a": a, "b": b, "c": c, "d": d, "po": None, "pe": None, "value": None, "se": None, "ci95": None,
           "status": "unverified", "reason": None}
    if n == 0:
        out["reason"] = "n=0 < 50"
        return out
    po = (a + d) / n
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    out["po"], out["pe"] = po, pe
    if n < 50:
        out["reason"] = f"n={n} < 50"
    elif pe >= 1:
        out["reason"] = "pe=1"
    else:
        k = (po - pe) / (1 - pe)
        se = math.sqrt(po * (1 - po) / (n * (1 - pe) ** 2))
        out.update({"value": k, "se": se, "ci95": [k - 1.96 * se, k + 1.96 * se], "status": "measured"})
    return out


def kappa_from_rows(rows, gold_runs, rater):
    """rater: 'judge' (level1.verdict pass/veto) or 'reward' (level0.passed, informative only) vs the human."""
    a = b = c = d = 0
    for r in rows:
        if r["run"] not in gold_runs or not r.get("level2"):
            continue
        h = r["level2"]["verdict"] == "pass"
        if rater == "judge":
            v = (r.get("level1") or {}).get("verdict")
            if v not in ("pass", "veto"):
                continue
            p = v == "pass"
        else:
            if not isinstance(r["level0"]["passed"], bool) or not r["level0"]["reward_informative"]:
                continue
            p = r["level0"]["passed"]
        if p and h:
            a += 1
        elif p and not h:
            b += 1
        elif not p and h:
            c += 1
        else:
            d += 1
    return kappa(a, b, c, d)


def kappa_diff(want, got):
    probs = []
    if not isinstance(got, dict):
        return ["missing"]
    for k in ("n", "a", "b", "c", "d", "status"):
        if want.get(k) != got.get(k):
            probs.append(f"{k} {got.get(k)!r} want {want.get(k)!r}")
    for k in ("po", "pe", "value", "se"):
        w, g = want.get(k), got.get(k)
        if (w is None) != (g is None) or (w is not None and abs(w - g) > 1e-9):
            probs.append(f"{k} {g!r} want {w!r}")
    w, g = want.get("ci95"), got.get("ci95")
    if (w is None) != (g is None) or (w and (not isinstance(g, list) or len(g) != 2 or any(abs(x - y) > 1e-9 for x, y in zip(w, g)))):
        probs.append(f"ci95 {g!r} want {w!r}")
    return probs


def tiles(n_rows, m_runs, kept):
    return {"labelled": f"{n_rows} / {m_runs} runs labelled",
            "kept": f"kept {kept} passed runs → training set · dropped {n_rows - kept} failed"}


# ----------------------------------------------------------------------------- oracles: rows, storage, leaks

def content_sha(item):
    msgs = []
    for m in item["messages"]:
        d = {"role": m["role"]}
        if "content" in m:
            d["content"] = m["content"]
        if "tool_call_id" in m:
            d["tool_call_id"] = m["tool_call_id"]
        if m.get("tool_calls"):
            d["tool_calls"] = [{"name": t["function"]["name"], "arguments": t["function"]["arguments"]} for t in m["tool_calls"]]
        msgs.append(d)
    return hashlib.sha256(json.dumps({"task": item["task"], "tools": item["tools"], "messages": msgs}, sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def canon_label_id(row):
    d = {k: v for k, v in row.items() if k != "label_id"}
    return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def root_rows(root):
    """[(file, lineno, row|None, text)] for every line of every .jsonl under the root (row None = not a JSON object)."""
    out = []
    if not os.path.isdir(root):
        return out
    for n in sorted(os.listdir(root)):
        p = os.path.join(root, n)
        if not n.endswith(".jsonl") or not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        row = None
                except ValueError:
                    row = None
                out.append((n, i, row, line))
    return out


def row_problems(row):
    probs = []
    for k in ("label_id", "level", "content_sha256", "item_id", "run", "labeller", "ts", "verdict"):
        if k not in row:
            probs.append(f"missing {k}")
    if probs:
        return probs
    if canon_label_id(row) != row["label_id"]:
        probs.append("label_id is not the canonical hash")
    if row["level"] == 1:
        for k in ("model", "rubric_sha256", "framing", "answers", "reasons", "error", "attempts"):
            if k not in row:
                probs.append(f"level 1 row missing {k}")
        if row.get("framing") not in ("a", "b"):
            probs.append(f"framing {row.get('framing')!r}")
        if row["verdict"] not in ("pass", "veto", "unknown"):
            probs.append(f"verdict {row['verdict']!r}")
        if not str(row["labeller"]).startswith("model:"):
            probs.append(f"labeller {row['labeller']!r}")
        if row["verdict"] != "unknown":
            a = row.get("answers") or {}
            if any(not isinstance(a.get(k), bool) for k in ANSWER_KEYS):
                probs.append("answers not four booleans")
            elif (verdict_of(a) != row["verdict"]):
                probs.append(f"verdict {row['verdict']} does not follow from answers {a}")
    elif row["level"] == 2:
        if row["verdict"] not in ("pass", "fail"):
            probs.append(f"level 2 verdict {row['verdict']!r}")
        if not str(row["labeller"]).startswith("human:"):
            probs.append(f"labeller {row['labeller']!r}")
    else:
        probs.append(f"level {row['level']!r}")
    return probs


def verdict_of(answers):
    return "veto" if (answers.get("policy_followed") is False or answers.get("right_tools") is False
                      or answers.get("hallucinated_facts") is True or answers.get("unsafe_action") is True) else "pass"


def windows(s, k=VERBATIM):
    return {s[i:i + k] for i in range(0, max(0, len(s) - k + 1))}


def verbatim_leaks(text, items, k=VERBATIM):
    """Items whose transcript has a k-character run inside text (CONTRACT.md §4)."""
    if len(text) < k:
        return []
    have = windows(text, k)
    bad = []
    for it in items:
        for s in content_strings(it):
            if len(s) >= k and any(w in have for w in windows(s, k)):
                bad.append(it["run"])
                break
    return bad


def files_under(*dirs):
    out = []
    for d in dirs:
        for a, _, fs in os.walk(d):
            for n in fs:
                out.append(os.path.join(a, n))
    return sorted(out)


def text_of_files(paths):
    parts = []
    for p in paths:
        try:
            parts.append(open(p, "rb").read().decode("utf-8", "replace"))
        except OSError:
            pass
    return "\n".join(parts)


def blind_leaks(view_text, item):
    """Remove the item's own strings from the view, then look for outcome words (CONTRACT.md §7)."""
    rest = view_text
    for s in sorted(content_strings(item), key=len, reverse=True):
        rest = rest.replace(s, " ")
        rest = rest.replace(json.dumps(s, ensure_ascii=False)[1:-1], " ")  # escaped form
    for s in sorted((t.get("function", {}).get("name", "") for m in item["messages"] for t in m.get("tool_calls") or []), key=len, reverse=True):
        if s:
            rest = rest.replace(s, " ")
    for t in item.get("tools") or []:
        rest = rest.replace(t["function"]["name"], " ")
    found = sorted({m.group(0).lower() for m in LEAK.finditer(rest)})
    return found, rest


def view_has_transcript(view_text, item):
    """Every message content and tool-call argument string must appear verbatim (HTML form is unescaped first)."""
    missing = []
    for s in content_strings(item):
        if s not in view_text:
            missing.append(short(s, 50))
    return missing


def strip_html(s):
    return L.strip_html(s)


# ----------------------------------------------------------------------------- synthetic datasets

def manifest_for(items, template, seed=0):
    """A manifest consistent with `items` the way retrain writes one (counts, m14, dataset_sha256)."""
    rows, agents = [], {}
    for it in items:
        n = sum(1 for m in it["messages"] if m.get("train")) if it["witness"]["agrees"] is not False else 0
        rows.append({"item_id": it["item_id"], "run": it["run"], "principal": it["principal"], "split": it["split"],
                     "passed": it["outcome"]["passed"], "trace_complete": it["trace_complete"]["status"],
                     "witness_agrees": it["witness"]["agrees"], "sft_examples": n})
        if it["principal"].count("/agent/") == 1 and "/sub/" not in it["principal"]:
            agents[it["run"]] = it
    train = [it for it in agents.values() if it["split"] == "train"]
    kept = sum(1 for it in train if it["outcome"]["passed"] is True)
    by = lambda f: sum(1 for it in agents.values() if f(it))  # noqa: E731
    return {"format": "avos-retrain-manifest/1", "seed": seed, "redact": None, "split_file": template.get("split_file"),
            "items": rows, "skipped": [],
            "counts": {"runs_requested": len(agents), "runs_exported": len(agents), "items": len(rows),
                       "sft_examples": sum(r["sft_examples"] for r in rows),
                       "passed": by(lambda it: it["outcome"]["passed"] is True), "failed": by(lambda it: it["outcome"]["passed"] is False),
                       "unknown": by(lambda it: it["outcome"]["passed"] is None), "train": len(train),
                       "held_out": by(lambda it: it["split"] == "held_out"), "unsplit": by(lambda it: it["split"] is None),
                       "flagged": sum(1 for r in rows if r["witness_agrees"] is False)},
            "m14": {"train_runs": len(train), "train_passed": kept, "kept_run_fraction": kept / len(train) if train else None},
            "dataset_sha256": hashlib.sha256("\n".join(r["item_id"] for r in rows).encode()).hexdigest()}


def write_dataset(path, items, template):
    os.makedirs(path, exist_ok=True)
    write_jsonl(os.path.join(path, "trajectories.jsonl"), items)
    write_jsonl(os.path.join(path, "sft.jsonl"), [])
    with open(os.path.join(path, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(manifest_for(items, template), indent=1, ensure_ascii=False) + "\n")
    with open(os.path.join(path, "stats.json"), "w") as f:
        f.write("{}\n")
    return path


def clone_item(src, run, vm, task_id=None, passed=None, salt=""):
    it = json.loads(json.dumps(src))
    it["run"], it["vm_id"] = run, vm
    it["principal"] = re.sub(r"vm-\d\d-[0-9a-f]{6}", vm, it["principal"])
    it["lineage"]["events"] = [hashlib.sha256((e + vm).encode()).hexdigest()[:len(e)] for e in it["lineage"]["events"]]
    for m in it["messages"]:
        if m.get("event_id"):
            m["event_id"] = hashlib.sha256((m["event_id"] + vm).encode()).hexdigest()[:len(m["event_id"])]
    if task_id is not None:
        it["task"], it["task_id"] = f"retail/{task_id}", str(task_id)
        it["split"] = L.split_of(str(task_id))
    if passed is not None:
        it["outcome"]["reward"], it["outcome"]["passed"] = (1.0 if passed else 0.0), passed
    if salt:  # a distinct transcript: a harmless extra sentence in the last assistant turn
        for m in reversed(it["messages"]):
            if m["role"] == "assistant" and isinstance(m.get("content"), str) and m["content"]:
                m["content"] += f" (ref {salt})"
                break
    it["item_id"] = L.canon_item_id(it)
    return it


def synthetic_items(base_items, n, seed=7, pass_share=0.5):
    """n τ² items cloned round-robin from the clean store items, distinct transcripts and run ids, mixed rewards."""
    src = [it for it in base_items if it["harness"] == "tau2" and it["witness"]["agrees"] is True
           and it["trace_complete"]["status"] == "complete" and it["outcome"]["reward"] is not None]
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        s = src[i % len(src)]
        task = str(rnd.choice(["0", "1", "2", "3", "7", "8", "15", "37", "52"]))
        run = f"run_retail-{task}_{10000 + i}"
        out.append(clone_item(s, run, f"vm-{(i % 90) + 10:02d}-{rnd.getrandbits(24):06x}", task_id=task,
                              passed=rnd.random() < pass_share, salt=f"{seed}-{i}"))
    return out


def dummy_key_in(text):
    return DUMMY_KEY in text




def percentile(xs, q):
    return L.percentile(xs, q)
