#!/usr/bin/env python3
"""POC 13 acceptance suite: black-box checks of eval/CONTRACT.md. Written before and without the implementation.

  python3 eval/tests/run.py --selftest     the suite's own oracles, fakes and PKI (~20 s)
  python3 eval/tests/run.py --quick        every local check: gateway on loopback with fake upstreams, fleet matrix,
                                           m15, train select, label (~3 min once implemented)
  python3 eval/tests/run.py --live         + one real tool-calling exchange with Anthropic through the gateway path
                                           (needs ANTHROPIC_API_KEY in the environment; < $0.05)
  python3 eval/tests/run.py --host         + the fleet host over ssh (avos-dev): one real closed-policy trial (~$1)
  python3 eval/tests/run.py --only G       only checks whose id starts with G

One line per check: PASS|FAIL|SKIP <id> [<target>] <measured vs bar>. Exit 0 only if every executed check passed.
A missing CLI reads `FAIL … not implemented: <path>`, never a traceback.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lib  # noqa: E402

CHECKS = []
HOST = "avos-dev"
HOST_REPO = "~/poc"
HOST_TMP = "/tmp/p13-tests"
HOST_MEM = 1024
LIVE_MAX_TOKENS = 1024


def check(cid, target, live=False, host=False):
    def deco(fn):
        CHECKS.append((cid, target, live, host, fn))
        return fn
    return deco


class Ctx:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="p13-tests-")
        self.transcripts = []
        self.cache = {}
        self.ssh_ctl = self.ssh_dir = None
        self.gateways = []
        self.extra_fakes = []

    def fake(self, *script, key=None):
        """A dedicated fake upstream (closed in close()) answering every GET/POST from `script`; with `key`, 401 unless
        `Authorization: Bearer <key>`, exactly as the real vLLM policy upstreams answer."""
        f = lib.FakeUpstream()
        f.set(*script)
        f.require = ("Authorization", f"Bearer {key}") if key else None
        self.extra_fakes.append(f)
        return f

    def scratch(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    # --- local CLIs
    def cli(self, path, *args, env=None, expect_rc=None, timeout=600):
        res = lib.cli(path, *args, env=env, timeout=timeout)
        self.transcripts.append((res["out"] + res["err"], " ".join(map(str, args[:3]))))
        if not lib.no_traceback(res):
            last = [l for l in (res["err"] + res["out"]).strip().splitlines() if l.strip()][-1:]
            raise AssertionError(f"traceback ({os.path.basename(path)} {' '.join(map(str, args[:2]))}): {lib.short(last[0] if last else '', 200)}")
        if expect_rc is not None and res["rc"] not in (expect_rc if isinstance(expect_rc, tuple) else (expect_rc,)):
            raise AssertionError(f"{os.path.basename(path)} {' '.join(map(str, args[:4]))}: rc={res['rc']} (want {expect_rc}) "
                                 f"stderr={lib.short(res['err'], 300)!r} stdout={lib.short(res['out'], 200)!r}")
        return res

    def fleet(self, *args, **kw):
        return self.cli(lib.FLEET, *args, **kw)

    def fleet_json(self, *args, expect_rc=0, **kw):
        res = self.fleet(*args, "--json", expect_rc=expect_rc, **kw)
        try:
            res["json"] = json.loads(res["out"])
        except ValueError:
            raise AssertionError(f"--json stdout is not one JSON object: {lib.short(res['out'], 200)!r}")
        return res

    def refusal(self, res, *needles):
        """A refusal names its reason (stdout JSON `error` or stderr) and every needle given."""
        blob = res["out"] + res["err"]
        missing = [n for n in needles if n not in blob]
        if missing:
            raise AssertionError(f"refusal does not name {missing}: rc={res['rc']} stdout={lib.short(res['out'], 200)!r} stderr={lib.short(res['err'], 200)!r}")

    # --- the gateway on loopback
    def certs(self):
        if "certs" not in self.cache:
            self.cache["certs"] = lib.make_certs(self.scratch("certs"))
        return self.cache["certs"]

    def fakes(self):
        if "fakes" not in self.cache:
            self.cache["fakes"] = (lib.FakeUpstream(), lib.FakeUpstream())
        return self.cache["fakes"]

    def keys(self):
        return {"ANTHROPIC_API_KEY": "sk-ant-api03-P13TESTSECRET-0123456789abcdefABCDEF", "OPENAI_API_KEY": "sk-oai-P13TESTSECRET-fedcba9876543210",
                "POLICY_API_KEY": "sk-pol-P13TESTSECRET-a1b2c3d4e5f60718"}

    def gw(self):
        """One gateway for every G check: a closed route to the fake Messages endpoint, an open route to the fake
        OpenAI endpoint, a route with several upstreams, and no route for `nowhere/model`."""
        if "gw" not in self.cache:
            anth, oai = self.fakes()
            anth.require = ("x-api-key", self.keys()["ANTHROPIC_API_KEY"])      # the real Messages API answers 401 without it
            routes = {lib.CLOSED: lib.anthropic_route(anth.url),
                      lib.OPEN_ROLES["policy"]: {"base_url": oai.url + "/v1", "key": "OPENAI_API_KEY"},
                      lib.OPEN_ROLES["simulator"]: oai.url + "/v1"}
            g = lib.Gateway(self.tmp, "main", routes, self.keys(), self.certs())
            self.gateways.append(g)
            self.cache["gw"] = g
        return self.cache["gw"]

    def anth(self, body, *responses, attempt=1):
        """One τ² request through the gateway to the fake Messages upstream. Returns (status, headers, reply, got, row)
        where `got` is what the fake received (headers + parsed body) and `row` the llm.call row written."""
        anth, _ = self.fakes()
        g = self.gw()
        before = len(g.rows())
        anth.set(*responses)
        status, hdrs, out = g.call(body, attempt=attempt)
        try:
            reply = json.loads(out)
        except ValueError:
            reply = out
        rows = g.rows()
        if len(rows) != before + 1:
            raise AssertionError(f"{len(rows) - before} llm.call rows written for one request (want 1)")
        got = None
        if anth.requests:
            r = anth.requests[-1]
            try:
                got = {**r, "json": json.loads(r["body"])}
            except ValueError:
                got = {**r, "json": None}
        return status, hdrs, reply, got, rows[-1], len(anth.requests)

    # --- ssh (host checks)
    def ssh_base(self):
        if self.ssh_ctl is None:
            self.ssh_dir = tempfile.mkdtemp(prefix="p13-ssh-", dir="/tmp")
            self.ssh_ctl = os.path.join(self.ssh_dir, "cm-%C")
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self.ssh_ctl}", "-o", "ControlPersist=300", HOST]

    def ssh(self, cmd, timeout=900, stdin=None):
        p = subprocess.run(self.ssh_base() + [cmd], capture_output=True, text=True, timeout=timeout, input=stdin)
        self.transcripts.append((p.stdout + p.stderr, "ssh"))
        return p

    def ssh_close(self):
        if self.ssh_ctl:
            subprocess.run(["ssh", "-o", f"ControlPath={self.ssh_ctl}", "-O", "exit", HOST], capture_output=True)
            self.ssh_ctl = None
            shutil.rmtree(self.ssh_dir, ignore_errors=True)

    def host_cleanup(self):
        self.ssh_close()
        subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=no", "-o", "ControlPath=none", HOST,
                        f"sudo pkill -TERM -f 'fleet run --out {HOST_TMP}' 2>/dev/null; sleep 3; sudo rm -rf {HOST_TMP}"],
                       capture_output=True, timeout=120)

    def close(self):
        for g in self.gateways:
            g.stop()
        for f in (*self.cache.get("fakes", ()), *self.extra_fakes):
            f.close()


# ====================================================================== G: the closed model behind the gateway (done test 1)

def _blocks(content):
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])


def _text_of(content):
    return content if isinstance(content, str) else "".join(b.get("text", "") for b in content if b.get("type") == "text")


@check("G1_request_shape", "done test 1: pinned effort, max_tokens, caching; refused fields dropped; tools and system translated")
def g1(ctx):
    body = lib.tau2_request([{"role": "system", "content": lib.SYSTEM}, {"role": "user", "content": "Hi, I want to check order #W1."}],
                            lib.TOOLS, top_p=1.0, seed=300, tool_choice="auto", max_tokens=50)
    status, _, reply, got, row, n_up = ctx.anth(body, (200, {}, lib.msg([lib.text("Sure, one moment.")])))
    if got is None:
        raise AssertionError(f"nothing reached the Messages upstream (gateway answered {status}: {lib.short(reply, 200)})")
    if n_up != 1:
        raise AssertionError(f"{n_up} upstream requests for one gateway request")
    if not got["path"].endswith("/v1/messages"):
        raise AssertionError(f"upstream path {got['path']!r} is not …/v1/messages")
    h = got["headers"]
    key = ctx.keys()["ANTHROPIC_API_KEY"]
    if h.get("x-api-key") != key:
        raise AssertionError(f"x-api-key header {lib.short(h.get('x-api-key'), 20)!r} is not the route's key")
    if h.get("anthropic-version") != lib.ANTHROPIC_VERSION:
        raise AssertionError(f"anthropic-version header {h.get('anthropic-version')!r}")
    j = got["json"]
    if j is None:
        raise AssertionError("upstream body is not JSON")
    want = {"model": lib.CLOSED, "max_tokens": lib.PINNED["max_tokens"]}
    bad = lib.diff(j, want)
    if (j.get("output_config") or {}).get("effort") != lib.PINNED["effort"]:
        bad.append(f"output_config.effort {j.get('output_config')!r} != {lib.PINNED['effort']!r}")
    if j.get("cache_control") != {"type": "ephemeral"}:
        bad.append(f"top-level cache_control {j.get('cache_control')!r} != ephemeral")
    for f in ("temperature", "top_p", "seed"):
        if f in j:
            bad.append(f"{f} forwarded ({j[f]!r}); the closed model refuses it")
    tc = j.get("tool_choice")
    if tc is not None and (not isinstance(tc, dict) or tc.get("type") not in ("auto", "none")):
        bad.append(f"forced tool_choice forwarded: {tc!r}")
    if j.get("stream") is True:
        bad.append("stream: true was sent; the gateway reads whole bodies")
    if _text_of(j.get("system", "")) != lib.SYSTEM:
        bad.append(f"system {lib.short(j.get('system'), 80)!r} is not the system message")
    msgs = j.get("messages") or []
    if len(msgs) != 1 or msgs[0].get("role") != "user" or _text_of(msgs[0].get("content")) != "Hi, I want to check order #W1.":
        bad.append(f"messages {lib.short(msgs, 160)!r}: want one user message with the user's text, no system message")
    tools = j.get("tools") or []
    if [t.get("name") for t in tools] != [t["function"]["name"] for t in lib.TOOLS]:
        bad.append(f"tools {[t.get('name') for t in tools]} != {[t['function']['name'] for t in lib.TOOLS]}")
    else:
        for t, w in zip(tools, lib.TOOLS):
            if t.get("input_schema") != w["function"]["parameters"] or t.get("description") != w["function"]["description"]:
                bad.append(f"tool {w['function']['name']}: input_schema/description not the OpenAI schema's")
    if bad:
        raise AssertionError("; ".join(bad))
    rb = lib.diff(row, {"model": lib.CLOSED, "route_kind": "anthropic", "effort": "medium", "max_tokens": 16000, "cache": True,
                        "status": 200, "upstream_status": 200, "error": None})
    dropped = set(row.get("dropped_fields") or [])
    for f in ("temperature", "top_p", "seed", "max_tokens"):
        if f not in dropped:
            rb.append(f"dropped_fields {sorted(dropped)} lacks {f}")
    if status != 200 or not isinstance(reply, dict) or reply.get("object") != "chat.completion":
        rb.append(f"client got {status} {lib.short(reply, 120)!r}, not a chat.completion")
    if rb:
        raise AssertionError("row/reply: " + "; ".join(rb))
    return "x-api-key + anthropic-version; max_tokens 16000, effort medium, cache_control ephemeral; temperature/top_p/seed/max_tokens dropped and listed; tools+system translated; 1 upstream request"


@check("G2_messages_translation", "done test 1: tool calls, merged tool results, tool_result before text, no empty text blocks")
def g2(ctx):
    tc = lambda i, name, args: {"id": i, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    msgs = [{"role": "system", "content": lib.SYSTEM}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [tc("toolu_01A", "get_order_details", {"order_id": "#W1"}),
                                                                tc("toolu_01B", "transfer_to_human_agents", {"summary": "x"})]},
            {"role": "tool", "tool_call_id": "toolu_01A", "content": json.dumps({"status": "delivered"})},
            {"role": "tool", "tool_call_id": "toolu_01B", "content": "ok"},
            {"role": "user", "content": "thanks, one more thing"},
            {"role": "assistant", "content": None, "tool_calls": [tc("toolu_01C", "get_order_details", {"order_id": "#W2"})]},
            {"role": "tool", "tool_call_id": "toolu_01C", "content": "{}"}]
    status, _, reply, got, row, _ = ctx.anth(lib.tau2_request(msgs, lib.TOOLS), (200, {}, lib.msg([lib.text("done")])))
    if got is None or got["json"] is None:
        raise AssertionError(f"nothing reached the Messages upstream (gateway answered {status}: {lib.short(reply, 200)})")
    am = got["json"].get("messages") or []
    roles = [m.get("role") for m in am]
    bad = []
    if roles != ["user", "assistant", "user", "assistant", "user"]:
        bad.append(f"roles {roles} != user/assistant alternation of 5 (consecutive tool results merged into one user turn)")
    for m in am:
        for b in _blocks(m.get("content")):
            if b.get("type") == "text" and b.get("text", "") == "":
                bad.append(f"empty text block in a {m.get('role')} turn")
    if len(am) == 5:
        a1 = [b for b in _blocks(am[1]["content"]) if b.get("type") != "thinking"]
        if [(b.get("type"), b.get("id"), b.get("name"), b.get("input")) for b in a1] != \
                [("tool_use", "toolu_01A", "get_order_details", {"order_id": "#W1"}), ("tool_use", "toolu_01B", "transfer_to_human_agents", {"summary": "x"})]:
            bad.append(f"assistant turn 1 blocks {lib.short(a1, 200)}: want two tool_use blocks with the ids, names and parsed arguments")
        u2 = _blocks(am[2]["content"])
        kinds = [b.get("type") for b in u2]
        if kinds != ["tool_result", "tool_result", "text"]:
            bad.append(f"user turn 2 block types {kinds} != [tool_result, tool_result, text] (results first, in order, then the user's text)")
        else:
            if [b.get("tool_use_id") for b in u2[:2]] != ["toolu_01A", "toolu_01B"]:
                bad.append(f"tool_result ids {[b.get('tool_use_id') for b in u2[:2]]}")
            if _text_of(u2[0].get("content")) != json.dumps({"status": "delivered"}) or _text_of(u2[1].get("content")) != "ok":
                bad.append("tool_result contents are not the tool messages' content")
            if u2[2].get("text") != "thanks, one more thing":
                bad.append("the user's text after the tool results is not kept")
        a3 = [b for b in _blocks(am[3]["content"]) if b.get("type") != "thinking"]
        if len(a3) != 1 or a3[0].get("type") != "tool_use" or a3[0].get("id") != "toolu_01C":
            bad.append(f"assistant turn 3 blocks {lib.short(a3, 120)}: want exactly one tool_use (content null → no text block)")
        u4 = _blocks(am[4]["content"])
        if len(u4) != 1 or u4[0].get("type") != "tool_result" or u4[0].get("tool_use_id") != "toolu_01C":
            bad.append(f"user turn 4 {lib.short(u4, 120)}: want one tool_result for toolu_01C")
    if bad:
        raise AssertionError("; ".join(bad))
    if status != 200 or row.get("status") != 200:
        raise AssertionError(f"client {status}, row status {row.get('status')}")
    return "5 alternating turns; 2 tool_use from one assistant message; 2 tool_result merged before the user's text; no empty text block"


REPLY_CASES = [
    ("text", [lib.text("Hello there")], "end_turn", "Hello there", 0, "stop"),
    ("tool_use", [lib.tool_use("toolu_1", "get_order_details", {"order_id": "#W1"})], "tool_use", None, 1, "tool_calls"),
    ("parallel", [lib.tool_use("toolu_1", "get_order_details", {"order_id": "#W1"}), lib.tool_use("toolu_2", "get_order_details", {"order_id": "#W2"}),
                  lib.tool_use("toolu_3", "transfer_to_human_agents", {"summary": "s"})], "tool_use", None, 3, "tool_calls"),
    ("text+tool", [lib.text("Let me check."), lib.tool_use("toolu_1", "get_order_details", {"order_id": "#W1"})], "tool_use", "Let me check.", 1, "tool_calls"),
    ("empty", [], "end_turn", "", 0, "stop"),
    ("thinking", [lib.thinking("secret reasoning about the order"), lib.text("Answer")], "end_turn", "Answer", 0, "stop"),
    ("max_tokens", [lib.text("truncated mid")], "max_tokens", "truncated mid", 0, "length"),
    ("refusal", [], "refusal", "", 0, "content_filter"),
]


@check("G3_reply_translation", "done test 1: text / tool_use / parallel / text+tool / empty / thinking / stop reasons survive as a chat completion")
def g3(ctx):
    base = lib.tau2_request([{"role": "system", "content": lib.SYSTEM}, {"role": "user", "content": "check #W1"}], lib.TOOLS)
    done = []
    for name, content, stop, want_text, n_tools, finish in REPLY_CASES:
        up = lib.msg(content, stop, mid=f"msg_{name}")
        status, _, reply, got, row, n_up = ctx.anth(base, (200, {}, up))
        if got is None:
            raise AssertionError(f"[{name}] nothing reached the upstream; gateway {status} {lib.short(reply, 120)}")
        if status != 200 or not isinstance(reply, dict):
            raise AssertionError(f"[{name}] client got {status}: {lib.short(reply, 200)}")
        bad = lib.diff(reply, {"id": f"msg_{name}", "object": "chat.completion"})
        ch = (reply.get("choices") or [{}])[0]
        m = ch.get("message") or {}
        if m.get("role") != "assistant":
            bad.append(f"message.role {m.get('role')!r}")
        if m.get("content") != want_text:
            bad.append(f"content {m.get('content')!r} != {want_text!r}")
        tcs = m.get("tool_calls") or []
        if len(tcs) != n_tools:
            bad.append(f"{len(tcs)} tool_calls != {n_tools}")
        else:
            for t, b in zip(tcs, [b for b in content if b["type"] == "tool_use"]):
                fn = t.get("function") or {}
                if t.get("id") != b["id"] or t.get("type") != "function" or fn.get("name") != b["name"]:
                    bad.append(f"tool_call {lib.short(t, 100)} != upstream block {b['id']}/{b['name']}")
                if not isinstance(fn.get("arguments"), str):
                    bad.append(f"tool_call {b['id']} arguments is {type(fn.get('arguments')).__name__}, not a JSON string")
                elif json.loads(fn["arguments"]) != b["input"]:
                    bad.append(f"tool_call {b['id']} arguments {fn['arguments']!r} do not parse to {b['input']!r}")
        if ch.get("finish_reason") != finish:
            bad.append(f"finish_reason {ch.get('finish_reason')!r} != {finish!r}")
        if name == "thinking" and "secret reasoning" in (m.get("content") or ""):
            bad.append("thinking text leaked into content")
        u = reply.get("usage") or {}
        if (u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens")) != (650, 45, 695):
            bad.append(f"usage {u} != prompt 650 (120+30+500), completion 45, total 695")
        rb = lib.diff(row, {"status": 200, "upstream_status": 200, "error": None, "finish_reason": finish, "stop_reason": stop,
                            "response_id": f"msg_{name}", "prompt_tokens": 650, "completion_tokens": 45,
                            "usage": {"input_tokens": 120, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 500,
                                      "output_tokens": 45, "thinking_tokens": 20}})
        if n_up != 1:
            rb.append(f"{n_up} upstream requests")
        if bad or rb:
            raise AssertionError(f"[{name}] reply: {'; '.join(bad) or 'ok'} | row: {'; '.join(rb) or 'ok'}")
        done.append(name)
    return f"{len(done)} cases: {', '.join(done)}; finish_reason stop/tool_calls/length/content_filter; arguments as JSON strings"


@check("G4_errors_passthrough", "done test 1: 429 (±retry-after), 500 and a malformed body recorded and passed back; never retried")
def g4(ctx):
    base = lib.tau2_request([{"role": "user", "content": "hi"}])
    err429 = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    err500 = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
    cases = [("429+retry-after", (429, {"retry-after": "7"}, err429), 429, err429),
             ("429", (429, {}, err429), 429, err429),
             ("500", (500, {}, err500), 500, err500),
             ("malformed", (200, {"Content-Type": "text/html"}, b"<html>oops</html>"), None, None)]
    notes = []
    for name, scripted, want_status, want_body in cases:
        status, hdrs, reply, got, row, n_up = ctx.anth(base, scripted)
        if got is None:
            raise AssertionError(f"[{name}] nothing reached the upstream; gateway {status} {lib.short(reply, 120)}")
        if n_up != 1:
            raise AssertionError(f"[{name}] {n_up} upstream requests for one gateway request: the gateway retried")
        bad = []
        if want_status is not None:
            if status != want_status:
                bad.append(f"client status {status} != {want_status}")
            if reply != want_body:
                bad.append(f"client body {lib.short(reply, 120)!r} is not the upstream's error body")
            if name == "429+retry-after" and hdrs.get("retry-after") != "7":
                bad.append(f"retry-after header not passed back ({hdrs.get('retry-after')!r})")
            bad += lib.diff(row, {"status": want_status, "upstream_status": want_status, "error": None, "finish_reason": None,
                                  "prompt_tokens": None, "completion_tokens": None, "stop_reason": None})
            u = row.get("usage")
            if u not in (None,) and not (isinstance(u, dict) and all(u.get(k) is None for k in lib.USAGE_KEYS)):
                bad.append(f"usage on a {want_status} row is not null: {u}")
        else:
            if not (500 <= status < 600):
                bad.append(f"malformed upstream body answered to the client with {status}, want 5xx")
            if not isinstance(reply, dict) or "upstream_bad_body" not in json.dumps(reply):
                bad.append(f"client body {lib.short(reply, 120)!r} does not name upstream_bad_body")
            bad += lib.diff(row, {"upstream_status": 200, "error": "upstream_bad_body", "finish_reason": None, "prompt_tokens": None})
            if row.get("status") != status:
                bad.append(f"row status {row.get('status')} != what the client got ({status})")
        if bad:
            raise AssertionError(f"[{name}] " + "; ".join(bad))
        notes.append(name)
    return f"{', '.join(notes)}: status + body passed back as they are, rows carry upstream_status/error, 1 upstream request each"


@check("G5_row_fields_and_hashes", "done test 1: usage ×5, prompt_tokens = input+cache write+cache read, seen() hashes on the OpenAI shapes, authz_ref join, key never logged")
def g5(ctx):
    g = ctx.gw()
    key = ctx.keys()["ANTHROPIC_API_KEY"]
    msgs1 = [{"role": "system", "content": lib.SYSTEM}, {"role": "user", "content": "Where is order #W1?"}]
    up1 = lib.msg([lib.tool_use("toolu_X1", "get_order_details", {"order_id": "#W1"})], "tool_use", mid="msg_step1")
    status, _, reply1, got, row1, _ = ctx.anth(lib.tau2_request(msgs1, lib.TOOLS), (200, {}, up1), attempt=7)
    if got is None or status != 200 or not isinstance(reply1, dict) or not reply1.get("choices"):
        raise AssertionError(f"step 1: gateway {status}, not a chat completion: {lib.short(reply1, 160)}")
    m1 = reply1["choices"][0]["message"]
    msgs2 = msgs1 + [m1, {"role": "tool", "tool_call_id": "toolu_X1", "content": json.dumps({"status": "delivered"})}]
    usage2 = {"input_tokens": 1000, "cache_creation_input_tokens": 250, "cache_read_input_tokens": 4000, "output_tokens": 80,
              "output_tokens_details": {"thinking_tokens": 30}}
    up2 = lib.msg([lib.text("It was delivered.")], "end_turn", usage=usage2, mid="msg_step2")
    sent2 = lib.tau2_request(msgs2, lib.TOOLS)
    status, _, reply2, got2, row2, _ = ctx.anth(sent2, (200, {}, up2), attempt=8)
    if got2 is None or status != 200 or not isinstance(reply2, dict) or not reply2.get("choices"):
        raise AssertionError(f"step 2: gateway {status}, not a chat completion: {lib.short(reply2, 160)}")
    bad = lib.diff(row2, {"usage": {"input_tokens": 1000, "cache_creation_input_tokens": 250, "cache_read_input_tokens": 4000,
                                    "output_tokens": 80, "thinking_tokens": 30},
                          "prompt_tokens": 5250, "completion_tokens": 80, "effort": "medium", "max_tokens": 16000, "cache": True,
                          "route_kind": "anthropic", "upstream_status": 200, "status": 200, "error": None, "response_id": "msg_step2",
                          "finish_reason": "stop", "attempt": 8, "model": lib.CLOSED})
    if "temperature" not in (row2.get("dropped_fields") or []):
        bad.append(f"dropped_fields {row2.get('dropped_fields')} lacks temperature")
    if (reply2.get("usage") or {}).get("prompt_tokens") != 5250 or (reply2.get("usage") or {}).get("completion_tokens") != 80:
        bad.append(f"client usage {reply2.get('usage')} != prompt 5250 / completion 80")
    bad += lib.diff(row2, lib.expect_hashes(sent2, reply2["choices"][0]["message"]))
    bad += lib.diff(row1, lib.expect_hashes(lib.tau2_request(msgs1, lib.TOOLS), m1))
    decisions = [r for r in g.rows("authz.decision") if r.get("resource") == f'Model::"{lib.CLOSED}"' and r.get("decision") == "allow"]
    if not decisions or row2.get("authz_ref") != decisions[-1]["event_id"]:
        bad.append(f"authz_ref {row2.get('authz_ref')} is not the allow decision that preceded the call")
    if not (isinstance(row2.get("upstream"), str) and str(ctx.fakes()[0].port) in row2["upstream"]):
        bad.append(f"upstream {row2.get('upstream')!r} is not the route's base_url")
    # a reply without cache fields or thinking detail: the absent fields are null, prompt_tokens = input alone
    up3 = lib.msg([lib.text("x")], "end_turn", usage={"input_tokens": 700, "output_tokens": 9}, mid="msg_step3")
    status, _, reply3, got3, row3, _ = ctx.anth(lib.tau2_request(msgs1), (200, {}, up3))
    bad += lib.diff(row3, {"prompt_tokens": 700, "completion_tokens": 9,
                           "usage": {"input_tokens": 700, "cache_creation_input_tokens": None, "cache_read_input_tokens": None,
                                     "output_tokens": 9, "thinking_tokens": None}})
    blob = g.everything_written()
    if key.encode() in blob or ctx.keys()["OPENAI_API_KEY"].encode() in blob:
        bad.append("an upstream key appears in the store or the gateway's output")
    if key.encode() in json.dumps(reply2).encode():
        bad.append("the key appears in a reply")
    if bad:
        raise AssertionError("; ".join(bad))
    return "usage×5 + thinking; prompt 1000+250+4000=5250; completion 80; hashes = content_sha(chat_form) of the OpenAI shapes; authz_ref joins; absent cache fields → null; no key in store/log"


@check("G6_open_route_unchanged", "done test 1: an open-model route forwards and answers byte-identically, row shape as today")
def g6(ctx):
    _, oai = ctx.fakes()
    g = ctx.gw()
    key = ctx.keys()["OPENAI_API_KEY"]
    sent = (b'{"model":"policy-qwen3.5-9b","messages":[{"role":"system","content":"S"},{"role":"user","content":"ping"}],'
            b'"temperature":0.0,   "seed":300, "max_tokens": 7}')
    canned = (b'{"id":"chatcmpl-p13","object":"chat.completion","created":1,"model":"policy-qwen3.5-9b",\n'
              b'"choices":[{"index":0,"message":{"role":"assistant","content":"pong"},"finish_reason":"length"}],'
              b'"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}')
    oai.set((200, {"Content-Type": "application/json"}, canned))
    before = len(g.rows())
    status, hdrs, out = g.call(sent, attempt=3)
    if not oai.requests:
        raise AssertionError(f"nothing reached the OpenAI-compatible upstream (gateway {status})")
    r = oai.requests[-1]
    bad = []
    if r["body"] != sent:
        bad.append("request body bytes changed on the open route")
    if r["path"] != "/v1/chat/completions":
        bad.append(f"path {r['path']!r}")
    if r["headers"].get("authorization") != f"Bearer {key}":
        bad.append("Authorization: Bearer <key> missing on the open route")
    if "x-api-key" in r["headers"] or "anthropic-version" in r["headers"]:
        bad.append("Anthropic headers sent on an open route")
    if status != 200 or out != canned:
        bad.append(f"reply changed on the open route (status {status})")
    rows = g.rows()
    if len(rows) != before + 1:
        bad.append(f"{len(rows) - before} rows for one call")
    row = rows[-1]
    extra, missing = set(row) - lib.TODAY_ROW_KEYS, lib.TODAY_ROW_KEYS - set(row)
    if extra or missing:
        bad.append(f"open-route row keys: extra {sorted(extra)}, missing {sorted(missing)}")
    bad += lib.diff(row, {"model": "policy-qwen3.5-9b", "status": 200, "upstream_status": 200, "error": None, "finish_reason": "length",
                          "prompt_tokens": 11, "completion_tokens": 7, "response_id": "chatcmpl-p13", "attempt": 3, "dropped": False})
    bad += lib.diff(row, lib.expect_hashes(json.loads(sent), json.loads(canned)["choices"][0]["message"]))
    if key.encode() in g.everything_written():
        bad.append("the OpenAI key appears in the store or log")
    if bad:
        raise AssertionError("; ".join(bad))
    return "body, path, Bearer header, reply bytes and row keys exactly as today (27 keys); hashes equal"


@check("G7_route_table", "CONTRACT §1.1: gateway.py route shows the kind and pinned settings; a bad anthropic entry is refused")
def g7(ctx):
    table = os.path.join(ctx.scratch("g7"), "routes.json")
    lib.write_json(table, {lib.CLOSED: lib.anthropic_route("https://api.anthropic.com"), "open/m": "http://10.0.0.9:8000/v1",
                           "bad-closed": {"base_url": "https://api.anthropic.com", "kind": "anthropic"}})

    def route(model, slot="vm-03"):
        res = lib.run([sys.executable, lib.GW_LOCAL, "route", "--table", table, model, slot], env={"AVOS_SUITE_ROOT": lib.ROOT})
        ctx.transcripts.append((res["out"] + res["err"], f"route {model}"))
        if not lib.no_traceback(res):
            raise AssertionError(f"traceback from gateway.py route {model}: {lib.short(res['err'].strip().splitlines()[-1], 160)}")
        try:
            j = json.loads(res["out"].strip().splitlines()[-1]) if res["out"].strip() else {}
        except ValueError:
            raise AssertionError(f"route {model} printed no JSON: {lib.short(res['out'], 120)!r}")
        return res["rc"], j

    rc, j = route(lib.CLOSED)
    bad = [] if rc == 0 else [f"closed route rc {rc}"]
    bad += lib.diff(j, {"model": lib.CLOSED, "upstream": "https://api.anthropic.com", "kind": "anthropic", "effort": "medium",
                        "max_tokens": 16000, "cache": True})
    if "ANTHROPIC_API_KEY" in json.dumps(j) or "key" in j and j["key"] not in (None, True, False):
        bad.append("route output names the key or its variable")
    rc, j = route("open/m")
    bad += ([] if rc == 0 else [f"open route rc {rc}"]) + lib.diff(j, {"upstream": "http://10.0.0.9:8000/v1", "kind": "openai"})
    rc, j = route("nowhere/model")
    if rc != 1 or j.get("error") != "no-route":
        bad.append(f"unrouted model: rc {rc} {j}")
    rc, j = route("bad-closed")
    if rc == 0 or j.get("error") != "bad-route":
        bad.append(f"anthropic entry without key/pins: rc {rc} {j} (want exit 1, error bad-route)")
    if bad:
        raise AssertionError("; ".join(bad))
    return "closed: kind anthropic + effort/max_tokens/cache, no key; open: kind openai; no-route and bad-route exit 1"


@check("G8_assistant_first_history", "CONTRACT §1.2 (ruling 2): τ²'s real history opens with the agent's greeting; forwarded as is, no synthetic turn")
def g8(ctx):
    tc = {"id": "toolu_01G", "type": "function", "function": {"name": "get_order_details", "arguments": json.dumps({"order_id": "#W7"})}}
    greeting = "Hi! How can I help you today?"
    msgs = [{"role": "system", "content": lib.SYSTEM}, {"role": "assistant", "content": greeting},
            {"role": "user", "content": "I want to return order #W7."},
            {"role": "assistant", "content": None, "tool_calls": [tc]},
            {"role": "tool", "tool_call_id": "toolu_01G", "content": json.dumps({"status": "delivered"})}]
    sent = lib.tau2_request(msgs, lib.TOOLS)
    status, _, reply, got, row, n_up = ctx.anth(sent, (200, {}, lib.msg([lib.text("Sure.")], mid="msg_g8")))
    if got is None or got["json"] is None:
        raise AssertionError(f"nothing reached the Messages upstream (gateway answered {status}: {lib.short(reply, 200)})")
    am = got["json"].get("messages") or []
    bad = []
    roles = [m.get("role") for m in am]
    if roles != ["assistant", "user", "assistant", "user"]:
        bad.append(f"roles {roles} != [assistant, user, assistant, user] (the greeting forwarded first, nothing added or dropped)")
    else:
        b0 = [b for b in _blocks(am[0].get("content")) if b.get("type") != "thinking"]
        if [(b.get("type"), b.get("text")) for b in b0] != [("text", greeting)]:
            bad.append(f"first turn {lib.short(b0, 120)} is not the greeting as one text block")
        if _text_of(am[1].get("content")) != "I want to return order #W7.":
            bad.append("the user's first message is not the second turn, unchanged")
        u3 = _blocks(am[3].get("content"))
        if [b.get("type") for b in u3] != ["tool_result"] or u3[0].get("tool_use_id") != "toolu_01G":
            bad.append(f"last turn {lib.short(u3, 120)} is not the one tool_result")
    for m in am:
        for b in _blocks(m.get("content")):
            if b.get("type") == "text" and not b.get("text"):
                bad.append(f"empty text block in a {m.get('role')} turn")
    if n_up != 1 or status != 200 or row.get("status") != 200:
        bad.append(f"{n_up} upstream requests, client {status}, row {row.get('status')}")
    bad += lib.diff(row, lib.expect_hashes(sent, (reply.get("choices") or [{}])[0].get("message") if isinstance(reply, dict) else None))
    if bad:
        raise AssertionError("; ".join(bad))
    return "system + greeting-first τ² history → [assistant, user, assistant, user], greeting kept as the first turn, no synthetic user turn; hashes on the OpenAI shape"


# ====================================================================== F: fleet.json and fleet matrix (done tests 2, 3)

def _fake_fleet_env(ctx, name, routes_table=None, env_vars="default"):
    """fleet/tests' fake job plus a route table at $AVOS_GATEWAY_ROUTES and a root-only key file at $AVOS_GATEWAY_ENV."""
    L = lib.fleet_lib()
    results = ctx.scratch(f"{name}_results")
    job = L.install_fake_job(ctx.scratch(f"{name}_job"))
    plan_path = os.path.join(ctx.tmp, f"{name}_plan.json")
    lib.write_json(plan_path, {"default": "done_pass"})
    env = L.fleet_env(ctx.tmp, results, job, plan_path)
    if routes_table is not None:
        p = os.path.join(ctx.tmp, f"{name}_routes.json")
        lib.write_json(p, routes_table)
        env["AVOS_GATEWAY_ROUTES"] = p
    else:
        env["AVOS_GATEWAY_ROUTES"] = os.path.join(ctx.tmp, f"{name}_no_routes.json")
    ep = os.path.join(ctx.tmp, f"{name}_gateway.env")
    with open(ep, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in (ctx.keys() if env_vars == "default" else env_vars).items()))
    os.chmod(ep, 0o600)
    env["AVOS_GATEWAY_ENV"] = ep
    return results, job, env


@check("F1_fleet_json_records_route", "done test 2: fleet.json carries policy, route kind and the pinned settings; a resume under another table is refused")
def f1(ctx):
    vllm = ctx.fake((200, {}, lib.vllm_models(lib.OPEN_ROLES["policy"], lib.ROOT_RETRAINED)), key=ctx.keys()["POLICY_API_KEY"])
    table = {lib.CLOSED: lib.anthropic_route("https://api.anthropic.com"),
             lib.OPEN_ROLES["policy"]: {"base_url": vllm.url + "/v1", "key": "POLICY_API_KEY"}}
    results, job, env = _fake_fleet_env(ctx, "f1", table)
    out = os.path.join(ctx.tmp, "f1_fleet")
    ctx.fleet("run", "--out", out, "--tasks", "0", "--trials", 1, "--concurrency", 1, "--job", job, "--policy", lib.CLOSED,
              "--simulator", lib.OPEN_ROLES["simulator"], "--judge", lib.OPEN_ROLES["nl_judge"], "--json", env=env, expect_rc=0, timeout=300)
    meta = lib.read_json(os.path.join(out, "fleet.json"))
    bad = lib.diff(meta, {"roles": {"policy": lib.CLOSED}, "policy_route": {"kind": "anthropic", "upstream": "https://api.anthropic.com",
                                                                            "effort": "medium", "max_tokens": 16000, "cache": True}})
    if "ANTHROPIC_API_KEY" in json.dumps(meta):
        bad.append("fleet.json names the key variable")
    # open policy: kind openai, upstream as written
    out2 = os.path.join(ctx.tmp, "f1_open")
    ctx.fleet("run", "--out", out2, "--tasks", "0", "--trials", 1, "--concurrency", 1, "--job", job, "--policy", lib.OPEN_ROLES["policy"],
              "--simulator", lib.OPEN_ROLES["simulator"], "--judge", lib.OPEN_ROLES["nl_judge"], "--json", env=env, expect_rc=0, timeout=300)
    bad += lib.diff(lib.read_json(os.path.join(out2, "fleet.json")), {"policy_route": {"kind": "openai", "upstream": vllm.url + "/v1"}})
    if bad:
        raise AssertionError("fleet.json: " + "; ".join(bad))
    # resume of the closed fleet under a table with another effort: refused, nothing run
    env2 = dict(env)
    p2 = os.path.join(ctx.tmp, "f1_routes_low.json")
    lib.write_json(p2, {**table, lib.CLOSED: lib.anthropic_route("https://api.anthropic.com", effort="low")})
    env2["AVOS_GATEWAY_ROUTES"] = p2
    lines_before = len(lib.read_jsonl(os.path.join(out, "trials.jsonl")))
    meta["status"], meta["trials_per_task"] = "stopped", 2      # one trial still owed, so a resume would start a job
    lib.write_json(os.path.join(out, "fleet.json"), meta)
    res = ctx.fleet("run", "--out", out, "--tasks", "0", "--trials", 2, "--concurrency", 1, "--job", job, "--policy", lib.CLOSED,
                    "--simulator", lib.OPEN_ROLES["simulator"], "--judge", lib.OPEN_ROLES["nl_judge"], "--json", env=env2, expect_rc=2, timeout=300)
    if "policy_route" not in (res["out"] + res["err"]) and "effort" not in (res["out"] + res["err"]):
        bad.append(f"the resume refusal does not name policy_route/effort: {lib.short(res['err'], 200)!r}")
    if len(lib.read_jsonl(os.path.join(out, "trials.jsonl"))) != lines_before:
        bad.append("the refused resume ran a trial")
    if bad:
        raise AssertionError("; ".join(bad))
    return "closed: policy_route anthropic/medium/16000/cache; open: kind openai; resume under effort low → exit 2, nothing run"


def _closed_plan():
    return {"10": ["done_pass", "done_fail"], "11": ["done_pass", "done_5xx_retried"], "12": ["done_fail", "done_fail"]}


@check("F2_matrix_refuses_bad_rows", "done test 2: another effort, another max_tokens, a missing usage field or another policy model → exit 1, named; clean rows → matrix")
def f2(ctx):
    prices = os.path.join(ctx.scratch("f2"), "prices.json")
    lib.closed_prices(prices)
    fdir, _, runs = lib.closed_fleet(ctx.tmp, "f2_ok", _closed_plan())
    res = ctx.fleet_json("matrix", "--fleet", fdir, "--prices", prices, expect_rc=0)
    if (res["json"].get("m12") or {}).get("policy", {}).get("basis") != "tokens":
        raise AssertionError(f"clean closed fleet: m12.policy.basis {res['json'].get('m12', {}).get('policy', {}).get('basis')!r} != tokens")
    any_run = sorted(runs)[0]
    cases = [("effort_mismatch", lambda run, i, r: r.__setitem__("effort", "low") if run == any_run and r["status"] == 200 else None),
             ("max_tokens_mismatch", lambda run, i, r: r.__setitem__("max_tokens", 8000) if run == any_run and r["status"] == 200 else None),
             ("usage_missing", lambda run, i, r: r["usage"].__setitem__("cache_read_input_tokens", None) if run == any_run and r["status"] == 200 else None),
             ("usage_missing", lambda run, i, r: r.pop("usage", None) if run == any_run and r["status"] == 200 else None),
             ("policy_mismatch", lambda run, i, r: r.__setitem__("model", "claude-sonnet-5") if run == any_run else None)]
    notes = []
    for k, (reason, patch) in enumerate(cases):
        fdir_k, _, _ = lib.closed_fleet(ctx.tmp, f"f2_{k}_{reason}", _closed_plan(), rows_patch=patch)
        res = ctx.fleet("matrix", "--fleet", fdir_k, "--prices", prices, "--json", expect_rc=1)
        ctx.refusal(res, reason, any_run)
        notes.append(reason)
    # a closed fleet.json without policy_route: usage error
    fdir_n, _, _ = lib.closed_fleet(ctx.tmp, "f2_noroute", _closed_plan())
    meta = lib.read_json(os.path.join(fdir_n, "fleet.json"))
    del meta["policy_route"]
    lib.write_json(os.path.join(fdir_n, "fleet.json"), meta)
    ctx.fleet("matrix", "--fleet", fdir_n, "--prices", prices, "--json", expect_rc=2)
    return f"clean → basis tokens; {', '.join(notes)} → exit 1 naming the run; no policy_route → exit 2"


@check("F3_m12_closed_from_rows", "done test 3: M12 from usage × dated prices with cache rates, per trial and per successful trial, uncached ceiling; follows the rows")
def f3(ctx):
    d = ctx.scratch("f3")
    prices = os.path.join(d, "prices.json")
    pj = lib.closed_prices(prices)
    fdir, results, runs = lib.closed_fleet(ctx.tmp, "f3", _closed_plan(), seed=23)
    want = lib.closed_m12_oracle(fdir, pj)
    got = ctx.fleet_json("matrix", "--fleet", fdir, "--prices", prices)["json"]
    pol = (got.get("m12") or {}).get("policy") or {}
    pass1 = got["m11"]["pass1"]
    exp = {"model": lib.CLOSED, "basis": "tokens", "calls": want["calls"], "ok": want["ok"], "prompt_tokens": want["prompt_tokens"],
           "completion_tokens": want["completion_tokens"], "usage": want["usage"], "usd_total": want["usd_total"],
           "usd_per_task": want["usd_per_task"], "usd_per_successful_task": want["usd_per_task"] / pass1,
           "uncached_ceiling_usd_per_task": want["uncached_ceiling_usd_per_task"], "source": "gateway"}
    bad = lib.diff(pol, exp)
    bad += lib.diff(pol.get("price"), {"prompt_per_mtok": 4.0, "completion_per_mtok": 20.0, "cache_write_per_mtok": 5.0, "cache_read_per_mtok": 0.2}, ".price")
    if isinstance(pol.get("token_ceiling_usd_per_task"), (int, float)) and abs(pol["token_ceiling_usd_per_task"] - want["uncached_ceiling_usd_per_task"]) > 1e-9:
        bad.append("token_ceiling_usd_per_task disagrees with uncached_ceiling_usd_per_task")
    if want["usd_total"] >= want["uncached_ceiling_usd_per_task"] * want["trials"]:
        bad.append("oracle sanity: cached cost should be below the uncached ceiling")
    for t in got.get("trials", []):
        bm = ((t.get("llm") or {}).get("by_model") or {}).get(lib.CLOSED) if t.get("llm") != "unverified" else None
        if bm is None:
            continue
        w = want["per_trial_usd"][t["run"]]
        if not isinstance(bm.get("usd"), (int, float)) or abs(bm["usd"] - w) > 1e-9:
            bad.append(f"{t['run']}: by_model.usd {bm.get('usd')} != {w}")
        if not isinstance(bm.get("usage"), dict) or set(bm["usage"]) < set(lib.USAGE_KEYS):
            bad.append(f"{t['run']}: by_model.usage lacks the five sums")
    # no cache rates → cost unverified, ceiling still a number
    prices2 = os.path.join(d, "prices_nocache.json")
    lib.closed_prices(prices2, cache=False)
    g2 = ctx.fleet_json("matrix", "--fleet", fdir, "--prices", prices2)["json"]["m12"]["policy"]
    for k in ("usd_total", "usd_per_task", "usd_per_successful_task"):
        if g2.get(k) != "unverified":
            bad.append(f"without cache rates {k} = {g2.get(k)!r}, want unverified")
    if not isinstance(g2.get("uncached_ceiling_usd_per_task"), (int, float)):
        bad.append("without cache rates the uncached ceiling should still be a number")
    # the rows change under the same size and mtime: the answer follows
    run = next(r for r in sorted(runs) if runs[r] == "done_pass")
    p = os.path.join(results, run, "events.jsonl")
    rows = lib.read_jsonl(p)
    target, di = None, 0
    for r in rows:
        if r.get("kind") != "llm.call" or r.get("model") != lib.CLOSED or r.get("status") != 200:
            continue
        i, c = r["usage"]["input_tokens"], r["usage"]["cache_read_input_tokens"]
        if i % 10 != 0 and c % 10 != 9 and c > 0:          # input −1, cache read +1: same digits, same sum
            target, di = r, -1
            break
        if i % 10 != 9 and c % 10 != 0 and c > 1:          # input +1, cache read −1
            target, di = r, 1
            break
    if target is None:
        raise AssertionError("suite: no row allows a same-size usage swap")
    u = target["usage"]
    old_i, new_i = f'"input_tokens": {u["input_tokens"]}', f'"input_tokens": {u["input_tokens"] + di}'
    old_c, new_c = f'"cache_read_input_tokens": {u["cache_read_input_tokens"]}', f'"cache_read_input_tokens": {u["cache_read_input_tokens"] - di}'
    text = open(p, encoding="utf-8").read()
    line = next(l for l in text.splitlines() if target["event_id"] in l)
    new_line = line.replace(old_i, new_i, 1).replace(old_c, new_c, 1)
    lib.same_size_replace(p, line, new_line)
    want2 = lib.closed_m12_oracle(fdir, pj)
    if abs(want2["usd_total"] - want["usd_total"]) < 1e-12:
        bad.append("oracle sanity: the swap should change the cost")
    g3 = ctx.fleet_json("matrix", "--fleet", fdir, "--prices", prices)["json"]["m12"]["policy"]
    bad += lib.diff(g3, {"usd_total": want2["usd_total"], "usd_per_task": want2["usd_per_task"]})
    if bad:
        raise AssertionError("; ".join(bad))
    return (f"usd_total {want['usd_total']:.6f} over {want['trials']} trials, ceiling {want['uncached_ceiling_usd_per_task']:.6f}/trial, "
            f"per-trial usd + usage sums; no cache rates → unverified; same-size row edit → {want2['usd_total']:.6f}")


@check("F4_open_arms_unchanged", "done test 3: an open fleet's matrix still equals fleet/tests' oracle (GPU-hours basis, token ceiling)")
def f4(ctx):
    L = lib.fleet_lib()
    prices = L.PRICES
    fdir, _, _ = L.build_synthetic_fleet(ctx.tmp, "f4_open", {"0": ["done_pass", "done_fail", "done_pass"], "1": ["done_pass", "failed_5xx", "done_pass"],
                                                              "2": ["done_fail", "done_fail", "done_length"]}, seed=5)
    got = ctx.fleet_json("matrix", "--fleet", fdir, "--prices", prices)["json"]
    want = L.matrix_oracle(fdir, lib.read_json(prices))
    bad = L.diff(got, want)
    if bad:
        raise AssertionError("; ".join(bad[:8]) + (f" (+{len(bad) - 8})" if len(bad) > 8 else ""))
    if got["m12"]["policy"]["basis"] != "gpu_hours":
        raise AssertionError("open policy basis is not gpu_hours")
    return f"m11/m12/m13/m16/causes equal the POC 10 oracle; policy basis gpu_hours, ceiling {got['m12']['policy']['token_ceiling_usd_per_task']}"


def _fleet_run(ctx, out, env, job, policy, trials=1, expect_rc=0):
    return ctx.fleet("run", "--out", out, "--tasks", "0", "--trials", trials, "--concurrency", 1, "--job", job, "--policy", policy,
                     "--simulator", lib.OPEN_ROLES["simulator"], "--judge", lib.OPEN_ROLES["nl_judge"], "--json", env=env, expect_rc=expect_rc, timeout=300)


def _nothing_ran(out, results):
    tj = os.path.join(out, "trials.jsonl")
    lines = lib.read_jsonl(tj) if os.path.exists(tj) else []
    started = [n for n in os.listdir(results) if n.startswith("run_")] if os.path.isdir(results) else []
    return [] if not lines and not started else [f"{len(lines)} journal lines, {len(started)} run dirs"]


@check("F5_policy_served_recorded", "CONTRACT §2.1 (arm provenance): fleet run records each policy upstream's served checkpoint; unprovable or mixed → refused before any trial")
def f5(ctx):
    pol = lib.OPEN_ROLES["policy"]
    key = ctx.keys()["POLICY_API_KEY"]
    t0 = len(ctx.transcripts)
    up = lambda f: {"base_url": f.url + "/v1", "key": "POLICY_API_KEY"}         # as on the host: keyed, 401 without the key
    u1 = ctx.fake((200, {}, lib.vllm_models(pol, lib.ROOT_RETRAINED)), key=key)
    u2 = ctx.fake((200, {}, lib.vllm_models(pol, lib.ROOT_RETRAINED)), key=key)
    table = {pol: [up(u1), up(u2)], lib.CLOSED: lib.anthropic_route("https://api.anthropic.com")}
    results, job, env = _fake_fleet_env(ctx, "f5", table)
    out = os.path.join(ctx.tmp, "f5_fleet")
    _fleet_run(ctx, out, env, job, pol)
    meta = lib.read_json(os.path.join(out, "fleet.json"))
    want = {"kind": "openai", "upstream": [u1.url + "/v1", u2.url + "/v1"],
            "served": [{"upstream": u1.url + "/v1", "id": pol, "root": lib.ROOT_RETRAINED}, {"upstream": u2.url + "/v1", "id": pol, "root": lib.ROOT_RETRAINED}]}
    bad = lib.diff(meta.get("policy_route"), want, ".policy_route")
    for f in (u1, u2):
        if not any(r["method"] == "GET" and r["path"] == "/v1/models" and r["headers"].get("authorization") == f"Bearer {key}" for r in f.requests):
            bad.append(f"no GET /v1/models with the route's Bearer key reached {f.url}")
    m = ctx.fleet_json("matrix", "--fleet", out, "--prices", lib.fleet_lib().PRICES, expect_rc=(0, 1))["json"]
    bad += lib.diff((m.get("inputs") or {}).get("policy_route"), meta.get("policy_route") or {"missing": True}, ".matrix.inputs.policy_route")
    # unprovable or mixed arms: refused before any trial
    u_name = ctx.fake((200, {}, lib.vllm_models("Qwen/Qwen3.5-9B", lib.ROOT_BASE)), key=key)   # serves under another name (a default model)
    u_base = ctx.fake((200, {}, lib.vllm_models(pol, lib.ROOT_BASE)), key=key)
    u_html = ctx.fake((200, {"Content-Type": "text/html"}, b"<html>not vllm</html>"), key=key)
    dead = lib.FakeUpstream()
    dead_url = dead.url
    dead.close()
    dead_up = {"base_url": dead_url + "/v1", "key": "POLICY_API_KEY"}
    others = {k: v for k, v in ctx.keys().items() if k != "POLICY_API_KEY"}
    cases = [("served name differs", [up(u_name)], "default"), ("two checkpoints behind one name", [up(u1), up(u_base)], "default"),
             ("not a models answer", [up(u_html)], "default"), ("unreachable", [dead_up], "default"),
             ("wrong key → 401", [up(u1)], {**others, "POLICY_API_KEY": "sk-pol-WRONG"}),
             ("key variable not in gateway.env", [up(u1)], others)]
    for k, (why, ups, ev) in enumerate(cases):
        res_k, job_k, env_k = _fake_fleet_env(ctx, f"f5_bad{k}", {pol: ups if len(ups) > 1 else ups[0]}, env_vars=ev)
        out_k = os.path.join(ctx.tmp, f"f5_bad{k}_fleet")
        res = _fleet_run(ctx, out_k, env_k, job_k, pol, expect_rc=1)
        try:
            ctx.refusal(res, "policy_unverified")
        except AssertionError as e:
            bad.append(f"[{why}] {e}")
        bad += [f"[{why}] refused run started trials: {x}" for x in _nothing_ran(out_k, res_k)]
    # a hosted provider (keyed, many ids, no root): the id listed → recorded with root null, the run proceeds; not listed → refused
    hosted_ids = ("meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen3-30B-A3B-Instruct-2507", "deepseek-ai/DeepSeek-V4-Flash-0731", "google/gemma-3-27b-it")
    hp = "Qwen/Qwen3-30B-A3B-Instruct-2507"
    hz = ctx.fake((200, {}, lib.hosted_models(*hosted_ids)), key=key)
    res_h, job_h, env_h = _fake_fleet_env(ctx, "f5_hosted", {hp: up(hz)})
    out_h = os.path.join(ctx.tmp, "f5_hosted_fleet")
    _fleet_run(ctx, out_h, env_h, job_h, hp)
    bad += lib.diff(lib.read_json(os.path.join(out_h, "fleet.json")).get("policy_route"),
                    {"kind": "openai", "upstream": hz.url + "/v1", "served": [{"upstream": hz.url + "/v1", "id": hp, "root": None}]}, ".hosted.policy_route")
    res_h2, job_h2, env_h2 = _fake_fleet_env(ctx, "f5_hosted_absent", {"policy-qwen3.5-9b": up(hz)})
    out_h2 = os.path.join(ctx.tmp, "f5_hosted_absent_fleet")
    try:
        ctx.refusal(_fleet_run(ctx, out_h2, env_h2, job_h2, "policy-qwen3.5-9b", expect_rc=1), "policy_unverified")
    except AssertionError as e:
        bad.append(f"[hosted, id not listed] {e}")
    bad += [f"[hosted, id not listed] {x}" for x in _nothing_ran(out_h2, res_h2)]
    # one upstream with a root, one without: two different answers behind one name → refused
    res_m, job_m, env_m = _fake_fleet_env(ctx, "f5_mixed", {pol: [up(u1), up(ctx.fake((200, {}, lib.hosted_models(pol)), key=key))]})
    out_m = os.path.join(ctx.tmp, "f5_mixed_fleet")
    try:
        ctx.refusal(_fleet_run(ctx, out_m, env_m, job_m, pol, expect_rc=1), "policy_unverified")
    except AssertionError as e:
        bad.append(f"[root + no root] {e}")
    # a resume whose upstreams now serve another checkpoint under the same name: policy_route differs → exit 2, nothing run
    u1.set((200, {}, lib.vllm_models(pol, lib.ROOT_BASE)))
    u2.set((200, {}, lib.vllm_models(pol, lib.ROOT_BASE)))
    lines_before = len(lib.read_jsonl(os.path.join(out, "trials.jsonl")))
    meta["status"], meta["trials_per_task"] = "stopped", 2
    lib.write_json(os.path.join(out, "fleet.json"), meta)
    res = _fleet_run(ctx, out, env, job, pol, trials=2, expect_rc=2)
    if "policy_route" not in res["out"] + res["err"]:
        bad.append(f"the resume refusal does not name policy_route: {lib.short(res['err'], 200)!r}")
    if len(lib.read_jsonl(os.path.join(out, "trials.jsonl"))) != lines_before:
        bad.append("the refused resume ran a trial")
    # no route table (fleet/tests, a laptop): nothing to ask, served null, the run proceeds
    res_n, job_n, env_n = _fake_fleet_env(ctx, "f5_notable")
    out_n = os.path.join(ctx.tmp, "f5_notable_fleet")
    _fleet_run(ctx, out_n, env_n, job_n, pol)
    pr = lib.read_json(os.path.join(out_n, "fleet.json")).get("policy_route") or {}
    if pr.get("upstream") is not None or pr.get("served") is not None:
        bad.append(f"no table: policy_route {pr} (want upstream null, served null)")
    # the key is read, sent, and never written: not in fleet.json, the matrix, or any stdout/stderr of these commands
    leak = [n for blob, n in ctx.transcripts[t0:] if key in blob or "sk-pol-WRONG" in blob]
    for d in (out, out_n, out_h):
        for n in os.listdir(d):
            if os.path.isfile(os.path.join(d, n)) and key in open(os.path.join(d, n), encoding="utf-8", errors="replace").read():
                leak.append(f"{os.path.basename(d)}/{n}")
    if key in json.dumps(m) or "POLICY_API_KEY" in json.dumps(meta) + json.dumps(m):
        leak.append("matrix / fleet.json")
    if leak:
        bad.append(f"the policy key or its variable name appears in: {leak[:4]}")
    if bad:
        raise AssertionError("; ".join(bad[:10]))
    return ("keyed upstreams (401 without the Bearer key): served [{upstream, id, root}] × 2 recorded in fleet.json and matrix inputs; other served "
            "name / two roots / non-models body / unreachable / wrong key / key absent → exit 1 policy_unverified, no trial; resume under another "
            "root → exit 2; no table → served null; key in no file or output; hosted provider (many ids, no root) → root null recorded, "
            "run proceeds; hosted without the id / root + no root → policy_unverified")


@check("F6_thinking_tokens_optional", "CONTRACT §2 (ruling 3): thinking_tokens may be null; the four billed counts stay required; cost unchanged")
def f6(ctx):
    prices = os.path.join(ctx.scratch("f6"), "prices.json")
    pj = lib.closed_prices(prices)
    plan = _closed_plan()
    fdir0, _, runs = lib.closed_fleet(ctx.tmp, "f6_probe", plan, seed=31)
    target = sorted(runs)[0]

    def no_thinking(run, i, r):
        if run == target and r["status"] == 200:
            r["usage"]["thinking_tokens"] = None
    fdir, _, _ = lib.closed_fleet(ctx.tmp, "f6", plan, seed=31, rows_patch=no_thinking)
    want = lib.closed_m12_oracle(fdir, pj)
    if not want["thinking_tokens_missing"]:
        raise AssertionError("suite: no row lost its thinking count")
    got = ctx.fleet_json("matrix", "--fleet", fdir, "--prices", prices, expect_rc=0)["json"]
    pol = (got.get("m12") or {}).get("policy") or {}
    bad = lib.diff(pol, {"basis": "tokens", "usage": want["usage"], "usd_total": want["usd_total"], "usd_per_task": want["usd_per_task"],
                         "thinking_tokens_missing": want["thinking_tokens_missing"]})
    # the billed counts are still required
    notes = []
    for field in ("input_tokens", "output_tokens", "cache_creation_input_tokens"):
        def drop(run, i, r, field=field):
            if run == target and r["status"] == 200:
                r["usage"][field] = None
        fk, _, _ = lib.closed_fleet(ctx.tmp, f"f6_{field}", plan, seed=31, rows_patch=drop)
        res = ctx.fleet("matrix", "--fleet", fk, "--prices", prices, "--json", expect_rc=1)
        try:
            ctx.refusal(res, "usage_missing", target)
            notes.append(field)
        except AssertionError as e:
            bad.append(f"[{field} null] {e}")
    if bad:
        raise AssertionError("; ".join(bad))
    return (f"{want['thinking_tokens_missing']} rows without thinking → accepted, thinking_tokens_missing {want['thinking_tokens_missing']}, "
            f"usd_total {want['usd_total']:.6f} as oracle; {', '.join(notes)} null → usage_missing")


@check("E1_export_policy_model", "CONTRACT §4 (ruling 1): retrain export tags a closed-policy item with policy_model; every open item byte-identical")
def e1(ctx):
    LL = lib.label_lib()
    env = LL.build_env(ctx.tmp, "e1_env")
    open_ex = LL.export_dataset(env, "e1_open")
    lib.add_closed_run(env)
    frozen = lib.read_json(os.path.join(lib.ROOT, "label/tests/fixtures/expected_poc8.json"))
    bad = []
    if open_ex["manifest"]["dataset_sha256"] != frozen["dataset_sha256"]:
        bad.append(f"open export dataset_sha256 {open_ex['manifest']['dataset_sha256'][:12]} != frozen POC 8 {frozen['dataset_sha256'][:12]}")
    tagged = [it["run"] for it in open_ex["items"] if "policy_model" in it]
    if tagged:
        bad.append(f"open items carry policy_model: {tagged[:3]}")
    mixed = LL.export_dataset(env, "e1_mixed")
    closed_items = [it for it in mixed["items"] if it["run"] == "run_retail-0_70"]
    if len(closed_items) != 1:
        raise AssertionError(f"closed run exported as {len(closed_items)} items (rc {mixed['res']['rc']}: {lib.short(mixed['res']['err'], 200)})")
    if closed_items[0].get("policy_model") != lib.CLOSED:
        bad.append(f"closed item policy_model {closed_items[0].get('policy_model')!r} != {lib.CLOSED}")
    lines = lambda d: {json.loads(l)["run"]: l for l in open(os.path.join(d, "trajectories.jsonl"), encoding="utf-8") if l.strip()}
    a, b = lines(open_ex["dir"]), lines(mixed["dir"])
    changed = [r for r in a if b.get(r) != a[r]]
    if changed or set(b) - set(a) != {"run_retail-0_70"}:
        bad.append(f"open items changed by adding a closed run: {changed[:3]}; new runs {sorted(set(b) - set(a))}")
    # label keeps nothing of the closed run and changes nothing else
    finals = {}
    for name, ds in (("open", open_ex["dir"]), ("mixed", mixed["dir"])):
        out = os.path.join(ctx.tmp, f"e1_label_{name}")
        ctx.cli(lib.LABEL, "run", "--dataset", ds, "--out", out, "--level", "0", env=lib.private_env(ctx.tmp, f"e1_{name}", {"AVOS_LABEL_DATASET": ds}), expect_rc=(0, 1))
        finals[name] = {r["run"]: r["final"] for r in lib.read_jsonl(os.path.join(out, "labels.jsonl"))}
    fc = finals["mixed"].get("run_retail-0_70") or {}
    if fc.get("kept") is not False or fc.get("rule") != "closed_policy":
        bad.append(f"label final of the closed item {fc}")
    moved = [r for r, f in finals["open"].items() if finals["mixed"].get(r) != f]
    if moved:
        bad.append(f"open items' labels changed: {moved[:3]}")
    if bad:
        raise AssertionError("; ".join(bad))
    return (f"open export = frozen POC 8 {frozen['dataset_sha256'][:12]}…, no policy_model; closed run → policy_model {lib.CLOSED}, "
            f"{len(a)} open item lines byte-identical; label: closed_policy, not kept, others unchanged")


# ====================================================================== T: closed outputs never train (done test 4)

@check("T1_select_refuses_closed", "done test 4: train select refuses a closed policy by name (fleet role or items), DIR not created")
def t1(ctx):
    T = lib.train_lib()
    tok = T.unpack_tokenizer(ctx.scratch("t1_tok"))
    notes = []
    for mark, policy in (("fleet", lib.CLOSED), ("fleet", lib.OPEN_ROLES["policy"]), ("items", lib.OPEN_ROLES["policy"])):
        ex, lb, fl, _ = lib.closed_dataset(ctx.tmp, f"t1_{mark}_{policy.replace('/', '_')}", mark=mark)
        out = os.path.join(ctx.tmp, f"t1_out_{mark}_{len(notes)}")
        res = ctx.cli(lib.TRAIN, "select", "--out", out, "--pair", ex, lb, "--fleet", fl, "--policy", policy, "--tokenizer", tok,
                      env=lib.private_env(ctx.tmp, "t1"), expect_rc=1, timeout=600)
        ctx.refusal(res, "closed_policy", lib.CLOSED)
        if os.path.exists(out):
            raise AssertionError(f"[{mark}/{policy}] DIR was created on refusal")
        notes.append(f"{mark}:{policy.split('-')[0]}")
    return f"{len(notes)} refusals (closed fleet role × 2 --policy, closed items under an open fleet): exit 1 naming closed_policy and {lib.CLOSED}"


@check("T2_label_never_keeps_closed", "done test 4: label run keeps no item whose policy is a closed model, even with a human pass")
def t2(ctx):
    ex, lb, fl, items = lib.closed_dataset(ctx.tmp, "t2", mark="items")
    env = lib.private_env(ctx.tmp, "t2", {"AVOS_LABEL_DATASET": ex})
    out = os.path.join(ctx.tmp, "t2_out")
    ctx.cli(lib.LABEL, "run", "--dataset", ex, "--out", out, "--level", "0", env=env, expect_rc=(0, 1))
    rows = lib.read_jsonl(os.path.join(out, "labels.jsonl"))
    if len(rows) != len(items):
        raise AssertionError(f"{len(rows)} rows for {len(items)} items")
    kept = [r["run"] for r in rows if r["final"]["kept"]]
    wrong_rule = [r["run"] for r in rows if r["final"]["rule"] != "closed_policy" or r["final"]["label"] != "fail"]
    if kept or wrong_rule:
        raise AssertionError(f"kept {kept}; rows without rule closed_policy/label fail: {wrong_rule[:3]}")
    summ = lib.read_json(os.path.join(out, "summary.json"))
    if summ["counts"]["kept"] != 0:
        raise AssertionError(f"summary.counts.kept {summ['counts']['kept']}")
    man = lib.read_json(os.path.join(out, "manifest.json"))
    if any(i.get("kept") for i in man.get("labels", {}).get("items", [])):
        raise AssertionError("manifest marks a closed item kept")
    # a human pass does not make it keepable
    run = next(r["run"] for r in rows if r["level0"].get("passed") is True)
    ctx.cli(lib.LABEL, "human", "--dataset", ex, "--run", run, "--verdict", "pass", "--labeller", "tester", env=env, expect_rc=(0, 1))
    out2 = os.path.join(ctx.tmp, "t2_out2")
    ctx.cli(lib.LABEL, "run", "--dataset", ex, "--out", out2, "--level", "0", env=env, expect_rc=(0, 1))
    r2 = next(r for r in lib.read_jsonl(os.path.join(out2, "labels.jsonl")) if r["run"] == run)
    if r2["final"]["kept"] or r2["final"]["rule"] != "closed_policy":
        raise AssertionError(f"after a human pass: {r2['final']}")
    return f"{len(rows)} closed items: 0 kept, rule closed_policy; human pass on {run} does not flip it"


# ====================================================================== M: M15 (done test 5)

def _real(name):
    return lib.read_json(os.path.join(lib.ROOT, lib.REAL_MATRICES[name]))


LEGACY_RETRAINED = {os.path.join(lib.ROOT, lib.REAL_MATRICES[n]): n for n in ("p12-heldout", "p12-heldout-r2")}


def _m15(ctx, retrained, closed, expect_rc=0, extra=(), assert_legacy=True):
    """fleet m15; the committed retrained sessions predate §2.1 and are passed with the operator's assertion."""
    args = ["m15"]
    for p in retrained:
        args += ["--retrained", p]
        if assert_legacy and p in LEGACY_RETRAINED:
            args += ["--assert-served", f"{LEGACY_RETRAINED[p]}=retrained"]
    for p in closed:
        args += ["--closed", p]
    args += list(extra)
    if expect_rc == 0:
        return ctx.fleet_json(*args, expect_rc=0)
    return ctx.fleet(*args, "--json", expect_rc=expect_rc)


def _check_m15(got, want):
    bad = []
    for arm in ("retrained", "closed"):
        g, w = got["arms"][arm], want["arms"][arm]
        bad += lib.diff(g.get("pooled"), w["pooled"], f".arms.{arm}.pooled")
        if sorted(g.get("tasks_short_4", [])) != sorted(w["tasks_short_4"]):
            bad.append(f".arms.{arm}.tasks_short_4 {g.get('tasks_short_4')} != {w['tasks_short_4']}")
        gs = g.get("sessions") or []
        if [s.get("fleet") for s in gs] != [s["fleet"] for s in w["sessions"]]:
            bad.append(f".arms.{arm}.sessions {[s.get('fleet') for s in gs]} != {[s['fleet'] for s in w['sessions']]}")
        else:
            for s, ws in zip(gs, w["sessions"]):
                bad += lib.diff(s, {k: ws[k] for k in ("pass1", "pass4", "trials", "policy")}, f".arms.{arm}.sessions[{ws['fleet']}]")
        bad += lib.diff(g.get("per_task"), w["per_task"], f".arms.{arm}.per_task")
    bad += lib.diff(got.get("diff_pass4"), want["diff_pass4"], ".diff_pass4")
    for k in ("x", "y"):
        w = dict(want[k])
        w.pop("undefined_resamples", None)
        bad += lib.diff(got.get(k), w, f".{k}")
    b = got.get("bootstrap") or {}
    if (b.get("seed"), b.get("B")) != (300, 2000):
        bad.append(f".bootstrap {b} != seed 300, B 2000")
    return bad


@check("M1_m15_reproduces_pooled", "done test 5: pooled pass^1/pass^4 + paired bootstrap on the real sessions equal data/poc12/pooled.json and the oracle")
def m1(ctx):
    R = [os.path.join(lib.ROOT, lib.REAL_MATRICES[n]) for n in ("p12-heldout", "p12-heldout-r2")]
    # --closed refuses an open model (§5 arm provenance), so the base sessions' (n, c) ride in closed-relabelled
    # copies: pooling and bootstrap read only (n, c) per task, so pooled.json's base numbers must still come out.
    C, CM = [], []
    for n in ("p11-heldout", "p12-heldout-base"):
        r = _real(n)
        m = lib.synthetic_matrix(r, f"{n}-as-closed", lib.CLOSED, {t["task"]: (t["n"], t["c"]) for t in r["m11"]["tasks"]}, basis="tokens")
        p = os.path.join(ctx.scratch("m1"), f"{n}-as-closed.json")
        lib.write_json(p, m)
        C.append(p)
        CM.append(m)
    got = _m15(ctx, R, C)["json"]
    want = lib.m15_oracle([_real("p12-heldout"), _real("p12-heldout-r2")], CM)
    bad = _check_m15(got, want)
    pooled = lib.read_json(os.path.join(lib.ROOT, "data/poc12/pooled.json"))["pooled"]
    for k in ("pass1", "pass4"):
        gr, gc = got["arms"]["retrained"]["pooled"], got["arms"]["closed"]["pooled"]
        for label, g, w in ((f"retrained {k}", gr.get(k), pooled[k]["retrained"]), (f"closed(base) {k}", gc.get(k), pooled[k]["base"])):
            if not isinstance(g, (int, float)) or round(g, 3) != w:
                bad.append(f"{label} {g} != pooled.json {w}")
        for label, g, w in ((f"retrained {k}_ci95", gr.get(k + "_ci95"), pooled[k]["retrained_ci95"]), (f"closed {k}_ci95", gc.get(k + "_ci95"), pooled[k]["base_ci95"])):
            if not isinstance(g, list) or [round(x, 3) for x in g] != w:
                bad.append(f"{label} {g} != pooled.json {w}")
    d4 = got.get("diff_pass4") or {}
    if not isinstance(d4.get("value"), float) or round(d4["value"], 3) != pooled["pass4"]["diff"] or [round(x, 3) for x in d4.get("ci95", [])] != pooled["pass4"]["diff_ci95"]:
        bad.append(f"diff_pass4 {d4} != pooled.json {pooled['pass4']['diff']} {pooled['pass4']['diff_ci95']}")
    if got.get("x", {}).get("verdict") not in ("pass", "fail", "unresolved") or got.get("y", {}).get("verdict") not in ("pass", "fail", "unresolved"):
        bad.append("claim verdicts missing")
    res = ctx.fleet("m15", "--retrained", R[0], "--assert-served", "p12-heldout=retrained", "--retrained", R[1], "--assert-served", "p12-heldout-r2=retrained",
                    "--closed", C[0], "--closed", C[1], expect_rc=0)
    for word in ("pass^1", "pass^4", "X", "Y", "0.75", "≥ 5", "asserted, not recorded"):
        if word not in res["out"] and word.replace("≥ ", ">= ") not in res["out"]:
            bad.append(f"text output lacks {word!r}")
    if bad:
        raise AssertionError("; ".join(bad[:10]))
    return (f"retrained pass^1 {got['arms']['retrained']['pooled']['pass1']:.3f} pass^4 {got['arms']['retrained']['pooled']['pass4']:.3f}; "
            f"base-as-closed pass^4 {got['arms']['closed']['pooled']['pass4']:.3f}; diff {d4['value']:+.3f} {[round(x, 3) for x in d4['ci95']]}; "
            f"X {got['x']['value']:.3f} {got['x']['verdict']}, Y {got['y']['value']:.3f} {got['y']['verdict']}")


def _synthetic_closed_pair(ctx, name, nc_fn, usd=None, **kw):
    base = _real("p12-heldout-base")
    out = []
    for i in (1, 2):
        nc = {t["task"]: nc_fn(t["task"], i) for t in base["m11"]["tasks"]}
        m = lib.synthetic_matrix(base, f"{name}-closed-{i}", lib.CLOSED, nc, basis="tokens", usd_per_trial=usd, **kw)
        p = os.path.join(ctx.scratch(name), f"closed-{i}.json")
        lib.write_json(p, m)
        out.append((p, m))
    return out


@check("M2_m15_closed_arm", "done test 5: X, Y and intervals with a token-priced closed arm equal the oracle; verdicts follow the bars")
def m2(ctx):
    rnd = lib.random.Random(42)
    pair = _synthetic_closed_pair(ctx, "m2", lambda t, i: (4, rnd.choice([1, 2, 3, 3, 4, 4])))
    R = [os.path.join(lib.ROOT, lib.REAL_MATRICES[n]) for n in ("p12-heldout", "p12-heldout-r2")]
    got = _m15(ctx, R, [p for p, _ in pair])["json"]
    want = lib.m15_oracle([_real("p12-heldout"), _real("p12-heldout-r2")], [m for _, m in pair])
    bad = _check_m15(got, want)
    for k, bar in (("x", 0.75), ("y", 5.0)):
        g = got.get(k) or {}
        if g.get("bar") != bar:
            bad.append(f"{k}.bar {g.get('bar')} != {bar}")
        ci = g.get("ci95")
        if isinstance(ci, list) and len(ci) == 2:
            v = "pass" if ci[0] >= bar else ("fail" if ci[1] < bar else "unresolved")
            if g.get("verdict") != v:
                bad.append(f"{k}.verdict {g.get('verdict')} != {v} for ci {ci} and bar {bar}")
    if bad:
        raise AssertionError("; ".join(bad[:10]))
    return (f"closed pass^4 {got['arms']['closed']['pooled']['pass4']:.3f}, usd/task {got['arms']['closed']['pooled']['usd_per_task']:.4f}; "
            f"X {got['x']['value']:.3f} {got['x']['ci95'] and [round(v, 3) for v in got['x']['ci95']]} → {got['x']['verdict']}; "
            f"Y {got['y']['value']:.2f} → {got['y']['verdict']}")


@check("M3_m15_adversarial", "done test 5: task sets differ / split / simulator / duplicate / stored pass^k / policy within arm → refused; n<4 listed; zero denominator → unresolved, never inf")
def m3(ctx):
    base = _real("p12-heldout-base")
    R = [os.path.join(lib.ROOT, lib.REAL_MATRICES[n]) for n in ("p12-heldout", "p12-heldout-r2")]
    good = _synthetic_closed_pair(ctx, "m3_good", lambda t, i: (4, 2 + (int(t) % 3 == 0)))
    gp = [p for p, _ in good]
    d = ctx.scratch("m3")

    def variant(name, **kw):
        nc = {t["task"]: (4, 2) for t in base["m11"]["tasks"]}
        m = lib.synthetic_matrix(base, name, lib.CLOSED, nc, basis="tokens", usd_per_trial=0.5, **kw)
        p = os.path.join(d, name + ".json")
        lib.write_json(p, m)
        return p, m

    notes = []
    cases = [("task_sets_differ", [variant("m3-drop", drop_tasks=("10",))[0], gp[1]]),
             ("split_mismatch", [variant("m3-split", split_sha="0" * 64)[0], gp[1]]),
             ("simulator_mismatch", [variant("m3-sim", simulator="other/simulator")[0], gp[1]]),
             ("duplicate_session", [gp[0], gp[0]]),
             ("pass_k_mismatch", [variant("m3-passk", pass_patch=lambda m: m["m11"]["tasks"][0].__setitem__("pass1", 0.9))[0], gp[1]])]
    for reason, closed in cases:
        res = _m15(ctx, R, closed, expect_rc=1)
        ctx.refusal(res, reason)
        notes.append(reason)
    # a second copy of the same session under another file name is still a duplicate
    dup = os.path.join(d, "dup.json")
    shutil.copy(gp[0], dup)
    ctx.refusal(_m15(ctx, R, [gp[0], dup], expect_rc=1), "duplicate_session")
    # policy differs within an arm
    other, _ = variant("m3-otherpolicy")
    om = lib.read_json(other)
    om["inputs"]["roles"]["policy"] = "claude-sonnet-5"
    lib.write_json(other, om)
    ctx.refusal(_m15(ctx, R, [gp[0], other], expect_rc=1), "policy_mismatch")
    notes.append("policy_mismatch")
    # n < 4: one retrained session alone has task 110 at n 3 → listed, excluded from pass^4 and X, never a crash
    got = _m15(ctx, [R[1]], gp)["json"]
    want = lib.m15_oracle([_real("p12-heldout-r2")], [m for _, m in good])
    bad = _check_m15(got, want)
    if "110" not in got["arms"]["retrained"].get("tasks_short_4", []) or got["x"].get("tasks") != 43:
        bad.append(f"n<4: tasks_short_4 {got['arms']['retrained'].get('tasks_short_4')}, x.tasks {got['x'].get('tasks')} (want 110 listed, 43 tasks)")
    # zero denominator: the closed arm never passes
    zero = _synthetic_closed_pair(ctx, "m3_zero", lambda t, i: (4, 0), usd=0.5)
    res = _m15(ctx, R, [p for p, _ in zero])
    gz = res["json"]
    if gz["x"].get("value") is not None or gz["x"].get("verdict") != "unresolved" or gz["x"].get("reason") != "zero_denominator":
        bad.append(f"zero denominator: x = {gz['x']}")
    low = res["out"].lower()
    for tok in ("inf", "nan"):
        if json.dumps(gz).lower().count(f'"{tok}') or f": {tok}" in low:
            bad.append(f"{tok} in the output")
    if bad:
        raise AssertionError("; ".join(bad[:10]))
    return f"{', '.join(notes)} refused (exit 1); duplicate by content refused; task 110 n=3 listed in tasks_short_4; zero denominator → unresolved"


@check("M4_m15_follows_sources", "CONTRACT §5: a session file edited under the same size and mtime changes the answer")
def m4(ctx):
    base = _real("p12-heldout-base")
    R = [os.path.join(lib.ROOT, lib.REAL_MATRICES[n]) for n in ("p12-heldout", "p12-heldout-r2")]
    d = ctx.scratch("m4")
    nc1 = {t["task"]: (4, 2) for t in base["m11"]["tasks"]}
    m_a = lib.synthetic_matrix(base, "m4-closed-a", lib.CLOSED, nc1, basis="tokens", usd_per_trial=0.5)
    m_b = lib.synthetic_matrix(base, "m4-closed-b", lib.CLOSED, nc1, basis="tokens", usd_per_trial=0.5)
    pa, pb = os.path.join(d, "a.json"), os.path.join(d, "b.json")
    pad = 4096
    for p, m in ((pa, m_a), (pb, m_b)):
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(m, indent=1, sort_keys=True) + " " * pad + "\n")
    first = _m15(ctx, R, [pa, pb])["json"]
    want1 = lib.m15_oracle([_real("p12-heldout"), _real("p12-heldout-r2")], [m_a, m_b])
    bad = _check_m15(first, want1)
    # every task of session b now passes 4/4, same bytes count, same mtime
    nc2 = {t["task"]: (4, 4) for t in base["m11"]["tasks"]}
    m_b2 = lib.synthetic_matrix(base, "m4-closed-b", lib.CLOSED, nc2, basis="tokens", usd_per_trial=0.5)
    st = os.stat(pb)
    body = json.dumps(m_b2, indent=1, sort_keys=True)
    size = st.st_size
    if len(body) + 1 > size:
        raise AssertionError("suite: padding too small for the same-size rewrite")
    with open(pb, "w", encoding="utf-8") as f:
        f.write(body + " " * (size - len(body) - 1) + "\n")
    os.utime(pb, ns=(st.st_atime_ns, st.st_mtime_ns))
    if os.stat(pb).st_size != size or os.stat(pb).st_mtime_ns != st.st_mtime_ns:
        raise AssertionError("suite: rewrite changed size or mtime")
    second = _m15(ctx, R, [pa, pb])["json"]
    want2 = lib.m15_oracle([_real("p12-heldout"), _real("p12-heldout-r2")], [m_a, m_b2])
    bad += _check_m15(second, want2)
    if abs(want2["arms"]["closed"]["pooled"]["pass4"] - want1["arms"]["closed"]["pooled"]["pass4"]) < 1e-9:
        bad.append("suite: the edit should change pooled pass^4")
    if bad:
        raise AssertionError("; ".join(bad[:8]))
    return f"closed pass^4 {want1['arms']['closed']['pooled']['pass4']:.3f} → {want2['arms']['closed']['pooled']['pass4']:.3f} after a same-size, same-mtime edit"


@check("M5_m15_arm_provenance", "CONTRACT §5 arm provenance: --closed refuses an open model, --retrained a closed one or the base checkpoint; legacy sessions only by a named assertion")
def m5(ctx):
    d = ctx.scratch("m5")
    pol = lib.OPEN_ROLES["policy"]
    legacy_rt = os.path.join(lib.ROOT, lib.REAL_MATRICES["p12-heldout"])
    legacy_base = os.path.join(lib.ROOT, lib.REAL_MATRICES["p12-heldout-base"])
    closed = [p for p, _ in _synthetic_closed_pair(ctx, "m5", lambda t, i: (4, 2 + (int(t) % 2)))]
    rt_real = _real("p12-heldout")

    def open_session(name, root, policy_route="set"):
        nc = {t["task"]: (t["n"], t["c"]) for t in rt_real["m11"]["tasks"]}
        route = lib.open_route(root) if policy_route == "set" else policy_route
        m = lib.synthetic_matrix(rt_real, name, pol, nc, basis="gpu_hours", usd_per_trial=0.02, policy_route=route)
        p = os.path.join(d, name + ".json")
        lib.write_json(p, m)
        return p, m
    rec1, mrec1 = open_session("m5-rt-1", lib.ROOT_RETRAINED)
    rec2, mrec2 = open_session("m5-rt-2", lib.ROOT_RETRAINED)
    on_base, _ = open_session("m5-rt-on-base", lib.ROOT_BASE)
    other_ck, _ = open_session("m5-rt-other", "/opt/avos/ckpt/p13-some-other-merge")
    base_root = ["--base-root", lib.ROOT_BASE]
    bad, notes = [], []

    def refused(reason, retrained, closed_, *needles, extra=(), assert_legacy=True, rc=1):
        res = _m15(ctx, retrained, closed_, expect_rc=rc, extra=extra, assert_legacy=assert_legacy)
        try:
            ctx.refusal(res, *((reason,) if reason else ()), *needles)
            notes.append(reason or f"exit {rc}")
        except AssertionError as e:
            bad.append(f"[{reason or rc}] {e}")

    # --closed: an open model (the base sessions, or any openai-route session) or a closed name without a recorded route
    refused("closed_arm_not_closed", [rec1], [legacy_base], "p12-heldout-base", extra=base_root)
    refused("closed_arm_not_closed", [rec1], [rec2], "m5-rt-2", extra=base_root)
    no_route = lib.read_json(closed[0])
    no_route["inputs"].pop("policy_route", None)
    p_nr = os.path.join(d, "closed-no-route.json")
    lib.write_json(p_nr, no_route)
    refused("closed_arm_not_closed", [rec1], [p_nr], extra=base_root)
    # --retrained: a closed-model session, the base checkpoint, two checkpoints pooled, or a legacy session with no assertion
    refused("retrained_arm_not_open", [closed[1]], [closed[0]], extra=base_root)
    refused("retrained_is_base", [rec1, on_base], closed, "m5-rt-on-base", extra=base_root)
    refused("retrained_checkpoint_mismatch", [rec1, other_ck], closed, extra=base_root)
    refused("provenance_missing", [legacy_rt], closed, "p12-heldout", assert_legacy=False)
    hosted_rt, _ = open_session("m5-rt-hosted", None)            # served from a provider that reports no root: weights unprovable
    refused("served_root_missing", [rec1, hosted_rt], closed, "m5-rt-hosted", extra=base_root)
    refused("served_root_missing", [hosted_rt], closed, "m5-rt-hosted", extra=[*base_root, "--assert-served", "m5-rt-hosted=retrained"])
    refused("assertion_not_needed", [rec1], closed, "m5-rt-1", extra=[*base_root, "--assert-served", "m5-rt-1=retrained"])
    refused(None, [rec1], closed, "base-root", extra=[], rc=2)                                    # recorded roots need --base-root
    refused(None, [legacy_rt], closed, extra=["--assert-served", "p12-heldout=closed"], assert_legacy=False, rc=2)
    refused(None, [legacy_rt], closed, extra=["--assert-served", "no-such-fleet=retrained"], rc=2)
    # accepted: recorded sessions, then recorded + one asserted legacy session
    got = _m15(ctx, [rec1, rec2], closed, extra=base_root)["json"]
    cms = [lib.read_json(p) for p in closed]
    bad += _check_m15(got, lib.m15_oracle([mrec1, mrec2], cms))
    for s in got["arms"]["retrained"].get("sessions", []):
        if s.get("provenance") != "recorded" or s.get("served_root") != lib.ROOT_RETRAINED:
            bad.append(f"recorded session {s.get('fleet')}: provenance {s.get('provenance')!r}, served_root {s.get('served_root')!r}")
    pv = got.get("provenance") or {}
    if pv.get("base_root") != lib.ROOT_BASE or pv.get("assertions") != []:
        bad.append(f"provenance {pv} (want base_root echoed, no assertions)")
    got2 = _m15(ctx, [rec1, legacy_rt], closed, extra=base_root)["json"]
    bad += _check_m15(got2, lib.m15_oracle([mrec1, rt_real], cms))
    ss = {s.get("fleet"): s for s in got2["arms"]["retrained"].get("sessions", [])}
    if (ss.get("p12-heldout") or {}).get("provenance") != "asserted, not recorded" or (ss.get("m5-rt-1") or {}).get("provenance") != "recorded":
        bad.append(f"session provenance {[(k, v.get('provenance')) for k, v in ss.items()]}")
    if (got2.get("provenance") or {}).get("assertions") != [{"fleet": "p12-heldout", "arm": "retrained", "status": "asserted, not recorded"}]:
        bad.append(f"assertions {(got2.get('provenance') or {}).get('assertions')}")
    if bad:
        raise AssertionError("; ".join(bad[:10]))
    return f"refused: {', '.join(notes)}; recorded roots accepted and echoed; a legacy session only via --assert-served, reported 'asserted, not recorded'"


# ====================================================================== L: one real exchange with Anthropic (--live)

@check("L1_live_tool_exchange", "done test 1: a real two-step tool exchange with claude-opus-5-5 through the gateway path; rows validated (< $0.05)", live=True)
def l1(ctx):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise lib.SkipCheck("ANTHROPIC_API_KEY not in the environment")
    routes = {lib.CLOSED: lib.anthropic_route("https://api.anthropic.com", max_tokens=LIVE_MAX_TOKENS)}
    g = lib.Gateway(ctx.tmp, "live", routes, {"ANTHROPIC_API_KEY": key}, ctx.certs())
    ctx.gateways.append(g)
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city.",
                                               "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
    msgs = [{"role": "system", "content": "You are a terse assistant. Always use the tool when asked about weather."},
            {"role": "assistant", "content": "Hi! How can I help you today?"},          # τ²'s history opens with the greeting (ruling 2)
            {"role": "user", "content": "What is the weather in Paris right now? Use the tool."}]
    status, _, out = g.call(lib.tau2_request(msgs, tools, top_p=1.0, seed=300), timeout=300)
    try:
        r1 = json.loads(out)
    except ValueError:
        raise AssertionError(f"step 1: non-JSON reply {status} {lib.short(out, 160)!r}")
    if status != 200:
        raise AssertionError(f"step 1: {status} {lib.short(r1, 300)}")
    m1 = r1["choices"][0]["message"]
    tcs = m1.get("tool_calls") or []
    if len(tcs) != 1 or tcs[0]["function"]["name"] != "get_weather" or r1["choices"][0]["finish_reason"] != "tool_calls":
        raise AssertionError(f"step 1: expected one get_weather tool call, got {lib.short(m1, 300)} finish {r1['choices'][0].get('finish_reason')}")
    json.loads(tcs[0]["function"]["arguments"])
    msgs2 = msgs + [m1, {"role": "tool", "tool_call_id": tcs[0]["id"], "content": json.dumps({"temp_c": 18, "sky": "clear"})}]
    status, _, out = g.call(lib.tau2_request(msgs2, tools), timeout=300)
    r2 = json.loads(out)
    if status != 200:
        raise AssertionError(f"step 2: {status} {lib.short(r2, 300)}")
    m2 = r2["choices"][0]["message"]
    if not isinstance(m2.get("content"), str) or not m2["content"].strip() or r2["choices"][0]["finish_reason"] != "stop":
        raise AssertionError(f"step 2: expected a text answer with finish stop, got {lib.short(m2, 200)} / {r2['choices'][0].get('finish_reason')}")
    rows = g.rows()
    bad = [] if len(rows) == 2 else [f"{len(rows)} rows for 2 calls"]
    for i, (row, rep) in enumerate(zip(rows, (r1, r2))):
        bad += lib.diff(row, {"status": 200, "upstream_status": 200, "error": None, "route_kind": "anthropic", "effort": "medium",
                              "max_tokens": LIVE_MAX_TOKENS, "cache": True, "model": lib.CLOSED, "response_id": rep.get("id")})
        u = row.get("usage") or {}
        if not all(isinstance(u.get(k), int) for k in lib.USAGE_KEYS):
            bad.append(f"row {i}: usage {u} is not five integers")
        elif row.get("prompt_tokens") != u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"] or row.get("completion_tokens") != u["output_tokens"]:
            bad.append(f"row {i}: prompt/completion {row.get('prompt_tokens')}/{row.get('completion_tokens')} do not follow usage {u}")
        if not str(row.get("response_id", "")).startswith("msg_"):
            bad.append(f"row {i}: response_id {row.get('response_id')!r}")
        for f in ("temperature", "top_p", "seed"):
            if i == 0 and f not in (row.get("dropped_fields") or []):
                bad.append(f"row 0: dropped_fields lacks {f}")
    bad += lib.diff(rows[1], lib.expect_hashes(lib.tau2_request(msgs2, tools), m2))
    if key.encode() in g.everything_written():
        bad.append("the real key appears in the store or log")
    if bad:
        raise AssertionError("; ".join(bad))
    u1, u2 = rows[0]["usage"], rows[1]["usage"]
    cost = sum((u["input_tokens"] * 4 + u["cache_creation_input_tokens"] * 5 + u["cache_read_input_tokens"] * 0.2 + u["output_tokens"] * 20) / 1e6 for u in (u1, u2))
    return f"tool_call then text; rows: usage {u1} / {u2}; ≈ ${cost:.4f} at list price"


# ====================================================================== H: the fleet host (--host)

def _host_json(ctx, cmd, timeout=900):
    p = ctx.ssh(cmd, timeout=timeout)
    if "Traceback" in p.stderr:
        raise AssertionError(f"traceback on the host: {lib.short(p.stderr.strip().splitlines()[-1], 200)}")
    try:
        return json.loads(p.stdout.strip().splitlines()[-1]), p
    except (ValueError, IndexError):
        raise AssertionError(f"no JSON from the host: rc={p.returncode} {lib.short(p.stderr, 200)!r}")


def _host_setup(ctx):
    global HOST_REPO
    p = ctx.ssh(f"test -f {HOST_REPO}/fleet/bin/fleet && echo yes", timeout=60)
    if p.returncode or "yes" not in p.stdout:
        raise lib.SkipCheck(f"ssh {HOST}: {lib.short(p.stderr, 160)} (repo not at {HOST_REPO}?)")
    HOST_REPO = ctx.ssh(f"cd {HOST_REPO} && pwd -P").stdout.strip() or HOST_REPO
    ctx.ssh(f"sudo rm -rf {HOST_TMP}; mkdir -p {HOST_TMP}; chmod 777 {HOST_TMP}")
    ctx.ssh(f"cat > {HOST_TMP}/prices.json", stdin=open(lib.PRICES).read())


@check("H1_live_route_table", "done test 2: the host's route table sends claude-opus-5-5 to Anthropic with the pinned settings", host=True)
def h1(ctx):
    _host_setup(ctx)
    j, p = _host_json(ctx, f"cd {HOST_REPO} && sudo /opt/avos/gateway/bin/python gateway/gateway.py route '{lib.CLOSED}' vm-03", timeout=60)
    bad = lib.diff(j, {"model": lib.CLOSED, "kind": "anthropic", "effort": "medium", "max_tokens": 16000, "cache": True})
    if not str(j.get("upstream", "")).startswith("https://api.anthropic.com"):
        bad.append(f"upstream {j.get('upstream')!r}")
    if bad:
        raise AssertionError("; ".join(bad))
    return f"route {j['upstream']} kind anthropic, effort medium, max_tokens 16000, cache on"


@check("H2_one_closed_trial", "done tests 1–3 on the host: one real closed-policy trial through the unchanged fleet; rows, fleet.json, matrix, refusal, no key", host=True)
def h2(ctx):
    _host_setup(ctx)
    out = f"{HOST_TMP}/closed"
    cmd = (f"cd {HOST_REPO} && sudo env TIMEOUT_S=900 MEM_MIB={HOST_MEM} python3 fleet/bin/fleet run --out {out} --tasks 0 --trials 1 "
           f"--concurrency 1 --slots vm-03-vm-03 --mem-mib {HOST_MEM} --policy {lib.CLOSED} --json 2> {HOST_TMP}/run.err")
    summ, _ = _host_json(ctx, cmd, timeout=1500)
    meta = json.loads(ctx.ssh(f"sudo cat {out}/fleet.json").stdout)
    bad = lib.diff(meta, {"roles": {"policy": lib.CLOSED}, "policy_route": {"kind": "anthropic", "effort": "medium", "max_tokens": 16000, "cache": True}})
    lines = [json.loads(l) for l in ctx.ssh(f"sudo cat {out}/trials.jsonl").stdout.splitlines() if l.strip()]
    if len(lines) != 1:
        raise AssertionError(f"{len(lines)} journal lines; summary {summ}")
    run = lines[0]["run"]
    ev = ctx.ssh(f"sudo cat /var/lib/avos/results/{run}/events.jsonl").stdout
    rows = [json.loads(l) for l in ev.splitlines() if l.strip()]
    pol = [r for r in rows if r.get("kind") == "llm.call" and r.get("model") == lib.CLOSED]
    ok = [r for r in pol if r.get("status") == 200]
    if not ok:
        raise AssertionError(f"no 200 policy row in {run} (statuses {[r.get('status') for r in pol]}); trial {lines[0]['status']}")
    for r in ok:
        u = r.get("usage") or {}
        if r.get("effort") != "medium" or r.get("max_tokens") != 16000 or not all(isinstance(u.get(k), int) for k in lib.USAGE_KEYS):
            bad.append(f"row {r['event_id']}: effort {r.get('effort')} max_tokens {r.get('max_tokens')} usage {u}")
            break
        if r.get("prompt_tokens") != u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"] or r.get("completion_tokens") != u["output_tokens"]:
            bad.append(f"row {r['event_id']}: prompt/completion do not follow usage")
            break
        if not str(r.get("response_id", "")).startswith("msg_") or r.get("upstream_status") != 200:
            bad.append(f"row {r['event_id']}: response_id {r.get('response_id')!r} upstream_status {r.get('upstream_status')}")
            break
    others = {r.get("model") for r in rows if r.get("kind") == "llm.call" and r.get("status") == 200} - {lib.CLOSED}
    if not others:
        bad.append("no simulator/judge rows: the other roles did not run")
    g, _ = _host_json(ctx, f"cd {HOST_REPO} && sudo python3 fleet/bin/fleet matrix --fleet {out} --prices {HOST_TMP}/prices.json --json")
    pm = g["m12"]["policy"]
    if pm.get("basis") != "tokens" or not isinstance(pm.get("usd_total"), (int, float)):
        bad.append(f"matrix m12.policy {lib.short(pm, 200)}")
    else:
        want = sum((u["input_tokens"] * 4 + u["cache_creation_input_tokens"] * 5 + u["cache_read_input_tokens"] * 0.2 + u["output_tokens"] * 20) / 1e6
                   for u in (r["usage"] for r in ok))
        if abs(pm["usd_total"] - want) > 1e-9:
            bad.append(f"usd_total {pm['usd_total']} != {want} from the rows at the fixture prices")
    # a copy with one row at another effort is refused
    ctx.ssh(f"sudo cp -r {out} {out}-bad && sudo cp -r /var/lib/avos/results/{run} {HOST_TMP}/{run} && "
            f"sudo sed -i '0,/\"effort\": \"medium\"/s//\"effort\": \"low\"/' {HOST_TMP}/{run}/events.jsonl && "
            f"sudo python3 - <<'EOF'\nimport json\np='{out}-bad/fleet.json'\nm=json.load(open(p))\nm['results_root']='{HOST_TMP}'\njson.dump(m,open(p,'w'))\nEOF")
    p = ctx.ssh(f"cd {HOST_REPO} && sudo python3 fleet/bin/fleet matrix --fleet {out}-bad --prices {HOST_TMP}/prices.json --json")
    if p.returncode != 1 or "effort_mismatch" not in (p.stdout + p.stderr):
        bad.append(f"tampered copy: rc {p.returncode}, no effort_mismatch")
    blob = ctx.ssh(f"sudo cat /var/lib/avos/results/{run}/*.json /var/lib/avos/results/{run}/events.jsonl {out}/*.json* {HOST_TMP}/run.err 2>/dev/null").stdout
    keyish = [l for l in blob.splitlines() if "sk-ant-" in l]
    if keyish:
        bad.append("an Anthropic key appears in a run directory, journal or runner output")
    if bad:
        raise AssertionError("; ".join(bad))
    return f"{run} {lines[0]['status']}: {len(ok)} policy rows with usage×5; fleet.json pinned; m12 tokens ${pm['usd_total']:.4f}; effort edit refused; no key"


# ====================================================================== selftest

def selftest(ctx):
    results = []

    def t(name, fn):
        try:
            msg = fn()
            results.append((True, name, msg))
        except Exception as e:
            results.append((False, name, f"{type(e).__name__}: {e}"))

    def manifest():
        bad = lib.fixture_drift()
        if bad:
            raise AssertionError(bad)
        return f"{sum(1 for l in open(lib.FIX_MANIFEST) if l.strip())} hashed files unchanged"

    def pooled():
        want = lib.m15_oracle([_real("p12-heldout"), _real("p12-heldout-r2")], [_real("p11-heldout"), _real("p12-heldout-base")])
        pj = lib.read_json(os.path.join(lib.ROOT, "data/poc12/pooled.json"))["pooled"]
        for k in ("pass1", "pass4"):
            for arm, name in (("retrained", "retrained"), ("closed", "base")):
                p = want["arms"][arm]["pooled"]
                if round(p[k], 3) != pj[k][name] or [round(x, 3) for x in p[k + "_ci95"]] != pj[k][name + "_ci95"]:
                    raise AssertionError(f"{arm} {k}: {p[k]} {p[k + '_ci95']} vs pooled.json {pj[k][name]} {pj[k][name + '_ci95']}")
        d = want["diff_pass4"]
        if round(d["value"], 3) != pj["pass4"]["diff"] or [round(x, 3) for x in d["ci95"]] != pj["pass4"]["diff_ci95"]:
            raise AssertionError(f"diff {d}")
        if want["x"]["value"] <= 0 or want["y"]["value"] <= 0:
            raise AssertionError("ratios")
        return f"pass1/pass4/ci95/diff reproduce pooled.json; X {want['x']['value']:.3f}, Y(base) {want['y']['value']:.3f}"

    def open_recompute():
        prices = lib.read_json(os.path.join(lib.ROOT, "data/poc12/prices.json"))
        notes = []
        for name in ("p12-heldout", "p12-heldout-base", "p12-heldout-r2"):
            m = _real(name)
            got = lib.open_m12_recompute(m, prices)
            for role in ("simulator", "nl_judge"):
                if abs(got[role]["usd_total"] - m["m12"][role]["usd_total"]) > 1e-9 or abs(got[role]["usd_per_task"] - m["m12"][role]["usd_per_task"]) > 1e-9:
                    raise AssertionError(f"{name} {role}: {got[role]} vs {m['m12'][role]}")
            if abs(got["policy"]["token_ceiling_usd_per_task"] - m["m12"]["policy"]["token_ceiling_usd_per_task"]) > 1e-9:
                raise AssertionError(f"{name} ceiling {got['policy']} vs {m['m12']['policy']['token_ceiling_usd_per_task']}")
            notes.append(f"{name} ceiling {m['m12']['policy']['token_ceiling_usd_per_task']:.4f}")
        return "; ".join(notes)

    def closed_oracle():
        d = ctx.scratch("st_closed")
        pj = lib.closed_prices(os.path.join(d, "prices.json"))
        fdir, results, runs = lib.closed_fleet(ctx.tmp, "st_closed", _closed_plan(), seed=3)
        w = lib.closed_m12_oracle(fdir, pj)
        if w["ok"] == 0 or not isinstance(w["usd_total"], float) or w["usd_total"] <= 0 or w["usd_total"] >= w["uncached_ceiling_usd_per_task"] * w["trials"]:
            raise AssertionError(w)
        hand = 0.0
        for run in runs:
            p = os.path.join(results, run, "events.jsonl")
            if os.path.exists(p):
                for r in lib.read_jsonl(p):
                    if r.get("kind") == "llm.call" and r.get("model") == lib.CLOSED and r["status"] == 200:
                        u = r["usage"]
                        hand += (u["input_tokens"] * 4 + u["cache_creation_input_tokens"] * 5 + u["cache_read_input_tokens"] * 0.2 + u["output_tokens"] * 20) / 1e6
        if abs(hand - w["usd_total"]) > 1e-9:
            raise AssertionError(f"hand {hand} vs oracle {w['usd_total']}")
        p2 = lib.closed_prices(os.path.join(d, "p2.json"), cache=False)
        w2 = lib.closed_m12_oracle(fdir, p2)
        if w2["usd_total"] != "unverified" or not isinstance(w2["uncached_ceiling_usd_per_task"], float):
            raise AssertionError(w2)
        return f"{w['ok']} rows, ${w['usd_total']:.4f} < ceiling ${w['uncached_ceiling_usd_per_task'] * w['trials']:.4f}; no cache rates → unverified"

    def ratio_math():
        ci, und = lib.ratio_ci([1.0, 1.0, 1.0], [0.0, 0.0, 0.0])
        if ci is not None or und != 2000:
            raise AssertionError((ci, und))
        ci, und = lib.ratio_ci([0.8] * 10, [0.4] * 10)
        if und or [round(x, 9) for x in ci] != [2.0, 2.0]:
            raise AssertionError((ci, und))
        if lib.pass_k(0, 3, 4) is not None or lib.pass_k(3, 8, 4) != lib.math.comb(3, 4) / lib.math.comb(8, 4):
            raise AssertionError("pass_k")
        want = lib.m15_oracle([_real("p12-heldout-r2")], [_real("p11-heldout")])
        if want["arms"]["retrained"]["tasks_short_4"] != ["110"] or want["x"]["tasks"] != 43:
            raise AssertionError(want["arms"]["retrained"]["tasks_short_4"])
        zero = lib.synthetic_matrix(_real("p12-heldout-base"), "z", lib.CLOSED, {t["task"]: (4, 0) for t in _real("p12-heldout-base")["m11"]["tasks"]}, usd_per_trial=0.5)
        z = lib.m15_oracle([_real("p12-heldout")], [zero])
        if z["x"]["verdict"] != "unresolved" or z["x"]["reason"] != "zero_denominator" or "inf" in json.dumps(z).lower():
            raise AssertionError(z["x"])
        return "zero denominators counted, never inf; constant ratio → [2, 2]; r2 alone lists task 110; zero closed arm → unresolved"

    def harness():
        anth, oai = ctx.fakes()
        routes = {"open/m": {"base_url": oai.url + "/v1", "key": "OPENAI_API_KEY"}}
        g = lib.Gateway(ctx.tmp, "selftest", routes, {"OPENAI_API_KEY": "sk-oai-selftest"}, ctx.certs())
        ctx.gateways.append(g)
        canned = {"id": "chatcmpl-st", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
        oai.set((200, {}, canned))
        status, _, out = g.call({"model": "open/m", "messages": [{"role": "user", "content": "x"}]})
        if status != 200 or json.loads(out) != canned:
            raise AssertionError(f"{status} {out[:120]}")
        if oai.requests[-1]["headers"].get("authorization") != "Bearer sk-oai-selftest":
            raise AssertionError("key not forwarded by today's gateway")
        rows = g.rows()
        if len(rows) != 1 or rows[0]["status"] != 200 or rows[0]["response_id"] != "chatcmpl-st" or set(rows[0]) != lib.TODAY_ROW_KEYS:
            raise AssertionError(f"row {rows and sorted(set(rows[0]) ^ lib.TODAY_ROW_KEYS)}")
        status, _, out = g.call({"model": "nowhere", "messages": []})
        if status != 403 or "no-route" not in out.decode():
            raise AssertionError(f"unrouted: {status} {out[:120]}")
        anth.set((429, {"retry-after": "1"}, {"e": 1}))
        c = lib.http.client.HTTPConnection("127.0.0.1", anth.port)
        c.request("POST", "/v1/messages", body=b"{}", headers={"Content-Type": "application/json"})
        r = c.getresponse()
        if r.status != 429 or r.getheader("retry-after") != "1" or anth.requests[-1]["body"] != b"{}":
            raise AssertionError("fake upstream")
        return f"mTLS + SPIFFE client cert → today's gateway (cedarpy={g.cedarpy}) forwards, writes 1 row of {len(lib.TODAY_ROW_KEYS)} keys, denies no-route; fakes record"

    def builders():
        ex, lb, fl, items = lib.closed_dataset(ctx.tmp, "st_ds", mark="items")
        T = lib.train_lib()
        for it in items:
            if it["policy_model"] != lib.CLOSED or T.item_id(it) != it["item_id"]:
                raise AssertionError("closed item")
        man = lib.read_json(os.path.join(ex, "manifest.json"))
        if man["dataset_sha256"] != T.dataset_sha(sorted(items, key=lambda i: (i["run"], i["principal"]))):
            raise AssertionError("manifest sha")
        rows = lib.read_jsonl(os.path.join(lb, "labels.jsonl"))
        if len(rows) != len(items) or any(r["content_sha256"] != T.content_sha(next(i for i in items if i["run"] == r["run"])) for r in rows):
            raise AssertionError("label rows")
        p = os.path.join(ctx.scratch("st_same"), "f.json")
        with open(p, "w") as f:
            f.write('{"a": 12, "b": 7}\n')
        lib.same_size_replace(p, '"a": 12', '"a": 21')
        if lib.read_json(p) != {"a": 21, "b": 7}:
            raise AssertionError("same_size_replace")
        return f"{len(items)} closed items with consistent manifest and label rows; same-size replace keeps size and mtime"

    def m1_relabel():
        CM = []
        for n in ("p11-heldout", "p12-heldout-base"):
            r = _real(n)
            CM.append(lib.synthetic_matrix(r, f"{n}-as-closed", lib.CLOSED, {t["task"]: (t["n"], t["c"]) for t in r["m11"]["tasks"]}, basis="tokens"))
        if any(m["inputs"]["policy_route"]["kind"] != "anthropic" for m in CM):
            raise AssertionError("relabelled copies lack the closed route")
        a = lib.m15_oracle([_real("p12-heldout"), _real("p12-heldout-r2")], CM)
        b = lib.m15_oracle([_real("p12-heldout"), _real("p12-heldout-r2")], [_real("p11-heldout"), _real("p12-heldout-base")])
        bad = lib.diff(a["arms"]["closed"]["per_task"], b["arms"]["closed"]["per_task"]) + lib.diff(a["diff_pass4"], b["diff_pass4"]) + lib.diff(a["x"], b["x"])
        for k in ("pass1", "pass1_ci95", "pass4", "pass4_ci95"):
            bad += lib.diff(a["arms"]["closed"]["pooled"][k], b["arms"]["closed"]["pooled"][k], k)
        if bad:
            raise AssertionError(bad[:3])
        legacy = [n for n in lib.REAL_MATRICES if "policy_route" in _real(n)["inputs"]]
        if legacy:
            raise AssertionError(f"committed sessions already carry policy_route: {legacy}")
        return "closed-relabelled base copies give the base's per-task (n, c), pooled pass^k, intervals, diff and X; committed sessions carry no policy_route"

    def provenance_builders():
        d = ctx.scratch("st_prov")
        LL = lib.label_lib()
        env = LL.build_env(d, "env")
        run, vm = lib.add_closed_run(env)
        ok, why = LL.L.validate_store(env, vm)
        if not ok:
            raise AssertionError(f"cloned closed run fails POC 6 validate/verify: {why}")
        models = {r["model"] for r in lib.read_jsonl(os.path.join(env.store, "vm", vm, "gateway.jsonl")) if r["kind"] == "llm.call"}
        if models != {lib.CLOSED} or LL.L.witness_models(env.witness(run)) != {lib.CLOSED}:
            raise AssertionError(f"closed run models {models} / witness {LL.L.witness_models(env.witness(run))}")
        f = lib.FakeUpstream()
        f.set((200, {}, lib.vllm_models("m", "/r")))
        c = lib.http.client.HTTPConnection("127.0.0.1", f.port)
        c.request("GET", "/v1/models")
        j = json.loads(c.getresponse().read())
        f.close()
        if j["data"][0]["root"] != "/r" or f.requests[-1]["method"] != "GET":
            raise AssertionError("fake vLLM models")
        k = ctx.fake((200, {}, lib.vllm_models("m", "/r")), key="sk-x")
        codes = []
        for h in ({}, {"Authorization": "Bearer sk-y"}, {"Authorization": "Bearer sk-x"}):
            c = lib.http.client.HTTPConnection("127.0.0.1", k.port)
            c.request("GET", "/v1/models", headers=h)
            r = c.getresponse()
            r.read()
            codes.append(r.status)
        if codes != [401, 401, 200]:
            raise AssertionError(f"keyed fake answered {codes}, want 401/401/200 like the real upstream")
        fdir, _, runs = lib.closed_fleet(ctx.tmp, "st_think", _closed_plan(), seed=31,
                                         rows_patch=lambda run, i, r: r["usage"].__setitem__("thinking_tokens", None) if r["status"] == 200 else None)
        w = lib.closed_m12_oracle(fdir, lib.read_json(lib.PRICES))
        if w["usage"]["thinking_tokens"] != 0 or w["thinking_tokens_missing"] != w["ok"]:
            raise AssertionError(w)
        return f"closed clone {run} passes POC 6 validate/verify with gateway + witness on {lib.CLOSED}; fake vLLM /v1/models; thinking-null oracle"

    for name, fn in (("m1_relabel", m1_relabel), ("provenance_builders", provenance_builders), ("fixtures", manifest), ("pooled_json", pooled), ("open_m12_recompute", open_recompute), ("closed_m12_oracle", closed_oracle),
                     ("ratio_bootstrap", ratio_math), ("gateway_harness", harness), ("dataset_builders", builders)):
        t(name, fn)
    ok = True
    for good, name, msg in results:
        print(f"{'PASS' if good else 'FAIL'} selftest.{name}  {msg}")
        ok &= good
    return ok


# ====================================================================== main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="every local check (the default)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--live", action="store_true", help="+ one real exchange with Anthropic (ANTHROPIC_API_KEY)")
    ap.add_argument("--host", action="store_true", help=f"+ the fleet host {HOST} over ssh")
    ap.add_argument("--only", default="")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--write-manifest", action="store_true", help="(tester only) rewrite fixtures/MANIFEST.sha256")
    a = ap.parse_args()
    if a.write_manifest:
        lib.write_fixture_manifest()
        print(open(lib.FIX_MANIFEST).read())
        return 0
    ctx = Ctx()
    failed = 0
    try:
        if a.selftest:
            ok = selftest(ctx)
            return 0 if ok else 1
        drift = lib.fixture_drift()
        if drift:
            print(f"FAIL fixtures  {len(drift)} hashed inputs changed: {drift[:3]}")
            return 1
        print(f"PASS fixtures  {sum(1 for l in open(lib.FIX_MANIFEST) if l.strip())} hashed inputs unchanged", flush=True)
        for cid, target, live, host, fn in CHECKS:
            if a.only and not cid.startswith(a.only):
                continue
            if live and not a.live:
                print(f"SKIP {cid}  [{target}]  needs --live", flush=True)
                continue
            if host and not a.host:
                print(f"SKIP {cid}  [{target}]  needs --host", flush=True)
                continue
            t0 = time.time()
            try:
                msg = fn(ctx)
                print(f"PASS {cid}  [{target}]  {msg}  ({time.time() - t0:.1f}s)", flush=True)
            except lib.NotImplementedCLI as e:
                failed += 1
                print(f"FAIL {cid}  [{target}]  not implemented: {e}", flush=True)
            except lib.SkipCheck as e:
                print(f"SKIP {cid}  [{target}]  {e}", flush=True)
            except AssertionError as e:
                failed += 1
                print(f"FAIL {cid}  [{target}]  {lib.short(e, 900)}", flush=True)
            except Exception as e:  # a bug in the suite or an unexpected environment: reported, never a bare traceback
                failed += 1
                tb = traceback.format_exc().strip().splitlines()[-1]
                print(f"FAIL {cid}  [{target}]  suite error {type(e).__name__}: {lib.short(e, 300)} ({tb})", flush=True)
            finally:
                if host:
                    ctx.host_cleanup()
        return 1 if failed else 0
    finally:
        ctx.close()
        if a.host:
            ctx.host_cleanup()
        if a.keep or failed:
            print(f"scratch kept: {ctx.tmp}", file=sys.stderr)
        else:
            shutil.rmtree(ctx.tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
