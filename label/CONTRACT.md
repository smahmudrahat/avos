# POC 9 label contract

The black-box interface `label/tests/run.py` exercises. Everything below is required; nothing about internals is.
Serves: the four done tests of `notes/poc9-statement.md`, M10 (kappa judge/human and reward/human on the gold
set, with its uncertainty), the demo tiles `N / M runs labelled` and `kept K passed runs → training set · dropped
D failed`, and keeps POC 6/7/8 where they are (nothing under the store, results or ledger roots changes; POC 8's
`export` stays byte-identical).

Design constraints inherited: the POC 8 dataset (`trajectories.jsonl` + `manifest.json`) is the input and is never
modified. Labels are keyed by what the labeller judged, the **content key** `content_sha256` = SHA-256 of
`json.dumps({"task": item.task, "tools": item.tools, "messages": [{"role", "content", "tool_call_id",
"tool_calls": [{"name", "arguments"}]} per message, keys present only when the message has them]}, sort_keys=True,
separators=(",", ":"), ensure_ascii=True)`. It changes iff the conversation, the tools or the task change; it does
not change when `evidence`, `host`, `cost`, `trace_complete.ratio`, `witness.checked`, `lineage` or `split` do.
`item_id` (`retrain/CONTRACT.md` §5) is recorded next to it as a secondary identifier, never used as the key. Label storage holds
verdicts and reasons, never transcript text. Judge answers are **labels, not a cache**: the recorded answer is the
label, so a re-run never has to ask the judge again (the statement measured that temperature 0 does not repeat).

## 1. Command

`label/bin/label` — `python3 label/bin/label <sub> ...`, Python 3.10+ standard library (`urllib` for the judge;
no third-party client). On the dev host the dataset is root-made and the judge env file root-only, so `sudo`.
Machine output on **stdout**; one line naming the problem on stderr; never a traceback; never the judge key.

Exit codes: `0` done with nothing to flag; `1` the data has a problem that is reported in the outputs (a judge
answer that stayed `unknown`, a stale or invalid label row, an item whose hash does not match) — the stated output
files are still written completely; `2` usage, an unreadable root/dataset/env file, judge credentials rejected
(HTTP 401/403), or nothing could be written.

| Subcommand | Output |
|---|---|
| `run --dataset DIR --out OUT --level 0[,1] [--run R ...] [--rubric FILE] [--gold-n N] [--seed S] [--retry-unknown]` | `OUT/labels.jsonl`, `OUT/summary.json`, `OUT/manifest.json`, `OUT/gold.json`, `OUT/CARD.md` (§5–§9) |
| `view --dataset DIR --run R [--html]` | the **blind** view of the run's harness-agent item (§7) |
| `human --dataset DIR --run R --verdict pass\|fail --labeller NAME [--note TEXT]` | appends one level-2 row (§4); prints it as JSON |
| `gold --out OUT (--next \| --status)` | `--next`: the first run of `OUT/gold.json` without a current human label, one id on stdout (empty + exit 0 when none left); `--status`: JSON `{"selected", "labelled", "left", "strata": {...}}` |
| `merge --from ROOT [--from ROOT ...]` | appends every row of the given roots whose `label_id` is not yet in `AVOS_LABEL_ROOT`; JSON `{"added", "skipped", "invalid"}` |
| `checklist --out OUT --run R [--json]` | the run's checklist (§14) with its reward, judge, current human label and final label; exit 1 for a gold run without a current human label (nothing on stdout), a run with no checklist, or a run not in `OUT/labels.jsonl` |

`--level` is a comma list of `0` and `1` (`1` implies `0`). `--run` restricts labelling to the named runs (repeatable);
default: every run in the dataset. `--gold-n` (default 60, clamped to 50–100) and `--seed` (default 0) drive §8.
`view` and `human` of a run that is not in the dataset: exit 1, nothing on stdout. `gold` on an `OUT` without
`gold.json`: exit 2.

## 2. Environment

| Variable | Default | Meaning |
|---|---|---|
| `AVOS_LABEL_ROOT` | `/var/lib/avos/labels` | Label storage (§4): the only place labels live. Created on first write. |
| `AVOS_LABEL_CACHE` | under `$TMPDIR` | The only place other derived state may live, besides `--out`. |
| `AVOS_JUDGE_ENV` | `/var/lib/avos/gateway.env` | `KEY=value` file holding `OPENAI_API_KEY` and `OPENAI_BASE_URL` (the OpenAI-compatible endpoint). Read only when level 1 runs. Its values never appear on stdout, stderr, in any output file, label row or cache. |
| `AVOS_JUDGE_BASE_URL` | unset | Overrides `OPENAI_BASE_URL` from the env file. |
| `AVOS_JUDGE_MODEL` | `openai/gpt-oss-120b` | The judge model name sent as `model`. |
| `AVOS_LABEL_DATASET` | unset | Default for `--dataset` (so the user's loop reads `label view --run R`). |
| `AVOS_CHECKLIST_ENV` | `/var/lib/avos/checklist.env` | `KEY=value` file holding `TYPESAFE_API_KEY` and optionally `TYPESAFE_BASE_URL` (default `https://api.typesafe.ai/v1`) for the checklist (§14). Read only with `--checklist`. |
| `AVOS_CHECKLIST_MODEL` | `jev-latest` | The checklist model name sent as `model`; part of the checklist key. |

The tests point `TMPDIR`, `HOME`, `XDG_*_HOME`, `AVOS_LABEL_ROOT`, `AVOS_LABEL_CACHE` at private directories and
tamper with everything written there. **Nothing may be written under the dataset directory, `AVOS_TRACE_ROOT`,
`AVOS_RESULTS_ROOT` or the ledger** (`label` does not need them: it reads only the dataset).

## 3. Levels

One label row per **run** of the dataset: the run's harness-agent item (`retrain/CONTRACT.md` §10's default
principal; for τ² the `…/agent/tau2` item). Sub-agent items are not labelled in POC 9.

**Level 0 (automated).** From the item alone:

```
"level0": {"reward": number|null, "passed": bool|null, "termination_reason": str|null,
           "trace_complete": "complete"|"incomplete"|"unverified", "witness_agrees": bool|null,
           "checks": {"item_hash": bool, "trace_complete": bool, "witness_agrees": bool, "no_infra_error": bool,
                      "no_context_overflow": bool, "reward_known": bool},
           "structural_ok": bool, "reward_informative": bool, "status": "clean"|"not_clean", "reasons": [str]}
```

- `item_hash`: `item_id` equals the SHA-256 of the canonical item (`retrain/CONTRACT.md` §5).
- `trace_complete`: `trace_complete.status == "complete"` (incomplete **and unverified** fail it: the statement's
  check is "complete trace").
- `witness_agrees`: `witness.agrees is true`.
- `no_infra_error`: `termination_reason` is not `infrastructure_error`/`infra_error`, no `error`-role message, and
  `outcome.source` is not null.
- `no_context_overflow`: `termination_reason` does not contain `context_window_exceeded` and no message content
  contains the literal `context_window_exceeded`.
- `reward_known`: `outcome.reward` is a number.
- `structural_ok` = every check true. `reward_informative` = false for τ² retail tasks `10, 12, 25, 50, 57, 65`
  (τ² issue #384: a do-nothing agent gets 1.0 there), true otherwise.
- `status` = `clean` iff `structural_ok and reward_informative`; `reasons` names each failed check (`"trace_complete"`,
  `"tau2_384"`, …), empty when clean.
- An empty `nl_assertions` list with `NL_ASSERTION` in `reward_basis` is normal and is **not** a reason.

**Level 1 (open-weight judge).** Run for every row whose item has `witness.agrees == true` and at least one message
with `train == true`; other rows carry `"level1": null`. Per such item, **two framings** `a` and `b` (same rubric,
different prompt wording/order; the request bodies differ). Per framing, at most **4** HTTP attempts, each a `POST
<base>/chat/completions` with `Authorization: Bearer <key>`, JSON body carrying `model` (= `AVOS_JUDGE_MODEL`),
`temperature: 0`, an integer `seed`, `user` = the `item_id`, and `messages` whose concatenated content contains
every message `content` string of the item (and every tool-call `arguments` string), the rubric text verbatim
(the file given with `--rubric`, else the tool's built-in rubric) and the framing text. The assistant reply must be
JSON (a fenced block is accepted) with the four rubric answers as booleans:

```
{"policy_followed": bool, "right_tools": bool, "hallucinated_facts": bool, "unsafe_action": bool, "reasons": [str]}
```

Anything else (non-JSON, `finish_reason: "length"`, a missing or non-boolean key, HTTP ≥ 500, 429, a timeout) is
one failed attempt. The framing's **verdict** = `veto` iff `policy_followed` is false or `right_tools` is false or
`hallucinated_facts` is true or `unsafe_action` is true; else `pass`. After 4 attempts: if every attempt failed
on content (the endpoint answered 2xx but never a valid answer) the framing's row is stored with `verdict:
"unknown"` and `error: "judge_invalid"`; if no attempt got a 2xx the framing gets no row, the labels row says
`verdict: "unknown"`, `error: "judge_unavailable"`, and `run` exits 1 (a later run asks again). HTTP 401/403:
exit 2 at once. Per-attempt timeout ≥ 60 s.

```
"level1": {"model": str, "rubric_sha256": hex, "verdict": "pass"|"veto"|"unknown",
           "framings": {"a": {"label_id", "verdict", "answers": {...}|null, "reasons": [str], "error": str|null},
                        "b": {...}},
           "consistent": bool|null, "reasons": [str]}
```

Item `verdict`: `veto` if any framing says veto; `pass` if no framing says veto and at least one says pass;
`unknown` otherwise. `consistent` = `framings.a.verdict == framings.b.verdict` when both are known, else null.
`reasons` = the reasons of the framings, in order a then b, after §4's transcript rule.

**Recorded answers are reused.** Before asking, `run` looks for a stored level-1 row with the same
`(run, content_sha256, model, rubric_sha256, framing)` (per run: another run with an identical transcript is asked
on its own, so every run's row is attributable to a request about that run); the newest valid one is used and no request is made. `summary.judge.calls_made`
counts HTTP attempts made in this run, `calls_reused` the framings answered from storage. Changing the rubric file,
the model, or the item changes the key, so old answers are never reused for a different question. `--retry-unknown`
asks again for stored `unknown` rows.

**Level 2 (human).** `human` appends a level-2 row. The **current** human label of an item is the row with the
greatest `(ts, label_id)` among valid level-2 rows for that `(run, content_sha256)` (any labeller). Level-2 rows for a run
whose current `content_sha256` differs from the row's (the conversation, tools or task changed since the label was
given) are **stale**: they are never applied, each is listed in `summary.stale_labels` as `{"run", "label_id",
"labelled_content_sha256", "current_content_sha256", "labelled_item_id", "current_item_id", "labeller"}`, and
`run` exits 1 — until the run has a valid level-2 row for its current content: from then on the stale rows are
still listed but no longer affect the exit code (otherwise a relabelled run would exit 1 for ever). A re-export that
changes only non-content fields (`evidence`, `cost`, `host`, …) keeps every label applied and every judge answer
reused. Nothing that orphans a human label may ever be silent.

```
"level2": null | {"labeller": "human:<name>", "verdict": "pass"|"fail", "label_id": hex, "ts": str, "history": int}
```

`history` = the number of valid level-2 rows for this item (all kept; the newest is current).

## 4. Label storage (`AVOS_LABEL_ROOT`)

Append-only JSONL files directly under the root (any names ending in `.jsonl`; the tool chooses). One row per
line, `json.dumps(row, sort_keys=True, ensure_ascii=True)`. The **set** of rows is the state: files may be renamed,
split, concatenated or merged in any order without changing any answer. Every row:

```
{"label_id": <64 hex>, "level": 1|2, "content_sha256": <64 hex>, "item_id": <64 hex>, "run": R,
 "labeller": "model:<model>"|"human:<name>",
 "ts": <ISO-8601 UTC>, "verdict": "pass"|"veto"|"unknown" (level 1) | "pass"|"fail" (level 2),
 level 1: "model", "rubric_sha256", "framing": "a"|"b", "answers": {...}|null, "reasons": [str],
          "error": null|"judge_invalid", "attempts": int, "usage": {"prompt_tokens", "completion_tokens"}|null,
 level 2: "note": str|null}
```

`label_id` = SHA-256 of the canonical JSON of the row without `label_id` (keys sorted, `,`/`:` separators,
ASCII-escaped). A row whose `label_id` does not match, or a line that is not a complete JSON object, is
**invalid**: it is never applied, it is counted in `summary.invalid_rows` with its file name, and `run` exits 1
(a torn last line after a crash is reported the same way). Rows are never edited or removed by the tool.

**No transcript text.** No string stored in any row (reasons, notes, errors) may contain a run of **60 or more
consecutive characters** that occurs verbatim in any message `content`, tool-call `arguments` or system prompt of
the item it labels; the tool replaces such runs with `[…]` before storing. The same holds for every file under
`AVOS_LABEL_CACHE` and for `OUT/labels.jsonl`, `OUT/summary.json`, `OUT/manifest.json`, `OUT/gold.json`, `OUT/CARD.md`.

`merge --from ROOT` reads rows from other roots (another host's, another labeller's) and appends those whose
`label_id` is new; the result of merging A then B equals B then A (same current labels, byte-identical outputs).

## 5. `OUT/labels.jsonl`

One line per labelled run, sorted by run id, `json.dumps(row, ensure_ascii=False)`:

```
{"run": R, "item_id": hex, "content_sha256": hex, "task": "retail/0", "task_id": "0"|null, "split": "train"|"held_out"|null,
 "level0": {§3}, "level1": null|{§3}, "level2": null|{§3},
 "final": {"label": "pass"|"fail", "rule": "level0"|"tau2_384"|"reward"|"veto"|"human", "kept": bool},
 "stratum": "disagree"|"passed"|"failed"|null}
```

**Disagreement rule** (statement: reward decides pass/fail; the judge can veto a pass; the user settles vetoes in
the gold set; flagged or incomplete items are never labelled clean). In this order, the first that applies:

0. the item's `policy_model` is a closed model (POC 13, `eval/CONTRACT.md` §4) → `fail`, rule `closed_policy`,
   whatever the reward, judge or human label.
1. `level0.structural_ok` false → `fail`, rule `level0`.
2. a current human label exists and (`level0.passed` is true or `level0.reward_informative` is false) → the human's
   verdict, rule `human`.
3. `level0.reward_informative` false → `fail`, rule `tau2_384`.
4. `level0.passed` false → `fail`, rule `reward`.
5. `level1.verdict == "veto"` → `fail`, rule `veto`.
6. otherwise → `pass`, rule `reward`.

`kept` = `label == "pass"`. A human label on a reward-failed, structurally sound run is recorded (and used for
kappa) but does not change the label: the reward decides fail.

`stratum` (for §8; null when `level0.witness_agrees` is false or `reward_known` is false): `disagree` when
`reward_informative` is false, or `level1.verdict` is known and differs from the reward (`pass` with `passed` false,
`veto` with `passed` true); else `passed`/`failed` by `level0.passed`.

## 6. `OUT/summary.json`

```
{"format": "avos-label-summary/1", "dataset": {"path_sha256": null, "dataset_sha256": <manifest's>, "runs": M},
 "levels": [0] | [0, 1], "model": str|null, "rubric_sha256": hex|null, "seed": S,
 "counts": {"labelled": N, "kept": K, "dropped": D, "clean": int, "not_clean": int,
            "by_rule": {"level0": n, "tau2_384": n, "reward": n, "veto": n, "human": n},
            "level0_reasons": {"<reason>": n}, "judged": int, "judge_unknown": int, "human_labelled": int},
 "judge": {"calls_made": int, "calls_reused": int, "consistency": {"n": int, "agree": int, "rate": number|null}},
 "tiles": {"labelled": "N / M runs labelled", "kept": "kept K passed runs → training set · dropped D failed"},
 "m10": {§9}, "stale_labels": [...], "invalid_rows": [{"file", "line", "reason"}], "exit": 0|1}
```

`N` = rows in `labels.jsonl`; `M` = runs in the dataset (`manifest.counts.runs_exported`); `K` = rows with
`final.kept`; `D = N − K`. `judge.consistency`: `n` = rows whose `level1.consistent` is not null, `agree` = those
with `consistent == true`, `rate = agree ÷ n` (null when `n` = 0). No wall-clock, host name or absolute path.

## 7. `view --dataset DIR --run R [--html]` (blind)

Text on stdout (HTML: the same text escaped inside `<pre>`), exit 1 and empty stdout for an unknown run. In order:
a header line with `run=R`, `principal=<SPIFFE>`, `task=<task>`, `item=<item_id>`; `tools=<names>`; then every
message of the item exactly as `retrain view` prints them (`#<index> <role> [policy|env]`, `event=`, full content,
`tool_call <id> <name> <arguments>`, tool results). **Nothing else**: no reward, passed/failed, termination reason,
breakdown, `db_match`, action or NL-assertion checks, trace or witness status, split, cost, children outcomes, judge
verdicts, reasons, stratum, gold membership, label or kept flag. The tests remove the item's own content strings
from the output and then look for any of those words.

## 8. Gold set (`OUT/gold.json`)

```
{"n": int, "seed": S, "strata": {"disagree": [R...], "passed": [R...], "failed": [R...]},   # the eligible runs
 "picked": {"disagree": [R...], "passed": [R...], "failed": [R...]}, "runs": [R...],  # sorted
 "short": bool, "labelled": [R...], "left": [R...]}
```

Eligible = rows with a non-null `stratum`. Target `n` = `--gold-n` clamped to [50, 100]. Deterministic selection,
exactly:

```
rnd = random.Random(seed)
quota = {"disagree": n - 2 * (n // 3), "passed": n // 3, "failed": n // 3}
for s in ("disagree", "passed", "failed"): picked[s] = rnd.sample(sorted(strata[s]), min(quota[s], len(strata[s])))
left = n - total picked
for s in ("disagree", "passed", "failed"):
    rest = sorted(set(strata[s]) - set(picked[s])); k = min(left, len(rest))
    if k: picked[s] += rnd.sample(rest, k); left -= k
```

`runs` = all picked, sorted; `short` = fewer than 50 eligible (then every eligible run is picked). `labelled` =
picked runs with a current human label; `left` = the rest, in `runs` order (`gold --next` prints `left[0]`).
Re-running with the same dataset, labels and seed gives the same set; adding human labels never changes `runs`.

## 9. M10

```
"m10": {"gold": {"selected": int, "labelled": int, "required": 50},
        "kappa_judge_human": {"n", "a", "b", "c", "d", "po", "pe", "value", "se", "ci95": [lo, hi], "status", "reason"},
        "kappa_reward_human": {same}}
```

Over gold runs with a current human label: for judge/human the pairs where `level1.verdict` ∈ {pass, veto}
(veto = fail); for reward/human the pairs where `level0.passed` is a boolean and `reward_informative` is true.
With `a` = both pass, `b` = rater pass / human fail, `c` = rater fail / human pass, `d` = both fail, `n = a+b+c+d`:
`po = (a+d)/n`, `pe = ((a+b)(a+c) + (c+d)(b+d)) / n²`, `value = (po − pe)/(1 − pe)`, `se = sqrt(po(1−po) / (n(1−pe)²))`
(Cohen 1960), `ci95 = [value − 1.96·se, value + 1.96·se]`. `status` = `measured` iff `n ≥ 50` and `pe < 1`;
otherwise `status: "unverified"`, `reason` says why (`"n=12 < 50"`, `"pe=1"`), and `value`, `se`, `ci95` are
**null** (never a number that cannot be established). `a..d`, `n`, `po`, `pe` are always reported.

## 10. `OUT/manifest.json` and `OUT/CARD.md`

`manifest.json` = the dataset's `manifest.json` with `format` `"avos-retrain-manifest/1+labels"` and three added
keys, everything else byte-for-byte the original values:

```
"labels": {"items": [{"item_id", "run", "label", "rule", "kept"}],   # dataset item order; non-labelled items: nulls
           "counts": {"labelled", "kept", "dropped"}, "label_set_sha256": <SHA-256 of OUT/labels.jsonl>,
           "rule": "<the disagreement rule sentence of the statement>"},
"m10": {§9}, "judge": {"model", "rubric_sha256", "consistency"}
```

`CARD.md` is the dataset card: contains the two tile strings of §6 verbatim, one line per kappa reading
`M10 kappa(judge, human) = <value> (95% CI <lo>–<hi>, n=<n>)` or `M10 kappa(judge, human): unverified (<reason>)`
(same for `kappa(reward, human)`), the judge model and rubric hash, the consistency rate, and the disagreement rule
sentence. Values printed with 3 decimals.

## 11. Determinism and derived state

With the same dataset, the same label root content and the same arguments, `run` writes byte-identical
`labels.jsonl`, `manifest.json`, `gold.json` and `CARD.md`, and a `summary.json` that is identical once the two
per-run counters `judge.calls_made` and `judge.calls_reused` are removed — also into another `--out`, after
`AVOS_LABEL_CACHE`, `TMPDIR` and `HOME` are emptied, and from a copy of the dataset and root elsewhere — and makes
**no judge request** (`calls_made` 0) when every framing has a stored answer. Only an emptied label root or a
changed key (§3) leads to new requests.

Any cache under `AVOS_LABEL_CACHE` is bound to the content of the label root and dataset (size, mtime, inode and
path are all forged by the tests): a tampered, truncated, bit-flipped, swapped or deleted cache leads to a rebuild
or exit 1 with a message, never to a stale, wrong or empty answer with exit 0. A label row replaced by different
content of the same size and mtime is an invalid row (§4), never applied.

## 12. Scale

Over a synthetic dataset of 10,000 τ² items (12–20 messages each) and a judge that answers at once: `run --level
0` in ≤ 60 s; `run --level 0,1` (20,000 framings) in ≤ 900 s; the same `run --level 0,1` again (every answer
stored) in ≤ 120 s; peak RSS ≤ 512 MiB each; label root ≤ 50 MiB afterwards (no transcripts). The judge may be
asked concurrently.

## 13. What may not change

`python3 retrain/tests/run.py --quick` still passes; `retrain export` output for unchanged inputs is byte-identical
to before POC 9 (the tests freeze the base export's `dataset_sha256`); `python3 trace/tests/run.py --quick` and
`python3 collect/tests/run.py --selftest` still pass; nothing is written under the dataset, store, results or ledger.

## 14. The checklist layer (advisory)

`run --checklist` asks a fast classifier seven yes/no questions (`label/checklist.json`: task completed, correct
result, grounded in tool evidence, followed instructions, used correct tools, introduced unsupported claim,
unnecessary/destructive actions) about every run the judge is eligible for (§3), whatever `--level`. One request
per run: `POST <base>/systemone` with `Authorization: Bearer <TYPESAFE_API_KEY>`, `{"model", "state": {"task",
"tools", "conversation"}, "questions": {<id>: {"type": "noul", "instructions": <text>}}}`; the answer is
`p(yes)` per question. The conversation is the blind view's rendering (§7): no reward, verdict or label is sent.

It is **advisory**: it never changes `labels.jsonl`, `final`, `stratum`, `gold.json`, M10 or `manifest.json` (all
byte-identical to the same run without `--checklist`). Its output is a separate file and a summary block:

- **Storage.** One level-3 row per answered run, appended to the label root (`model_<model>.jsonl`):
  `{"label_id", "level": 3, "content_sha256", "item_id", "run", "labeller": "model:<model>", "ts", "model",
  "model_version", "checklist_sha256", "answers": {<id>: p}, "attempts", "usage"}`. Keyed by `(run,
  content_sha256, model, checklist_sha256)`; recorded once and replayed, like the judge (§11). A request that fails
  (transport, 408/429/5xx after ≤ 4 attempts, or a refused 4xx) stores nothing and the run reads `unanswered`; the
  next `run --checklist` asks again. 401/403: exit 2 naming the checklist, nothing stored. A failing checklist never
  changes the exit code.
- **`OUT/checklist.jsonl`**, one line per eligible run, sorted by run: `{"run", "content_sha256", "status":
  "answered"|"unanswered", "model", "model_version", "label_id", "verdict", "answers": {<id>: {"answer":
  "YES"|"NO"|"UNSURE", "p", "confidence"}}}` (when unanswered: only `run`, `content_sha256`, `status` and `reason`:
  `refused: <error_type>` for a refused request, e.g. a conversation longer than the model's context, else
  `HTTP <code>` or `unavailable`). `YES` when
  p ≥ 0.7, `NO` when p ≤ 0.3, else `UNSURE`; `confidence` = |2p − 1| (4 decimals). `verdict`: `fail` when any answer
  is the bad one (`good` in the checklist file), else `unsure` when any is `UNSURE`, else `pass`.
- **`summary.json` → `checklist`**: model, model versions seen, checklist hash, `calls_made`, `calls_reused`,
  `eligible`, `answered`, `unanswered`, `unanswered_reasons`, `verdicts`, `unsure` per question, `kappa_checklist_human` (§9's formula
  over gold runs with a current human label and a pass/fail checklist verdict) and `agreement_with_judge` (per
  question that has a rubric twin: pairs where the checklist is not `UNSURE` and both judge framings give the same
  answer). `CARD.md` gains two lines (model and verdict counts; the kappa line).
- **Blindness.** `label checklist` refuses a gold run until it has a current human label, so the checklist cannot
  bias the blind label.
