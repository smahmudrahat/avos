# POC 7 (collect) — independent acceptance suite

```
sudo python3 collect/tests/run.py              # on the host, from ~/poc: everything (≈ 30 min incl. claim 1 and poc4)
sudo python3 collect/tests/run.py --quick      # 100k-event day, 300 latency calls, claim1/trace suites in --quick
sudo python3 collect/tests/run.py --only D5    # prefix filter on check ids
python3 collect/tests/run.py --selftest        # anywhere: proves the checkers on hand-written fixtures (53 items)
```

One line per check: `PASS|FAIL <id> <what> measured=<v> bar=<v>`. Exit 0 only if every executed check passed. Raw
data in `/tmp/collect-tests/` (per-stage JSON, claim1/poc4/trace-suite logs, `*.error.txt` tracebacks). A missing
interface is `FAIL <id> not implemented: collect/bin/avos`. VMs, iptables rules, killed services, the scratch store
and ssh masters are cleaned up in `finally`.

Black box: the suite reads no implementation. It drives `infra/vm`, `POST /spawn`, the shim socket (children),
`supervisor/ctl.py`, `infra/job`, `systemctl`, `iptables`, and the contract surface `collect/bin/avos`
(`collect/CONTRACT.md`). Ground truth never comes from the store: it is the agents' own JSONL logs
(`guest_agent.py`: every HTTP attempt with its status and body, phase markers, exit), the spawn responses, the
receipts `ctl` prints, the τ² transcript, and a generator whose expected answers are computed from the generated
file. POC 6's `trace/bin/validate` and `verify` are used as black boxes on the store's files.

## The tree (checks D1–D4, O1, O3)

vm-01: top-level `support` spawns `B1`, `B2`, `B3` through the shim. `support` makes 10 allowed refunds (100 ¢),
one model call, one refused refund (48,000 ¢). `B1`/`B3`: three refused refunds and a model call. `B2`: model call,
8 s idle (the host halts it by SPIFFE name, holds 1.5 s, resumes), the refusal (48,000 ¢, `default-deny`), a model
call, exit. vm-02: a decoy `support` that waits 45 s then makes 1,000 back-to-back refunds (the latency sample and
the cross-VM isolation witness). Everything exits on its own; then the VMs are destroyed and the services restarted.

## Checks

| id | claim / metric / done test | what must hold | bar and why |
|---|---|---|---|
| D1_tree | done 1 | The tree really did the work: all five principals exited, B2 reached its idle phase, both `ctl` commands returned receipts, `support` had ≥ 1 allowed refund, B2 ≥ 2 model-call attempts and ≥ 1 refusal, ≥ 1 model call reached upstream, 0 unanswered calls. | All facts true. Without this the later equalities could be vacuous (an idle tree has a trivially complete trace). 0 unanswered is the claim 1 reliability bar: a clean run must be clean. |
| D2_b2_timeline | done 2 | `avos trace --principal <B2>` == B2's own log, shape for shape and in order: spawn; for every call a decision (action, amount/model, allow/deny as the agent saw it) and, when allowed, the call event whose `authz_ref` is that decision; halt and resume with the receipts `ctl` printed, `target` = B2, B2 ∈ `nodes`, `authority.by` set; exit. Every deny carries `policy_ids`, equal to the ids in the 403 body the agent saw. Every line has the POC 6 envelope and an `authority`. Nothing with another principal. | 0 problems. This is the statement's done test verbatim; the policy id and receipt are what make the timeline an audit answer rather than a log. |
| D2_siblings | done 2 ("nothing from its siblings") | The same exactness for `support`, `B1`, `B3`; the five answers (incl. the decoy) share no event id; the decoy's events all carry vm-02's id. | 0 problems. Isolation is only proven if the siblings' answers are also right and pairwise disjoint. |
| D2_run | contract §3 | `avos trace --run <vm_id>` is sorted, contains every event of the four principals, the `vm.boot`, and nothing with another `vm_id`. | 0 problems. A run must be reconstructible as one unit (M8 is per run). |
| D3_counts | done 3 | Per principal: stored `authz.decision` = attempts, `tool.call` = allowed tool answers, `llm.call` = model answers, 1 spawn, 1 exit, B2 1 halt 1 resume. Per run: `completeness.stored` equals the sum over the four agents plus the VM principal's own rows, spawns 4, exits 4, halt/resume 1. | All equal. The gateway counted N tool calls, the store has exactly N (verbatim done 3), with N observed from the agents' answers, not from any host counter. |
| D3_m8_clean | M8 | The M8 record of the clean run: `status: complete`, `ratio` exactly `1.0`, every `happened` counter present, `stored == happened == observed`, no gaps; record shape and `ratio` formula (Σ min ÷ (Σ max + gaps), guest counter included) per contract §4. | Exactly 100 %. metrics.md M8 is stored ÷ happened; a clean run that reads anything else means the counters are wrong. |
| D3_tau2 | done 1 ("a real task"), M8 | One real τ² job (`infra/job run tau2 retail/<train[0]>`): `--run run_id` and `--run vm_id` byte-identical and sorted; `session.open/close`, `agent.spawn`, decisions, model calls present; stored `llm.call` of `agent/tau2` == messages with `usage` in `result.json` (`simulations[0]`; POC 6 showed 14 == 14, agent + user-simulator turns); M8 complete. | 0 problems. The transcript is the only witness that owes nothing to the host. |
| D4_destroy | done 4 | After `infra/vm destroy`: B2's answer byte-identical; `vm.exit` present in `--run`; M8 still complete. | Identical. "Re-run the query. Same answer (the flush worked)". |
| D4_restart | done 4, contract §2 | After `systemctl restart` of gateway, registrar and every service `avos store` lists: B2's answer byte-identical; services active; every store file's earlier bytes are a prefix of its current bytes; no listed file vanished; a declared retention horizon is ≥ 30 days. | Identical, append-only. An audit store that loses or rewrites rows across a restart is not a record. |
| D5_gateway, D5_registrar, D5_kill_<svc>… | done 5 | `systemctl kill -s KILL` of the gateway, the registrar, and each unit the contract lists, while two callers hammer the gateway every 20 ms (2 s timeout) and two short-lived agents are spawned during the outage. Afterwards `lost` = Σ over principals of (attempts − stored decisions)⁺ + (allowed answers − stored calls)⁺ + (observed exits − stored exits)⁺ + missing spawn. | If `lost > 0`: `status ≠ complete` and `ratio ≠ 1.0`; the record must be self-consistent (contract §4). `lost = 0` is also a pass (buffered). Silent loss fails. |
| D5_path | done 5, known gap | 4 s `iptables DROP` of guest→gateway :8443 on the host while the callers run. The agents' timeouts fail (required: ≥ 1 unanswered, else the injection did nothing). | Same rule. This is the loss class host counters cannot see; the guest counter in M8 (`guest_requests`) must surface it or the run must read `unverified`. |
| D5_guest | done 5 | `systemctl kill -s KILL avos-agent` inside the guest mid-run (forwarder and shim die, agents with them). | Same rule: whatever the host could not establish must not read `complete`. |
| S1_store_valid | POC 6 contract | The live store's files, concatenated in listed order, pass `trace/bin/validate` and `trace/bin/verify` (exit 0). | Both 0. Every stored event is a POC 6 event: ids, per-writer `seq`, the pinned hash chain, authority references, liveness. |
| M9_import | M9, contract §1 | One generated day (1,000,000 events; real mix: ≈ 65 % decisions, tool/llm calls, lifecycle, halts incl. parent halts covering sub-agents; ≈ 5,800 VM lives plus one agent owning 3 % of the day) imports into a scratch root (`AVOS_TRACE_ROOT`); `imported` == lines; the first query (index build allowed) is correct; the scratch store validates and verifies. | First query ≤ 20 s (POC 6 cold bar), rc 0. |
| M9_small, M9_median, M9_largest | M9 | `avos trace --principal P --day D` cold (caches dropped, fresh process) and warm, answer == the audit oracle computed from the generated file (ids in order, authority). Small ≈ 3 events, median ≈ 50, largest ≈ 25,000. | Small/median: cold ≤ 3 s, warm ≤ 1 s. Largest: cold ≤ 6 s, warm ≤ 2 s. M9 has no bar in metrics.md; POC 6 set warm ≤ 1 s at 1 M. A full scan of a 550 MB day from disk takes ≥ 5 s on this host and grows with the day, so the cold bar separates "partitioned by principal" from "grep"; the largest answer is ≈ 15 MB of NDJSON, which Python alone emits in ≈ 0.5 s, hence 2×. |
| M8_imported | M8, contract §4 | `avos completeness --run <imported vm>` reads `unverified`, `ratio: null`, counters `null`. | Unverified. A run with no live counters must never read complete; computing "happened" from the store would pass every other check and fail this one. |
| I1_two_roots | contract §1 ("answers come from the root's current content") | Day A in scratch root R, day B (another UTC day, another host, same fixed principal names `probe` and the big `support`) in root S; six queries alternating R, S, R, S, R, S must each equal their own root's oracle, hash for hash. | 6/6 correct, or an explicit non-zero exit with a message. A cache keyed on anything but the content of the root being queried answers with another root's rows; found as a silent `0 of 14, exit 0` in the POC 6 tooling. |
| I2_same_path_same_mtime | contract §1 | Root R imports day A, warm queries; R is removed and re-created at the same path with day A′ = A with every `order_id` digit rotated and the chain recomputed (identical byte size, identical ids, different hashes); the store files get A's mtimes back. Queries for `probe` and `support` must return A′'s rows. | New content (hash-checked) or explicit error; empty or A's rows fail. This is exactly the size+mtime trap `docs/trace.md` §9 names for the POC 6 side index. |
| I3_append_invalidates | contract §1, §2 | After warm queries on root R (day A), day B is imported into R. Queries without `--day` must return A ∪ B in `(ts, writer, seq)` order; `--day 2026-09-01` / `--day 2026-09-02` must split them. | import rc 0 and all four correct, or explicit error. An audit store grows after it was first read; a warm index that never notices the append answers yesterday's question. |
| O1_latency_single | claim 2 | 1,000 back-to-back refunds from one agent (vm-02, otherwise idle host) with collection on: p50, p99. | p50 ≤ 20 ms, p99 ≤ 60 ms. POC 4 recorded median 15 ms, worst 30 ms (new connection each). +5 ms at p50 is the cost of one synchronous flush per event on gp3; a design that pays that on the request path is what the bar excludes. p99 at 2× the recorded worst absorbs 2-vCPU scheduling noise. |
| O2_latency_concurrent | claim 2 | p50/p99 from the claim 1 suite's 20,000-call check (8 agents, 2 VMs). | p50 ≤ 55 ms, p99 ≤ 130 ms (recorded 41 / 84; +1/3, +50 %). Run-to-run noise of that check is ≈ 10–20 %; a per-event flush doubles it. |
| O3_guest_rss | claim 2 (M17) | RSS of the guest agent (shim + forwarder) during the tree. | ≤ 30 MB (recorded 25 MB; +5 MB = 0.25 % of a 2 GB VM). Guest-side collection must not eat density. |
| R1_claim1 | claim 1 | `just claim1` 8/8, check 7 ≥ 20,000 calls, 0 unanswered (p50/p99 reported). | Unchanged bars. |
| R2_poc4 | M5 | `just poc4` exit 0, no `FAIL`. | Unchanged. |
| R3_trace_suite | POC 6 | `python3 trace/tests/run.py` all pass. | Unchanged. |

## How the hard measurements are made honestly

- **Ground truth before the store.** Each principal logs every HTTP attempt (`t0`, `t1`, status or exception, first
  400 bytes of the body). "Happened" for a principal is therefore: decisions = attempts, calls = answers other than
  403, exits = its own `exit` record. A tool answer that is not 200/403 came from the path, not from the gateway,
  and counts as unanswered. Nothing is derived from the ledger, the store, or any host counter.
- **Receipts** come from `ctl`'s own stdout (regex `hr_xxxx`); if `ctl` ever prints none, any well-formed receipt is
  accepted for the shape but the authority/event receipt must still agree.
- **Loss** is only what the agents saw and the store lacks; an attempt that the forwarder answered with a timeout
  while the gateway still decided it is not counted against the store (stored decisions may exceed answered calls).
- **M9 oracle**: the expected answer is recomputed from the generated file in one streaming pass
  (`lib.audit_oracle_stream`) and cross-checked in `--selftest` against POC 6's `trace/bin/query audit`.
- **Cold** = `sync; echo 3 > /proc/sys/vm/drop_caches` immediately before a fresh `avos` process.
- **Stale answers are visible.** The oracle compares `event_id`s in order, `authority`, and each row's `hash`; two
  days generated with the same fixed principal names, or the digit-rotated twin of a day, differ in every hash while
  keeping ids, sizes and (for the twin) byte length identical, so a cached answer cannot pass by accident.

## Selftest (53 items, no host)

The B2 oracle accepts a hand-written correct answer (`fixtures/b2_query.jsonl` vs `fixtures/b2_log.jsonl`) and
rejects 13 single mutations: a sibling's event, a missing refusal, a missing halt, a wrong receipt, an authority
receipt that disagrees, a wrong `authz_ref`, a deny without policy ids, policy ids other than the agent saw, a
reorder, a duplicate id, a missing authority, an extra decision, a halt not naming the principal. The M8 checker
is run on 12 records (`fixtures/m8_cases.json`): clean-complete passes; clean-incomplete, silent loss (complete
with known loss), unverified-with-ratio, complete-with-null, guest counter higher, stored ≠ observed, missing
`guest_requests`, complete-with-gap, and an incomplete record whose `ratio` is not the contract §4 formula all
fail; honest incomplete and honest unverified pass. The τ² witness counter reads `simulations[0].messages` of the
POC 6 fixture as 14. Also: `lost_events`
arithmetic, the 403-body policy parser, the claim1 line parsers, the append-only checker (append ok, rewrite and
truncation caught), and the generator: a 20k day is one UTC day, has the real mix, picks small < median < largest,
includes a parent halt in a sub-agent's oracle, and passes POC 6 `validate` and `verify`. Stale-index oracle: the digit-rotated twin has the same byte size, the same ids,
different hashes, and validates and verifies; the POC 6 reference answer over the original day is rejected as an answer
for the twin; two days on two hosts concatenate into one valid store and the oracle over both splits by `--day`; a
row with a foreign hash is rejected.

## Ambiguities resolved

- **"Run"** for a demo tree (no τ² job): a VM life. Both `run_*` and `vm_id` resolve to one VM (contract §3).
- **Heartbeat calls of the VM principal** are part of the run's totals; the per-run observed count adds the VM
  principal's own rows (read from the store, but agent losses are checked per agent, so this cannot hide one).
- **A 403 is an answer** (claim 1 suite rule); the refusal is the done test's event, not a loss.
- **Sub-agents cannot refund** (policies key on the exact SPIFFE path), so B2's refusal is its 48,000 ¢ call and
  the allowed tool work is the parent's; B2's model calls are its allowed events.
- **M8 under a buffered design** that loses nothing is a pass; the contract only forbids reading complete when
  the agents saw more than the store has, and forbids "complete" whenever a counter is missing.
- **Scale data is imported, not generated through the system** (1 M real calls would take hours on 2 vCPUs); the
  contract's `import` is a documented path and the imported day is queried through the same command.
