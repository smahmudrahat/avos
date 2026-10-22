# Trace

The trace is the one record of what happened in Avos: every VM, agent, model call, tool call, decision and control,
in a form that can be audited per principal, priced per run and turned back into training data. Interface:
`trace/CONTRACT.md`. Schema: `trace/schema.json`. Tools: `trace/bin/` (`ingest`, `validate`, `verify`,
`completeness`, `query`, `project`), shared code in `trace/lib_trace.py`. Results: `data/poc6/README.md`.

## 1. Envelope
Every event is one JSON line with: `event_id`, `kind`, `ts`, `writer`, `seq`, `host`, `principal`, `vm_id`, `prev`,
`hash`. A **writer** is one emitting stream on one host; `seq` counts its events from 1 with no gaps. Since POC 7 the
emitters write these events themselves, one stream per VM (`<host>/gateway/<vm_id>`, `<host>/registrar/<vm_id>`,
`<host>/ctl/<vm_id>`, `<host>/vm/<vm_id>`, `<host>/job/<vm_id>`; `docs/architecture.md` §7). `ingest` still converts
POC 1–5 ledger rows, under the per-host writers `<host>/gateway`, `<host>/registrar`, ... and `<host>/harness/<run>`. The **principal**
is who the event is about (a VM, an agent or a sub-agent, by SPIFFE ID) and is the partition key.

**Event id** = 12 hex of the host-clock milliseconds, 12 hex of SHA-256 of the writer name, 10 hex of `seq`
(36 characters, e.g. `01a15fce2ef6-96a1c0357590-0000000a54`). Ids sort by time, are minted by the writer alone,
and cannot collide: `seq` never repeats within a writer, and the writer name starts with the host.

## 2. Event catalog
| Kind | Principal | Fields beyond the envelope | Written by |
|---|---|---|---|
| `vm.boot` / `vm.exit` | the VM | `vm`, `boot_ms` / — | `infra/vm` / registrar |
| `session.open` / `session.close` | the VM | `run`, `harness`, `task` / `run`, `status`, `wall_ms` | `infra/job` |
| `agent.spawn` / `agent.exit` | the agent | `spiffe_id`, `parent`, `name` / `spiffe_id`, `exit_reason` | registrar |
| `authz.decision` | the caller | `action`, `resource`, `args`, `decision`, `policy_ids`, `errors` | gateway |
| `llm.call` | the caller | `authz_ref`, `model`, `status`, `ms`, `prompt_tokens`, `completion_tokens`, `dropped`, `receipt` if dropped; `response_id`, `system_sha256`, `tools_sha256`, `messages_sha256`, `output_sha256` (§5); `price`, `cost_usd`; merged: `role`, `guest_ts`, `input`, `output` | gateway (+ harness content) |
| `tool.call` / `tool.result` | the caller | `via`, `tool`, `authz_ref` (gateway), `call_id` (harness), `args` / `call_ref`, `result`, `error` | gateway or harness |
| `net.connect` | the caller | `authz_ref`, `dest` | gateway |
| `fs.write` | the writing process | `path` | (reserved; no emitter yet) |
| `halt` / `resume` | the target | `receipt`, `action`, `by`, `target`, `nodes`, `ms` | `supervisor/ctl.py` |
| `label` | the run's agent | `run`, `labels` (`reward`, `termination_reason`), `source` | harness |

**Authority is always a stamped reference.** A gated event (`llm.call`, `net.connect`, gateway `tool.call`) names
the `authz.decision` that allowed it in `authz_ref`; a dropped answer names the halt or cut in `receipt`. Nothing is
inferred from parents or timestamps. The gateway stamps `authz_ref` with the decision's own id when it writes the call,
and a dropped answer's `receipt` from the kill switch it just read (since POC 7 it writes no second, delivery-time
deny: one request, one decision). For POC 1–5 ledger rows, which have no ids, `ingest` stamps the links once, from
the ledger itself: the earliest unused `allow` for the same principal and model (or tool and arguments), and for a
dropped answer the `hr_` receipt in the deny the gateway logged when it refused delivery. Every decision and call
written since POC 7 also carries `attempt`, the forwarder's per-principal request number (M8, `docs/architecture.md` §7).

## 3. Lifecycle
A principal is **born** at `vm.boot.ts − boot_ms` (the create request; the gateway can see the VM before boot
finishes) or at `agent.spawn.ts`, with `parent` naming a known principal. It is **working** while its events start
inside its life (an `llm.call` starts at `ts − ms`). It **exits** at `vm.exit` / `agent.exit`, or is **halted**
(`halt` … `resume`, receipts `hr_xxxx`, `nodes` = every principal frozen). A run is **labeled** when the host closes
the session: one `label` event carries the harness's verdict. Records written after the fact (`vm.exit`,
`agent.exit`, `session.close`, `label`, and a deny carrying a receipt) are exempt from the liveness rule.

## 4. Two writers, reconciled
The host rows are the **evidence**: the gateway saw every model call and decided it. The harness transcript
(`result.json`) is a **witness**: it knows what was said and which tools ran inside the harness, but runs on a clock
and in a process the agent controls. `ingest --run-dir` merges the witness into the evidence and never the reverse:
- Each harness message with `usage` is matched, in order, to the next gateway `llm.call` of the run's agent with the
  same `prompt_tokens` and `completion_tokens`. The call keeps the gateway's `ts`, status, latency and tokens; it gains
  `role` and `guest_ts`, and with content capture on its `output` (the message) and `input` (messages new since the
  previous call that no other event carries, such as τ²'s fixed greeting). Gateway calls with no match stay, without
  content; harness messages with no match make `ingest` exit 1 (no event can vouch for them).
- In the real run `run_retail-0_9`, the gateway logged **14** calls and the transcript has **10** assistant turns. The
  difference is the user simulator, which runs in the same process under the same identity: 9 agent calls + 5
  user-simulator calls = 14, all matched by usage sequence, 0 left over. The tenth assistant turn is the greeting,
  which no model produced.
- In-harness tool calls (τ² runs its tools in-process, so no gateway sees them) become `tool.call` (`via: "harness"`,
  `call_id`, `tool`, `args`) and `tool.result` (`call_ref`, `result`) on the writer `<host>/harness/<run>`,
  timestamped 1 ms after the host time of the model call that issued them. That `ts` is an anchor for ordering, not
  an observation: nothing outside the harness saw the tool run, which is what `via: "harness"` records.
- Cost uses the gateway's tokens (observed); the harness's own usage is advisory and only used to match. Failed
  and partial runs are kept: without `result.json` the host rows are ingested alone.

## 5. Projections
- **Audit** (`query audit --principal P`): every event of P plus each halt/resume whose `nodes` include P, in
  `(ts, writer, seq)` order, each with its `authority` (decision and policy ids, receipt, or `via: harness`). The
  first query writes a side index under `$TMPDIR`: per principal, the byte offsets of its lines, in 256 hash buckets
  (checked against the file on every use, §9).
  A repeat query reads one bucket and P's lines, so its cost follows P's size, not the file's.
- **Chat** (`project chat --run R`): the run as OpenAI Chat Completions messages with the original tool call ids,
  arguments and per-message usage, plus reward and termination reason from the `label`. Requires content capture.

**Content capture** (`AVOS_TRACE_CONTENT=1`, off by default) adds message text, harness tool arguments and results;
off, only their SHA-256 (`output_sha256`, `args_sha256`, `result_sha256`) is kept. Gateway tool calls keep `args` in
both modes: they equal the decision's `args`, which is the audit record. A tool result that is JSON text is stored
as JSON when that is lossless.

**Model-call hashes** (since POC 8, always on, no text). The gateway holds each request and reply whole, so it adds
to the `llm.call` it writes what the model was shown and what it said, as hashes only: `response_id` (the upstream
reply's `id`), `system_sha256` (the system message's content), `tools_sha256` (the tool schemas sent; `null` when
none were), `messages_sha256` (each request message, in order) and `output_sha256` (the reply). Every hash is
`content_sha(chat_form(x))` from `trace/lib_trace.py`: a message reduced to role, content (empty = none), tool calls
as (id, name, parsed arguments) and `tool_call_id`, then SHA-256 of its JSON with nulls dropped, keys sorted, compact.
That is the form a reader of the harness transcript can compute too: on a real run the τ² transcript reproduces all
14 replies and all 9 agent prompts hash for hash, though the transcript stores parsed arguments and the client adds
fields of its own on the wire. Nothing here needs content capture, and nothing on the host holds the text.
(`messages_sha256` is a list, so it does not reuse POC 6's `input_sha256`, a string. The 14 calls of
`run_retail-0_15` carry a pre-release form of these fields, hashes of the raw wire messages under `input_sha256`
as a list; the schema accepts it and §10 compares only what that form allows.)

## 6. Tamper evidence
Within a writer, file order must equal `seq` order: each event's `seq` is greater than the previous one of that writer
in the file (`validate`: `E_SEQ_ORDER`; gaps are allowed). The chain is defined in that order. Per writer, `prev` is
the previous event's `hash` (the first: 64 zeros) and `hash` is SHA-256 of the event's
canonical JSON without `hash` (sorted keys, compact, ASCII-escaped). `verify` recomputes the chain without the writer:
an edit shows as `E_CHAIN_HASH` at the event, a re-hashed edit as `E_CHAIN_PREV` at the next event of that writer,
and a deletion as `E_CHAIN_PREV` there plus `E_CHAIN_GAP` naming how many `seq` are missing. A `seq` jump with the
chain intact is not tampering: it is a gap for `completeness`. Only rewriting a writer's whole tail goes unnoticed; the per-writer `head` that `verify`
prints is what to anchor elsewhere (a printed receipt, the parent's ledger) against that.

## 7. Completeness (M8)
M8 = events stored ÷ events that happened. Per writer, `seq` says how many happened: expected =
Σ (last_seq − first_seq + 1), and every missing `seq` is named as a gap (`completeness`). Since `seq` only grows in
file order (§6), a gap is an event that was lost, never one written out of place. For a τ² run the harness is
the second ground truth: one `llm.call` per message with usage, one `tool.call` + `tool.result` per harness tool
call, one `label`. A writer that loses its tail cannot be seen by `seq` alone; its anchored `head` shows it.

## 8. Clock, partitioning, order
- **Clock:** `ts` is always the host's clock, UTC, ms (`llm.call`: completion time). Guest clocks go to `guest_ts`
  and are never used for order or validity; a harness clock off by an hour changes nothing (check Q01).
- **Partition = UTC day** of `ts`, then **principal**; **order within = `vm_id`, `principal`, `ts`**; across writers
  the total order is `(ts, writer, seq)`, within a writer `seq` is authoritative. Ids are local, so nothing needs a
  global index or sequencer to be correct; an index is a derived cache that can always be rebuilt from the file.

## 9. Known limits
- **Seq and links of POC 1–5 rows are stamped at ingest.** For those rows `ingest` assigns `seq`, ids and the chain
  from the order of the ledger file, so two conversions of overlapping slices number the same writer differently and
  must not be concatenated. Events collected since POC 7 carry id, `seq` and chain from emission; `ingest --run-dir`
  on a run copied from the store (`infra/job` writes `events.jsonl` with `avos trace --run`) still re-chains them,
  because merging the witness's content changes the model calls: its output is a derived training file, the store
  remains the record.
- **No model input text is recorded.** The gateway does not log prompts and τ² does not store them, so
  `llm.call.input` holds only the messages no other event carries. Since POC 8 the gateway hashes every prompt
  message (§5): the text comes from the witness, and the host can tell whether it is the text the model saw.
- **The user simulator shares the agent's identity.** τ² runs the simulated user in the same process, so the
  gateway attributes its calls to `agent/tau2`. Since POC 8 the host tells them apart by what it saw: agent calls
  carry the agent's system prompt and tools, simulator calls carry neither (`tools_sha256` null), and §10 checks
  that against the witness's `role`. Cost per task (M12) is split that way; the training projection treats the
  simulator's turns as the environment, never as policy output. Authority still does not separate: the fix belongs
  in the harness image (start the simulator as its own principal, which POC 3 gives for free).
- **VM pauses:** the POC 5 emitter omitted a VM target from its own `nodes` until 2026-10-21; `ingest` adds it to
  those legacy rows.
- **The side index is a cache that is checked, not trusted.** It is valid only while the file's device, inode, size,
  mtime and first/last 64 KB match what it was built from, and every line it points at must be an event of the
  principal asked for; otherwise it is rebuilt, and a query never answers from an index that disagrees with the file.
  (Until 2026-10-22 it trusted size and mtime alone, and a stale index for a different file at the same path once
  answered an audit with nothing.) An edit in the middle of a file that keeps its size, mtime, inode and both ends
  still slips past the fingerprint, though not past the per-line check or `verify`.

## 10. Training store (POC 8)
`retrain/bin/retrain` turns runs into training data and into a labeler's view (`retrain/CONTRACT.md`; results:
`data/poc8/README.md`). It keeps no store of its own: an **item** (one run, one principal) is a pure function of
that run's files, the VM's streams, its counters, its `result.json` and the pinned τ² metadata
(`retrain/tau2_pinned.json`: the 16 retail tool schemas and `llm_agent`'s prompt template at `96a3ef9`), rebuilt on
every call. There is no cache to go stale, and nothing is written under the three roots; a dataset version is the
export directory plus its manifest.

- **Evidence.** A run's host rows come from the store (`evidence: store`, M8 exactly as `avos completeness`), else
  from its POC 1–6 ledger rows converted as `ingest --ledger` does (`ledger`: ids stamped by that conversion, so not
  the store's; `unverified`), else from nowhere (`witness`: exported only on request, nothing trained). Each stream's
  chain is recomputed on every read; a row edited in place is a problem on every item of the run, not a different
  answer. The `events.jsonl` copy `infra/job` leaves in a run directory is never read.
- **Ids are the store's.** Every model-produced message names its stored `llm.call` (`event_id`, host `ts`), and
  `lineage.events` lists every stored event the item was built from. Nothing is re-minted or re-chained: this retires
  the re-chain of §9 for training data (`ingest --run-dir` still re-chains for its own derived file).
- **Join.** A witness message with `usage` carries the upstream reply id (`raw_data.id`); when every call has a
  `response_id`, they join on it, in order and with equal token counts. Runs from before POC 8 have no ids on the host
  and join by §4's token-count order. `witness.join` says which (`response_id`, `tokens`, `count` for coding runs). Only calls to the models the witness names
  (`info.agent_info.llm`, `info.user_info.llm`) take part: τ²'s NL-assertion judge calls a different model under the
  same identity after the conversation, so its calls grade the run rather than speak in it. They stay in the cost and
  the lineage and are listed in `witness.other_calls` with their model and tokens; a judge configured with the agent's own model would be a
  leftover call, so the run would be flagged, never silently merged.
- **Accepted only where the host vouches for it.** Where the call carries the §5 hashes: an agent turn (witness role
  `assistant`) must have been sent the item's system prompt (`system_sha256`) and tools (`tools_sha256`); a
  simulator turn (`user`) must not have been (the host's own view of who spoke); the turn's content must hash to
  `output_sha256`; and an agent turn's prompt, every item message before it, must hash message for message to
  `messages_sha256`. That covers in-harness tool results and the fixed greeting as well, since they are part of
  every later prompt. `witness.checked` lists what was compared, named after the host fields (`tokens` only for older runs, `count` for coding runs; `role`, `system`,
  `tools`, `tokens` for the pre-release form; all six since). Any mismatch is a `witness.problems` entry: the item is
  written and viewable, every `train` flag is false, no `sft.jsonl` row comes from it, and `export` exits 1. The host
  rows are never changed to fit the witness, and the witness is never trimmed to fit the host.
- **What is trained.** `train` is true only on an assistant turn matched to a call of this principal. The system
  prompt, τ²'s fixed greeting (no call), the simulator's turns and tool results are context. Two forms:
  `trajectories.jsonl` (whole conversation, per-message `train`, the `tools` column: Axolotl `chat_template` with
  `message_field_training`) and `sft.jsonl` (one prompt→completion row per policy turn, for TRL's completion-only
  loss: TRL's `messages` form with `assistant_only_loss` would train the greeting, which the template marks as an
  assistant turn).
- **Per principal.** Each principal with a model or tool call is an item. Principals with no witness, such as a POC 7
  tree, are what the host saw: one content-less assistant message per `llm.call`, each gateway tool call and result
  with the store's ids. A parent lists its children's item ids and outcomes.
- **Versioning.** `item_id` is the SHA-256 of the canonical item, so ids change with content (a redaction hook gives
  new ids). The manifest lists items in file order with split, outcome, M8 status and witness verdict, and
  `dataset_sha256` hashes the id list: re-exporting the same sources gives the same bytes, wherever the roots live.
  Datasets from separate hosts merge by concatenating items and re-sorting. Splits come from
  `data/splits/retail_user_disjoint_v1.json`; a held-out task is never `train`.
- **Statistics** (`stats.json`): policy turns and messages per item, the share of policy turns that call a tool,
  which tool is called at which message index, the most common (tool, index) with a flag at ≥ 90 % of items (the
  published τ-bench failure had 98 % at one index), and exact duplicates.
