# POC 14 results: can a stronger simulator separate the models?

2026-10-25. POC 13 found Opus 5.5, the retrained 9B and even the untrained 9B tied under the open-weight user
simulator (gemma-3-27b-it). Claim 3 was amended to use one fixed Claude simulator, chosen on train tasks by a gate
written down before any run. This POC ran that gate. **The gate failed, so no held-out run was made, as the gate
rule requires.** It ends with the claim 3 verdict below.

## Gate (pre-registered)

20 retail train tasks (`random.Random(300).sample` of the 65 train tasks: 0 7 8 15 37 38 39 47 52 56 60 61 67 73
75 92 96 98 99 104) × 2 trials, policies Opus 5.5 (POC 13 pins: effort medium, `max_tokens` 16000, caching) and the
untrained Qwen3.5-9B, under each candidate simulator at its documented default effort (Haiku 5.5 medium, Sonnet 5.5
high; no temperature, which Claude models reject). Rule: a simulator passes if Opus − base pass^1, paired by task,
has a 95 % bootstrap interval (seed 300, 2,000 resamples) above 0; pick the cheaper passing one; if none passes,
stop.

| Simulator | Opus pass^1 | Base 9B pass^1 | Opus − base | Simulator $/trial | Verdict |
|---|---|---|---|---|---|
| Claude Haiku 5.5 | 0.750 | 0.650 | +0.100 [−0.075, +0.300] | $0.0013–0.0021 | fail |
| Claude Sonnet 5.5 | 0.775 | 0.825 | −0.050 [−0.150, +0.050] | $0.020–0.029 | fail |
| gemma-3-27b-it (check, not a candidate) | 0.650 | 0.413 † | +0.237 [+0.069, +0.419] | $0.0006 | — |

† Base under gemma from `data/poc11/p11-train` (8 trials per task, another day); Opus under gemma ran today. All 200
gate trials completed (M2 200/200, none excluded). Directories `gate/g-*` (fleet.json, trials.jsonl, matrix.json).

**What the gate shows.** Neither Claude simulator separates Opus from the untrained 9B. Both make the tasks easier for
the 9B (0.41 → 0.65 / 0.83) more than for Opus (0.65 → 0.75 / 0.78): a more cooperative user rescues a weaker
agent. The family worry (a Claude user favouring a Claude agent) did not show; the effect ran the other way.

## Power: what it would take to decide X

Resampling the POC 13 per-task results (retrained 3 sessions, Opus 2) at larger task counts, with X near its
measured 0.96:

| Held-out tasks | X 95 % interval |
|---|---|
| 44 (retail, today) | [0.55, 1.63] |
| 64 (retail + airline) | [0.59, 1.51] |
| 110 | [0.67, 1.34] |
| 200 | [0.74, 1.21] |
| 400 | [0.80, 1.14] |

τ²-bench retail and airline give about 64 user-disjoint held-out tasks; deciding X against 0.75 needs about 200. A
held-out run here (≈ $150) would have been unresolved by design, so it was not made.

## Claim 3 verdict

> A ~9B open-weight model, LoRA-retrained only on its own passed runs, reaches ≥ 75 % of Claude Opus 5.5's pass^4 at
> ≥ 5× lower cost per task on held-out, user-disjoint τ²-bench retail.

- **Y (cost) — passes, conclusively.** 18.2× [17.0, 19.4] (POC 13): Opus $0.102 per trial billed with caching
  ($0.36 uncached), the retrained 9B $0.0056 on two L40S.
- **X (score) — not established, and not refuted; τ²-bench cannot decide it.** Every point estimate is above the
  bar (0.96 pooled, 0.81 same-day, 0.90 for the untrained base), every interval contains it, and no simulator tried
  (gemma-3-27b on held-out; Haiku 5.5, Sonnet 5.5 on train) separates Opus from the untrained 9B. With the tasks τ²
  has, the interval cannot be narrowed enough (table above).
- **So the claim as pre-registered is not met.** What can be said: on held-out τ²-bench retail, the retrained 9B is
  statistically indistinguishable from Opus 5.5 at 18× lower cost, and so is the untrained 9B; the benchmark does not
  show what the retraining or Opus add.

Deciding X needs a benchmark with ≈ 200+ user-disjoint held-out tasks on which Opus measurably beats the untrained
9B (for example τ² telecom plus others, or a harder task set), with the same gate first.

## Spend

Anthropic ≈ $16 (Opus policy $14.2, Sonnet simulator $2.0, Haiku $0.1). AWS ≈ $8 (fleet host and two L40S ≈ 1.3 h).
All hosts stopped; the API key and Claude routes removed from the fleet host.

## What this does not prove

- **That the models are equal.** "Indistinguishable" here is a statement about this benchmark's resolution.
- **The gate's size.** 20 tasks × 2 trials; Haiku's +0.10 could be real and too small to show at this size.
- **The gemma row is cross-session** (base from POC 11, Opus today); POC 12 measured session noise at ±0.05.
- **Two Claude simulators, default effort only.** Other simulators or efforts were not tried, by design.
- **The power table** assumes POC 13's per-task results are representative of new tasks.
- **Airline was not built.** The gate stopped the POC before the airline split and retraining it would have needed.
