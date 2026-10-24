# train

LoRA-retrain the policy model on its own kept train runs, merge the adapter into a checkpoint the frozen vLLM
command serves, and compare two fleets (POC 12). Interface: `train/CONTRACT.md`; acceptance suite:
`train/tests/` (`just train-test --quick`, or `--host GPU --model-path DIR` for the GPU checks).

**Environment.** `train/bin/train` starts under any `python3`. `select` needs Python 3.12 with transformers 5 and
`fit`/`merge` need torch, transformers 5, peft and safetensors; when the running interpreter lacks them the command
re-executes under `$AVOS_TRAIN_PYTHON`, then `~/train/bin/python`, then (for `select` only) `uv run` with
`transformers==5.19.0`, using the user's own uv cache. `serve-cmd` resolves vLLM from `$AVOS_VLLM`, `PATH`, then
`~/vllm/bin/vllm`.

**`select`** builds the training set from `retrain export` / `label run` pairs: an item is selected only when its
label row says kept, the pair's `fleet.json` (one `--fleet` per `--pair`) declares `--policy` as its policy
role and was run on the same split file, the split file, the item and the row all say train, the witness agrees and the trace is
complete; a kept row that contradicts the data (held-out, unsplit, flagged, incomplete, wrong content key or item id,
a foreign dataset hash or split file, a duplicate, missing usage) refuses the whole command and writes nothing.
Every policy turn is rendered with the given tokenizer and chat template exactly as vLLM did (tool-call arguments
parsed to objects, tools passed, thinking off) and must reproduce the gateway's `prompt_tokens` and
`completion_tokens`, else `token_parity`. There is no cache; every run recomputes from the sources.

```
train/bin/train select --out DATA --pair EXPORT LABELS --fleet FLEET.json [--pair EXPORT2 LABELS2 --fleet FLEET2.json] \
    --policy policy-qwen3.5-9b --tokenizer MODEL_DIR [--exclude-task 17,57]
```

**`fit`** trains a LoRA adapter (r 16, alpha 32, dropout 0, every linear layer of the language model, none in the
vision tower or MTP head) with AdamW at 5e-5, cosine schedule with 5 % warmup, 2 epochs, 16 sequences per optimizer
step, seed 300, bf16, non-reentrant gradient checkpointing. Turns of one item are packed into one sequence when a
turn's whole prompt + completion is a token prefix of a longer turn's, so every loss position conditions on exactly
the tokens the model saw; logits are computed only at completion positions, in chunks. Lengths are padded at the end
to multiples of `--pad-to` to bound kernel autotuning. Every optimizer step is a line of `log.jsonl`; `fit.json`,
`card.json` and `CARD.md` record the hyperparameters, data hash, wall time and cost. `--steps 0` writes the
identity adapter (all LoRA B zero).

```
train/bin/train fit --data DATA --model MODEL_DIR --out FIT --gpu-usd-per-h 1.861 [--epochs 2 | --steps N]
```

**`card`** prints the record of a fit (`--json` for the machine form) from `fit.json` and `log.jsonl` only.

**`merge`** adds `alpha/r · B·A` to each targeted base tensor directly in the base checkpoint's safetensors shards
(same shard names, same tensor keys, the MTP head kept, the index and every other file copied byte for byte), so the
merged directory loads exactly like the base. Merging the identity adapter reproduces the base weights bit for bit.

```
train/bin/train merge --model MODEL_DIR --adapter FIT/adapter --out CKPT
```

**`serve-cmd`** prints the frozen POC 11 vLLM command for a model directory, and **`replay`** sends recorded prompts
to `/v1/completions` at temperature 0, seed 300, special tokens kept, and records the answers; replaying against the
base and against the identity merge must give identical tokens.

```
$(train/bin/train serve-cmd --model CKPT --port 8000)
train/bin/train replay --url http://127.0.0.1:8000/v1 --requests REQ.jsonl --out OUT.jsonl
```

**`fleet compare`** (in `fleet/bin/fleet`) pairs two `fleet matrix --json` outputs by task and reports the
differences of pass^1 and pass^4 with a paired percentile bootstrap over tasks, and of M12, M13 and failure causes;
matrices with a different split, simulator or judge, or whose per-task pass values disagree with their own counts,
are refused.

```
python3 fleet/bin/fleet compare --a data/poc11/p11-heldout/matrix.json --b NEW/matrix.json
```
