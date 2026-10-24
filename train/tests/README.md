# POC 12 acceptance suite

Independent, black-box checks of `train/CONTRACT.md`. Written before and without the implementation; the only
things it touches are `train/bin/train`, `fleet/bin/fleet compare`, the raw fixtures and, with `--host`, a GPU
host over ssh (read-only except its own scratch under `/tmp/p12-tests`, removed in `finally`).

```
python3 train/tests/run.py --selftest                 # the suite's own oracles on the real fixtures (~10 s, needs uv)
python3 train/tests/run.py --quick                    # every local check, no GPU (~3 min once implemented, warm uv cache)
python3 train/tests/run.py --host GPU --model-path /models/Qwen3.5-9B --steps 2 --port 8011 [--host-repo ~/poc]
                                                      # + fit / merge / identity serving on the GPU host (3 vLLM starts, ~15 min)
python3 train/tests/run.py --only K                   # one prefix
```

One line per check: `PASS|FAIL|SKIP <id> [<target>] <measured vs bar>`. Exit 0 only if every executed check passed.
A missing CLI reads `FAIL … not implemented: train/bin/train` / `not implemented: fleet compare`, never a traceback.
Scratch is a fresh `mkdtemp` per run, removed on success (`--keep` keeps it). Host scratch is `/tmp/p12-tests`;
every vLLM the suite starts goes through `hostproc.py` (scp'd to the host): `start` forks, `setsid`s, detaches 0/1/2 and execs, so the ssh call returns at once and the pid file holds the **session leader** (pid = pgid = sid); `stop` (in `finally`, and again for every pid file at exit) TERMs then KILLs every process in that session (vLLM's `EngineCore` child included), `fuser -k`s the port, and reports what is still alive (cross-checked with `nvidia-smi --query-compute-apps`) and whether the port is still open — any leftover is a FAIL line naming the pid. Readiness is polled with separate short ssh calls under the 900 s budget. Port 8000 (the host's production vLLM) is refused by both `run.py` and `hostproc.py stop`. Selftest exercises start/stop against a two-process fake that ignores SIGTERM.

## Fixtures (`fixtures/`, hash-gated by `MANIFEST.sha256` at the start of every run and in selftest)

| Path | What |
|---|---|
| `p11/export/trajectories.jsonl`, `manifest.json` | 12 real items of the POC 11 train export (`retrain export`, 2026-10-24), chosen small: 8 kept (`run_retail-17_11/10/14`, `77_15`, `73_72`, `68_11`, `51_8`, `57_9`) and 4 dropped (`17_9` veto, `57_10` tau2_384, `73_74` and `84_10` reward 0). `17_14` and `57_9` are human-kept; `57_9` has reward 0.0 (the human overrode τ² #384). `77_15`, `73_72`, `51_8` carry integer/boolean tool arguments. The manifest is the real one trimmed to these items with its counts and `dataset_sha256` recomputed. |
| `p11/labels/labels.jsonl` | their 12 real `label run` rows (`labels-p12`, with the 60 gold human labels applied) |
| `p11/fleet.json` | the real `data/poc11/p11-train/fleet.json` (policy role `policy-qwen3.5-9b`, split hash): what `--fleet` points at |
| `tokenizer/` | the served `Qwen/Qwen3.5-9B` tokenizer: `tokenizer.json.gz` (unpacked to scratch), `tokenizer_config.json`, `chat_template.jinja`, `config.json` |
| `data/poc11/p11-heldout/matrix.json`, `p11-train/matrix.json`, `data/splits/retail_user_disjoint_v1.json` | real repo files, hashed by the manifest so a changed baseline cannot pass unnoticed |

Everything else is synthesised at run time by `lib.py`: clones of real items for a second fleet (new host/VM/run,
same conversation, new `item_id`), held-out / unsplit / witness-flagged / content-edited variants with consistent or
deliberately inconsistent label rows, export directories with consistent manifests, synthetic matrices with known
per-task differences (pass fields recomputed from the parts), fit directories with known arithmetic, a fake vLLM
`/v1/completions` server for `replay`.

## The oracles (`lib.py`, `tok_oracle.py`)

- **Tokenizer oracle** (`tok_oracle.py`, run once per suite run under `uv run --with transformers --with jinja2
  --with tokenizers`, ~3 s warm): renders every policy turn of the 12 items exactly as vLLM did
  (`apply_chat_template(msgs[:k], tools, add_generation_prompt=True, enable_thinking=False)`, completion = what the
  template adds for `msgs[:k+1]` through `<|im_end|>`). Selftest proves it reproduces the gateway's
  `prompt_tokens` / `completion_tokens` on all 87 policy turns; the tester measured 958/958 on 60 items of the full
  export. Any solver rendering that disagrees with the gateway is therefore wrong, not "a different convention".
- **Select oracle**: CONTRACT §2's rules in 40 lines over the raw items and rows (content key = label contract's
  formula, `item_id` = retrain contract's canonical hash; both recomputed and proven on the real fixtures).
- **Compare oracle**: pass^k = C(c,k)/C(n,k), the pinned paired bootstrap (`Random(seed)`, B = 2000, indices 50 and
  1949). Selftest proves it reproduces the real held-out matrix's own `pass1_ci95` / `pass4_ci95` exactly.
- `diff` compares the solver's JSON to the oracle with tolerance 1e-9, strings exact, `"unverified"` vs a number is
  a difference.

## Checks → target → bar

| Check | Target | Bar and why |
|---|---|---|
| A0_cli_surface | CONTRACT §1 | no args / unknown sub / `select` without pair, with a missing export or tokenizer dir, with a torn `trajectories.jsonl`, with a label row lacking `final` / `card` without or with an empty fit dir / `serve-cmd` and `replay` without args / `compare` with a missing or non-JSON file → exit 2, nothing created, no traceback. |
| S1_select_real | **done tests 1–2**, real data | the real pair → 8 of 12 runs selected, 4 `not_kept`; every example's `input_ids` **equal** the oracle's prompt + completion ids, `loss_mask` is exactly `[0]*P + [1]*C`, `P`/`C` equal the gateway's `usage`, last id is `<|im_end|>`; sorted by `(item_id, turn_index)`; `examples_sha256`, counts, tokenizer and split hashes, `sources[0]` = 12/8/8/4; `--json` = `manifest.json`; nothing written under the fixtures. Exact ids, not lengths: a prompt rendered with a different template could keep the count and shift every token. |
| S2_leak_refusals | **done test 1** (held-out never enters) | kept rows for: a held-out run (task 10) saying `held_out`; the same run with item and row both claiming `train` (the split file decides); an unsplit run (task 64); a content-edited item whose row still carries the old `content_sha256`; a witness-flagged item; an item without a row; a row for a run the export does not hold; a manifest with a foreign `dataset_sha256`; a different split file → each **exit 1 naming the reason, no output dir**. A dropped held-out run and `--exclude-task 17,57` are rejections (exit 0) and absent from the data; the remaining examples still match the oracle. Refusal (not rejection) for a kept row that contradicts the data: a leak is corrupted input, never something to skip quietly. |
| S3_two_pairs | statement (T 0 and T 0.7 train fleets) | the real pair + a second pair of 8 cloned runs (new host/VM/run ids, same conversations) → 16 runs, `source` 0/1 on every example, ids still exact; the same item in both pairs → `duplicate_item`. |
| S4_determinism_caches | CONTRACT §2 | two runs (different `--out`, caches/TMPDIR/HOME emptied) byte-identical and holding only the two files; the sources copied to other paths → `examples.jsonl` byte-identical, manifest identical but for `sources[].export/labels/fleet` (recorded "as given"); every cache file bit-flipped and truncated → still the same data or exit 1; `labels.jsonl` replaced by a **same-size, same-mtime** variant (17_11 kept→false, 73_74 false→true) → the set changes accordingly; a `usage.prompt_tokens` edited in place → `token_parity`. mtime/size-keyed caches are the cheapest way to pass S1 and the way to train on stale data. |
| S5_parity_refusals | **done test 2** | `completion_tokens − 1` → `token_parity`, `usage` removed → `usage_missing`, exit 1; the tool-argument string re-serialised (`indent=2`, sorted keys, label re-keyed) → **selected with the original ids** (arguments are parsed before the template); one argument *value* changed (label re-keyed, `usage` untouched) → `token_parity` (selftest proves the oracle's counts move). The gateway's count is the only evidence of what the server saw; a trainer that silently re-counts can train on a prompt the model never received. |
| S6_policy_role | the wrong-policy incident (a train fleet run under the fleet's default role) | `--fleet` omitted or fewer than `--pair` → exit 2, nothing created; a second pair whose `fleet.json` declares `policy-qwen3.5-9b-think` → `policy_mismatch` naming that fleet and role, no dir; `--policy` naming another model with the real fleet → `policy_mismatch`; a fleet built on another split hash → `split_mismatch`; the good case records `policy` and `fleet_name`/`fleet_policy` per source. The export carries no model name, the fleet's declared role is the only carrier, and it must be checked at select time, by name, not found later through token parity. |
| C1_card_arithmetic | **done test 3** | synthetic fit dir (7 steps, 1234.5 s, 2 GPUs, $1.861, 178 runs): `gpu_hours = 1234.5 × 2 / 3600`, `cost_usd = gpu_hours × 1.861` within 1e-9, note `LoRA · Qwen/Qwen3.5-9B · 178 passed runs` verbatim as the first text line, loss per step, every hyperparameter printed, pure and byte-identical; steps 0 → empty per_step and nulls; a log with 4 lines for 5 steps, steps out of order, a NaN loss → exit 1. |
| V1_serve_cmd | **done test 4** (frozen stack) | argv carries `--served-model-name policy-qwen3.5-9b`, `--tool-call-parser qwen3_coder`, `--enable-auto-tool-choice`, `--default-chat-template-kwargs {"enable_thinking": false}`, `--max-model-len 32768`, `--enable-prefix-caching`, `--seed 300`, `--port`; two model dirs differ **only** in the path. `docs/versions.md` POC 11 flags; the merged checkpoint must be served exactly as the baseline was. |
| P1_replay_format | CONTRACT §7 | against a fake `/v1/completions`: 3 requests posted verbatim with `model policy-qwen3.5-9b`, `temperature 0`, `seed 300`, `skip_special_tokens false`, `max_tokens`; rows in order with `text`, `completion_tokens`, `finish_reason`, `status 200`; two replays byte-identical; a 500 → `status 500` row and exit 1. Special tokens kept so `<|im_end|>` is part of the compared text. |
| K1_compare_self | **done test 5** | held-out matrix vs itself: 44 paired, `diff` exactly `0.0`, `ci95` exactly `[0.0, 0.0]`, every m12/m13/cause diff 0, byte-identical, text mode shows `pass^1`/`pass^4`. |
| K2_compare_known_diff | **done test 5** (M11 Δ with interval, M12, M13, causes) | synthetic candidate: +2/+1/+3/+1/+1/+2/+1/+1 successes on 8 tasks, −1/−2/−1 on 3, two tasks removed, median −12 s, `agent_timeout: 3`, 3 trials/task → equals the oracle in every field (paired diff, pinned bootstrap, 42 paired + 2 `a_only`, warning on trials, m13 −12, cause +3); `--seed 11` changes the interval (the paired diffs are quarters, so many seeds tie on the percentiles — 7 does, proven in selftest) and still equals the oracle; `--out` = stdout; held-out vs train (real matrices) → exit 1. |
| K3_compare_refusals | CONTRACT §8 | a `pass4` edited against its own `n`/`c` → exit 1 naming it; another simulator, another split part, no paired task → exit 1; no `m11.tasks` → exit 2; a new policy name compares fine. A comparison built on a tampered or stale matrix is worth nothing. |
| H1_select_on_host | CONTRACT §2 | `select` on the GPU host with the **model directory** as tokenizer gives the same `examples_sha256` as the local run with the fixture tokenizer: the served tokenizer and the fixture are the same, and the output is host-independent. |
| H2_fit_identity | **done test 4**, **done test 3** | `fit --steps 0`: `b_all_zero true`, 0 log lines, adapter files, `card.json` = `train card` recomputation, cost = wall × count / 3600 × $1.861, note `… · 8 passed runs`, data hash = the data dir's. |
| H3_fit_steps | **done test 3** | `fit --steps N` (default 2): N log lines `1..N`, finite positive losses, loss tokens > 0, `b_all_zero false`, wall and epochs > 0, card consistent. Tiny budget: minutes, not a training run. |
| H4_merge | **done test 4** | both merges: tokenizer, template, generation config byte-identical to the base (`cmp`), `config.json` architectures equal, safetensors present, no adapter files, `merge.json` hashes correct. |
| H5_identity_serving | **done test 4** | base served by `serve-cmd` → replay 6 real POC 11 prompts (first and last policy turn of 3 kept items, 64 tokens) → stop → identity merge served the same way → replay → `text`, `completion_tokens`, `finish_reason` **identical on every prompt**, all non-empty; then the trained merge serves and answers every request 200. Token identity at T 0 on the frozen stack is the only black-box proof that merge + serve did not change the model. |

## Thresholds in one place (`TH` in `run.py`)

`select_s 300` (one select over 12 items; the first call may bootstrap the solver's environment), `tok_oracle_s 900`
(first `uv` install of transformers), `vllm_ready_s 900` (9B bf16 load on an L40S is ~2–4 min), `fit_s 1800`,
`merge_s 1200`, `replay_max_tokens 64`, `replay_prompts 6`, `price 1.861` (the POC 11 g6e.xlarge price,
`data/poc11/prices.json`), `tol 1e-9`.
