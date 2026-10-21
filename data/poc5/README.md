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

- **M7 definition note (2026-10-21, later the same day):** `ctl.py stats` now applies the same ±1 s control window
  to failed sibling gateway calls that it applies to late ticks, and reports both kinds outside a window as jitter.
  A re-run after that change: M6 median 28 ms, worst 49 ms; M7 0 over 2,571 ticks, 2 stalls outside any window.
  `nodes_under` now lists a VM target in its own receipt (the earlier VM-pause receipts omitted it).
- **Receipts:** every command left one `halt` or `resume` row with `receipt`, `by`, `target`, `nodes`, `ms`; the
  gateway's refusals name the same receipt, so M5 stays **891/891 = 100%**.
- **Cost:** no new process in the VM; the shim is the supervisor. One cgroup per principal (5 in vm-01 at the end,
  one per live top-level agent). M1 unchanged (2.7–3.6 s).

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
