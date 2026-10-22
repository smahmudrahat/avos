# POC 8 results

`retrain/tests/run.py` (written independently from the targets before the solution) locally and on the dev host
(c8i.large, 2 vCPUs, nested virt), 2026-10-22. Design: `docs/trace.md` §10. Interface: `retrain/CONTRACT.md`.
Host demo: `just poc8`.

Built: `retrain/bin/retrain` (`runs`, `view`, `export`, `stats`; 330 lines) over `retrain/build.py` (570 lines), which
builds one item per (run, principal) from that run's own files on every call: the store's events by their own ids
(chains recomputed on read), the counters (M8 via `avos`'s own function), the τ² transcript as a witness, and the
pinned τ² prompt template and 16 retail tool schemas (`retrain/tau2_pinned.json`). No cache, no service, standard
library only; nothing is written under the store, the results or the ledger. The gateway adds five fields to the
`llm.call` it already writes, hashes only (`response_id`, `system_sha256`, `tools_sha256`, `messages_sha256`,
`output_sha256`; `docs/trace.md` §5), over one form in `trace/lib_trace.py` that a reader of the transcript can
compute too. No message text is kept on the host.

| Done test | Result |
|---|---|
| 1. A labeler judges a run from the viewer alone | `view --run R` (text, or `--html`): run, principal, task, evidence, M8 status; `WITNESS DISAGREES` first when it does; reward, passed/failed, termination, `db_match` and every action check with its arguments and verdict, NL assertions; cost and timestamps; the tools; then the rendered system prompt and every message in full, each marked `[policy]` or `[env]` with the store event that vouches for it, every tool call with id and arguments, every tool result; children with their item ids. Checked on a failed and a passed run (16 messages each) and on a POC 7 tree (`support`'s 10 gateway tool calls by their `tool.call` ids, its three sub-agents' own conversations). 0.19 s on the live store, incl. ssh. |
| 2. TRL / Axolotl accept the export | TRL 1.16 `SFTTrainer` (Qwen3 tokenizer) tokenizes `trajectories.jsonl` (5/5 store runs; also all 15 items of the 20-run environment) and `sft.jsonl` (35 and 86 prompt→completion rows, one per policy turn); in every row only the completion carries labels, never the greeting, the system prompt or the simulator. `trajectories.jsonl` has a boolean `train` on every message and a `tools` column (Axolotl `chat_template`, `message_field_training`). |
| 3. τ² run → events → trajectory → same messages and tool-call ids | 5 store runs, 80 messages: identical order, roles, content, tool-call ids, arguments and usage (system prompt aside). Inside the pinned τ² on the host, `SimulationRun.model_validate` of the rebuilt runs and a message-by-message comparison: 5/5 identical (with the suite's script made dict-safe; see disputes). |
| 4. Manifest lists every item's hash and split; re-export is identical | `trajectories.jsonl`, `sft.jsonl`, `manifest.json`, `stats.json` byte-identical across re-runs, with caches and `$HOME` emptied, with another `--out`, from a copy of the roots elsewhere, and twice over the host's 63 runs in the final pass. `item_id` is the SHA-256 of the canonical item; `dataset_sha256` hashes the id list. |

Acceptance suite, final pass (2026-10-22): selftest 48/48; local full **41/41** twice; `--host` **47/47** twice in a
row without clearing state (incl. HOST0/HOSTZ: no stray process or scratch on the host, no ssh master on the laptop). Before the rulings: full 30/34; the
disputes below were all accepted.

## Native runs with host hashes

Four τ² runs on train tasks through the new gateway (one VM each, destroyed): `run_retail-1_4` (failed) and
`run_retail-0_16` (passed), run for this POC, and `run_retail-0_17` (failed) and `run_retail-0_18` (passed) from
the POC 7 suite's τ² check. 47 of 47 calls join by `response_id`, and all four runs pass all six comparisons
(`tokens`, `role`, `system`, `tools`, `output`, `messages`): every agent turn's prompt, message by message, is the
item's messages before it; every turn's content is what the model answered. Agent calls carry the 16 pinned
schemas and the rendered system prompt; simulator calls carry no tools and another system prompt, which is how the
host splits policy (31 calls) from environment (16) without the witness's say-so.

Tampering a copy of `run_retail-1_4` (the live roots untouched), each with token counts unchanged:

| Edit to the witness or the metadata | Result |
|---|---|
| one word of an agent reply | flagged: `#3` output hash, and `#5` prompt differs from `#3` |
| one tool-call argument | flagged: output hash `#5`, prompt `#7` |
| one character of a simulator turn | flagged: output hash `#4`, prompt `#5` |
| one in-harness tool result (no model produced it) | flagged: prompt `#7` and `#9` differ from `#6` |
| the policy (system prompt) | flagged: system hash and prompt from `#0` |
| one tool description in `AVOS_TOOL_SCHEMAS` | flagged: tool schemas differ on every agent call |

Every flagged item stays inspectable, trains nothing, yields no `sft.jsonl` row and makes `export` exit 1.

## Measured

| Item | Measured | Bar |
|---|---|---|
| `export` of 700 synthetic runs (`--runs-file`) | 2.2 s, 61 MiB peak RSS, 700 items, 4,900 sft rows | ≤ 145 s, ≤ 512 MiB |
| `export --all` in the suite's scale environment (700 + 14 real runs) | 2.4 s, 62 MiB, 708 items, rc 1 (6 witness-only runs skipped) | see disputes |
| 20 runs, store with 20 vs 2,020 VMs | 0.10 s vs 0.10 s (×1.01) | ≤ 3× |
| `view` on the 2,020-VM store / live store incl. ssh | 0.05 s / 0.19 s | ≤ 2 s / ≤ 2.5 s |
| Gateway: hashing one call (largest request of a real run, 32 KB, 19 messages) | 0.29 ms | upstream answers take 0.6–2.6 s |
| Live store, `validate` + `verify` of all files after the change (incl. the 14 pre-release events) | 158,551 events, 0 errors | 0 |
| Host `export --all` (final pass): 63 runs | 36 exported, 27 witness-only (skipped `no_host_rows`); exit 1 for the skips; two exports' `manifest.json` byte-identical | — |
| Host dataset | 36 items, 203 sft rows; 15 passed / 21 failed / 0 unknown; 34 train, 0 held-out, 2 unsplit; M14 = 14 / 34 train runs = 0.41 | — |
| Host items flagged | 13: 11 τ² and 2 coding runs from before the gateway existed (0 model calls on the host for 13 and 30 transcript turns), and `run_coding-slugify_2` (12 assistant turns vs 15 calls) | never trained |
| Judge calls set aside (`witness.other_calls`: event id, model, tokens) | 3 runs, 1 call each (`deepseek-ai/DeepSeek-V4-Flash-0731`) | — |
| Shortcut statistic (host) | top `find_user_id_by_name_zip` at index 5 in 37.5 % of items: not flagged | flag ≥ 90 % |

Regressions, final pass (2026-10-22): POC 6 suite **65/65** full locally. On the host: POC 7 `--quick` **28/28**;
`claim1 --quick` **8/8** (M6 halt 62 ms median and worst, also after 20,000 appended events; 3,200 calls, 0
unanswered, p50 27 ms, p99 76 ms); `just poc4` exit 0, M5 6/6 = 100 %, gateway round trip median 15 ms, guest agent
RSS 25 MB. An earlier POC 7 run read 26/28 from a race in that suite (it counts a principal's events and then takes
`avos completeness` while the VM keeps calling; a call landed in between, 29 vs 28); it did not recur. The whole live
store validates and verifies.

## Contract disputes (all accepted by the tester, 2026-10-22)

| Check | Observation | Minimal change |
|---|---|---|
| `D2_trl_dry_tokenization` | 3 of 35 rows fail "environment text ≥ 24 chars trained": the policy's own `exchange_delivered_order_items` call begins `{"order_id": "#W2378156"`, the same 24 characters as an earlier tool result. The trained text is exactly the completion (the template re-serializes the arguments itself). | Skip a prompt snippet that also occurs in the completion's own content or arguments. |
| `D4b_manifest_hashes_counts` | `counts.runs_requested` is 21; the check expects 20. The environment has 21 run directories (14 copied + 7 clones), and `A0` counts them from the directory. | Compare with the number of run directories. |
| `H7_mini_swe_agent` | `trajectory.messages` ends with `{"role": "exit"}`. `H7` wants all 30 messages verbatim; `D4b` (and §5) allow only system/user/assistant/tool roles. Both cannot hold. The export drops the end marker; its status is `outcome.termination_reason`. | Compare `trajectory.messages` without role `exit`. |
| `SC1_export_700_runs` | The scale environment holds the 14 fixture runs too, 6 witness-only. `--all` must include and skip them (§6), so the result is 708 items and exit 1. The 700 clones alone: 2.2 s, 61 MiB, 700 items, exit 0. | Build the scale environment without the fixture runs, or expect 708 items and only `no_host_rows` skips. |
| `HOST2_tau2_roundtrip_pinned` | `tau2_roundtrip.py` reads `m.usage.prompt_tokens`; in the pinned τ² `usage` is a dict, so it raises after the messages compared equal. With that one line dict-safe: 5/5 runs, 80 messages, identical. | `m.usage["prompt_tokens"]` when `usage` is a dict. |
| §4 (no check) | τ²'s NL-assertion judge calls its own model under the agent's identity after the conversation. Under "every such llm.call is matched", every run of the 40 tasks with NL assertions is flagged and never trained (3 such host runs). The export matches only calls to the models the witness names (`info.agent_info.llm`, `info.user_info.llm`) and lists the others in `witness.other_calls`. A judge on the agent's own model is still flagged. | §4: "the next stored `llm.call` of the run's harness agent to a model the witness names". |

## Tech debt

| Item | Effect today | Fix | When |
|---|---|---|---|
| **Simulator prompts are not compared.** The simulator sees the conversation with roles flipped and tool turns dropped, under its own system prompt. | Its output hash and the role split are checked; its input is not. It is never trained. | Rebuild τ²'s flipped view from `info.user_info` and compare `messages_sha256`. | If simulator turns are ever trained or judged. |
| **Pre-release hashes (`run_retail-0_15`).** Its 14 calls hash raw wire messages. | Role, system, tools and tokens are checked; output and input are not. | None (history); later runs carry the final form. | — |
| **Legacy runs are witness-trusted beyond token counts.** No ids or hashes on the host before POC 8. | 27 τ² runs (4 store, 23 ledger) join by token-count order only (`checked: tokens`). | None possible; they say so in `witness.join` / `checked`. | — |
| **Imported store files are scanned per run.** `avos import` writers span VMs, and `retrain` builds no index for them. | Cost grows with imported bytes (none on the host today). | Read `avos`'s import index, verified line by line as `avos` does. | When imports hold training runs. |
| **Judge calls are told apart by model.** | A judge configured with the agent's model reads as a leftover call: flagged, never merged. | The gateway could hash and tag the judge's request, or the harness could run the judge as its own principal. | Before POC 10 if the judge model changes. |
| **Per-run work is serial.** 700 runs take 2.2 s. | — | Shard runs over processes; manifests merge by sorting items. | Beyond ~10⁵ runs per export. |

## Does not prove

- **Not what the model saw for older runs.** Only runs collected through the changed gateway (the four above and
  the τ² runs the regression suites started since) carry the final hashes; every earlier exported run's content is
  the witness's, checked against token counts (or, for coding runs, call counts) only. 13 host runs predate the
  gateway and can never be trained.
- **Not the user simulator's input**, nor anything inside a reply the client drops before τ² stores it (reasoning
  fields, refusals); `chat_form` covers role, content, tool calls and `tool_call_id`.
- **Not at fleet scale.** 700 synthetic runs cloned from five real ones; four native runs with the final hashes.
- **Not label quality.** The statistics show the tool-at-index shortcut and exact duplicates. Whether a passed run
  is a good example is POC 9's question.
- **Not tamper-proof.** Someone who rewrites a stream and its chain, and the transcript to match, is caught by nothing
  here (as in POC 7: heads are not anchored).
- **Axolotl itself was not run**, only the shape its loader reads (`train` per message, `tools` column).
