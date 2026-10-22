# POC 6 results

`just poc6` on a laptop (Apple silicon, one core per CLI), 2026-10-21. Design: `docs/trace.md`. Interface and
acceptance suite: `trace/CONTRACT.md`, `trace/tests/` (written independently, before the solution).

Built: `trace/schema.json` (JSON Schema 2020-12, 15 kinds), `trace/bin/{ingest,validate,verify,completeness,query,
project}` and `trace/lib_trace.py` (≈ 880 lines in total, standard library only, every CLI streams). The
emitters are unchanged; `ingest` converts today's ledger rows and a τ² run into contract events. Inputs:
`trace/tests/fixtures/real_events.jsonl` (5,516 host rows, POC 4–5) and `data/poc6/run_retail-0_9/` (one real run,
both halves: 34 host rows + `result.json`).

| Done test | Result |
|---|---|
| 1. Schema, validator and lifecycle doc exist | `trace/schema.json` names all 15 kinds; `validate` reads its field table from it. Lifecycle, writers, clock and order: `docs/trace.md`. |
| 2. One real run converted and validated, every model and tool call exactly once | `run_retail-0_9` → 45 events; `validate` and `verify` ok with content on and off. **14/14** gateway `llm.call`s carry the harness content (9 agent + 5 user-simulator turns, matched by usage sequence, 0 unmatched), **5/5** tool calls + 5 results, 1 `label` (reward 1.0, `user_stop`). M8 on the run: **31/31** against the harness ground truth, 45/45 by writer `seq`, no gaps. The real ledger window: 5,516 rows → 6,043 events (+527 `tool.result`), valid, chain intact, 6,043/6,043. |
| 3. Audit and training queries correct | Audit of 4 halted agents, a VM and a sub-agent: every event in host order with its authority (decision + policy ids, receipt for the 4 dropped answers). Lineage reaches the VM for all 119 agents (49 sub-agents). Chat projection of the real run: 20/20 messages, 5 tool calls with original ids and arguments, per-message usage; also 3 `data/poc2` runs and a guest clock skewed by 1 h (identical output). Cost of the run from the trace alone: 14 priced calls, 66,832 + 1,234 tokens, **$0.0070534**, wall 30,692 ms, host `ip-172-31-44-187`. |
| 4. Broken files fail | 16 golden files, one rule broken each: every one fails with the expected `code@event_id`; edits, deletions and re-hashed edits are caught by `verify` at the event or its successor. |

Acceptance suite: `--quick` **55/55** pass (9 s), full **60/60** pass (58 s), selftest 46/46. Re-run independently on
the Linux dev host (c8i.large, Ubuntu 24.04, Python 3.12) via `just poc6`: **60/60** in 52 s; `validate` 114,200 ev/s
at 174 MiB, `verify` 105,500 ev/s, cold audit 4.3 s, warm 0.05 s (ratio 1.0), `completeness` 282,800 ev/s. The
real-run outputs on the host matched the laptop (45 events, 14 priced calls, $0.0070534, 20 messages, reward 1.0).
Regressions with the POC 5 stats fix: `just poc5` M6 median 28 ms / worst 49 ms, M7 0; `just poc4` M5 5/5.

## Measured (full suite, 1,000,126 synthetic events with the real kind mix, 533 B median)

| Item | Measured | Bar |
|---|---|---|
| `validate` | 93,800 ev/s, 166 MiB peak | ≥ 50,000 ev/s, ≤ 512 MiB |
| `verify` | 99,400 ev/s, 15 MiB | ≥ 40,000 ev/s, ≤ 512 MiB |
| `completeness` | 256,000 ev/s, 15 MiB | ≥ 40,000 ev/s, ≤ 512 MiB |
| `query audit`, first call (builds the per-principal index) | 4.5 s, 60 MiB | ≤ 20 s |
| `query audit`, repeat (M9) | 0.06 s at 1M, 0.03 s at 100k (ratio 1.1) | ≤ 1.0 s, ratio ≤ 3 |
| Event size, real ledger, content off | median 613 B, max 739 B | ≤ 768 B, ≤ 4,096 B |
| Real run trace with content | 45 events, 42 KB | — |
| Ids from 8 concurrent ingests with different hosts | 48,344 ids, 0 collisions | 0 |

`validate` holds one entry per event id (needed for duplicate and reference checks), so its memory grows with
the file: ≈ 170 MiB per million events. All other CLIs use constant memory or memory proportional to principals.

## Contract disputes (resolved by the tester, 2026-10-21)

- **VM-pause `nodes`.** The POC 5 emitter wrote VM-pause `halt`/`resume` rows (`hr_c5c9`, `hr_6053`, `hr_7599` and
  their resumes) whose `nodes` omitted the VM itself, so L05 (nodes unchanged) and K02/§3 (target ∈ nodes) could not
  both hold. Ruling: ingest adds the target to such legacy rows (§11) and L05 compares against legacy nodes ∪ {target};
  `supervisor/ctl.py` now lists the target first in new receipts.
- **`seq` order within a writer.** The 1M fixture had file order ≠ `seq` order inside a writer. Ruling: `seq` must
  increase in file order per writer (new validator code `E_SEQ_ORDER`, gaps allowed), the chain is defined in that
  order, and the generator was fixed. `verify` reports `E_CHAIN_GAP` together with the `E_CHAIN_PREV` it explains.

## Tech debt

| Item | Effect today | Fix | When |
|---|---|---|---|
| **Seq, ids and chain are stamped at ingest, not at emission.** The emitters still write POC 1–5 rows; `ingest` numbers each writer by its position in the input file. | Two conversions of overlapping slices of one ledger (a run directory and the whole host log) number the same writer differently and must not be concatenated. Deletions from the raw ledger *before* ingest are invisible. | Each emitter keeps its own `seq` and head and writes contract events directly (`lib_trace` already has the id and chain code); the gateway stamps `authz_ref` instead of `ingest` pairing allows FIFO. | POC 7, before traces are shipped off the host. |
| **No model prompts.** Neither the gateway nor τ² records what was sent to the model. | `llm.call.input` holds only messages no other event carries (τ²'s greeting); the projection rebuilds the conversation, not the exact prompt (system prompt, user-simulator view). | Gateway hashes the request body (`input_sha256`) always and stores it when content capture is on. | When training needs exact prompts (POC 8+). |
| ~~**Audit index trusts size + mtime.**~~ **Fixed 2026-10-22.** | It happened: on the dev host an index left in `/tmp/avos-trace-index` by an earlier run matched a different file at the same path with the same size and mtime, and `query audit` silently answered 0 of 14 events for check A01. | The index is now one per path and valid only while the file's device, inode, size, mtime and SHA-256 of its first and last 64 KB match (checked on every use); every line fetched by offset must be an event of the principal asked for, or the index is rebuilt once and, if it still disagrees, the query fails rather than answers. | Done. |
| **Legacy VM-pause rows are patched at ingest.** Rows written before the 2026-10-21 `ctl.py` fix get the VM added to `nodes`. | The stored event differs from the raw row in that one field (L05 checks the union). | None needed for new rows; drop the patch once no pre-fix ledger is converted. | When POC 1–5 ledgers are retired. |
| **`fs.write` has no emitter.** | The kind exists in the schema and validator only. | Guest-side writer for `/work`, when file outputs matter (mini-swe-agent). | With the coding harness. |
