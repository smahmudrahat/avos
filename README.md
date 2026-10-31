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
| 7. Collection | Every principal's events written append-only on the host as they happen; any identity's timeline answered from its VM's streams; loss before the host made visible (M8) | Done |
| 8. Training store | Every run as per-principal training data and a labeler's view, derived from the trace with the store's own ids; transcript content accepted only where it matches hashes the gateway took of what the model saw | Done |
| 9. Labels | Every run labelled by reward and structural checks, an open-weight judge in two framings and a blind gold set; judge answers recorded once and replayed; label quality (M10) on the dataset card | Done (gold set labelled by an Opus 5.5 proxy for a human labeller) |
| 10. Fleet dry run | 32 concurrent microVMs on one host running the benchmark; the control claim re-tested under that load | Done |
| 11. Baseline | Held-out and train runs of the open policy model (Qwen3.5-9B), cost and density measured | Done |
| 12. Retraining | LoRA on the model's own passed runs; held-out re-run against the baseline | Done |
| 13. Frontier comparison | Claude Opus 5.5 through the same gateway, scaffold, simulator and judge; cost and score ratios | Done |
| 14. Simulator check | Claude simulators tested for whether the benchmark can separate models | Done |

Results for each milestone are in `data/pocN/README.md`.

## Results

| Measure | Result | Source |
|---|---|---|
| Stop one agent by name, 32 to 34 microVMs running | 40 ms median, 43 ms worst; 0 sibling actions lost; 20,000 gateway calls, 0 unanswered | [`data/poc10`](data/poc10/README.md) |
| Identity per agent and sub-agent | 21/21 (100%), median 1.2 s to issue; 60 s credentials bound to their VM | [`data/poc3`](data/poc3/README.md) |
| Denials attributable to an agent and a policy | 100% | [`data/poc4`](data/poc4/README.md) |
| Held-out runs recorded in full | 176/176; one agent's actions found in 0.03 to 0.04 s over 390,886 events | [`data/poc11`](data/poc11/README.md) |
| Retrained Qwen3.5-9B vs Claude Opus 5.5, τ²-bench retail held-out (44 tasks) | pass^1 0.477 vs 0.492, pass^4 0.188 vs 0.197; statistical tie | [`data/poc13`](data/poc13/README.md) |
| Cost per task, retrained 9B vs Opus | $0.0056 vs $0.102: 18.2x cheaper [17.0, 19.4] | [`data/poc13`](data/poc13/README.md) |
| Infrastructure cost per task, nested-virt AWS host | $0.00412: 19% below AWS AgentCore, above Cloud Run and one t3.small per job; bare-metal hosts not yet measured | [`data/poc11`](data/poc11/README.md) |

A plain-language summary for non-technical readers is in the [work report](https://impartial-filament-9e5.notion.site/Avos-the-cloud-built-for-AI-agents-405c7fa4698281119182cb0142d8fe20). A replay of a recorded session runs at [avoslab.com](https://www.avoslab.com); the site is generated from this repository (`site/`).

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
just poc5-baseline   # the same agents with no control at all: what M7 is measured against
just claim1          # claim 1 acceptance suite, written independently (≈ 9 min)
just poc6            # trace schema, validator, projections on a real run + acceptance suite
just poc8            # training store: export, viewer, statistics on the host's runs + acceptance suite
just poc9            # labels on the dry-run set: level 0, card, gold set (`just poc9 0,1` asks the judge)
just poc10           # fleet run at concurrency 32 on the benchmark, with the matrix of results
just fleet --help    # fleet runner: run, matrix, compare, m15 (retrained vs frontier)
just train-test      # retraining acceptance suite (`--host` on the GPU host)
just eval-test       # frontier-comparison acceptance suite (`--host`, `--live`)
```

## Repository layout

| Path | Contents |
|---|---|
| `infra/` | microVM lifecycle (`vm`), jobs (`job`), guest image, guest agent and spawner shim |
| `identity/` | SPIRE Server configuration, installer, registrar |
| `gateway/` | authorization gateway and policies (milestone 4) |
| `supervisor/` | control table: halt, resume, kill, cut by name (milestone 5) |
| `trace/` | event schema, validator, verifier, projections and their acceptance suite (milestone 6) |
| `collect/` | the host trace store: emitters' stream writer, `avos` audit CLI, acceptance suite (milestone 7) |
| `retrain/` | runs as training data and a labeler's view: `retrain` CLI, pinned τ² metadata, acceptance suite (milestone 8) |
| `label/` | labels in three levels, blind view, gold set, M10: `label` CLI, judge rubric, acceptance suite (milestone 9) |
| `fleet/` | fleet runner: concurrent benchmark runs, results matrix, comparisons, M15 (milestones 10 to 13) |
| `train/` | retraining: select passed runs, LoRA fit, merge, serve (milestone 12) |
| `eval/` | benchmark split tooling, coding tasks, frontier-comparison acceptance suite (milestone 13) |
| `site/` | the website, generated from the results in `data/` (`python3 site/build.py`) |
| `data/` | dataset splits and per-milestone results |
| `demo/` | the demo command: readable output from the real tools and saved results ([`docs/demo.md`](docs/demo.md)) |
| `docs/` | architecture, conventions, metrics, pinned versions |

## Documentation

- [`docs/architecture.md`](docs/architecture.md): components and request paths
- [`docs/trace.md`](docs/trace.md): the event catalog, lifecycle, two writers, projections, tamper evidence, training store, labels
- [`docs/conventions.md`](docs/conventions.md): benchmark, model policy, naming, environment
- [`docs/metrics.md`](docs/metrics.md): metrics M1 to M17 and where each is measured
- [`docs/versions.md`](docs/versions.md): every pinned tool, image and model
- [`docs/split-notes.md`](docs/split-notes.md): how the held-out split was built
- [`docs/demo.md`](docs/demo.md): the demo commands in order, what each does behind the scenes, and how to test it
- [`docs/reproduce.md`](docs/reproduce.md): every model, judge, setting, data format and command needed to recreate the results

## License

[Apache License 2.0](LICENSE).
