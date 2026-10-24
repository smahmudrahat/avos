# Architecture

Avos runs every agent in its own Firecracker microVM, gives every agent and sub-agent its own cryptographic
identity, decides every outbound action against policy outside the agent's reach, and records the result as a trace
that can later train an open-weight model.

```
                         host
 ┌───────────────────────────────────────────────────────────────────────┐
 │  SPIRE Server ── registrar ── trace store ─── policy (Cedar)  ctl     │
 │        ▲            ▲              ▲              │        (control  │
 │        │            │              │       ┌──────▼──────┐  table)   │
 │        │            │              └───────┤   gateway   ├──► model APIs, tools,
 │        │            │                      └──────▲──────┘     allowed hosts
 │  ┌─────┼────────────┼── microVM ──────────────────┼────────┼────────┐ │
 │  │ SPIRE Agent   spawner shim = supervisor   egress forwarder (root) │ │
 │  │      ▲             │  (one cgroup per principal)  ▲               │ │
 │  │      └── SVIDs ──  agent ── sub-agent ────────────┘ (only route out)│ │
 │  └──────────────────────────────────────────────────────────────────┘ │
 └───────────────────────────────────────────────────────────────────────┘
```

## 1. Compute (`infra/`)
- `infra/vm`: create and destroy microVMs under the Firecracker jailer (chroot, uid/gid per VM, cgroup v2). Each VM
  gets a tap device (`172.16.N.1` host, `172.16.N.2` guest), an anti-spoofing rule, and its configuration through
  MMDS V2 rather than its disk.
- `infra/image/build.sh`: guest kernel and Ubuntu 24.04 root filesystem with the guest agent, the SPIRE Agent, and a
  shared read-only disk of pinned harnesses (τ²-bench, mini-swe-agent).
- `infra/job`: one ephemeral job = fresh VM → one trial → result copied to the host → VM destroyed, also on failure.

## 2. Identity (`identity/`)
- One SPIRE Server per host, trust domain `avoslab.com`. Each VM boots with a single-use join token bound to its
  per-boot name, `spiffe://avoslab.com/vm/vm-NN-xxxxxx`.
- Nothing is registered in advance. The **spawner shim** inside the VM gives each new principal a fresh Unix uid,
  asks the host **registrar** for a registration entry bound to that uid, and starts the process as that uid. The
  process obtains its X.509 SVID (60 s TTL, rotated by the SPIRE Agent) from the Workload API.
- Sub-agents are spawned through the shim's Unix socket; the caller is identified by `SO_PEERCRED`, so a process can
  only create children under its own identity.
- The registrar keeps the birth and death ledger (`agent.spawn`, `agent.exit`, `vm.exit`) and a per-host index of
  principals beside it, so spawns, exits and controls never scan the ledger. It deletes entries when a
  principal dies. `just who` shows the live tree.
- The shim calls the registrar over mTLS with the VM's own SVID, which only root in the VM can read, and the
  registrar checks it against the VM the connection comes from. An agent cannot register identities for itself.
  Top-level agents are started only from the host (`POST /spawn` on the guest's port 80 refuses any other caller).

## 3. Evaluation (`eval/`, `data/`)
User-disjoint τ²-bench retail split, its generator and checker, and coding tasks for the mini-swe-agent harness.
See `conventions.md` §1.

## 4. Authorization (`gateway/`)

### 4.1 Model
Authorization follows a **workload credential broker** pattern: identity is attested at the edge, policy is decided
and credentials are attached at a broker the workload cannot reach into, and every decision is logged.

| Role | Component | Runs in |
|---|---|---|
| Trust provider: proves who the caller is | SPIRE attestation by uid (§2) | VM + host |
| Edge proxy: intercepts outbound calls, attaches the caller's identity | egress forwarder | VM, root |
| Policy decision point | Cedar policies evaluated by the gateway | host |
| Policy enforcement point + credential provider | gateway: forwards allowed calls, attaches upstream credentials | host |
| Audit | `authz.decision` events in the trace store (§7) | host |

Two properties are deliberate:
- **Decisions are local.** The policy engine runs on the same host as the VMs. There is no remote control plane on
  the request path, so a decision costs microseconds and works offline.
- **Credentials never enter a VM.** Upstream secrets (model API keys, tool credentials) live only in the gateway and
  are attached to a request after it is allowed. An agent can use a credential only through a decision, cannot read
  it, and cannot take it with it.

### 4.2 Request path
1. An agent process (uid U) sends an ordinary HTTP request to the forwarder at `127.0.0.1:8090`. For model calls this
   is just `OPENAI_BASE_URL`, so harnesses need no changes.
2. The forwarder maps the TCP connection to uid U (`/proc/net/tcp`), and U to the principal through the shim's
   table. Root's connections act as the VM itself.
3. It opens mTLS to the gateway on the host with **that principal's own SVID** (re-fetched every 25 s), and checks the
   gateway's SVID. The gateway learns the caller from the certificate, not from anything the agent sends, and refuses
   a certificate from a different VM than the connection's source (`vm-binding`).
4. The gateway evaluates `principal, action, resource, context` with Cedar. Actions are `llm.call`, `tool.call`, and
   `net.connect`; the context carries the arguments (for example a refund amount).
5. Allow: attach the upstream credential, forward, return the answer, write `llm.call` (status, latency, token
   counts) or `tool.call` (arguments and result). Deny: return HTTP 403 with the principal, policy IDs and arguments.
6. Both outcomes write one `authz.decision` event: `principal`, `action`, `resource`, `args`, `decision`,
   `policy_ids`, and the forwarder's attempt number (§7). So does a request no policy can be asked about (no such
   tool, bad body: `malformed-request`), so every request the gateway receives has its decision.

### 4.3 Containment
- **No route out.** The host never forwards guest traffic. On the host, a tap may reach only the SPIRE Server
  (8081), the registrar (8082) and the gateway (8443). Guests have no DNS. The only way to the internet is a
  `CONNECT` through the gateway, which the default policy refuses for every host.
- **No secrets in the VM.** The model API key lives only in the host's `gateway.env`; `vm create` refuses an
  `agent.env` that contains a key. MMDS still holds the VM's join token, which is single-use and already spent.
- **Each SVID stays with its principal.** A principal's SVID and key sit in its own `/run/avos/<id>` (mode 700), so
  other principals cannot read them. A copy taken by root works only from the same VM, and only until it expires
  (60 s). Root inside a VM is trusted: it is the VM's own control plane.
- **Why not a per-uid firewall in the guest:** the guest kernel ships without the iptables `owner` match. Agents
  may therefore open connections to the gateway themselves, but only with their own identity, which is what the
  forwarder would present for them anyway.

### 4.4 Policy
Policies are Cedar files in `gateway/policies/`, keyed by agent name (`.../agent/<name>`, `.../sub/<name>`), with
explicit policy IDs so every denial names the rule that caused it. Example: a support agent may issue refunds up to
$200, a refund-manager agent up to $1,000; everything not permitted is denied.

### 4.5 Acceptance test (`just poc4`)
1. The same tool call is allowed for one identity and denied for another.
2. Every denial is attributable: principal + policy ID + arguments (metric M5, target 100%).
3. A principal keeps the same identity and permissions for its whole life, and a sub-agent does not inherit
   permissions its policy does not grant.
4. Another principal cannot read a principal's SVID; a copy used from another VM is refused; an expired copy is
   refused at the TLS handshake. An agent cannot register an identity or start a top-level agent.
5. The test passes again on a fresh host after 30 minutes (rotation does not change outcomes).

### 4.6 Measured budget
| Item | Value | Effect |
|---|---|---|
| Cedar decision | 0.10 ms median, 0.15 ms p99 | negligible |
| Tool call through forwarder and gateway (new connection, mTLS handshake, Cedar, log) | 15 ms median, 30 ms worst | model calls take 0.4–2 s; see `data/poc4/README.md` for the job-level effect (M13) |
| Guest agent (shim and forwarder in one process) | 25 MB RSS | ~1.2% of a 2 GB VM (M17) |

τ²-bench executes its
tools inside the harness process, so its tool calls cannot be gated by any network component; they are covered by
the harness's own policy, and gateway-hosted tools are used for the acceptance test.

## 5. Supervisor (`supervisor/`)

### 5.1 Model
Every principal is also a node in a **supervision tree**: the shim starts it in its own cgroup (v2), nested inside
its parent's cgroup exactly as its SPIFFE ID is nested inside its parent's. Stopping a node therefore stops its whole
branch, including anything it forked itself, and nothing else. The control table on the host (`supervisor/ctl.py`)
resolves a name to the VM, the cgroup and the SPIFFE prefix, and acts through three enforcement points:

| Verb | Agent or sub-agent | VM | Network |
|---|---|---|---|
| `halt` / `resume` | `cgroup.freeze` 1/0 on its cgroup, in the guest; the supervisor waits for `cgroup.events: frozen 1` | Firecracker `PATCH /vm Paused/Resumed` | the gateway refuses the branch while it is halted |
| `kill` | `cgroup.kill`; each death is ledgered as `agent.exit (signal 9)` and its SPIRE entry deleted | `infra/vm destroy` | |
| `cut` / `uncut` | | | the gateway refuses every request from the branch (`cut.json`, by SPIFFE prefix) |

Every command writes one `halt` or `resume` event to the trace store with a receipt (`hr_xxxx`), who issued it, the
target, the nodes under it and the time it took; `ctl.py ls` shows each live principal's state and receipt.

Three properties are deliberate:
- **Out of band.** The agent is never asked to stop. The freezer acts on the kernel's scheduler, the kill switch on
  the gateway; a process cannot ignore, delay or undo either. Controls come only from the host (`POST /halt` on the
  guest refuses any other caller; cgroup files are root-owned).
- **In-flight answers are dropped.** The gateway checks the kill switch before it forwards a request *and* before it
  returns a response. A model answer that arrives for a branch halted or cut mid-call is not delivered; the
  `llm.call` event records `dropped: true` and the receipt (a tool result held back: its `tool.result`).
- **No central hot path.** Names are hierarchical and resolved on the host that runs the VM; a stop costs one write
  per branch, whatever the size of the fleet.

Known limits (tech debt, details in `data/poc5/README.md`): an open `CONNECT` tunnel is not torn down by a halt or
cut, because only model calls and tool results are re-checked before delivery; and a halt does not free memory, since
the freezer only stops scheduling.

### 5.2 Acceptance test (`just poc5`)
1. Halt a sub-agent by name: it and its children freeze; the parent, siblings and the other VM keep ticking.
2. Resume: it continues with no gap in its sequence.
3. Ten halt/resume repetitions: M6 (command → `frozen 1`) median and worst ≤ 500 ms.
4. M7 = 0: no tick of a never-named principal lost, delayed or failed during any control.
5. Kill by name: `agent.exit (signal 9)`, entry gone, the rest unaffected.
6. Cut by name: its calls get 403 naming the receipt, it keeps running; an answer in flight at a halt is dropped.
7. Pause a VM by name; the other VM is unaffected; resume.
8. An agent cannot halt anything: `POST /halt` from inside is refused, writing a cgroup file is `Permission denied`.

## 6. Trace (`trace/`)
One event stream serves the auditor and the trainer. Every event carries a locally minted id, host time, the
principal it is about, the writer stream and its sequence number, and a hash chained to the writer's previous event;
every gated action names the `authz.decision` that allowed it (or the receipt that stopped it). Host-side rows are
the evidence; the harness transcript is a declared witness merged in for in-process tool calls and the outcome. Two
projections: a per-principal audit timeline with authority, and a chat-format training run with reward. Design,
lifecycle and limits: `docs/trace.md`; interface and acceptance suite: `trace/CONTRACT.md`, `trace/tests/`.

## 7. Collection (`collect/`)
Every host-side emitter writes contract events (§6) itself, at the moment it acts: the gateway, the registrar,
`ctl`, `infra/vm` and `infra/job`. Nothing in a VM writes to the store and no guest clock reaches `ts`. Interface and
acceptance suite: `collect/CONTRACT.md`, `collect/tests/`; results: `data/poc7/README.md`.

**Streams.** One stream per (host, component, VM): writer `<host>/<component>/<vm_id>`, file
`/var/lib/avos/trace/vm/<vm_id>/<component>.jsonl` (`trace/host/` for an event with no VM). An append mints the id,
`seq`, host `ts` and chain link under an exclusive `flock` on the file, so the processes that share a stream (the
registrar service and `vm-down`; every `ctl` run) keep one gap-free chain, and writes the whole line in one
`O_APPEND` write. A writer killed at any instant leaves whole lines; there is no `fsync` on the request path (the
kernel holds the line once `write` returns; a host crash can lose the last moments). The gateway stamps `authz_ref`
with the decision's own id at decision time; nothing is linked after the fact. A principal's events are in its VM's
four or five small files, so `avos trace --principal P` and `--run R` read only those: no index, no scan. The
registrar's per-host principal index (§2) and the hot paths are unchanged; `infra/job` finds its harness in the VM's
own registrar stream.

**Attempt numbering.** The in-VM forwarder reads the HTTP/1.1 requests it relays (keep-alive, `Content-Length` or
chunked bodies; after `CONNECT` the bytes are a tunnel, from the same single thread as before) and stamps each with
`X-Avos-Attempt: n`, counted per principal when the request head is complete, before the gateway is dialled. The
gateway copies `n` onto the decision and the call. A request lost between the VM and the host leaves its number
missing on the host (`attempt_gaps`), and the guest's count higher than the host's.

**Counters and teardown.** M8 compares the store with counts kept outside it: `trace/counters/<vm_id>` gets one byte
per act, appended just before the act's event is written (gateway: request received, model answer or tool call
forwarded; registrar: spawn, exit; `ctl`: halt, resume, counted before it acts). The guest's per-principal counts
reach the host three ways, each host-stamped: the shim sends a principal's count with its `/exit` (retried until
the registrar answers), the gateway sees the running number on every request, and `infra/vm destroy` reads the
forwarder's final table from `GET /health` **before** it kills the VM, which the registrar files on `vm.exit`. Kill
paths (`infra/job`'s EXIT trap, a bare `vm destroy`) go through the same destroy.

**M8 statuses** (`avos completeness --run R`): `complete` when every counter is known, equals what is stored, the
guest's count equals the stored decisions and no writer has a `seq` gap; `incomplete` when the counters are known
and something differs; `unverified` when a counter cannot be produced: the guest was unreachable at teardown, its
forwarder restarted (a `/run` marker, or attempt numbers starting over), or the run was imported. A request the
forwarder counted but the gateway never received therefore reads `incomplete`, never `complete`.

**Retention.** Everything is kept: files are only appended to, never rotated or deleted. Imported days (`avos
import`) are kept under their own writers; a derived index clusters their lines by VM so a principal's day is one
read, rebuilt whenever the imported files change.

**Tamper-proofing** is out of scope. It would go on the per-writer `head`s that `trace/bin/verify` prints: sign a
VM's heads at `vm.exit` and anchor them off the host, so a rewritten tail no longer verifies.

Known limits: an in-VM action that makes no request has no writer but the audited party; requests sent to the gateway
without the forwarder (an agent may dial it with its own SVID) carry no number and read as more decisions than guest
requests; and while a VM lives, a principal that has not exited counts as the highest number the host has seen.

## 8. Training store (`retrain/`)
Runs become training data without a second record. `retrain` builds one item per (run, principal) from the run's
own files on every call: the store's events by their own ids, the harness transcript as a witness, and the pinned
τ² prompt template and tool schemas. The gateway adds hashes of what each model call was shown and what it answered
to the `llm.call` it already writes (no text); a transcript message is accepted only where it hashes to what the
host saw, and the host's view of each call (the agent's prompt and tools, or neither) decides whether it was the
policy or the user simulator. Only the policy's own turns are trained. Outputs: `trajectories.jsonl` (per-message
`train` flag, `tools` column), `sft.jsonl` (prompt→completion per policy turn), a manifest of item hashes, splits and
M8 status that is byte-identical on re-export, diversity statistics, and a plain-text or HTML viewer for labelers.
Design: `docs/trace.md` §10; interface and acceptance suite: `retrain/CONTRACT.md`, `retrain/tests/`; results:
`data/poc8/README.md`.

## 9. Labels (`label/`)
A dataset is labelled in three levels without touching the record. `label` reads a POC 8 export: level 0 is the
reward and the structural checks, level 1 an open-weight judge asked a fixed rubric in two framings, level 2 the user,
blind, on a stratified gold set. Every judge answer and human verdict is a row appended to a per-labeller JSONL file
under `/var/lib/avos/labels`, keyed by the hash of the conversation that was judged; the recorded judge answer is the
label and is replayed, never re-asked. The labelled manifest, the dataset card and M10 (kappa of judge and reward
against the human, with an interval, or `unverified`) are derived from the export and those rows alone, byte-identical
on re-run. Design: `docs/trace.md` §11; interface and acceptance suite: `label/CONTRACT.md`, `label/tests/`; results:
`data/poc9/README.md`.

## 10. Fleet runner (`fleet/`)
One host runs many trials at a stated concurrency with no service: `fleet run` is a process that starts at most C
`infra/job run tau2 retail/<task>` children, each with its own `SLOT=vm-NN` from the fleet's `--slots` range
(slot = jail, tap, /30 network; disjoint ranges let claim 1 or a second runner share the host), and
exports the three roles as `MODEL`, `USER_MODEL`, `JUDGE_MODEL`. One trial is one VM is one run directory; trials are
never packed. `infra/job` claims the run id itself (lowest free `mkdir`); the runner never assigns or scans for ids
while jobs run, it reads the id back from the `job.json` that names its slot when the job exits, then snapshots
`avos completeness` into `<run>/completeness.json` and appends one line to `DIR/trials.jsonl`. Only tasks of the
split's chosen part can run (`--part train` by default; held-out only with `--part held_out`).

**Stop and resume.** SIGTERM/SIGINT: no new trial, SIGTERM to every job's process group (the job's EXIT trap destroys
its VM and writes `job.json` `aborted` with its stage), journal them `interrupted` (not part of the plan, re-run
later). Resume = the same command: it re-runs exactly what the journal lacks. The run ids that existed when the fleet
was created are kept in `DIR/preexisting.json`; any other run directory of the fleet's tasks with no journal line
(a SIGKILLed runner's orphans) is adopted as it is, or, without `job.json`, recorded `aborted`/`unknown` and re-run.
The runner holds a `flock` on `DIR/.lock` that its jobs inherit, so a resume cannot start while a killed runner's
jobs are still alive. With no coordinator, the same runner runs one instance per host over that host's share of
tasks.

**Matrix** (`fleet matrix`): computed outside τ² from DIR, the run directories and a prices file alone, recomputed on
every call (no cache), deterministic (sorted JSON). One cause per non-done trial from evidence, in the order gateway
(no-route, deny; a 5xx is infra only when the upstream itself answered ≥ 500 or was unreachable, while the
gateway's own 600 s timeout or an unattributed 5xx stays an agent failure in `n`; 4xx) > roles (`role_mismatch`: a 200 from a model outside the fleet's three roles, or none
from the policy; the only cause a `done` trial can carry) > witness (`witness_mismatch`: `result.json` is not exactly one simulation whose messages match the gateway's
calls) > `job.json` stage > `finish_reason: length` (`agent_length`) > τ² termination reason; infra causes leave pass^k's `n`, M2 keeps every
trial. Only evidence of the fleet's own trials counts (label rows of other runs are ignored and
warned). M12 per role from the gateway's `llm.call` tokens × listed prices (policy: GPU $/h × fleet wall, token price as
a ceiling; label judge from its own rows, outside the gateway); a missing part or price reads `unverified`. M17
(`fleet sweep`, `fleet m17`) uses host-side time: trial wall − Σ `llm.call.ms`.

**Routes.** `/var/lib/avos/gateway.routes.json` maps model name → upstream (a URL, or base URL plus the name of the
gateway.env variable that holds its key) or a list of them; with a list, a call from slot `vm-NN` goes to entry
`NN mod n`, so one trial's conversation stays on one policy server and slots split evenly; each `llm.call` names its
`upstream`. `gateway.py route [--table F] MODEL SLOT` prints the choice without touching the network. A model with no route gets one `authz.decision` deny
with `no-route` and a 403; nothing is forwarded. Every `llm.call` also carries `upstream_status` (null when nothing
answered), `error` (`gateway_timeout`, `upstream_unreachable: …`, `upstream_bad_body`) and the reply's `finish_reason`.
A trial is exactly one τ² simulation: the agent runs τ² with `--max-retries 0` (`docs/versions.md`). Without the file the gateway keeps POC 4's single upstream.
Interface and acceptance suite: `fleet/CONTRACT.md`, `fleet/tests/`; results: `data/poc10/README.md`.

## 11. Planned
- **Training:** LoRA retraining on the runs the labels keep.
- **Evaluation:** full held-out runs with pass^k, cost and latency, compared with frontier-model baselines.
