# POC 8 acceptance suite

Independent, black-box checks of `retrain/CONTRACT.md`. Written before and without the implementation; the only
things it touches are `retrain/bin/retrain`, the POC 6/7 tools as oracles (`trace/bin/*`, `collect/bin/avos`),
and raw fixtures.

```
python3 retrain/tests/run.py --selftest   # the suite's own oracles on the fixtures (33 items, ~15 s, uv for the TRL harness)
python3 retrain/tests/run.py --quick      # everything but the scale checks (~2 min once implemented; TRL needs uv + network once)
python3 retrain/tests/run.py              # + 700-run scale and store-size independence
python3 retrain/tests/run.py --host       # + live checks on avos-dev over ssh (repo pushed to ~/poc; ssh control socket closed in finally)
python3 retrain/tests/run.py --only D2    # one prefix
```

One line per check: `PASS|FAIL|SKIP <id> [<target>] <measured vs bar>`. Exit 0 only if every executed check passed.
A missing CLI reads `FAIL … not implemented: retrain/bin/retrain`, never a traceback.

## Fixtures (`fixtures/`, benchmark data only, no secrets)

| Path | What | From |
|---|---|---|
| `host/store/` | POC 7 streams, counters and `runs/` entries of the five τ² runs `run_retail-0_11…0_15` and the tree VM `vm-01-6cc405` (support → B1, B2, B3; 10 gateway tool calls, a halt/resume) | `/var/lib/avos/trace` on avos-dev, 2026-10-22 |
| `host/results/` | `job.json`/`result.json`(/`events.jsonl` copy) of those five runs, `run_retail-0_10` (legacy, vm-01-c060a5), `run_retail-2_1` (reward_info null), `run_retail-2_0` (no job.json), `run_coding-slugify_2` (mini-swe-agent, legacy) | `/var/lib/avos/results` |
| `host/ledger/legacy_slice.jsonl` | the POC 1–6 ledger rows of vm-01-c060a5 and vm-01-fc4298 | `/var/lib/avos/events.jsonl` |
| `tau2_retail_tools.json` | the 16 retail tool schemas (`Tool.openai_schema`) | pinned τ² `96a3ef9` in `~/tau2-bench/.venv` |
| `tau2_llm_agent_prompt_template.json` | `SYSTEM_PROMPT` and `AGENT_INSTRUCTION` of τ²'s `llm_agent` | same |

`data/poc2/run_*` (witness-only runs) and `data/poc6/run_retail-0_9` (legacy run with its ledger slice) join the
results root at run time. From these the suite clones POC 6-valid runs (ids re-minted from `(ts, writer, seq)`,
chains rebuilt, `validate`/`verify` clean, `avos completeness` answers as intended): a held-out task
(`run_retail-10_0`), an exact duplicate (`0_80`), a null reward (`0_90`), a tampered witness (`0_91`, one
`prompt_tokens` off by one), truncated counters (`0_92`, M8 incomplete), no counters (`0_93`, unverified), a stale
`events.jsonl` copy from another run (`0_94`); `run_retail-0_15` loses its copy.

## Checks → target → bar

| Check | Target | Bar and why |
|---|---|---|
| A0_cli_surface | CONTRACT §1/§3 | no args / unknown subcommand exit 2, no traceback; `runs` lists the 20 runs with evidence `store`/`ledger`/`witness` and `exportable`. |
| D1_viewer_judgeable, D1b_viewer_html | **done test 1** | On a failed (`0_12`) and a passed (`0_13`) run, in this order: header (`run=`, `principal=`, `task=`, `evidence=store`, `trace=complete`), `reward=`, `passed`/`failed`, `termination_reason=`, `db_match=`, every `action <name> … match=`, `tools=`, the system prompt verbatim, then each of the 16 messages with its full content, every `tool_call <id> <name>` with every argument value and every `tool <id>` result verbatim. `[policy]` markers = assistant turns matched to a stored `llm.call` (7 and 7), `[env]` present, every `event=` id in the store. The labeler judges from this alone, so nothing may be truncated or reordered. HTML form carries the same strings. |
| D1c_viewer_errors | §10 | unknown run / principal: exit 1, empty stdout. |
| D1d_viewer_tree | statement (per principal, parent references children) | `support` view: 10 `tool_call` lines with the store's `tool.call` ids in order, results shown, `children=3` and a `child … item=<64 hex> passed=null` line per sub-agent; `B2` view: its own `llm.call` ids, none of the parent's calls, no `[policy]`; default principal of a VM run = first agent. |
| D2_trl_dry_tokenization | **done test 2**, claim 3 | Runs `trl_check.py` in the isolated uv env (trl 1.16, Qwen3 tokenizer, tiny random model). `trajectories.jsonl` (messages + tools) tokenizes without error, all items. `sft.jsonl` (prompt-completion, completion-only loss) yields exactly one example per `train:true` turn of every non-flagged item; per example the tokens with labels ≠ −100 contain that turn's own text (or tool name) and never the greeting, the system prompt, or any user/tool text ≥ 24 chars from its prompt; 0 < trained < all tokens. Measured on the collator's `labels` (what training would see). The probe showed that the `messages` format with `assistant_only_loss` trains the fixed greeting, so the trainable file must be prompt-completion. |
| D2b_axolotl_shape | done test 2 (Axolotl `chat_template`) | roles ⊆ {system,user,assistant,tool}; boolean `train` on every message (`message_field_training`); `train` true only on assistant turns the store vouches for (count = matched assistant usage messages); greeting never trained; `tools` non-empty. Axolotl itself is not installed (GPU-sized dependency set); the shape is what its loader reads. |
| D3_tau2_roundtrip | **done test 3** | Five real runs: item messages minus the system message normalise to exactly the SimulationRun's `(role, content, tool calls (id, name, sorted args), usage)` sequence (POC 6's comparator) and the tool-call id sequence is identical. HOST2 repeats it inside the pinned τ² with `SimulationRun.model_validate`. |
| D3b_system_prompt | done 1/3 | `messages[0]` == `SYSTEM_PROMPT.format(domain_policy=sim.policy, agent_instruction=AGENT_INSTRUCTION)` of the pinned τ² (7,056 chars), `train:false`, `event_id:null`. "You see the system prompt" means the real one. |
| D3c_tools_column | statement (`tools` column) | tool names == the 16 pinned retail tools, `parameters` equal, every call's argument keys covered by its schema. |
| D4_manifest_deterministic | **done test 4** | `trajectories.jsonl`, `sft.jsonl`, `manifest.json`, `stats.json` byte-identical: re-run, after caches/TMPDIR/HOME are emptied, with another `--out`, and from a copy of the three roots elsewhere (so ids are content hashes, not paths or clocks). |
| D4b_manifest_hashes_counts | done test 4, M14, demo counts | `manifest.items` = the file's items in order; `item_id` = SHA-256 of the canonical item (recomputed); `dataset_sha256`; `split_file` sha; counts (`items`, `runs_exported`, `passed`/`failed`/`unknown`, `train`/`held_out`/`unsplit`, `flagged`, `sft_examples`, `runs_requested`=20) and `m14` equal the oracle's over the 20-run environment; no root path or wall-clock field in manifest or items. |
| L1_lineage_store_ids | statement (POC 7 debt: ids must be the store's) | every message `event_id` and `lineage.events` id exists in the store; every stored `llm.call` of the principal is referenced; `lineage.spawn`/`session_open` are the real events; no id that `ingest --run-dir` would re-mint; message `ts` = the call's host `ts`. |
| L2_copy_not_authoritative | §2 | run with its `events.jsonl` copy removed exports from the store; run with a stale copy of another run next to it carries its own VM's ids, none of the other's. |
| P1_tree_per_principal | statement (per principal, children, missing content) | tree VM: 5 items (VM, support, B1, B2, B3); `support.children` = B1, B2, B3 with their `item_id`s; parents right; support's 10 `tool_calls` ids/names/arguments/results = the store's `tool.call`/`tool.result` events; one content-less assistant message per `llm.call`; nothing `train`; outcome null; `sft.jsonl` empty; cost tokens = store sums. |
| S1_split_user_disjoint | claim 3, statement (held-out never train) | every item's `split` = the user-disjoint file's verdict for its `task_id`; task 10 → `held_out`; coding → null; a `--split` file that holds out task 0 flips task-0 items. Any held-out task tagged train fails. |
| S2_outcome_cost_time | statement (outcome, cost, tokens, timestamps) | τ² items: `reward`/`passed`/`reward_breakdown`/`termination_reason` from `reward_info`; tokens and call counts from the gateway rows (`policy_calls` 7 vs `environment_calls` 4 on `0_12`); `usd` null when nothing is priced; `start`/`end`/`wall_ms` = `agent.spawn`/`agent.exit`/`session.close`; `witness.sha256` = SHA-256 of `result.json`. |
| S3_m8_carried | M8 | `trace_complete` == `avos completeness` for every store run (`complete` for the five, `incomplete` for truncated counters, `unverified` for none); legacy runs `unverified`; the manifest repeats the item. An unverified/incomplete run is never shown complete. |
| V1_stats_match_oracle | statement (diversity statistics) | `stats.json` and `stats --dataset` equal the suite's own computation over the 20-run export: policy-turn and message histograms, `tool_call_share` (±1e-9), `tools_called`, `tool_at_index`, top (tool, index) share and flag, exact duplicates. |
| V2_planted_shortcut_flagged | statement (published failure: 98 % auth at one index) | hand-made 100-item set, 98 % calling `find_user_id_by_email` at message index 3: `shortcut` = that tool/index, share 0.98, `flag` true; 3 planted duplicates in one group; a 30 % set is not flagged. **Flag bar 0.9**: the failure had 98 %; 0.9 catches it with margin; the number is the tester's. |
| V3_duplicate_real_run | statement (exact duplicates) | `run_retail-0_13` and its clone `0_80` land in one duplicate group with different `item_id`s. |
| H2_legacy_runs | statement (POC 1–5 runs) | `0_10`, `0_9`: `evidence=ledger`, `unverified`, conversation equal to the witness, outcome right, no store id claimed, system prompt present. |
| H3_reward_info_null | statement (4 real runs) | `0_90`: reward/passed/breakdown null, counted `unknown`, in M14's denominator, viewer says `reward=unknown`. |
| H4_witness_tampered | docs/trace.md §4 (never silently merged) | `0_91`: `witness.agrees=false` with problems, 0 sft examples, `flagged=1`, exit 1, `WITNESS DISAGREES` before the first message, still inspectable; also flagged inside `--all`. |
| H5_unknown_run | §6/§10 | skipped `unknown_run`, exit 1, the known run next to it still exported; viewer exit 1. |
| H6_witness_only | §3 | `run_retail-8_1` (no host rows): skipped `no_host_rows` by default; with `--include-witness-only`: `evidence=witness`, unverified, no event ids, nothing trained, conversation = witness. |
| H7_mini_swe_agent | statement (coding runs) | `run_coding-slugify_2`: harness, ledger evidence, `passed` = `tests_passed`, messages = `trajectory.messages`; 12 assistant turns vs 15 ledger calls → `witness.agrees` must be false and nothing trained. |
| A1_native_join_by_response_id | class A (host hashes, trace/CONTRACT.md §5) | four native runs (`0_16`, `0_17`, `0_18`, `1_4`): every message with usage names the call whose `response_id` = its `raw_data.id` (not position); `witness.join = response_id`, `checked` = the six host fields, agrees, trains; the suite's own hash oracle (verified on these runs in selftest) finds no problem. |
| A2_tampered_text_same_tokens, A3_tampered_tool_call_same_tokens, A4_tampered_prompt_and_tools, A5_join_not_by_order | class A | clones of `0_16` whose witness differs only in text (reply, simulator turn, tool result), a tool call's arguments or id, the policy text, the tool schemas (`AVOS_TOOL_SCHEMAS` edited), or two swapped `raw_data.id`s — token counts identical: each `agrees=false` with problems, no `train`, 0 sft rows, exit 1, item still written. Catches the class: anything the host hashed (`output`, `messages`, `system`, `tools`) and the join key, not one instance. |
| J1_judge_call_other_model | §4 (dispute 6) | an injected call to `deepseek-ai/DeepSeek-V4-Flash-0731` under the agent's identity after the conversation (M8 kept complete): listed in `witness.other_calls` as `{event_id, model, tokens}`, kept in `lineage.events` and `cost` (15 calls), never a message; run agrees and trains. |
| J2_leftover_call_same_model | §4 | the same injection on the agent's own model: flagged, nothing trained, exit 1. |
| HOST0_no_stray_loops, HOSTZ_no_leftover_processes | class B | before the live checks no `while pgrep` loop / earlier suite process runs on the host; after `ssh_close()` a one-shot ssh (no control master) finds no `retrain`, `tau2_roundtrip` or loop process on the host, and no control master or helper on the laptop. |
| FIXTURES (gate) | suite integrity | `fixtures/MANIFEST.sha256` (92 files) is checked before any run and in selftest; drift fails everything loudly. The host-derived files were re-verified byte-for-byte against avos-dev on 2026-10-22. |
| X1_witness_replaced_same_size_mtime | §11 (derived state) | after a warm export, `result.json` changes content with the same size, mtime and inode (zip 19122→19123 everywhere): the next export and `view` carry the new text, `item_id` changes. |
| X2_store_edited_in_place | §11 | a stored `llm.call`'s `prompt_tokens` changed in place (same size, mtime): the answer must be a flagged witness or a refusal; a stale identical item or a silent merge with exit 0 fails. |
| X3_cache_tampered | §11 (POC 6 §10 / POC 7 §1) | after a warm export, everything under `AVOS_RETRAIN_CACHE`, `TMPDIR`, `HOME` is truncated / bit-flipped / swapped pairwise / deleted; each following export is byte-identical to the clean one or exits non-zero without writing a different manifest. |
| R1_redaction_hook | statement (redaction hook) | `--redact redact_hook:digits`: no digit left in any message content, tool-call arguments or tool result of `trajectories.jsonl`, `sft.jsonl` and `view`; redacted text shown; `manifest.redact` and `item.redact` set; `item_id`s differ from the unredacted export. |
| SC1_export_700_runs (full only) | §12 | 700 cloned runs (12–20 messages, 7–14 calls): `export --all` ≤ **145 s** (0.2 s/run + 5 s: the POC 11/12 dataset is 65×8 + 44×4 = 696 runs, `conventions.md` §1; a trainer re-exports after every labeling pass, so minutes, not tens of minutes), peak RSS ≤ **512 MiB** (POC 6 §10's streaming bar), 700 items. |
| SC2_store_size_independence (full only) | §12, M9 | the same 20 runs from a store with 20 VMs vs 2,020 VMs: ≤ **3×** (POC 6 §10's scale factor; POC 7 answers per VM from its own files); `view` ≤ **2 s** on the big store (POC 7's warm M9 bar for the largest principal). |
| Z_roots_untouched | collect §2, §6 | after every check the store, results and ledger are byte-identical to the start (nothing written under the roots). |
| HOST1_live_export_lineage_m8 | §2/§6, M8, store append-only | on avos-dev, as root, the five POC 7 runs from `/var/lib/avos/trace`: every id in the live store, every `llm.call` referenced, `trace_complete` = `avos completeness`, witness agrees; every store file's earlier bytes are a prefix afterwards (POC 7 rule). |
| HOST2_tau2_roundtrip_pinned | done test 3 | `tau2_roundtrip.py` in `~/tau2-bench/.venv`: `Results.load` → export → `SimulationRun.model_validate` of the rebuilt messages; class, role, content, tool-call ids/names/arguments, tool ids, error flags and usage sequence identical on the five runs. |
| HOST3_all_runs_counts_determinism | done test 4, demo counts | `export --all` twice over the 57 real result dirs: byte-identical manifest and items; per run `passed` = `reward == 1.0` from its `result.json` (15 at 1.0, 34 at 0.0, 4 null); `runs_requested` = 57; skip reasons from the contract's set. |
| HOST4_view_latency | M9 | `view run_retail-0_12` on the live store ≤ 2 s (+0.5 s ssh). |

## Dispute rulings (2026-10-22)

1. D2 leak snippet — **accepted**: an environment snippet is ignored when the completion itself contains it.
2. D4b run count — **accepted** (suite bug): computed from the results root.
3. H7 `exit` role — **accepted**: non-chat roles are dropped from the comparison and from items (§5).
4. SC1 — **accepted**: the scale export uses `--runs-file` with the 700 clones.
5. HOST2 usage dict — **accepted**: `tau2_roundtrip.py` reads usage dict-or-object.
6. Judge calls — **accepted and pinned** in §4: only calls to the witness-named models join; others go to
   `witness.other_calls` as `{event_id, model, prompt_tokens, completion_tokens}` (dicts: the labeler needs the model);
   a leftover on the agent's model flags the run. `checked` names follow the host fields (`messages`, not `input`).

## Ambiguities resolved by the tester

- **Which TRL loss.** The statement says "assistant-only loss"; the probe (`trl_check.py` on a hand-made item)
  showed that TRL's `messages` + `assistant_only_loss` trains every assistant message, including τ²'s fixed
  greeting that no model produced. The contract therefore requires `sft.jsonl` in TRL's prompt-completion form, one
  example per policy turn (completion-only loss, the default), and `trajectories.jsonl` must still be accepted by
  the `messages` path. D2 checks the labels, not the config name.
- **System prompt.** `result.json` holds only the domain policy; the prompt the model saw is τ²'s `llm_agent`
  template around it. The exact rendering is required (D3b), with the template shipped as a fixture.
- **Tool schemas.** `tool_defs` is null in every stored result; `AVOS_TOOL_SCHEMAS` supplies the pinned τ² schemas
  (fixture extracted from the host's venv) and D3c checks them.
- **Witness agreement** is the POC 6 usage-sequence match, both sides fully matched. A disputed item is written and
  viewable but yields no training example and exits 1; it is never dropped or merged.
- **Legacy and witness-only runs** get their own evidence level; the store remains the only source of `store` ids.
  Witness-only runs need an explicit flag so a dataset never silently contains runs no host saw.
- **M14** counts runs by the harness agent's outcome; unknown outcomes stay in the denominator.
- **Shortcut index** is the message index as the model sees it (system prompt at 0), over `train:true` turns only.
- **Scale bars** (145 s, 512 MiB, 3×, 2 s) are derived from the dataset size in `conventions.md`, POC 6 §10 and the
  POC 7 M9 results; they are the tester's numbers where the statement gives none.
