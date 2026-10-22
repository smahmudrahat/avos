# POC 9 results

`label/tests/run.py` (written independently from the targets before the solution) locally and on the dev host
(c8i.large, 2 vCPUs, nested virt), 2026-10-22. Design: `docs/trace.md` §11. Interface: `label/CONTRACT.md`.
Host demo: `just poc9` (level 0), `just poc9 0,1` (asks the judge).

Built: `label/bin/label` (`run`, `view`, `human`, `gold`, `merge`; 828 lines) and the judge's rubric
(`label/rubric.json`). It reads a POC 8 export and the label root, nothing else; it writes its output directory and
appends rows to the label root (`/var/lib/avos/labels`, one JSONL file per labeller). No cache, no database, no
service, standard library only (`http.client` for the judge, kept-alive connections, 8 in parallel). `retrain` is
unchanged. The dry-run set is recorded in `data/splits/dryrun_v1.json` (the seed-300 draw and the 80 run ids).

| Done test | Result |
|---|---|
| 1. `label run --level 0,1` over the dry-run set: reward, judge verdict, reasons, consistency per run | Level 0 over all 80 dry-run runs: one row each; reward and termination equal `result.json`; 80 clean (every run went through the POC 8 gateway: complete traces, agreeing witnesses), 40 passed by reward; tiles `80 / 80 runs labelled`, `kept 40 passed runs → training set · dropped 40 failed`. Then `just poc9 0,1` (approved spend) into the real label root: all 80 judged with reasons naming message indices, both framings each, 160 requests, 0 retries, 0 `unknown`; framing consistency **0.850** (68/80). Result: `kept 19 passed runs → training set · dropped 61 failed` (by rule: reward 59, veto 21). |
| 2. The user labels the gold set blind; kappa(judge, you) and kappa(reward, you) printed | `label view --run R` (text or `--html`) is POC 8's message rendering with the header reduced to run, principal, task, item and tools: no reward, outcome, breakdown, trace or witness status, split, cost, judge, stratum or label (80/80 host views carry the whole transcript and no outcome word). `label gold --next / --status`, `label human` and M10 work end to end (host: two blind labels, kappa `unverified (n=2 < 50)` with its parts). Gold set the tool picks over the judged dry-run set (seed 0, n 60): 21 disagree + 19 passed + 20 failed; first to label `run_retail-0_22`. **Open:** the user's labels. |
| 3. Manifest carries labels and M10; re-running gives the same labels | `OUT/manifest.json` = the POC 8 manifest (every original key equal) + `labels` (label, rule, kept per item; the label set's sha256; the rule; M14 after veto) + `m10` + `judge`; `CARD.md` carries both tiles, both M10 lines, judge, rubric hash, consistency and rule. Re-runs (caches and `$HOME` emptied, another `--out`, dataset and root copied elsewhere, 10,000 items, and `just poc9 0,1` a second time over the real 80 runs) give byte-identical `labels.jsonl`, `manifest.json`, `gold.json` and `CARD.md` with **0** judge requests (160 reused, label root unchanged); `summary.json` differs only in the two per-invocation counters `calls_made`/`calls_reused` (ruling 1). |
| 4. A run where reward and judge disagree, settled by the gold label; the rule written | The rule is in `docs/trace.md` §11, on every card and in every manifest. Real disagreements exist: 21 of the 40 reward-passed dry-run runs are vetoed by the real judge (e.g. `run_retail-0_21`: a price difference the tool results do not support); 15 of them are in the gold set. The settling human label is the user's to give; the suite proves the mechanism (a human pass on a reward-pass/judge-veto run keeps it, a human fail drops it, a human pass never promotes a reward-fail). **Open** until the user labels. |

Acceptance suite, final pass after the dispute rulings (2026-10-22): selftest 33/33; local full **26/26** twice;
`--host` **32/32** twice in a row without clearing state. Before the rulings: full 18/26, `--host` 23/32, every
failure one of the disputes below. Regressions: POC 8 suite
**41/41** local, **47/47** `--host`; the suite's M3 runs POC 8 `--quick` and the collect selftest (pass); M2: POC 8's
export of the fixture environment still has the frozen `dataset_sha256`.

## Measured

| Item | Measured | Bar |
|---|---|---|
| 10,000 synthetic items, `--level 0` | 5.5 s, 100 MiB | ≤ 60 s, ≤ 512 MiB |
| same, `--level 0,1` against the stub (20,000 framings) | 12 s, 197 MiB | ≤ 900 s, ≤ 512 MiB |
| same again (every answer stored) | 6.1 s, 250 MiB, 0 requests | ≤ 120 s, 0 requests |
| label root after 20,000 judge rows | 17.0 MiB | ≤ 50 MiB |
| Suite's real-judge check (HOST2), 4 runs × 2 framings | 8 requests, ≈ 72 K tokens, 6 s; consistency 3/4; replay 0 requests | ≤ 32 attempts, ≤ $1 |
| Real level 0,1 over the 80 dry-run runs (`just poc9 0,1`) | 160 requests, 1 attempt each, 0 `unknown`, 0 errors; 1.20 M prompt + 0.16 M completion tokens (≈ 7.5 K / 1.0 K per framing); 75 s wall incl. export, 8 in parallel; label root 167 KiB (160 rows) | ≈ 1.5 M tokens approved |
| Veto rate | reward-passes **21/40** (52.5 %), reward-fails 34/40 (85 %); per framing 98 veto / 62 pass. Answers behind the vetoes on passes (per framing): hallucinated facts 30, policy not followed 15, wrong tools 15, unsafe action 11 | — (the gold set measures it) |
| Framing consistency | 68/80 = **0.850** (reward-passes 35/40, reward-fails 33/40) | — |
| Replay of the same run | 0 requests, 160 reused; four files byte-identical, `summary.json` equal but for the two counters | 0 requests |
| Dry-run set, level 0 | 80 runs, 80 clean, 40 passed / 40 failed (per task as produced: 9 tasks mixed, 6 all-pass, 5 all-fail); no τ² #384 task among the 20 | — |
| Gold set at level 0 (seed 0, n 60) | 60 = 40 passed + 20 failed (no disagreement stratum without the judge) | 50–100 |
| Gold set after level 1 (seed 0, n 60) | strata 27 disagree (21 vetoed passes, 6 reward-fails the judge passed) / 19 passed / 34 failed; picked 21 disagree (15 vetoed passes, 6 judge-passed fails) + all 19 passed + 20 failed = 60 | 50–100 |
| M14 → M14 after veto (dry-run set, before any human label) | 40/80 = 0.50 → 19/80 = 0.24 | — |

The judge vetoes half the reward-passes (an earlier probe with a different prompt: 1 of 5), mostly for facts the
tool results do not support. Until the user settles them, M14 after veto halves M14. Whether the judge is right is
exactly what kappa(judge, human) on the gold set will say; 15 of the 21 vetoed passes are in it.

## Contract disputes (all accepted by the tester, 2026-10-22)

| Check | Observation | Ruling |
|---|---|---|
| `J2`, `J3`, `X3`, `SC1`, `HOST2` | `summary.judge.calls_made`/`calls_reused` count this invocation (J1: 62, J2: 0), yet `summary.json` had to be byte-identical to the first run; those two fields were the only difference. | `summary.json` compared without the two counters. |
| `X2_root_tampered` (swap) | Wanted rc 0 against a clean run that exits 1 (`run_retail-0_14` stays `unknown` by design). | The clean run's rc. |
| `J1b_storage_rows`, CONTRACT.md §3 reuse key | The fixture's clones share task, tools and conversation (`0_11`/`0_90`, `0_14`/`0_92`/`0_93`, `0_16`/`0_95`): 24 content keys for 32 rows, and a content-only key gave `0_14` its clones' answers on replay, so J1 and J2 could not both hold. | Key `(run, content_sha256, model, rubric_sha256, framing)`; a human label is current per `(run, content_sha256)`. The implementation adds a hash of the framing text and sampling settings, so a changed prompt re-asks. |
| `X1_row_replaced_same_size`, `MG1_merge_order_independent` | Both copied the shared synthetic root after G2/G3 had added 64 human labels; each passed alone. | They copy the root as built. |
| CONTRACT.md §3 stale labels | A stale label kept `run` at exit 1 for ever, even after the user relabelled the new content. | Once the run has a valid label on its current content, stale rows are listed but no longer set the exit code (G4 extended; implemented). |

`counts.by_rule` lists only rules that occur (G3 compares it with the rules present).

## Tech debt

| Item | Effect today | Fix | When |
|---|---|---|---|
| **The judge bypasses the gateway.** `label` runs on the host with the gateway's key. | No `llm.call` row, no hashes of what the judge saw; its spend is not in M12. Rows carry model, usage and the upstream response id. | A judge principal in a microVM through the gateway . | Before POC 10. |
| **Kappa's interval assumes independent runs.** The dry-run set is 20 tasks × 4 trials. | The 95 % interval is too narrow; no prevalence-robust companion (Gwet's AC1) and no intra-rater check. | Task-clustered bootstrap and AC1 as extra fields; the user relabels 10 runs a day later. | When the gold set is labelled. |
| **The gold set follows the strata of the run that draws it.** | Running the judge after labelling started changes the disagreement stratum and so the set (human labels never do). | Label after level 1; or freeze `gold.json` once labelling starts. | If the judge is re-run mid-labelling. |
| **Tokens of failed attempts are not kept.** | A framing that needed retries records only the valid attempt's usage. | Sum usage over attempts. | When judge cost enters M12. |
| **Rate limits.** 8 requests in parallel, 4 attempts, 0.5–2 s backoff. | A throttled endpoint leaves framings `judge_unavailable` (nothing stored, exit 1); the next run asks only for those. | Honour `Retry-After`. | If the endpoint throttles at 8. |
| **`kept` is not split-aware.** | The tile counts every kept run; a held-out run labelled pass would be counted. `labels.m14_after_veto` is train-only. | POC 12 trains on `kept ∧ split == train`. | POC 12. |

## Does not prove

- **Not M10.** No human label exists yet; both kappas read `unverified`. Done tests 2 and 4 close when the user has
  labelled the gold set (7–13 h estimated).
- **Not the judge's quality.** 80 real runs judged, none checked by a human yet. Recorded and replayed answers make
  the labels reproducible, not right; the framings disagree on 15 % of runs.
- **Not that the judge is deterministic.** It is not (measured); only replay is.
- **Not tamper-proof.** `label_id` is a content hash, not a signature: a row rewritten together with its hash is
  accepted. It catches edits and torn writes, as the store's chains do.
- **Not free of quoted values.** Reasons are paraphrases with every 60-character copy removed; they still cite
  amounts, item names and ids from the conversation.
- **Not the real endpoint at scale.** 20,000 framings ran against the suite's stub; the real model answered about 230 requests in this POC.
