# Actual fixed-request supplement diagnostic

The six-session supplement failed: **4/6 business outputs succeeded**. All three Unicode sessions passed; only native passed TAB. The existing fixed-request API correctly refused malformed RPE arguments but did not solve TAB parsing or repair within the configured attempt budget. Original and supplemental results remain unchanged.

## Bound TAB admission and repair

The required delimiter is one U+0009 TAB. In both RPE arms, attempts 1–3 instead supplied literal backslash-plus-`t`, code points 92 and 116, length 2. Both required preservation constraints were present. Every other normalized field agrees with the fixed request.

This was established from metadata alone: each persisted receipt contract SHA-256 exactly matches the public required normalized contract with only that delimiter substitution. No raw chats or private tool bodies were accessed.

| Arm | Attempts 1–3 | Later attempts | Actual executor runs/output |
| --- | --- | --- | --- |
| rpe_no_experience | `request_contract_mismatch`, over-escaped TAB | Attempt 4 remains over-escaped; `task_attempt_limit` | None / absent |
| rpe_learned | `request_contract_mismatch`, over-escaped TAB | Attempts 4 and 5 match the exact one-TAB fixed contract, but return `task_attempt_limit` | None / absent |

The learned arm eventually repaired the argument, but after consuming its three permitted parameter attempts. The admission guard correctly kept the exhausted budget authoritative. This is a late repair, not a successful execution or candidate reuse.

All native request observations for these failed sessions retained `tool_choice=rpe_task`. Because admission rejects contained no durable executor `run_id`, the strict terminal detector had no accepted durable outcome from which to switch to `none`. The attempt guard still denied subsequent calls. Thus three task attempts bound admission; they did not cap upstream tool emissions or API requests. The fixed session recorded four model tool emissions over five API requests; learned recorded five over six. Neither crossed the admission boundary.

This differs from the original natural-prompt TAB failures. Those produced a durable validator-rejected run and then closed entry with `none`. Fixed-request binding moved the failure earlier, to contract admission, allowing within-budget argument repair. In this supplement neither arm supplied the correct argument within that budget.

## Source, output, and final gate

Both RPE input files are stable regular files and still match fixture SHA-256 `e5de589799df43d63f9c0faedc0eab76f395fe10441c9843252b669428e5f93d`. Fixed-contract injection and the bound input hash were positively recorded. Both requested output paths are absent.

The helper's source-first check correctly reports `source_preserved=true` even though output reading raises `FileNotFoundError`. That exception kept the result failed; the final `evaluate_session` call was not reached for these absent-output records. The recorded receipts independently establish admission rejection. No learned candidate or built-in executor ran, so these TAB outcomes do not establish a producer-code failure.

Unicode does provide a separate positive observation: native business validation passed, the fixed RPE workflow passed, and the learned workflow passed with the exact frozen candidate hash/version, no fallback, and fresh session/run IDs. This is a known-input diagnostic success, not a new holdout result or a rescore of the original Unicode session.

## Delivery scope and cost

The original formal report independently records 48 completed sessions, completed comparison, verified maintenance, a verified learning loop, and **14 verified reuse sessions on new inputs**. `all_evaluation_reuses_verified=false` and `learning_benefit_established=false`. These 14 sessions do not mean 14 distinct input cases, and the known-input supplement does not add novel-input evidence.

Experimental v1.7 can claim the implemented generation/feedback/qualification/activation/reuse loop and observed fail-closed contract admission. It cannot claim full workflow reliability, that fixed binding solved argument parsing, or an established quality/cost benefit. No source fix or additional model call was performed during this diagnostic.

The supplement ledger records 27 API requests and 187,870 tokens. The current whole-round cost summary records a known lower bound of **2,437,543 tokens**, with `total_tokens=null`; its recorded component lower bounds sum to that number. Earlier missing observations/incomplete usage, billing cost, and developer/orchestrator cost remain unknown. This diagnostic checked that summary's arithmetic, not a second full ledger audit, and makes no cost-saving claim.

Full metadata, receipt hashes, per-attempt policy/error outcomes, immutable result hashes, formal scope, and cost-summary provenance are in `binding-supplement-contract-diagnostic.json` beside this file.
