# POC 13 statement: compare with a closed frontier model

Run the same held-out benchmark with Claude Opus 5.5 as the policy, record score and cost, and compute claim 3's
M15 (`notes/claims.md` is private; the claim is restated here): a ~9B open-weight model, LoRA-retrained only on its
own passed runs, reaches **X = retrained pass^4 ÷ Opus 5.5 pass^4 ≥ 0.75** at **Y = Opus cost per task ÷ retrained
cost per task ≥ 5**, on held-out user-disjoint τ²-bench retail, with one fixed open-weight user simulator. Same split,
simulator, seeds, 4 trials and default `llm_agent` scaffold for every arm. Opus reasoning effort pinned before the
run. Interval printed next to the point estimate.

## Inputs that exist
- Fleet runs and their `fleet matrix` output (`fleet/CONTRACT.md`, `docs/architecture.md` §10–§11): held-out
  sessions of the base model (`data/poc11/p11-heldout`, `data/poc12/p12-heldout-base`) and of the retrained model
  (`data/poc12/p12-heldout`, `data/poc12/p12-heldout-r2`), each 44 tasks × 4, with `trials.jsonl`, `matrix.json`,
  `fleet.json`.
- The gateway (`gateway/gateway.py`) forwards OpenAI-format chat calls to the upstream named in its route table
  (`{model: [{base_url, key}]}`) and writes one `llm.call` row per call (`docs/trace.md`). τ² calls the policy
  through LiteLLM's `openai/` provider with `--agent-llm-args` (today always with a `temperature`).
- Anthropic's Messages API (`POST https://api.anthropic.com/v1/messages`, headers `x-api-key`,
  `anthropic-version: 2023-06-01`). Facts measured 2026-10-24 that the design must live with:
  - Claude Opus 5.5 returns 400 for any `temperature` (also on Anthropic's OpenAI-compatible endpoint).
  - Thinking cannot be disabled on Opus 5.5; depth is set by `output_config.effort` (`low`…`max`). The
    OpenAI-compatible endpoint ignores `reasoning_effort` and returns no cache or thinking usage, so it cannot
    show that effort was pinned.
  - `max_tokens` is required. Forced `tool_choice` (`any`/`tool`) returns 400.
  - Usage fields: `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`
    (billed, includes thinking), `output_tokens_details.thinking_tokens`. Top-level
    `cache_control: {"type": "ephemeral"}` turns on automatic prompt caching.
  - List prices (2026-10-24): input $4/Mtok, 5-minute cache write $5/Mtok (1.25×), cache read $0.20/Mtok,
    output $20/Mtok.
  - Opus can return several tool calls in one turn; τ² sends one `role: tool` message per call.
- Pinned for the closed arm (decided 2026-10-24, before any closed-model score): model `claude-opus-5-5`, effort
  `medium` (Anthropic's documented default for this model), `max_tokens` 16000, automatic prompt caching on.

## Done tests
1. **A closed model behind the same gateway.** A call from a VM naming the closed model reaches Anthropic's
   Messages API with the pinned effort, `max_tokens` and caching, and comes back to τ² as an OpenAI chat
   completion. Tool calls, parallel tool calls, tool results, text with tool calls, empty content and finish
   reasons survive the round trip. Sampling fields the model refuses (`temperature`, `top_p`) and fields it cannot
   take (`seed`, forced `tool_choice`) are not sent, and the row says which were dropped. Every `llm.call` row of
   the closed model records the effort actually sent, `max_tokens`, and the full billed usage (the four token
   counts and thinking tokens); `prompt_tokens` = input + cache write + cache read, `completion_tokens` = output.
   The upstream's own status and errors (429, 5xx, a refusal, `max_tokens` stop) are recorded and passed back as
   they are, never retried by the gateway: one trial is one attempt. The existing hashes (`seen()`) and the
   response-id join keep working.
2. **Same everything else.** A fleet run with the closed policy uses the same split, simulator, NL judge, seed,
   trials and concurrency as the open arms; `fleet.json` records the policy, the route kind, and the pinned settings
   (effort, `max_tokens`, caching). A run whose rows show another effort, a missing usage field or another policy
   is refused by `fleet matrix`, not averaged.
3. **Cost from the bill.** For a token-priced policy, M12 is computed from the recorded usage of every policy call
   at dated list prices including the cache rates, per trial and per successful trial, next to the uncached
   ceiling (all prompt tokens at the input price). A third party can recompute it from `trials.jsonl`/rows and
   `prices.json` alone. The open arms keep their GPU-hours basis, with their token ceiling also printed.
4. **Closed outputs never train.** Runs whose policy is a closed model are refused by name by `train select` and
   cannot be kept by `label`.
5. **M15.** A command takes one or more sessions per arm (the retrained arm and the closed arm), pools trials per
   task, and prints pass^1 and pass^4 per arm, X, Y and the difference of pass^4, each with a paired percentile
   bootstrap interval over tasks (seed 300, 2,000 resamples; the ratio's interval resamples tasks and recomputes
   the ratio), the per-session values of every arm, and both claim bars with pass / fail / unresolved (an interval
   that straddles the bar is unresolved).
6. **The run.** Two closed-model sessions and one same-day retrained session, each held-out 44 × 4, through the
   unchanged fleet configuration; M15 reported with the existing two retrained sessions pooled with the new one,
   and also same-day only.

## Metrics touched
M11, M12, M13 for the closed arm; M15.
