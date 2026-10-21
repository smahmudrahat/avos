# POC 3 results

`just poc3 20` on the dev host (c8i.large, nested virt), 2026-10-19, VM `vm-01-82ec92`, trust domain `avoslab.com`. One SPIRE Server per host, one SPIRE Agent per microVM (join token, `unix` workload attestor), the spawner shim in the guest agent, ledger rows in `/var/lib/avos/events.jsonl`.

| Done test | Result |
|---|---|
| 1. A started; `who` shows `spiffe://avoslab.com/vm/vm-01-82ec92/agent/A`, created_at, VM | yes |
| 2. A spawns B through the shim; `who` shows B under A | yes; B's ledger row exists before B's process starts |
| 3. A and B log lines carry their own SPIFFE ID | yes (`journalctl -u avos-agent`) |
| 4. B exits; `who` shows exited_at and `exit 0`; A live | yes |
| 5. Process started outside the shim asks the Workload API | `PermissionDenied: no identity issued` |
| 6. B's SVID copied to another file | verifies while fresh; `certificate has expired` after the 60 s TTL |

- **M3 identity_coverage:** 21/21 = 100% (B and the 20 bench sub-agents; A and `bench` were still live, so not counted).
- **M4 identity_issue_ms** (shim starts process → SVID written by the Workload API, n = 21): median 1172, worst 1595, best 714. Before setting the server's `cache_reload_interval` to 500 ms (default 5 s): ~4.5–5 s.
- M1 with SPIRE on: 2.7–3.5 s (was 2.6 s).
- Teardown: 0 VMs, taps, iptables rules, registration entries, attested agents left.

## Follow-up 2026-10-19: identities for real runs

- **The VM holds a credential.** At boot the registrar adds an entry `spiffe://avoslab.com/vm/<vm id>` (parent: that same ID, the SPIRE agent's alias) for uid 20000. The guest fetches it into `/run/avos/vm` (mode 700). Issued in 560 ms. Root and agent uids get nothing for it.
- **The harness is a principal.** τ² and mini-swe-agent start through the shim: `.../vm/<vm id>/agent/tau2` (or `/agent/mini-swe-agent`), own uid, owning `/var/avos`. The shim fetches every principal's SVID as that uid, in parallel with the process, so M4 is measured the same way for harnesses and demo agents.
- **Job records name identities.** `job.json`, `session.open` and the new `session.close` carry `vm_id`, `vm_spiffe_id` and `agent_spiffe_id`. `vm` is only the reused slot. `vm.boot.ts` is now the create time, so a VM is always born before its principals.
- `just poc2 10`: M2 10/10, M13 median 39.3 s / worst 71.4 s, reward 3/10. Harness principals: M3 10/10, M4 median 451 ms / worst 1379 ms. `just poc3 20` still passes 6/6 (M4 median 899 ms over 21 sub-agents). 0 VMs, taps, entries left.
- One mini-swe-agent run (`coding/slugify`) hit its 30-step limit with tests failing: its `sed` patterns had unescaped `[^a-z0-9]`, so every edit was a silent no-op. Model behaviour, not permissions (0 permission errors; the files were owned by the harness uid).
