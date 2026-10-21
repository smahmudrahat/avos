# POC 6 acceptance suite

```
python3 trace/tests/run.py              # full: all checks incl. 1M-event scale (≈ 3 min on an M2 laptop)
python3 trace/tests/run.py --quick      # skips S01–S05
python3 trace/tests/run.py --selftest   # proves the suite's oracles on the golden fixtures (no solver needed)
python3 trace/tests/run.py --only P0    # prefix filter
```

Exit 0 only when every executed check passes. Each line: `PASS|FAIL|SKIP  id  [claim/metric]  measured vs threshold`.
A missing CLI is reported as `FAIL ... not implemented: trace/bin/<name>`.

Black box: only the paths in `trace/CONTRACT.md` are run. Every expected answer is computed independently in
`lib.py` from raw data (the legacy ledger, the τ² `result.json`, or a generator in `synth.py`), never from the
solver's output. **Real** = uses `fixtures/real_events.jsonl` (5,516 rows of the host ledger, 2026-10-21 07:49 →
end: POC 4–5 era, all `avoslab.com`, every VM boot carries `vm_id`) or `fixtures/run_retail-0_9` (one real
τ² run with both halves). **Synthetic** = generated.

## Fixtures

| Path | What |
|---|---|
| `fixtures/real_events.jsonl` | Real host ledger window (vm.boot/exit, session.open/close, agent.spawn/exit, 4,046 authz.decision, 527 tool.call, 375 llm.call incl. 4 dropped, 84 halt, 78 resume). |
| `fixtures/run_retail-0_9/` | Real run: `result.json` (20 messages, 14 with usage, 5 tool calls, reward 1.0), `job.json`, `events.jsonl` (34 host rows). |
| `fixtures/run_retail-{0_2,2_3,8_1}/` | `data/poc2` harness files (pre-gateway, so the tests synthesize the host half in today's emitter shapes). |
| `fixtures/golden/good.jsonl` | 23 hand-sized contract events: VM, agent, sub-agent, allow/deny, halt, dropped answer, resume, net, fs, label. |
| `fixtures/golden/bad_*.jsonl`, `bad_cases.json` | One rule broken per file; the expected `code@event_id`. |
| `fixtures/golden/mini_run/` | 8-message τ² run + host rows + `expected_chat.json`. |
| `fixtures/prices.json` | Price table for the economics checks. |

Regenerate goldens with `python3 trace/tests/synth.py golden` (selftest asserts they are reproducible).

## Checks

| id | claim / metric | proves | threshold and why | data |
|---|---|---|---|---|
| V01 | audit, M8 | A machine-readable schema exists and names every event kind | `$schema` present; 15 kinds named. Without it there is nothing to validate downstream POCs against. | — |
| V02 | audit, M8 | Validator accepts a valid trace, counts events | rc 0, `ok`, `events` = 23 | golden |
| V03–V17 | audit | Validator rejects each broken file (incl. `E_SEQ_ORDER`: two events of one writer swapped in the file, chain intact in seq order) for the right reason and names the event | rc 1 and the expected `code@event_id` present. "Right reason" matters because POC 7 will act on codes (drop vs. quarantine). | golden |
| V20 | audit, M8 | The real POC 1–5 ledger, ingested, validates | rc 0. The statement's "done": a real trace validates. | real |
| V21, V22 | retraining / privacy | Real run validates with content on and off | rc 0 both. Schema must hold both ways. | real |
| V23 | contract | Bad input → exit 2, no traceback | Operability of the CLI in POC 7 scripts. | — |
| L01 | M8 | Ingest keeps one event per legacy row per kind (+ `tool.result` split) | counts equal; the ingest cannot silently drop rows. | real |
| L02 | M3, audit | Every principal is in the ledger and alive at the event's start | 0 unknown, 0 dead across 5,500+ events; VM life starts at `vm.boot.ts − boot_ms`, `llm.call` starts at `ts − ms` (both observed in the real ledger). | real |
| L03 | M5, audit | Every gateway `tool.call` links to the `allow` that permitted it: same principal, tool, args, earlier, used once | 527/527 (FIFO pairing on the raw ledger pairs all of them). | real |
| L04 | M5, audit | Every `llm.call` links to its allow; usage/status/ms preserved in order | 375/375 | real |
| L05 | claim 1 | Receipts, targets, nodes and the 4 dropped-answer receipts survive ingest | receipts exact; `nodes` == legacy nodes ∪ {target} (three POC 5 VM-pause rows omitted their own target; ingest adds it, §11); the receipt of a dropped answer is the `hr_` in the delivery-time deny. | real |
| L06 | M3, audit | `query lineage` reaches the VM for all 119 agents (incl. sub-agents) | equals the spawn-parent chain. | real |
| L07 | scale | Ids minted on 8 "hosts" concurrently never collide | 0 collisions over 44k+ ids; ids ≤ 64 chars. Trillion-agent target forbids a central id service. | real ×8 |
| L08 | scale | Per-event size without content | max ≤ 4,096 B, median ≤ 768 B. Real rows average 316 B; the contract adds ids, writer/seq, chain (≈ 250 B); 2.4× median headroom, no blobs. | real |
| C01 | M8 | Counters report 10,000/10,000 on an intact trace | exact | synthetic |
| C02 | M8 | One dropped event in 10,000 is detected and named | stored 9,999, expected 10,000, ratio 0.9999, gap `{writer, seq}` exact. M8's unit is 1 event. | synthetic |
| C03 | M8 | Real run: stored == happened per kind | 14 `llm.call` (messages with usage: 9 agent + 5 user-sim), 5 `tool.call`, 5 `tool.result`, 1 `label`, lifecycle rows; CLI reports no gaps. | real |
| P01 | retraining | Mini run projects to its golden chat | normalized equality + reward/termination/usage/principal | golden |
| P02 | claim 3, retraining | Real run reconstructed as OpenAI-format chat | every message, tool call (id, name, args), tool result, usage, exactly once and in order; reward 1.0, `user_stop`. Expected built from `result.json` in the test. | real |
| P03–P05 | retraining | Three `data/poc2` runs (one with reward 0.0, one with 10 tool calls) | same comparison | poc2 + synthetic host rows |
| P06 | retraining, M8 | Trace-level: `llm.call` usage sequence == harness sequence; tool events carry harness ids/args; `llm.call.ts` == gateway ts | exact. Stops "stuff the whole result.json into one event". | real |
| P07 | privacy, audit | `AVOS_TRACE_CONTENT=0` leaks no message/argument text; `=1` carries all | 0 of 23 needles with content off, all with content on; max event ≤ 4,096 B off | real |
| P08 | retraining | The result is a `label` event by the agent | exactly one; `reward`, `termination_reason` equal `job.json` | real |
| A01 | M9, audit | Audit of a sub-agent: all its events, in order, each with authority | ids order == oracle; authority ⊇ oracle | golden |
| A02 | M9, audit | Audit of real principals (4 halted agents, a VM, a sub-agent) | same | real |
| A03 | clock, M9 | Events from 3 writers on 2 hosts, written out of order, with a +1 h guest clock, sort by host `(ts, writer, seq)` | exact order; file order differs from expected | synthetic |
| T01 | trust | Verifier accepts an intact chain, reports per-writer heads | heads == recomputed | golden |
| T02 | trust | A field edited after the fact is detected, event named | `E_CHAIN_HASH@id` | golden |
| T03 | trust | An event deleted mid-file is detected at its successor | `E_CHAIN_PREV|E_CHAIN_GAP` at the next event of that writer | real |
| T04 | trust | Editing *and* re-hashing an event is caught at the next link | `E_CHAIN_PREV` at the successor. Strength chosen: per-writer SHA-256 chain, detects anything short of rewriting the whole tail; heads allow external anchoring. | real |
| T05 | trust | The solver's chain is the pinned algorithm (independent recomputation) | 0 breaks. Without pinning, `verify` could be a no-op. | real |
| T06 | trust | Verifier accepts the ingested real trace | ok, events == rows | real |
| K01 | claim 1, M7 | Each dropped answer is `dropped: true` with the receipt of a halt whose target is a prefix of the principal and precedes it; audit shows the receipt | 4/4 | real |
| K02 | claim 1 | Every halt/resume: target == principal ∈ nodes, nodes under target and in the ledger, receipts unique | 162 controls, 0 bad | real |
| E01 | claim 2, M12 | Cost of a synthetic run: 5 calls incl. a failed call and an unpriced model | exact to 1e-9; `wall_ms`, `host`, `vm_id`, counts | synthetic |
| E02 | claim 2, M12, M16 | Real run priced via `--prices`: each `llm.call` carries `price` and `cost_usd` = tokens × price; `query cost` sums; `wall_ms` 30,692; host attribution | exact to 1e-9 | real |
| E03 | claim 2 | Cost is read from the trace alone: a copy with doubled tokens in an otherwise empty dir doubles the answer | exact | real (mutated) |
| Q01 | clock | Guest clock +1 h: trace validates, projection identical, `llm.call.ts` stays the gateway's, tool events sit between their issuing call and the next, all inside the session | all true | real (mutated) |
| S01 | scale, M8 | Validator over 1,000,126 events | ≥ 50k ev/s, ≤ 512 MiB. `json.loads` alone runs ≈ 110k/s on these 533-B rows; loading the file whole costs 2.5 GB, so 512 MiB proves streaming. | synthetic (real mix) |
| S02 | scale, trust | Verifier over 1M | ≥ 40k ev/s (SHA-256 + canonical JSON measured at 140k/s), ≤ 512 MiB | synthetic |
| S03 | M9 | Cold per-principal audit over 1M (index build allowed) | ≤ 20 s and correct: about two sequential passes at `json.loads` speed. | synthetic |
| S04 | M9 | Warm audit over 1M | ≤ 1.0 s, ≤ 512 MiB, and ≤ 3× the warm time over 100k. A full scan takes ≥ 4 s here and grows 10× with the file; an answer partitioned by principal does neither. M9 has no bar in `metrics.md`; this is the bar that distinguishes "indexed by principal" from "grep". | synthetic |
| S05 | M8, scale | Completeness counters over 1M | stored == rows, no gaps, ≥ 40k ev/s, ≤ 512 MiB | synthetic |

The synthetic 1M file mimics the real ledger: 4 hosts × 3 writers, ≈ 73% `authz.decision` (mostly refund
probes denied), 10% `tool.call`+`tool.result`, 7% `llm.call` with real-sized token counts, 1–3 principals per
VM with 40% sub-agents, halt/resume pairs, labels. Median row 533 B.

## Selftest

`--selftest` runs 45 items with no solver: golden files regenerate byte-identically; `lint` (the suite's
independent rule checker) accepts `good.jsonl` and reports the expected `code@event_id` for each `bad_*`; the
chain oracle catches edit, deletion and re-hash; the chat oracle equals `expected_chat.json` and catches a dropped
tool message, a reorder, a usage change, an argument change, a duplicated call and a changed tool id; the real
run's harness usage sequence equals the gateway's 14 rows; the real ledger window satisfies the liveness, pairing
and lineage rules (so the real-data checks are satisfiable); the completeness, audit-sort, cost and big-fixture
oracles behave as claimed; the skew and poc2 generators do what the checks assume.
