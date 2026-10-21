# POC 2 sample runs

Real τ²-bench retail trials (`just poc2`) and one mini-swe-agent coding run (`just job mini-swe-agent coding/slugify`), 2026-10-19, kept as seed data for POC 6's trace schema.
Each dir: `result.json` (τ²: `Results` with one `SimulationRun`; mini-swe-agent: diff, test outcome, trajectory) and `job.json` (host-side job record).
Policy and user simulator: `Qwen/Qwen3-30B-A3B-Instruct-2507`; NL judge: `deepseek-ai/DeepSeek-V4-Flash-0731`; all on Nebius.

- `run_retail-0_2`: reward 1 (DB only).
- `run_retail-2_3`: reward 0; NL-assertion judge task.
- `run_retail-8_1`: reward 1.
- `run_coding-slugify_0`: mini-swe-agent 2.4.6 fixed `eval/coding/slugify` (POC 2 done test 2); original tests pass, 27 model calls, 345 s. The diff also shows scratch files the agent left behind. `harness_version` corrected by hand (the run predates that field).
