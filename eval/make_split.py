"""Build the user-disjoint τ²-bench retail split (`docs/conventions.md` §1).

Usage: python eval/make_split.py <tau2-bench-checkout> [out.json]
Whole users go to train or held-out; seed 300. Deterministic.
"""
import json, random, re, subprocess, sys
from collections import defaultdict
from pathlib import Path

SEED = 300
HELD_OUT_TARGET = 44          # target: ~70 train / ~44 held-out (of 114, before exclusions)
# Gold trajectory wrong or disputed; see docs/split-notes.md
EXCLUDE = {
    "64": "#499: exchange on a pending order; wrong action in gold trajectory",
    "105": "#499: gold exchange raises (gift card $17 < $21.10), gold DB == initial DB",
    "18": "gold actions differ in amazon-agi/tau2-bench-verified",
    "49": "gold actions differ in amazon-agi/tau2-bench-verified",
    "91": "gold actions differ in amazon-agi/tau2-bench-verified",
}


def resolve_user(task, db):
    users, orders = db["users"], db["orders"]
    by_email = {u["email"].lower(): uid for uid, u in users.items()}
    by_name_zip = {(u["name"]["first_name"].lower(), u["name"]["last_name"].lower(),
                    u["address"]["zip"]): uid for uid, u in users.items()}
    acts = (task.get("evaluation_criteria") or {}).get("actions") or []
    for a in acts:
        ar = a["arguments"]
        if ar.get("user_id") in users:
            return ar["user_id"]
        if a["name"] == "find_user_id_by_email" and ar["email"].lower() in by_email:
            return by_email[ar["email"].lower()]
        if a["name"] == "find_user_id_by_name_zip":
            k = (ar["first_name"].lower(), ar["last_name"].lower(), ar["zip"])
            if k in by_name_zip:
                return by_name_zip[k]
        if ar.get("order_id") in orders:
            return orders[ar["order_id"]]["user_id"]
    ki = (task["user_scenario"]["instructions"].get("known_info") or "")
    for uid in users:
        if uid in ki:
            return uid
    for em, uid in by_email.items():
        if em in ki.lower():
            return uid
    for (f, l, z), uid in by_name_zip.items():
        if f"{f} {l}" in ki.lower() and z in ki:
            return uid
    return None


def main():
    root = Path(sys.argv[1])
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "data/splits/retail_user_disjoint_v1.json")
    d = root / "data/tau2/domains/retail"
    tasks = json.load(open(d / "tasks.json"))
    db = json.load(open(d / "db.json"))
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

    by_user, unresolved = defaultdict(list), []
    for t in tasks:
        if t["id"] in EXCLUDE:
            continue
        u = resolve_user(t, db)
        (by_user[u].append(t["id"]) if u else unresolved.append(t["id"]))
    assert not unresolved, f"unresolved users for tasks {unresolved}"

    # Sort for determinism, shuffle with the seed, fill held-out until target reached.
    users = sorted(by_user)
    random.Random(SEED).shuffle(users)
    held, n = [], 0
    for u in users:
        if n >= HELD_OUT_TARGET:
            break
        held.append(u)
        n += len(by_user[u])
    train = [u for u in users if u not in held]
    key = lambda i: int(i)
    split = {
        "meta": {"benchmark": "tau2-bench retail", "tau2_commit": commit, "seed": SEED,
                 "held_out_target": HELD_OUT_TARGET, "excluded": EXCLUDE,
                 "unit": "task ids (strings); users are db.json user_ids"},
        "train": sorted((i for u in train for i in by_user[u]), key=key),
        "held_out": sorted((i for u in held for i in by_user[u]), key=key),
        "users": {"train": sorted(train), "held_out": sorted(held)},
        "task_user": {i: u for u, ids in by_user.items() for i in ids},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(split, open(out, "w"), indent=1, ensure_ascii=False)
    print(f"wrote {out}: train={len(split['train'])} held_out={len(split['held_out'])} "
          f"excluded={len(EXCLUDE)} users={len(users)}")


if __name__ == "__main__":
    main()
