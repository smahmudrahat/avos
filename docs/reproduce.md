# Reproducing the results

Everything needed to recreate the benchmark results in the [work report](https://impartial-filament-9e5.notion.site/Avos-the-cloud-built-for-AI-agents-405c7fa4698281119182cb0142d8fe20):
which model plays which role, with what settings, what each judge sees and decides, the exact data formats, and the
commands in order. Every value here is copied from the pinned sources named in each section; when they disagree,
the source wins.

Hosted models do not repeat at temperature 0 (measured in POC 9), so a re-run gives close but not identical
numbers. The recorded runs in `data/poc11` to `data/poc14` let you recompute every reported figure exactly.

## 1. Roles and models

Source: `docs/versions.md` ("Models"), `docs/conventions.md` §2.

| Role | Model | Where it runs | Settings | Output used for training? |
|---|---|---|---|---|
| Policy (the agent under test) | `Qwen/Qwen3.5-9B`, bf16, served as `policy-qwen3.5-9b` | vLLM 0.32.0, two AWS g6e.xlarge (NVIDIA L40S 48 GB) | Temperature 0 (train sampling runs: 0.7, top_p 0.8), thinking off, seed 300 | Yes, only its own passed train runs |
| Retrained policy | LoRA on the above, merged to a bf16 checkpoint, same served name | Same vLLM command | Same | No (evaluation only) |
| User simulator (plays the customer) | `google/gemma-3-27b-it` | Nebius Token Factory | Temperature 0 | Never |
| Benchmark judge (grades natural-language assertions inside τ²) | `deepseek-ai/DeepSeek-V4-Flash-0731` | Nebius Token Factory | τ² default temperature, `max_tokens` 4096, one retry if the answer is not JSON | Never |
| Label judge (decides which runs become training data) | `openai/gpt-oss-120b` | Nebius Token Factory | Temperature 0, seed 300, `max_tokens` 4000, JSON object | Never |
| Frontier comparison | `claude-opus-5-5` | Anthropic Messages API, through the Avos gateway | Effort `medium`, `max_tokens` 16000, automatic prompt caching; no temperature, top_p, seed or forced tool choice | Never |
| Simulator check (POC 14 only) | `claude-haiku-5-5` (effort medium), `claude-sonnet-5-5` (effort high) | Anthropic, through the gateway | `max_tokens` 16000, caching, no temperature | Never |

Rules that hold for every arm:

- One fixed user simulator and one fixed benchmark judge, the same for every model compared.
- τ²'s default `llm_agent` scaffold, unchanged.
- Closed models are used only for evaluation. Their outputs never enter the training data.

### Policy server command

```
vllm serve <MODEL_DIR> --served-model-name policy-qwen3.5-9b --host 0.0.0.0 --port 8000 \
  --max-model-len 32768 --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  --enable-prefix-caching --gpu-memory-utilization 0.92 --seed 300 \
  --default-chat-template-kwargs '{"enable_thinking": false}'
```

`train/bin/train serve-cmd` prints this command for a given checkpoint. With two servers, the gateway sends a call
from VM slot `vm-NN` to server `NN mod 2`, so a whole conversation stays on one server.

## 2. Benchmark

Source: `docs/conventions.md` §1, `docs/split-notes.md`, `docs/versions.md` ("Benchmark").

| Item | Value |
|---|---|
| Benchmark | τ²-bench **retail**, `sierra-research/tau2-bench` commit `96a3ef92329baf908ffe9dfe188cea34e8701d2a` |
| Install | `uv sync --frozen` (LiteLLM 1.81.11) and `uv pip install websockets==17.2` |
| Patches (applied by `infra/image/build.sh`, which fails if upstream text changes) | Benchmark judge read from env `TAU2_NL_JUDGE`; judge `max_tokens` 4096; judge asked once more when its answer is not JSON |
| Invocation | `tau2 run --domain retail --task-ids <T> --num-trials 1 --max-concurrency 1 --max-retries 0 --seed 300`, one microVM per trial |
| Split | `data/splits/retail_user_disjoint_v1.json`: tasks grouped by the customer they act on, whole customers assigned with seed 300. 65 train tasks, 44 held-out tasks, 5 excluded (64, 105, 18, 49, 91) |
| Trials | 8 per train task, 4 per held-out task per session |
| Rebuild the split | `python3 eval/make_split.py <tau2-checkout> && python3 eval/check_split.py <tau2-checkout>` |

`--max-retries 0` matters: at this commit τ² otherwise re-runs a failed simulation up to 3 times and keeps only the
success, so one trial could hide two conversations.

Scores on the 40 tasks that use natural-language assertions are graded by the open-weight judge above, not τ²'s
default `gpt-4.1-2025-04-14`, so they are not directly comparable with the public leaderboard.

## 3. The two judges

### 3.1 Benchmark judge (inside τ²)

τ² grades each trial with its own reward: a database-state check, action checks, and, for 40 retail tasks, a list
of natural-language assertions graded by an LLM. That LLM is `deepseek-ai/DeepSeek-V4-Flash-0731`, set through
`TAU2_NL_JUDGE`. Its prompt is τ²'s own (`src/tau2/evaluator/evaluator_nl_assertions.py` at the pinned commit),
unchanged except for the `max_tokens` cap and the single retry. A trial passes when its reward is 1.0.

### 3.2 Label judge (which runs become training data)

Source: `label/bin/label`, `label/rubric.json`, `docs/trace.md` §11, `label/CONTRACT.md`.

The label judge never sees the reward. It reads the whole conversation and the tool descriptions and answers a fixed
rubric (`label/rubric.json`, name `avos-rubric-v1`):

| Field | Question | Veto when |
|---|---|---|
| `policy_followed` | Did the agent follow the domain policy at every step, including authenticating the user and getting explicit confirmation before any change? | `false` |
| `right_tools` | Did the agent call the tools the task needed, with the right arguments, and no wrong or unnecessary ones? | `false` |
| `hallucinated_facts` | Did the agent tell the user anything the tool results and the policy do not support? | `true` |
| `unsafe_action` | Did the agent perform or offer an action the user did not ask for or the policy forbids? | `true` |

Answer format: one JSON object `{"policy_followed": bool, "right_tools": bool, "hallucinated_facts": bool,
"unsafe_action": bool, "reasons": [string]}`, at most three short reasons.

**Two framings.** Every run is judged twice, with different wording and order:

| Framing | System message | User message |
|---|---|---|
| `a` | "You review a recorded conversation between a customer-service agent and a user. Judge the agent's conduct with the rubric below." plus the rubric | The question, then the conversation, then the tools |
| `b` | "You audit an agent's work for any mistake, however small: a skipped policy step, a wrong or missing tool call, an unsupported statement, an action nobody asked for." | The question, then the rubric, then the tools, then the conversation |

The exact strings are `FRAMINGS` in `label/bin/label`. Request body: `temperature` 0, `seed` 300, `max_tokens`
4000, `response_format: {"type": "json_object"}`; up to 4 attempts, 120 s timeout, 8 parallel workers. A reply that
stops at the length limit or is not a valid rubric object is invalid.

A framing's verdict is `veto` if any rubric answer is in its veto column, else `pass`. If either framing vetoes,
the run is vetoed. The share of runs where both framings agree is the judge's **consistency**.

**Answers are recorded once and replayed.** Each answer is stored with its run, content hash, model, rubric hash and
prompt hash. A later run reuses it; changing any of those asks again.

### 3.3 Gold set and label quality (M10)

A gold set of 60 runs is drawn with a fixed seed from three strata: reward passed, reward failed, and judge
disagrees with the reward or the reward is uninformative. It is labelled blind (`label view` hides the outcome,
judge and split). M10 is Cohen's kappa of judge against the gold label and of reward against the gold label, with a
95% interval; below 50 gold labels it reads `unverified`.

In POC 12 the gold labels were given by an Opus 5.5 model acting as a proxy for a human labeller (labeller name
`opus-proxy`), through a script that is not part of this repository. Results: kappa judge/proxy 0.55 [0.34, 0.76]
(n 60), kappa reward/proxy 0.38 [0.13, 0.62] (n 57) (`data/poc12/p11-train-labels-p12/summary.json`). To reproduce
with a person instead, label the gold set with `label gold --next` and `label human`.

### 3.4 The labelling rule

In this order (`label/CONTRACT.md`):

1. A structural failure fails: item hash mismatch, incomplete trace, a harness record that disagrees with the
   gateway's, an infrastructure error, a context-window overflow, or an unknown reward.
2. A gold label decides a reward-passed or uninformative run.
3. An uninformative reward fails (rule `tau2_384`): retail tasks 10, 12, 25, 50, 57 and 65 pay a do-nothing agent
   1.0.
4. A failed reward fails.
5. A judge veto fails.
6. Otherwise the run passes and is **kept** as training data.

Label counts for the two training fleets:

| Fleet | Runs | Kept | Dropped | Judge consistency | Source |
|---|---|---|---|---|---|
| `p11-train` (policy temperature 0), after the gold labels | 520 | 178 | 342 | 0.873 | `data/poc12/p11-train-labels-p12/summary.json`, `data/poc11/p11-train/labels/summary.json` |
| `p12-train-t07` (policy temperature 0.7, top_p 0.8) | 520 | 178 | 342 | 0.844 | `data/poc12/p12-train-t07/labels/summary.json` |

## 4. Data formats

### 4.1 Event (`trace/schema.json`, `docs/trace.md`)

One JSON object per line, stored per VM under `/var/lib/avos/trace`. Kinds: `vm.boot`, `vm.exit`, `session.open`,
`session.close`, `agent.spawn`, `agent.exit`, `llm.call`, `tool.call`, `tool.result`, `fs.write`, `net.connect`,
`authz.decision`, `halt`, `resume`, `label`. Every event has `event_id`, `kind`, `ts` (host clock, UTC, ms),
`writer`, `seq`, `host`, `principal` (a SPIFFE ID such as `spiffe://avoslab.com/vm/vm-01-82ec92/agent/tau2`),
`vm_id`, and `prev` / `hash` (hash chain per writer).

`llm.call` adds: `authz_ref` (the decision that allowed it), `model`, `status`, `ms`, `prompt_tokens`,
`completion_tokens`, `response_id`, hashes of what the model saw and said (`system_sha256`, `tools_sha256`,
`messages_sha256`, `output_sha256`), `upstream`, `upstream_status`, `error`, `finish_reason`. Calls to Anthropic
also record `route_kind`, `effort`, `max_tokens`, `cache`, `dropped_fields`, `usage` (input, cache write, cache
read, output, thinking tokens) and `stop_reason`. No prompt or reply text is stored on the host.

### 4.2 Training item (`retrain/CONTRACT.md` §5)

`retrain export` writes one item per run and principal to `trajectories.jsonl`:

```
{"item_id", "run", "principal", "parent", "children", "vm_id", "host", "harness", "task", "task_id",
 "evidence": "store"|"ledger"|"witness",
 "messages": [OpenAI chat messages], "tools": [OpenAI function schemas],
 "outcome": {"reward", "passed", "termination_reason", "reward_breakdown", "source"},
 "cost": {"usd", "prompt_tokens", "completion_tokens", "llm_calls", "policy_calls", "environment_calls"},
 "timestamps": {"start", "end", "wall_ms"},
 "trace_complete": {"status", "ratio"},
 "witness": {"present", "agrees", "problems", "sha256", "join", "checked", "other_calls"},
 "lineage": {..., "events": [every event_id the item was built from]},
 "split": "train"|"held_out"|null, "redact"}
```

`item_id` is the SHA-256 of the item's canonical JSON without `item_id`
(`json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`). Policy turns carry `train: true` and
the gateway's `usage`. `sft.jsonl` holds one prompt/completion example per policy turn.

### 4.3 Label row (`label/CONTRACT.md` §5)

One row per run in `labels.jsonl`:

```
{"run", "item_id", "content_sha256", "task", "task_id", "split",
 "level0": {"reward", "passed", "termination_reason", "trace_complete", "witness_agrees", "checks",
            "structural_ok", "reward_informative", "status", "reasons"},
 "level1": {"model", "rubric_sha256", "verdict": "pass"|"veto"|"unknown",
            "framings": {"a": {...}, "b": {...}}, "consistent", "reasons"},
 "level2": null | {gold label},
 "final": {"label": "pass"|"fail", "rule": "level0"|"tau2_384"|"reward"|"veto"|"human", "kept": bool},
 "stratum"}
```

`content_sha256` hashes only what the labeller read (the task, the tools and every message's role, content, tool
calls and `tool_call_id`), so a harmless re-export does not orphan a label.

### 4.4 Results matrix (`fleet/CONTRACT.md`)

`fleet matrix` writes `matrix.json` per session: `m11` (pass^1, pass^4 and their intervals, per-task `c` and `n`),
`m12` (cost per role), `m13` (wall time), `m2`, `m8`, `m16`, and one row per trial with its status and cause.

## 5. Scoring

Source: `fleet/CONTRACT.md`, `docs/architecture.md` §10 and §11.

- Per task: `n` valid trials, `c` passed. Trials removed for an infrastructure cause leave `n`; trials that fail
  because of the agent stay in `n`.
- **pass^1** = mean over tasks of `c / n`. **pass^k** = mean over tasks of `C(c, k) / C(n, k)` with k = 4; a task
  with `n < 4` is listed and left out.
- Sessions of one arm are pooled per task (`n` and `c` summed) before scoring.
- **Intervals**: paired percentile bootstrap over tasks, seed 300, 2,000 resamples, 95%.
- **X** = retrained pass^4 / Opus pass^4; **Y** = Opus cost per task / retrained cost per task. Their intervals
  resample tasks and recompute the ratio; a resample with a zero denominator is counted, never infinite.
- **Cost per task**. Open policy: GPU-hours, `2 × $1.861/h × session wall time / trials`. Opus: billed tokens at
  list price, input $4, cache write $5, cache read $0.20, output $20 per million tokens (`data/poc13/prices.json`).
  The uncached ceiling (all prompt tokens at the input price) is printed beside it.

## 6. Retraining

Source: `train/README.md`, `train/CONTRACT.md`, `docs/versions.md` ("POC 12 retraining").

| Setting | Value |
|---|---|
| Base | `Qwen/Qwen3.5-9B`, Hugging Face snapshot `c202236` |
| Data | Kept train runs of `p11-train` and `p12-train-t07`; 17 train tasks held back as a regression gate. 265 runs, 3,089 policy turns, 24.6 M tokens |
| Prompt parity | Every policy turn re-rendered with the served tokenizer and chat template (tool-call arguments parsed to objects, tools passed, thinking off) must reproduce the gateway's `prompt_tokens` and `completion_tokens` (3,089 of 3,089) |
| Loss | Cross-entropy on completion tokens only |
| LoRA | r 16, alpha 32, dropout 0, every linear layer of the language model (q/k/v/o, in_proj_qkv/z/a/b, out_proj, gate/up/down), none in the vision tower or MTP head |
| Optimiser | AdamW, learning rate 5e-5, cosine with 5% warmup, 2 epochs, 16 sequences per step, seed 300, bf16, gradient checkpointing |
| Libraries | torch 2.13.0+cu130, transformers 5.19.0, peft 0.21.2, flash-linear-attention 0.6.0 |
| Hardware and cost | One g6e.xlarge (L40S), 160 steps, 15,295 s, 4.25 GPU-hours, $7.91 |
| Merge | Adapter merged into the base bf16 shards; tokenizer and chat template byte-identical; served by the same vLLM command |

## 7. Commands, in order

Prerequisites: a Linux host with `/dev/kvm` (Ubuntu 24.04), root, Python 3.12, `uv`, `just`; a GPU host for the
policy; an OpenAI-compatible endpoint for the open models; an Anthropic API key for the frontier arm. Copy
`.env.example` to `.env`.

```
# 1. Host setup (README "Quick start")
just push && just spire && just gateway && just image

# 2. Baseline train and held-out runs (POC 11)
sudo python3 fleet/bin/fleet run --out OUT/p11-train --part train --trials 8 --concurrency 32 \
  --slots vm-03-vm-34 --mem-mib 2048 --policy policy-qwen3.5-9b --json
sudo python3 fleet/bin/fleet run --out OUT/p11-heldout --part held_out --trials 4 --concurrency 32 \
  --slots vm-03-vm-34 --mem-mib 2048 --policy policy-qwen3.5-9b --json

# 3. Sampled train run (POC 12)
sudo python3 fleet/bin/fleet run --out OUT/p12-train-t07 --part train --trials 8 --concurrency 32 \
  --slots vm-03-vm-34 --mem-mib 2048 --policy policy-qwen3.5-9b --agent-temperature 0.7 --agent-top-p 0.8 --json

# 4. Export, label, select, fit, merge (POC 8, 9, 12)
retrain/bin/retrain export --out EXPORT --all --split data/splits/retail_user_disjoint_v1.json
label/bin/label run --dataset EXPORT --out LABELS --level 0,1
train/bin/train select --out DATA --pair EXPORT LABELS --fleet OUT/p11-train/fleet.json \
  --pair EXPORT2 LABELS2 --fleet OUT/p12-train-t07/fleet.json --policy policy-qwen3.5-9b --tokenizer MODEL_DIR \
  --exclude-task <17 gate tasks>
train/bin/train fit --data DATA --model MODEL_DIR --out FIT --gpu-usd-per-h 1.861
train/bin/train merge --model MODEL_DIR --adapter FIT/adapter --out MERGED   # serve MERGED with the command in section 1

# 5. Retrained held-out runs, then Opus (POC 12, 13). The Opus route in /var/lib/avos/gateway.routes.json:
#    "claude-opus-5-5": {"base_url": "https://api.anthropic.com", "key": "ANTHROPIC_API_KEY",
#                        "kind": "anthropic", "effort": "medium", "max_tokens": 16000, "cache": true}
sudo python3 fleet/bin/fleet run --out OUT/p13-heldout-opus --part held_out --trials 4 --concurrency 32 \
  --slots vm-03-vm-34 --mem-mib 2048 --policy claude-opus-5-5 --json

# 6. Score
python3 fleet/bin/fleet matrix --fleet OUT/<session> --prices data/poc13/prices.json --json --out OUT/<session>/matrix.json
python3 fleet/bin/fleet m15 --retrained <retrained matrices> --closed <opus matrices> --out m15.json
```

Exact flags for every command: `fleet/CONTRACT.md`, `retrain/CONTRACT.md`, `label/CONTRACT.md`,
`train/CONTRACT.md`, `eval/CONTRACT.md`.

## 8. Recomputing the reported numbers from the recorded data

No hosts or API keys are needed. From the repository root:

```
python3 fleet/bin/fleet m15 \
  --retrained data/poc12/p12-heldout/matrix.json --retrained data/poc12/p12-heldout-r2/matrix.json \
  --retrained data/poc13/p13-heldout-retrained/matrix.json \
  --closed data/poc13/p13-heldout-opus/matrix.json --closed data/poc13/p13-heldout-opus-r2/matrix.json \
  --base-root /home/ubuntu/.cache/huggingface/hub/models--Qwen--Qwen3.5-9B/snapshots/c202236235762e1c871ad0ccb60c8ee5ba337b9a \
  --assert-served p12-heldout=retrained --assert-served p12-heldout-r2=retrained
```

Expected: retrained pass^1 0.477 and pass^4 0.188, Opus pass^1 0.492 and pass^4 0.197, X 0.957 [0.552, 1.626],
Y 18.18 [17.02, 19.37]. The two POC 12 sessions predate the record of which checkpoint served them, so their arm is
asserted on the command line and the output marks them "asserted, not recorded".

The website's numbers are rebuilt the same way with `python3 site/build.py`.
