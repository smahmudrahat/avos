# POC 7 results

`just poc7` (`collect/tests/run.py`, written independently from the targets before the solution) on the dev host
(c8i.large, 2 vCPUs, nested virt), 2026-10-22. Design: `docs/architecture.md` §7. Interface: `collect/CONTRACT.md`.

Built: the emitters (gateway, registrar, `ctl`, `infra/vm`, `infra/job`) write POC 6 contract events themselves into
one stream per (host, component, VM) under `/var/lib/avos/trace` (`collect/store.py`, 180 lines); the in-VM forwarder
numbers every request per principal (`X-Avos-Attempt`) and keeps the counts; `infra/vm destroy` reads them before the
kill; `collect/bin/avos` answers `trace`, `completeness`, `store`, `import` (300 lines). Standard library only. No new
service on the host or in the VM. `events.jsonl` is no longer written; it stays as POC 1–6 history and the reports
(`registrar.py who/stats`, `ctl.py stats`, `gateway.py stats`) read it together with the store.

| Done test | Result |
|---|---|
| 1. A real tree does real work | vm-01: `support` spawns `B1`, `B2`, `B3` through the shim; 10 allowed refunds, refusals (48,000 ¢, `default-deny`), model calls; `B2` halted by SPIFFE name for 1.5 s and resumed; vm-02: a decoy `support` with 1,000 calls. 1,023 calls, **0 unanswered**. A real τ² job (`run_retail-0_12`) through `infra/job`. |
| 2. `avos trace --principal B2` = B2's own log | spawn; each call's decision (action, amount or model, allow/deny as B2 saw it) and, when allowed, the call naming that decision in `authz_ref`; the deny with `policy_ids: ["default-deny"]` = the 403 body; halt and resume with the receipts `ctl` printed, `authority.by` set; exit. 0 problems; same exactness for `support`, `B1`, `B3`; the five answers share no event; `--run <vm_id>` is the VM's whole life and nothing of vm-02. |
| 3. Stored = what happened | Per principal and per run, stored decisions / tool calls / model calls / spawns / exits / halt / resume equal what the agents observed (run: 28 decisions, 10 model calls, 10 tool calls, 4 spawns, 4 exits, 1 halt, 1 resume). M8 on the clean run: **complete, ratio 1.0**, every live counter equal to the store. τ² run: 11 stored model calls = 11 messages with usage in `result.json`, M8 complete (the suite's check reads the witness at the wrong level; see disputes). |
| 4. Same answer after destroy and restart | After `infra/vm destroy`: B2's answer byte-identical, `vm.exit` present, M8 still complete. After restarting the gateway and the registrar: byte-identical; every store file's earlier bytes are a prefix of its current bytes. |
| 5. Loss is never silent | SIGKILL of the gateway mid-run (two agents calling every 20 ms): the agents lost **44** calls; the gateway's counter equals the store (522 = 522); the guest's count is **566**, with **44 attempt gaps**, exactly the lost calls → `incomplete`, ratio 0.947. SIGKILL of the registrar: no event lost (the two spawns tried during the outage were refused at the shim, nothing half-done) → complete. 4 s `DROP` of guest → gateway: the agents' 2 s timeouts fired, every request still reached the gateway after the drop (SYN retry) → complete, nothing lost. SIGKILL of the guest agent: its counters died with it → `unverified`. By hand: registrar stopped for 8 s across an agent's exit; the shim kept retrying, the exit landed 1 s after the restart as `exit 0` with its attempt count, M8 complete. |

Acceptance suite, final run on the host after the disputes and the index fix below: **28/28** (987 s);
selftest 53/53. The first full run was 24/25 (`D3_tau2`, a test bug; see disputes).

## Measured

| Item | Measured | Bar |
|---|---|---|
| M8, clean tree run / τ² run | complete, 1.0 / complete, 1.0 | exactly 1.0 |
| M8, gateway killed / registrar killed / path cut / guest agent killed | incomplete 0.947 (44 = 44 lost) / complete (0 lost) / complete (0 lost) / unverified | never complete when anything was lost |
| M9, 996,163 imported events, one UTC day: import | 10 s (incl. the index: VM-clustered copy, 256 buckets) | — |
| M9 first query after import | 0.04 s | ≤ 20 s |
| M9 small (3 events) cold / warm | 0.16 / 0.03 s | ≤ 3 / ≤ 1 s |
| M9 median (49 events) cold / warm | 0.17 / 0.03 s | ≤ 3 / ≤ 1 s |
| M9 largest (24,855 events, 3 % of the day) cold / warm | 0.58 / 0.41 s | ≤ 6 / ≤ 2 s |
| Collected principals (live store): `trace --principal` | 0.03–0.05 s, reads its VM's 4–5 files | — |
| Gateway tool call, one agent, 1,000 back-to-back (O1) | p50 5 ms, p99 9 ms | p50 ≤ 20, p99 ≤ 60 ms |
| Gateway, 8 agents on 2 VMs, 20,000 calls (claim 1 check 7, O2) | p50 42 ms, p99 88 ms, 0 unanswered (claim 1 recorded 41 / 84) | p50 ≤ 55, p99 ≤ 130 ms |
| Guest agent RSS (shim + forwarder) during the tree | 25 MB | ≤ 30 MB (POC 4: 25 MB) |
| Cost of one decision on the request path (counter byte + id, chain, one `O_APPEND` write under `flock`) | 53 µs p50, 71 µs p99 | — |
| Stored event size (live store) | 624 B mean | — |
| M6 halt by name (claim 1 check 1) | median 63 ms, worst 68 ms; 62 ms before and after 200,000 appended legacy rows | ≤ 500 ms (POC 5 ≈ 60 ms) |

Regressions, final run on the host: `just claim1` **8/8** (M6 median 62 ms; cut median 59 ms; 20,000 calls, 0
unanswered, p99 87 ms); `just poc4` exit 0, M5 6/6 = 100 %; POC 6 suite **65/65** full (1 M scale checks pass) and
65/65 `--quick` twice in a row without clearing `/tmp`.

## Contract disputes (resolved by the tester, 2026-10-22)

- **`D3_tau2` read the τ² witness at the wrong level** (`result.json["messages"]`; the run is under
  `["simulations"][0]`). Accepted as a test bug and fixed; the check now passes (10 = 10 on the final run).
- **The §4 ratio left out the guest's request count**, so a loss only the guest saw would read `incomplete` with
  ratio 1.0. Ruling: `ratio = Σ_k min(sources_k) ÷ (Σ_k max(sources_k) + |gaps|)`, written into §4; the suite now
  recomputes the ratio and rejects any other value.
- **Found by the final run, not by the suite: a stale index answered an audit query with nothing.** POC 6's
  `query audit` returned 0 of 14 events (exit 0) on the host because a side index left from an earlier run matched
  a different file's size and mtime. Fixed in both indexes (see tech debt); both testers added adversarial checks
  (POC 6 X01–X05, POC 7 I1–I3: same size and mtime, in-place edits, swapped and corrupted caches). All pass.

## Tech debt

| Item | Effect today | Fix | When |
|---|---|---|---|
| **No fsync.** Lines are durable against a killed writer, not against a host crash. | The last moments before a power loss can vanish (and the chain shows the tail missing only against an anchored head). | A 1 s group fsync thread per process, outside the request path. | Before traces leave the dev host. |
| **A control writes its receipt after it acts.** `ctl` counts the command before acting (so a `ctl` killed between the act and the event reads incomplete), but the `halt` event itself carries the measured `ms` and the guest's `nodes`, so it is written after. | A crash in that window leaves an M8 gap rather than a receipt. | Write the receipt first and the measurement as a second event (needs a kind, or `ms` optional in the POC 6 schema). | If M8 gaps from `ctl` are ever seen. |
| **A live VM's guest count is provisional.** Until the VM is destroyed, a principal that has not exited counts as the highest attempt number the host saw. | A request lost at the very end of a still-running principal's activity shows only at teardown. | The host reads `/health` periodically (one more moving part), or asks it at query time. | If audits of live VMs need final M8. |
| **Requests that bypass the forwarder** (an agent may dial the gateway with its own SVID) carry no attempt number. | They read as more decisions than guest requests: `incomplete`, never silent. | A per-uid egress rule in the guest (the kernel lacks `owner` match; needs a kernel rebuild). | With the guest kernel work. |
| ~~**Derived indexes trusted size (+ mtime).**~~ **Fixed 2026-10-22.** The final host run found POC 6's audit index answering 0 of 14 events from a stale index of a different file at the same path (same size and mtime); `avos`'s import index had the same shape of check (sizes only). | Both now key validity on the file's identity and content (device, inode, size, mtime, SHA-256 of first and last 64 KB), checked on every use, and verify what they read: `query audit` that each fetched line is an event of the principal asked for, `avos` that each indexed range is fully readable and every line is an event of the VM asked for. Any mismatch rebuilds once; a second mismatch fails the query (exit 1/2), never an answer. | Done. |
| **The imported-day index is rebuilt whole** when any imported file changes. | 10 s per million imported events at each import. | Index per import segment. | When imports become frequent. |
| ~~**`claim1` check 4 waits on the retired ledger.**~~ **Fixed 2026-10-22** by the claim 1 tester: checks 4 and 6 now read through `avos trace` / `avos import`, and missing corroboration fails. | — | — | Done. |
| **The training projection re-chains.** `ingest --run-dir` on a stored run rebuilds events to merge the witness's content. | The derived file's ids differ from the store's. | Carry content as its own event kind referencing the stored `llm.call`. | POC 8. |

## Does not prove

- **Nothing about actions that make no request.** An agent that only computes, or writes files in its VM, has no
  writer but itself; the trace sees what crosses the forwarder, the shim and the host.
- **Not tamper-proof.** Root on the host can rewrite a stream and its chain; the per-writer heads are not signed or
  anchored anywhere yet.
- **Not crash-durable.** Kill-safe (whole lines, chained), not fsync-safe.
- **Not at fleet scale.** One host, two VMs at a time; M9 at 1 M events is an imported synthetic day with the real
  kind mix, not a million real calls; collected per-VM streams were measured only at tens of events per VM.
- **The `D5_path` run did not lose a request.** The 4 s drop delayed requests past the agents' timeouts, but every
  one reached the gateway on SYN retry; the "lost before the host" class was exercised by the gateway kill (requests
  refused at connect), where the guest counter caught all 44.
- **Not every forwarder failure.** A guest whose counter is reset or unreadable reads `unverified`; that says nothing
  about what was lost there.
