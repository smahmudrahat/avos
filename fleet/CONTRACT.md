# POC 10 fleet contract

The black-box interface `fleet/tests/run.py` exercises. Everything below is required; nothing about internals is.
Serves: POC 10 done tests 1–3 (`data/poc10/README.md`) (the 260-trial run at a stated concurrency, the one-command
matrix, the density sweep), claim 2 (M16, M17) and the M11/M12/M13 numbers the later POCs compare against.
Written before and without the implementation.

Every CLI: `python3 fleet/bin/fleet <sub> ...`, Python 3.10+ standard library only, no network except what
`infra/job` itself does. Exit `0` success, `1` the input fails a check named on stdout/stderr, `2` usage or
unreadable input. Never a traceback on stderr. Machine output on **stdout** as JSON when `--json` is given.

## 1. Files

| Path | Role |
|---|---|
| `fleet/bin/fleet` | `run`, `matrix`, `sweep`, `m17` (§2–§7). |
| `gateway/` | routes model calls by model name (§8). |
| `fleet/tests/` | this suite; its `fixtures/` are read-only for the solver. |

## 2. The fleet directory (`DIR`)

`fleet run --out DIR` creates and owns `DIR`. It is the only input of `matrix` and `m17`; **every number in the
matrix is computed from `DIR`, the run directories it names and the prices file, nothing else** (no host state,
no network, no environment variable). Contents:

`DIR/fleet.json` (rewritten atomically; every key below required):

```
{"fleet": <name>, "harness": "tau2", "domain": "retail",
 "tasks": ["0", "1", ...],                 task ids as strings, in the order given, no duplicates
 "trials_per_task": N, "concurrency": C, "mem_mib": M,
 "slots": ["vm-03", "vm-04", ...],         the slot range the fleet may use (`--slots`), |slots| ≥ C
 "split": {"path": <as given>, "sha256": <sha of the file>, "part": "train"|"held_out"},
 "roles": {"policy": <model>, "simulator": <model>, "nl_judge": <model>},   three distinct names
 "results_root": <absolute path>,          where infra/job writes run directories
 "sessions": [{"started": TS, "ended": TS|null, "pid": int}, ...],   one per `fleet run` invocation, in order
 "status": "running"|"stopped"|"done",
 "peak_guest_mem_mib": number|null}        peak over the run of Σ resident memory of the live VMs (null when not measured)
```

`TS` = `YYYY-MM-DDTHH:MM:SS.mmmZ`, host clock, UTC.

`DIR/trials.jsonl`: append-only journal, **one line per trial that reached its end**, in end order:

```
{"task": "0", "run": "run_retail-0_27", "slot": "vm-01", "status": "done"|"failed"|"aborted",
 "interrupted": bool, "started": TS, "ended": TS}
```

`run` names `<results_root>/<run>/`, written by `infra/job` (`job.json`; `result.json` or `error.log`;
`events.jsonl`). After the trial the runner adds `<run>/completeness.json` = the stdout of
`collect/bin/avos completeness --run <run>` (M8 record; `status` ∈ `complete|incomplete|unverified`).
Extra keys anywhere are allowed and ignored.

**Trial accounting.** The plan is `tasks × trials_per_task` trials. A journal line with `interrupted: false` is one
trial of the plan; its run directory is a trial directory of the fleet. A line with `interrupted: true` is a trial
the runner itself cut short (its own stop, §3) and is **not** part of the plan: it is listed, never counted, and its
`(task)` is run again. `fleet run` ends with `status: done` iff the journal holds exactly `trials_per_task`
non-interrupted lines per task, with pairwise distinct `run` ids, and every one of those run directories has a
`job.json`. A run id never appears on two non-interrupted lines. Every run directory the fleet created under
`results_root` for one of its tasks is on some journal line (none is silently dropped). Run ids are
*discovered*, not assigned: `infra/job` claims `run_<domain>-<task>_<trial>` itself (lowest free trial number by
`mkdir`), so the runner must tell its own directories from pre-existing ones (snapshot them at start).

## 3. `fleet run`

```
fleet run --out DIR --tasks T[,T...] | --part train|held_out  --trials N --concurrency C
          [--split data/splits/retail_user_disjoint_v1.json] [--results ROOT] [--mem-mib M]
          [--slots vm-NN-vm-MM] [--policy MODEL] [--simulator MODEL] [--judge MODEL] [--job PATH] [--name NAME] [--json]
```

- `--slots vm-NN-vm-MM` (default `vm-01-vm-CC`): the only slots the fleet's jobs may receive as `SLOT`; the range
  must hold ≥ `C` slots (else exit 2). The range bounds *which* slots a job may get, never *how many* jobs run:
  at most `C` jobs are live at any instant, on a first run, a resume and every sweep level (a level runs at its
  own `L`, never at `|slots|`); M13 and M17 are meaningless otherwise. A fleet on `vm-03-vm-34` leaves `vm-01`/`vm-02` to `supervisor/tests/claim1.py`
  (done test 4); two consumers with disjoint ranges share a host without ever sharing a slot. The range is part of
  the plan a resume must repeat.

- Every task must belong to the chosen `--part` of the split (`--part train` default; an explicit `--tasks` list
  is checked against that part). A task outside the part (held-out while `--part train`, or an excluded id such as
  `64`) → exit 2, nothing created, the offending ids named. The held-out split is never run by accident.
- Runs `--trials` trials of every task through `infra/job run tau2 retail/<task>` (`--job` substitutes the
  executable, used by the tests with a fake job that behaves exactly like §9), at most `C` jobs at once, each
  job with its own `SLOT=vm-NN` from `slots` (two concurrent jobs never share a slot), `MEM_MIB` from
  `--mem-mib`, and the roles exported as `MODEL`, `USER_MODEL`, `JUDGE_MODEL`. At most one trial of a task at a
  time is **not** required. Order of tasks is the solver's.
- Prints one line per trial end on stderr (free format) and a final JSON summary on stdout with `--json`:
  `{"fleet", "planned", "done", "failed", "aborted", "interrupted", "status", "wall_s"}`.
  Exit 0 iff `status: done`; 1 when the plan is complete but not every trial is `done` is **not** an error
  (exit 0: M2 < 100 % is a result, not a failure); exit 1 only when the plan could not be completed (stopped,
  results root unwritable mid-run); exit 2 on usage.
- **Stop.** On SIGTERM or SIGINT the runner starts no new trial, terminates its in-flight jobs (their
  `infra/job` aborts, destroys its VM and writes `job.json` `status: aborted`), journals them with
  `interrupted: true`, writes `status: stopped` and the session's `ended`, and exits within 60 s. No VM, tap,
  jail directory, iptables rule or SPIRE entry of the fleet survives it.
- **Resume.** `fleet run --out DIR ...` with an existing `DIR` continues it: the same `--tasks/--trials/--concurrency/
  --split/--part/--slots/--roles` (or exit 2 naming the difference), a new `sessions` entry, no re-run of any
  non-interrupted journal line, re-run of exactly what is missing. The number of jobs a resume starts is exactly
  `planned − (non-interrupted lines already journaled)`. A run directory of the fleet that has no
  journal line (the runner died without journaling it, e.g. SIGKILL) is adopted: if its `job.json` exists it is
  journaled as it is; if `job.json` is missing it is journaled `status: "aborted", interrupted: true` after a
  `job.json` `{"run", "task", "status": "aborted", "stage": "unknown", "exit_code": -1}` is written for it, and
  the trial is run again. Resuming a `done` fleet does nothing and exits 0.
- `AVOS_FLEET_RESULTS` / `--results` default `/var/lib/avos/results`; `AVOS_TRACE_ROOT` is passed through to
  `avos completeness`.
- **Isolation.** A job that cannot start (its slot busy, `vm create` refused) touches nothing it does not own: no
  other slot's VM, tap, jail, SPIRE entry or run directory changes; its own trial is journaled `aborted` (stage
  `create`). Two runners sharing a results root and a slot must not disturb each other's in-flight trials.

## 4. `fleet matrix`

```
fleet matrix --fleet DIR --prices PRICES.json [--labels PATH] [--json] [--out FILE]
```

Reads §2, every trial directory named by the non-interrupted journal lines, `PRICES.json` (§6) and optionally
label rows (§5 M12). Prints the matrix as text (one metric per block, every number with the parts it came from)
or, with `--json`, exactly the object below. `--out FILE` writes the JSON there as well. Exit 0 when the matrix
was produced (parts may read `unverified`): a non-interrupted line whose directory or `job.json` is missing is
still a trial (`status: "missing"`, cause `no_job_json`) and is named in `warnings`; a run id repeated on
non-interrupted lines is counted **once** and named in `warnings`. Exit 2 when `DIR/fleet.json`, the journal or
the prices file is missing or not JSON, or when the three roles are not three distinct model names.

**Determinism.** The same `DIR` + prices + labels produce byte-identical stdout (text and JSON) every time,
on any machine: no wall-clock, hostname, absolute path of the suite's choosing, or dictionary-order dependence
(sort every list; `json.dumps(sort_keys=True)`). Numbers are JSON numbers; percentages are plain fractions in
JSON (`0.75`) and `%` in text. `unverified` is the JSON string `"unverified"`, never 0, 1, 100 or null.

**Caches.** The tool may keep derived state (under `DIR`, `$TMPDIR` or `$HOME`). It is bound to the content of
its sources: the tests replace a source by a different file of the same size and mtime, edit a source in place, and
truncate / bit-flip / swap / delete whatever the tool wrote. After any of that the answer is the correct one for the
sources as they are, or exit 1 with an `error` naming the stale/damaged thing. Never a wrong number.

**Per-trial facts** (the parts; `trials[]` in JSON, sorted by run id; `m11.tasks` in `fleet.json` task order):

```
{"run", "task", "slot", "status": "done"|"failed"|"aborted"|"missing",
 "reward": number|null, "termination_reason": str|null, "wall_ms": int|null,
 "cause": <§5 cause>|null, "cause_class": "agent"|"infra"|null,
 "valid": bool,                       counts in M11: cause null, or cause_class agent
 "passed": bool|null,                 reward == 1.0 (null unless done with cause null)
 "llm": {"calls": n, "ms": Σ llm.call.ms, "by_model": {model: {"calls", "ok", "prompt_tokens", "completion_tokens", "ms"}}}
        | "unverified"               when events.jsonl is missing or holds a line that is not a JSON object
 "host_side_ms": wall_ms − llm.ms | "unverified",
 "m8": {"status", "ratio"} | "unverified"}   from completeness.json; missing/unreadable → unverified
```

`llm.call` rows are read from `<run>/events.jsonl` (every row with `kind == "llm.call"` of that run's VM, any
status). `ok` = rows with `status == 200`. Tokens are summed over `ok` rows only (a refused call has none).

**Metrics** (JSON keys; every value also has its parts next to it):

- `m2`: `{"done", "trials", "value": done/trials}`; `trials` = non-interrupted journal lines (deduplicated),
  including `missing` (no `job.json`) and every aborted stage. Never excludes anything.
- `m8`: `{"complete", "incomplete", "unverified", "mean_ratio": mean of ratio over verified trials | "unverified"}`
  plus per trial in `trials[]`; a trial with cause `witness_mismatch` has `m8.status: "witness_mismatch"` and counts
  under `incomplete` (its ratio still comes from `completeness.json`). The word `unverified` for a trial whose record is missing, never `1.0`.
- `m11`: per task `{"task", "trials": all its trials, "n": valid trials, "c": passed, "infra": infra-excluded trials, "pass1", "pass4", "avg4"}`
  with `pass_k = C(c,k)/C(n,k)` when `n ≥ k`, else `null` (the task is listed in `tasks_short_k`, never dropped);
  `avg4` = mean of `reward` (null reward → 0) over the task's valid trials, null when `n == 0`;
  `tasks_all_infra` = tasks with `n == 0` and ≥ 1 trial. Overall `pass1`, `pass4`, `avg4` = mean over tasks
  whose value is not null, `"unverified"` when no task qualifies. `ci95` for each: percentile bootstrap over
  tasks, **exactly**: `rng = random.Random(300)`, `vals` = the per-task values in task order, `T = len(vals)`,
  `B = 2000`, `means = sorted(mean(vals[rng.randrange(T)] for _ in range(T)) for _ in range(B))`,
  `lo = means[50]`, `hi = means[1949]`; `[lo, hi]` (`"unverified"` when `T < 2`). `infra_excluded` = Σ infra.
  `causes`: `{cause: count}` over non-done trials, and `by_class: {"agent": n, "infra": n}`.
- `m12`: per role `policy`, `simulator`, `nl_judge`, `label_judge`, `unattributed`:
  `{"model", "source": "gateway"|"label rows", "outside_gateway": bool, "calls", "ok", "prompt_tokens",
  "completion_tokens", "usd_total", "usd_per_task", "usd_per_successful_task", "basis": "gpu_hours"|"tokens",
  "token_ceiling_usd_per_task"}`. Hosted roles (`simulator`, `nl_judge`): `usd_total = (prompt_tokens ×
  prompt_per_mtok + completion_tokens × completion_per_mtok) / 1e6` with the model's price; `usd_per_task =
  usd_total / m2.trials`; `usd_per_successful_task = usd_per_task / m11.pass1`. Policy: `basis: gpu_hours`,
  `usd_total = hosts.gpu.usd_per_h × fleet_wall_h` (§6, fleet wall = Σ over sessions of `ended − started`, the
  open session counted to its last journal `ended`), per task as above, and `token_ceiling_usd_per_task` from the
  policy model's token price when one is listed. `label_judge`: `source: "label rows"`, `outside_gateway: true`,
  tokens from `--labels` (every `*.jsonl` under the path, rows with `level == 1`, a `usage` object **and a `run`
  that is one of this fleet's non-interrupted trials**; rows for any other run are ignored and their count named
  in `warnings` (`"<n> label rows for runs outside this fleet ignored"`); `model` = the rows' `model`, which must
  be one model; usd by its price). With `--labels` but no row of this fleet: `calls 0`, tokens 0, `usd_total 0.0`
  (the label judge has not run over this fleet; nothing of its spend belongs here). Without `--labels` every
  number is `unverified`. The same rule holds for every metric: only evidence inside the fleet's own trial
  directories and the fleet directory counts; nothing is borrowed from other runs on the host.
  `unattributed`: `{"models": sorted names, "calls", "ok", "prompt_tokens", "completion_tokens", "usd_total"}` over
  rows whose model is none of the three roles (`usd_total` by price when every such model is priced, else
  `"unverified"`; `0.0` when there are none), never folded into a role. Any missing price, host price or a `pass1` of 0/`unverified` → that field
  `"unverified"`; exception: a token-priced role or `unattributed` with **0 calls** has `usd_total 0.0` (nothing
  was spent, no price needed), while ≥ 1 call without a price is `"unverified"`. Tolerance 1e-9.
- `m13`: over `done` trials' `wall_ms`: `{"n", "median_s", "p90_s", "worst_s", "concurrency"}`; median = the
  usual (mean of the two middle values when even), p90 = `sorted[ceil(0.9·n) − 1]`, `"unverified"` when `n == 0`.
- `m16`: `{"host_usd_per_h", "fleet_wall_h", "completed": done trials, "value": host_usd_per_h × fleet_wall_h /
  completed, "baselines": {name: usd_per_trial}}`. Baseline per trial (§6): `vm` kind:
  `usd_per_h × max(min_s, m13.median_s + boot_s) / 3600`; `serverless` kind: `(vcpu × usd_per_vcpu_h + gb ×
  usd_per_gb_h) × max(min_s, m13.median_s) / 3600`. `ratio_to_best_baseline = value / min(baselines)`.
  `"unverified"` when `hosts.fleet` is missing, `completed == 0`, or a baseline lacks a field (that baseline only).
- `unverified`: sorted list of dotted paths of every field that reads `unverified` (`["m12.label_judge", ...]`).
- `warnings`: sorted list of strings; must contain the run id of every duplicated journal line and of every trial
  whose directory or `job.json` is missing, and the name of every unattributed model.
- `inputs`: `{"fleet": name, "trials_per_task", "concurrency", "roles", "prices_sha256", "split": {...}}`.

## 5. Failure causes

Exactly one cause for every trial whose `status` is not `done`; `null` for a done trial, except `role_mismatch`
(rule 6) and `witness_mismatch` (rule 6b), the two causes a `done` trial can carry. A trial leaves the pass^k denominator only when the evidence shows something other than the agent failed (rules 1–5, 6, 6b, 7–10, 11–12); τ²'s `infrastructure_error` alone is not that evidence when the agent's own reply caused it (rule 6a), and a harness timeout alone is not either (rule 10a) (it stays `done` for M2, is excluded from M11 as infra, and
`passed` is null). Derived only from the trial directory and the fleet's declared roles. First rule that
matches, top to bottom:

| # | cause | class | evidence |
|---|---|---|---|
| 1 | `no_job_json` | infra | no readable `job.json` (status `missing`) |
| 2 | `model_unrouted` | infra | an `authz.decision` row in `events.jsonl` with `decision: "deny"` and `"no-route"` in `policy_ids` |
| 3 | `authz_denied` | infra | any other `authz.decision` with `decision: "deny"` |
| 4 | `upstream_5xx` | infra | an `llm.call` row with `status ≥ 500` **and** `upstream_status ≥ 500` (the upstream really answered 5xx) |
| 4b | `upstream_unreachable` | infra | a `status ≥ 500` row with `upstream_status: null` and `error` starting `upstream_unreachable` (connect refused / DNS / reset before any byte) |
| 4c | `gateway_timeout` | **agent** | a `status ≥ 500` row with `error: "gateway_timeout"` (the gateway's own read timeout: nothing proves the model server failed; τ² sets no `max_tokens`, so a policy generating until the gateway gives up is the agent's behaviour and stays in `n` as a failure) |
| 4d | `gateway_5xx_unattributed` | **agent** | any other `status ≥ 500` row (no `upstream_status`/`error` evidence, e.g. rows written before §8's fields existed): without evidence the trial is not excluded |
| 5 | `upstream_4xx` | infra | an `llm.call` row with `400 ≤ status < 500` |
| 6 | `role_mismatch` | infra | `events.jsonl` readable and holds ≥ 1 `llm.call`, and either a `status: 200` row names a model that is none of the fleet's three roles, or no `status: 200` row names the policy model (the roles never reached the VM, or a default model answered) |
| 6a | `agent_empty_reply` | **agent** | `status: failed`, `termination_reason: infrastructure_error`, and the error text (`job.json.error` or `result.json` `simulations[0].info.error`) matches `AssistantMessage must have either content or tool_calls`, while the trial's last policy `llm.call` is a clean 200 (`status 200`, `upstream_status 200`, `error null`) or there is no policy row at all: the model answered, its reply was unusable (e.g. every token in a reasoning field). τ² calls that infrastructure; it is the agent's, and the trial stays in `n` as a failure. The pattern list grows only with a dated reason here. |
| 6b | `witness_mismatch` | infra | **`done` trials only**: `result.json` readable and not **one** simulation whose messages carrying `usage` equal, in order and in `(prompt_tokens, completion_tokens)`, the trial's `status: 200` `llm.call` rows of the policy and simulator models (NL-judge rows are not messages): τ² retried the simulation (`max_retries` 3 at the pinned commit), the witness was edited, or two simulations were saved. A trial is exactly one conversation; a failed trial keeps its own cause (rules 6a, 7–14), a `done` trial stays `done` for M2, is excluded from M11 (counted in `infra`), `passed` null, and its `m8.status` reads `witness_mismatch` (counted under `m8.incomplete`), never plain `complete`. |
| 7 | `vm_create_failed` | infra | `job.json` `status: aborted, stage: create` |
| 8 | `session_open_failed` | infra | `status: aborted, stage: session` |
| 9 | `harness_aborted` | infra | `status: aborted` with any other stage (`harness`, `unknown`, …) |
| 9b | `agent_length` | agent | a `status: 200` row of the **policy** model with `finish_reason: "length"` (the policy ran into a token limit; whatever τ² then reported, the trial stays in `n`). A judge or simulator row with `length` is not the agent's: the trial falls through to rule 12. |
| 10 | `harness_timeout` | infra | `status: failed, http: "timeout"` (infra/job's `TIMEOUT_S` expired) **and the policy upstream stalled**: `events.jsonl` unreadable, no policy `llm.call` row, no policy row with `status 200`, or the last policy `status 200` row ends ≥ **300 s** before the trial's end (`session.close.ts`, else the last row's `ts`). 300 s = 10× the worst healthy policy call seen in POC 10 (~30 s). |
| 10a | `agent_timeout` | **agent** | `status: failed, http: "timeout"` otherwise: the policy kept answering (a successful call within the last 300 s) until the clock ran out, i.e. a long failing conversation (an agent looping on tools). Stays in `n` as a failure: at concurrency 32 the worst done trial took 1,050 s, so trials hitting 1,200 s are as often the agent as a slow model, and excluding them would raise pass^k for exactly the configurations that loop. |
| 11 | `harness_http_error` | infra | `status: failed`, `http` present and ≠ `"200"` |
| 12 | `sim_<reason>` | infra | `status: failed`, `termination_reason` ∈ `user_error`, `timeout`, `infrastructure_error`, `unexpected_error` (e.g. `sim_user_error`) |
| 13 | `agent_<reason>` | agent | `status: failed`, `termination_reason` ∈ `agent_error`, `too_many_errors`, `context_window_exceeded`, `max_steps`, `agent_stop`, `user_stop` |
| 14 | `unknown` | agent | anything else (stays in `n`: an unexplained failure is not evidence against the infrastructure) |

Rules 2–5 (4b–4d included) apply only to non-done trials; rule 9b too (a `done` trial with a retried 5xx row has no cause); rule 6 applies to
every status. Rules 2–6 look at every row of the trial's `events.jsonl`; an unreadable or missing `events.jsonl`
skips them (the trial also reads `llm: "unverified"`).

## 6. Prices file

Read on the run day, never a constant in code. Any section may be absent; what depends on it reads `unverified`.
`hosts.gpu` describes **every GPU host that served the policy during the run**: `usd_per_h` is the rate of one
such host and `count` how many ran (a run may use two); the policy's cost basis is `usd_per_h × count × fleet
wall hours`. The matrix prints `m12.policy.gpu_hosts = {"name", "usd_per_h", "count", "usd_per_h_total"}` next to
the cost (text and JSON) so a single-host rate on a two-host run is visible, not silent.

```
{"read_on": "YYYY-MM-DD",
 "hosts": {"fleet": {"name": str, "usd_per_h": number},
           "gpu": {"name": str, "usd_per_h": number, "count": int ≥ 1 (default 1)}},
 "models": {<model>: {"prompt_per_mtok": number, "completion_per_mtok": number, "currency": "USD"}},
 "baselines": {<name>: {"kind": "vm", "usd_per_h": n, "min_s": n, "boot_s": n}
             | {<name>: {"kind": "serverless", "usd_per_vcpu_h": n, "usd_per_gb_h": n, "vcpu": n, "gb": n, "min_s": n}}}
```

## 7. `fleet sweep` and `fleet m17`

```
fleet sweep --out DIR --levels L1,L2,... --tasks ... --trials N [run options of §3] [--json]
fleet m17 DIR [--json]
```

`sweep` runs one fleet per level under `DIR/level_<L>/` (a §2 fleet directory, `concurrency: L`, every level the
same tasks × trials, `peak_guest_mem_mib` measured), ascending, then prints `m17`. `m17` is pure: it reads the level
directories (`DIR/level_*/`), needs nothing else, is deterministic, and prints

```
{"levels": [{"concurrency": L, "trials": n, "skipped": k, "host_side_ms": {"median", "n"} | "unverified",
             "wall_ms_median", "llm_ms_median", "peak_guest_mem_mib": number | "unverified"}, ...],   ascending
 "baseline_concurrency": lowest level, "baseline_host_side_ms": its median,
 "m17": L | "unverified", "ceiling_reached": bool, "rule": "median host-side trial time ≤ 1.05 × lowest level"}
```

`host_side_ms` of a trial = `wall_ms − Σ llm.call.ms` over **done** trials of the level (a `done` trial with
`llm: "unverified"` is skipped and counted in `skipped`); a level with no such trial is `"unverified"`.
`m17` = the largest `L` such that every level `≤ L` has `median ≤ 1.05 × baseline median`; `ceiling_reached` is
true iff some level fails the rule. The median, never the mean, and host-side time, never raw wall (a level whose
raw wall doubles because the model slowed down but whose host-side time is flat is **within** M17).
`peak_guest_mem_mib` comes from the level's `fleet.json`; null → `"unverified"` for that level (never 0).

## 8. Gateway routes

The gateway reaches a different upstream per model name from a route table (`/var/lib/avos/gateway.env` or a
file next to it; format is the solver's). Observable contract:

- An `llm.call` row carries the `model` the guest asked for and the upstream's `status`; the three roles of a
  fleet show up as three distinct model names with `status: 200` rows in one trial's `events.jsonl`.
- A model that is in no route is **refused, not forwarded**: the gateway writes an `authz.decision` with
  `decision: "deny"`, `policy_ids` containing `"no-route"`, `resource: Model::"<model>"`, answers 4xx to the guest,
  and writes **no** `llm.call` with `status: 200` for that model. The trial's cause is then `model_unrouted` (§5).
- Every `llm.call` row the gateway writes carries three more fields: `upstream_status` (int: the HTTP status the
  upstream answered; `null` when no response arrived), `error` (`null`, `"gateway_timeout"` when the gateway's
  own read/connect timeout fired after a connection was made, `"upstream_unreachable: <reason>"` when no connection
  could be made, or `"upstream_bad_body"` when the reply was not JSON) and `finish_reason` (the reply's
  `choices[0].finish_reason`, `null` without one). A `status: 200` row has `upstream_status: 200` and a non-null
  `finish_reason`. These are what §5 tells an upstream failure, the gateway's own timeout and a truncated
  generation apart; a row without them cannot exclude a trial from M11.
- **Several upstreams per model.** The route table maps a model to one upstream (today's form) or to a list of
  upstreams; a list of one behaves like the single form. A call from slot `vm-NN` (or identity `vm-NN-xxxxxx`)
  for a model with `n` upstreams goes to `upstreams[NN mod n]`, so a trial's whole conversation stays on one
  server and slots split evenly. Every `llm.call` row carries `upstream` (the chosen upstream's URL as written in
  the table; `null` only when the model had no route). Pure entry point for the tests:
  `gateway.py route [--table FILE] MODEL SLOT` → stdout `{"model", "slot", "upstream": <url>|null}`, exit 0 when
  routed, exit 1 with `"error": "no-route"` when not; `--table` is a JSON file `{model: url | [url, ...]}` (the
  live table when omitted). The command touches no network and no host state.
- No key leaves the host; no key appears in any run directory, journal, matrix or sweep output.

## 9. What the tests' fake `infra/job` does (the runner may rely on nothing more from the real one)

`job run tau2 retail/<task>`: honours `SLOT` (default: lowest free slot), claims `<results>/run_retail-<task>_<t>`
by `mkdir` with the lowest free `t`, writes `job.json` in **every** outcome (done / failed / aborted with `stage`
and `exit_code`), `result.json` or `error.log`, `events.jsonl`, prints the `job.json` object as the last stdout
line when it reached the end, exits 0 iff `status: done`, aborts (status `aborted`, stage `harness`, exit code
143) when it receives SIGTERM, records its environment (`SLOT`, `MODEL`, `USER_MODEL`, `JUDGE_MODEL`, `MEM_MIB`)
in `<run>/env.json`, and refuses to start when its slot is already in use (a second concurrent job with the same
`SLOT` fails with exit 1 and `stage: create`). Behaviour per task is scripted by the test (`AVOS_FAKE_JOB_PLAN`).
