"""Done test 3 on the pinned τ² (runs on the dev host inside ~/tau2-bench/.venv):

  python tau2_roundtrip.py RESULT_JSON TRAJECTORIES_JSONL RUN PRINCIPAL

Loads the original SimulationRun through τ²'s own `Results.load`, rebuilds a SimulationRun from the exported
item's messages (system message dropped; tool calls back to τ² ToolCall objects), validates it with τ²'s pydantic
models, and compares the ordered messages: class, role, content, tool-call ids/names/arguments, tool message ids,
error flags. Prints one JSON line: {"ok": bool, "n": messages, "problems": [...]}. Standard library + τ² only.
"""
import json
import sys


def norm(m):
    tcs = [(t.id, t.name, json.dumps(t.arguments, sort_keys=True)) for t in (getattr(m, "tool_calls", None) or [])]
    return (type(m).__name__, m.role, m.content, tuple(tcs), getattr(m, "id", None), bool(getattr(m, "error", False)))


def main(result_path, traj_path, run, principal):
    from tau2.data_model.simulation import Results, SimulationRun

    orig = Results.load(result_path).simulations[0]
    item = next((json.loads(l) for l in open(traj_path, encoding="utf-8")
                 if l.strip() and json.loads(l)["run"] == run and json.loads(l)["principal"] == principal), None)
    if item is None:
        print(json.dumps({"ok": False, "n": 0, "problems": [f"no item for {run} {principal}"]}))
        return 1
    rebuilt = []
    for m in item["messages"]:
        if m["role"] == "system":
            continue
        if m["role"] == "tool":
            rebuilt.append({"role": "tool", "id": m["tool_call_id"], "content": m.get("content"),
                            "requestor": "assistant", "error": bool(m.get("error", False))})
        else:
            d = {"role": m["role"], "content": m.get("content")}
            if m.get("tool_calls"):
                d["tool_calls"] = [{"id": t["id"], "name": t["function"]["name"],
                                    "arguments": json.loads(t["function"]["arguments"]), "requestor": "assistant"}
                                   for t in m["tool_calls"]]
            if m.get("usage"):
                d["usage"] = {"prompt_tokens": m["usage"]["prompt_tokens"], "completion_tokens": m["usage"]["completion_tokens"]}
            rebuilt.append(d)
    base = json.load(open(result_path))["simulations"][0]
    base["messages"] = rebuilt
    problems = []
    try:
        sim = SimulationRun.model_validate(base)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "n": len(rebuilt), "problems": [f"τ² rejects the rebuilt run: {type(e).__name__}: {str(e)[:300]}"]}))
        return 1
    a, b = [norm(m) for m in orig.messages], [norm(m) for m in sim.messages]
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            problems.append(f"message {i}: original {x!r}"[:300] + f" rebuilt {y!r}"[:300])
            break
    if len(a) != len(b):
        problems.append(f"length: original {len(a)} rebuilt {len(b)}")
    def usage(m):  # pinned τ² keeps usage as a dict; be safe either way
        u = getattr(m, "usage", None)
        if u is None:
            return None
        g = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
        return (g("prompt_tokens"), g("completion_tokens"))
    ou = [usage(m) for m in orig.messages if usage(m)]
    ru = [usage(m) for m in sim.messages if usage(m)]
    if ou != ru:
        problems.append(f"usage sequence differs: {len(ou)} vs {len(ru)}")
    print(json.dumps({"ok": not problems, "n": len(a), "problems": problems}))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:5]))
