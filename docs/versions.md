# Pinned versions

Every tool, image and model the POCs depend on, recorded when first used (`conventions.md` §5). Change a row only with a date and a reason.

## Host

| Item | Version | Since | Notes |
|---|---|---|---|
| Instance | AWS c8i.large, `NestedVirtualization=enabled`, us-east-2 | POC 1, 2026-10-18 | Dev host, not the benchmark host. |
| OS | Ubuntu 24.04.5 LTS, kernel `7.0.0-1014-aws` | POC 1 | |
| Firecracker | v1.17.0, sha256 `99ad0f5cd0514a88aad0e9ae8cfdb3cc3b4ab9d190e1194602406c786b5de7a5` | POC 1 | |
| Jailer | v1.17.0, sha256 `65ef226e96f0ceda55ba643f445801ef2cc0ea667ef67cad8ac4f406c9c8434f` | POC 2 prereq, 2026-10-19 | Per-VM chroot, uid/gid 10000+N, cgroup v2. |
| uv | 0.13.1 | POC 2, 2026-10-19 | Installs τ²-bench on the host. |
| SPIRE | v1.15.4 (`spire-1.15.4-linux-amd64-musl.tar.gz`, sha256 `d8e3ee9a9d48636249484d91af37566dfc0a9d7e71edcbe7acd49cf18bc3c87c`) | POC 3, 2026-10-19 | Server on the host (`identity/install.sh`); the same tarball's `spire-agent` is baked into the guest rootfs. Server `cache_reload_interval` 500 ms, agent `sync_interval` 1 s. |
| cedarpy (Cedar policy engine) | 4.12.2, in `/opt/avos/gateway` (uv venv, Python 3.12) | POC 4, 2026-10-21 | Gateway policy decisions (`gateway/install.sh`). |

## Guest image

| Item | Version | Since | Notes |
|---|---|---|---|
| Kernel | Firecracker CI `vmlinux-6.1.155` (v1.15 CI set), sha256 `e20e46d0c36c55c0d1014eb20576171b3f3d922260d9f792017aeff53af3d4f2` | POC 1 | v1.17 CI artifacts not published yet. |
| Rootfs | Firecracker CI `ubuntu-24.04.squashfs` (Ubuntu 24.04.3, v1.15 CI set), sha256 `68321e0482baeb3844dafe8a6b08a6902401a7afc41fbfd8c3d9ea08aadd244f` | POC 1 | Python 3.12 inside. |
| Base URL for both | `https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.15/x86_64` | POC 1 | See `infra/image/build.sh`. |

## Benchmark

| Item | Version | Since | Notes |
|---|---|---|---|
| τ²-bench | `sierra-research/tau2-bench` commit `96a3ef92329baf908ffe9dfe188cea34e8701d2a` (2026-10-15, package 1.0.1) | POC 0 split, first run POC 2 | The split in `data/splits/retail_user_disjoint_v1.json` was built from this commit. |
| τ²-bench install | `uv sync --frozen` (its `uv.lock`; LiteLLM 1.81.11) + `uv pip install websockets==17.2` | POC 2, 2026-10-19 | Upstream bug at this commit: core `tau2` imports the voice module, which needs `websockets`. The full `voice` extra is not needed. |
| τ²-bench patch | `src/tau2/config.py`: `DEFAULT_LLM_NL_ASSERTIONS` read from env `TAU2_NL_JUDGE` (default unchanged) | POC 2, 2026-10-19 | Applied in `infra/image/build.sh`; build fails if the upstream line changes. `conventions.md` §2. |
| τ²-bench patch (NL judge) | `src/tau2/config.py`: `DEFAULT_LLM_NL_ASSERTIONS_ARGS` adds `"max_tokens": 4096`; `src/tau2/evaluator/evaluator_nl_assertions.py`: the judge call is asked once more when its answer is not JSON | POC 10, 2026-10-23 | Applied in `infra/image/build.sh`; build fails if either upstream text changes. In the POC 10 runs 2 of 444 judge calls (both task 67, at concurrency 32) ran to the provider's 8,192-token default (`finish_reason: length`) and τ² filed the trial `infrastructure_error`; the other 442 used ≤ 2,099 tokens, and 44 replays of the two prompts all stopped by 560. Only the measurement is repeated, never the conversation; both calls are `llm.call` rows. Two bad answers in a row still end the trial as before. |
| τ²-bench invocation | `tau2 run ... --num-trials 1 --max-concurrency 1 --max-retries 0` (`infra/agent/agent.py`) | POC 10, 2026-10-23 | At this commit `run_with_retry` (`src/tau2/runner/progress.py`) re-runs a whole simulation up to `--max-retries` (default 3) when an exception escapes and keeps only the successful attempt, so one trial could hold two conversations (`run_retail-60_13`). With 0 a trial is exactly one simulation; an attempt that errors is saved as that trial's `infrastructure_error` result. The local runner's work queue uses `max_attempts=1` (no unit requeue). `fleet matrix` keeps `witness_mismatch` as the backstop. |
| mini-swe-agent | `mini-swe-agent==2.4.6` + `litellm==1.81.11`, full freeze in the image at `/opt/harness/mini-swe-agent/freeze.txt` | POC 2, 2026-10-19 | LiteLLM pinned to τ²'s version: unpinned install pulled 1.106.0, which fails to import (`No module named 'mcp'`). Only top-level packages are pinned; the freeze records the rest. |
| Coding task | `eval/coding/slugify` (TASK.md, test.sh) | POC 2, 2026-10-19 | Done test 2. Tests restored from the clean copy before grading. |
| Verified fork (reference only) | `amazon-agi/tau2-bench-verified` commit `864350a8971a8f8ee9e7b8472e2edc380a806b0c` | POC 0 | Used to choose exclusions; see `split-notes.md`. |

## Models

| Role | Model | Endpoint | Since | Notes |
|---|---|---|---|---|
| Dev model (POC 1–9 plumbing) | `Qwen/Qwen3-30B-A3B-Instruct-2507` | Nebius Token Factory, `https://api.tokenfactory.nebius.com/v1` | 2026-10-19 | Not the policy model (8–9B, chosen in POC 10). `conventions.md` §2. |
| NL-assertion judge | `deepseek-ai/DeepSeek-V4-Flash-0731` | Nebius Token Factory | 2026-10-19 | Bare-JSON verdicts correct in 2/2 checks, 2–3 s. `zai-org/GLM-5.3` timed out (>120 s) 2/2. |
| Label judge (POC 9 level 1) | `openai/gpt-oss-120b` | same endpoint (`OPENAI_BASE_URL` of the gateway's env file) | 2026-10-22 | Open weights, Apache-2.0, not the policy's family. Temperature 0, seed 300, `max_tokens` 4000 (reasoning counts against it: valid JSON 60/60 in the probe), JSON object; answers recorded once and replayed (`docs/trace.md` §11). Rubric `label/rubric.json`. |
| Checklist (POC 9, advisory) | `jev-latest` (answered as `jev-1.14.0` on 2026-10-22) | TypeSafe system-one API, `https://api.typesafe.ai/v1` | 2026-10-22 | Seven yes/no questions (`label/checklist.json`) as Noul probabilities; never decides a label. Hosted, weights not published; data terms: no training on user data (zero retention is enterprise-only). Answers recorded once and replayed. |
| Policy model | `Qwen/Qwen3.5-9B` (bf16, served as `policy-qwen3.5-9b`) | vLLM 0.32.0 on AWS g6e.xlarge (L40S 48 GB), reached only through the gateway | 2026-10-23 | Picked over `Qwen/Qwen3-8B` on the dry-run set (20 train tasks × 4, concurrency 32): pass^1 0.54 vs 0.30, pass^4 0.20 vs 0.10. Flags: `--tool-call-parser qwen3_coder` (20/20 valid tool calls), `--default-chat-template-kwargs '{"enable_thinking": false}'` (thinking is on by default and its text lands in the reply), `--max-model-len 32768`, `--enable-prefix-caching`, `--seed 300`. POC 12 trains in the same mode. `meta-llama/Llama-3.1-8B-Instruct` not tried (gated). |
| User simulator | `google/gemma-3-27b-it` | Nebius Token Factory, through the gateway | 2026-10-23 | Larger than the policy and from a different family; temperature 0 (τ² `--user-llm-args`). Its output is never training data. |
