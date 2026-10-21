# Avos

**An open-source agent cloud that learns.** Avos runs AI agents in isolated microVMs, gives every agent and
sub-agent its own identity and permissions, records what each one does, and retrains an open-weight model on the
runs that passed.

```
Deploy  →  Trace  →  Train  →  Serve
```

Every step is reproducible from this repository: scripts, configuration, pinned versions, seeds and dataset splits.

## Status

| Milestone | Scope | Status |
|---|---|---|
| 1. Compute | Firecracker microVMs with a guest agent, created and destroyed on demand | Done |
| 2. Harnesses | Ephemeral VM per job running τ²-bench and mini-swe-agent end to end | Done |
| 3. Identity | SPIFFE/SPIRE identity per VM, agent and sub-agent, with a birth/death ledger | Done |
| 4. Authorization | Per-principal policy at a host gateway; credentials never enter a VM | Done |
| 5. Supervisor | Stop, resume, kill or cut off any principal by name without disturbing siblings | Done |
| 6. Trace design | One event schema serving audit (who, when, with what authority) and retraining (inputs → outputs → result), validated on a real run | Done |
| 7–9. Trace and labels | Audit store, training store, labeling pipeline | Planned |
| 10–13. Evaluation | Benchmark runs, retraining, comparison with frontier models | Planned |

Results for each completed milestone are in `data/pocN/README.md`.

## Architecture

See [`docs/architecture.md`](docs/architecture.md). In short:

- **Compute:** one Firecracker microVM per job, under the jailer, configured through MMDS.
- **Identity:** one SPIRE Server per host; every principal gets a short-lived X.509 SVID such as
  `spiffe://avoslab.com/vm/vm-01-82ec92/agent/tau2/sub/search`.
- **Authorization:** a host gateway decides every model call, tool call and network connection with Cedar policies,
  identified by the caller's SVID over mTLS, and attaches upstream credentials only after a call is allowed.
- **Supervisor:** every principal runs in its own cgroup, nested like its identity; one control table on the host
  halts, resumes, kills or cuts off any branch by name, with a receipt, in milliseconds.
- **Trace:** one append-only event stream per host, every action stamped with the decision that allowed it, hash-chained
  per writer, partitioned by principal; two projections (audit timeline, chat-format training run).

## Requirements

- Linux host with `/dev/kvm` (Ubuntu 24.04), root access, Python 3.12, [`uv`](https://github.com/astral-sh/uv) and
  [`just`](https://github.com/casey/just).
- An OpenAI-compatible endpoint serving open-weight models (development uses Nebius Token Factory).
- Copy `.env.example` to `.env` and fill in the keys you use.

## Quick start

```sh
just push            # from your workstation: copy the repo and model config to the host
just spire           # on the host, once: SPIRE Server and identity registrar
just gateway         # on the host, once: authorization gateway (holds the model API key)
just image           # build the guest kernel and root filesystem
just poc1            # acceptance test: create, reach, destroy a microVM
just job tau2 retail/0
just poc3            # identity acceptance test; `just who` shows the principal tree
just poc4            # authorization acceptance test
just poc5            # supervisor acceptance test; `just ctl ls`, `just ctl halt <name>`
just poc6            # trace schema, validator, projections on a real run + acceptance suite
```

## Repository layout

| Path | Contents |
|---|---|
| `infra/` | microVM lifecycle (`vm`), jobs (`job`), guest image, guest agent and spawner shim |
| `identity/` | SPIRE Server configuration, installer, registrar |
| `gateway/` | authorization gateway and policies (milestone 4) |
| `supervisor/` | control table: halt, resume, kill, cut by name (milestone 5) |
| `trace/` | event schema, validator, verifier, projections and their acceptance suite (milestone 6) |
| `eval/` | benchmark split tooling and coding tasks |
| `data/` | dataset splits and per-milestone results |
| `docs/` | architecture, conventions, metrics, pinned versions |

## Documentation

- [`docs/architecture.md`](docs/architecture.md): components and request paths
- [`docs/trace.md`](docs/trace.md): the event catalog, lifecycle, two writers, projections, tamper evidence
- [`docs/conventions.md`](docs/conventions.md): benchmark, model policy, naming, environment
- [`docs/metrics.md`](docs/metrics.md): metrics M1–M17 and where each is measured
- [`docs/versions.md`](docs/versions.md): every pinned tool, image and model
- [`docs/split-notes.md`](docs/split-notes.md): how the held-out split was built

## License

[Apache License 2.0](LICENSE).
