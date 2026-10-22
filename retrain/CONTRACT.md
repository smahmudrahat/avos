# POC 8 retrain contract

The black-box interface `retrain/tests/run.py` exercises. Everything below is required; nothing about internals is.
Serves: the four done tests of `notes/poc8-statement.md`, claim 3 (a run reconstructs as training data, only the
policy's own turns are trained, held-out tasks never leak into train), M8 (carried, never upgraded), M14
(computable from the manifest), the demo counts `N / M runs` and `kept K passed · dropped D failed`.

Design constraints inherited from POC 6/7: the store under `AVOS_TRACE_ROOT` is **the record**; everything here
is derived from it and never writes to it. Ids are the store's, never re-minted. Content comes from the witness
(`result.json`) only where a stored event vouches for the call that produced it (`docs/trace.md` §4).

## 1. Command

`retrain/bin/retrain` — `python3 retrain/bin/retrain <sub> ...`, Python 3.10+ standard library, no network. On
the dev host the store is root-readable, so `sudo`. Machine output on **stdout**; one line naming the problem on
stderr; never a traceback.

Exit codes: `0` answered or exported with nothing to flag; `1` the data fails (unknown run, skipped or flagged
runs, store or witness unreadable, derived state that cannot be reconciled with the sources) — the stated output
files are still written completely when any run was exported; `2` usage, or an unreadable root, or nothing could
be written.

| Subcommand | Output |
|---|---|
| `runs` | JSON array: every run and its evidence (§3) |
| `view --run R [--principal P] [--redact H] [--html]` | the labeler's view of one principal's trajectory (§10) |
| `export --out DIR (--run R ... \| --all \| --runs-file F) [--split F] [--seed N] [--redact H] [--include-witness-only]` | `trajectories.jsonl`, `sft.jsonl`, `manifest.json`, `stats.json` (§5–§9) |
| `stats --dataset DIR` | JSON: §9 recomputed from `DIR/trajectories.jsonl` |

## 2. Environment

| Variable | Default | Meaning |
|---|---|---|
| `AVOS_TRACE_ROOT` | `/var/lib/avos/trace` | The POC 7 store (`collect/CONTRACT.md` §2). Read-only for `retrain`. |
| `AVOS_RESULTS_ROOT` | `/var/lib/avos/results` | Run directories `run_*/{job.json, result.json[, events.jsonl]}`. `events.jsonl` there is a copy `infra/job` made; it is **not** authoritative — the store is. Read-only. |
| `AVOS_LEDGER` | `/var/lib/avos/events.jsonl` when it exists | POC 1–6 ledger rows (as `trace/bin/ingest --ledger` reads them) for runs that predate the store. Read-only. |
| `AVOS_TOOL_SCHEMAS` | unset | JSON file `{"<harness>/<domain>": [OpenAI function schemas]}` (e.g. `"tau2/retail"`). Unset: the tool may use its own pinned copy; either way the schemas must be the pinned τ²'s (`docs/versions.md`). |
| `AVOS_RETRAIN_CACHE` | under `$TMPDIR` | The only place derived state (indexes, caches) may live, besides `--out`. |

The tests point `TMPDIR`, `HOME`, `XDG_*_HOME` and `AVOS_RETRAIN_CACHE` at private directories and tamper with
everything written there. Nothing may be written under the three roots.

## 3. Runs, principals, evidence

`R` is a job run id `run_<task>_<trial>` or a VM id `vm-NN-xxxxxx` (one VM = one run, `collect/CONTRACT.md` §3).
A run's **evidence** is where its host rows come from:

| `evidence` | Source | `trace_complete` |
|---|---|---|
| `store` | events of the run's `vm_id` under `AVOS_TRACE_ROOT` | exactly `avos completeness --run R` (status and ratio) |
| `ledger` | rows of the run's `vm_id` in `AVOS_LEDGER`, converted as `ingest --ledger` does (ids stamped at conversion, so lineage ids are ledger-stamped and say so) | `{"status": "unverified", "ratio": null}` |
| `witness` | no host rows at all; only `result.json` | `{"status": "unverified", "ratio": null}`; exported only with `--include-witness-only`, otherwise skipped with reason `no_host_rows` |

One **item** per (run, principal) for every principal of the run that has at least one `llm.call` or `tool.call`,
plus the run's harness agent always. A VM principal that made calls (root inside the VM) is an item too.
Sub-agents are their own items; a parent item lists its children and their results (§5 `children`).

`runs` → JSON array sorted by run id, one entry per run directory in `AVOS_RESULTS_ROOT` ∪ every `session.open`
run in the store: `{"run", "harness", "task", "vm_id", "evidence": "store"|"ledger"|"witness", "witness": bool,
"exportable": bool, "reason": str|null}`.

## 4. Witness reconciliation (never silently merged)

Only the stored `llm.call`s of the item's principal to the **models the witness names** take part
(`info.agent_info.llm`, `info.user_info.llm`, provider prefix such as `openai/` dropped). Calls to any other model
(τ²'s NL-assertion judge runs under the agent's identity after the conversation) are **not** merged: they are
listed in `witness.other_calls` (`[{"event_id", "model", "prompt_tokens", "completion_tokens"}]`), stay in `cost`
and `lineage.events`, and never carry content. A judge configured with the agent's own model is a leftover call
and flags the run.

**Join.** When every participating call has a `response_id` (gateway rows since POC 8), each witness message with
`usage` joins the call whose `response_id` equals its `raw_data.id`; the joined calls must come in the witness's
order with equal token counts, and every participating call must be joined (`witness.join = "response_id"`).
Otherwise (POC 7 rows, ledger rows) the POC 6 §4 rule applies: in order, next call with the same token counts, both
sides fully matched (`witness.join = "tokens"`). Coding runs join by count (`"count"`).

**Host hashes.** Where a joined call carries the `trace/CONTRACT.md` §5 hashes, the witness is accepted only if,
with `content_sha`/`chat_form` of `trace/lib_trace.py`: an assistant turn's call has `system_sha256` =
`content_sha(messages[0].content)` and `tools_sha256` = `content_sha(item.tools)`; a user-simulator turn's call has
neither equal to those (the host's own view of who spoke); the turn's text hashes to `output_sha256`
(`content_sha(chat_form(message, "assistant"))`); and an assistant turn's `messages_sha256` equals
`[content_sha(chat_form(m)) for m in messages[:k]]`, the item's messages before it. `witness.checked` lists what
was compared, named after the host fields: `["tokens"]` (POC 7 / ledger rows), `["count"]` (coding runs), or all
six `["tokens", "role", "system", "tools", "output", "messages"]` for native rows (`witness.join` says how the
calls were joined).

The witness **agrees** when every check passes. Otherwise `witness.agrees = false`, `witness.problems` names each
mismatch (message index, what differed), the item is still written with every `train` false, **no `sft.jsonl`
example is produced from it**, `view` prints `WITNESS DISAGREES` before the messages, `manifest.counts.flagged`
counts it, and `export` exits 1. The host rows are never edited to fit the witness and the witness is never trimmed
to fit the host.

The host records no prompt text, so content (system prompt, messages, tool arguments and results) is the
witness's; a message's `event_id` names the stored event that vouches for it (the joined `llm.call`), or is
`null` when no event does (τ²'s fixed greeting, in-harness tool messages, the system prompt).

## 5. Item (one line of `trajectories.jsonl`)

```
{"item_id": <64 hex>, "run": R, "principal": <SPIFFE>, "parent": <SPIFFE>|null,
 "children": [{"principal", "item_id", "outcome": {"reward", "passed", "termination_reason"}}],
 "vm_id", "host", "harness": "tau2"|"mini-swe-agent"|..., "task": "retail/0", "task_id": "0"|null,
 "evidence": "store"|"ledger"|"witness",
 "messages": [...], "tools": [...],
 "outcome": {"reward": number|null, "passed": bool|null, "termination_reason": str|null,
             "reward_breakdown": object|null, "source": "tau2"|"mini-swe-agent"|null},
 "cost": {"usd": number|null, "prompt_tokens": int, "completion_tokens": int,
          "llm_calls": int, "policy_calls": int, "environment_calls": int},
 "timestamps": {"start": ts|null, "end": ts|null, "wall_ms": int|null},
 "trace_complete": {"status": "complete"|"incomplete"|"unverified", "ratio": number|null},
 "witness": {"present": bool, "agrees": bool|null, "problems": [str], "sha256": hex|null,
             "join": "response_id"|"tokens"|"count"|null, "checked": [str], "other_calls": [...]},
 "lineage": {"session_open": id|null, "session_close": id|null, "spawn": id|null, "exit": id|null,
             "label": id|null, "events": [<every stored event_id the item was built from>, sorted]},
 "split": "train"|"held_out"|null, "redact": "MOD:FUNC"|null}
```

- `item_id` = SHA-256 of the canonical JSON of the item without `item_id` (keys sorted, `,`/`:` separators,
  ASCII-escaped: `json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`). Nothing in an item
  may depend on where the roots live (no absolute paths, no wall-clock of the export).
- `messages`: OpenAI Chat Completions messages, roles `system`, `user`, `assistant`, `tool`; assistant
  `tool_calls: [{"id", "type": "function", "function": {"name", "arguments": <JSON string>}}]`; tool messages
  `{"role": "tool", "tool_call_id", "content"}`. Every message also carries `train` (bool) and `event_id`
  (string|null); messages matched to an `llm.call` also carry `ts` (the call's host `ts`) and
  `usage: {"prompt_tokens", "completion_tokens"}`.
  - **`train` is true iff the message is an assistant message produced by the policy**: a witness assistant
    message with `usage` matched to an `llm.call` of this item's principal. It is false for `system`, `user`
    (including the user simulator, which is environment: `docs/trace.md` §9), `tool`, τ²'s fixed opening greeting
    (no model produced it), and for content-less messages (store-only runs).
  - τ² runs: `messages[0]` is `{"role": "system", "content": <the pinned τ² `llm_agent` system prompt for the
    run's domain policy>}` (`SYSTEM_PROMPT.format(domain_policy=sim.policy, agent_instruction=AGENT_INSTRUCTION)`
    at the pinned commit), followed by the witness's messages in order: assistant/user with their `content`,
    assistant tool calls with the **original ids**, names and arguments (`arguments` = JSON string of the original
    dict), tool messages with the original `tool_call_id` and content (and `error: true` when the harness said so).
    Dropping `system` messages from `messages` must give back exactly the witness's ordered messages and ids
    (done test 3).
  - Store-only principals (no witness, e.g. a POC 7 tree): each `llm.call` is an assistant message with
    `content: null`, `train: false`, `event_id` = the call; each gateway `tool.call` is an assistant message with
    one `tool_calls` entry whose `id` is the `tool.call` event id and `arguments` its `args`; each `tool.result`
    a tool message with that `tool_call_id`, `content` = its `result` as text (JSON for structured results) and
    `event_id` = the result event. Order is host `(ts, writer, seq)`.
  - mini-swe-agent runs: `trajectory.messages` of `result.json` in order (system, user, assistant with
    `tool_calls`, tool; entries with any other role, such as `exit`, are dropped), `train` true on assistant messages matched to the principal's `llm.call`s by order
    (one per `assistant` message with content) when the counts agree, else `witness.agrees = false`.
- `tools`: the function schemas of the run's harness/domain (`AVOS_TOOL_SCHEMAS` or the pinned τ²), a list of
  `{"type": "function", "function": {"name", "description", "parameters"}}`; every tool called in `messages`
  has a schema whose `parameters.properties` contain every argument key used.
- `outcome`: τ²: `reward` = `reward_info.reward`, `reward_breakdown` = `reward_info` verbatim,
  `termination_reason` = the simulation's; `passed` = `reward == 1.0` (null when `reward_info` is null).
  mini-swe-agent: `passed` = `tests_passed`, `reward` = 1.0/0.0, `reward_breakdown`
  `{"tests_passed", "exit_status"}`. Store-only: all null.
- `cost`: over the stored `llm.call`s of this principal and run: `usd` = Σ `cost_usd` when every priced call has
  one, else null; tokens summed (gateway counts, not the witness's); `policy_calls` = calls matched to assistant
  witness messages, `environment_calls` = calls matched to user-role messages, `llm_calls` = all.
- `timestamps`: `start` = the principal's `agent.spawn.ts` (VM: `vm.boot.ts − boot_ms`), `end` = its
  `agent.exit.ts` / `vm.exit.ts` (null while unknown), `wall_ms` = the run's `session.close.wall_ms` for the
  harness agent, else `end − start`.
- `split`: from `--split FILE` (default `data/splits/retail_user_disjoint_v1.json`): `task_id` in `train` →
  `"train"`, in `held_out` → `"held_out"`, else `null` (excluded tasks, other benchmarks). **A held-out task is
  never `"train"`.**
- `children`: every principal whose `agent.spawn.parent` is this principal, with its item's `item_id` and
  outcome summary (store-only children: nulls). `parent` is the `agent.spawn.parent` (VM items: null).
- `redact`: the hook name when one was given (§8); content differs, so `item_id` differs.

## 6. `export`

```
export --out DIR (--run R [--run R ...] | --all | --runs-file FILE)
       [--split FILE] [--seed N] [--redact MOD:FUNC] [--include-witness-only]
```

Writes into `DIR` (created; replaced atomically — on failure the previous content stays):

| File | Content |
|---|---|
| `trajectories.jsonl` | one item (§5) per line, sorted by `(run, principal)`, `json.dumps(item, ensure_ascii=False)` |
| `sft.jsonl` | one TRL prompt-completion example per **policy turn** of every item with `witness.agrees != false`: `{"item_id", "turn_index": <index into messages>, "prompt": messages[:turn_index], "completion": [messages[turn_index]], "tools": item.tools}`, sorted by `(run, principal, turn_index)`. The prompt keeps roles and content verbatim (the `train`/`event_id`/`ts`/`usage` keys may stay). |
| `manifest.json` | §7 |
| `stats.json` | §9, computed over `trajectories.jsonl` |

`--all` = every run `runs` lists. `--seed` (default 0) is recorded; any sampling the tool does uses it. Exit 0
when every requested run was exported and no item is flagged; 1 when any requested run is in `skipped` or any
item has `witness.agrees == false` (incomplete or unverified traces are exported and carried as such; they do
not change the exit code); 2 usage.

**Determinism (done test 4):** the four files are byte-identical across re-runs with the same arguments, after
`AVOS_RETRAIN_CACHE`, `TMPDIR` and `HOME` are emptied, with a different `--out`, and when the three roots are
copied elsewhere (same relative split path).

**Store untouched:** after any command every file under `AVOS_TRACE_ROOT` and `AVOS_RESULTS_ROOT` is byte-identical
(the tests snapshot and compare; the POC 7 byte-prefix rule applies to the live store).

## 7. Manifest

```
{"format": "avos-retrain-manifest/1", "seed": N, "redact": str|null,
 "split_file": {"path": <as given>, "sha256": hex}|null,
 "items": [{"item_id", "run", "principal", "split", "passed", "trace_complete": <status>,
            "witness_agrees": bool|null, "sft_examples": int}],            # same order as trajectories.jsonl
 "skipped": [{"run", "reason": "unknown_run"|"no_host_rows"|"witness_unreadable"|"store_unreadable"|<text>}],
 "counts": {"runs_requested", "runs_exported", "items", "sft_examples", "passed", "failed", "unknown",
            "train", "held_out", "unsplit", "flagged"},
 "m14": {"train_runs", "train_passed", "kept_run_fraction": number|null},
 "dataset_sha256": <SHA-256 of the item_ids joined by "\n" in file order>}
```

`passed`/`failed`/`unknown` count **runs** by the harness agent's `outcome.passed` (true/false/null);
`train`/`held_out`/`unsplit` count runs by split; `flagged` counts items with `witness.agrees == false`.
M14 = `train_passed ÷ train_runs` over runs whose split is `train` (unknown outcomes are in the denominator and
never in the numerator); `null` when `train_runs` is 0. No timestamps, host names or absolute paths beyond
`split_file.path` as given.

## 8. Redaction hook

`--redact MOD:FUNC`: `MOD` is an importable module (through `PYTHONPATH`) or a path ending in `.py`; `FUNC(text:
str, field: str) -> str` with `field` ∈ `system`, `content`, `arguments`, `tool_result`. It is applied to every
content string of every message in `trajectories.jsonl`, `sft.jsonl` and `view` output: system prompt,
assistant/user `content`, each `tool_calls[].function.arguments` (the JSON string; the hook receives and returns
the string), tool message `content`. Not applied to `tools`, `outcome`, ids, lineage. The manifest records the
hook. Without `--redact` nothing is changed (the hook may be a no-op on benchmark data).

## 9. Diversity statistics (`stats.json`, and `stats --dataset DIR` recomputing it from `DIR/trajectories.jsonl`)

Over the items, where a **policy turn** is a message with `train == true` and its **index** is its position in
`messages` (0-based, system prompt included: this is the position the model sees):

```
{"items": n, "runs": m,
 "policy_turns_hist": {"<policy turns per item>": items},
 "messages_hist": {"<len(messages)>": items},
 "tool_call_share": <policy turns with tool_calls ÷ policy turns>|null,
 "tools_called": {"<tool>": <policy-turn tool calls>},
 "tool_at_index": {"<tool>": {"<index>": <items that call it at that index>}},
 "shortcut": {"tool", "index", "share": <items calling tool at index ÷ items>, "flag": share ≥ 0.9}|null,
 "exact_duplicates": {"groups": [[item_id, ...], ...], "items": <items in groups>}}
```

`shortcut` is the (tool, index) with the largest share; `flag` is true at `share ≥ 0.9` (the published failure had
98 % of examples calling authentication at one index; 0.9 is the tester's number). `exact_duplicates` groups items
whose messages are identical in `(role, content, tool name, arguments)` sequence (ids, `event_id`, `ts`, `usage`
ignored), groups of size ≥ 2, each group and the list sorted. Shares are floats; counts are ints.

## 10. `view --run R [--principal P] [--redact MOD:FUNC] [--html]`

Text on stdout (HTML with `--html`: the same text, escaped, inside `<pre>` or equivalent). Unknown run or
principal: exit 1, nothing on stdout. Default principal: the run's harness agent (`job.json`
`agent_spiffe_id`, or the `agent.spawn` named by the session's harness), else the first agent spawned in the VM.
In this order:

1. Header lines containing `run=R`, `principal=<SPIFFE>`, `task=<task>`, `evidence=<evidence>`,
   `trace=<complete|incomplete|unverified>`.
2. `WITNESS DISAGREES: <problems>` when `witness.agrees == false`.
3. Outcome: `reward=<number|unknown>`, one of `passed`/`failed`/`unknown`, `termination_reason=<…>`, then the
   breakdown: for τ² `db_match=<true|false>` and one line `action <name> <arguments JSON> match=<true|false>` per
   `action_checks` entry, plus `nl_assertion ... met=<true|false>` per `nl_assertions` entry when present; for
   mini-swe-agent `tests_passed=<true|false> exit_status=<…>`.
4. `tools=<comma-separated names>`.
5. Every message in order: a line `#<index> <role> [policy|env]` (`policy` iff `train`), then `event=<event_id>`
   when not null, then the full content verbatim (no truncation), each tool call as
   `tool_call <id> <name> <arguments JSON>`, tool results as `#<index> tool <tool_call_id>` then content.
6. `children=<n>` and one line per child `child <principal> item=<item_id> passed=<true|false|null>`.

Children and `--principal` let a labeler open each sub-agent's own conversation.

## 11. Derived state

Any index or cache is the tool's own risk and must follow POC 6 §10 / POC 7 §1: bound to the files' **content**
(size, mtime, inode and path are all forged by the tests), checked on every use; a cache that does not match the
sources, or that was truncated, bit-flipped, swapped or deleted, leads to a rebuild or exit 1 with a message —
never to a stale, wrong or empty answer with exit 0. A source replaced by different content of the same size and
mtime, or edited in place, must change the answer accordingly (a changed witness changes the item; a changed
store row that breaks the chain or the match is reported as a problem).

## 12. Scale

Over a synthetic store and results root mimicking the real distribution (12–20 messages, 7–14 model calls per
τ² run): `export --all` of 700 runs (the POC 11/12 dataset size: 65 train tasks × 8 trials + 44 held-out × 4,
`docs/conventions.md` §1) in ≤ 145 s (0.2 s per run + 5 s) with peak RSS ≤ 512 MiB (POC 6 §10). Per-run work is
independent of the store's size: exporting the same 20 runs from a store holding 2,000 unrelated VMs takes
≤ 3× the time it takes from a store holding only those runs (POC 6's scale factor), and `view` of one run on the
large store answers in ≤ 2 s (POC 7's warm M9 bar for the largest principal).

## 13. What may not change

`python3 trace/tests/run.py --quick` and `python3 collect/tests/run.py --selftest` still pass; `retrain` never
writes under the three roots.
