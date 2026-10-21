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
| mini-swe-agent | `mini-swe-agent==2.4.6` + `litellm==1.81.11`, full freeze in the image at `/opt/harness/mini-swe-agent/freeze.txt` | POC 2, 2026-10-19 | LiteLLM pinned to τ²'s version: unpinned install pulled 1.106.0, which fails to import (`No module named 'mcp'`). Only top-level packages are pinned; the freeze records the rest. |
| Coding task | `eval/coding/slugify` (TASK.md, test.sh) | POC 2, 2026-10-19 | Done test 2. Tests restored from the clean copy before grading. |
| Verified fork (reference only) | `amazon-agi/tau2-bench-verified` commit `864350a8971a8f8ee9e7b8472e2edc380a806b0c` | POC 0 | Used to choose exclusions; see `split-notes.md`. |

## Models

| Role | Model | Endpoint | Since | Notes |
|---|---|---|---|---|
| Dev model (POC 1–9 plumbing) | `Qwen/Qwen3-30B-A3B-Instruct-2507` | Nebius Token Factory, `https://api.tokenfactory.nebius.com/v1` | 2026-10-19 | Not the policy model (8–9B, chosen in POC 10). `conventions.md` §2. |
| NL-assertion judge | `deepseek-ai/DeepSeek-V4-Flash-0731` | Nebius Token Factory | 2026-10-19 | Bare-JSON verdicts correct in 2/2 checks, 2–3 s. `zai-org/GLM-5.3` timed out (>120 s) 2/2. |
| Policy model | — | — | POC 10 | |
| User simulator | — | — | POC 10 | Larger and from a different family than the policy model. |
