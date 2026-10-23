"""Independent oracles and fixture builders for the POC 8 acceptance suite (retrain/CONTRACT.md).

Nothing here knows how the solver works. Every function reads raw fixtures, runs a contract CLI as a black box,
or computes an *expected* answer from the raw data (store streams, result.json, the split file, the pinned τ²
tool schemas and prompt template). Python 3.10+ standard library only.
"""
import copy
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
FIX = os.path.join(HERE, "fixtures")
HOSTFIX = os.path.join(FIX, "host")
RETRAIN = os.path.join(ROOT, "retrain", "bin", "retrain")
AVOS = os.path.join(ROOT, "collect", "bin", "avos")
TRACE_BIN = os.path.join(ROOT, "trace", "bin")
SPLIT_REL = os.path.join("data", "splits", "retail_user_disjoint_v1.json")
SPLIT = os.path.join(ROOT, SPLIT_REL)
TOOLS_FIX = os.path.join(FIX, "tau2_retail_tools.json")
PROMPT_FIX = os.path.join(FIX, "tau2_llm_agent_prompt_template.json")

sys.path.insert(0, os.path.join(ROOT, "trace"))
import lib_trace as T


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T6 = _load("poc6_testlib", os.path.join(ROOT, "trace", "tests", "lib.py"))

TD = "spiffe://avoslab.com"
STORE_RUNS = ["run_retail-0_11", "run_retail-0_12", "run_retail-0_13", "run_retail-0_14", "run_retail-0_15"]
NATIVE_RUNS = ["run_retail-0_16", "run_retail-0_17", "run_retail-0_18", "run_retail-1_4"]
FIX_MANIFEST = os.path.join(FIX, "MANIFEST.sha256")
CHAT_ROLES = ("system", "user", "assistant", "tool")
TREE_VM = "vm-01-6cc405"
GREETING = "Hi! How can I help you today?"


class NotImplementedCLI(Exception):
    """A contract path is missing: the check reports FAIL 'not implemented: <path>'."""


def _run(cmd, env=None, timeout=900, rss=False, cwd=None):
    e = dict(os.environ)
    e.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if env:
        e.update(env)
    stats = None
    if rss:
        stats = os.path.join(e.get("TMPDIR", "/tmp"), f".rss.{os.getpid()}.{time.time_ns()}.json")
        wrapper = ("import json,resource,subprocess,sys;r=subprocess.run(sys.argv[2:]);"
                   "json.dump({'maxrss':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss},open(sys.argv[1],'w'));"
                   "sys.exit(r.returncode)")
        cmd = [sys.executable, "-c", wrapper, stats, *cmd]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, env=e, timeout=timeout, cwd=cwd or ROOT)
    res = {"rc": p.returncode, "out": p.stdout, "err": p.stderr, "secs": time.perf_counter() - t0, "maxrss_mib": None}
    if stats and os.path.exists(stats):
        raw = json.load(open(stats))["maxrss"]
        os.unlink(stats)
        res["maxrss_mib"] = raw / 2**20 if sys.platform == "darwin" else raw / 1024
    return res


def retrain(*args, env=None, timeout=900, rss=False):
    if not os.path.exists(RETRAIN):
        raise NotImplementedCLI(os.path.relpath(RETRAIN, ROOT))
    return _run([sys.executable, RETRAIN, *map(str, args)], env=env, timeout=timeout, rss=rss)


def avos(*args, env=None):
    return _run([sys.executable, AVOS, *map(str, args)], env=env)


def trace_tool(name, *args):
    return _run([sys.executable, os.path.join(TRACE_BIN, name), *map(str, args)])


def no_traceback(res):
    return "Traceback" not in (res["err"] or "")


def fixture_drift():
    """Files under fixtures/ whose SHA-256 differs from MANIFEST.sha256 (or are missing / unlisted). The suite's
    inputs are frozen by the tester; any drift fails every run loudly."""
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


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_snapshot(root):
    """{relative path: (size, sha256)} of every file under root (roots must be byte-identical after any command)."""
    snap = {}
    for d, _, fs in os.walk(root):
        for n in fs:
            p = os.path.join(d, n)
            snap[os.path.relpath(p, root)] = (os.path.getsize(p), sha_file(p))
    return snap


def prefix_broken(snap):
    """POC 7 append-only rule: each file of an earlier {path: (size, sha)} snapshot still starts with those bytes."""
    bad = []
    for p, (n, sha) in snap.items():
        try:
            h, left = hashlib.sha256(), n
            with open(p, "rb") as f:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    h.update(chunk)
                    left -= len(chunk)
            if left > 0 or h.hexdigest() != sha:
                bad.append(p)
        except OSError:
            bad.append(p)
    return bad


def same_size_replace(path, old, new, count=-1):
    """Replace text in a file keeping its byte size and mtime (what a derived-state fingerprint on size+mtime sees)."""
    assert len(old.encode()) == len(new.encode()), (old, new)
    st = os.stat(path)
    data = open(path, "rb").read()
    assert old.encode() in data, f"{old!r} not in {path}"
    data = data.replace(old.encode(), new.encode(), count)
    with open(path, "r+b") as f:
        f.seek(0)
        f.write(data)
        f.truncate()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return path


def tamper_dir(d, mode, rnd):
    """Damage every regular file under d: truncate to half, flip a byte, or swap contents pairwise."""
    files = sorted(os.path.join(a, n) for a, _, fs in os.walk(d) for n in fs)
    if mode == "truncate":
        for p in files:
            n = os.path.getsize(p)
            with open(p, "r+b") as f:
                f.truncate(n // 2)
    elif mode == "flip":
        for p in files:
            data = bytearray(open(p, "rb").read())
            if data:
                i = rnd.randrange(len(data))
                data[i] ^= 0x55
                open(p, "wb").write(bytes(data))
    elif mode == "swap":
        for a, b in zip(files[0::2], files[1::2]):
            da, db = open(a, "rb").read(), open(b, "rb").read()
            open(a, "wb").write(db)
            open(b, "wb").write(da)
    return len(files)


class Env:
    """A private copy of the fixtures laid out as the contract expects: a POC 7 store root, a results root, a
    legacy ledger, the pinned tool schemas, and private TMPDIR/HOME/cache directories."""

    def __init__(self, tmp, name="env"):
        self.dir = os.path.join(tmp, name)
        self.store = os.path.join(self.dir, "store")
        self.results = os.path.join(self.dir, "results")
        self.ledger = os.path.join(self.dir, "ledger.jsonl")
        self.cache = os.path.join(self.dir, "cache")
        self.tmpdir = os.path.join(self.dir, "tmp")
        self.home = os.path.join(self.dir, "home")
        self.outs = os.path.join(self.dir, "out")
        shutil.rmtree(self.dir, ignore_errors=True)
        for d in (self.cache, self.tmpdir, self.home, self.outs):
            os.makedirs(d, exist_ok=True)
        shutil.copytree(os.path.join(HOSTFIX, "store"), self.store)
        shutil.copytree(os.path.join(HOSTFIX, "results"), self.results)
        for src in (os.path.join(ROOT, "data", "poc2"), os.path.join(ROOT, "data", "poc6")):
            for r in sorted(os.listdir(src)):
                if r.startswith("run_") and not os.path.exists(os.path.join(self.results, r)):
                    shutil.copytree(os.path.join(src, r), os.path.join(self.results, r))
        with open(self.ledger, "w") as out:
            out.write(open(os.path.join(HOSTFIX, "ledger", "legacy_slice.jsonl")).read())
            out.write(open(os.path.join(ROOT, "data", "poc6", "run_retail-0_9", "events.jsonl")).read())
        self.n_out = 0

    def env(self, **extra):
        e = {"AVOS_TRACE_ROOT": self.store, "AVOS_RESULTS_ROOT": self.results, "AVOS_LEDGER": self.ledger,
             "AVOS_TOOL_SCHEMAS": TOOLS_FIX, "AVOS_RETRAIN_CACHE": self.cache, "TMPDIR": self.tmpdir,
             "HOME": self.home, "XDG_CACHE_HOME": os.path.join(self.home, ".cache"),
             "XDG_DATA_HOME": os.path.join(self.home, ".local", "share"),
             "XDG_STATE_HOME": os.path.join(self.home, ".local", "state"),
             "XDG_CONFIG_HOME": os.path.join(self.home, ".config"), "PYTHONPATH": HERE}
        e.update(extra)
        return e

    def out(self, name=None):
        self.n_out += 1
        p = os.path.join(self.outs, name or f"ds{self.n_out}")
        return p

    def clear_caches(self):
        for d in (self.cache, self.tmpdir, self.home):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)

    def cache_dirs(self):
        return [self.cache, self.tmpdir, self.home]

    def roots_snapshot(self):
        return {"store": tree_snapshot(self.store), "results": tree_snapshot(self.results),
                "ledger": (os.path.getsize(self.ledger), sha_file(self.ledger))}

    def vm_of_run(self, run):
        if run.startswith("vm-"):
            return run
        return open(os.path.join(self.store, "runs", run)).read().strip()

    def vm_files(self, vm):
        d = os.path.join(self.store, "vm", vm)
        return [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".jsonl")]

    def store_events(self, vm):
        evs = []
        for p in self.vm_files(vm):
            evs.extend(read_jsonl(p))
        evs.sort(key=lambda e: (e["ts"], e["writer"], e["seq"]))
        return evs

    def all_store_ids(self):
        ids = {}
        vmdir = os.path.join(self.store, "vm")
        for vm in os.listdir(vmdir):
            for e in self.store_events(vm):
                ids[e["event_id"]] = e["hash"]
        return ids

    def witness(self, run):
        p = os.path.join(self.results, run, "result.json")
        return json.load(open(p)) if os.path.exists(p) else None

    def job(self, run):
        p = os.path.join(self.results, run, "job.json")
        return json.load(open(p)) if os.path.exists(p) else None

    def sim(self, run):
        w = self.witness(run)
        return w["simulations"][0] if w and "simulations" in w else None


def _rechain(files_rows):
    """files_rows: [(path, rows)] in store order; re-mint ids from (ts, writer, seq), fix refs, chain per writer."""
    idmap = {}
    for _, rows in files_rows:
        for r in rows:
            new = T.mint_id(r["ts"], r["writer"], r["seq"])
            idmap[r["event_id"]] = new
            r["event_id"] = new
    for _, rows in files_rows:
        for r in rows:
            for k in ("authz_ref", "call_ref"):
                if r.get(k) in idmap:
                    r[k] = idmap[r[k]]
        T6.chain(rows)
    return idmap


def clone_vm(env, src_vm, new_vm, run=None, new_run=None, task=None, counters="copy"):
    """Copy a VM's streams under a new VM id (text-level rename of the id everywhere, then re-mint ids and re-chain).
    Optionally rename the run and the task. Returns the new events in (ts, writer, seq) order."""
    src = os.path.join(env.store, "vm", src_vm)
    dst = os.path.join(env.store, "vm", new_vm)
    os.makedirs(dst, exist_ok=True)
    files_rows = []
    for n in sorted(os.listdir(src)):
        if not n.endswith(".jsonl"):
            continue
        rows = []
        for line in open(os.path.join(src, n), encoding="utf-8"):
            if not line.strip():
                continue
            line = line.replace(src_vm, new_vm)
            r = json.loads(line)
            if run and r.get("run") == run:
                r["run"] = new_run
            if task and r["kind"] == "session.open":
                r["task"] = task
            rows.append(r)
        files_rows.append((os.path.join(dst, n), rows))
    _rechain(files_rows)
    for path, rows in files_rows:
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    cnt = os.path.join(env.store, "counters", src_vm)
    if counters == "copy" and os.path.exists(cnt):
        shutil.copy(cnt, os.path.join(env.store, "counters", new_vm))
    elif counters == "truncate" and os.path.exists(cnt):
        data = open(cnt, "rb").read()
        open(os.path.join(env.store, "counters", new_vm), "wb").write(data[:-2])
    if new_run:
        open(os.path.join(env.store, "runs", new_run), "w").write(new_vm)
    evs = [r for _, rows in files_rows for r in rows]
    evs.sort(key=lambda e: (e["ts"], e["writer"], e["seq"]))
    return evs


def clone_run(env, src_run, new_run, new_vm, task_id=None, reward_info="keep", tamper_usage=False,
              stale_copy=False, counters="copy", mutate=None):
    """A new, POC 6-valid run in the store and the results root, derived from a real one. `mutate(sim)` edits the
    witness simulation in place (class A tampering: text, ids, arguments, policy, raw ids), token counts untouched."""
    src_vm = env.vm_of_run(src_run)
    task = f"retail/{task_id}" if task_id is not None else None
    evs = clone_vm(env, src_vm, new_vm, run=src_run, new_run=new_run, task=task, counters=counters)
    sdir, ddir = os.path.join(env.results, src_run), os.path.join(env.results, new_run)
    os.makedirs(ddir, exist_ok=True)
    job = json.load(open(os.path.join(sdir, "job.json")))
    job.update({"run": new_run, "vm_id": new_vm, "vm_spiffe_id": f"{TD}/vm/{new_vm}",
                "agent_spiffe_id": job["agent_spiffe_id"].replace(src_vm, new_vm)})
    if task:
        job["task"] = task
    res = json.load(open(os.path.join(sdir, "result.json")))
    sim = res["simulations"][0]
    if task_id is not None:
        sim["task_id"] = str(task_id)
        for t in res.get("tasks") or []:
            t["id"] = str(task_id)
    if reward_info != "keep":
        sim["reward_info"] = reward_info
        job["reward"] = None if reward_info is None else reward_info.get("reward")
    if tamper_usage:
        m = next(m for m in sim["messages"] if m["role"] == "assistant" and m.get("usage"))
        m["usage"]["prompt_tokens"] += 1
    if mutate:
        mutate(sim)
    json.dump(job, open(os.path.join(ddir, "job.json"), "w"), indent=1)
    json.dump(res, open(os.path.join(ddir, "result.json"), "w"))
    if stale_copy and os.path.exists(os.path.join(sdir, "events.jsonl")):
        shutil.copy(os.path.join(sdir, "events.jsonl"), os.path.join(ddir, "events.jsonl"))
    return evs


def validate_store(env, vm):
    """The cloned VM's concatenated streams pass POC 6 validate and verify (sanity of the fixture builder)."""
    cat = os.path.join(env.dir, f"cat-{vm}.jsonl")
    with open(cat, "w") as out:
        for p in env.vm_files(vm):
            out.write(open(p).read())
    v = trace_tool("validate", cat)
    w = trace_tool("verify", cat)
    return v["rc"] == 0 and w["rc"] == 0, (v["out"][:200], w["out"][:200])


def inject_call(env, vm, model, prompt_tokens=1234, completion_tokens=7, response_id="chatcmpl-judge-0001"):
    """Append one authz.decision + llm.call of the VM's harness agent to `model` after the last call (what τ²'s
    NL-assertion judge leaves on the host), keeping M8 complete: attempt numbers, agent.exit/vm.exit guest counts
    and the counters file follow. Streams are re-chained. Returns the new llm.call's event_id."""
    gw = os.path.join(env.store, "vm", vm, "gateway.jsonl")
    rg = os.path.join(env.store, "vm", vm, "registrar.jsonl")
    g, r = read_jsonl(gw), read_jsonl(rg)
    last = max((e for e in g if e["kind"] == "llm.call"), key=lambda e: e["seq"])
    agent = last["principal"]
    attempt = max(e.get("attempt", 0) for e in g if e["principal"] == agent) + 1
    ex = next(e for e in r if e["kind"] == "agent.exit" and e["principal"] == agent)
    ts_ms = T.ts_ms(ex["ts"]) - 20
    seq = max(e["seq"] for e in g)
    dec = {"event_id": "x", "kind": "authz.decision", "ts": T.ms_ts(ts_ms - 600), "writer": last["writer"], "seq": seq + 1,
           "host": last["host"], "principal": agent, "vm_id": vm, "action": "llm.call", "resource": f'Model::"{model}"',
           "args": {}, "decision": "allow", "policy_ids": ["llm-all"], "errors": [], "attempt": attempt}
    call = {"event_id": "y", "kind": "llm.call", "ts": T.ms_ts(ts_ms), "writer": last["writer"], "seq": seq + 2, "host": last["host"],
            "principal": agent, "vm_id": vm, "authz_ref": "x", "model": model, "status": 200, "ms": 580,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "dropped": False, "attempt": attempt,
            "response_id": response_id, "system_sha256": "0" * 64, "tools_sha256": None, "messages_sha256": ["0" * 64],
            "output_sha256": "1" * 64}
    g += [dec, call]
    if isinstance(ex.get("attempts"), int):
        ex["attempts"] += 1
    vx = next((e for e in r if e["kind"] == "vm.exit"), None)
    if vx and (vx.get("guest") or {}).get("attempts", {}).get(agent) is not None:
        vx["guest"]["attempts"][agent] += 1
    files = [(gw, g), (rg, r)]
    _rechain(files)
    for path, rows in files:
        with open(path, "w", encoding="utf-8") as f:
            for e in rows:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    cnt = os.path.join(env.store, "counters", vm)
    if os.path.exists(cnt):
        open(cnt, "ab").write(b"al")
    return call["event_id"]


def witness_models(result):
    info = result.get("info") or {}
    out = set()
    for k in ("agent_info", "user_info"):
        llm = (info.get(k) or {}).get("llm")
        if isinstance(llm, str):
            out.add(llm.split("/", 1)[1] if llm.startswith("openai/") else llm)
    return out


def chat_message(m):
    """A τ² witness message as the Chat Completions message the item must carry (no system prompt here)."""
    if m["role"] == "tool":
        return {"role": "tool", "tool_call_id": m["id"], "content": m.get("content")}
    d = {"role": m["role"], "content": m.get("content")}
    if m.get("tool_calls"):
        d["tool_calls"] = [{"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])}}
                           for t in m["tool_calls"]]
    return d


def witness_verdict(result, calls, tools, system_prompt):
    """CONTRACT §4 computed independently: (pairs [(witness index, call)], problems, join, other_calls).
    `calls` = the principal's successful llm.calls in host order; `tools`/`system_prompt` = what the item must carry."""
    sim = result["simulations"][0]
    named = witness_models(result)
    part = [c for c in calls if c["model"] in named] if named else list(calls)
    other = [c for c in calls if c not in part]
    msgs = sim["messages"]
    usage_msgs = [(i, m) for i, m in enumerate(msgs) if m.get("usage")]
    problems, pairs = [], []
    by_id = {c.get("response_id"): c for c in part if c.get("response_id")}
    if part and all(c.get("response_id") for c in part):
        join = "response_id"
        order = []
        for i, m in usage_msgs:
            rid = (m.get("raw_data") or {}).get("id")
            c = by_id.get(rid)
            if c is None:
                problems.append(f"message {i}: no stored llm.call with response_id {rid!r}")
                continue
            if (c["prompt_tokens"], c["completion_tokens"]) != (m["usage"]["prompt_tokens"], m["usage"]["completion_tokens"]):
                problems.append(f"message {i}: token counts differ from call {c['event_id']}")
            pairs.append((i, c))
            order.append(part.index(c))
        if order != sorted(order):
            problems.append("joined calls are not in the witness's order")
        if len(set(c["event_id"] for _, c in pairs)) != len(part):
            problems.append(f"{len(part) - len(set(c['event_id'] for _, c in pairs))} stored llm.calls joined no witness message")
    else:
        join = "tokens"
        pairs, problems = match_witness(msgs, part)
    sys_sha, tools_sha = T.content_sha(system_prompt), T.content_sha(tools)
    conv = [{"role": "system", "content": system_prompt}]
    pair_at = {i: c for i, c in pairs}
    for i, m in enumerate(msgs):
        c = pair_at.get(i)
        if c is not None and c.get("output_sha256"):
            if m["role"] == "assistant":
                if c.get("system_sha256") != sys_sha:
                    problems.append(f"message {i}: system prompt differs from what the host saw")
                if c.get("tools_sha256") != tools_sha:
                    problems.append(f"message {i}: tools differ from what the host saw")
                want = [T.content_sha(T.chat_form(x)) for x in conv]
                if c.get("messages_sha256") != want:
                    problems.append(f"message {i}: prompt messages differ from what the host saw")
            else:
                if c.get("system_sha256") == sys_sha or c.get("tools_sha256") == tools_sha:
                    problems.append(f"message {i}: a user turn was sent the agent's prompt")
            if T.content_sha(T.chat_form(m, "assistant")) != c["output_sha256"]:
                problems.append(f"message {i}: text differs from what the host saw")
        conv.append(chat_message(m))
    return pairs, problems, join, other


def hashed(calls):
    return bool(calls) and all(c.get("output_sha256") and c.get("response_id") for c in calls)


def mut_reply_text(sim):
    m = next(m for m in sim["messages"] if m["role"] == "assistant" and m.get("usage") and m.get("content"))
    m["content"] = m["content"] + " Thanks!"


def mut_user_text(sim):
    m = next(m for m in sim["messages"] if m["role"] == "user" and m.get("usage") and m.get("content"))
    m["content"] = m["content"].replace("I", "We", 1) if "I" in m["content"] else m["content"] + "!"


def mut_tool_args(sim):
    m = next(m for m in sim["messages"] if m.get("tool_calls"))
    t = m["tool_calls"][0]
    k = next(iter(t["arguments"]))
    v = t["arguments"][k]
    t["arguments"][k] = (v + "x") if isinstance(v, str) else v


def mut_tool_id(sim):
    m = next(m for m in sim["messages"] if m.get("tool_calls"))
    old = m["tool_calls"][0]["id"]
    new = old[:-4] + "beef"
    m["tool_calls"][0]["id"] = new
    for x in sim["messages"]:
        if x["role"] == "tool" and x.get("id") == old:
            x["id"] = new


def mut_policy(sim):
    sim["policy"] = sim["policy"].replace("one tool call at a time", "two tool calls at a time")


def mut_swap_raw_ids(sim):
    a = [m for m in sim["messages"] if m["role"] == "assistant" and m.get("usage")]
    a[0]["raw_data"]["id"], a[1]["raw_data"]["id"] = a[1]["raw_data"]["id"], a[0]["raw_data"]["id"]


def mut_tool_result(sim):
    m = next(m for m in sim["messages"] if m["role"] == "tool")
    m["content"] = (m.get("content") or "") + " "


def agent_calls(evs, principal):
    return [e for e in evs if e["kind"] == "llm.call" and e["principal"] == principal
            and e.get("status") == 200 and not e.get("dropped")]


def match_witness(sim_messages, calls):
    """Contract §4: usage messages matched in order to the next llm.call with the same token counts; agrees only
    when both sides are fully matched. Returns (pairs [(msg index, call)], problems)."""
    pairs, problems, ci = [], [], 0
    for i, m in enumerate(sim_messages):
        u = m.get("usage")
        if not u:
            continue
        want = (u["prompt_tokens"], u["completion_tokens"])
        j = ci
        while j < len(calls) and (calls[j]["prompt_tokens"], calls[j]["completion_tokens"]) != want:
            j += 1
        if j >= len(calls):
            problems.append(f"message {i} usage {want} matches no stored llm.call")
            continue
        for k in range(ci, j):
            problems.append(f"llm.call {calls[k]['event_id']} matches no witness message")
        pairs.append((i, calls[j]))
        ci = j + 1
    for k in range(ci, len(calls)):
        problems.append(f"llm.call {calls[k]['event_id']} matches no witness message")
    return pairs, problems


def system_prompt_for(sim):
    t = json.load(open(PROMPT_FIX))
    return t["SYSTEM_PROMPT"].format(domain_policy=sim["policy"], agent_instruction=t["AGENT_INSTRUCTION"])


def tau2_norm(sim):
    return T6.tau2_norm_messages(sim)


def item_norm(item, drop_system=True):
    ms = [m for m in item["messages"] if not (drop_system and m["role"] == "system")]
    return T6.chat_norm_messages({"messages": ms})


def first_diff(a, b):
    return T6.first_diff(a, b)


def expected_train_flags(sim, pairs):
    """Per witness message index: train iff assistant with usage matched to a stored call."""
    matched = {i for i, _ in pairs}
    return [m["role"] == "assistant" and bool(m.get("usage")) and i in matched for i, m in enumerate(sim["messages"])]


def split_of(task_id, split_path=SPLIT):
    s = json.load(open(split_path))
    if task_id in s["train"]:
        return "train"
    if task_id in s["held_out"]:
        return "held_out"
    return None


def task_id_of(task):
    return task.split("/", 1)[1] if task and task.startswith("retail/") else None


def passed_of_sim(sim):
    ri = sim.get("reward_info")
    if not ri or ri.get("reward") is None:
        return None
    return ri["reward"] == 1.0


def m14_oracle(agent_items):
    train = [i for i in agent_items if i["split"] == "train"]
    k = sum(1 for i in train if i["outcome"]["passed"] is True)
    return {"train_runs": len(train), "train_passed": k, "kept_run_fraction": (k / len(train)) if train else None}


def canon_item_id(item):
    d = {k: v for k, v in item.items() if k != "item_id"}
    return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


ROLES = {"system", "user", "assistant", "tool"}
STATUSES = {"complete", "incomplete", "unverified"}


def item_problems(it):
    """Shape of one item against CONTRACT §5 (types and required keys only; values are checked elsewhere)."""
    p = []

    def need(k, types, where=it, name=""):
        if k not in where:
            p.append(f"missing {name}{k}")
        elif not isinstance(where[k], types) or (bool in (types if isinstance(types, tuple) else (types,)) is False and isinstance(where[k], bool) and types is not bool):
            p.append(f"{name}{k} has type {type(where[k]).__name__}")
    for k in ("item_id", "run", "principal", "vm_id", "harness", "task", "evidence"):
        need(k, str)
    if it.get("evidence") not in ("store", "ledger", "witness"):
        p.append(f"evidence={it.get('evidence')!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", it.get("item_id") or ""):
        p.append("item_id not 64 hex")
    elif canon_item_id(it) != it["item_id"]:
        p.append("item_id is not the SHA-256 of the canonical item")
    need("parent", (str, type(None)))
    need("task_id", (str, type(None)))
    need("messages", list)
    need("tools", list)
    for sect, keys in (("outcome", ("reward", "passed", "termination_reason", "reward_breakdown", "source")),
                       ("cost", ("usd", "prompt_tokens", "completion_tokens", "llm_calls", "policy_calls", "environment_calls")),
                       ("timestamps", ("start", "end", "wall_ms")), ("trace_complete", ("status", "ratio")),
                       ("witness", ("present", "agrees", "problems", "sha256")),
                       ("lineage", ("session_open", "session_close", "spawn", "exit", "label", "events"))):
        if not isinstance(it.get(sect), dict):
            p.append(f"missing {sect}")
            continue
        for k in keys:
            if k not in it[sect]:
                p.append(f"missing {sect}.{k}")
    if isinstance(it.get("trace_complete"), dict) and it["trace_complete"].get("status") not in STATUSES:
        p.append(f"trace_complete.status={it['trace_complete'].get('status')!r}")
    if "split" not in it or it["split"] not in ("train", "held_out", None):
        p.append(f"split={it.get('split')!r}")
    if "children" not in it or not isinstance(it["children"], list):
        p.append("missing children")
    if "redact" not in it:
        p.append("missing redact")
    for i, m in enumerate(it.get("messages") or []):
        if m.get("role") not in ROLES:
            p.append(f"message {i} role {m.get('role')!r}")
        if not isinstance(m.get("train"), bool):
            p.append(f"message {i} train not bool")
        if "event_id" not in m:
            p.append(f"message {i} without event_id key")
        if m.get("role") == "tool" and not isinstance(m.get("tool_call_id"), str):
            p.append(f"message {i} tool without tool_call_id")
        for t in m.get("tool_calls") or []:
            fn = t.get("function") or {}
            if t.get("type") != "function" or not isinstance(t.get("id"), str) or not isinstance(fn.get("name"), str):
                p.append(f"message {i} malformed tool_call")
            elif not isinstance(fn.get("arguments"), str):
                p.append(f"message {i} tool_call arguments not a JSON string")
            else:
                try:
                    json.loads(fn["arguments"])
                except ValueError:
                    p.append(f"message {i} tool_call arguments not JSON")
        if m.get("train") and m.get("role") != "assistant":
            p.append(f"message {i} train on a {m.get('role')} message")
    for t in it.get("tools") or []:
        fn = (t or {}).get("function") or {}
        if t.get("type") != "function" or not isinstance(fn.get("name"), str) or not isinstance(fn.get("parameters"), dict):
            p.append("malformed tool schema")
            break
    return p


def tools_cover_calls(item):
    """Every tool called in the item has a schema whose parameters.properties contain the argument keys used."""
    by = {t["function"]["name"]: t["function"] for t in item["tools"]}
    bad = []
    for m in item["messages"]:
        for t in m.get("tool_calls") or []:
            name = t["function"]["name"]
            if name not in by:
                bad.append(f"no schema for {name}")
                continue
            props = set((by[name].get("parameters") or {}).get("properties") or {})
            try:
                args = json.loads(t["function"]["arguments"])
            except ValueError:
                continue
            extra = set(args) - props if isinstance(args, dict) else set()
            if extra:
                bad.append(f"{name}: arguments {sorted(extra)} not in schema")
    return bad


def _args_key(a):
    try:
        return json.dumps(json.loads(a), sort_keys=True)
    except (ValueError, TypeError):
        return a


def dup_key(item):
    return json.dumps([(m["role"], m.get("content"),
                        [(t["function"]["name"], _args_key(t["function"]["arguments"])) for t in m.get("tool_calls") or []])
                       for m in item["messages"]], ensure_ascii=True)


def stats_oracle(items):
    n = len(items)
    pt_hist, msg_hist, tools_called = Counter(), Counter(), Counter()
    tool_at, per_item = defaultdict(Counter), Counter()
    pturns = ptc = 0
    groups = defaultdict(list)
    for it in items:
        ms = it["messages"]
        k, seen = 0, set()
        for idx, m in enumerate(ms):
            if m.get("train"):
                k += 1
                tcs = m.get("tool_calls") or []
                if tcs:
                    ptc += 1
                for t in tcs:
                    name = t["function"]["name"]
                    tools_called[name] += 1
                    seen.add((name, idx))
        for name, idx in seen:
            tool_at[name][str(idx)] += 1
            per_item[(name, idx)] += 1
        pturns += k
        pt_hist[str(k)] += 1
        msg_hist[str(len(ms))] += 1
        groups[dup_key(it)].append(it["item_id"])
    shortcut = None
    if per_item:
        top = max(per_item.values())
        cands = sorted(k for k, v in per_item.items() if v == top)
        shortcut = {"tool": cands[0][0], "index": cands[0][1], "share": top / n, "flag": top / n >= 0.9, "_unique": len(cands) == 1}
    dups = sorted(sorted(g) for g in groups.values() if len(g) >= 2)
    return {"items": n, "runs": len({i["run"] for i in items}), "policy_turns_hist": dict(pt_hist),
            "messages_hist": dict(msg_hist), "tool_call_share": (ptc / pturns) if pturns else None,
            "tools_called": dict(tools_called), "tool_at_index": {k: dict(v) for k, v in tool_at.items()},
            "shortcut": shortcut, "exact_duplicates": {"groups": dups, "items": sum(len(g) for g in dups)}}


def stats_diff(want, got):
    """Problems between the oracle's statistics and the tool's (counts exact, shares within 1e-9)."""
    p = []
    for k in ("items", "runs", "policy_turns_hist", "messages_hist", "tools_called", "tool_at_index"):
        if got.get(k) != want[k]:
            p.append(f"{k}: want {json.dumps(want[k])[:120]} got {json.dumps(got.get(k))[:120]}")
    a, b = want["tool_call_share"], got.get("tool_call_share")
    if (a is None) != (b is None) or (a is not None and abs(a - b) > 1e-9):
        p.append(f"tool_call_share: want {a} got {b}")
    ws, gs = want["shortcut"], got.get("shortcut")
    if ws is None:
        if gs is not None:
            p.append("shortcut should be null")
    elif not isinstance(gs, dict):
        p.append("shortcut missing")
    else:
        if abs(ws["share"] - (gs.get("share") or -1)) > 1e-9 or gs.get("flag") != ws["flag"]:
            p.append(f"shortcut share/flag: want {ws['share']}/{ws['flag']} got {gs.get('share')}/{gs.get('flag')}")
        if ws["_unique"] and (gs.get("tool") != ws["tool"] or str(gs.get("index")) != str(ws["index"])):
            p.append(f"shortcut tool/index: want {ws['tool']}@{ws['index']} got {gs.get('tool')}@{gs.get('index')}")
    wd, gd = want["exact_duplicates"], got.get("exact_duplicates") or {}
    if sorted(sorted(g) for g in gd.get("groups") or []) != wd["groups"] or gd.get("items") != wd["items"]:
        p.append(f"exact_duplicates: want {wd['items']} items in {len(wd['groups'])} groups, got {gd.get('items')} in {len(gd.get('groups') or [])}")
    return p


def planted_dataset(path, n=100, shortcut_share=0.98, seed=3, tool="find_user_id_by_email", index=3):
    """A hand-made trajectories.jsonl: n items, shortcut_share of them calling `tool` at message `index`."""
    rnd = random.Random(seed)
    items = []
    others = ["get_order_details", "get_user_details", "cancel_pending_order", "list_all_product_types"]
    for i in range(n):
        ms = [{"role": "system", "content": "policy", "train": False, "event_id": None},
              {"role": "assistant", "content": GREETING, "train": False, "event_id": None},
              {"role": "user", "content": f"hello {i}", "train": False, "event_id": None}]
        first = tool if i < round(n * shortcut_share) else rnd.choice(others)
        ms.append({"role": "assistant", "content": None, "train": True, "event_id": f"e{i}-1",
                   "tool_calls": [{"id": f"c{i}-1", "type": "function", "function": {"name": first, "arguments": json.dumps({"x": i})}}]})
        ms.append({"role": "tool", "tool_call_id": f"c{i}-1", "content": "ok", "train": False, "event_id": None})
        for j in range(rnd.randint(0, 3)):
            t = rnd.choice(others)
            ms.append({"role": "assistant", "content": None, "train": True, "event_id": f"e{i}-{j+2}",
                       "tool_calls": [{"id": f"c{i}-{j+2}", "type": "function", "function": {"name": t, "arguments": "{}"}}]})
            ms.append({"role": "tool", "tool_call_id": f"c{i}-{j+2}", "content": "ok", "train": False, "event_id": None})
        ms.append({"role": "assistant", "content": f"done {i}", "train": True, "event_id": f"e{i}-9"})
        it = {"run": f"run_retail-0_{i}", "principal": f"{TD}/vm/vm-01-{i:06x}/agent/tau2", "messages": ms, "tools": [],
              "split": "train", "outcome": {"passed": True}}
        it["item_id"] = canon_item_id(it)
        items.append(it)
    for k in (1, 2):
        items[k]["messages"] = copy.deepcopy(items[0]["messages"])
        items[k]["item_id"] = canon_item_id(items[k])
    write_jsonl(os.path.join(path, "trajectories.jsonl"), items)
    return items


def in_order(text, needles):
    """Each needle appears, and after the previous one. Returns the first missing needle or None."""
    pos = 0
    for n in needles:
        i = text.find(n, pos)
        if i < 0:
            return n
        pos = i + len(n)
    return None


def viewer_needles(sim, system_prompt, tools):
    """What a labeler must see, in order (CONTRACT §10), from the witness alone."""
    ri = sim.get("reward_info") or {}
    needles = [f"reward={ri.get('reward') if ri.get('reward') is not None else 'unknown'}",
               "passed" if passed_of_sim(sim) else ("failed" if passed_of_sim(sim) is False else "unknown"),
               f"termination_reason={sim.get('termination_reason')}"]
    if ri.get("db_check"):
        needles.append(f"db_match={str(ri['db_check']['db_match']).lower()}")
    for a in ri.get("action_checks") or []:
        needles.append(f"action {a['action']['name']} ")
        needles.append(f"match={str(a['action_match']).lower()}")
    needles.append("tools=")
    needles.append(system_prompt)
    for i, m in enumerate(sim["messages"]):
        if m["role"] == "tool":
            needles.append(f"tool {m['id']}")
            needles.append(m.get("content") or "")
        else:
            needles.append(f"{m['role']} ")
            if m.get("content"):
                needles.append(m["content"])
            for t in m.get("tool_calls") or []:
                needles.append(f"tool_call {t['id']} {t['name']} ")
                for k, v in t["arguments"].items():
                    needles.append(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)
    return needles


def strip_html(s):
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", s))


DIGIT = re.compile(r"\d")


def content_strings(item_or_example):
    """Every content field the hook must have touched: (where, text)."""
    out = []
    msgs = item_or_example.get("messages")
    if msgs is None:
        msgs = list(item_or_example.get("prompt") or []) + list(item_or_example.get("completion") or [])
    for i, m in enumerate(msgs):
        if isinstance(m.get("content"), str):
            out.append((f"message {i} content", m["content"]))
        for t in m.get("tool_calls") or []:
            out.append((f"message {i} arguments", t["function"]["arguments"]))
    return out


def gen_scale(env, n_runs, n_noise_vms, seed=11):
    """n_runs τ² runs cloned round-robin from the five real store runs (12–20 messages, 7–14 calls) and
    n_noise_vms unrelated VMs cloned from the tree VM. Deterministic."""
    rnd = random.Random(seed)
    runs = []
    for i in range(n_runs):
        src = STORE_RUNS[i % len(STORE_RUNS)]
        task_id = str(rnd.choice([0, 1, 2, 3, 4, 10, 11, 12]))
        run = f"run_retail-{task_id}_{1000 + i}"
        clone_run(env, src, run, f"vm-{(i % 90) + 10:02d}-{rnd.getrandbits(24):06x}", task_id=task_id)
        runs.append(run)
    for i in range(n_noise_vms):
        clone_vm(env, TREE_VM, f"vm-{(i % 90) + 10:02d}-{rnd.getrandbits(24):06x}")
    return runs


def percentile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[k]


def short(s, n=160):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"
