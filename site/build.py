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


def sample_trace():
    """One real τ² trial's event stream (kinds, models, timing; no content, the store keeps none)."""
    rows = [json.loads(l) for l in (ROOT / "fleet/tests/fixtures/results/run_retail-0_22/events.jsonl").open()]
    t0 = min(r["ts"] for r in rows)
    keep = []
    for r in sorted(rows, key=lambda r: r["ts"]):
        e = {"kind": r["kind"], "principal": r["principal"].split("avoslab.com/")[-1]}
        for k in ("model", "status", "ms", "prompt_tokens", "completion_tokens", "decision", "boot_ms"):
            if r.get(k) is not None:
                e[k] = r[k]
        keep.append(e)
    return {"run": "run_retail-0_22", "start": t0, "events": keep}


def build():
    m13 = load("data/poc13/m15-pooled.json")
    gate = {s: {a: load(f"data/poc14/gate/g-{s}-{a}/matrix.json")["m11"]["pass1"] for a in ("base", "opus")}
            for s in ("haiku", "sonnet")}
    fit = load("data/poc12/fit-gate/card.json")
    loss = [json.loads(l)["loss"] for l in (ROOT / "data/poc12/fit-gate/log.jsonl").open()]
    heldout = load("data/poc13/p13-heldout-opus/matrix.json")
    return {
        "generated_from": ["data/poc10/README.md", "data/poc11/README.md", "data/poc12/fit-gate", "data/poc13/m15-pooled.json",
                           "data/poc14/gate", "fleet/tests/fixtures/results/run_retail-0_22/events.jsonl"],
        "sample": False,
        "control": {"stop_ms_median": 40, "stop_ms_worst": 43, "cut_ms_median": 67, "sibling_events_lost": 0,
                    "vms_running": 32, "gateway_calls": 20000, "gateway_unanswered": 0, "bar_ms": 500,
                    "source": "data/poc10/README.md (claim 1 with 32 VMs running, measured outside the tool)"},
        "trace": {"trials": 176, "complete": 176, "events_in_store": 390886, "query_s": 0.04,
                  "source": "data/poc11/README.md (M8, M9)"},
        "retrain": {"method": "LoRA", "base": fit.get("base_model", "Qwen/Qwen3.5-9B"), "runs": 265, "steps": len(loss),
                    "loss": loss, "usd": 7.91, "gpu_hours": 4.25, "source": "data/poc12/fit-gate"},
        "score": {"benchmark": "τ²-bench retail, held-out, user-disjoint, 44 tasks × 4 trials per session",
                  "retrained": arm(m13, "retrained"), "opus": arm(m13, "closed"), "base": pooled_base(),
                  "x": m13["x"], "y": m13["y"], "diff_pass4": m13["diff_pass4"],
                  "opus_usd_uncached": heldout["m12"]["policy"]["uncached_ceiling_usd_per_task"],
                  "gate_pass1": gate, "source": "data/poc13/README.md, data/poc14/README.md"},
        "trace_sample": sample_trace(),
    }


if __name__ == "__main__":
    cfg = json.dumps(build(), indent=1, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        sys.exit(0 if OUT.exists() and OUT.read_text() == cfg else "site/data/cfg.json is stale: run python3 site/build.py")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(cfg)
    print(f"wrote {OUT.relative_to(ROOT)}")
