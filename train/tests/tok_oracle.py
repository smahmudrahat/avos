"""Tokenizer oracle: renders every policy turn of the given items exactly as vLLM did (CONTRACT §2) and writes the
expected prompt/completion ids. Run by lib.tok_oracle under `uv run --with transformers --with jinja2 --with tokenizers`.

  python -I tok_oracle.py TOK_DIR items.jsonl out.json
"""
import hashlib
import json
import sys

from transformers import AutoTokenizer

tok_dir, items_p, out_p = sys.argv[1:4]
tok = AutoTokenizer.from_pretrained(tok_dir)
GEN = "<think>\n\n</think>\n\n"


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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
    return hashlib.sha256(canonical({"task": item["task"], "tools": item["tools"], "messages": ms}).encode()).hexdigest()


def reduce(m):
    d = {"role": m["role"]}
    if m.get("content") is not None:
        d["content"] = m["content"]
    if m.get("tool_calls"):
        d["tool_calls"] = [{"id": t["id"], "type": "function",
                            "function": {"name": t["function"]["name"], "arguments": json.loads(t["function"]["arguments"])}} for t in m["tool_calls"]]
    if m.get("tool_call_id"):
        d["tool_call_id"] = m["tool_call_id"]
    return d


out = {"_im_end_id": tok.convert_tokens_to_ids("<|im_end|>")}
for line in open(items_p, encoding="utf-8"):
    if not line.strip():
        continue
    it = json.loads(line)
    msgs = [reduce(m) for m in it["messages"]]
    turns = {}
    for k, m in enumerate(it["messages"]):
        if m.get("train") is not True:
            continue
        prompt = tok.apply_chat_template(msgs[:k], tools=it["tools"], add_generation_prompt=True, enable_thinking=False, tokenize=False)
        full = tok.apply_chat_template(msgs[:k + 1], tools=it["tools"], add_generation_prompt=False, enable_thinking=False, tokenize=False)
        assert prompt.endswith(GEN) and full.startswith(prompt[: -len(GEN)]), (it["run"], k)
        comp = full[len(prompt):]
        if comp.endswith("\n"):
            comp = comp[:-1]
        u = m.get("usage") or {}
        turns[str(k)] = {"prompt": tok(prompt, add_special_tokens=False)["input_ids"],
                         "completion": tok(comp, add_special_tokens=False)["input_ids"],
                         "prompt_text": prompt,
                         "usage": [u.get("prompt_tokens"), u.get("completion_tokens")]}
    out[content_sha(it)] = turns
with open(out_p, "w", encoding="utf-8") as f:
    json.dump(out, f)
