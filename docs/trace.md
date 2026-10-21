# Trace

The trace is the one record of what happened in Avos: every VM, agent, model call, tool call, decision and control,
in a form that can be audited per principal, priced per run and turned back into training data. Interface:
`trace/CONTRACT.md`. Schema: `trace/schema.json`. Tools: `trace/bin/` (`ingest`, `validate`, `verify`,
`completeness`, `query`, `project`), shared code in `trace/lib_trace.py`. Results: `data/poc6/README.md`.

## 1. Envelope
Every event is one JSON line with: `event_id`, `kind`, `ts`, `writer`, `seq`, `host`, `principal`, `vm_id`, `prev`,
`hash`. A **writer** is one emitting stream on one host (`<host>/gateway`, `<host>/registrar`, `<host>/ctl`,
`<host>/vm`, `<host>/job`, `<host>/harness/<run>`); `seq` counts its events from 1 with no gaps. The **principal**
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
| `llm.call` | the caller | `authz_ref`, `model`, `status`, `ms`, `prompt_tokens`, `completion_tokens`, `dropped`, `receipt` if dropped; `price`, `cost_usd`; merged: `role`, `guest_ts`, `input`, `output` | gateway (+ harness content) |
| `tool.call` / `tool.result` | the caller | `via`, `tool`, `authz_ref` (gateway), `call_id` (harness), `args` / `call_ref`, `result`, `error` | gateway or harness |
| `net.connect` | the caller | `authz_ref`, `dest` | gateway |
| `fs.write` | the writing process | `path` | (reserved; no emitter yet) |
| `halt` / `resume` | the target | `receipt`, `action`, `by`, `target`, `nodes`, `ms` | `supervisor/ctl.py` |
| `label` | the run's agent | `run`, `labels` (`reward`, `termination_reason`), `source` | harness |

**Authority is always a stamped reference.** A gated event (`llm.call`, `net.connect`, gateway `tool.call`) names
the `authz.decision` that allowed it in `authz_ref`; a dropped answer names the halt or cut in `receipt`. Nothing is
inferred from parents or timestamps. Today's ledger has no ids, so `ingest` stamps the links once, from the ledger
itself: the earliest unused `allow` for the same principal and model (or tool and arguments), and for a dropped answer
the `hr_` receipt in the deny the gateway logged when it refused delivery.

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
  first query writes a side index under `$TMPDIR`: per principal, the byte offsets of its lines, in 256 hash buckets.
  A repeat query reads one bucket and P's lines, so its cost follows P's size, not the file's.
- **Chat** (`project chat --run R`): the run as OpenAI Chat Completions messages with the original tool call ids,
  arguments and per-message usage, plus reward and termination reason from the `label`. Requires content capture.

**Content capture** (`AVOS_TRACE_CONTENT=1`, off by default) adds message text, harness tool arguments and results;
off, only their SHA-256 (`output_sha256`, `args_sha256`, `result_sha256`) is kept. Gateway tool calls keep `args` in
both modes: they equal the decision's `args`, which is the audit record. A tool result that is JSON text is stored
as JSON when that is lossless.

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
- **Seq and links are stamped at ingest, not at emission.** The emitters still write today's rows; `ingest` assigns
  `seq`, ids and the chain from the order of the ledger file. Two conversions of overlapping slices of one ledger
  (a run directory and the whole host ledger) number the same writer differently and must not be concatenated.
  Moving id, `seq` and chain into the emitters closes this.
- **No model input is recorded.** The gateway does not log prompts and τ² does not store them, so `llm.call.input`
  holds only the messages no other event carries; the full prompt is the projection up to that call.
- **The user simulator shares the agent's identity.** τ² runs the simulated user in the same process, so the
  gateway attributes its 5 calls to `agent/tau2` and only the witness's `role` tells them apart. Cost per task
  (M12) must therefore be split by `role`, and the training projection must treat `role: user` turns as the
  environment, never as policy output. The fix belongs in the harness image: start the simulator as its own
  principal (POC 3 gives one for free), so authority and cost separate without the witness.
- **VM pauses:** the POC 5 emitter omitted a VM target from its own `nodes` until 2026-10-21; `ingest` adds it to
  those legacy rows.
- **The side index trusts file size and mtime** to detect a changed file; an in-place edit that keeps both would be
  served from a stale index (`verify` still sees it).
