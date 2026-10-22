# POC 5 results

`just poc5` on the dev host (c8i.large, 2 vCPUs, nested virt), 2026-10-21, VMs `vm-01-c5b862` and `vm-02-caf1be`.
Design: `docs/architecture.md` §5. Every principal runs in its own cgroup, nested like its SPIFFE ID; the host
control table (`supervisor/ctl.py`) halts, resumes, kills or cuts off any branch by name and writes a receipt.

Tree under test, all ticking every 50 ms with a gateway call every 10th tick:
vm-01: `A → B → B1`, `support`, `D`, `G`, `H`, later `F`; vm-02: `E`, `J`. `G`, `H`, `J` are never named by a control.

| Done test | Result |
|---|---|
| 1. Halt `A/sub/B` by name | B and B1: `cgroup.events` `frozen 1`; B's tick counter stayed at 70 for 2 s. A, support, D (vm-01) and E (vm-02) kept ticking. Receipt `hr_…`, 2 nodes under it. |
| 2. Resume | `frozen 0`; B continued from tick 70 to 99 with no gap in its sequence. |
| 3. M6 over 10 halt/resume repetitions | **median 18 ms, worst 42 ms** end to end (command on the host → `frozen 1` in the guest), n = 13 halts including F and the VM pause. Inside the guest the freeze itself takes 0.1 ms; the rest is Python start-up, ledger lookup and one HTTP round trip. Budget: 500 ms. |
| 4. M7 | **0** over 3,453 ticks of the 3 never-named principals on vm-01 (`A`, `G`, `H`): 0 lost, 0 delayed during a control (±1 s window), 0 failed gateway calls. Two stalls were seen *outside* any control window (650 ms on A, 152 ms on H, 3–4 s from the nearest command): host scheduling jitter on a 2-vCPU host running two VMs, reported, not counted. |
| 5. Kill `D` by name | `agent.exit (signal 9)` in the ledger, SPIRE entry deleted, 23 ms; A still ticking. |
| 6. Cut `support` by name | Its refund calls flipped from 200 to `403 cut hr_d491`; it kept ticking (1013 → 1055); D's calls unchanged. `uncut` restored them. **In flight:** F was frozen 1.5 s into a 25 s model call; when the answer reached the gateway it was not delivered (`llm.call … status 200, 1492 tokens, dropped: true`); after resume F saw `403 halted by hr_d0d0`. |
| 7. Pause `vm-02` by name | E advanced 11 ticks in 2.2 s (≈ 44 expected): paused. A on vm-01 unaffected. Resume brought E back. 43 ms. |
| 8. An agent cannot use the controls | `POST /halt` from an agent: `top-level spawns and controls come from the host`. Writing another branch's `cgroup.freeze`: `Permission denied`. |

- **Superseded the same day:** the M6 and M7 numbers above were measured before the two fixes below. See
  "Baseline and two fixes".
- **Receipts:** every command left one `halt` or `resume` row with `receipt`, `by`, `target`, `nodes`, `ms`; the
  gateway's refusals name the same receipt, so M5 stays **891/891 = 100%**.
- **Cost:** no new process in the VM; the shim is the supervisor. One cgroup per principal (5 in vm-01 at the end,
  one per live top-level agent). M1 unchanged (2.7–3.6 s).

## Baseline and two fixes (2026-10-21, later the same day)

`just poc5-baseline 10 120` runs the same tree with the same 50 ms ticks for 120 s and **no control at all**, ten
times, to measure what happens anyway. It found two real problems that the M7 window had been treating as jitter.

| | Before | After |
|---|---|---|
| Baseline, no controls: failed sibling gateway calls | **8 / 20,881** (each a 10 s hang, then a timeout) | **0 / 21,005** |
| Baseline: worst stall of any tick | 10,076 ms | 726 ms |
| `just poc5` × 5: M6 (command → `frozen 1`) | median 140–163 ms, worst 317 ms (38,000-row ledger) | **median 2–3 ms, worst 6 ms** (88,000-row ledger) |
| `just poc5` × 5: M7 | 2 in one run (a failed call and a 10.5 s stall, 0.6 s after a thaw) | **0** in all five |

1. **A request lost between the forwarder and the gateway.** A packet capture showed the TLS handshake completing
   and then no request: the agent's bytes never left. `pipe()` read and wrote the same TLS socket from two threads,
   and OpenSSL objects are not thread-safe; a TLS 1.3 session ticket arriving while the request was written lost
   it, about once in 2,600 calls, more often with more agents per VM. Every model call in every job took this path.
   **Fix:** `pipe()` in the forwarder and the gateway now copies both directions from one thread with a selector
   (checked locally on 3,020 TLS 1.3 connections with 300 B and 2 MiB bodies, 0 wrong replies).
2. **M6 grew with the host's history.** Every control, spawn, exit and VM boot re-read the whole ledger to find one
   principal: 147 ms at 38,000 rows, linear in rows. **Fix:** the registrar keeps a per-host index (one file per
   principal, a directory of markers per live VM; `registrar.py reindex` rebuilds it from the ledger). The ledger
   stays the record; only reports read it. M6 no longer depends on how much the host has done.

**M7 definition, made precise:** a failed sibling call is a lost event whenever it happens. A late sibling tick
(gap > tick + 100 ms) counts when it falls within 1 s of a halt or resume. The window for late ticks is justified
by the baseline: late ticks occur at about 0.04 % of ticks with no control at all (host scheduling on a 2-vCPU
nested-virtualization host, worst 726 ms), and 0 of 8,773 in-window ticks were late across the five runs. An earlier
version of `ctl.py stats` also windowed failed calls; that was reverted, and under the precise definition the
pre-fix run scores M7 = 2.

Not yet done: the double-fork escape test, a measured network-cut latency, ten repetitions of the cut, and the run
with 32 VMs that claim 1 requires (POC 10).

## Regression with the supervisor in place
- `just poc1`: passes twice.
- `just poc3 10`: all checks pass; M3 11/11 = 100%; M4 median 916 ms (1235 ms in POC 4).
- `just poc4`: all checks pass; M5 6/6 = 100%; gateway round trip median 12 ms, worst 27 ms.

## Found on the way
- A frozen agent's model call keeps running upstream. Without the response-side check the answer would have been
  handed to the agent the moment it was resumed, so the kill switch is consulted twice per call.
- Python's `Popen(user=…)` drops privileges before `preexec_fn`, so a process cannot join a root-owned cgroup from
  there; the shim enters the cgroup as root in a one-line `sh` wrapper and then `setpriv`s to the principal's uid.

## Tech debt

| Item | Effect today | Fix | When |
|---|---|---|---|
| **In-flight drop does not cover CONNECT tunnels.** The gateway re-checks the kill switch before returning a model answer (`llm()`) or a demo-tool result (`/tools/*`). A `CONNECT` tunnel opened before a halt or cut stays open; bytes keep flowing until either side closes it. | A halted or cut branch can still receive data on a tunnel it opened earlier. The default policy allows no host, so no tunnel exists today; done test 6 covers model calls and tools only. | Keep open tunnels per SPIFFE ID in the gateway; on a `cut.json` change, close the ones whose principal is now covered (`shutdown` both sockets) and log `net.connect … closed_by: hr_xxxx`. Add a test: open a tunnel, cut, check the tunnel dies. | Before the first `net.connect` permit is added to the policy. |
| **Halting does not free memory.** `cgroup.freeze` stops scheduling; the frozen branch keeps all its RAM, and a paused VM keeps its full guest memory. | A halted agent costs as much memory as a running one, so halts do not raise density (M17) or lower infra cost per trial (M16). | Per branch: `memory.reclaim` on the frozen cgroup (swap or zram in the guest), or checkpoint to disk and kill (CRIU). Per VM: balloon device or Firecracker snapshot plus a restore. Measure memory freed and resume latency (must stay well within the M6 budget). | When M17 is measured (POC 10 sweep), or earlier if halted agents are expected to stay halted for long. |
