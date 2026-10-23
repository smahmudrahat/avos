"""Dry tokenization of an export with TRL's SFTTrainer (done test 2). Runs inside the isolated uv environment:

  PYTHONNOUSERSITE=1 uv run --isolated --no-project --python 3.12 --with trl --with transformers --with datasets \
      --with torch --with accelerate python -I retrain/tests/trl_check.py DATASET_DIR OUT_JSON

Two passes: `trajectories.jsonl` in the conversational `messages` format (must be accepted as-is, tools column
included); `sft.jsonl` in the prompt-completion format with completion-only loss (the default), where this script
records, per example, exactly which text ends up with labels != -100. run.py compares that text with the policy
turns it computed independently from the export. A tiny randomly initialised model with the Qwen3 tokenizer and
chat template is used: the check is about tokens and masks, not weights.
"""
import json
import os
import sys
import tempfile
import traceback

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("WANDB_DISABLED", "true")

TOK = "Qwen/Qwen3-0.6B"


def main(ds_dir, out_path):
    rep = {"ok": False, "versions": {}, "trajectories": {}, "sft": {}, "errors": []}
    try:
        import torch
        import trl, transformers, datasets
        from datasets import load_dataset
        from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
        from trl import SFTConfig, SFTTrainer
        rep["versions"] = {"trl": trl.__version__, "transformers": transformers.__version__, "datasets": datasets.__version__}
        tok = AutoTokenizer.from_pretrained(TOK)
        cfg = AutoConfig.from_pretrained(TOK)
        for k, v in {"num_hidden_layers": 1, "hidden_size": 64, "intermediate_size": 128, "num_attention_heads": 2,
                     "num_key_value_heads": 1, "head_dim": 32}.items():
            setattr(cfg, k, v)
        model = AutoModelForCausalLM.from_config(cfg)
        out = tempfile.mkdtemp()

        def trainer(path, **kw):
            ds = load_dataset("json", data_files=path, split="train")
            args = SFTConfig(output_dir=out, per_device_train_batch_size=1, max_length=32768, report_to=[], **kw)
            return ds, SFTTrainer(model=model, args=args, train_dataset=ds, processing_class=tok)

        traj = os.path.join(ds_dir, "trajectories.jsonl")
        n_lines = sum(1 for l in open(traj, encoding="utf-8") if l.strip())
        try:
            ds, tr = trainer(traj)
            rep["trajectories"] = {"lines": n_lines, "loaded": len(ds), "tokenized": len(tr.train_dataset),
                                   "columns": tr.train_dataset.column_names}
        except Exception as e:
            rep["trajectories"] = {"lines": n_lines, "error": f"{type(e).__name__}: {e}"[:600]}
            rep["errors"].append("trajectories.jsonl rejected by SFTTrainer")

        sft = os.path.join(ds_dir, "sft.jsonl")
        raw = [json.loads(l) for l in open(sft, encoding="utf-8") if l.strip()]
        if not raw:
            rep["sft"] = {"lines": 0, "examples": []}
        else:
            ds, tr = trainer(sft)
            tds = tr.train_dataset
            examples = []
            for i in range(len(tds)):
                ex = tds[i]
                batch = tr.data_collator([ex])
                ids, labels = batch["input_ids"][0].tolist(), batch["labels"][0].tolist()
                trained = tok.decode([t for t, l in zip(ids, labels) if l != -100], skip_special_tokens=False)
                full = tok.decode(ids, skip_special_tokens=False)
                examples.append({"item_id": ex.get("item_id"), "turn_index": ex.get("turn_index"), "trained": trained,
                                 "n_trained": sum(1 for l in labels if l != -100), "n_tokens": len(ids), "full_len": len(full)})
            rep["sft"] = {"lines": len(raw), "loaded": len(ds), "tokenized": len(tds), "examples": examples,
                          "assistant_only_loss_supported": hasattr(SFTConfig, "assistant_only_loss")}
            if len(tds) != len(raw):
                rep["errors"].append(f"sft.jsonl: {len(raw)} lines but {len(tds)} tokenized examples (fully masked examples are dropped silently)")
        rep["ok"] = not rep["errors"]
    except Exception as e:
        rep["errors"].append(f"{type(e).__name__}: {e}"[:600])
        rep["traceback"] = traceback.format_exc()[-3000:]
    json.dump(rep, open(out_path, "w"), indent=1)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
