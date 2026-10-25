"""Build site/data/cfg.json from the measured runs in data/, so every number on the site comes from a committed file.

Usage: python3 site/build.py [--check]
--check: rebuild in memory and exit 1 if site/data/cfg.json differs (CI uses it).
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "site/data/cfg.json"


def load(p):
    return json.loads((ROOT / p).read_text())


def arm(m15, name):
    p = m15["arms"][name]["pooled"]
    return {"pass1": p["pass1"], "pass1_ci95": p["pass1_ci95"], "pass4": p["pass4"], "pass4_ci95": p["pass4_ci95"],
            "trials": p["trials"], "usd_per_task": p["usd_per_task"]}


def pooled_base():
    """Untrained 9B, two held-out sessions pooled per task, pass^4 = mean C(c,4)/C(n,4)."""
    from math import comb
    tasks, usd = {}, []
    for p in ("data/poc11/p11-heldout/matrix.json", "data/poc12/p12-heldout-base/matrix.json"):
        m = load(p)
        usd.append(m["m12"]["policy"]["usd_per_task"])
        for t in m["m11"]["tasks"]:
            a = tasks.setdefault(t["task"], [0, 0])
            a[0] += t["c"]
            a[1] += t["n"]
    p4 = [comb(c, 4) / comb(n, 4) for c, n in tasks.values() if n >= 4]
    p1 = [c / n for c, n in tasks.values() if n]
    return {"pass1": sum(p1) / len(p1), "pass4": sum(p4) / len(p4), "trials": sum(n for _, n in tasks.values()),
            "usd_per_task": sum(usd) / len(usd)}


REPLAY_FILE, REPLAY_VM = "trace/tests/fixtures/real_events.jsonl", "vm-01-417bd7"


def iso_s(ts):
    from datetime import datetime
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def replay():
    """Every event one microVM wrote to the host ledger during a recorded claim 1 session, in order, unedited
    except that principals are shortened to the path after the VM and text fields the store never had stay absent."""
    rows = sorted((json.loads(l) for l in (ROOT / REPLAY_FILE).open()), key=lambda r: r["ts"])
    rows = [r for r in rows if r.get("vm_id") == REPLAY_VM]
    t0 = iso_s(rows[0]["ts"])

    def short(p):
        p = (p or "").split(REPLAY_VM, 1)[-1]
        return p.removeprefix("/agent/") or "vm"
    keep = []
    for r in rows:
        e = {"t": round(iso_s(r["ts"]) - t0, 3), "ts": r["ts"], "kind": r["kind"],
             "who": short(r.get("principal") or r.get("target") or r.get("spiffe_id"))}
        if r["kind"] == "agent.spawn":
            e["who"], e["parent"] = short(r["spiffe_id"]), short(r.get("parent"))
        for k in ("decision", "action", "receipt", "ms", "model", "status", "prompt_tokens", "completion_tokens",
                  "tool", "boot_ms", "dropped"):
            if r.get(k) is not None:
                e[k] = r[k]
        if r["kind"] == "authz.decision":
            e["resource"], e["policy_ids"] = r.get("resource"), r.get("policy_ids")
            if (r.get("args") or {}).get("amount_cents") is not None:
                e["amount_cents"] = r["args"]["amount_cents"]
        if r["kind"] in ("halt", "resume"):
            e["nodes"] = [short(n) for n in r.get("nodes") or []]
        keep.append(e)
    return {"source": f"{REPLAY_FILE} (host ledger, vm {REPLAY_VM}, {rows[0]['ts'][:10]})", "host": rows[0].get("host"),
            "duration_s": keep[-1]["t"], "events": keep}


def labels():
    """Label counts of the two train fleets the fit drew from (label run summaries)."""
    out = []
    for name, p in (("p11-train (policy T 0)", "data/poc12/p11-train-labels-p12/summary.json"),
                    ("p12-train-t07 (policy T 0.7)", "data/poc12/p12-train-t07/labels/summary.json")):
        c = load(p)["counts"]
        out.append({"fleet": name, "runs": c["labelled"], "kept": c["kept"], "dropped": c["dropped"]})
    return out


def build():
    m13 = load("data/poc13/m15-pooled.json")
    gate = {s: {a: load(f"data/poc14/gate/g-{s}-{a}/matrix.json")["m11"]["pass1"] for a in ("base", "opus")}
            for s in ("haiku", "sonnet")}
    fit = load("data/poc12/fit-gate/card.json")
    loss = [json.loads(l)["loss"] for l in (ROOT / "data/poc12/fit-gate/log.jsonl").open()]
    heldout = load("data/poc13/p13-heldout-opus/matrix.json")
    return {
        "generated_from": ["data/poc10/README.md", "data/poc11/README.md", "data/poc12/fit-gate", "data/poc12/*/labels",
                           "data/poc13/m15-pooled.json", "data/poc14/gate", REPLAY_FILE],
        "sample": False,
        "control": {"stop_ms_median": 40, "stop_ms_worst": 43, "cut_ms_median": 67, "sibling_events_lost": 0,
                    "vms_running": 32, "gateway_calls": 20000, "gateway_unanswered": 0, "bar_ms": 500,
                    "source": "data/poc10/README.md (claim 1 with 32 VMs running, measured outside the tool)"},
        "trace": {"trials": 176, "complete": 176, "events_in_store": 390886, "query_s": 0.04,
                  "source": "data/poc11/README.md (M8, M9)"},
        "retrain": {"method": "LoRA", "base": fit.get("base_model", "Qwen/Qwen3.5-9B"), "runs": fit["data"]["runs"],
                    "steps": len(loss), "loss": loss, "usd": 7.91, "gpu_hours": 4.25, "source": "data/poc12/fit-gate"},
        "labels": labels(),
        "score": {"benchmark": "τ²-bench retail, held-out, user-disjoint, 44 tasks × 4 trials per session",
                  "retrained": arm(m13, "retrained"), "opus": arm(m13, "closed"), "base": pooled_base(),
                  "x": m13["x"], "y": m13["y"], "diff_pass4": m13["diff_pass4"],
                  "opus_usd_uncached": heldout["m12"]["policy"]["uncached_ceiling_usd_per_task"],
                  "gate_pass1": gate, "source": "data/poc13/README.md, data/poc14/README.md"},
        "replay": replay(),
    }


if __name__ == "__main__":
    cfg = json.dumps(build(), indent=1, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        sys.exit(0 if OUT.exists() and OUT.read_text() == cfg else "site/data/cfg.json is stale: run python3 site/build.py")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(cfg)
    print(f"wrote {OUT.relative_to(ROOT)}")
