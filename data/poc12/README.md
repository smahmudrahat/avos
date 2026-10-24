# POC 12 results

Retrain the open-weight policy on its own kept train runs, run the held-out benchmark again with the retrained
model, compare with the baseline. 2026-10-24. Tools: `train/` (`train/CONTRACT.md`, `docs/architecture.md` §11),
`fleet run --agent-temperature`, `fleet compare` (§10). Training settings frozen in `docs/versions.md` ("POC 12
retraining") before the fit; every choice made on train data only.

## Result

The retrained model does **not** beat its baseline on held-out tasks by a margin the data can show. Both point
estimates are higher and both intervals include zero.

Held-out, 44 tasks, two sessions per arm (4 trials each, pooled to 8 per task; one trial excluded, see below), paired
by task, percentile bootstrap over tasks (seed 300, 2,000 resamples), `pooled.json`:

| | Baseline (Qwen3.5-9B) | Retrained (LoRA) | Retrained − baseline |
|---|---|---|---|
| **pass^1** | 0.440 [0.341, 0.543] | 0.481 [0.386, 0.580] | **+0.040 [−0.026, +0.108]** |
| **pass^4** (C(c,4)/C(n,4), n 7–8) | 0.177 [0.090, 0.279] | 0.211 [0.119, 0.312] | **+0.034 [−0.049, +0.113]** |
| Tasks better / worse / equal (pass^1) | | | 19 / 15 / 10 |

Per session (each 44 × 4, same fleet configuration as POC 11, `matrix.json` in each directory):

| Session | Model | pass^1 | pass^4 | Excluded |
|---|---|---|---|---|
| `data/poc11/p11-heldout` | baseline | 0.415 [0.318, 0.523] | 0.159 [0.068, 0.273] | 0 |
| `p12-heldout-base` (control, same day as the retrained runs) | baseline | 0.466 [0.358, 0.580] | 0.227 [0.114, 0.364] | 0 |
| `p12-heldout` | retrained | 0.511 [0.409, 0.614] | 0.182 [0.091, 0.295] | 0 |
| `p12-heldout-r2` | retrained | 0.449 [0.341, 0.557] | 0.233 [0.116, 0.349] | 1 |

**Why two sessions per arm.** The first retrained run against the POC 11 baseline gave pass^1 +0.097 [+0.034,
+0.165] (`compare-p11-vs-p12.json`). The task bootstrap does not include session-to-session variance: the same base
weights and flags gave 0.415 and then 0.466 on the same tasks (+0.051), because the user simulator does not repeat
at temperature 0. A second retrained session gave 0.449. One run per arm would have reported noise as a gain.

**Excluded trial.** `run_retail-110_14` (`p12-heldout-r2`): `sim_infrastructure_error`; both NL-judge calls returned
4,096 completion tokens (the limit), so the run could not be graded. The judge is part of the environment, not the
agent; the trial leaves `n` for that task (7 instead of 8).

| Metric | Baseline sessions | Retrained sessions |
|---|---|---|
| M2 harness_e2e_pass | 176/176, 176/176 | 176/176, 176/176 |
| M8 trace_completeness | 176 complete each | 176 complete each |
| M12 policy, GPU-hours | $0.0060, $0.0079 / trial | $0.0058, $0.0041 / trial |
| M12 policy, hosted token ceiling | $0.0097, $0.0102 / trial | $0.0099, $0.0100 / trial |
| Policy completion tokens | 444,711; 447,308 | 423,242; 426,282 (−5 %) |
| M13 latency median / p90 | 114 / 190 s; 141 / 234 s | 112 / 166 s; 108 / 158 s |
| Collapse check (`export/stats.json`): tool-call share, top (tool, index) share, exact duplicates, policy turns per run | 0.596, 0.761, 2, 12.2; 0.601, 0.773, 0, 12.7 | 0.598, 0.773, 0, 12.5; 0.594, 0.761, 2, 12.5 |

Policy GPU cost follows each session's wall time, which varies with the simulator; the token ceiling (the same
prices for both arms) shows the cost per trial is unchanged. No sign of collapse: the retrained runs' diversity
statistics fall between the two baseline sessions' (no shortcut flagged; the 0.9 threshold is far off).

## Training

| | |
|---|---|
| Note | `LoRA · Qwen/Qwen3.5-9B · 265 passed runs` |
| Data | kept ∧ train runs of two train fleets, 17 train tasks held back (below): 132 from `p11-train` (policy T 0, POC 11) + 133 from `p12-train-t07` (policy T 0.7, top_p 0.8); 3,089 policy turns, 24.6 M tokens (`data-fit/manifest.json`) |
| Prompt parity | every policy turn re-rendered with the served tokenizer and chat template reproduces the gateway's `prompt_tokens` and `completion_tokens` exactly (3,089 / 3,089) |
| Fit | 160 optimizer steps, 2 epochs, 1,275 packed sequences; loss 0.062 (step 1) → 0.020 (step 160), `fit-gate/log.jsonl` |
| Wall, cost | 15,295 s on one L40S, 4.25 GPU-hours × $1.861 = **$7.91** (`fit-gate/card.json`); peak 32.0 GiB |
| Merge | 248 modules into the base shards, 775 tensor keys = base, tokenizer and template byte-identical; two independent merges byte-identical (`merged-gate/merge.json`) |

**Sampled train runs (`p12-train-t07`).** 65 × 8 with the policy at T 0.7 / top_p 0.8 and the simulator at T 0:
pass^1 0.448 (T 0: 0.419), 233 reward-passes, 178 kept after the judge (consistency 0.84), kept runs on 49 tasks, 6
of them with no kept run at T 0 (35, 58, 60, 71, 98, 111). Reason: at T 0 the base model already gives its own
targets 99.7 % of the time as the argmax (mean NLL 0.050), so training only on those runs could hardly change it;
sampled passes add tokens the model would not have chosen greedily.

**Regression gate (train tasks only).** 17 train tasks (users disjoint from the fitted ones) were left out of the fit
and run × 4 with the retrained model before any held-out run: pass^1 0.485 vs 0.463 (POC 11, × 8), paired +0.022
[−0.103, +0.162]; rule (pre-registered) ≥ −0.10 and no new empty replies: passed. The gated checkpoint ran on
held-out unchanged; nothing was tuned after a held-out number was seen.

## Labels (M10, second loop)

The POC 11 train gold set (60 runs, three strata) was labelled blind by **Opus 5.5 as a proxy for a human
labeller** (rows `human:opus-proxy`): kappa judge/proxy **0.55 [0.34, 0.76]** (n 60), kappa reward/proxy **0.38
[0.13, 0.62]** (n 57) (`p11-train-labels-p12/summary.json`). This is agreement with a closed model standing in for a
person, not human agreement. The proxy's verdicts only decide keep/drop for those 60 runs under the label tool's
human rule (25 runs); no closed-model text is training data. Kept after the proxy: 178 (was 182).

M14 second loop (kept-run fraction with the retrained model on train) was not run.

## Acceptance

`train/tests/run.py --quick` 13/13; `--host` on the training host 18/18, three times in a row without clearing
state (H1 select byte-equal to the local run; H2 identity adapter; H3 a 2-step fit; H4 merge; H5 the identity merge
serves token-identical replies to the base on 6 replayed requests, 306 completion tokens; no process, port or
scratch directory left behind). `fleet/tests/run.py --quick` 25/25.

## What this does not prove

- **Sample size.** 44 held-out tasks; even pooling two sessions per arm, the pass^1 interval is ±0.07 around the
  difference. A gain under ~0.07 cannot be shown on this split.
- **Session variance dominates.** Same weights, same flags: 0.415 vs 0.466. Two sessions per arm is still a small
  sample of sessions.
- **One recipe.** One LoRA configuration, one round, one-hot cross-entropy on the model's own tokens; no search over
  learning rate, epochs or data mix (by design: nothing was chosen on held-out).
- **The sampled runs' labels.** They come from the same judge whose agreement with the proxy is 0.55.
- **The M10 "human".** Opus 5.5, not a person. Its agreement with the user's own label was 0 of 1 in POC 9.
- **Tester and solver share a model family** and the same documents.
- **Scores use an open-weight NL judge** (`docs/conventions.md`), not comparable with the public leaderboard.

## Tech debt

- `train fit` runs one sequence per forward pass (about 96 s per 16-sequence step on one L40S); `causal_conv1d` is
  not installed (the reference implementation is used).
- `train merge` needs about 19 GB of host RAM per shard pass.
- `train select` keeps no cache and re-renders every run (18 s for 520).
- Fleet's default policy role is the dev model; a fleet run without `--policy` silently uses it (caught here by
  `select`'s provenance check, not by `fleet run`).
