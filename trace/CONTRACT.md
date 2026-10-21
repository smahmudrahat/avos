# POC 6 trace contract

The black-box interface `trace/tests/run.py` exercises. Everything below is required; nothing about internals is.
Serves: claim 1 (control: receipts joinable to what they froze), claim 2 (economy: per-run cost from the trace
alone), claim 3 (learning: a run reconstructs as training data), M3, M5, M8, M9, M12, M16. Design constraint
from the statement: nothing may need central coordination per event, and everything must partition by principal.

## 1. Files

| Path | Role |
|---|---|
| `trace/schema.json` | Machine-readable schema for one event (JSON Schema; top-level `$schema` key; every kind in §3 named). |
| `trace/bin/ingest` | Converts today's ledger rows and a τ²-bench run into contract events. |
| `trace/bin/validate` | Validates a trace file against §3–§5. |
| `trace/bin/verify` | Checks the tamper-evidence chain (§6) without the writer. |
| `trace/bin/completeness` | M8 counters (§7). |
| `trace/bin/query` | `audit`, `lineage`, `cost` projections (§8). |
| `trace/bin/project` | `chat` projection for retraining (§9). |
| `trace/tests/fixtures/` | Test data (real ledger window, real run, golden files, prices). Read-only for the solver. |

Every CLI: `python3 <path> ...`, Python 3.10+ standard library, no network, no files outside its arguments
(plus an optional side index next to the input file or under `$TMPDIR`). Exit `0` success, `1` the input
fails the check, `2` usage or unreadable input (no traceback on stderr). Machine output on **stdout** as JSON.

## 2. Trace file

A trace is a UTF-8 file, one JSON object per line (JSON Lines), any order of lines. A file may hold one run,
one VM, one host-day or any concatenation; validity rules apply within the file (plus `--ledger` for identity
rows kept elsewhere).

## 3. Event

Common fields, all required:

| Field | Type | Meaning |
|---|---|---|
| `event_id` | string, `[A-Za-z0-9_.:-]{1,64}` | Unique without coordination (minted on the emitting host). |
| `kind` | string | One of: `vm.boot vm.exit session.open session.close agent.spawn agent.exit llm.call tool.call tool.result fs.write net.connect authz.decision halt resume label`. |
| `ts` | string `YYYY-MM-DDTHH:MM:SS.mmmZ` | **Host** clock, UTC, ms. For `llm.call` it is the completion time. |
| `writer` | string, starts with `host + "/"` | The emitting stream, e.g. `ip-172-31-44-187/gateway`. |
| `seq` | int ≥ 1 | Position in the writer's own log, +1 per event; a gap means a lost event. Within a file, a writer's events appear in increasing `seq` order (files may interleave writers in any way). |
| `host` | string | Host that wrote the event. |
| `principal` | SPIFFE ID | Who the event is about (see per-kind). The partition key. |
| `vm_id` | string | `vm-NN-xxxxxx`. |
| `prev`, `hash` | 64 hex | Chain, §6. |

Optional, with fixed meaning when present: `run` (`run_<task>_<trial>`), `guest_ts` (a clock inside the VM; never
used for ordering), `receipt` (`hr_[0-9a-f]{4}`), `role` (`assistant`|`user` for harness-derived model calls),
`price` (`{"prompt_per_mtok","completion_per_mtok","currency"}`), `cost_usd`, `agent_id` (`ag_xxxx`),
`call_id` (harness tool-call id), content fields (§5), `*_sha256`.

Per-kind required fields and `principal`:

| kind | principal | required fields |
|---|---|---|
| `vm.boot` | the VM | `vm`, `boot_ms` (int) — the VM's life starts at `ts - boot_ms` |
| `vm.exit` | the VM | — |
| `session.open` | the VM | `run`, `harness`, `task` |
| `session.close` | the VM | `run`, `status`, `wall_ms` (int) |
| `agent.spawn` | the new agent | `spiffe_id` (= principal), `parent` (SPIFFE ID of a known principal), `name` |
| `agent.exit` | the agent | `spiffe_id`, `exit_reason` |
| `llm.call` | the caller | `authz_ref`, `model`, `status` (int), `ms` (int), `prompt_tokens`, `completion_tokens` (ints when `status` = 200), `dropped` (bool); `receipt` when `dropped` |
| `tool.call` | the caller | `via` (`gateway`\|`harness`), `tool`; `authz_ref` when `via` = `gateway` |
| `tool.result` | the caller | `call_ref` (event_id of its `tool.call`) |
| `fs.write` | the writer process | `path` |
| `net.connect` | the caller | `authz_ref`, `dest` (`host:port`) |
| `authz.decision` | the caller | `action` (`llm.call`\|`tool.call`\|`net.connect`), `resource`, `args` (object), `decision` (`allow`\|`deny`), `policy_ids` (list, non-empty on deny), `errors` |
| `halt`, `resume` | the target | `receipt`, `action`, `by`, `target` (= principal), `nodes` (list incl. target), `ms` |
| `label` | the run's agent | `run`, `labels` (object; for τ²: `reward`, `termination_reason`), `source` |

## 4. Validity rules and error codes

`validate FILE [--ledger FILE]` applies, in addition to §3 types:

| Code | Rule |
|---|---|
| `E_JSON` | line is not a JSON object |
| `E_SCHEMA` | missing/mistyped field (message names the field) |
| `E_KIND` | unknown `kind` |
| `E_TS` | `ts` not `YYYY-MM-DDTHH:MM:SS.mmmZ` |
| `E_DUP_ID` | `event_id` seen before in the file |
| `E_TS_ORDER` | within one `writer`, `ts` decreases relative to the previous event of that writer in the file |
| `E_SEQ_ORDER` | within one `writer`, `seq` is not greater than the previous event of that writer in the file (gaps are allowed; `completeness` reports them) |
| `E_REF` | `authz_ref` / `call_ref` / `parent` does not name an earlier event (or known principal) of the right kind |
| `E_AUTHZ_MISSING` | `llm.call`, `net.connect`, or `tool.call` via gateway without `authz_ref` |
| `E_AUTHZ_DENIED` | the referenced decision is not `allow`, or is for another principal/action, or is later than the call |
| `E_PRINCIPAL_UNKNOWN` | `principal` has no `vm.boot`/`agent.spawn` in the file or `--ledger` |
| `E_PRINCIPAL_DEAD` | event **start** is outside the principal's life. Start = `ts`, except `llm.call`: `ts - ms`. Life: VM `[vm.boot.ts - boot_ms, vm.exit.ts]`, agent `[agent.spawn.ts, agent.exit.ts]`, open-ended without exit. Exempt (post-hoc records): `vm.exit`, `agent.exit`, `session.close`, `label`, and an `authz.decision` deny whose `policy_ids` carry a receipt (an in-flight answer refused after halt/kill/cut). |
| `E_RECEIPT` | `halt`/`resume` without `hr_xxxx`, or `dropped: true` without `receipt` |
| `E_TOKENS` | `llm.call` with `status` 200 and non-integer token counts |

Output: `{"ok": bool, "events": N, "errors": [{"line": n, "event_id": id|null, "code": ..., "message": ...}]}`.
Report every error you find (at least the first per event); the test looks for the expected `code` **and**
`event_id` pair. Exit 0 iff `ok`. The validator must stream (§10).

## 5. Content capture

Environment `AVOS_TRACE_CONTENT` (`0` default, `1`) read by `ingest`. With `1`, events may carry content:
`llm.call.input` (array of chat messages) and `llm.call.output` (assistant message), `tool.call.args`,
`tool.result.result`, `authz.decision.args`. With `0`, no event may contain prompt, completion, tool argument or
tool result text (hashes allowed); `authz.decision.args` is still kept (it is the audit record of what was
decided — today it is small structured data). The schema and all rules hold in both modes; the projection
in §9 requires `1`.

## 6. Tamper evidence

Per writer stream, in file order (which `E_SEQ_ORDER` makes equal to `seq` order): `prev` = `hash` of the previous
event with the same `writer` in the file (first event: 64 zeros); `hash` = SHA-256 hex of the canonical JSON of the event without `hash`: keys sorted, separators
`,` and `:`, ASCII-escaped (exactly Python `json.dumps(ev, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`).

`verify FILE` → `{"ok": bool, "events": N, "writers": {writer: {"count": n, "head": hash}}, "errors": [{"line", "event_id", "writer", "seq", "code", "message"}]}`,
codes `E_CHAIN_HASH` (event does not hash to its `hash`), `E_CHAIN_PREV` (`prev` ≠ previous head),
`E_CHAIN_GAP` (seq jump; may accompany `E_CHAIN_PREV`). Strength: any edit or deletion not followed by rewriting
every later event of that writer is detected; `head` per writer lets a run's heads be anchored elsewhere
(printed receipt, parent's ledger) against full rewrites. Exit 1 on any error.

## 7. Completeness (M8)

`completeness FILE` → JSON with OpenMetrics-style names:
`{"avos_trace_events_stored_total": n, "avos_trace_events_expected_total": m, "avos_trace_completeness_ratio": n/m,
"gaps": [{"writer": w, "seq": s}, ...], "writers": {w: {"stored", "first_seq", "last_seq"}}}` where
expected = Σ_w (last_seq − first_seq + 1). A single missing `seq` is a gap. Ground truth for a τ² run is the
harness file: one `llm.call` per message that carries `usage` (agent **and** user-simulator turns both go through
the gateway: the real run has 14), one `tool.call` + one `tool.result` per harness tool call, one `label`.

## 8. Queries (`query <sub> ... FILE`)

- `audit --principal P FILE` → NDJSON, one line per event, in `(ts, writer, seq)` order: every event with
  `principal == P` **plus** `halt`/`resume` events whose `nodes` contain P. Each line is the event plus an
  `authority` object: for `llm.call`/`tool.call`/`net.connect` `{"authz_ref", "decision", "policy_ids"}` copied
  from the referenced decision (+ `"receipt"` when dropped); for `tool.call` via harness `{"via": "harness"}`;
  for `authz.decision` `{"decision", "policy_ids"}`; for `halt`/`resume` `{"receipt", "by"}`; otherwise `{}`.
  Extra keys allowed. Cost must not grow with the file: see §10.
- `lineage --principal P FILE` → JSON array of SPIFFE IDs from P up through `agent.spawn.parent` links to the VM
  (last element), e.g. `[".../agent/A/sub/B", ".../agent/A", "spiffe://avoslab.com/vm/vm-01-xxxxxx"]`.
- `cost --run R FILE` → `{"run", "vm_id", "host", "llm_calls", "priced_calls", "prompt_tokens", "completion_tokens",
  "cost_usd", "wall_ms"}` computed **only** from the file: `cost_usd` = Σ over `llm.call` with `run` = R and a `price`:
  `(prompt_tokens × prompt_per_mtok + completion_tokens × completion_per_mtok) / 1e6` (tolerance 1e-9);
  `wall_ms` from `session.close`; `host` from the VM's `vm.boot`. Unpriced calls count in `llm_calls`, not `priced_calls`.

## 9. Chat projection (retraining)

`project chat --run R FILE` → one JSON object (requires a trace ingested with `AVOS_TRACE_CONTENT=1`):

```
{"run": R, "principal": <agent SPIFFE ID>, "reward": <number>, "termination_reason": <string>,
 "usage": {"prompt_tokens": <sum>, "completion_tokens": <sum>}, "messages": [ ... ]}
```

`messages` use the **OpenAI Chat Completions** message format: `{"role": "assistant"|"user", "content": str|null,
"tool_calls": [{"id", "type": "function", "function": {"name", "arguments": <JSON string>}}]}` and
`{"role": "tool", "tool_call_id", "content"}`; per-message `usage` `{"prompt_tokens","completion_tokens"}`
on messages that came from a model call. Every model call and tool call of the original SimulationRun appears
exactly once, in order, with the original ids, arguments and usage (the fixed opening greeting has no usage).

## 10. Scale and clock

- `validate`, `verify`, `completeness` and `query` stream: peak RSS ≤ 512 MiB on a 1,000,000-event file.
- `validate` ≥ 50,000 events/s and `verify` ≥ 40,000 events/s on one core of a laptop.
- `query audit` over 1,000,000 events: first call ≤ 20 s (may build a side index), repeat call ≤ 1.0 s and
  ≤ 3× the same query over 100,000 events. The answer must be derivable per principal without a global scan.
- Clock: `ts` is always the host's clock. Guest clocks (harness message timestamps) go to `guest_ts` only.
  Harness-derived events (in-harness `tool.call`/`tool.result`, the merged content of a model call) are anchored
  to host time by the gateway rows of the same run: a merged `llm.call` keeps the gateway's `ts`; a tool event's
  `ts` lies between the `llm.call` that issued it and the next `llm.call`, inside `[session.open.ts, session.close.ts]`.
  A guest clock skewed by an hour must not change ordering, validity or the projection. Order across writers is
  `(ts, writer, seq)`; within a writer `seq` is authoritative.

## 11. Ingest

```
ingest --ledger EVENTS.jsonl -o OUT.jsonl [--host NAME] [--prices PRICES.json]
ingest --run-dir DIR         -o OUT.jsonl [--host NAME] [--prices PRICES.json]
```

- `--ledger`: a POC 1–5 `events.jsonl` (rows exactly as `infra/vm`, `infra/job`, `identity/registrar.py`,
  `gateway/gateway.py`, `supervisor/ctl.py` write them today). Output: one event per input row and kind, plus one
  `tool.result` per legacy `tool.call` (its inline `result`). Links are derived from the ledger itself: a
  `tool.call`'s `authz_ref` is the earliest unconsumed `allow` for the same principal, tool and args; an
  `llm.call`'s the earliest unconsumed `allow` for the same principal and model; a dropped `llm.call`'s `receipt`
  is the `hr_` id in the delivery-time deny that precedes it. Legacy `halt`/`resume` rows whose `nodes` omit
  their own `target` (the POC 5 VM-pause rows) get the target added to `nodes`; everything else in `nodes` is kept.
  `--host` overrides the host name (default: the
  `host` of the `vm.boot` rows); event ids from different hosts never collide.
- `--run-dir`: a directory with `events.jsonl` (the host's rows for that VM), `result.json` (τ² SimulationRun)
  and `job.json`. The harness's messages are merged into the trace: each message with `usage` is the content
  of one gateway `llm.call` (same order; the token counts match), in-harness tool calls become
  `tool.call`(`via: "harness"`, `call_id`, `tool`, `args`) + `tool.result`(`call_ref`, `result`), and the run's
  `reward`/`termination_reason` become one `label` event by the agent.
- `--prices`: `{model: {"prompt_per_mtok", "completion_per_mtok", "currency"}}`; sets `price` and `cost_usd`
  on each priced `llm.call`.
- The output must pass `validate` and `verify` as a standalone file.
