# POC 13 results

Run the held-out benchmark with Claude Opus 5.5 as the policy, record score and cost, and compute claim 3's M15
against the LoRA-retrained 9B. 2026-10-25. Tools: the gateway's `anthropic` route kind and `fleet m15`
(`eval/CONTRACT.md`, `docs/architecture.md`). Closed-arm settings frozen in `docs/versions.md` ("POC 13") before any
closed-model score: `claude-opus-5-5`, effort `medium` (Anthropic's documented default), `max_tokens` 16000,
automatic prompt caching.

## Result

**Y passes; X is unresolved, and the benchmark as configured does not separate the two models.**

Held-out, 44 tasks, pooled per task, paired percentile bootstrap over tasks (seed 300, 2,000 resamples),
`m15-pooled.json` (retrained: 3 sessions, 528 trials; Opus: 2 sessions, 352 trials, one excluded):

| | Retrained 9B (LoRA) | Opus 5.5 | |
|---|---|---|---|
| pass^1 | 0.477 [0.390, 0.570] | 0.492 [0.401, 0.591] | |
| pass^4 | 0.188 [0.106, 0.277] | 0.197 [0.110, 0.299] | difference −0.009 [−0.116, +0.085] |
| Cost per trial | $0.0056 (GPU-hours) | $0.102 (billed tokens) | |
| Cost per successful trial | $0.0117 | $0.207 | |
| **X** = retrained pass^4 ÷ Opus pass^4 (bar ≥ 0.75) | | | **0.96 [0.55, 1.63] → unresolved** |
| **Y** = Opus cost ÷ retrained cost (bar ≥ 5) | | | **18.2 [17.0, 19.4] → pass** |

Same-day only (the POC 13 retrained session against both Opus sessions, `m15-same-day.json`): X 0.81 [0.33, 1.59]
unresolved, Y 14.8 [13.8, 15.7] pass.

Per session (each 44 × 4, `matrix.json` in each directory):

| Session | Model | pass^1 | pass^4 | Cost / trial | Provenance |
|---|---|---|---|---|---|
| `data/poc12/p12-heldout` | retrained | 0.511 | 0.182 | $0.0058 | asserted, not recorded |
| `data/poc12/p12-heldout-r2` | retrained | 0.449 | 0.233 | $0.0041 | asserted, not recorded |
| `p13-heldout-retrained` (same day as Opus) | retrained | 0.472 [0.369, 0.574] | 0.159 [0.068, 0.273] | $0.0069 | recorded: both servers report `/home/ubuntu/p12/merged-gate` |
| `p13-heldout-opus` | Opus 5.5 | 0.483 [0.381, 0.585] | 0.182 [0.068, 0.295] | $0.1035 | recorded |
| `p13-heldout-opus-r2` | Opus 5.5 | 0.500 [0.403, 0.602] | 0.186 [0.093, 0.302] | $0.0997 | recorded |

For context (not part of the claim): the untrained base (`p11-heldout` + `p12-heldout-base`) against the same Opus
sessions gives X 0.90 [0.45, 1.69].

**Why X is unresolved, and what it says.** Opus 5.5 scores the same as the 9B here, base or retrained. Per-task pass
rates of the two models correlate at r 0.61 (pooled), close to the agreement of a model with itself across sessions
(Opus 0.75, retrained 0.69): which tasks pass is set mostly by the task and the simulated user, not by the policy.
10 tasks fail for both (≤ 25 %), 7 pass for both (≥ 75 %); Opus is much better on 3 (21, 70, 94), the 9B on 1 (40).
In failed trials of both arms the usual pattern is a gold action never taken (Opus 158 of 178 failures, same-day
retrained 86 of 93). With pass^4 near 0.19 for both and 44 tasks, the ratio's interval spans 0.55–1.63; the bar sits inside
it. The literature found the same: swapping only the user simulator moved τ-family scores by 15 points with a frozen
agent (alphaXiv 2609.38043). This run does not show that the 9B matches Opus; it shows that under this open-weight
simulator (gemma-3-27b-it) and judge the benchmark cannot tell them apart.

**Excluded trial.** `run_retail-13_27` (`p13-heldout-opus-r2`): `harness_http_error`, the job failed at start with
`SSLError … PEM lib` loading its certificate, before any model call. Infrastructure, not the agent; `n` 7 for task 13.

## Opus arm

| | Session 1 | Session 2 |
|---|---|---|
| Policy calls | 1,589, all 200 | 1,628, all 200 |
| Stop reasons | tool_use / end_turn only (no `max_tokens`, no refusal) in 3,217 calls | |
| Pins on every row | effort medium, max_tokens 16000, cache on (3,217 / 3,217) | |
| Fields not sent | `temperature`, `seed` (every call) | |
| Billed tokens: input / cache write / cache read / output (thinking) | 4,578 / 1,861,232 / 12,346,644 / 321,050 (41,847) | 4,684 / 1,650,710 / 13,094,628 / 333,225 (46,150) |
| Billed cost | $18.21 ($0.1035 / trial) | $17.56 ($0.0997 / trial) |
| Uncached ceiling (all prompt tokens at input price) | $0.3595 / trial | $0.3731 / trial |
| M13 median / p90 | 46 / 64 s | 50 / 70 s |
| M2 | 176 / 176 | 175 / 176 |

Prompt caching read 87 % of prompt tokens; without it Opus costs 3.5× more per trial and Y would be ≈ 52–65×. Opus
needs fewer policy calls per trial than the 9B (9.1 vs 12.5).

## Deviations from "same scaffold" (forced, decided before the run)

- `temperature` is not sent to Opus: Opus 5.5 rejects any value (400). The open arms run at temperature 0.
- Thinking cannot be disabled on Opus 5.5; the open arms run non-thinking. Thinking was 13 % of Opus output tokens.
- τ²'s history has no slot for thinking blocks, so Opus never sees its own earlier reasoning (accepted by the API).
- `seed` is not sent (the Messages API has none).
Everything else (split, simulator, NL judge, seeds, 4 trials, `llm_agent`, concurrency 32, `TIMEOUT_S` 1200) is
the POC 11 configuration. No closed-model output entered any training or label store (`train select` and `label`
refuse closed policies by name, `eval/tests` T1/T2/E1).

## Acceptance

`eval/tests/run.py`: `--selftest` 9/9; `--quick` 23 pass, 3 skip (host/live); `--live` L1 pass; `--host` H1, H2 pass
twice in a row on the fleet host without clearing state (two real Opus trials, $0.09 each). Regressions: fleet
`--quick` 25/25 and `--host` 33/33; train 13/13, label 29/29, retrain 39/39 quick; `gateway.py check`.

Found on the real host and turned into checks (the suite's fakes were more permissive than the real upstreams):
vLLM's `/v1/models` needs the policy key (401 without it); hosted providers list model ids without a checkpoint
`root`.

## Spend

Anthropic API ≈ $37 (two sessions $35.77, probes and host checks ≈ $1). AWS: fleet host ≈ 2.2 h ≈ $5.6, two L40S ≈
1.5 h each ≈ $5.6. Total ≈ $48.

## What this does not prove

- **That the 9B matches Opus.** X's interval contains 0.75 and 1; the simulator sets most outcomes. A stronger or
  different simulator could separate them; it was not tried (claim 3 fixes one open-weight simulator).
- **Sample size.** 44 held-out tasks; pass^4 near 0.19 leaves the ratio's interval ±0.5 wide.
- **Sessions.** Two Opus sessions, three retrained (two from POC 12, on another day, whose weights are asserted, not
  recorded: their `fleet.json` predates the served-checkpoint record).
- **One effort level.** Opus ran at `medium` only; `high` was not tried, by design (the comparator's strength is not
  chosen from a score).
- **Scores use an open-weight NL judge and simulator** (`docs/conventions.md`), not comparable with the public
  leaderboard; entry declared Custom.
- **Cost basis differs by arm.** The 9B is GPU-hours on two L40S at on-demand price; Opus is list-price tokens with
  caching. The 9B's own token ceiling ($0.0099 / trial) also gives Y ≈ 10.
- **Tester and solver share a model family** with the closed arm (Claude); the tester ran on Opus after the first
  tester (Fable) hit its usage limit.
