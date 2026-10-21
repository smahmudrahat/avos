# Architecture

Avos runs every agent in its own Firecracker microVM, gives every agent and sub-agent its own cryptographic
identity, decides every outbound action against policy outside the agent's reach, and records the result as a trace
that can later train an open-weight model.

```
                         host
 ┌───────────────────────────────────────────────────────────────────────┐
 │  SPIRE Server ── registrar ── events.jsonl ── policy (Cedar)          │
 │        ▲            ▲              ▲              │                   │
 │        │            │              │       ┌──────▼──────┐            │
 │        │            │              └───────┤   gateway   ├──► model APIs, tools,
 │        │            │                      └──────▲──────┘     allowed hosts
 │  ┌─────┼────────────┼── microVM ──────────────────┼─────────────────┐ │
 │  │ SPIRE Agent   spawner shim        egress forwarder (root)        │ │
 │  │      ▲             │                     ▲                       │ │
 │  │      └── SVIDs ──  agent ── sub-agent ───┘ (only route out)      │ │
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
- The registrar keeps the birth and death ledger (`agent.spawn`, `agent.exit`, `vm.exit`) and deletes entries when a
  principal dies. `just who` shows the live tree.

## 3. Evaluation (`eval/`, `data/`)
User-disjoint τ²-bench retail split, its generator and checker, and coding tasks for the mini-swe-agent harness.
See `conventions.md` §1.

## 4. Authorization (`gateway/`), in progress

### 4.1 Model
Authorization follows a **workload credential broker** pattern: identity is attested at the edge, policy is decided
and credentials are attached at a broker the workload cannot reach into, and every decision is logged.

| Role | Component | Runs in |
|---|---|---|
| Trust provider: proves who the caller is | SPIRE attestation by uid (§2) | VM + host |
| Edge proxy: intercepts outbound calls, attaches the caller's identity | egress forwarder | VM, root |
| Policy decision point | Cedar policies evaluated by the gateway | host |
| Policy enforcement point + credential provider | gateway: forwards allowed calls, attaches upstream credentials | host |
| Audit | `authz.decision` rows in `events.jsonl` | host |

Two properties are deliberate:
- **Decisions are local.** The policy engine runs on the same host as the VMs. There is no remote control plane on
  the request path, so a decision costs microseconds and works offline.
- **Credentials never enter a VM.** Upstream secrets (model API keys, tool credentials) live only in the gateway and
  are attached to a request after it is allowed. An agent can use a credential only through a decision, cannot read
  it, and cannot take it with it.

### 4.2 Request path
1. An agent process (uid U) sends an ordinary HTTP request to the forwarder at `127.0.0.1:8090`. For model calls this
   is just `OPENAI_BASE_URL`, so harnesses need no changes.
2. The forwarder maps the TCP connection to uid U (`/proc/net/tcp`), and U to the principal's SPIFFE ID through the
   shim's table.
3. It opens mTLS to the gateway on the host with **that principal's own SVID**. The gateway learns the caller from
   the certificate, not from anything the agent sends.
4. The gateway evaluates `principal, action, resource, context` with Cedar. Actions are `llm.call`, `tool.call`, and
   `net.connect`; the context carries the arguments (for example a refund amount).
5. Allow: attach the upstream credential, forward, stream the answer back, write `llm.call` (with token counts) or
   `tool.result`. Deny: return HTTP 403 with the principal, policy ID and arguments.
6. Both outcomes write one `authz.decision` row: `principal`, `action`, `resource`, `args`, `decision`, `policy_ids`.

### 4.3 Containment
- Inside the VM, agent uids may connect only to the forwarder; all other outbound traffic from them is dropped, and
  MMDS is reachable only by root. A copied SVID is therefore useless to another process: it cannot reach the gateway
  directly, and it expires within 60 s.
- On the host, each tap may reach only the gateway, the SPIRE Server and the registrar. Default deny: a VM has no
  route to the internet except through the gateway's allowlist.
- The model API key is removed from VM metadata.

### 4.4 Policy
Policies are Cedar files in `gateway/policies/`, keyed by agent name (`.../agent/<name>`, `.../sub/<name>`), with
explicit policy IDs so every denial names the rule that caused it. Example: a support agent may issue refunds up to
$200, a refund-manager agent up to $1,000; everything not permitted is denied.

### 4.5 Acceptance test (`just poc4`)
1. The same tool call is allowed for one identity and denied for another.
2. Every denial is attributable: principal + policy ID + arguments (metric M5, target 100%).
3. A principal keeps the same identity and permissions for its whole life, and a sub-agent does not inherit
   permissions its policy does not grant.
4. A process using another principal's copied SVID is refused.
5. The test passes again on a fresh host after 30 minutes (rotation does not change outcomes).

### 4.6 Measured budget
| Item | Value | Effect |
|---|---|---|
| Cedar decision | 0.10 ms median, 0.15 ms p99 | negligible |
| Gateway hop | ~1–2 ms per call (to be confirmed) | 15–27 model calls per job → < 0.1% of job time (M13) |
| Forwarder memory | ~20–30 MB per VM (to be confirmed) | ~1–1.5% of a 2 GB VM (M17) |

Limits: if the hop exceeds 5 ms or the forwarder exceeds 50 MB, the design is revisited. τ²-bench executes its
tools inside the harness process, so its tool calls cannot be gated by any network component; they are covered by
the harness's own policy, and gateway-hosted tools are used for the acceptance test.

## 5. Planned
- **Supervisor:** stop, resume or cut off the network of any principal by name, without disturbing its siblings.
- **Trace:** one schema for audit ("what did principal X do") and for training ("which inputs led to which result").
- **Labeling and training:** automated checks, open-weight judges, human review; LoRA retraining on passed runs.
- **Evaluation:** full held-out runs with pass^k, cost and latency, compared with frontier-model baselines.
