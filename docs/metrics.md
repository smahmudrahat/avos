# Baseline metrics (M1–M17)

Frozen 2026-10-18. Every POC's done test names the metrics it touches.

| ID | Metric | Definition and unit | First measured | Moved by |
|---|---|---|---|---|
| M1 | vm_boot_ms | Create request → agent process running, ms (median, worst of 20). | POC 1 | POC 2, POC 10 |
| M2 | harness_e2e_pass | Share of ephemeral jobs that end with a result file, %. | POC 2 | POC 10 |
| M3 | identity_coverage | Share of agent/sub-agent processes with an SVID and a ledger row, %. Target 100. | POC 3 | POC 5, POC 7 |
| M4 | identity_issue_ms | Process start → SVID in hand, ms. | POC 3 | POC 5 |
| M5 | authz_attributable_denials | Share of denials carrying principal + policy ID + arguments, %. Target 100. | POC 4 | POC 7 |
| M6 | stop_latency_ms | Stop command → branch frozen (`cgroup.events` frozen 1), ms. | POC 5 | — |
| M7 | sibling_disruption | Sibling events lost or delayed during a stop, count. Must be 0. Precisely (2026-10-21, after `just poc5-baseline` showed what happens with no control): every failed sibling call, at any time, plus every sibling tick late by more than 100 ms within 1 s of a halt or resume. | POC 5 | — |
| M8 | trace_completeness | Events stored ÷ events that happened (gateway and supervisor counters, harness messages), %. | Defined POC 6, measured POC 7 | POC 10, POC 11 |
| M9 | query_by_principal_ms | Time to answer "what did principal X do" over one day of events, ms. | POC 7 | POC 11 |
| M10 | label_quality | Cohen's kappa of judge vs human and reward vs human on the gold set, with Cohen's (1960) standard error and 95% interval. `unverified` (parts printed, no value) below 50 human labels or when the expected agreement is 1. | POC 9 | POC 12 (second loop) |
| M11 | pass^1, pass^4 | τ²-bench retail held-out, k trials per task, with bootstrap intervals. | POC 10 (subset), POC 11 (baseline) | POC 12; compared in POC 13 |
| M12 | cost_per_task, cost_per_successful_task | USD per trial (open models: measured on one self-hosted stack, API list price as ceiling; closed: API list price); USD ÷ pass rate. | POC 10 | POC 12; compared in POC 13 |
| M13 | latency_per_task | Wall-clock seconds per trial. | POC 2 (first reading), POC 10 | POC 12 |
| M14 | kept_run_fraction | Passed ÷ total training-split runs, %. | POC 11 | POC 12 (second loop) |
| M15 | ratio_to_frontier, cost_ratio | X = retrained pass^4 ÷ Opus pass^4; Y = Opus cost/task ÷ retrained cost/task. | POC 13 | — |
| M16 | infra_cost_per_trial | (Host $/h × wall hours including idle) ÷ completed trials, excluding model tokens, USD. Compared with a per-VM-per-job baseline and a serverless-sandbox baseline. | POC 10 (first reading), POC 11 | — |
| M17 | density | Max concurrent microVMs on one host before trial latency rises more than 5%. Quoted next to M16. | POC 10 (sweep), POC 11 | — |

**M14 after veto (2026-10-22, POC 9).** POC 8's `m14` stays passed ÷ train-split runs from the reward alone. The
labelled manifest adds `labels.m14_after_veto`: train-split runs kept by the POC 9 rule (reward passed, structurally
sound, not vetoed by the judge unless the user settled it, τ² #384 tasks only with a human pass) ÷ train-split runs.
POC 11 reports both.

**M17 on host-side time (2026-10-22, POC 10).** "Trial latency" in M17 is the host-side trial time: trial wall minus
the sum of `llm.call.ms` of that trial (the gateway records it per call). Reason: in the POC 9 dry run the trial wall is
almost all model time, so a raw-wall knee would measure the model server's saturation, not microVM density. No earlier
POC measured M17, so nothing is recomputed. M13 stays raw wall.
