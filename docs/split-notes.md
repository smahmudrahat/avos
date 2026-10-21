# Split notes (POC 0, 2026-10-18)

- τ²-bench pinned (all pins: `versions.md`) at `sierra-research/tau2-bench` commit `96a3ef92329baf908ffe9dfe188cea34e8701d2a` (2026-10-15). Verified fork compared at `amazon-agi/tau2-bench-verified` `864350a8971a8f8ee9e7b8472e2edc380a806b0c`.
- Clone both under `../third_party/` (outside this repo). Reproduce: `python3 eval/make_split.py ../third_party/tau2-bench && python3 eval/check_split.py ../third_party/tau2-bench`.
- Unit of grouping: the db.json `user_id` each task acts on (resolved from golden actions: user_id, email/name+zip lookups, or order owner; fallback known_info). Cross-checked: all 109 resolved users match the name/email/username in the task's own text.
- Result: 109 tasks, 52 users; train 65 tasks / 29 users, held-out 44 tasks / 23 users, zero shared users. (Official split: 74/40 and shares users.)

## Excluded (5 tasks)
| Task | Reason |
|---|---|
| 64 | Issue #499: gold `exchange_delivered_order_items` on a pending order (error swallowed). |
| 105 | Issue #499: gold exchange raises (gift card $17 < $21.10), so gold DB equals initial DB and the DB check rewards doing nothing. |
| 18, 49, 91 | Gold actions differ between sierra HEAD and amazon-agi verified fork: disputed, so dropped. |

## Deviation from the initial "exclude #499-flagged tasks"
Issue #499 flags 15 tasks. 13 of them (2,3,4,35,37,38,39,46,47,54,55,67,68) are stale read-only calls that cannot change the DB hash, so they are **kept**. Only tasks that can change a score (64, 105) are dropped. Verified-fork-only issue (task 21 TypeError) is not present in the sierra HEAD actions, so not excluded. Reverse this by editing `EXCLUDE` in `eval/make_split.py` and re-running.
Held-out target 44 is hit exactly. Fork also reworded instructions in 28 tasks; we use sierra HEAD wording.

## Decision (2026-10-18)
Keep the the 5-task exclusion (64, 105, 18, 49, 91). Split v1 is frozen.
