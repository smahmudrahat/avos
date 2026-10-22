# Claim 1 (control) — independent acceptance suite

Black-box tester for claim 1:

> "In Avos, any single sandbox, or its network, can be stopped, resumed or cut off by name without disrupting
> the others. M6: stop or network cut takes effect in ≤ 500 ms. M7: 0 sibling events lost or delayed. Both hold
> on 10 repetitions."

This suite reads no implementation. It drives the system only through documented interfaces — `infra/vm`,
`POST /spawn`, the shim's `/run/avos/spawn.sock` (`op=spawn`), `supervisor/ctl.py`, the gateway as seen from
inside a guest (`http://127.0.0.1:8090`), and `cgroup.events` read from inside the guest. Three files:

- `claim1.py` — host orchestrator. Run from `~/poc` as `sudo python3 supervisor/tests/claim1.py [--only 1,3] [--quick]`.
- `workload.py` — guest principal: ticks, gateway calls, child spawns via the shim, and the two escape modes.
- `watch.py` — guest freeze watcher: `poll(POLLPRI)` on `cgroup.events`, prints each frozen-edge with a guest wall time.

Stdlib only. `--quick` = 3 repetitions and smaller volumes. One line per check:
`PASS|FAIL <id> <what> measured=<v> bar=<v>`. Exit 0 only if every check passes. Raw measurements in `/tmp/claim1/`.
VMs and any appended ledger rows are removed in `finally`, even on failure.

## How the hard measurements are made honestly

- **M6 independent of `ctl`.** The driver records a host wall time *just before* it launches `ctl.py` (so the
  measurement includes `ctl`'s own process start-up and name resolution, which is part of "stop command →"), and
  takes the freeze time from `watch.py`'s `poll(POLLPRI)` edge on the target's own `cgroup.events` — the kernel's
  own signal, not anything `ctl` prints. Guest wall time is converted to the host clock with a per-VM offset
  measured by 11 NTP-style round trips (observed jitter < 0.5 ms over a persistent SSH channel, so the offset
  adds negligible error to a 500 ms bar).
- **Freeze proven twice.** Beyond the `cgroup.events frozen 1` edge, the target's own tick counter must make zero
  progress inside every halt window (a counter that stops advancing), and a double-forked `setsid` daemon's
  counter file must stop too. Two independent signals.
- **Cut latency** = host time of the `ctl cut` command → the first target gateway call that comes back `403`
  (the target calls continuously; before the cut its calls are `200`). Uncut is the mirror (first `200` back).

## Checks

| # | Claim / metric | What it proves | Bar and why |
|---|---|---|---|
| 1 | halt/resume by name; M6 | Halt a sub-agent that has its own child: M6 per rep; the whole branch freezes (child `cgroup.events frozen 1` **and** the target's tick counter stops inside every halt window); parent + two siblings keep ticking; resume continues with a contiguous tick sequence (nothing lost). | M6 worst ≤ **500 ms** (metrics.md M6); every other sub-signal exactly 0/true. 500 ms is the pre-registered bar; freeze/progress/sequence are binary. |
| 2 | branch containment on halt & kill | A double-forked `setsid` daemon (reparented to init) and a rapid short-lived forker. Halt freezes both (counter + forker line-count stop advancing); a never-named sibling keeps ticking; `kill` removes **both branch cgroups entirely** (cgroup.kill reaps the reparented daemon) and the daemon counter is frozen for good. | Containment is binary: `halt_froze = reps`, both cgroups gone, 0 progress after kill. Cgroup removal (not `pgrep`) is the proof — a reparented daemon still lives in its origin cgroup, so `cgroup.kill` is what matters. |
| 3 | network cut/uncut by name; M6 | Target calls `issue_refund` continuously. Cut latency (command → first refused call) and uncut latency (command → first allowed call), N reps; the target keeps ticking during the cut (not frozen); a sibling's every call is answered (200 or its normal 403 policy answer — 403 is still an answer). | cut & uncut worst ≤ **500 ms** (M6 covers "network cut takes effect"); `kept_running = reps`; `sibling_unanswered = 0`. |
| 4 | in-flight answer dropped | A slow model call (`max_tokens=1500`, ≈ 9 s) is put in flight; the caller is halted (then, separately, cut) mid-call. After release the caller must **not** receive the answer. Two independent witnesses, both required: the caller's own log (its in-flight call returns anything but `200`) **and** the store, read through the documented query `avos trace --principal P` (collect/CONTRACT.md §1), whose `llm.call` event for that principal must say `dropped: true`. The control is **held until that event is stored** (bounded at 240 s — the upstream model has been seen to take ~40 s per 1500 tokens); releasing earlier would make a later delivery legitimate, which is exactly the false "FAIL" seen during development. A missing event, `dropped: false`, or a call that was not actually in flight at the control is a FAIL, never a timeout-pass. | `delivered_to_caller = 0` **and** `store_not_dropped_or_missing = 0`. Architecture §5.1: "a model answer that arrives for a branch halted or cut mid-call is not delivered; the llm.call row records dropped: true." |
| 5 | M7 under load | 4 (quick: 2) never-named siblings per VM across both VMs tick every 50 ms and call the gateway. A control-free baseline period is run first, then an equal period during which a *separate* victim in each VM is halted/resumed/cut/uncut repeatedly. M7 = every failed sibling call (any time) + every sibling tick late by > 150 ms within 1 s of a control (metrics.md M7, verbatim). Baseline failed-calls and >150 ms gaps are reported alongside so the cost of control is visible. | **M7 = 0** (metrics.md). Delay bound = 50 ms tick + 100 ms (metrics.md's "more than 100 ms"). |
| 6 | M6 scale independence | The live store is append-only (collect/CONTRACT.md §2): synthetic history can neither be put there nor taken back. The contract's documented path for a large history is a scratch root (`AVOS_TRACE_ROOT=<dir>`, §1/§5), and `ctl.py` honours that variable (probed: a halt issued with it set lands in the scratch store, not the live one). So both M6 series run with `ctl`'s trace root pointed at the scratch root — first empty, then after `avos import` of 200 000 (quick: 20 000) well-formed `authz.decision` events for a dead VM id, built with `trace/bin/ingest --ledger` (the Justfile's documented raw-rows → trace-file path). The check also asserts `imported == N` and that `ctl`'s own halt/resume rows are answered from the scratch root (`avos trace --principal`), i.e. the large history really was on the control path. The scratch root is deleted afterwards; live-store `events` before/after are recorded. | after ≤ **500 ms** and growth ≤ **20 ms**. 20 ms ≈ one SSH round trip of noise; name resolution uses the registrar index, so a correct implementation shows no growth. A store scan on the control path would blow this bar. Limitation: this exercises the history the control path *writes into*; the registrar's live index itself is not made large by any documented path, and the suite says so rather than pretending. |
| 7 | gateway reliability (no control) | ≥ 20 000 (quick: 3 200) `issue_refund` calls from 8 concurrent agents across both VMs, no control at all. Count calls with no answer; report latency p50/p99/max. | `unanswered = 0`; latencies reported (not barred — context for M7). A control claim is worthless if the gateway drops calls on its own. |
| 8 | agents cannot control | A spawned agent (its own uid) tries: `POST /halt` on the guest, writing another branch's and its own `cgroup.freeze`, shim `op=halt`/`op=kill`, and reading a victim's private SVID key. Every attempt must be refused; the victim keeps ticking. | `escapes_succeeded = 0`. Architecture §5.1 / §2: controls come only from the host; cgroup files are root-owned; per-principal dirs are mode 700. |

## Notes / resolved ambiguities

- "Sibling gets its normal policy answer": a sibling whose policy denies it gets `403`. The task says a `403` is
  still an answer; only *no* answer (timeout, connection error) is an M7 failure. The suite counts only non-`{200,403}`
  statuses as failed calls.
- A sub-agent named `support` does **not** inherit the top-level `support` policy (policies are keyed to the exact
  SPIFFE path), so check 3 uses a top-level `support` as the allowed-caller target.
- `ctl kill` leftover check uses cgroup removal, not `pgrep` — `pgrep -f daemon.cnt` matches the driver's own SSH
  command line (which contains that string), a false positive seen during development.
- POC 7 moved events from `/var/lib/avos/events.jsonl` (now frozen) to the append-only store behind
  `collect/bin/avos`. The suite never reads the store's files; it uses only `avos trace --principal`, `avos store`,
  `avos import` and `AVOS_TRACE_ROOT` (collect/CONTRACT.md). Checks 1–3, 5, 7, 8 never touched the ledger.
- Clock offset host−guest is ≈ 323 ms on the dev host but extremely stable; measured per VM and cancelled in all
  before/after comparisons.
