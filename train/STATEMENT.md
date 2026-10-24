# POC 12 statement: retrain and re-run

Retrain the open-weight policy model on its own labelled passed runs from the train split, run the benchmark again
with the retrained model, collect traces again, and compare with the POC 11 baseline (`data/poc11/README.md`).
Contract: a ~9B open model, LoRA-retrained only on its own passed runs, beats its baseline on held-out tasks.

## Inputs that exist
- `retrain export` output directories (`retrain/CONTRACT.md` §5–§7): `trajectories.jsonl` items whose policy
  messages carry `train: true` and `usage: {prompt_tokens, completion_tokens}` as counted by the gateway when the
  policy produced them.
- `label run` output directories (`label/CONTRACT.md` §5): `labels.jsonl` rows with `final.kept`, `split`, and the
  item's `content_sha256`.
- Two such pairs for POC 12: the POC 11 train fleet (policy at temperature 0) and a second train fleet with the same
  configuration except the policy sampled at temperature 0.7, top_p 0.8 (`fleet run --agent-temperature
  --agent-top-p`, recorded as `agent_sampling` in `fleet.json`).
- The policy model `Qwen/Qwen3.5-9B` (Hugging Face), served by vLLM 0.32.0 with the command in `docs/versions.md`
  ("POC 11 run configuration"), chat template kwargs `enable_thinking: false`, tool parser `qwen3_coder`.

## Done tests
1. **Only own passed train runs.** The training set is exactly the items with `final.kept == true`, `split ==
   "train"`, and a witness that agrees, from the given label outputs, minus any task the caller excludes. A held-out
   or unsplit run can never enter it; a label row whose `content_sha256` does not match the item is refused.
2. **Train on what the model saw.** For every policy turn, the token prompt the trainer conditions on equals the
   prompt the server rendered at generation time (its length equals the gateway's `prompt_tokens`), and the target
   is exactly the policy's completion through the end-of-turn token (its length equals `completion_tokens`). Loss is
   computed only on those completion tokens.
3. **A record of the training.** Loss per optimizer step, total steps, epochs, the hyperparameters, the training
   data's hash and run count, wall time and GPU-hours with their USD cost, and a one-line note
   `LoRA · <base model> · <K> passed runs`.
4. **Serves on the frozen stack.** The retrained model is a merged checkpoint that the frozen vLLM command serves
   under the same served name, with the same tokenizer and chat template. An adapter that changes nothing (all
   LoRA B weights zero), merged the same way, must give token-identical completions to the base model on replayed
   recorded requests at temperature 0.
5. **Compare.** Held-out 44 tasks × 4 with the retrained model through the same fleet configuration as POC 11; a
   comparison of two fleets' matrices: paired by task, differences of pass^1 and pass^4 with bootstrap intervals
   over tasks, and the differences of M12 (policy cost per trial), M13 and failure causes.
6. **Collapse check.** Diversity statistics (`retrain stats`) of the retrained model's runs next to the baseline's.

## Metrics touched
M11, M12, M13, M14 (second loop: kept-run fraction with the retrained model on train, if run), M10 (second loop).
