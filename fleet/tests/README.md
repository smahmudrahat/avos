# POC 10 acceptance suite

Independent, black-box checks of `fleet/CONTRACT.md`. Written before and without the implementation; the only
things it touches are `fleet/bin/fleet`, a fake `infra/job` the suite runs itself (CONTRACT §9), raw fixtures and,
with `--host`, the real dev host over ssh (read-only except its own scratch under `/tmp/p10-tests`).

```
python3 fleet/tests/run.py --selftest   # the suite's own oracles, fake job and comparator (~5 s)
python3 fleet/tests/run.py --quick      # everything but the 10,000-trial scale check (~3 min once implemented)
python3 fleet/tests/run.py              # + 10,000-trial scale
python3 fleet/tests/run.py --host       # + live checks on avos-dev over ssh (repo pushed to ~/poc), ≤ 15 min, a few cents of tokens
python3 fleet/tests/run.py --only M3    # one prefix
```

One line per check: `PASS|FAIL|SKIP <id> [<target>] <measured vs bar>`. Exit 0 only if every executed check passed.
A missing CLI reads `FAIL … not implemented: fleet/bin/fleet`, never a traceback. Scratch is a fresh `mkdtemp` per
run, removed on success; host scratch is `/tmp/p10-tests`, removed in `finally`; the ssh control socket lives under
`/tmp/p10-*` and is closed in `finally`. Every `--host` wait polls for evidence with a bound; nothing sleeps a fixed time.

## Fixtures (`fixtures/`, hash-gated by `MANIFEST.sha256` at the start of every run and in selftest)

| Path | What |
|---|---|
| `results/run_retail-0_22`, `0_27`, `1_5`, `7_4` | real done runs from the dev host (POC 9 dry run and the new `infra/job`): 14 / 9 / 9 / 102 gateway `llm.call` rows, rewards 1/0/0/0, `user_stop` ×3 and `max_steps` |
| `results/run_retail-0_26` | a real run aborted at `stage: create` by the new `infra/job` (54 ms, exit 2) |
| `prices.json` | the §6 shape with the statement's hosts, four model prices and three baselines (numbers are placeholders read on 2026-10-22; the real run reads its own) |

Everything else is synthesised at run time by `lib.py`: run directories shaped exactly like `infra/job`'s (job.json
per status/stage, result.json or error.log, events.jsonl with gateway rows, completeness.json), 31 named trial
shapes (`lib.KINDS`), fleet directories, label rows, sweep levels, and the fake job.

## The oracle (`lib.py`)

`matrix_oracle` and `m17_oracle` compute every number of CONTRACT §4–§7 straight from the raw files, in ~30 lines
each: causes by the §5 table, pass^k = C(c,k)/C(n,k), the pinned bootstrap (`Random(300)`, B = 2000, indices 50 and
1949), M12 from prices × token sums and GPU $/h × Σ session wall, M13 median/p90/worst, M16 host $/h × wall ÷ done
with the two baseline formulas, M17 on the median host-side time. `diff` compares the solver's JSON to it with
tolerance 1e-9, strings exact, and treats `"unverified"` vs a number as a difference. `--selftest` proves the
oracle on hand-computed cases and that the fake job claims run ids, honours `SLOT`, aborts on SIGTERM with exit
143 and refuses a busy slot.

## Checks → target → bar

| Check | Target | Bar and why |
|---|---|---|
| A0_cli_surface | §1 | no args / unknown sub / `matrix` without `--fleet` / empty fleet dir / non-JSON or missing prices / `m17` on a dir without levels → exit 2; no traceback anywhere; a valid fleet exits 0. |
| R1_refuse_held_out | statement (held-out never run) | `--tasks 10` (held-out), `0,10`, `64` (excluded), `64 --part held_out`, `0 --part held_out`, `999` → exit 2 naming the task, no fleet dir, no run directory. The split is the only authority on what may run. |
| R2_run_concurrency_slots | **done test 1** | 3 tasks × 2 trials at concurrency 2 with 1.2-s fake trials: fleet.json has every §2 key, journal 6 unique runs each with `job.json` + `completeness.json`, summary `planned 6 done 5 failed 1 status done` with exit **0** (a failed trial is a result), peak overlap **= 2** jobs (concurrency really used, wall < 6 × 1.2 s), no job ever saw a busy `SLOT`, `SLOT` ∈ {vm-01, vm-02} and equals the journal's, `MODEL`/`USER_MODEL`/`JUDGE_MODEL` reach the job; re-running a done fleet changes nothing. |
| R3_kill_resume | **done test 1** (restartable) | 6-s fake trials; SIGTERM after two jobs started: runner exits ≤ **60 s**, no fake job left running, no slot lock, `status: stopped` with the session ended, ≥ 1 `interrupted` line whose `job.json` is `aborted`. Resume: exit 0, `done`, exactly 2 non-interrupted unique trials per task, every fleet directory journaled, a pre-existing `run_retail-0_0` **not** adopted, re-run count = interrupted + unfinished, `--trials 3` on the same dir → exit 2 and no new directory. 60 s is the real `infra/job` abort path (destroy VM ≤ 5 s) with margin. |
| R4_sigkill_adopt | §3 resume | SIGKILL mid-run; the orphaned jobs finish by themselves; one planted directory without `job.json`: on resume the finished orphans are adopted as they are (non-interrupted, `done`), the empty one gets `stage: unknown`, is journaled interrupted and re-run; exactly 4 jobs were ever started (nothing twice). |
| R5_busy_slot_isolation | class A (a failing job never touches resources it does not own) | fleet A runs a 5-s trial on vm-01; fleet B (same results root, concurrency 1) is started and meets the busy slot: A's trial still ends `done`, rc 0, its directory byte-identical; B's busy-slot trial is journaled `aborted`, cause `vm_create_failed`, never done; no refusal on a slot nobody held. |
| R6_roles_reach_the_trial | class B (declared roles = models in the rows) | 4 live trials: status-200 models ⊆ the declared roles and the policy present, no cause. The same artifacts with the declared policy changed → every trial `role_mismatch` (infra, invalid, still `done` for M2), M11 `unverified`; a done trial with one extra undeclared model row → `role_mismatch`. A runner that drops a role can no longer be counted. |
| M10_label_rows_own_runs | class A (only the fleet's own evidence) | 2 label rows of this fleet + 3 of other runs (POC 9 leftover, another task, another domain): `label_judge` counts 2 calls / 3000 prompt tokens, warning `3 label rows for runs outside this fleet ignored`; only foreign rows → 0 calls, $0.0. Spend of other runs can never land on this fleet. |
| M11_gateway_evidence_causes | class B (upstream error vs gateway timeout vs length) | `gateway_timeout` and a 5xx row without `upstream_status`/`error` evidence are class *agent* (stay in `n`, pass^1 0.75 on a task with one), `upstream_5xx` needs `upstream_status ≥ 500` and `upstream_unreachable` an `error` saying so (both excluded), a 200 row with `finish_reason: length` on a failed trial is `agent_length`; a done trial with a length row has no cause. A long generation cannot raise pass^k by being excluded. |
| M12_one_simulation_per_trial | class D (store = witness) | τ²-retried simulation (store holds two conversations, witness one), a tampered usage, two saved simulations, retried + failed: each `witness_mismatch`, infra, excluded, `passed` null, `m8.status witness_mismatch` counted as incomplete; M2 still counts them done. |
| R7_slot_ranges | class C (claim 1 and the fleet share a host) | two fleets with `--slots vm-03-vm-04` and `vm-05-vm-06` run side by side on one results root: no busy slot, peak overlap ≥ 3, every job's `SLOT` inside its fleet's range, vm-01/vm-02 never used; range smaller than concurrency → exit 2; a different range on resume → exit 2. H1 runs its live fleet on `vm-03-vm-04` and asserts the jail list never contains vm-01/vm-02. |
| R8_concurrency_cap_wide_slots | class E (live jobs ≤ C whatever the range) | the fake job logs start/end: 12 slots with `--concurrency 3` → peak overlap exactly 3 and slots inside the range; SIGTERM + resume with 18 slots / C=2 → the resumed jobs peak at 2; `sweep --levels 1,2,4 --slots vm-03-vm-12` → each level's trials peak at exactly its level and the sweep never exceeds 4. Found live: a sweep level ran at `|slots|` = 80 instead of 4. |
| M1_matrix_real_fixtures | **done test 2** (real data) | matrix over the 5 real directories equals the oracle in every part; 102 and 14 llm rows counted from the gateway's rows; `0_26` → `vm_create_failed`; M2 = 4/5 (an aborted trial stays in); M8 `unverified` ×5 and `mean_ratio` `unverified` (no completeness.json: never 100 %); nothing written into the results root; text shows `4/5`, `unverified` and the cause. |
| M2_synthetic_all_kinds | **§5**, M11, M12 | 31 shapes across 14 tasks: every non-done trial has exactly the §5 cause (gateway evidence before job stage before τ² reason), done trials none; a task with all-infra trials has `n = 0` and is listed, a task with 2 valid trials has `pass4 = null` and is listed in `tasks_short_4` (never dropped), `C(3,4)/C(5,4)` and Avg@4 by hand; rows under a model matching no role land in `unattributed` with a warning; label judge without rows is `unverified` and `outside_gateway`. |
| M3_passk_oracle | **M11** arithmetic | 3 random fleets × 12 tasks (3–8 trials, mixed agent/infra failures): per-task `n`, `c`, `infra`, pass^1, pass^4, Avg@4 and the overall means and bootstrap intervals equal the oracle to **1e-9**; every point estimate lies inside its interval. The bootstrap is pinned so the interval is a number, not a mood. |
| M4_unverified_everywhere | statement (missing → `unverified`) | prices `{}`: every M12 role and M16 `unverified`, M2/M11 still computed; one model price and the GPU price removed: only that role / the GPU basis unverified while the token ceiling stays; a baseline missing a field: that baseline only; an all-infra fleet: pass^1 and its interval `unverified` (never 0), M13/M16 `unverified`, M2 = 0/4, cost per successful task `unverified`; label rows via `--labels`: usd = Σ tokens × price (hand), flagged outside the gateway; text output says `unverified`, never `null`/`None`. |
| M5_determinism | **done test 2** | 3 runs with fresh `HOME`/`TMPDIR` plus a copy of the fleet dir at another path with mtime 1970: text and JSON byte-identical; no scratch path or today's date in the JSON; `inputs.prices_sha256` = sha of the prices file. |
| M6_cache_tamper | §4 caches | after a warm run: `"reward": 1.0 → 0.0` in a job.json, `"status":200 → 502` in an events row, `"ratio": 1.0 → 0.5` in a completeness.json, each **same size and same mtime**: the matrix changes exactly as the oracle says. Then everything the tool wrote (fleet dir, `TMPDIR`, `HOME`) truncated / bit-flipped / swapped / deleted: output byte-identical to the clean run or exit 1 with an error. A size+mtime fingerprint fails this. |
| M7_duplicates_and_missing | §4 | a run id journaled twice counts once (warned); a line whose directory does not exist is a trial with `status: missing`, cause `no_job_json` (stays in M2's denominator); an `interrupted` line is ignored; 5 trials total. |
| M9_wrong_model_names | §2 roles, M12 | `policy == simulator` in fleet.json → matrix exit 2; `fleet run --policy a/b --simulator a/b` → exit 2; roles renamed so no row matches: every role has 0 calls and `usd_total 0.0`, all tokens under `unattributed` with the three names. Tokens are never guessed onto a role. |
| S1_scale_696 | POC 11 size | 65 × 8 + 44 × 4 = 696 synthetic trials: matrix ≤ **20 s**, peak RSS ≤ **512 MiB** (POC 6 §10's bar, as in every later suite), equal to the oracle, second run identical. One command, not a batch job. |
| S2_scale_10k (full only) | §4 scale | 10,000 trials (≈ 15 × POC 11, 122 MiB of run directories): ≤ **180 s** and ≤ 512 MiB. The oracle itself needs 2.5 s, so 180 s leaves room for a careful, streaming reader but not for a per-trial subprocess. |
| D1_m17_synthetic | **done test 3 / M17** | planted sweeps: flat → M17 = 8, ceiling not reached; knee (10.0, 10.2, 10.45, 12.0, 20.0 s) → M17 = 4, levels in numeric order (`level_16` after `level_8`, not after `level_1`); two outliers per level that push the **mean** over +5 % but not the median → M17 unchanged; raw wall up 60 % with host-side flat → within M17 (host-side, not wall); peak null → `unverified` (never 0); a level with only failed trials → `unverified` and M17 stops before it; unreadable baseline → M17 `unverified`; every output equals the oracle and is deterministic. |
| Z_fixtures_untouched | fixtures read-only | every fixture file matches `MANIFEST.sha256` after the run. |
| H0_preflight | class B | ssh works, `fleet/bin/fleet` is on the host, no runner / firecracker / jail before; records the baseline counts of `tap-vm*` links, `tap-vm` iptables rules, jail dirs, SPIRE entries, firecracker processes. |
| H1_kill_resume_live | **done test 1** on the real host | 2 tasks × 2 trials at concurrency 2, 1 GiB VMs, dev model: waits (≤ 8 min, 5-s polls) for the first journal line, SIGTERMs the runner, waits (≤ 90 s) for it to be gone; every leftover count equals the baseline; `status: stopped`; resume (≤ 12 min) ends `done` with 4 unique trials each with `job.json`; counts equal the baseline again; `fleet matrix` on the host equals the oracle run on the host; every non-done trial has a cause; done trials carry **≥ 2** distinct model names with status-200 rows (policy and simulator reach different upstreams); M8 verified for every live trial (completeness.json written). |
| H2_unrouted_model_live | §8 | 1 trial with `--policy nobody/no-such-model-9b`: the run's events hold an `authz.decision` deny with `no-route` naming the model, **0** `llm.call` rows with status 200 for it, cause `model_unrouted` (infra, invalid); no key-like string in the run directory, journal or log; no leftovers. |
| H3_sweep_live | **done test 3** on the real host | `fleet sweep --levels 1,2 --tasks 0,1 --trials 1`: both levels with a positive host-side median, wall > host-side (model time really subtracted), peak guest memory within [32, 1600] MiB × level, `fleet m17` reproduces the sweep's output, equals the oracle on the host; no leftovers. |
| H4_matrix_poc9_real | **done test 2** over real data | a fleet dir built on the host over the 80 POC 9 dry-run runs + `0_26` (aborted create) + `2_1` (τ² infrastructure_error, no events) + `2_0` (no job.json): matrix equals the oracle, those three causes as expected, M8 `unverified` (no completeness.json), deterministic. |
| HZ_cleanup | class B | scratch removed, no runner, counts equal the baseline, live section ≤ **15 min**. |

## Ambiguities resolved here (also stated in CONTRACT.md)

- **A trial the runner itself cut short** (its own SIGTERM) is `interrupted: true`: listed, not a trial of the plan,
  re-run. Otherwise a restart would count as an infrastructure failure of the system under test.
- **Cause precedence**: gateway evidence (no-route deny, other deny, 5xx, 4xx) beats `role_mismatch`, which beats the
  job stage, which beats the τ² termination reason; `unknown` is class *agent* so an unexplained failure never
  inflates pass^k. `role_mismatch` is the one cause a `done` trial can carry.
- **Gateway evidence** (§8): every `llm.call` carries `upstream_status`, `error`, `finish_reason`. Without them a 5xx row cannot exclude a trial (`gateway_5xx_unattributed`, agent); the gateway's own timeout is the agent's behaviour (τ² sets no `max_tokens`).
- **One simulation per trial**: the witness's usage-bearing messages must equal the store's policy+simulator 200 rows in order and tokens; otherwise `witness_mismatch` (infra, excluded, M8 not complete).
- **Label rows** count only when their `run` is one of the fleet's trials; foreign rows are warned, never priced.
- **Resume** starts exactly `planned − journaled non-interrupted` jobs (an interrupted trial is one of the missing).
- **Warnings** name every duplicated run id, every missing directory / `job.json`, every unattributed model.
- **τ² `timeout` and `user_error`** are infra (the environment or simulator stopped the trial), the agent reasons
  (`agent_error`, `too_many_errors`, `context_window_exceeded`, `max_steps`, `agent_stop`, `user_stop` with a null
  reward) are agent and stay in `n`.
- **M12 denominators** are all trials started (every trial consumed the GPU and the simulator); M16's is done trials
  as the metric says; M13 is over done trials (an aborted-at-create 54-ms wall is not a latency).
- **Fleet wall** = Σ over runner sessions (`ended − started`), so idle slots count and a night between sessions does not.
- **M8 off-host**: the runner snapshots `avos completeness` into `<run>/completeness.json`; the matrix reads only that.
