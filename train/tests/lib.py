"""Oracles and fixture synthesis for the POC 12 suite (train/CONTRACT.md). Standard library only; the tokenizer
oracle runs tok_oracle.py in a one-shot `uv run` with transformers."""
import gzip
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
TRAIN_BIN = os.path.join(REPO, "train", "bin", "train")
FLEET_BIN = os.path.join(REPO, "fleet", "bin", "fleet")
SPLIT = os.path.join(REPO, "data", "splits", "retail_user_disjoint_v1.json")
MATRIX_HELDOUT = os.path.join(REPO, "data", "poc11", "p11-heldout", "matrix.json")
MATRIX_TRAIN = os.path.join(REPO, "data", "poc11", "p11-train", "matrix.json")
EXPORT = os.path.join(FIX, "p11", "export")
LABELS = os.path.join(FIX, "p11", "labels")
FLEET = os.path.join(FIX, "p11", "fleet.json")
TOK_FIX = os.path.join(FIX, "tokenizer")
BASE_MODEL_NAME = "Qwen/Qwen3.5-9B"
SERVED = "policy-qwen3.5-9b"
UV = shutil.which("uv")


# ---------------------------------------------------------------- small helpers

def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def read_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def read_jsonl(p):
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def write_json(p, obj, indent=1):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent, sort_keys=True)
        f.write("\n")


def write_jsonl(p, rows):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def short(s, n=200):
    s = str(s).strip().replace("\n", " | ")
    return s if len(s) <= n else s[:n] + "…"


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def no_traceback(res):
    return "Traceback (most recent call last)" not in (res["err"] + res["out"])


def tree_sha(root):
    """{relative path: sha256} of every file under root."""
    out = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = sha_file(p)
    return out


def run(argv, env=None, timeout=600, cwd=None, stdin=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=e, cwd=cwd or REPO, input=stdin)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as ex:
        rc, out, err = 124, (ex.stdout or b"").decode(errors="replace") if isinstance(ex.stdout, bytes) else (ex.stdout or ""), f"timeout after {timeout}s"
    return {"rc": rc, "out": out, "err": err, "s": time.monotonic() - t0}


def train_env(tmp, tag):
    """Private TMPDIR/HOME/XDG/AVOS_TRAIN_CACHE for one invocation family."""
    base = os.path.join(tmp, "env_" + tag)
    d = {k: os.path.join(base, k.lower()) for k in ("TMPDIR", "HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "AVOS_TRAIN_CACHE")}
    for p in d.values():
        os.makedirs(p, exist_ok=True)
    return d


def train(*args, env=None, timeout=600):
    if not (os.path.isfile(TRAIN_BIN) and os.access(TRAIN_BIN, os.X_OK)):
        raise AssertionError("not implemented: train/bin/train (missing or not executable)")
    return run([TRAIN_BIN] + [str(a) for a in args], env=env, timeout=timeout)


def fleet(*args, env=None, timeout=600):
    res = run([sys.executable, FLEET_BIN] + [str(a) for a in args], env=env, timeout=timeout)
    if args and args[0] == "compare" and res["rc"] == 2 and re.search(r"invalid choice: 'compare'", res["err"]):
        raise AssertionError("not implemented: fleet compare")
    return res


# ---------------------------------------------------------------- fixtures

def check_fixture_manifest():
    man = os.path.join(FIX, "MANIFEST.sha256")
    if not os.path.exists(man):
        raise AssertionError("fixtures/MANIFEST.sha256 missing")
    want = {}
    for line in open(man, encoding="utf-8"):
        line = line.strip()
        if line:
            h, p = line.split("  ", 1)
            want[p] = h
    have = {p: h for p, h in tree_sha(FIX).items() if p != "MANIFEST.sha256"}
    for p in (REPO_FILES):
        have[p] = sha_file(os.path.join(REPO, p))
    if want != have:
        diff = sorted(set(want) ^ set(have)) + sorted(p for p in want if p in have and want[p] != have[p])
        raise AssertionError(f"fixture hashes differ: {diff[:6]}")
    return len(want)


REPO_FILES = ("data/poc11/p11-heldout/matrix.json", "data/poc11/p11-train/matrix.json", "data/splits/retail_user_disjoint_v1.json")


def write_fixture_manifest():
    rows = {p: h for p, h in tree_sha(FIX).items() if p != "MANIFEST.sha256"}
    for p in REPO_FILES:
        rows[p] = sha_file(os.path.join(REPO, p))
    with open(os.path.join(FIX, "MANIFEST.sha256"), "w") as f:
        for p in sorted(rows):
            f.write(f"{rows[p]}  {p}\n")


def unpack_tokenizer(dst):
    os.makedirs(dst, exist_ok=True)
    with gzip.open(os.path.join(TOK_FIX, "tokenizer.json.gz"), "rb") as fi, open(os.path.join(dst, "tokenizer.json"), "wb") as fo:
        shutil.copyfileobj(fi, fo)
    for n in ("tokenizer_config.json", "chat_template.jinja", "config.json"):
        shutil.copy(os.path.join(TOK_FIX, n), os.path.join(dst, n))
    return dst


def load_items():
    return read_jsonl(os.path.join(EXPORT, "trajectories.jsonl"))


def load_rows():
    return read_jsonl(os.path.join(LABELS, "labels.jsonl"))


# ---------------------------------------------------------------- content keys (label/CONTRACT.md §0, retrain §5)

def content_sha(item):
    ms = []
    for m in item["messages"]:
        d = {"role": m["role"]}
        if "content" in m:
            d["content"] = m["content"]
        if "tool_call_id" in m:
            d["tool_call_id"] = m["tool_call_id"]
        if "tool_calls" in m:
            d["tool_calls"] = [{"name": t["function"]["name"], "arguments": t["function"]["arguments"]} for t in m["tool_calls"]]
        ms.append(d)
    return sha_bytes(canonical({"task": item["task"], "tools": item["tools"], "messages": ms}).encode())


def item_id(item):
    d = {k: v for k, v in item.items() if k != "item_id"}
    return sha_bytes(canonical(d).encode())


def rehash(item):
    item["item_id"] = item_id(item)
    return item


def policy_turns(item):
    return [k for k, m in enumerate(item["messages"]) if m.get("train") is True]


def dataset_sha(items):
    return sha_bytes("\n".join(it["item_id"] for it in items).encode())


# ---------------------------------------------------------------- synthesis

def clone_item(item, run_suffix, host="gpu-b", vm="vm-21-aa55ee"):
    """Another fleet's run of the same conversation: same content key, different item_id."""
    it = json.loads(json.dumps(item))
    old_vm = it["vm_id"]
    it["vm_id"] = vm
    it["host"] = host
    it["principal"] = it["principal"].replace(old_vm, vm)
    it["run"] = re.sub(r"_\d+$", f"_{run_suffix}", it["run"])
    return rehash(it)


def set_task(item, task_id, split):
    it = json.loads(json.dumps(item))
    it["task"] = f"retail/{task_id}"
    it["task_id"] = task_id
    it["split"] = split
    return rehash(it)


def _policy_tool_call(item):
    m = next((m for m in item["messages"] if m.get("train") and m.get("tool_calls")), None)
    if m is None:
        raise AssertionError(f"{item['run']} has no policy tool call")
    return m["tool_calls"][0]["function"]


def reserialise_args(item):
    """The same argument object as a different JSON string (indent, key order): same rendering, new content key."""
    it = json.loads(json.dumps(item))
    fn = _policy_tool_call(it)
    fn["arguments"] = json.dumps(json.loads(fn["arguments"]), indent=2, sort_keys=True)
    if fn["arguments"] == _policy_tool_call(item)["arguments"]:
        raise AssertionError("re-serialisation did not change the string")
    return rehash(it)


def mutate_arg_value(item):
    """One argument *value* replaced by a longer string: the rendering, and so the counts, change; usage stays."""
    it = json.loads(json.dumps(item))
    fn = _policy_tool_call(it)
    args = json.loads(fn["arguments"])
    k = next(iter(args))
    args[k] = f"{args[k]} plus several extra words that the gateway never counted"
    fn["arguments"] = json.dumps(args)
    return rehash(it)


def row_for(item, template_row, kept=None, split=None):
    r = json.loads(json.dumps(template_row))
    r["run"] = item["run"]
    r["item_id"] = item["item_id"]
    r["content_sha256"] = content_sha(item)
    r["task"] = item["task"]
    r["task_id"] = item["task_id"]
    r["split"] = item["split"] if split is None else split
    if kept is not None:
        r["final"]["kept"] = kept
        r["final"]["label"] = "pass" if kept else "fail"
    return r


def fleet_path(export_dir):
    """The fleet.json beside an export directory (the fixture's and make_pair's layout, like data/poc11/p11-train)."""
    return os.path.join(os.path.dirname(export_dir), "fleet.json")


def make_pair(tmp, name, items, rows, split_path=SPLIT, manifest_patch=None, fleet_patch=None):
    """A retrain-export directory + a labels directory + a fleet.json, consistent with the items given."""
    ex = os.path.join(tmp, name, "export")
    lb = os.path.join(tmp, name, "labels")
    fl = read_json(FLEET)
    fl["fleet"] = name
    fl["split"]["sha256"] = sha_file(split_path)
    if fleet_patch:
        for k, v in fleet_patch.items():
            (fl["roles"] if k in fl["roles"] else fl)[k] = v
    write_json(os.path.join(tmp, name, "fleet.json"), fl)
    items = sorted(items, key=lambda it: (it["run"], it["principal"]))
    write_jsonl(os.path.join(ex, "trajectories.jsonl"), items)
    man = {"format": "avos-retrain-manifest/1", "seed": 0, "redact": None,
           "split_file": {"path": "data/splits/retail_user_disjoint_v1.json", "sha256": sha_file(split_path)},
           "items": [{"item_id": it["item_id"], "run": it["run"], "principal": it["principal"], "split": it["split"],
                      "passed": it["outcome"]["passed"], "trace_complete": it["trace_complete"]["status"],
                      "witness_agrees": it["witness"]["agrees"], "sft_examples": len(policy_turns(it))} for it in items],
           "skipped": [],
           "counts": {"runs_requested": len(items), "runs_exported": len(items), "items": len(items),
                      "sft_examples": sum(len(policy_turns(it)) for it in items),
                      "passed": sum(1 for it in items if it["outcome"]["passed"] is True),
                      "failed": sum(1 for it in items if it["outcome"]["passed"] is False), "unknown": 0,
                      "train": sum(1 for it in items if it["split"] == "train"),
                      "held_out": sum(1 for it in items if it["split"] == "held_out"),
                      "unsplit": sum(1 for it in items if it["split"] is None),
                      "flagged": sum(1 for it in items if it["witness"]["agrees"] is False)},
           "m14": {"train_runs": len(items), "train_passed": 0, "kept_run_fraction": None},
           "dataset_sha256": dataset_sha(items)}
    if manifest_patch:
        man.update(manifest_patch)
    write_json(os.path.join(ex, "manifest.json"), man)
    write_jsonl(os.path.join(lb, "labels.jsonl"), sorted(rows, key=lambda r: r["run"]))
    return ex, lb


def same_size_replace(path, new_bytes):
    """Replace a file by different content of the same size, keeping mtime (forged caches must notice)."""
    st = os.stat(path)
    if len(new_bytes) != st.st_size:
        raise AssertionError(f"same_size_replace: {len(new_bytes)} != {st.st_size}")
    with open(path, "wb") as f:
        f.write(new_bytes)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def tamper_tree(root):
    """Bit-flip, truncate or empty every file under root; returns how many."""
    n = 0
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            b = bytearray(open(p, "rb").read())
            if len(b) > 8:
                b[len(b) // 2] ^= 0x5A
                b = b[: max(8, len(b) * 3 // 4)]
            with open(p, "wb") as fo:
                fo.write(b)
            n += 1
    return n


# ---------------------------------------------------------------- select oracle (CONTRACT §2)

REFUSALS = {"kept_not_train", "kept_witness_disagrees", "kept_trace_incomplete", "content_sha256_mismatch", "item_id_mismatch",
            "label_run_unknown", "label_missing", "dataset_sha256_mismatch", "split_mismatch", "duplicate_item", "usage_missing",
            "token_parity", "policy_mismatch"}


def select_oracle(pairs, exclude=(), split_path=SPLIT, policy=SERVED):
    """pairs: [(items, rows)] or [(items, rows, fleet)]. Returns ("ok", selected[(item_id, run, source)], rejected[(run, source, reason)]) or ("refuse", reason, run)."""
    train_ids = set(read_json(split_path)["train"])
    selected, rejected, seen = [], [], {}
    for i, pair in enumerate(pairs):
        items, rows = pair[0], pair[1]
        fl = pair[2] if len(pair) > 2 else read_json(FLEET)
        if fl["roles"]["policy"] != policy:
            return ("refuse", "policy_mismatch", fl["fleet"])
        if fl["split"]["sha256"] != sha_file(split_path):
            return ("refuse", "split_mismatch", fl["fleet"])
        by_run = {}
        for r in rows:
            by_run[r["run"]] = r
        runs = {it["run"] for it in items}
        for r in rows:
            if r["run"] not in runs:
                return ("refuse", "label_run_unknown", r["run"])
        for it in items:
            r = by_run.get(it["run"])
            if r is None:
                return ("refuse", "label_missing", it["run"])
            if r["content_sha256"] != content_sha(it):
                return ("refuse", "content_sha256_mismatch", it["run"])
            if r["item_id"] != it["item_id"]:
                return ("refuse", "item_id_mismatch", it["run"])
            if not r["final"]["kept"]:
                rejected.append((it["run"], i, "not_kept"))
                continue
            if it["task_id"] not in train_ids or it["split"] != "train" or r["split"] != "train":
                return ("refuse", "kept_not_train", it["run"])
            if it["witness"]["agrees"] is not True:
                return ("refuse", "kept_witness_disagrees", it["run"])
            if it["trace_complete"]["status"] != "complete":
                return ("refuse", "kept_trace_incomplete", it["run"])
            if it["task_id"] in set(exclude):
                rejected.append((it["run"], i, "excluded_task"))
                continue
            turns = policy_turns(it)
            if not turns:
                rejected.append((it["run"], i, "no_policy_turn"))
                continue
            for k in turns:
                if not it["messages"][k].get("usage"):
                    return ("refuse", "usage_missing", it["run"])
            if it["item_id"] in seen:
                return ("refuse", "duplicate_item", it["run"])
            seen[it["item_id"]] = i
            selected.append((it["item_id"], it["run"], i))
    return ("ok", sorted(selected), sorted(rejected, key=lambda x: (x[1], x[0])))


# ---------------------------------------------------------------- tokenizer oracle (one-shot uv run)

def tok_oracle(tmp, tok_dir, items, timeout=900):
    """{content_sha: {turn_index(str): {"prompt": ids, "completion": ids, "usage": [P, C]}}, "_im_end_id": int}."""
    if not UV:
        raise AssertionError("uv not on PATH: the tokenizer oracle needs `uv run --with transformers`")
    items_p = os.path.join(tmp, "tok_items.jsonl")
    out_p = os.path.join(tmp, "tok_oracle.json")
    write_jsonl(items_p, items)
    res = run([UV, "run", "--python", "3.12", "--with", "transformers", "--with", "jinja2", "--with", "tokenizers",
               "python", "-I", os.path.join(HERE, "tok_oracle.py"), tok_dir, items_p, out_p], timeout=timeout, cwd=HERE)
    if res["rc"] != 0 or not os.path.exists(out_p):
        raise AssertionError(f"tokenizer oracle failed: rc={res['rc']} {short(res['err'], 400)}")
    return read_json(out_p)


# ---------------------------------------------------------------- compare oracle (CONTRACT §8)

def pass_k(n, c, k):
    if n < k:
        return None
    return math.comb(c, k) / math.comb(n, k)


def bootstrap(vals, seed=300, B=2000):
    T = len(vals)
    if T < 2:
        return "unverified"
    rng = random.Random(seed)
    means = sorted(sum(vals[rng.randrange(T)] for _ in range(T)) / T for _ in range(B))
    return [means[50], means[1949]]


def _mean(xs):
    return sum(xs) / len(xs)


def compare_oracle(a, b, seed=300):
    ta = {t["task"]: t for t in a["m11"]["tasks"]}
    tb = {t["task"]: t for t in b["m11"]["tasks"]}
    paired = [t["task"] for t in a["m11"]["tasks"] if t["task"] in tb]
    tasks, out = [], {"format": "avos-fleet-compare/1"}
    for side, m in (("a", a), ("b", b)):
        out[side] = {"fleet": m["inputs"]["fleet"], "policy": m["inputs"]["roles"]["policy"], "trials_per_task": m["inputs"]["trials_per_task"],
                     "concurrency": m["inputs"]["concurrency"], "tasks": len(m["m11"]["tasks"])}
    out["paired_tasks"] = paired
    out["a_only"] = sorted(set(ta) - set(tb))
    out["b_only"] = sorted(set(tb) - set(ta))
    for t in paired:
        row = {"task": t}
        for side, src in (("a", ta), ("b", tb)):
            x = src[t]
            row[side] = {"n": x["n"], "c": x["c"], "pass1": pass_k(x["n"], x["c"], 1), "pass4": pass_k(x["n"], x["c"], 4)}
        for k in ("pass1", "pass4"):
            row["d_" + k] = None if row["a"][k] is None or row["b"][k] is None else row["b"][k] - row["a"][k]
        tasks.append(row)
    out["tasks"] = tasks
    for k in ("pass1", "pass4"):
        rows = [r for r in tasks if r["d_" + k] is not None]
        if rows:
            d = [r["d_" + k] for r in rows]
            out[k] = {"a": _mean([r["a"][k] for r in rows]), "b": _mean([r["b"][k] for r in rows]), "diff": _mean(d),
                      "ci95": bootstrap(d, seed), "tasks": len(rows)}
        else:
            out[k] = {"a": "unverified", "b": "unverified", "diff": "unverified", "ci95": "unverified", "tasks": 0}

    def trio(va, vb):
        d = "unverified" if isinstance(va, str) or isinstance(vb, str) or va is None or vb is None else vb - va
        return {"a": va, "b": vb, "diff": d}

    def get(m, *path):
        x = m
        for p in path:
            x = x.get(p, "unverified") if isinstance(x, dict) else "unverified"
        return x

    out["m12"] = {"policy_usd_per_task": trio(get(a, "m12", "policy", "usd_per_task"), get(b, "m12", "policy", "usd_per_task")),
                  "policy_usd_per_successful_task": trio(get(a, "m12", "policy", "usd_per_successful_task"), get(b, "m12", "policy", "usd_per_successful_task")),
                  "simulator_usd_per_task": trio(get(a, "m12", "simulator", "usd_per_task"), get(b, "m12", "simulator", "usd_per_task")),
                  "nl_judge_usd_per_task": trio(get(a, "m12", "nl_judge", "usd_per_task"), get(b, "m12", "nl_judge", "usd_per_task"))}
    out["m13"] = {k: trio(get(a, "m13", k), get(b, "m13", k)) for k in ("median_s", "p90_s", "worst_s")}
    ca, cb = a["m11"].get("causes", {}), b["m11"].get("causes", {})
    out["causes"] = {"a": dict(sorted(ca.items())), "b": dict(sorted(cb.items())),
                     "diff": {c: cb.get(c, 0) - ca.get(c, 0) for c in sorted(set(ca) | set(cb))}}
    out["by_class"] = {c: trio(a["m11"].get("by_class", {}).get(c, 0), b["m11"].get("by_class", {}).get(c, 0)) for c in ("agent", "infra")}
    out["bootstrap"] = {"seed": seed, "B": 2000, "rule": "paired percentile bootstrap over tasks"}
    return out


def diff(got, want, path="", tol=1e-9, ignore=()):
    """Differences between the solver's JSON and the oracle; strings exact, numbers within tol."""
    out = []
    if path in ignore:
        return out
    if isinstance(want, dict):
        if not isinstance(got, dict):
            return [f"{path}: want object, got {type(got).__name__}"]
        for k in want:
            if k not in got:
                out.append(f"{path}.{k}: missing")
            else:
                out += diff(got[k], want[k], f"{path}.{k}", tol, ignore)
        return out
    if isinstance(want, list):
        if not isinstance(got, list) or len(got) != len(want):
            return [f"{path}: want list of {len(want)}, got {short(got, 80)}"]
        for i, (g, w) in enumerate(zip(got, want)):
            out += diff(g, w, f"{path}[{i}]", tol, ignore)
        return out
    if isinstance(want, bool) or want is None or isinstance(want, str):
        if got != want:
            out.append(f"{path}: want {want!r}, got {short(got, 60)!r}")
        return out
    if isinstance(want, (int, float)):
        if isinstance(got, bool) or not isinstance(got, (int, float)) or abs(got - want) > tol:
            out.append(f"{path}: want {want!r}, got {short(got, 60)!r}")
        return out
    return out


def synthetic_matrix(base, c_delta=None, median_delta=0.0, add_cause=None, fleet="p12-heldout", policy="policy-qwen3.5-9b-p12",
                     drop_tasks=(), trials_per_task=None, simulator=None, part=None):
    """A consistent variant of a real matrix with known changes (pass fields recomputed from the parts)."""
    m = json.loads(json.dumps(base))
    m["inputs"]["fleet"] = fleet
    m["inputs"]["roles"]["policy"] = policy
    if trials_per_task:
        m["inputs"]["trials_per_task"] = trials_per_task
    if simulator:
        m["inputs"]["roles"]["simulator"] = simulator
    if part:
        m["inputs"]["split"]["part"] = part
    tasks = []
    for t in m["m11"]["tasks"]:
        if t["task"] in drop_tasks:
            continue
        if c_delta and t["task"] in c_delta:
            t["c"] = max(0, min(t["n"], t["c"] + c_delta[t["task"]]))
        t["pass1"] = pass_k(t["n"], t["c"], 1)
        t["pass4"] = pass_k(t["n"], t["c"], 4)
        t["avg4"] = t["pass1"]
        tasks.append(t)
    m["m11"]["tasks"] = tasks
    for k in ("pass1", "pass4"):
        vals = [t[k] for t in tasks if t[k] is not None]
        m["m11"][k] = _mean(vals) if vals else "unverified"
        m["m11"][k + "_ci95"] = bootstrap(vals)
    if median_delta:
        m["m13"]["median_s"] = m["m13"]["median_s"] + median_delta
    if add_cause:
        m["m11"].setdefault("causes", {})[add_cause[0]] = add_cause[1]
        m["m11"].setdefault("by_class", {"agent": 0, "infra": 0})
        m["m11"]["by_class"][add_cause[2]] = m["m11"]["by_class"].get(add_cause[2], 0) + add_cause[1]
    return m


# ---------------------------------------------------------------- fit directory synthesis (CONTRACT §3–§4)

def make_fitdir(tmp, name, steps, wall_s, gpu_count, price, runs, examples=1000, losses=None, base=BASE_MODEL_NAME, log_lines=None):
    d = os.path.join(tmp, name)
    os.makedirs(os.path.join(d, "adapter"), exist_ok=True)
    losses = losses if losses is not None else [round(2.0 - 0.1 * i, 4) for i in range(steps)]
    hp = {"lr": 1e-4, "rank": 16, "alpha": 32, "dropout": 0.05, "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
          "batch_tokens": 16384, "max_len": 32768, "seed": 300, "precision": "bf16", "optimizer": "adamw", "schedule": "cosine",
          "epochs": None, "steps_requested": steps}
    fit = {"format": "avos-train-fit/1", "base_model": "/models/Qwen3.5-9B", "base_model_name": base,
           "data": {"path": "/data/p12", "examples_sha256": "ab" * 32, "runs": runs, "examples": examples, "tokens": 123456},
           "hyperparameters": hp, "steps": steps, "epochs": steps * hp["batch_tokens"] / 123456,
           "started": "2026-10-25T10:00:00.000Z", "ended": "2026-10-25T10:20:34.500Z", "wall_s": wall_s,
           "gpu": {"name": "L40S", "count": gpu_count, "usd_per_h": price},
           "adapter": {"path": "adapter", "b_all_zero": steps == 0, "trainable_params": 1234567}}
    write_json(os.path.join(d, "fit.json"), fit)
    lines = log_lines if log_lines is not None else [{"step": i + 1, "loss": losses[i], "lr": 1e-4, "tokens": 16000 + i, "elapsed_s": 3.0 * (i + 1)} for i in range(steps)]
    write_jsonl(os.path.join(d, "log.jsonl"), lines)
    return d, fit, losses
