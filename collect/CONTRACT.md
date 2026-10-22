# POC 7 collect contract

The black-box interface `collect/tests/run.py` exercises. Everything below is required; nothing about internals
is. Serves: the five done tests of POC 7 (the audit question "what did agent X do inside the system"), M8 and M9,
and keeps claims 1 and 2 where POC 4–6 left them. Every stored event is a POC 6 event (`trace/CONTRACT.md`).

## 1. Command

`collect/bin/avos` — `sudo python3 collect/bin/avos <sub> ...`, Python 3.12 standard library, no network.
Exit `0` answered, `1` the store is unreadable or fails its own checks, `2` usage (no traceback on stderr, one line
naming the problem). Machine output on **stdout**, JSON or NDJSON as stated. `avos` reads the environment variable
`AVOS_TRACE_ROOT` (default `/var/lib/avos/trace`): with a non-default root every subcommand reads and writes only
under that root (used for the scale test, so it never touches the live store).

| Subcommand | Output |
|---|---|
| `trace --principal P [--day YYYY-MM-DD]` | NDJSON: the **audit projection** of POC 6 §8 for P: every stored event with `principal == P` plus every `halt`/`resume` whose `nodes` contain P, in `(ts, writer, seq)` order, each line the event plus its `authority` object (`{"authz_ref","decision","policy_ids"[,"receipt"]}` for `llm.call`/`tool.call`/`net.connect`; `{"via":"harness"}` for harness tool calls; `{"decision","policy_ids"}` for `authz.decision`; `{"receipt","by"}` for `halt`/`resume`; `{}` otherwise). `--day` keeps only events whose `ts` falls on that UTC day. No event of any other principal ever appears. An unknown principal answers an empty output with exit 0. |
| `trace --run R [--day D]` | NDJSON, same shape, every event of the run (§3), in `(ts, writer, seq)` order. |
| `completeness --run R` | JSON, the M8 record of §4. |
| `store` | JSON: `{"root": <dir>, "files": [<absolute paths, in order>], "events": <int>, "retention": {"rule": <text>, "keep_days": <int or null>}, "services": [<systemd unit names on the host that take part in collection, other than avos-gateway, avos-registrar and avos-spire-server>], "flush_ms": <int ≤ 5000>}`. |
| `import FILE [FILE...]` | Appends every event of a POC 6-valid trace file (already carrying `event_id`, `writer`, `seq`, `prev`, `hash`) to the store under its own writers. JSON `{"imported": <int>}`. Afterwards those events answer `trace` queries exactly like collected ones; a later import into the same root is answered too. Imported runs have no live counters, so §4 reports them `unverified`. |

**Answers come from the root's current content.** Any index or cache `avos` keeps is derived state: a `trace`
answer is the rows that are in the root's files at the time of the query (same `event_id`s, same `hash`es), whatever
was queried before, from this root or another, and whatever a file's size or mtime happens to be. When `avos`
cannot establish that, it exits non-zero with a message rather than answering from the cache; an empty or stale
answer with exit 0 is a defect.

## 2. Store

- Location: the directory `avos store` names (`/var/lib/avos/trace` by default). Its `files`, concatenated in the
  listed order, are one POC 6 trace: `python3 trace/bin/validate` and `python3 trace/bin/verify` pass on that
  concatenation with exit 0 at any moment the system is idle (no event half-written). Each `writer` keeps its own
  `seq` and hash chain across files.
- **Append-only.** Once an event is in a listed file it is never changed, reordered or removed: the content of each
  listed file at time t is a byte prefix of its content at any later time, and a file once listed stays listed (within
  retention).
- **Retention:** POC 7 keeps everything (`keep_days: null`). If a rule with a horizon is declared, nothing younger
  than the horizon may disappear, and the horizon is ≥ 30 days.
- **Flush:** an event is answered by `trace` at most `flush_ms` (≤ 5 s) after it happened on the host, including
  across `infra/vm destroy` of the VM it came from and across `systemctl restart` of every service in §1's
  `services` list plus `avos-gateway` and `avos-registrar`. A query asked after the VM is destroyed, or after the
  services restart, returns byte-identical output for a principal that had already exited.
- Tamper-proofing (signing, remote anchoring of per-writer `head`s) is out of scope; it would go on the per-writer
  heads `trace/bin/verify` prints.

## 3. Run

`R` is either a job run id `run_<task>_<trial>` or a VM id `vm-NN-xxxxxx`. A job VM lives for exactly one run, so
both name one VM life: the run's events are **every stored event with that `vm_id`** (VM, agents and sub-agents,
decisions, calls, controls that targeted or covered them, exits). `run_*` is resolved through the run's
`session.open`. An unknown run answers exit 0 with empty output (`trace`) or `status: "unverified"` (`completeness`).

## 4. Completeness (M8) per run

`avos completeness --run R` →

```
{"run": R, "vm_id": <vm_id or null>,
 "status": "complete" | "incomplete" | "unverified",
 "ratio": <float in [0,1]> | null,
 "stored":   {"authz.decision": n, "llm.call": n, "tool.call": n, "agent.spawn": n, "agent.exit": n, "halt": n, "resume": n},
 "happened": {"authz.decision": n|null, "llm.call": n|null, "tool.call": n|null, "agent.spawn": n|null,
              "agent.exit": n|null, "halt": n|null, "resume": n|null, "guest_requests": n|null},
 "gaps": [{"writer": w, "seq": s}, ...],
 "sources": {<counter name>: <where it came from, free text>}}
```

- `stored` is counted from the store. `happened` is counted from **counters that are not the store**: what the
  gateway accepted (`authz.decision`: one per request received for that VM, counted before it is decided or
  written), what it forwarded (`llm.call`, `tool.call`), what the registrar processed (`agent.spawn`, `agent.exit`),
  what `ctl` issued (`halt`, `resume`), and `guest_requests`: the number of requests the VM's own forwarder accepted
  from principals, counted inside the guest. A counter that cannot be produced (its component restarted during the
  run, the guest was unreachable, the run was imported) is `null`.
- `status` is `complete` only when every `happened` counter is non-null, `stored[k] == happened[k]` for every kind,
  `stored["authz.decision"] == happened["guest_requests"]`, and `gaps` is empty. It is `incomplete` when every
  counter is non-null and any of those equalities fails; `unverified` when any counter is `null`.
- `ratio` (`null` when `unverified`): per kind, the minimum of its sources over the maximum of its sources, summed
  over the seven kinds, with one extra unit in the denominator per `seq` gap. A kind's sources are `stored[k]` and
  `happened[k]`; for `authz.decision` also `happened["guest_requests"]`.
  `ratio = Σ_k min(sources_k) ÷ (Σ_k max(sources_k) + |gaps|)`, `1.0` when the denominator is 0. So `ratio == 1.0`
  exactly when `status == "complete"` (ruled 2026-10-22: the earlier Σ stored ÷ Σ happened ignored the guest
  counter). A run whose completeness cannot be established is **never** `complete` and never `ratio: 1.0`.
- Known gap this closes: a request lost between the forwarder and the gateway leaves no host row. The guest
  counter sees it: `guest_requests > stored["authz.decision"]` → `incomplete`. A guest whose counter did not survive
  (forwarder killed) → `null` → `unverified`.

## 5. Query latency (M9)

M9 = wall time of `avos trace --principal P --day D` as a fresh process, over a store holding ≥ 1,000,000 events of
the real kind mix on day D (imported through §1). **Cold** = page cache dropped (`echo 3 > /proc/sys/vm/drop_caches`)
immediately before; the very first query after an import may build an index. **Warm** = the same command run again
at once. Measured for the smallest, the median and the largest principal of the day. Output must equal the audit
projection computed independently from the imported file. Bars are in `collect/tests/README.md`.

## 6. What may not change

`just claim1` (8/8, including 20,000 gateway calls with 0 unanswered), `just poc4`, and `python3 trace/tests/run.py`
pass with collection on. Guest agent RSS and gateway call latency stay within the bars in `collect/tests/README.md`
(claim 2: collection must not buy auditability with density or latency).
