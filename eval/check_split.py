"""Check the split: zero shared users, every task assigned once, counts printed.

Usage: python eval/check_split.py <tau2-bench-checkout> [split.json]
"""
import json, sys
from pathlib import Path

root = Path(sys.argv[1])
s = json.load(open(sys.argv[2] if len(sys.argv) > 2 else "data/splits/retail_user_disjoint_v1.json"))
all_ids = {t["id"] for t in json.load(open(root / "data/tau2/domains/retail/tasks.json"))}
tr, ho, ex = s["train"], s["held_out"], set(s["meta"]["excluded"])

assert not set(s["users"]["train"]) & set(s["users"]["held_out"]), "shared users in user lists"
assert not {s["task_user"][i] for i in tr} & {s["task_user"][i] for i in ho}, "shared users across tasks"
assert len(tr) == len(set(tr)) and len(ho) == len(set(ho)), "duplicate task in a split"
assert not set(tr) & set(ho), "task in both splits"
assert set(tr) | set(ho) | ex == all_ids and not (set(tr) | set(ho)) & ex, "tasks missing or wrongly excluded"
print(f"OK train={len(tr)} held_out={len(ho)} excluded={len(ex)} total={len(all_ids)} "
      f"users train={len(s['users']['train'])} held_out={len(s['users']['held_out'])} shared=0")
