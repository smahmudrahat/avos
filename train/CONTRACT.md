# POC 12 train contract

The black-box interface `train/tests/run.py` exercises. Everything below is required; nothing about internals is.
Serves: the six done tests of `train/STATEMENT.md`, claim 3 (a ~9B open model, LoRA-retrained only on its own
passed runs, beats its baseline on held-out tasks), M11–M14 (second loop) and the comparison against
`data/poc11/README.md`. Written before and without the implementation; revised once after the solver's dispute
round (§2 `--fleet`/`--policy` and `policy_mismatch`, the re-serialised-arguments clarification, the determinism
wording) — every revision is marked **(rev. 1)**.

Inherited constraints: the POC 8 export (`retrain/CONTRACT.md` §5–§7) and the POC 9 labels (`label/CONTRACT.md`
§5) are inputs and are **never modified**; the gateway's `usage` on every policy message is the only authority on
what the model saw and said; the serving stack is frozen (`docs/versions.md`, "POC 11 run configuration").

Every CLI: `train/bin/train <sub> ...` (an executable; Python 3.12; its shebang and dependency bootstrap are the
solver's, e.g. `uv run --script`), `python3 fleet/bin/fleet compare ...` (standard library only, like the rest of
`fleet`). Exit `0` success; `1` the input fails a check named on stderr (and in `error` on stdout with `--json`);
`2` usage or unreadable input. Never a traceback. Machine output on **stdout** as one JSON object with `--json`.
No network except `replay` (to the URL given) and `fit`/`merge` reading a local model directory. No secret, host
name, wall-clock or absolute path of the tester's choosing in any output file except paths recorded "as given".

## 1. Files

| Path | Role |
|---|---|
| `train/bin/train` | `select`, `fit`, `card`, `merge`, `serve-cmd`, `replay` (§2–§7) |
| `fleet/bin/fleet compare` | two matrices → paired comparison (§8; appended to the fleet contract by this file) |
| `train/tests/` | this suite; `fixtures/` is read-only for the solver |

## 2. `train select` — the training set (done test 1, done test 2)

```
train select --out DIR --pair EXPORT_DIR LABELS_DIR [--pair EXPORT_DIR LABELS_DIR ...]
             --fleet FLEET_JSON [--fleet FLEET_JSON ...] --policy NAME --tokenizer TOK_DIR
             [--split FILE] [--exclude-task T[,T...]] [--json]
```

`EXPORT_DIR` is a `retrain export` directory (`trajectories.jsonl`, `manifest.json`); `LABELS_DIR` holds a `label
run`'s `labels.jsonl`. Pairs are the POC 11 train fleet and the sampled train fleet (any number ≥ 1). `TOK_DIR` is
a Hugging Face tokenizer directory: `tokenizer.json`, `tokenizer_config.json`, `chat_template.jinja`
(`config.json` may be present); it must be the served model's. `--split` defaults to
`data/splits/retail_user_disjoint_v1.json`.

**Provenance (rev. 1).** `--fleet` is given once per `--pair`, in pair order: the `fleet.json` of the fleet that
produced that export (`fleet/CONTRACT.md` §2, e.g. `data/poc11/p11-train/fleet.json`; the export carries no model
name, the fleet's declared `roles.policy` is the authority on which model answered). `--policy` is the served name
of the model being trained, the one `serve-cmd` emits (`policy-qwen3.5-9b`). Both are **required**: a fleet run
with the wrong policy role (the fleet's default role, served elsewhere) must be refused here, by name, not
discovered later through `token_parity`. Exit 2 when either is missing, when the `--fleet` count differs from the
`--pair` count, or when a `fleet.json` is unreadable or lacks `fleet`, `roles.policy` or `split.sha256`.

**Selection.** An item of pair `i` is **selected** iff all of:

1. its `labels.jsonl` row (joined on `run`) has `final.kept == true`;
2. `task_id` is in the split file's `train` list, the item's `split == "train"` and the row's `split == "train"`;
3. `witness.agrees == true` and `trace_complete.status == "complete"`;
4. `task_id` is not in `--exclude-task`;
5. it has ≥ 1 policy turn (a message with `train == true`).

An item that fails 1, 4 or 5 is **rejected** with reason `not_kept`, `excluded_task` or `no_policy_turn` (exit 0).
An item with `final.kept == true` that fails 2 or 3 is a contradiction between the label and the data and the whole
command is **refused**: exit 1, `DIR` not created, stderr naming the run and the reason (`kept_not_train`,
`kept_witness_disagrees`, `kept_trace_incomplete`). The same refusal for: a row whose `content_sha256` differs from
the item's content key (`label/CONTRACT.md` §0 formula) → `content_sha256_mismatch`; a row whose `item_id` differs
from the item's → `item_id_mismatch`; a row whose `run` is not in its pair's export → `label_run_unknown`; an
item without a row → `label_missing` (every exported run must have a verdict); an export whose
`manifest.dataset_sha256` differs from the SHA-256 of the item_ids joined by `\n` in file order →
`dataset_sha256_mismatch`; an export whose `manifest.split_file.sha256` differs from the SHA-256 of `--split` →
`split_mismatch`; a pair's `fleet.json` whose `split.sha256` differs from the SHA-256 of `--split` →
`split_mismatch`; a pair's `fleet.json` whose `roles.policy` differs from `--policy` → `policy_mismatch`, stderr
naming the fleet (`fleet`), its role and `--policy` (rev. 1); the same `item_id` selected from two pairs →
`duplicate_item`; a policy turn without `usage` → `usage_missing`; a policy turn whose rendering does not reproduce
the gateway's counts (below) → `token_parity`.
Items that are rejected (not refused) never stop a run. A held-out or unsplit run can therefore never be in `DIR`
whatever any label says; it can only be rejected or cause a refusal.

**Rendering (done test 2).** For every policy turn at index `k` of a selected item, with `msgs` = the item's
messages reduced to `role`, `content` (when not null), `tool_calls` (with `function.arguments` parsed from the JSON
string to an object, ids kept) and `tool_call_id`:

- `prompt_ids` = the token ids of `apply_chat_template(msgs[:k], tools=item.tools, add_generation_prompt=True,
  enable_thinking=False)` with `TOK_DIR`'s template and tokenizer, no BOS added — exactly what vLLM rendered;
  `len(prompt_ids) == messages[k].usage.prompt_tokens`.
- `completion_ids` = the token ids of the text that `apply_chat_template(msgs[:k+1], tools=…,
  add_generation_prompt=False, enable_thinking=False)` adds after that prompt, without the trailing `\n`, so it
  ends with `<|im_end|>`; `len(completion_ids) == messages[k].usage.completion_tokens` and
  `completion_ids[-1] == id("<|im_end|>")`.

The POC 11 export satisfies both equalities on every one of its policy turns (the tester measured 958/958 on 60
items); a turn that does not is `token_parity` (refused), never silently padded, truncated or re-counted. Because
`function.arguments` is parsed before the template, an argument string that is only re-serialised (indent, key
order) renders the same tokens and is selected with the original ids; a changed argument *value* renders other
tokens and is `token_parity` (rev. 1).

**Output `DIR`** (created; written atomically — on failure the previous content stays, on refusal nothing exists):

`DIR/examples.jsonl`, one line per policy turn of every selected item, sorted by `(item_id, turn_index)`,
`json.dumps(ex, sort_keys=True, separators=(",", ":"))`:

```
{"item_id", "run", "task_id", "source": <pair index, 0-based>, "turn_index": k,
 "prompt_tokens": P, "completion_tokens": C,
 "input_ids": prompt_ids + completion_ids,          ints, length P + C
 "loss_mask": [0] * P + [1] * C}                     the only positions that take loss
```

`DIR/manifest.json` (`json.dumps(..., indent=1, sort_keys=True)`):

```
{"format": "avos-train-data/1",
 "policy": <--policy>,                                                                                 (rev. 1)
 "tokenizer": {"path": <as given>, "tokenizer_sha256": sha(tokenizer.json), "chat_template_sha256": sha(chat_template.jinja),
               "im_end_id": int},
 "split_file": {"path": <as given>, "sha256"},
 "excluded_tasks": [sorted strings],
 "sources": [{"export": <as given>, "labels": <as given>, "dataset_sha256": <export manifest's>,
              "fleet": <as given>, "fleet_name": <fleet.json's fleet>, "fleet_policy": <fleet.json's roles.policy>,   (rev. 1)
              "labels_sha256": sha(labels.jsonl), "items": n, "kept": n, "selected": n, "rejected": n}],   pair order
 "selected": [{"item_id", "run", "task_id", "source", "examples", "prompt_tokens", "completion_tokens"}],  sorted by (item_id)
 "rejected": [{"run", "source", "reason"}],                                                              sorted by (source, run)
 "counts": {"runs": K, "tasks": distinct task ids, "examples": N, "prompt_tokens", "completion_tokens", "tokens"},
 "examples_sha256": SHA-256 of the bytes of examples.jsonl}
```

`K` (runs) is what the card's note calls `<K> passed runs`. With `--json`, stdout is the manifest.

**Determinism.** The two files are byte-identical across re-runs with the same arguments and a different `--out`,
after `AVOS_TRAIN_CACHE`, `TMPDIR`, `HOME` and `XDG_*_HOME` are emptied. With the same sources copied to other
paths, `examples.jsonl` is byte-identical and `manifest.json` differs only in the "as given" path strings
(`sources[].export/labels/fleet`; `tokenizer.path`, `split_file.path` when those move) (rev. 1). Nothing is
written under any `EXPORT_DIR`, `LABELS_DIR`, `FLEET_JSON`'s directory or `TOK_DIR`.

**Caches.** Derived state may live only under `AVOS_TRAIN_CACHE` (default under `$TMPDIR`) or `--out`. It is bound
to the content of the sources (`retrain/CONTRACT.md` §11): a source replaced by a different file of the same size
and mtime, or edited in place, changes the answer; a cache that is truncated, bit-flipped, swapped or deleted leads
to a rebuild or exit 1 naming it — never a stale or wrong `DIR` with exit 0.

## 3. `train fit` — the LoRA run (done test 3)

```
train fit --data DIR --model MODEL_DIR --out FITDIR --gpu-usd-per-h P [--gpu-count G]
          (--steps N | --epochs E) [--lr F] [--rank R] [--alpha A] [--dropout D] [--batch-tokens T] [--max-len L]
          [--seed S] [--json]
```

Trains a LoRA adapter on `DIR/examples.jsonl`: the model is conditioned on `input_ids` and loss is taken on
exactly the positions where `loss_mask == 1` (the completion through `<|im_end|>`), nothing else; sequences are
never re-tokenized from text. `--steps 0` is allowed and performs no optimizer step: the adapter is then the
**identity adapter** (every LoRA B matrix all zeros), used by done test 4. `FITDIR` (created) holds:

| File | Content |
|---|---|
| `fit.json` | raw facts, below |
| `log.jsonl` | exactly `steps` lines, `{"step": 1…steps, "loss": finite float, "lr": float, "tokens": int (loss positions in the step), "elapsed_s": float}` in step order |
| `adapter/` | the PEFT adapter (`adapter_config.json`, `adapter_model.safetensors`), the input of `merge` |
| `card.json`, `CARD.md` | `train card --fit FITDIR` (§4) run at the end |

```
fit.json = {"format": "avos-train-fit/1", "base_model": <as given>, "base_model_name": "Qwen/Qwen3.5-9B" (config's or as given),
            "data": {"path": <as given>, "examples_sha256", "runs": K, "examples": N, "tokens": n},
            "hyperparameters": {"lr", "rank", "alpha", "dropout", "target_modules": [...], "batch_tokens", "max_len", "seed",
                                "precision", "optimizer", "schedule", "epochs": E|null, "steps_requested": N|null},
            "steps": int, "epochs": float (examples seen ÷ N),
            "started": TS, "ended": TS, "wall_s": float (ended − started),
            "gpu": {"name": str, "count": G, "usd_per_h": P},
            "adapter": {"path": "adapter", "b_all_zero": bool, "trainable_params": int}}
```

`TS` = `YYYY-MM-DDTHH:MM:SS.mmmZ` UTC. `b_all_zero` is true iff every LoRA B tensor is all zeros (true after
`--steps 0`, false after ≥ 1 step with a non-zero gradient). Exit 1 when `DIR/manifest.json` is missing or its
`examples_sha256` does not match `examples.jsonl`; 2 on usage or an unreadable model.

## 4. `train card` — the record (done test 3)

```
train card --fit FITDIR [--json]
```

Pure and deterministic: reads only `fit.json` and `log.jsonl`, writes nothing, byte-identical stdout across runs.
Prints `CARD.md` text, or with `--json`:

```
{"format": "avos-train-card/1",
 "note": "LoRA · <base_model_name> · <K> passed runs",
 "base_model": <fit.json base_model_name>,
 "data": {"examples_sha256", "runs": K, "examples": N, "tokens": n},
 "hyperparameters": <fit.json hyperparameters>,
 "steps", "epochs",
 "loss": {"per_step": [every log line's loss, in order], "first", "last", "min"} (nulls when steps == 0),
 "wall_s", "gpu_count", "gpu_hours": wall_s × gpu_count / 3600, "gpu_usd_per_h": P, "cost_usd": gpu_hours × P}
```

Tolerance 1e-9 on the arithmetic. `CARD.md`'s first line is the `note` verbatim; it also prints `steps`, `epochs`,
every hyperparameter, `runs`, `examples_sha256`, `wall_s`, `gpu_hours` and `cost_usd` with their values. Exit 1
(`error`) when `log.jsonl` has a different line count than `steps`, a step out of sequence, or a non-finite
loss; 2 when `fit.json` is missing or not JSON.

## 5. `train merge` — the served checkpoint (done test 4)

```
train merge --model MODEL_DIR --adapter FITDIR/adapter --out CKPT [--json]
```

Writes a merged checkpoint the frozen vLLM command serves as `policy-qwen3.5-9b`: `config.json` with the base's
`architectures`, the merged weights (`*.safetensors` + index), and **byte-identical copies** of the base's
`tokenizer.json`, `tokenizer_config.json`, `chat_template.jinja`, `generation_config.json`, `vocab.json` and
`merges.txt` where the base has them. No `adapter_config.json` / `adapter_model.*` inside `CKPT` (fully merged).
`CKPT/merge.json`:

```
{"format": "avos-train-merge/1", "base_model": <as given>, "adapter": <as given>,
 "b_all_zero": <adapter fit.json's>, "tokenizer_sha256": sha(CKPT/tokenizer.json), "chat_template_sha256": sha(CKPT/chat_template.jinja)}
```

Merging the identity adapter must give a checkpoint whose completions are token-identical to the base's (§7, H5).

## 6. `train serve-cmd`

```
train serve-cmd --model DIR --port PORT [--served-name policy-qwen3.5-9b] [--json]
```

Pure: prints the frozen serving command (`docs/versions.md`, POC 11 run configuration) for `DIR`, nothing else.
JSON `{"argv": [...]}`; text: the argv shell-quoted on one line. The argv contains `--served-model-name
policy-qwen3.5-9b`, `--tool-call-parser qwen3_coder`, `--enable-auto-tool-choice`, `--default-chat-template-kwargs`
followed by a JSON object with `"enable_thinking": false`, `--max-model-len 32768`, `--enable-prefix-caching`,
`--seed 300`, `--port PORT` and `DIR`. The argv for two different `DIR`s differs **only** in `DIR`.

## 7. `train replay`

```
train replay --url http://HOST:PORT/v1 --requests FILE --out FILE [--json]
```

`--requests`: JSONL `{"index": int, "prompt": <rendered prompt text>, "max_tokens": int}`. Each is sent in order to
`POST {url}/completions` as `{"model": "policy-qwen3.5-9b", "prompt", "max_tokens", "temperature": 0, "seed": 300,
"skip_special_tokens": false}`; the response's `choices[0].text`, `usage.completion_tokens` and `finish_reason`
are recorded. Output JSONL `{"index", "text", "completion_tokens", "finish_reason", "status": 200}` in request
order; a non-200 answer is recorded as `{"index", "status", "error"}` and the command exits 1. With `--json`, stdout
`{"requests", "ok", "failed"}`. Two replays of the same file against servers that generate the same tokens give
byte-identical output files.

**Identity check (done test 4, host check H5).** With base `B` and `I = merge(B, fit --steps 0)`: serve `B` with
`serve-cmd`, replay the suite's recorded prompts (real POC 11 prompts rendered as in §2, `max_tokens` 64); stop it;
serve `I` the same way; replay again. Every pair of results has identical `text`, `completion_tokens` and
`finish_reason`. Then the trained merge `T = merge(B, fit --steps N)` must serve and answer every request with
status 200 (its tokens may differ).

## 8. `fleet compare` — two matrices, paired by task (done test 5)

Appended to `fleet/CONTRACT.md` by this file; same conventions as `fleet matrix`.

```
fleet compare --a MATRIX_A.json --b MATRIX_B.json [--seed 300] [--json] [--out FILE]
```

`A` is the baseline, `B` the candidate; both are `fleet matrix --json` outputs (`fleet/CONTRACT.md` §4). Pure,
deterministic (byte-identical stdout for the same inputs), no other input. Exit 2 when a file is missing, not JSON,
or lacks `inputs`, `m11.tasks`, `m12`, `m13`. Exit 1 (`error` naming the field) when: `inputs.split.sha256` or
`inputs.split.part` differ; `inputs.roles.simulator` or `inputs.roles.nl_judge` differ (the policy may); a task's
recorded `pass1`/`pass4` differs by more than 1e-9 from `C(c,k)/C(n,k)` of its own `n`, `c` (a matrix whose parts
and values disagree is tampered or stale, never compared); no task is paired. Different `trials_per_task` or
`concurrency` are not errors: they are named in `warnings`.

```
{"format": "avos-fleet-compare/1",
 "a": {"fleet", "policy", "trials_per_task", "concurrency", "tasks": n}, "b": {same},
 "paired_tasks": [ids in A's task order], "a_only": [sorted], "b_only": [sorted],
 "tasks": [{"task", "a": {"n", "c", "pass1", "pass4"}, "b": {same}, "d_pass1": b − a | null, "d_pass4": b − a | null}],   paired, A's order
 "pass1": {"a", "b", "diff", "ci95": [lo, hi], "tasks": T},        over paired tasks where both sides are non-null
 "pass4": {same},
 "m12": {"policy_usd_per_task": {"a", "b", "diff"}, "policy_usd_per_successful_task": {...},
         "simulator_usd_per_task": {...}, "nl_judge_usd_per_task": {...}},
 "m13": {"median_s": {"a", "b", "diff"}, "p90_s": {...}, "worst_s": {...}},
 "causes": {"a": {cause: n}, "b": {cause: n}, "diff": {cause: b − a over the union, sorted keys}},
 "by_class": {"agent": {"a", "b", "diff"}, "infra": {"a", "b", "diff"}},
 "bootstrap": {"seed": S, "B": 2000, "rule": "paired percentile bootstrap over tasks"},
 "warnings": [sorted strings]}
```

- Per task, `pass_k = C(c,k)/C(n,k)` recomputed from `n` and `c` (`null` when `n < k`). `pass1.a` = mean of A's
  values over the `T` paired tasks where both sides are non-null; `b` likewise; `diff = mean(b_i − a_i)`.
- `ci95`, **exactly**: `rng = random.Random(seed)`, `d = [b_i − a_i]` in paired order, `T = len(d)`, `B = 2000`,
  `means = sorted(mean(d[rng.randrange(T)] for _ in range(T)) for _ in range(B))`, `[means[50], means[1949]]`;
  `"unverified"` when `T < 2`. A fleet compared with itself gives `diff` exactly `0.0` and `ci95` exactly
  `[0.0, 0.0]`.
- `m12` / `m13` values are read from each matrix (`m12.<role>.usd_per_task`, `m12.policy.usd_per_successful_task`,
  `m13.median_s` …); `diff = b − a`; any side `"unverified"` → `diff` `"unverified"`.
- `causes` from `m11.causes` and `by_class` from `m11.by_class`; a cause absent on one side counts 0 there.
- `--out FILE` writes the JSON there too. Text mode prints one block per metric with the parts; numbers in JSON are
  JSON numbers, `"unverified"` the string.

## 9. Environment

| Variable | Default | Meaning |
|---|---|---|
| `AVOS_TRAIN_CACHE` | under `$TMPDIR` | The only place derived state of `select` may live, besides `--out` |

The tests point `TMPDIR`, `HOME`, `XDG_*_HOME` and `AVOS_TRAIN_CACHE` at private directories and tamper with
everything written there.

## 10. What may not change

`python3 fleet/tests/run.py --quick`, `retrain/tests/run.py --quick` and `label/tests/run.py --quick` still pass;
`train` never writes under an export, labels, tokenizer or model directory; `fleet matrix` output is unchanged.
