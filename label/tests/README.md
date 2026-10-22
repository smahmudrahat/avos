# POC 9 acceptance suite

Independent, black-box checks of `label/CONTRACT.md`. Written before and without the implementation; the only
things it touches are `label/bin/label`, POC 8's `retrain` (as the dataset producer and the oracle for items), a
stub judge the suite runs itself, and raw fixtures. POC 8's suite library builds the environment (fixture store,
results, ledger, cloned runs).

```
python3 label/tests/run.py --selftest   # the suite's own oracles and the stub judge (31 items, ~5 s)
python3 label/tests/run.py --quick      # everything but the 10,000-item scale check and the full POC 8 suite (~3 min once implemented)
python3 label/tests/run.py              # + scale, POC 8 --quick suite, collect selftest
python3 label/tests/run.py --host       # + live checks on avos-dev over ssh (repo pushed to ~/poc; waits for DONE in /tmp/p9-dryrun.log)
python3 label/tests/run.py --only J2    # one prefix
```

One line per check: `PASS|FAIL|SKIP <id> [<target>] <measured vs bar>`. Exit 0 only if every executed check passed.
A missing CLI reads `FAIL … not implemented: label/bin/label`, never a traceback. Scratch is a fresh `mkdtemp` per
run, removed on success; host scratch is `/tmp/p9-tests`, removed in `finally`; the ssh control socket lives under
`/tmp/p9-*` and is closed in `finally`. The suite can run twice in a row without clearing anything.

## Environment (built at run time from POC 8's fixtures)

`retrain/tests/lib.Env` plus six clones: `run_retail-50_0` (τ² #384 task, reward 1.0), `0_90` (reward null),
`0_91` (witness tampered: flagged), `0_92` (truncated counters: incomplete), `0_93` (no counters: unverified),
`0_95` (`termination_reason` = `context_window_exceeded`). POC 8's `export --all` over it gives **18 runs, one
harness-agent item each**: 9 clean (5 passed, 4 failed), 9 that each fail exactly one level-0 rule. Its
`dataset_sha256` is frozen in `fixtures/expected_poc8.json` (M2 proves POC 8's export did not move).

The **stub judge** (`lib.Judge`) is an in-process OpenAI-compatible server: deterministic per request body,
adversarial on demand (HTTP 500, non-JSON body, truncated JSON with `finish_reason: length`, wrong schema,
string booleans, fenced JSON, a 3-s delay, 401, a drifting verdict on every call, items that never answer validly,
items that answer differently to the two framings), quoting 200 transcript characters into its reasons. It logs
every attempt (item from `user`, body hash, auth header, model, temperature, seed, whether given strings were in
the prompt) and what it served, so checks compare rows against the *served* answers.

## Fixtures (`fixtures/`, hash-gated by `MANIFEST.sha256` at the start of every run and in selftest)

| Path | What |
|---|---|
| `expected_poc8.json` | `dataset_sha256` and counts of POC 8's export over the environment, frozen 2026-10-22 |
| `rubric_alt.json` | a second rubric file (changed wording): `--rubric` must re-ask the judge and carry its sha |

## Checks → target → bar

| Check | Target | Bar and why |
|---|---|---|
| A0_cli_surface | CONTRACT.md §1 | no args / unknown sub / missing `--dataset` / `--level 3` / unknown dataset / missing judge env → 2; `view`/`human` of an unknown run → 1 with empty stdout; nothing written to the label root when no judge was reachable; no traceback anywhere. |
| L0_rows_match_oracle | **done test 1**, demo tiles | `run --level 0`: one row per run (18, sorted); every `level0` field, `checks`, `status`, `reasons`, `final`, `stratum`, `item_id`/`task_id`/`split` equal the suite's own computation from the item; tiles and counts recomputed (`N / M runs labelled`, `kept K … dropped D`); the empty-`nl_assertions` run (`0_13`) is clean and kept. |
| L0b_never_clean | statement (never labelled clean) | flagged (`0_91`, coding run), incomplete (`0_92`), unverified (`0_93`, `0_10`), null reward (`0_90`), context overflow (`0_95`), #384 task (`50_0`): each `not_clean` for exactly that reason, `final` fail with rule `level0`/`tau2_384`, never kept. |
| L0c_item_hash | §3 | one item's text edited in place in a copy of the dataset: that run's `item_hash` false, not kept, exit 1. Labels are keyed by `item_id`, so the tool must notice when content and id disagree. |
| J1_judge_protocol | **done test 1**, §3 | with the adversarial stub: exactly the eligible runs (witness agrees ∧ a policy turn) are judged; per run two distinct request bodies (framings), ≤ **4** attempts per framing (the stub needs ≤ 3), `Bearer <key>`, `temperature 0`, integer `seed`, `model`, `user = item_id`, every message string in the prompt; stored answers = the answers the stub served; item verdict = any-veto rule; `consistent` right for the 2 planted inconsistent items and the consistency number `agree/n` recomputed; the always-invalid item is `unknown`/`judge_invalid`; reasons carried, with no ≥ 60-char transcript run; `calls_made` = attempts seen; exit 1 because one item stayed unknown. |
| J1b_storage_rows | §4 | every row under the root parses, has the canonical `label_id`, model, rubric sha, framing; verdict follows from the answers; 2 × judged rows; 2 `unknown` rows with `judge_invalid`; no transcript text or key under root or caches. |
| J2_rerun_identical_no_calls | **done test 3** | re-run with caches/HOME emptied, another `--out`, and from a copy of dataset + root: all five outputs byte-identical, **0** judge requests (`calls_made 0`, `calls_reused 2×judged`). The measured fact (temperature 0 does not repeat) makes this impossible unless answers are stored. |
| J3_changed_rubric_or_model_reasks | done test 3 (no silent reuse) | `--rubric fixtures/rubric_alt.json`: ≥ 2×judged new requests, the file's text in every prompt, its sha in rows and summary; `AVOS_JUDGE_MODEL` changed: new requests with that `model`; back to the original key: 0 requests and outputs identical to the first run. |
| J4_no_repeat_without_storage | statement (measured fact) | root emptied, stub drifting: the judge is asked again (2×judged), at least one verdict changes, and every stored verdict equals the judge's newest served answers. Proves the labels come from the judge, not from a hash. |
| J5_judge_unavailable_or_rejected | §3 | closed port: every judged run `unknown`/`judge_unavailable`, exit 1, **no row stored** (so a later run retries), level 0 complete; judge back: asked; 401: exit 2, no rows. |
| J6_secret_hygiene | hard rule | the dummy key appears in no file under the scenario roots (except the env file itself) and in no stdout/stderr the suite saw. |
| G1_blind_view | **done test 2**, §7 | for every run (text and `--html`): header with `run=`, `item=`, `tools=`; every message content and tool-call argument string verbatim; after removing the item's own strings, none of `reward|passed|failed|termination|db_match|match=|met=|WITNESS|trace=|witness=|split=|verdict|veto|judge|kappa|stratum|flagged|kept|label=|gold|nl_assertion|cost|usd=|STUBREASON`; `AVOS_LABEL_DATASET` honoured. POC 8's own view fails this (selftest proves the oracle catches it). |
| G2_gold_selection | statement (50–100, stratified, deterministic, resumable) | 200-run synthetic dataset, stub verdict map (judge agrees with reward 70 %): strata recomputed from the rows; `gold.json` equals the §8 algorithm for (60, seed 3), (80, seed 4), `--gold-n 7` clamps to 50; three `human` labels leave `runs` unchanged, `labelled`/`left` right, `gold --next` = `left[0]`, `--status` = 60/3/57; the 18-run dataset is `short` with every eligible run. |
| G3_m10_kappa | **done test 2**, **M10**, done test 4 | 20 labels → both kappas `unverified`, `value`/`ci95` null, card reads `unverified (n=20 < 50)`; 60 labels (one relabelled by a second labeller a second later): `n,a,b,c,d,po,pe,value,se,ci95` equal the §9 formula from the rows (1e-9), `status measured`, card lines `M10 kappa(judge, human) = x.xxx (95% CI lo–hi, n=60)`; current label = newest, `history 2`, both rows in storage; every gold row's `final` follows the §5 rule; ≥ 1 reward-pass/judge-veto run settled by the human; a human `pass` never promotes a reward-fail; `counts.by_rule`, tiles and `manifest.m10` recomputed. **n ≥ 50** because the statement sizes the gold set at 50–100 and at n = 50 the CI half-width is already ≈ ±0.25; below that a point estimate would mislead. |
| G5_key_is_content | ruling (label key) | three human labels, then the dataset re-exported with `evidence`/`host`/`cost.usd`/`ratio`/`lineage` changed (new `item_id`): labels applied, 0 judge requests, no stale; then one word in one reply of one run: only that run stale (reported, rc 1, label not applied, 2 requests), the other two kept. The key is what the labeller saw. |
| G4_stale_label_reported | §3 | a human label, then the run's witness changes and POC 8 re-exports (new `item_id`): the label is listed in `stale_labels` with both ids and the labeller, not applied, exit 1. |
| M1_manifest_card | **done test 3**, dataset card | manifest = dataset manifest (every original key equal) + `labels` (items in dataset order with label/rule/kept, counts, `label_set_sha256` = sha of `labels.jsonl`, the rule sentence) + `m10` + `judge`; card has both tiles verbatim, both M10 lines, model, rubric sha, consistency rate, rule; no absolute path or wall-clock in summary/manifest. |
| M2_poc8_untouched | §13 | the dataset directory is byte-identical after every command; POC 8's `export` over the environment has the frozen `dataset_sha256` and counts, and two exports are identical. |
| M3_poc8_suite | §13 | quick: `retrain/tests/run.py --only A0` and `--only D4` pass; full: `--quick` passes and `collect/tests/run.py --selftest` passes. |
| X1_row_replaced_same_size | §4/§11 | a stored level-2 verdict flipped in place (`pass`↔`fail`: same size, mtime, inode): listed in `invalid_rows`, not applied, exit 1. |
| X2_root_tampered | §4/§11 | files swapped pairwise: exit 0 and identical outputs (rows are a set); a byte flipped: exit 1 with `invalid_rows`; truncated to half: either reported or the lost answers re-asked (`calls_made` = requests seen); 18 rows every time; no traceback. |
| X3_cache_tampered | §11 | after a warm run, everything under `AVOS_LABEL_CACHE`, `TMPDIR`, `HOME` truncated / flipped / swapped / deleted: outputs byte-identical to the clean run or exit non-zero. |
| MG1_merge_order_independent | statement (two hosts / labellers) | roots A (alice, 5) and B (bob, 5 + a later relabel of one of alice's): `merge` adds 6 / 5, idempotent, A←B and B←A produce byte-identical outputs; bob's later label wins with `history 2`; 10 human-labelled runs. |
| T1_no_transcript_text_anywhere | statement (no transcript copy) | no ≥ **60**-character run of any item's content under the label root, the caches or the outputs, for the judged and the synthetic scenario (the stub quotes 200 chars into every reason). 60 is below a sentence of τ² dialogue and above tool names, ids and amounts a reason legitimately cites. |
| SC1_scale_10k (full only) | §12 | 10,000 synthetic items: `--level 0` ≤ **60 s**; `--level 0,1` (20,000 framings against the stub, which serves > 2,000 req/s) ≤ **900 s** (≤ 45 ms per framing: a labelling pass over the POC 11/12 dataset of ~700 runs then costs a minute, and 10⁴ a quarter hour); re-run ≤ **120 s** with 0 requests; peak RSS ≤ **512 MiB** (POC 6 §10's bar, as in POC 8); label root ≤ **50 MiB** (rows without transcripts are ≈ 1 KB each). |
| Z_roots_untouched | collect §2 / retrain §2 | store, results, ledger and dataset byte-identical to the start after every check. |
| HOST0_no_stray_loops, HOSTZ_no_leftover_processes | class B | no stray loop or suite process before; after cleanup no process, no `/tmp/p9-tests`, no ssh master on the laptop. |
| HOST1_dryrun_level0 | **done test 1** (real data) | waits (≤ 90 min, 30-s polls) for `DONE` in `/tmp/p9-dryrun.log`; the dry-run runs = results on the 20 tasks created after the log's first line (expects 80, accepts ≥ 60); `retrain export --runs-file` → `label run --level 0`: one row per run, reward and termination equal `result.json` (summaries only cross the wire), clean only with an agreeing witness and a complete trace, empty NL assertions never a reason, tiles recomputed. |
| HOST2_real_judge_budgeted | done test 1/3 (real judge, **≤ $1**) | 4 clean runs × 2 framings through the real model (≤ 32 attempts, ≈ 80–300 K tokens, cents): ≥ 3 of 4 get a verdict with reasons, stored rows canonical, token usage recorded, no key-like string on stdout; the same command again: 0 requests, five files identical. |
| HOST3_blind_view_all | done test 2 | the blind view of every dry-run run, compared on the host against the item's own strings: full transcript, no outcome word. |
| HOST4_gold_and_human_loop | done test 2/4 (the user's path) | level 0 over all dry-run runs (no judge spend): `gold.json` = algorithm (seed 0, n 60; `short` when < 50 eligible), two blind labels via `human`, `gold --next`/`--status` right, M10 `unverified` with `n=2` and null values. |

## Ambiguities resolved by the tester

- **Input is the POC 8 dataset, not the roots.** The statement says "POC 8 manifest + labels". Labels are keyed by
  `content_sha256` (task, tools, messages as the blind view shows them), not by `item_id`, which also hashes
  evidence, cost, host and lineage: a later `avos import` or a price table must not orphan 7–13 hours of human
  labels or force the judge to be re-asked. `item_id` is recorded as a secondary id. Anything that orphans a human
  label is reported (`stale_labels`, exit 1), never silent.
- **"Complete trace" means `complete`.** Unverified (ledger / witness-only) runs are structurally not clean; they
  are still labelled and judged, never kept. The statement's structural check list is taken literally.
- **Judge answers are the record.** A stored level-1 row keyed by (item, model, rubric, framing) is reused for
  ever; changing any part of the key asks again. Content failures are stored as `unknown` (reproducible),
  transport failures are not (retryable).
- **Veto by any framing.** With two framings, one veto vetoes; the human settles it in the gold set. Consistency
  is the share of judged runs whose two framings agree.
- **The human decides only where the statement lets them**: reward-passed runs (settling vetoes or dropping a
  pass) and #384 runs (reward uninformative). A human `pass` on a reward-failed run is recorded for kappa but the
  reward decides fail.
- **#384 tasks** (`10, 12, 25, 50, 57, 65`) are `reward_informative: false`: never kept on reward alone, in the
  `disagree` stratum so the gold set covers them, kept only by a human `pass`.
- **Kappa uncertainty** is Cohen's 1960 large-sample SE with a 1.96 interval — closed-form, so the suite recomputes
  it exactly; a bootstrap would not be checkable without pinning the resampling. `unverified` below n = 50 or at
  `pe = 1`, with the parts still printed.
- **Blind view** is a `label` subcommand with POC 8's message rendering minus every outcome line; the test strips the
  item's own text before scanning so transcript words such as "failed" cannot trip it.
- **Scale bars** (60 / 900 / 120 s, 512 MiB, 50 MiB) are the tester's numbers where the statement gives none.

## Dispute rulings (2026-10-22)

1. Per-run counters vs byte-identical `summary.json` — **accepted** (contract contradiction): `summary.json` is
   compared without `judge.calls_made`/`calls_reused` (J2, J3, X3, SC1, HOST2).
2. X2 swap expected rc 0 while the clean run exits 1 (an item stays `unknown` by design) — **accepted**: the clean
   run's rc.
3. Reuse key: the fixture holds five clones with transcripts identical to their source run, so a content-only key
   gives 24 keys for 32 rows — **accepted the per-run reading**: key `(run, content_sha256, model, rubric,
   framing)`; a human label is current for `(run, content)`. Every row stays attributable to a request about its
   run; identical transcripts in other runs are rare and cheap. Selftest now asserts the fixture's keys are distinct
   under the contract's key (`fixture_keys_distinct`), which would have caught the mismatch before the solver did.
4. X1 and MG1 reused the synthetic root after G2/G3 appended labels — **accepted** (suite bug, order-dependent
   shared state): they copy the root as built (`root0`). Class check: every check that mutates a root must own a copy.
5. A stale label kept `run` at exit 1 for ever — **accepted**: once the run has a valid label on its current
   content the stale rows are listed but no longer set the exit code (G4 extended).
