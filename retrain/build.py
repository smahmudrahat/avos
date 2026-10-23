"""Training items from the trace store and the harness witness (POC 8). Interface: retrain/CONTRACT.md. Design:
docs/trace.md §10.

The store under $AVOS_TRACE_ROOT is the record; an item is a pure function of one run's files: its VM's streams
(chains verified on every read), its counters, its `result.json` and the pinned τ² metadata. Nothing is cached and
nothing is written anywhere. Message ids are the store's own event ids. Witness content is accepted where a stored
`llm.call` vouches for it: joined by the upstream response id (else by the token-count order of docs/trace.md §4),
and, where the gateway hashed what the model saw and said, only if the witness hashes to the same.
"""
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "collect"))
import store
from store import T


def _load(name, path):
    loader = SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


avos = _load("avos", os.path.join(REPO, "collect", "bin", "avos"))
ingest = _load("ingest", os.path.join(REPO, "trace", "bin", "ingest"))

TD = "spiffe://avoslab.com"
RESULTS = os.environ.get("AVOS_RESULTS_ROOT", "/var/lib/avos/results")
LEDGER = os.environ.get("AVOS_LEDGER") or (store.LEGACY if os.path.exists(store.LEGACY) else None)
PINNED = os.path.join(HERE, "tau2_pinned.json")
DEFAULT_SPLIT = os.path.join("data", "splits", "retail_user_disjoint_v1.json")
VM_ID = re.compile(r'"vm_id":\s*"([^"]+)"')
UNVERIFIED = {"status": "unverified", "ratio": None}


class Skip(Exception):
    """A requested run that cannot be exported; `reason` is the manifest's."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def read_json(path):
    with open(path, "rb") as f:
        return json.loads(f.read())


def imported_lines(match):
    """Lines of the store's imported files that contain `match` (imports span VMs, so they are scanned)."""
    d = f"{store.ROOT}/import"
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if n.endswith(".jsonl"):
            with open(f"{d}/{n}", encoding="utf-8") as f:
                yield from (line for line in f if match in line)


def store_events(vm):
    """The VM's stored events and the rows that break their writer's chain. Each collected stream's chain is
    recomputed on every read: a row edited or removed in place is reported, never silently a different item."""
    evs, broken = [], []
    for path in store.vm_files(vm):
        head = T.GENESIS
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                try:
                    ev = json.loads(line)
                except ValueError:
                    raise Skip("store_unreadable", f"{os.path.basename(path)}:{n} of {vm} is not JSON")
                if ev.get("prev") != head or ev.get("hash") != T.ev_hash(ev):
                    broken.append(f"store: {vm}/{os.path.basename(path)} line {n} ({ev.get('event_id')}) breaks its "
                                  "writer's chain")
                head = ev.get("hash")
                evs.append(ev)
    for line in imported_lines(f'"{vm}"'):
        ev = json.loads(line)
        if ev.get("vm_id") == vm:
            evs.append(ev)
    return [e for e in evs if e.get("vm_id") == vm], broken


class Sources:
    """The three roots as one command sees them: run ids, their VMs, and the legacy ledger rows of those VMs."""

    def __init__(self):
        if os.path.exists(RESULTS) and not os.access(RESULTS, os.R_OK | os.X_OK):
            raise PermissionError(f"results root {RESULTS} is not readable")
        if os.path.exists(store.ROOT) and not os.access(store.ROOT, os.R_OK | os.X_OK):
            raise PermissionError(f"trace root {store.ROOT} is not readable")
        self.dirs = sorted(n for n in (os.listdir(RESULTS) if os.path.isdir(RESULTS) else [])
                           if n.startswith("run_") and store.SAFE.fullmatch(n) and os.path.isdir(f"{RESULTS}/{n}"))
        rd = f"{store.ROOT}/runs"
        self.store_runs = {n: open(f"{rd}/{n}").read().strip() for n in (os.listdir(rd) if os.path.isdir(rd) else [])}
        for line in imported_lines('"session.open"'):
            ev = json.loads(line)
            self.store_runs.setdefault(ev["run"], ev["vm_id"])
        self.jobs = {}
        for n in self.dirs:
            try:
                self.jobs[n] = read_json(f"{RESULTS}/{n}/job.json")
            except (OSError, ValueError):
                self.jobs[n] = {}
        self._ledger = None

    def runs(self):
        return sorted(set(self.dirs) | set(self.store_runs))

    def vm_of(self, run):
        return self.store_runs.get(run) or (self.jobs.get(run) or {}).get("vm_id")

    def ledger(self):
        """{vm_id: [ledger lines]}, one pass over the ledger per command (POC 1-6 rows are history, not appended)."""
        if self._ledger is None:
            self._ledger = {}
            if LEDGER:
                with open(LEDGER, encoding="utf-8") as f:
                    for line in f:
                        m = VM_ID.search(line)
                        if m:
                            self._ledger.setdefault(m[1], []).append(line)
        return self._ledger

    def evidence(self, vm):
        if vm and store.SAFE.fullmatch(vm) and store.vm_files(vm):
            return "store"
        if vm and vm in self.ledger():
            return "ledger"
        return "witness"

    def ledger_events(self, vm):
        """The VM's ledger rows converted exactly as `ingest --ledger` does: ids, seq and links are stamped by this
        conversion of these rows (deterministic for the same ledger), not by any emitter."""
        rows = self.ledger()[vm]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", encoding="utf-8") as src:
            src.writelines(rows)
            src.flush()
            host = ingest.first_host(src.name) or next((json.loads(r).get("host") for r in rows if '"host"' in r), None)
            out = io.StringIO()
            ingest.convert(src.name, ingest.Out(out, host or "ledger"), False, {})
        return [json.loads(x) for x in out.getvalue().splitlines()]


def pinned():
    """Tool schemas ({"<harness>/<domain>": [...]}) and τ²'s llm_agent prompt template at the pinned commit.
    AVOS_TOOL_SCHEMAS replaces the schemas (a bare list stands for tau2/retail, the only domain in use)."""
    p = read_json(PINNED)
    tools = p["tools"]
    if os.environ.get("AVOS_TOOL_SCHEMAS"):
        t = read_json(os.environ["AVOS_TOOL_SCHEMAS"])
        tools = t if isinstance(t, dict) else {"tau2/retail": t}
    return tools, p["llm_agent"]


def locate(src, r):
    """Where a requested run's rows live: {run, vm, job, evidence, witness_path}. Raises Skip("unknown_run")."""
    if not store.SAFE.fullmatch(r or ""):
        raise Skip("unknown_run")
    if r.startswith("vm-"):
        vm = r
        run = next((n for n, v in sorted(src.store_runs.items()) if v == vm), None)
        if run is None:
            run = next((n for n in src.dirs if src.jobs[n].get("vm_id") == vm), r)
    else:
        if r not in src.dirs and r not in src.store_runs:
            raise Skip("unknown_run")
        run, vm = r, src.vm_of(r)
    ev = src.evidence(vm)
    wp = f"{RESULTS}/{run}/result.json" if run in src.dirs else None
    if r.startswith("vm-") and ev == "witness":
        raise Skip("unknown_run")
    return {"run": run, "vm": vm, "job": src.jobs.get(run) or {}, "evidence": ev,
            "witness_path": wp if wp and os.path.exists(wp) else None, "has_dir": run in src.dirs}


def describe(src, r):
    """The `runs` entry."""
    loc = locate(src, r)
    harness, task = loc["job"].get("harness"), loc["job"].get("task")
    witness, reason = False, None
    if loc["witness_path"]:
        try:
            w = read_json(loc["witness_path"])
            witness = isinstance(w, dict)
            harness = harness or ("tau2" if "simulations" in w else "mini-swe-agent" if "trajectory" in w else None)
        except (OSError, ValueError):
            reason = "witness_unreadable"
    if loc["evidence"] == "store" and not (harness and task):
        so = next((e for e in store.vm_events(loc["vm"]) if e["kind"] == "session.open"), {})
        harness, task = harness or so.get("harness"), task or so.get("task")
    if loc["evidence"] == "witness":
        reason = reason or "no_host_rows"
    return {"run": loc["run"], "harness": harness, "task": task, "vm_id": loc["vm"], "evidence": loc["evidence"],
            "witness": witness, "exportable": reason is None, "reason": reason}


def run_items(src, r, tools_by, template, split, redact=None, witness_only=False):
    """(run, {principal: item}, default principal) for one requested run, or Skip."""
    loc = locate(src, r)
    run, vm, job, evidence = loc["run"], loc["vm"], loc["job"], loc["evidence"]
    w = wsha = None
    if loc["witness_path"]:
        try:
            raw = open(loc["witness_path"], "rb").read()
            w, wsha = json.loads(raw), hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError):
            raise Skip("witness_unreadable", loc["witness_path"])
        if not isinstance(w, dict):
            raise Skip("witness_unreadable", loc["witness_path"])
    elif loc["has_dir"] and evidence == "witness":
        raise Skip("witness_unreadable", f"{run} has no result.json")
    if evidence == "witness" and not witness_only:
        raise Skip("no_host_rows")
    evs, broken = store_events(vm) if evidence == "store" else (src.ledger_events(vm) if evidence == "ledger" else [], [])
    evs.sort(key=lambda e: (e["ts"], e["writer"], e["seq"]))
    m8 = avos.m8(vm, evs) if evidence == "store" else UNVERIFIED
    tc = {"status": m8["status"], "ratio": m8["ratio"]}

    by_p, spawn, exit_, boot, vm_exit = {}, {}, {}, None, None
    session = {}
    for e in evs:
        by_p.setdefault(e["principal"], []).append(e)
        k = e["kind"]
        if k == "agent.spawn":
            spawn.setdefault(e["principal"], e)
        elif k == "agent.exit":
            exit_.setdefault(e["principal"], e)
        elif k == "vm.boot":
            boot = boot or e
        elif k == "vm.exit":
            vm_exit = vm_exit or e
        elif k in ("session.open", "session.close", "label"):
            session.setdefault(k, e)
    so = session.get("session.open", {})
    harness = job.get("harness") or so.get("harness") or (
        "tau2" if w and "simulations" in w else "mini-swe-agent" if w and "trajectory" in w else None)
    task = job.get("task") or so.get("task")
    sim = (w.get("simulations") or [None])[0] if w and harness == "tau2" else None
    if not task and sim:
        task = f"{((w.get('info') or {}).get('environment_info') or {}).get('domain_name')}/{sim.get('task_id')}"
    vm_p = f"{TD}/vm/{vm}" if vm else None
    agent = job.get("agent_spiffe_id") or next(
        (p for p, e in spawn.items() if e.get("name") == harness and e.get("parent") == vm_p), None)
    first = next((p for p, _ in sorted(spawn.items(), key=lambda x: (x[1]["ts"], x[1]["writer"], x[1]["seq"]))), None)
    default = agent or first
    principals = {p for p, es in by_p.items() if any(e["kind"] in ("llm.call", "tool.call") for e in es)}
    if default:
        principals.add(default)
    if evidence == "witness":
        default = agent = agent or (f"{vm_p}/agent/{harness}" if vm_p else None)
        principals = {agent}
    domain, _, tid = (task or "").partition("/")
    task_id = tid if harness == "tau2" and domain == "retail" and tid else None
    tools = tools_by.get(f"{harness}/{domain}", []) if harness and task else []
    host = evs[0]["host"] if evs else None

    built = {}
    for p in principals:
        pe = by_p.get(p, [])
        calls = [e for e in pe if e["kind"] == "llm.call"]
        wit = witness_block(False)
        outcome = {"reward": None, "passed": None, "termination_reason": None, "reward_breakdown": None, "source": None}
        roles = {}
        if p == agent and w is not None:
            ok = [c for c in calls if c.get("status") == 200 and not c.get("dropped")]
            try:
                if harness == "tau2":
                    system = template["SYSTEM_PROMPT"].format(domain_policy=sim["policy"],
                                                             agent_instruction=template["AGENT_INSTRUCTION"])
                    info = w.get("info") or {}
                    named = {(info.get(k) or {}).get("llm") for k in ("agent_info", "user_info")} - {None}
                    other = [c for c in ok if named and not any(n == c.get("model") or n.endswith("/" + str(c.get("model")))
                                                                for n in named)]
                    msgs, roles, wit = tau2_messages(sim, [c for c in ok if c not in other], system, tools, evidence)
                    wit["other_calls"] = [{k: c.get(k) for k in ("event_id", "model", "prompt_tokens", "completion_tokens")}
                                          for c in other]
                    ri = sim.get("reward_info")
                    reward = ri.get("reward") if isinstance(ri, dict) else None
                    outcome = {"reward": reward, "passed": None if reward is None else reward == 1.0,
                               "termination_reason": sim.get("termination_reason"), "reward_breakdown": ri, "source": "tau2"}
                elif harness == "mini-swe-agent":
                    msgs, roles, wit = mswe_messages(w, ok, evidence)
                    passed = w.get("tests_passed")
                    outcome = {"reward": None if passed is None else float(bool(passed)), "passed": passed,
                               "termination_reason": w.get("exit_status"),
                               "reward_breakdown": {"tests_passed": passed, "exit_status": w.get("exit_status")},
                               "source": "mini-swe-agent"}
                else:
                    raise Skip("witness_unreadable", f"no reader for harness {harness}")
            except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
                raise Skip("witness_unreadable", f"{run}: {type(e).__name__}: {e}")
            wit["sha256"] = wsha
        else:
            msgs = store_messages(pe)
        if vm_p and p == vm_p:
            start = T.ms_ts(T.ts_ms(boot["ts"]) - int(boot.get("boot_ms") or 0)) if boot else None
            end_ev, spawn_ev = vm_exit, boot
        else:
            start = spawn[p]["ts"] if p in spawn else None
            end_ev, spawn_ev = exit_.get(p), spawn.get(p)
        end = end_ev["ts"] if end_ev else None
        wall = (session["session.close"].get("wall_ms") if p == agent and "session.close" in session else None)
        if wall is None and start and end:
            wall = T.ts_ms(end) - T.ts_ms(start)
        priced = [c for c in calls if "price" in c or "cost_usd" in c]
        usd = round(sum(c["cost_usd"] for c in priced), 9) if priced and all(
            type(c.get("cost_usd")) in (int, float) for c in priced) else None
        ints = lambda k: sum(c[k] for c in calls if type(c.get(k)) is int)
        lineage_ids = {e["event_id"] for e in pe}
        if p in (agent, vm_p):
            lineage_ids |= {e["event_id"] for k, e in session.items() if k != "label"}
        built[p] = {
            "run": run, "principal": p,
            "parent": None if p is None or p == vm_p else (spawn[p].get("parent") if p in spawn else None),
            "children": [], "vm_id": vm, "host": host, "harness": harness, "task": task, "task_id": task_id,
            "evidence": evidence, "messages": msgs, "tools": tools if p == agent else [],
            "outcome": outcome,
            "cost": {"usd": usd, "prompt_tokens": ints("prompt_tokens"), "completion_tokens": ints("completion_tokens"),
                     "llm_calls": len(calls), "policy_calls": roles.get("assistant", 0),
                     "environment_calls": roles.get("user", 0)},
            "timestamps": {"start": start, "end": end, "wall_ms": wall},
            "trace_complete": tc, "witness": wit,
            "lineage": {"session_open": session.get("session.open", {}).get("event_id"),
                        "session_close": session.get("session.close", {}).get("event_id"),
                        "spawn": spawn_ev["event_id"] if spawn_ev else None,
                        "exit": end_ev["event_id"] if end_ev else None,
                        "label": next((e["event_id"] for e in pe if e["kind"] == "label"), None),
                        "events": sorted(lineage_ids)},
            "split": split.get(task_id) if task_id else None, "redact": redact.name if redact else None}

    items = {}
    for p in sorted(built, key=lambda p: (-(p or "").count("/"), p or "")):
        it = built[p]
        if broken:
            it["witness"].update(agrees=False, problems=broken + it["witness"]["problems"])
            for m in it["messages"]:
                m["train"] = False
        if redact:
            apply_redact(it["messages"], redact)
        kids = sorted(q for q, e in spawn.items() if e.get("parent") == p)
        it["children"] = [{"principal": q, "item_id": items[q]["item_id"] if q in items else None,
                           "outcome": {k: (items[q]["outcome"][k] if q in items else None)
                                       for k in ("reward", "passed", "termination_reason")}} for q in kids]
        items[p] = {"item_id": item_id(it), **it}
    return run, items, default


def witness_block(present):
    return {"present": present, "agrees": None, "problems": [], "sha256": None, "join": None, "checked": [],
            "other_calls": []}


def item_id(d):
    return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def message(role, content, train=False, event_id=None, **extra):
    return order({"role": role, "content": content, **extra, "train": train, "event_id": event_id})


def store_messages(pe):
    """A principal with no witness: what the host saw, in (ts, writer, seq) order. A model call has no content."""
    out = []
    for e in pe:
        k = e["kind"]
        if k == "llm.call":
            m = message("assistant", None, event_id=e["event_id"], ts=e["ts"])
            if type(e.get("prompt_tokens")) is int and type(e.get("completion_tokens")) is int:
                m["usage"] = {"prompt_tokens": e["prompt_tokens"], "completion_tokens": e["completion_tokens"]}
            out.append(m)
        elif k == "tool.call":
            args = json.dumps(e["args"], ensure_ascii=False) if "args" in e else "null"
            out.append(message("assistant", None, event_id=e["event_id"], ts=e["ts"], tool_calls=[
                {"id": e["event_id"], "type": "function", "function": {"name": e["tool"], "arguments": args}}]))
        elif k == "tool.result":
            r = e.get("result")
            text = r if isinstance(r, str) or r is None else json.dumps(r, ensure_ascii=False)
            extra = {"error": True} if e.get("error") else {}
            out.append(message("tool", text, event_id=e["event_id"], tool_call_id=e.get("call_ref"), **extra))
    return out


def tau2_messages(sim, calls, system, tools, evidence):
    """τ²'s transcript as Chat Completions messages behind the pinned system prompt, reconciled with the calls."""
    msgs = [message("system", system)]
    for m in sim["messages"]:
        if m["role"] == "tool":
            cm = {"role": "tool", "content": m.get("content"), "tool_call_id": m.get("id")}
            if m.get("error"):
                cm["error"] = True
        else:
            cm = {"role": m["role"], "content": m.get("content")}
            if m.get("tool_calls"):
                cm["tool_calls"] = [{"id": t["id"], "type": "function",
                                     "function": {"name": t["name"], "arguments": json.dumps(t["arguments"], ensure_ascii=False)}}
                                    for t in m["tool_calls"]]
        cm.update(train=False, event_id=None)
        u = m.get("usage")
        if u:
            cm["usage"] = {"prompt_tokens": u["prompt_tokens"], "completion_tokens": u["completion_tokens"]}
        msgs.append(cm)
    wit = witness_block(True)
    if evidence == "witness":
        return [order(m) for m in msgs], {}, wit
    use = [(k + 1, m) for k, m in enumerate(sim["messages"]) if m.get("usage")]
    rids = [((m.get("raw_data") or {}).get("id")) for _, m in use]
    if calls and all(c.get("response_id") for c in calls) and all(rids):
        pairs, problems = join_by_id(use, rids, calls)
        wit["join"] = "response_id"
    else:
        pairs, problems = join_by_usage(use, calls)
        wit["join"] = "tokens"
    checked = {"tokens"} if pairs else set()
    problems += hash_problems(msgs, pairs, system, tools, checked)
    roles = {}
    for k, c in pairs:
        msgs[k].update(event_id=c["event_id"], ts=c["ts"])
        roles[msgs[k]["role"]] = roles.get(msgs[k]["role"], 0) + 1
    for k, _ in pairs:
        msgs[k]["train"] = not problems and msgs[k]["role"] == "assistant"
    wit.update(agrees=not problems, problems=problems, checked=sorted(checked))
    return [order(m) for m in msgs], roles, wit


def join_by_usage(use, calls):
    """docs/trace.md §4: each message with usage takes the next call with the same token counts; both sides must
    be used up."""
    pairs, problems, ci = [], [], 0
    for k, m in use:
        want = (m["usage"]["prompt_tokens"], m["usage"]["completion_tokens"])
        j = ci
        while j < len(calls) and (calls[j].get("prompt_tokens"), calls[j].get("completion_tokens")) != want:
            j += 1
        if j >= len(calls):
            problems.append(f"#{k} usage {want[0]}/{want[1]} matches no stored llm.call")
            continue
        problems += [f"llm.call {c['event_id']} matches no witness message" for c in calls[ci:j]]
        pairs.append((k, calls[j]))
        ci = j + 1
    problems += [f"llm.call {c['event_id']} matches no witness message" for c in calls[ci:]]
    return pairs, problems


def join_by_id(use, rids, calls):
    """The witness message names the upstream reply it came from (raw_data.id); the gateway logged the same id.
    The calls must come in the same order and carry the same token counts."""
    pos = {c["response_id"]: i for i, c in enumerate(calls)}
    pairs, problems, last, used = [], [], -1, set()
    for (k, m), rid in zip(use, rids):
        i = pos.get(rid)
        if i is None or i in used:
            problems.append(f"#{k} response {rid} matches no stored llm.call")
            continue
        c = calls[i]
        if i < last:
            problems.append(f"#{k} comes after llm.call {calls[last]['event_id']} in the witness but before it on the host")
        if (c.get("prompt_tokens"), c.get("completion_tokens")) != (m["usage"]["prompt_tokens"], m["usage"]["completion_tokens"]):
            problems.append(f"#{k} usage {m['usage']['prompt_tokens']}/{m['usage']['completion_tokens']} differs from "
                            f"llm.call {c['event_id']} ({c.get('prompt_tokens')}/{c.get('completion_tokens')})")
        used.add(i)
        last = max(last, i)
        pairs.append((k, c))
    problems += [f"llm.call {c['event_id']} matches no witness message" for i, c in enumerate(calls) if i not in used]
    return pairs, problems


def hash_problems(msgs, pairs, system, tools, checked):
    """Where the gateway hashed the call: the agent's turns were produced from this system prompt and these tools,
    the simulator's were not (the host's own view of who spoke), the content is what the model answered, and an
    agent turn's prompt is exactly the messages before it. A list under input_sha256 is the pre-release form of
    these hashes: only the role, system and tools hashes are comparable there."""
    sys_h, tools_h = T.content_sha(system), T.content_sha(tools)
    forms, problems = [], []
    for k, c in pairs:
        if "tools_sha256" not in c:
            continue
        m, ev = msgs[k], c["event_id"]
        checked |= {"role", "system", "tools"}
        if m["role"] == "assistant":
            if c["tools_sha256"] != tools_h:
                problems.append(f"#{k}: the tool schemas sent with llm.call {ev} are not the item's tools")
            if c["system_sha256"] != sys_h:
                problems.append(f"#{k}: the system prompt sent with llm.call {ev} is not the item's")
        elif c["tools_sha256"] is not None or c["system_sha256"] == sys_h:
            problems.append(f"#{k}: the witness says {m['role']}, but llm.call {ev} was sent the agent's prompt")
        if "messages_sha256" not in c:
            continue
        checked.add("output")
        if T.content_sha(T.chat_form(m, "assistant")) != c["output_sha256"]:
            problems.append(f"#{k}: content differs from what llm.call {ev} answered (output_sha256)")
        if m["role"] == "assistant":
            checked.add("messages")
            while len(forms) < k:
                forms.append(T.content_sha(T.chat_form(msgs[len(forms)])))
            sent = c["messages_sha256"]
            if sent != forms[:k]:
                d = next((i for i, (a, b) in enumerate(zip(sent, forms)) if a != b), min(len(sent), k))
                problems.append(f"#{k}: the prompt differs from what llm.call {ev} sent, from message #{d}")
    return problems


def mswe_messages(w, calls, evidence):
    """mini-swe-agent's trajectory; its assistant turns pair with the calls by order when the counts agree."""
    msgs = []
    for m in w["trajectory"]["messages"]:
        if m["role"] not in ("system", "user", "assistant", "tool"):
            continue
        cm = {"role": m["role"], "content": m.get("content")}
        if m.get("tool_calls"):
            cm["tool_calls"] = [{"id": t.get("id"), "type": "function", "function": {
                "name": t["function"]["name"], "arguments": a if isinstance(a := t["function"].get("arguments"), str)
                else json.dumps(a, ensure_ascii=False)}} for t in m["tool_calls"]]
        if m.get("tool_call_id"):
            cm["tool_call_id"] = m["tool_call_id"]
        cm.update(train=False, event_id=None)
        msgs.append(cm)
    wit = witness_block(True)
    if evidence == "witness":
        return msgs, {}, wit
    turns = [k for k, m in enumerate(msgs) if m["role"] == "assistant"]
    wit["join"] = "count"
    if len(turns) != len(calls):
        wit.update(agrees=False, problems=[f"{len(turns)} assistant messages vs {len(calls)} stored llm.calls"])
        return msgs, {}, wit
    for k, c in zip(turns, calls):
        msgs[k].update(train=True, event_id=c["event_id"], ts=c["ts"])
        if type(c.get("prompt_tokens")) is int:
            msgs[k]["usage"] = {"prompt_tokens": c["prompt_tokens"], "completion_tokens": c["completion_tokens"]}
    wit.update(agrees=True, checked=["count"])
    return msgs, {"assistant": len(turns)}, wit


def order(m):
    """Chat fields first, then the item's own (train, event_id, ts, usage)."""
    keys = ("role", "content", "tool_calls", "tool_call_id", "error", "train", "event_id", "ts", "usage")
    return {k: m[k] for k in keys if k in m}


class Redactor:
    def __init__(self, spec):
        mod, _, fn = spec.rpartition(":")
        if not mod or not fn:
            raise ValueError(f"--redact {spec}: want MOD:FUNC")
        m = _load("redact_hook_file", mod) if mod.endswith(".py") else importlib.import_module(mod)
        self.name, self.fn = spec, getattr(m, fn)

    def __call__(self, text, field):
        return self.fn(text, field) if isinstance(text, str) else text


def apply_redact(msgs, red):
    for m in msgs:
        field = {"system": "system", "tool": "tool_result"}.get(m["role"], "content")
        m["content"] = red(m.get("content"), field)
        for t in m.get("tool_calls") or []:
            t["function"]["arguments"] = red(t["function"]["arguments"], "arguments")
