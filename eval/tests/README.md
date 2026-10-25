# POC 13 acceptance suite

Independent, black-box checks of `eval/CONTRACT.md`. Written before and without the implementation. It touches
only the contract CLIs (`gateway/gateway.py` served on loopback by `gw_local.py`, `fleet/bin/fleet`,
`train/bin/train`, `label/bin/label`), the hash-gated inputs below and, with `--host`, the fleet host over ssh.

```
python3 eval/tests/run.py --selftest    # the suite's own oracles, fakes, PKI and builders (~20 s)
python3 eval/tests/run.py --quick       # every local check (~1 min; today: fails cleanly, "not implemented" / named gaps)
python3 eval/tests/run.py --live        # + L1: one real tool exchange with claude-opus-5-5 (ANTHROPIC_API_KEY; < $0.05)
python3 eval/tests/run.py --host        # + H1/H2 on avos-dev (repo at ~/poc): one real closed-policy trial (≈ $1, ≤ 20 min)
python3 eval/tests/run.py --only M      # one prefix
```

One line per check: `PASS|FAIL|SKIP <id> [<target>] <measured vs bar>`. Exit 0 only if every executed check passed.
A missing CLI reads `FAIL … not implemented: <path>`; a suite bug reads `FAIL … suite error …`; never a traceback.
Scratch is a fresh `mkdtemp` per run (under `$TMPDIR`; kept on failure or `--keep`). Host scratch is
`/tmp/p13-tests`, the ssh control socket `/tmp/p13-ssh-*/cm-*` (short path: macOS `TMPDIR` is too long for a
unix socket); both removed in `finally`, together with any `fleet run --out /tmp/p13-tests/…` still alive. The
live gateway subprocess and the fake upstreams are stopped in `finally`.

## How the gateway is tested without a host

`gw_local.py serve` imports `gateway/gateway.py` as a module, points `JAIL`/`CUT`/`ROUTES`/`UPSTREAM`/`ROUTE_TABLE`
at scratch files, gives `CTX` a server certificate signed by a test CA, and serves the real `Handler` on
127.0.0.1 with client certificates required. The client presents a certificate whose URI SAN is
`spiffe://avoslab.com/vm/vm-01-abcdef/agent/tau2`, exactly what the forwarder relays; the one seam is that the
slot and VM id, which the real `setup` reads from the 172.16.NN.2 client address and the jail, come from the
command line. Routing, Cedar decision, translation, the upstream request, the reply, the row and the hashes all run
in the gateway's own code. Without `cedarpy` (a laptop) a stub that allows `llm.call` and denies the rest stands in
for the policy engine (that is what `policies/avos.cedar` decides for model calls); each run prints which one ran.
Two fake upstreams record every request (path, headers, body bytes) and answer from a script, and, like the real ones, 401 without the route's key (`x-api-key` / `Authorization: Bearer`): one plays Anthropic's
Messages API, one an OpenAI-compatible server. No key ever leaves the scratch directory.

## Checks → done test / claim / metric → bar and why

| Check | Serves | Bar | Why this bar |
|---|---|---|---|
| `fixtures` | all | every hashed input unchanged | a changed baseline (matrix, pooled.json, prices, split) cannot pass unnoticed |
| G1 request shape | done test 1; M12 (cache), pinning | `/v1/messages`, `x-api-key`, `anthropic-version`; `max_tokens` 16000, `effort` medium, top-level `cache_control`; no `temperature`/`top_p`/`seed`/client `max_tokens`/forced `tool_choice`; system → `system`; tools → `input_schema`; exactly 1 upstream request; row `dropped_fields` lists all four + `max_tokens` | the statement's measured facts: Opus 5.5 400s on `temperature`, needs `max_tokens`, effort is the only thinking control; the row must prove what was sent |
| G2 messages translation | done test 1 | 5 alternating turns, two `tool_use` from one assistant message, consecutive tool messages merged before the user's text, no empty text block | τ² sends one `role: tool` per call and the Messages API rejects consecutive same-role turns and empty text; a wrong merge silently changes what the model sees |
| G3 reply translation | done test 1; M11 (finish reasons feed causes) | 8 scripted replies (text, tool_use, 3 parallel, text+tool, empty, thinking, max_tokens, refusal) come back as valid chat completions: `content` str/null/"", `tool_calls` with JSON-string arguments and upstream ids, `finish_reason` stop/tool_calls/length/content_filter, usage 650/45/695; row `finish_reason`, `stop_reason`, `usage`, `response_id` | LiteLLM/τ² parse exactly these fields; `length` drives `agent_length`; thinking must not leak into content |
| G4 errors passthrough | done test 1 ("one trial is one attempt") | 429 ±`retry-after`, 500: same status + body back, row `upstream_status`, null usage; malformed 200 → 5xx `upstream_bad_body`, row `upstream_status 200`; one upstream request each | a gateway retry would turn one trial into several attempts and hide rate limiting from M13/causes |
| G5 row fields + hashes | done test 1; M12; `retrain` witness join | `usage` ×5 incl. thinking; `prompt_tokens` = 1000+250+4000, `completion_tokens` 80; absent cache fields → null; `system/tools/messages/output_sha256` = `content_sha(chat_form)` of the OpenAI shapes; `authz_ref` = the preceding allow; key absent from store + log | M12 and the witness check are computed from these fields alone; a key in the store is a leak |
| G6 open route unchanged | done test 1 ("existing behaviour"), claims 1–2 regression | request bytes, path, Bearer header, reply bytes, 27 row keys, hashes exactly as today | the open arms' numbers must stay comparable with POC 11/12 |
| G7 route table | CONTRACT §1.1 | `route` shows kind + pins, never the key; open → `openai`; `no-route`; anthropic entry without pins → `bad-route` | pins must be visible and mandatory, never defaulted in code |
| G8 assistant-first history | ruling 2 (§1.2) | τ²'s exact shape (system, greeting, user, tool call, tool result) → upstream `[assistant, user, assistant, user]`, greeting the first turn, no synthetic user turn, hashes on the OpenAI shape | the solver's probe: 35 real τ² calls in this shape answered 200; adding or dropping a turn changes what the model sees |
| F1 fleet.json records route | done test 2 | `policy_route` = kind/upstream/effort/max_tokens/cache for the closed policy, `openai` for an open one; a resume under another table exits 2 and runs nothing | "same everything else": the pins are part of the plan |
| F2 matrix refuses bad rows | done test 2 | effort / max_tokens mismatch, a null or missing usage field, another policy model → exit 1 naming the reason and run; clean rows → basis `tokens`; no `policy_route` → exit 2 | a row at another effort or without usage must not be averaged into a frontier number |
| F5 served checkpoint recorded | §2.1 class checks (wrong arm; fakes no more permissive than the real upstream) | open policy behind 2 keyed fake vLLM upstreams (401 without `Bearer <key>` from `$AVOS_GATEWAY_ENV`, as on the host); wrong key / key variable absent → `policy_unverified`; key and its name in no file or output; `policy_route.served` = [{upstream, id, root}] × 2 in fleet.json and matrix `inputs.policy_route`, a GET /v1/models on each; served name ≠ policy, two roots, non-models body, unreachable → exit 1 `policy_unverified`, no trial; resume under another root → exit 2; no table → served null | POC 12 lost 520 trials to a fleet that silently used the default model; base and retrained record the same served name, so only the checkpoint root tells them apart |
| F6 thinking optional | ruling 3 (§2) | thinking null on one run's rows → matrix accepted, cost = oracle, `thinking_tokens_missing` counted; input/output/cache-write null → `usage_missing` | billing needs the four billed counts; thinking is inside output; a null must be counted, never a silent 0 |
| F3 M12 from rows | done test 3; M12 | `usd_total` = Σ usage × cache-aware rates (1e-9), per task, per successful task (÷ pass1), uncached ceiling, per-trial `usd` + `usage`, `price` echoed; no cache rates → `unverified`; same-size-same-mtime row edit → new numbers | "a third party can recompute it from rows + prices alone"; the cache is not trusted |
| F4 open arms unchanged | done test 3 | an open synthetic fleet's matrix equals `fleet/tests`' oracle; basis `gpu_hours` | the open arms keep their GPU-hours basis |
| E1 export tags closed items | ruling 1 (§4) | label/tests' fixture env exports to the frozen POC 8 `dataset_sha256` without `policy_model`; a cloned run on the closed model → `policy_model` on that item only, every other line byte-identical; label: `closed_policy`, not kept, others unchanged | T1/T2 plant `policy_model` themselves; this proves the export emits it |
| T1 select refuses closed | done test 4 | closed fleet role (with either `--policy`) and closed items under an open fleet → exit 1 naming `closed_policy` + model, `DIR` absent | closed outputs never train, by name, before any rendering |
| T2 label never keeps closed | done test 4 | 12 closed items: 0 kept, rule `closed_policy`; a human pass does not flip it | the keep flag is what `select` reads |
| M1 m15 reproduces pooled | done test 5; M15, M11 | retrained = the two committed sessions with `--assert-served`, closed = closed-relabelled copies of the two base sessions (same n, c): pooled pass^1/pass^4/ci95/diff equal the oracle (1e-9) and `data/poc12/pooled.json` (3 decimals); sessions listed; verdict words present | the bootstrap, pooling and task order are fixed by the contract; the committed numbers pin them |
| M2 m15 closed arm | done test 5; M15 | with a token-priced synthetic closed arm: X, Y, intervals, per-session values equal the oracle; verdicts follow lo ≥ bar / hi < bar / straddle | the ratio interval is recomputed per resample, not derived from the two intervals |
| M3 m15 adversarial | done test 5 | task sets differ, split, simulator, duplicate (same path or same content), stored pass^k wrong, policy differs within an arm → exit 1 named; task 110 at n 3 listed in `tasks_short_4`, 43 tasks in X; all-zero closed arm → X null/`unresolved`/`zero_denominator`, no inf/nan | the cheapest wrong implementations: silent union of tasks, pass^4 of n<4 as 0, inf ratios |
| M4 m15 follows sources | CONTRACT §5 | a session rewritten under the same size and mtime changes pooled pass^4 and X | a cache keyed on (size, mtime) would answer from the old file |
| M5 m15 arm provenance | §5 class check | refused: base / open-route / route-less session as `--closed` (`closed_arm_not_closed`), closed session as `--retrained`, recorded root = `--base-root` (`retrained_is_base`), two roots, legacy without assertion (`provenance_missing`), assertion on a recorded session; exit 2: recorded without `--base-root`, assertion to `closed` or an unknown fleet; accepted: recorded (`provenance: recorded`, `served_root`) and recorded + asserted legacy (`asserted, not recorded` in sessions and `provenance.assertions`), numbers = oracle | `--closed` / `--retrained` are operator words; the session itself must prove them, and a legacy session enters only by a named, reported assertion |
| L1 live (`--live`) | done test 1 | a greeting-first history, then a real `get_weather` tool call then a text answer through the gateway path at effort medium, `max_tokens` 1024; rows: 200/200, five integer usage fields, prompt = sum, `msg_` id, hashes, no key | the fake cannot prove the Messages API accepts the translated multi-turn tool exchange |
| H1 route table (`--host`) | done test 2 | the host's live table sends `claude-opus-5-5` to api.anthropic.com with the pins | |
| H2 one closed trial (`--host`) | done tests 1–3 | one `fleet run --policy claude-opus-5-5` (task 0, 1 trial, slot vm-03) through the unchanged fleet: `fleet.json` pinned, every 200 policy row carries effort/max_tokens/usage with prompt = sum and `msg_` ids, simulator/judge rows present, `fleet matrix` basis tokens with `usd_total` = Σ rows at the fixture prices, an effort-edited copy refused, no key anywhere | the end-to-end path on the real mTLS/SPIFFE host; ≈ one Opus trial of spend |

## Fixtures (`fixtures/`, hash-gated by `MANIFEST.sha256` at the start of every run and in selftest)

| Path | What |
|---|---|
| `fixtures/prices.json` | `data/poc12/prices.json` plus `claude-opus-5-5` at the statement's list prices with cache rates (§3) |
| `data/poc11/p11-heldout/matrix.json`, `data/poc12/p12-heldout{,-base,-r2}/matrix.json`, `data/poc12/pooled.json`, `data/poc12/prices.json`, `data/poc11/prices.json`, `data/poc12/p12-heldout/fleet.json`, `data/splits/retail_user_disjoint_v1.json`, `fleet/tests/fixtures/results/run_retail-0_22/events.jsonl`, `label/tests/fixtures/expected_poc8.json` | real repo files hashed by the manifest: the M15 inputs, the numbers M1 must reproduce, the open-arm M12 regression, today's row shape |

Everything else is synthesised at run time by `lib.py`: closed-policy fleets built with `fleet/tests/lib.py`'s run
builder plus the §1.4 row fields, synthetic matrices of the real shape with chosen (n, c) and per-trial costs,
retrain exports and label rows cloned from `train/tests/fixtures` with `policy_model` set, test certificates
(`openssl`), scripted Messages replies.

## The oracles (`lib.py`)

- `m15_oracle`: pooling over sessions, `C(c,k)/C(n,k)`, `fleet/CONTRACT.md` §4's bootstrap (seed 300, B 2000,
  `means[50]`/`means[1949]`), ratio intervals by resampling tasks and recomputing the ratio with undefined resamples
  counted, per-task cost from per-trial `usd` or the GPU-hours split. Selftest proves it reproduces
  `data/poc12/pooled.json` (all 12 numbers to 3 decimals) and that one r2 session alone lists task 110.
- `closed_m12_oracle`: M12 from rows × prices with cache rates; selftest cross-checks it against a hand sum.
- `open_m12_recompute`: hosted roles and the token ceiling from `trials[]` parts; selftest checks all three POC 12
  matrices to 1e-9.
- `expect_hashes`: `trace/lib_trace.py`'s `content_sha(chat_form)` on the OpenAI shapes, the same code the witness
  check uses.
- The harness selftest drives today's gateway over mTLS on loopback end to end (forward, row of 27 keys, no-route).

## Ambiguities resolved here (see `eval/CONTRACT.md` for the full text)

- The route table names an Anthropic upstream with `kind: "anthropic"` and carries the pins; the fleet copies them
  into `fleet.json` from `$AVOS_GATEWAY_ROUTES` so a laptop test can plant a table.
- Empty content → `""`; tool calls without text → `null`; `refusal` → `content_filter`; raw `stop_reason` kept.
- A malformed upstream body is a 502 to the VM (no translation is possible), with `upstream_status 200` on the row.
- Closed = `claude-`/`anthropic/` prefix, a non-openai `policy_route.kind`, or `route_kind` on rows; items carry
  `policy_model` so `label` can refuse without seeing the fleet.
- M15 takes `fleet matrix` JSON files; per-task cost of a GPU-hours session is uniform (`usd_total / trials`), so
  Y's interval reflects the token-priced arm's spread plus the pass rates.
- `n < 4` after pooling: listed in `tasks_short_4`, excluded, never 0; zero denominator: `null` + `unresolved`.
