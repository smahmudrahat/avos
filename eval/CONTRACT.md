# POC 13 contract: a closed frontier model behind the same gateway, and M15

The black-box interface `eval/tests/run.py` exercises. Everything below is required; nothing about internals is.
Serves: `eval/STATEMENT.md` done tests 1–5 (done test 6 is the run itself), metrics M11/M12/M13 for the closed arm
and M15. Written before and without the implementation. Everything the earlier contracts require
(`fleet/CONTRACT.md`, `train/CONTRACT.md`, `label/CONTRACT.md`, `trace/CONTRACT.md`, `collect/CONTRACT.md`) stays
required; `fleet/tests`, `train/tests` and `label/tests` must still pass.

Every CLI below: Python 3.10+ standard library (the gateway keeps its `cedarpy`), exit `0` success, `1` the input
fails a check named on stdout (`{"error": <reason>, ...}` with `--json`) or stderr, `2` usage or unreadable input;
never a traceback; never a key. Determinism and tolerance as in `fleet/CONTRACT.md` §4 (sorted JSON, 1e-9).

The pinned closed arm (statement): model `claude-opus-5-5`, `output_config.effort` `medium`, `max_tokens` 16000,
automatic prompt caching on. "Pinned" means: in the route table, sent on every call, recorded on every row and in
`fleet.json`, cross-checked by `fleet matrix`.

## 1. The gateway (done test 1)

### 1.1 Route table

`/var/lib/avos/gateway.routes.json` keeps its form (`{model: upstream | [upstream, ...]}`, upstream = url or
`{"base_url", "key"}`), with one more optional field on an object upstream:

```
"claude-opus-5-5": {"base_url": "https://api.anthropic.com", "key": "ANTHROPIC_API_KEY",
                    "kind": "anthropic", "effort": "medium", "max_tokens": 16000, "cache": true}
```

- `kind` absent or `"openai"`: today's behaviour, byte for byte (§1.5). `kind: "anthropic"`: the upstream is
  Anthropic's Messages API at `<base_url>/v1/messages`; `key` (the name of a `gateway.env` variable), `effort`
  (`low|medium|high|max`), `max_tokens` (int ≥ 1) and `cache` (bool) are **required**. An anthropic entry missing
  one of them is a **bad route**: `gateway.py route` exits 1 with `{"error": "bad-route"}`, and `gateway.py serve`
  refuses to start (non-zero exit naming the model). Never silently defaulted.
- `gateway.py route [--table FILE] MODEL SLOT` prints, besides today's `model`, `slot`, `upstream`: `"kind"`
  (`"openai"` or `"anthropic"`) and, for anthropic, `"effort"`, `"max_tokens"`, `"cache"`. Never the key or its
  variable name. `no-route` stays exit 1 `{"error": "no-route"}`.
- A list of anthropic upstreams behaves like today's lists (slot modulo); every entry carries its own pins.

### 1.2 What is sent upstream (kind anthropic)

For one `POST /v1/chat/completions` from a VM (an OpenAI chat-completion request as LiteLLM's `openai/` provider
sends it: `model` without provider prefix, `messages`, `tools`, `temperature`, …), the gateway makes **exactly one**
`POST <base_url>/v1/messages` — never two, whatever the upstream answers (429, 5xx, timeout, malformed) — with
headers `x-api-key: <key>`, `anthropic-version: 2023-06-01`, `content-type: application/json`, and a body where:

- `model` = the request's model; `max_tokens` = the route's; `output_config.effort` = the route's;
  `cache_control: {"type": "ephemeral"}` at the top level when `cache` is true; `stream` never true.
- `temperature`, `top_p`, `seed`, a client `max_tokens`, and a **forced** `tool_choice` (`"required"`, or an
  object naming a function) are **not sent**. `tool_choice` absent / `"auto"` / `"none"` may be sent as
  `{"type": "auto"}` / `{"type": "none"}` or omitted. Any other field the Messages API does not take is dropped too.
  The row lists what was dropped (§1.4).
- `system` = the content of the single `system` message (string, or one text block with that text); no system
  message in `messages`.
- `messages`: roles alternate `user`/`assistant`; the first is the client's first non-system message, forwarded as
  is: `user`, or `assistant` when the history opens with the agent's turn (τ²'s `llm_agent` history starts with the
  greeting "Hi! How can I help you today?"; ruling 2026-10-24: 35 real τ² calls in that shape answered 200, and no
  synthetic user turn may be added). An assistant message with `tool_calls`
  becomes `tool_use` blocks `{id, name, input}` with `input` = the parsed `arguments` JSON (a text block first only
  when the message has non-empty text). Every `role: tool` message becomes a `tool_result` block
  `{tool_use_id, content}`; **consecutive** tool messages, and a user message that follows them, merge into **one**
  user turn with the `tool_result` blocks first, in order, then the user's text. No text block is ever empty
  (`""`/null content → no text block). Thinking blocks the solver chooses to replay in assistant turns are allowed;
  nothing else may be added, reordered or dropped.
- `tools`: one `{name, description, input_schema}` per OpenAI tool, `input_schema` = that tool's `parameters`
  object unchanged, same order. `cache_control` markers inside tools/system/messages are allowed.

Rulings 2026-10-24 on the solver's disputes (the check that keeps each purpose in brackets):
1. *`policy_model` only on closed items* — amended (§4). [E1 exercises `retrain export` itself.]
2. *Assistant-first history* (above) — accepted. [G8 sends τ²'s exact shape: system, greeting, user, tool call,
   tool result → upstream `[assistant, user, assistant, user]`, greeting first, nothing added; L1 sends it live.]
3. *`thinking_tokens` absent* — amended (§1.4, §2): billing needs the four billed counts; thinking is inside
   `output_tokens`. [F6: null thinking accepted and counted, a null billed count still refused.]
Added class check (§2.1, §5 arm provenance; F5, M5): a run whose recorded policy is not the one the operator meant.
Added class check (F5, G1–G5): a fake more permissive than the real upstream. Every fake standing in for a keyed
upstream answers 401 unless it gets the route's key the way the real one wants it (`Authorization: Bearer` for the
vLLM policy, `x-api-key` for Anthropic).

### 1.3 What the VM gets back

An OpenAI chat completion, status 200, `content-type: application/json`:

```
{"id": <upstream id, "msg_…">, "object": "chat.completion", "model": <request model>, "created": int,
 "choices": [{"index": 0, "message": {"role": "assistant", "content": <str|null>, "tool_calls": [...]|absent},
              "finish_reason": "stop"|"tool_calls"|"length"|"content_filter"}],
 "usage": {"prompt_tokens": P, "completion_tokens": C, "total_tokens": P + C}}
```

- `content`: the concatenated text blocks; `null` when the reply has tool calls and no text; `""` when the
  reply has no text and no tool calls (empty content list, a refusal). Thinking blocks never appear in `content`
  (an extra `reasoning_content` field is allowed).
- `tool_calls[i]` = `{"id": <tool_use.id>, "type": "function", "function": {"name", "arguments": <JSON **string**
  of input>}}`, in upstream order, all of them (parallel calls are N entries).
- `finish_reason`: `end_turn`/`stop_sequence` → `stop`, `tool_use` → `tool_calls`, `max_tokens` → `length`,
  `refusal` → `content_filter` (anything new → the raw string, and the row keeps it in `stop_reason`).
- `P` = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` (absent → 0);
  `C` = `output_tokens` (billed, thinking included).
- A non-2xx upstream answer (429, 500, …) is passed back **as it is**: same status, same body bytes, and a
  `retry-after` header when the upstream sent one. A 2xx whose body is not JSON, or not a Messages response
  (`content` not a list): status 502 with `{"error": "upstream_bad_body"}`. Gateway timeout / unreachable: as today
  (504 / 502, `error` on the row). The gateway never retries and never adds a delay: one trial is one attempt.

### 1.4 The `llm.call` row (kind anthropic)

Everything today's row carries (`docs/trace.md` §2, §5; `fleet/CONTRACT.md` §8) with the same meaning, plus:

| Field | Value |
|---|---|
| `route_kind` | `"anthropic"` |
| `effort`, `max_tokens`, `cache` | what was **sent** (the route's), on every row of the model, whatever the status |
| `dropped_fields` | sorted list of the client's top-level fields that were not forwarded (`["max_tokens", "seed", "temperature", "tool_choice", "top_p"]` when all were present; `[]` when none) |
| `usage` | `{"input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens", "thinking_tokens"}`: the upstream's numbers (`thinking_tokens` from `output_tokens_details.thinking_tokens`); a field the upstream did not send is `null`, never 0 (a reply without `output_tokens_details` records `thinking_tokens: null`); every field `null` on a row without a 2xx reply |
| `prompt_tokens`, `completion_tokens` | `P`, `C` of §1.3 (`null` without a 2xx reply) |
| `stop_reason` | the upstream's raw `stop_reason` (`null` without one); `finish_reason` = the mapped one the client got |
| `upstream_status`, `error`, `response_id` | as today: the upstream's own HTTP status (`null` when none answered), `null` / `gateway_timeout` / `upstream_unreachable: …` / `upstream_bad_body`, the upstream `id` |
| `system_sha256`, `tools_sha256`, `messages_sha256`, `output_sha256` | **exactly as for an open model**: `content_sha(chat_form(x))` over the OpenAI-shaped request the VM sent and the OpenAI-shaped reply the VM got (`trace/lib_trace.py`), so the witness check of `retrain` keeps working unchanged |

`authz_ref`, `attempt`, `upstream` (the route's `base_url`), `dropped`/`receipt` (kill switch) as today. No key, no
prompt text, no reply text anywhere on the host (store, counters, stdout, stderr, journals, matrices).

### 1.5 Open routes unchanged

For a route without `kind` or with `kind: "openai"`, the request body bytes, the path, the `Authorization: Bearer`
header, the reply bytes and status, and the row's key set and values are **exactly today's** (the 27 keys of
`eval/tests/lib.py` `TODAY_ROW_KEYS`; none of §1.4's new keys). `fleet/tests/run.py --quick` must still pass.

### 1.6 Local test seam

`gateway/gateway.py` stays importable as a module with these names keeping their meaning, because the suite serves
the real `Handler` on loopback with test certificates and a fake upstream (`eval/tests/gw_local.py`): `Handler`
(its `setup`, `parse_request`, `llm`, `reply`), `Server`, `env_file`, `load_routes(env, path)`,
`route(table, model, n)`, `route_cli(args)`, and the module globals `UPSTREAM`, `ROUTE_TABLE`, `CTX`, `JAIL`,
`CUT`, `ROUTES`; `import cedarpy` and `import store` at module import; the store root from `AVOS_TRACE_ROOT`.
Translation code may live in another module the gateway imports.

## 2. The fleet (done test 2)

- `fleet run` reads the route table at `$AVOS_GATEWAY_ROUTES` (default `/var/lib/avos/gateway.routes.json`) at
  start and records the policy's route in `fleet.json`:
  `"policy_route": {"kind": "openai"|"anthropic", "upstream": <url, list of urls, or null when no table>,
  and for anthropic "effort", "max_tokens", "cache", for openai "served" (§2.1)}`. Never the key name. The policy's
  entry is the one `gateway.py route` would pick for the fleet's first slot. `policy_route` is part of the plan: a
  resume whose table gives another `policy_route` (including another `served`) exits 2 naming `policy_route`,
  starts nothing.
- `fleet matrix` copies `fleet.json`'s `policy_route` verbatim into `inputs.policy_route` (absent when the fleet has
  none: every pre-POC-13 fleet). M15 reads each session's arm provenance from there (§5).
- Everything else of a closed-policy run is the unchanged fleet: same `--split`/`--part`, simulator, NL judge,
  seed, trials, concurrency, `infra/job`, `llm_agent` scaffold; `--policy claude-opus-5-5` is the only difference.
- **`fleet matrix` on a fleet whose `policy_route.kind` is `"anthropic"`** (else: exactly today):
  - `fleet.json` without `policy_route` → exit 2.
  - Every `status: 200` `llm.call` row of the policy model must carry `effort == policy_route.effort`,
    `max_tokens == policy_route.max_tokens`, and `usage` whose four billed fields (`input_tokens`,
    `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`) are integers. Otherwise the whole
    matrix is **refused**: exit 1, `{"error": "effort_mismatch"|"max_tokens_mismatch"|"usage_missing", "run": <run>,
    ...}` naming the first offending run; nothing is averaged. `thinking_tokens` is an integer or `null` (ruling
    2026-10-24: not every reply shape is known to carry it, and it is billed inside `output_tokens`); a `null` is
    never read as 0 silently: the `thinking_tokens` sums (§3) add the integers only, and
    `m12.policy.thinking_tokens_missing` counts the 200 policy rows whose `thinking_tokens` is `null`.
  - Every non-interrupted trial with a readable `events.jsonl` that holds ≥ 1 `llm.call` row must have ≥ 1
    `status: 200` row of the policy model and no `status: 200` row of a model outside the three roles; otherwise
    exit 1 `{"error": "policy_mismatch", "run": <run>, "model": <offending model or null>}` (for a closed arm this is a
    refusal, not today's `role_mismatch` cause: a frontier number must not come from a trial another model answered).
  - Non-200 policy rows (429, 5xx) keep today's causes; their `usage` nulls do not count as missing.

### 2.1 Which weights answered: recorded at run start (open policy)

The class of mistake this closes (POC 12 lost 520 trials to a fleet that silently used the default model): the
run's recorded policy is not the one the operator meant. The base and the retrained sessions record the **same**
`roles.policy` (`policy-qwen3.5-9b`, the frozen served name), so the name cannot tell the arms apart. vLLM's
`GET /v1/models` lists each served model with `root`, the checkpoint path its server was started on, which differs
between the base and a merged checkpoint (`train serve-cmd` argv differs only in `DIR`).

- When the table gives the policy a `kind: "openai"` route, `fleet run` sends `GET <base_url>/models` to **every**
  upstream of that route (table order; `base_url` as written, e.g. `http://10.0.1.21:8000/v1` → `…/v1/models`)
  before any trial starts, on a fresh run and on a resume, and records
  `policy_route.served = [{"upstream": <base_url>, "id": <roles.policy>, "root": <that entry's root>}, ...]`.
- The GET authenticates exactly as the gateway does for that upstream: an object upstream with `key` sends
  `Authorization: Bearer <value>`, the value read from the variable `key` names in the gateway's env file
  (`$AVOS_GATEWAY_ENV`, default `/var/lib/avos/gateway.env`, root-only; `fleet run` runs as root on the host, as
  every host run already does under `sudo`); a string upstream sends no header. The real policy upstreams answer
  401 without it (found on the host 2026-10-24), so the suite's fakes do too. The env file unreadable or without
  that variable, or a 401/403 answer → `policy_unverified` naming the upstream and the variable's *absence*, never
  the value. The key value and its variable name appear nowhere: not in `fleet.json`, `trials.jsonl`, the matrix,
  stdout or stderr.
- Refused before any trial (exit 1, `policy_unverified`, naming the upstream; no journal line, no job started): an
  upstream unreachable, or answering non-2xx, non-JSON or without a `data` list; no entry whose `id` is
  `roles.policy`; two upstreams reporting different roots, or a root on one and none on another (two checkpoints
  behind one name would pool two arms).
- An entry for `roles.policy` without a `root` (hosted OpenAI-compatible providers, e.g. Nebius, list many ids and
  no roots; found on the host 2026-10-24) is recorded as `"root": null` and the run proceeds: the provider and id are
  all such an upstream can say. A null root proves nothing about weights, so it cannot serve the retrained arm
  (§5 `served_root_missing`); the suite keeps both fake shapes (vLLM with roots, hosted with many ids and none).
- No table (`$AVOS_GATEWAY_ROUTES` missing): `upstream` and `served` are `null` and the run proceeds as today
  (`fleet/tests`). An anthropic route records no `served`: the model name at Anthropic is the identity (§5).

## 3. M12 for a token-priced policy (done test 3)

Prices file (`fleet/CONTRACT.md` §6) extended per model:

```
"claude-opus-5-5": {"prompt_per_mtok": 4.0, "completion_per_mtok": 20.0,
                    "cache_write_per_mtok": 5.0, "cache_read_per_mtok": 0.2, "currency": "USD", "listed_on": "2026-10-24"}
```

When `policy_route.kind` is `anthropic`, `m12.policy` has `basis: "tokens"` and, from the `status: 200` policy rows
of the fleet's non-interrupted trials and the prices alone:

- `usage`: the five sums (`thinking_tokens` over the rows that carry it); `thinking_tokens_missing` (§2); `calls`,
  `ok`, `prompt_tokens`, `completion_tokens` as today.
- `usd_total = Σ_rows (input_tokens × prompt_per_mtok + cache_creation_input_tokens × cache_write_per_mtok +
  cache_read_input_tokens × cache_read_per_mtok + output_tokens × completion_per_mtok) / 1e6`;
  `usd_per_task = usd_total / m2.trials`; `usd_per_successful_task = usd_per_task / m11.pass1`.
- `uncached_ceiling_usd_per_task = Σ_rows (prompt_tokens × prompt_per_mtok + completion_tokens ×
  completion_per_mtok) / 1e6 / m2.trials` (every prompt token at the input price); `token_ceiling_usd_per_task`
  equals it.
- A missing cache rate → `usd_total`, `usd_per_task`, `usd_per_successful_task` read `"unverified"` (the ceiling
  still prints); a missing price → everything `"unverified"`; `pass1` 0 → `usd_per_successful_task` unverified.
- Per trial: `trials[].llm.by_model[<policy>]` gains `usage` (the trial's five sums) and `usd` (that trial's cost
  by the formula above), so a third party recomputes every number from `trials.jsonl` + `events.jsonl` + prices.
- `m12.policy.price = {<the model's prices entry as read>}` (so the matrix names the rates it used).
- Open policies: unchanged (`gpu_hours` basis, `token_ceiling_usd_per_task`); recomputing the committed
  `data/poc11`/`data/poc12` matrices from their parts must give their numbers.
- The matrix is still a pure function of its sources: a source replaced by different content with the same size
  and mtime gives the new answer (or exit 1 naming the damage), never the old one.

## 4. Closed outputs never train (done test 4)

**Closed model** = a model whose name starts with `claude-` or `anthropic/`, or whose fleet records
`policy_route.kind != "openai"`, or whose rows carry `route_kind: "anthropic"`. (The list grows only with a dated
reason here.)

- `retrain export`: an item whose policy calls (the gateway calls matched to its assistant messages) were answered
  by a closed model carries `"policy_model": <that model, as the gateway recorded it>`; every other item has **no**
  `policy_model` key, so an export without closed runs stays byte-identical to POC 8's (`label/tests` M2, frozen
  `dataset_sha256`) and open items keep their `item_id`s. Absent = open, as for pre-POC-13 exports. (Ruling
  2026-10-24, amended from "every item gains it", which broke the frozen POC 8 export.) Checked on the export
  itself: the label/tests fixture environment exports to the frozen `dataset_sha256` with no `policy_model`;
  adding one run whose gateway calls are the closed model gives that item `policy_model: "claude-opus-5-5"` and
  leaves every other item's line byte-identical; `label run` keeps nothing of it (rule `closed_policy`) and labels
  the others as before.
- `train select`: a `--fleet` whose `roles.policy` is closed, or `policy_route.kind` is not `"openai"`, or any item
  of a pair whose `policy_model` is closed → the whole command is **refused**: exit 1, `DIR` not created, stderr
  (and `--json` `error`) naming `closed_policy` and the model name. Checked before `policy_mismatch`, before any
  rendering, whatever `--policy` says.
- `label run`: an item whose `policy_model` is closed gets `final = {"label": "fail", "rule": "closed_policy",
  "kept": false}` whatever its reward, judge or human label; `summary.counts.kept` and `manifest.labels` agree.
  `label human` may still record a verdict (it is a fact about the run); it never changes the rule.

## 5. M15 (done test 5)

```
fleet m15 --retrained MATRIX.json [--retrained MATRIX.json ...] --closed MATRIX.json [--closed ...]
          [--base-root ROOT] [--assert-served FLEET=retrained ...]
          [--seed 300] [--bar-x 0.75] [--bar-y 5] [--json] [--out FILE]
```

Inputs are `fleet matrix --json` files (sessions). Pure function of those files; same inputs → byte-identical
output; same-size-same-mtime edit of a session → the new answer.

**Refusals** (exit 1, `{"error": <reason>, ...}` naming the sessions): `task_sets_differ` (any session's task set
differs from any other's, both arms), `split_mismatch` (`inputs.split.sha256` or `part` differ), `simulator_mismatch`
(`inputs.roles.simulator` differ), `judge_mismatch` (`inputs.roles.nl_judge` differ), `policy_mismatch` (two sessions
of one arm name different `roles.policy`), `duplicate_session` (the same `inputs.fleet` or the same file content given
twice, in either arm), `pass_k_mismatch` (a task's stored `pass1`/`pass4` ≠ `C(c,k)/C(n,k)` of its `n`, `c`),
`not_a_matrix` (missing `inputs`, `m11.tasks`, `m12.policy`, `m2`, `trials`). Exit 2 for an unreadable file or
no session on a side.

**Arm provenance** (checked before anything is computed; the arm must be provable from the session itself):

- `--closed`: `inputs.roles.policy` closed by §4's name rule **and** `inputs.policy_route.kind == "anthropic"`;
  otherwise exit 1 `closed_arm_not_closed` naming the session (`inputs.fleet`): the committed base sessions, any
  open-route session, a closed name without a recorded route. No assertion can make a session closed.
- `--retrained`: `inputs.policy_route.kind`, when present, must be `"openai"` and the policy not closed by name,
  else exit 1 `retrained_arm_not_open`. A session is **recorded** when `inputs.policy_route.served` is a non-empty
  list (§2.1); its `served_root` is the one root its entries report. Recorded sessions need `--base-root ROOT` (the
  base checkpoint's `root` as its server reports it; exit 2 naming `base-root` when absent); a recorded root equal
  to it → exit 1 `retrained_is_base`; recorded sessions with different roots → exit 1
  `retrained_checkpoint_mismatch`. A recorded session whose roots are `null` (hosted provider, §2.1) → exit 1
  `served_root_missing`, also when asserted: a run that recorded what it could cannot be overridden by assertion.
- A session without `served` (the four committed sessions, any fleet before §2.1) is **legacy**: refused, exit 1
  `provenance_missing` naming it, unless the operator names it with `--assert-served <its inputs.fleet>=retrained`.
  An asserted session is used and reported `"provenance": "asserted, not recorded"` in the JSON and on its text
  line, never silently accepted. `--assert-served` naming a recorded session → exit 1 `assertion_not_needed`;
  naming no given session, or an arm other than `retrained` → exit 2.

**Pooling.** Per arm and task: `n = Σ_sessions n`, `c = Σ_sessions c`, `trials = Σ_sessions trials`. Tasks in
ascending numeric id order (the order of `vals` for the bootstrap). `pass_k(task) = C(c,k)/C(n,k)` when `n ≥ k`,
else undefined: the task is listed in the arm's `tasks_short_4` and left out of that arm's pass^4, of X and of
the difference (never dropped silently, never counted as 0). An arm's `pass1`/`pass4` = mean over defined tasks.

**Cost per task.** Per trial: a token-priced session (`m12.policy.basis == "tokens"`) uses
`trials[].llm.by_model[<policy>].usd`; a GPU-hours session uses `m12.policy.usd_total / m2.trials` for every trial.
Per task: mean over the task's trials of both arms' sessions; an arm's `usd_per_task` = mean over tasks;
`usd_per_successful_task = usd_per_task / pass1`. A session whose per-trial cost cannot be read →
`usd_per_task` `"unverified"` and Y `unresolved` with `reason: "cost_unverified"`.

**Estimates.** `X = retrained.pass4 / closed.pass4`, `Y = closed.usd_per_task / retrained.usd_per_task`,
`diff_pass4 = retrained.pass4 − closed.pass4`, over the tasks defined in both arms.

**Intervals.** `rng = random.Random(seed)`, `B = 2000`, `T` = the number of tasks used, one draw of `T` indices
`rng.randrange(T)` per resample, in this order of statistics: pass1 (retrained), pass1 (closed), pass4
(retrained), pass4 (closed), diff_pass4, X, Y, each with a **fresh** `random.Random(seed)` so every interval is
reproducible on its own. For a mean: `vals` per task, `means[50]`, `means[1949]` of the sorted resample means
(`fleet/CONTRACT.md` §4). For a ratio: each resample recomputes `mean(numerator tasks) / mean(denominator tasks)`
over the same drawn indices; a resample whose denominator is 0 is counted in `undefined_resamples` and dropped;
`ci95 = [ratios[50], ratios[1949]]` only when `undefined_resamples == 0`, else `null`. A point estimate with a zero
denominator is `null` with `reason: "zero_denominator"`. Never `inf`, `nan` or a crash.

**Bars.** `claims.x`: pass when `ci95[0] ≥ bar`, fail when `ci95[1] < bar`, else `unresolved` (straddles, or no
interval). `claims.y` the same with its bar. Both printed next to the point estimate.

**JSON output** (`--json`; `--out` writes the same):

```
{"format": "avos-m15/1", "bootstrap": {"seed": 300, "B": 2000, "rule": "paired percentile bootstrap over tasks"},
 "tasks": [ids, numeric order],
 "arms": {"retrained": ARM, "closed": ARM},
 "diff_pass4": {"value", "ci95", "tasks"},
 "x": {"value": number|null, "ci95": [lo, hi]|null, "bar": 0.75, "verdict": "pass"|"fail"|"unresolved",
       "tasks": int, "undefined_resamples": int, "reason"?: str},
 "y": {same, "bar": 5},
 "provenance": {"base_root": ROOT|null, "assertions": [{"fleet", "arm": "retrained", "status": "asserted, not recorded"}, in the order given]},
 "warnings": [sorted strings: trials_per_task or concurrency differ between sessions, …]}
ARM = {"sessions": [{"fleet", "path", "policy", "trials", "pass1", "pass4", "usd_per_task", "basis",
                     "provenance": "recorded"|"asserted, not recorded", "served_root": str|null}, in the order given],
       "pooled": {"pass1", "pass1_ci95", "pass4", "pass4_ci95", "usd_per_task", "usd_per_successful_task", "trials"},
       "per_task": {task: {"n", "c"}}, "tasks_short_4": [ids]}
```

Text output (no `--json`): the same numbers, with the lines `pass^1`, `pass^4` per arm and per session, `X … ≥ 0.75
→ pass|fail|unresolved`, `Y … ≥ 5 → …`, each interval printed next to its point estimate, and each session's
provenance (`asserted, not recorded` on every asserted session's line). With the two committed retrained sessions
asserted (`--retrained data/poc12/p12-heldout/matrix.json --assert-served p12-heldout=retrained --retrained
data/poc12/p12-heldout-r2/matrix.json --assert-served p12-heldout-r2=retrained`) and, as the closed arm,
closed-relabelled copies of `data/poc11/p11-heldout` and `data/poc12/p12-heldout-base` (same per-task `n`, `c`;
policy `claude-opus-5-5`, `policy_route` anthropic, token basis; the base sessions themselves are refused as
`--closed`), the pooled pass^1/pass^4 and the pass^4 difference with their intervals equal `data/poc12/pooled.json`
to three decimals.

## 6. Environment and what may not change

- Keys live in `/var/lib/avos/gateway.env` only. No key or key variable name in rows, `fleet.json`, matrices, M15
  output, journals, run directories, stdout or stderr of any command.
- The suite points `TMPDIR`, `HOME`, `XDG_*_HOME`, `AVOS_TRACE_ROOT`, `AVOS_LABEL_ROOT`, `AVOS_LABEL_CACHE`,
  `AVOS_GATEWAY_ROUTES` at private directories; tools write nowhere else (besides `--out`).
- Nothing in `data/`, `fleet/tests/fixtures`, `train/tests/fixtures`, `label/tests/fixtures`, `eval/tests` changes.
- The open-model path is byte-identical to today (§1.5); `fleet compare`, `train`, `label`, `retrain` keep their
  contracts; the three existing suites keep passing.
