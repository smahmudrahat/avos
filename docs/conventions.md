# Conventions

The fixed choices every component is checked against. A change to anything here needs a dated reason in the same commit.

## 1. Benchmark
- τ²-bench **retail**, 114 tasks, pinned at commit `96a3ef9` (see `versions.md`).
- Split: tasks grouped by the user they act on; whole users go to train or held-out with seed **300**.
  Result: `data/splits/retail_user_disjoint_v1.json` (65 train / 44 held-out after 5 exclusions; see `split-notes.md`).
- Trials: 8 per train task, 4 per held-out task (pass^4).
- User simulator: one pinned open-weight model, temperature 0, the same for every arm; larger than the policy model
  and from a different family.
- Scaffold: τ²'s default `llm_agent` for every arm. Reported as a Custom leaderboard entry:
  `τ²-bench retail · held-out tasks · pass^4`.

## 2. Model policy
- Policy model: an 8–9B open-weight model.
- Development model: `Qwen/Qwen3-30B-A3B-Instruct-2507` on Nebius Token Factory (OpenAI-compatible). It is not the
  policy model; swapping it is configuration only.
- Natural-language assertion judge: τ² hard-codes `gpt-4.1-2025-04-14`. The image build patches τ² to read the
  judge from `TAU2_NL_JUDGE`, set to the open-weight `deepseek-ai/DeepSeek-V4-Flash-0731`. 40 of the 114 retail
  tasks carry such assertions, so scores on them are not directly comparable with the public leaderboard. Every
  Avos arm, closed-model baselines included, uses the same judge.
- Anything whose output becomes training data is open-weight. Closed models are used only as evaluation baselines
  and never enter the training store.

## 3. Naming
- A **principal** is anything that holds an identity: a microVM, an agent, a sub-agent.
- VM slot `vm-NN` (network and jail slot, reused). VM identity `vm-NN-xxxxxx` (slot + 6 hex, minted per boot, never
  reused). Run `run_<task>_<trial>`. Principal display ID `ag_xxxx`.
- SPIFFE trust domain `avoslab.com`. Paths:
  `spiffe://avoslab.com/vm/<vm-id>` for a VM, `/vm/<vm-id>/agent/<name>` for an agent,
  `.../agent/<name>/sub/<name>` for a sub-agent (nested to any depth).
- Event kinds (one JSON object per line in `events.jsonl`):
  `vm.boot`, `vm.exit`, `session.open`, `session.close`, `agent.spawn`, `agent.exit`, `llm.call`, `tool.call`,
  `tool.result`, `fs.write`, `net.connect`, `authz.decision`, `halt`, `resume`, `label`.
  `agent.spawn` / `agent.exit` rows carrying `spiffe_id` form the identity ledger.

## 4. Repository
- One directory per component: `infra/` (microVMs), `identity/`, `gateway/`, `supervisor/`, `trace/`, `eval/`,
  `data/`, `docs/`; later `train/` as it is built.
- The `Justfile` has one target per milestone acceptance test (`just poc1`, `just poc2`, ...).
- Dataset splits live in `data/splits/`; each `data/pocN/` holds that milestone's recorded results.

## 5. Environment
- Linux host with `/dev/kvm`, Ubuntu 24.04. The development host is an AWS c8i.large with nested virtualization.
- Each microVM: 1 vCPU, 2 GB memory; no snapshots or ballooning.
- Python 3.12 and `uv`. Every external tool, image and model is pinned in `versions.md` when first used.
- `.env.example` names every secret and fills none.
- Training: at least one GPU with ≥ 24 GB for bf16 LoRA on a 9B model.
