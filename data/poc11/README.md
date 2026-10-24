# POC 11 results

Full baseline run of τ²-bench retail with the baseline harness and open-weight models only, 2026-10-24: every train
task × 8 trials (the training data for POC 12) and every held-out task × 4 trials (the baseline for claim 3).
Runner, matrix and gateway are POC 10's (`fleet/CONTRACT.md`, `docs/architecture.md` §10), with the changes below.
Configuration frozen in `docs/versions.md` ("POC 11 run configuration") before the train run, chosen on train data
only, and unchanged for the held-out run (serving command lines byte-compared before it started).

| Role | Model | Served by |
|---|---|---|
| Policy | `Qwen/Qwen3.5-9B` bf16, non-thinking, `qwen3_coder` tool parser, prefix caching | vLLM 0.32.0 on two AWS g6e.xlarge (L40S 48 GB), route list by slot number mod 2 |
| User simulator | `google/gemma-3-27b-it`, T 0 | hosted, through the gateway |
| NL-assertion judge | `deepseek-ai/DeepSeek-V4-Flash-0731` (`max_tokens` 4096, one retry on non-JSON) | hosted, through the gateway |
| Label judge (train only) | `openai/gpt-oss-120b` | hosted, called from the host (outside the gateway) |

Fleet host: AWS m8i.12xlarge (48 vCPU, 192 GB, nested virtualization), concurrency 32 on slots vm-03…vm-34, one
trial = one microVM, τ² `--max-retries 0`, seed 300, agent and simulator at T 0. Prices read on the run day
(`prices.json`): fleet host $2.54016/h, GPU 2 × $1.861/h.

## Baseline matrix

| Metric | Held-out (44 tasks × 4 = 176) | Train (65 tasks × 8 = 520) |
|---|---|---|
| M2 harness_e2e_pass | 176 / 176 | 520 / 520 |
| M8 trace_completeness | 176 complete, 0 incomplete, 0 unverified | 520 complete, 0, 0 |
| **M11 pass^1** | **0.415 [0.318, 0.523]** | 0.419 [0.342, 0.498] |
| **M11 pass^4** | **0.159 [0.068, 0.273]** | 0.152 [0.084, 0.227] (pass^4 of 8 trials) |
| Trials excluded from pass^k | 0 | 0 |
| M12 policy (GPU-hours) | $1.063, **$0.0060 / trial**, $0.0146 / successful trial | $2.803, $0.0054 / trial |
| M12 policy token ceiling (hosted list price) | $0.0097 / trial | $0.0102 / trial |
| M12 simulator, NL judge | $0.173, $0.055 | $0.555, $0.156 |
| M12 label judge | not labelled (held-out is never training data) | $1.772 (1,040 calls, **outside the gateway**) |
| M13 latency_per_task | median 114 s, p90 190 s, worst 865 s | median 139 s, p90 230 s, worst 735 s |
| M14 kept_run_fraction | — | **0.419** by reward (218 / 520); **0.350** after the judge's veto (182 / 520) |
| M16 infra_cost_per_trial | **$0.00412** (5.0× the cheapest baseline) | $0.00368 (3.8×) |
| M9 query_by_principal_ms | 0.03–0.04 s (principals with 4, 20 and 74 events; store 390,886 events) | — |
| M17 density | 32 (two GPUs: host-side 10.9 s at 32, 19.0 s at 48) | — |

pass^k is the mean over tasks of C(c,k)/C(n,k); intervals are a bootstrap over tasks. Train and held-out agree within
noise (pass^1 0.419 vs 0.415), so the train runs are a fair sample of the model's behaviour on held-out tasks.

Retraining data for POC 12: `retrain export` over the 520 train runs, 520 items, 6,428 SFT examples, 0 witness
disagreements; `label run --level 0,1`: 520 judged, 182 kept, framing consistency 0.87.

## Claim 2 fails on this setup

Claim 2 needs Avos at least 20 % cheaper per completed trial (infrastructure only) than every baseline.

| | Held-out, USD per completed trial | Avos ÷ baseline |
|---|---|---|
| Avos, m8i.12xlarge at concurrency 32 | 0.00412 | — |
| AWS t3.small, one VM per job (boot 29.8 s measured, 60 s minimum) | 0.00083 | 5.0× |
| Google Cloud Run | 0.00251 | 1.6× |
| AWS AgentCore Runtime v2 | 0.00511 | 0.81× (Avos 19 % cheaper) |

Avos is 5.0× the cheapest baseline and wins only against AgentCore, so the claim fails. On the train run's own
numbers Avos breaks even with one VM per job at about 121 concurrent VMs per host and meets the bar at about 151; this
host type doubles its host-side trial time already at 48 (19.0 s vs 9.7 s at 4). The held-out figure is worse than the
train figure because its 176 trials end in a tail where few VMs are still running while the host is paid for. A second
GPU shortened model time (98 s median vs 135 s with one) but does not change the ratio: both sides pay for the same
trial wall. The claim is not tested on a setup where the per-VM cost is lower (denser host, cheaper host).

## How the configuration was chosen (train dry-run set, 20 tasks × 4, concurrency 32)

| Choice | Measured | Decision |
|---|---|---|
| Thinking mode (`--reasoning-parser qwen3`) vs non-thinking | pass^1 0.475 vs 0.512, paired difference −0.04 [−0.18, +0.09]; pass^4 0.20 vs 0.15; 1.5× policy tokens, 30 % slower, 5 / 80 empty replies (0 in 600 non-thinking trials) | non-thinking |
| MTP speculative decoding (2 tokens) | identical first replies; +14 % throughput at 32 concurrent per GPU, −8 % at 64 | not used |
| Concurrency 48 vs 32 on two GPUs | host-side 19.0 s vs 10.9 s; 1 gateway timeout at 48, 0 at 32 | 32 |
| Two GPUs vs one | model time 98 s vs 135 s median at 32; GPU cost per trial unchanged per GPU-hour | two |

## Changes in this POC

- **Gateway:** a model may map to a list of upstreams; a call goes to `upstreams[slot mod n]`, so one trial's
  conversation stays on one server; every `llm.call` row names its `upstream`; `gateway.py route MODEL SLOT` prints
  the choice.
- **Matrix causes:** `agent_empty_reply` (an empty reply from a clean 200 policy call is the agent's failure, not τ²
  infrastructure); `agent_timeout` vs `harness_timeout` decided by whether the policy kept answering;
  `witness_mismatch` only on done trials; `hosts.gpu.count` prices every GPU serving the policy.
- Found while choosing the configuration: thinking mode's empty replies were filed as infrastructure and dropped from
  pass^k, which favoured the mode that produced them.

## Acceptance

`fleet/tests/run.py --quick` 25/25; `--host` 33/33 twice in a row on the dev host without clearing state.
Regressions: trace 65/65, retrain 39/39, label 29/29 (local).

## What this does not prove

- **Sample size.** 44 held-out tasks: pass^4 0.159 has the interval [0.068, 0.273]. POC 12 needs a change larger
  than that interval's width, or more trials, to show an effect.
- **Repeatability.** Every trial runs at seed 300 and T 0, so pass^4 measures the serving stack's own nondeterminism
  as much as the model's; a closed model that repeats exactly in POC 13 will look more consistent for that reason.
- **Only the setup measured.** Claim 2's loss is for one host type at one density with two L40S serving the policy.
- **Judge spend outside the gateway.** The label judge's cost comes from its own rows.
- **The NL judge at scale.** 244 judge calls across both runs (176 train, 68 held-out), none failed; the patch held.
- **Train and held-out share a session and servers.** Prefix caches are exact and outputs did not change between
  them, but they are not independent deployments.
- **Tester and solver share a model family** and the same documents.
- **Scores use an open-weight NL judge** (`docs/conventions.md`), so they are not comparable with the public
  leaderboard.

## Tech debt

- The gateway reads the route table only at start (`gateway/install.sh` after a change); its 600 s upstream timeout
  and the matrix's 300 s stall threshold are constants.
- The empty-reply rule matches one fixed τ² error string.
- Earlier POC 10 debt stands (jailer pid-file race, run-directory adoption by snapshot, one runner per host by
  convention, `peak_guest_mem_mib` from host RSS).
