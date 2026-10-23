# POC 10 results

Dry run of POC 1–9 at fleet concurrency on τ²-bench retail, train split only, 2026-10-23. Suite:
`fleet/tests/run.py` (written independently from the targets before the solution). Design: `docs/architecture.md`
§10. Interface: `fleet/CONTRACT.md`. Command: `just poc10` (run + matrix); `just fleet sweep ...` for M17.

Built: `fleet/bin/fleet` (`run`, `matrix`, `sweep`, `m17`; standard library only, no cache, no service), a route
table in the gateway (`/var/lib/avos/gateway.routes.json`: model → upstream; a model with no route is denied
`no-route`, never forwarded; every `llm.call` carries `upstream_status`, `error`, `finish_reason`), and `infra/job` /
`infra/vm` fixes the runner needs (`SLOT`, atomic run id, `aborted` `job.json` with stage and exit code, a host lock
around identity and network setup, `iptables -w`, roles passed to the VM, a busy slot never destroys the VM holding
it). The guest agent runs τ² with `--max-retries 0` so one trial is exactly one simulation.

Hosts (us-east-2, on-demand, prices read 2026-10-23): fleet host m8i.12xlarge (48 vCPU, 192 GB, nested
virtualization) $2.54016/h; policy GPU g6e.xlarge (L40S 48 GB) $1.861/h. Roles: policy `Qwen/Qwen3.5-9B` (bf16,
vLLM 0.32.0, served as `policy-qwen3.5-9b`), user simulator `google/gemma-3-27b-it`, τ² NL-assertion judge
`deepseek-ai/DeepSeek-V4-Flash-0731`, label judge `openai/gpt-oss-120b` (both hosted). `docs/versions.md` has the flags.

## Claim 2 fails on this setup

Claim 2 says running the workload on Avos costs at least 20 % less per completed trial (infrastructure only, model
tokens excluded) than every baseline: one VM per job, and managed serverless sandboxes. On this setup it costs
more than two of the three baselines measured and is 5.0× the cheapest:

| | USD per completed trial | Avos ÷ baseline |
|---|---|---|
| Avos, m8i.12xlarge at concurrency 32 | 0.00508 | — |
| AWS t3.small, one VM per job (boot measured 29.8 s, 60 s minimum) | 0.00102 | 5.0× |
| Google Cloud Run | 0.00323 | 1.6× |
| AWS AgentCore Runtime v2 | 0.00659 | 0.77× (Avos 23 % cheaper) |

Avos would need to be at least 20 % below every baseline; it is 5.0× the cheapest. The one comparison it wins
(AgentCore) does not make the claim. The cause is measured: a single L40S serves the policy for all 32 VMs, so a
trial spends a median 135 s waiting on the model and holds its slot of a $2.54/h host for 147 s; a one-VM-per-job
baseline pays only for its own small VM over the same time. More VMs per host do not rescue it here: M17 is 16 on
this host type, and model time rises faster than host-side time at every level. This is a result, not a defect to
work around, and it stays in the write-up. It does not show the claim fails on every setup: a host where the model
is not the bottleneck, or a cheaper host, is untested.

## Done tests

| Done test | Result |
|---|---|
| 1. 260-trial run at a stated concurrency; every trial has `job.json`; every non-done trial one cause | `poc10-r2/`: 65 train tasks × 4 at concurrency 32 on slots vm-03…vm-34, 1,857 s. 258 done, 2 failed, each with one cause. |
| 2. One command prints the matrix from the artifacts alone | `fleet matrix --fleet DIR --prices P --labels /var/lib/avos/labels` → `matrix-poc10-r2.txt`, `poc10-r2/matrix.json`; byte-identical on two runs; equal to the suite's oracle on real and synthetic data (10,000 trials in 2.6 s, 80 MiB). |
| 3. Density sweep prints M17 | `m17.json`: levels 4, 16, 32, 64, 80 × the same 80 trials. **M17 = 16.** |
| 4. Claim 1 with 32 VMs running | `supervisor/tests/claim1.py` **8/8** on vm-01/02 while a fleet held 32–34 VMs (sampled every 5 s for the whole suite): M6 40 ms median / 43 ms worst, cut 67 / 86 ms, M7 0, 20,000 gateway calls 0 unanswered (p99 42 ms). |
| 5. retrain and label over the 260 runs; earlier suites pass | `retrain export --runs-file`: 260 items, 3,216 SFT examples, M14 0.50, one witness disagreement (the trial below). `label run --level 0,1`: 260 labelled, 88 kept, 524 judge calls, framing consistency 0.82. Regressions: trace 65/65, retrain 39/39, label 29/29, collect 28/28 on the host (with claim1 and poc4). |
| 6. Policy model picked on the dry-run set | Qwen3.5-9B over Qwen3-8B (20 train tasks × 4, concurrency 32): pass^1 0.54 vs 0.30, pass^4 0.20 vs 0.10 (`pick-*/`). `meta-llama/Llama-3.1-8B-Instruct` not tried (gated). |

Acceptance suite, final pass: `--quick` 23/23 locally; `--host` 29/29 twice in a row on the dev host without
clearing state (kill and resume, unrouted model, live sweep, POC 9 runs through the matrix, cleanup).

## Baseline matrix (`poc10-r2`, 260 trials, concurrency 32)

| Metric | Value | Parts |
|---|---|---|
| M2 harness_e2e_pass | 258 / 260 = 99.2 % | 2 failed: 1 `gateway_timeout`, 1 `witness_mismatch` |
| M8 trace_completeness | 259 complete, 1 incomplete (`witness_mismatch`), 0 unverified | stored ÷ happened per VM, mean ratio 1.0 |
| M11 pass^1 | 0.432 [0.356, 0.508] | bootstrap over 65 tasks, seed fixed in the contract |
| M11 pass^4 | 0.141 [0.063, 0.234] | per task C(c,4)/C(n,4); task 67 has 3 valid trials (pass^4 n/a) |
| M11 Avg@4 | 0.432 | equal to pass^1 by construction (binary reward) |
| M11 excluded | 1 trial (infra) | the `gateway_timeout` trial stays in n as a failure |
| M12 policy | $0.960 total, $0.0037 / trial, $0.0085 / successful trial | GPU $/h × run wall; token list-price ceiling $0.0103 / trial |
| M12 simulator | $0.286, $0.0011 / trial | 1,619 calls, 2.49 M + 0.12 M tokens |
| M12 NL judge | $0.080, $0.0003 / trial | 87 calls |
| M12 label judge | $0.885, $0.0034 / trial | 516 label rows of these runs, **outside the gateway** (rows of other runs ignored and named in warnings) |
| M13 latency_per_task | median 147 s, p90 268 s, worst 1,050 s | done trials, at concurrency 32 |
| M16 infra_cost_per_trial | **$0.00508** | $2.54016/h × 0.516 h ÷ 258 |
| M16 baselines | t3.small per VM $0.00102 (boot measured 29.8 s, 60 s minimum), Cloud Run $0.00323, AgentCore v2 $0.00659 | Avos is **5.0× the cheapest** at this density |

## M17 (host-side trial time = wall − Σ `llm.call.ms`)

| Concurrency | Host-side median | ÷ level 4 | Model time median | Wall median | Peak guest memory |
|---|---|---|---|---|---|
| 4 | 9.7 s | 1.00 | 91 s | 101 s | 2.2 GB |
| 16 | 10.2 s | 1.05 | 97 s | 108 s | 8.5 GB |
| 32 | 10.7 s | 1.10 | 135 s | 147 s | 16.8 GB |
| 64 | 26.3 s | 2.71 | 194 s | 219 s | 33.5 GB |
| 80 | 35.0 s | 3.59 | 216 s | 251 s | 41.6 GB |

M17 = 16 by the 5 % rule. Memory is not the limit (≈ 520 MB per guest of the 2 GB given); the knee is CPU: 64 VMs on
48 vCPUs. Model time grows faster than host-side time at every level: one L40S serving the policy is the first limit.

## Found in the real runs, fixed

- **Label rows of other runs** were counted in the label judge's M12. Only the fleet's own trials count, for every
  metric.
- **The gateway's own 600 s timeout was recorded as a 502** and filed as an upstream failure, dropping a policy that
  generated without stop from pass^k. Now `gateway_timeout` and `finish_reason: length` are agent failures in n.
- **Slots collided with claim 1's vm-01/02.** `--slots vm-NN-vm-MM` is part of the plan.
- **τ² retried a whole simulation** (default `max_retries` 3) inside one trial and kept the success:
  `run_retail-60_13` of the first run held two judged conversations; only POC 8's witness check noticed. Retries
  are off; the matrix flags any trial whose result is not one simulation matching the gateway's calls.
- **The runner filled every slot in `--slots`, ignoring `--concurrency`** (a sweep level of 4 ran 80 VMs). Seen by
  counting live VMs during the sweep; no check had |slots| > C. Fixed; the suite now checks peak overlap.
- **The NL judge ran to the provider's 8,192-token default** twice and τ² filed the trial `infrastructure_error`.
  `infra/image/build.sh` now patches the judge to `max_tokens` 4096 with one retry on a non-JSON answer. Both hosts'
  images were then rebuilt by `build.sh` (the runs above used an image with `agent.py` copied in by hand); task 67
  ran twice on the rebuilt image with the judge stopping at ≈ 290 tokens, and `--host` passed 30/30 on it.

The first 260-trial run (`poc10/`, before these fixes) read pass^1 0.505, pass^4 0.172. The fixed rerun reads
0.432 / 0.141 with the same tasks, models and concurrency. The difference is run-to-run noise plus retried
simulations counted as passes in the first run; one rerun cannot separate the two.

## What this does not prove

- **Sample size.** 65 tasks × 4 trials: the pass^4 interval (0.06–0.23) is too wide to detect a 10-point change.
  Same-config reruns differ (0.505 vs 0.432 above). These are shakedown numbers, not a model comparison.
- **The model pick** rests on 80 trials per candidate and two candidates; Llama-3.1-8B was not tried.
- **Claim 2 fails on the only setup measured** (section above). The failure is shown for one host type,
  one policy GPU and this workload; it does not show the claim fails on a host where the model is not the
  bottleneck, which is untested. M17 = 16 holds for this setup only. The baseline prices are list prices read on the
  run day (the t3.small rate confirmed with the AWS Pricing API); Cloud Run and AgentCore are priced from their rate
  cards, not run.
- **Judge spend outside the gateway.** The label judge's cost comes from its own rows, not from gateway records.
- **The NL-judge fix is shown on replays, not at scale.** The `witness_mismatch` trial is DeepSeek's verdict cut by
  `finish_reason: length` (2 of 444 judge calls, both task 67, at concurrency 32) → τ² `infrastructure_error`. After
  the runs the judge got `max_tokens` 4096 and one retry on a non-JSON answer (`docs/versions.md`), checked on 44
  replays of the two prompts and on task 67 in the rebuilt image; no 260-trial run has used it yet.
- **Tester and solver share a model family** and the same documents; the suite's oracle and the solution could
  share a misreading of the contract.
- **Scores use an open-weight NL judge** (`docs/conventions.md`), so they are not comparable with the public leaderboard.

## Tech debt

- A trial whose NL judge fails twice is still filed as `witness_mismatch`; a cause naming the judge would be clearer.
- The gateway reads the route table only at start (`gateway/install.sh` after a change), and its 600 s upstream
  timeout is a constant.
- A SIGTERM that lands while `infra/vm create` is between launching the jailer and writing its pid file can leak a
  Firecracker process. Not seen.
- Run directories are told apart from other users of the same results root by the snapshot taken when the fleet was
  created; a manual `infra/job` of a fleet task between sessions would be adopted.
- One runner per host is the design; nothing beyond `infra/vm`'s busy-slot refusal enforces it.
- `peak_guest_mem_mib` is Σ VmRSS of the fleet's Firecracker processes sampled every 0.2 s, not guest-reported.
- The policy GPU is a single g6e.xlarge; its capacity in us-east-2 was unavailable for minutes at a time.
