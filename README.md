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
| 4. Authorization | Per-principal policy at a host gateway; credentials never enter a VM | In progress |
| 5. Supervisor | Stop or cut off any principal by name without disturbing siblings | Planned |
| 6–9. Trace and labels | Trace schema, audit store, training store, labeling pipeline | Planned |
| 10–13. Evaluation | Benchmark runs, retraining, comparison with frontier models | Planned |

Results for each completed milestone are in `data/pocN/README.md`.

## Architecture

See [`docs/architecture.md`](docs/architecture.md). In short:

- **Compute:** one Firecracker microVM per job, under the jailer, configured through MMDS.
- **Identity:** one SPIRE Server per host; every principal gets a short-lived X.509 SVID such as
  `spiffe://avoslab.com/vm/vm-01-82ec92/agent/tau2/sub/search`.
- **Authorization:** a host gateway decides every model call, tool call and network connection with Cedar policies,
  identified by the caller's SVID over mTLS, and attaches upstream credentials only after a call is allowed.

## Requirements

- Linux host with `/dev/kvm` (Ubuntu 24.04), root access, Python 3.12, [`uv`](https://github.com/astral-sh/uv) and
  [`just`](https://github.com/casey/just).
- An OpenAI-compatible endpoint serving open-weight models (development uses Nebius Token Factory).
- Copy `.env.example` to `.env` and fill in the keys you use.

## Quick start

```sh
just push            # from your workstation: copy the repo and model config to the host
just spire           # on the host, once: SPIRE Server and identity registrar
just image           # build the guest kernel and root filesystem
just poc1            # acceptance test: create, reach, destroy a microVM
just job tau2 retail/0
just poc3            # identity acceptance test; `just who` shows the principal tree
```

## Repository layout

| Path | Contents |
|---|---|
| `infra/` | microVM lifecycle (`vm`), jobs (`job`), guest image, guest agent and spawner shim |
| `identity/` | SPIRE Server configuration, installer, registrar |
| `gateway/` | authorization gateway and policies (milestone 4) |
| `eval/` | benchmark split tooling and coding tasks |
| `data/` | dataset splits and per-milestone results |
| `docs/` | architecture, conventions, metrics, pinned versions |

## Documentation

- [`docs/architecture.md`](docs/architecture.md): components and request paths
- [`docs/conventions.md`](docs/conventions.md): benchmark, model policy, naming, environment
- [`docs/metrics.md`](docs/metrics.md): metrics M1–M17 and where each is measured
- [`docs/versions.md`](docs/versions.md): every pinned tool, image and model
- [`docs/split-notes.md`](docs/split-notes.md): how the held-out split was built

## License

[Apache License 2.0](LICENSE).
