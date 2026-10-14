# Task 3 independent review

Scope: commit `d41e239`, evolution cycle/store/maintenance against `docs/superpowers/specs/2026-10-14-rpe-v16-design.md`. The current working tree also includes the narrow host-error ordering/filter correction; finding line numbers below refer to that working tree. Read-only checkout review; no production changes or model calls. Existing `maintenance/final-tests.log` records 44 passed; those tests were not rerun.

Spec compliance: monotonic discovery cursor, waiting/pending/done records, bounded rotating rechecks, fresh original active evidence checks, ledger append, semantic deduplication and persistent ownership lease are implemented. Candidate certificates, activation lease and CAS remain intact. Terminal-work scheduling does not fully meet the requirement that current/terminal candidates consume no new-work budget.

Code quality: durable discovery-before-cursor advancement and enqueue-before-run-completion support replay. Active evidence is correctly checked independently of batch membership. The deferred-activation flag needs lifecycle cleanup on terminal branches.

## P2 — Clear deferred activation when a queued proposal becomes terminal

Location: `src/rpe_harness/evolution/cycle.py:367-370`, interacting with `:336-346` and `src/rpe_harness/evolution/maintenance.py:172-175`. (At committed d41e239, cycle positions are four lines earlier.)

A no-auto-apply cycle marks a validated candidate's queue row done with `deferred_apply=true`. If the candidate is then activated via the existing explicit `apply_candidate` API/CLI before the next cycle, the next auto-apply cycle changes the row back to pending. The terminal-state branch recognizes the now-applied proposal and marks it done, but preserves `deferred_apply=true`. Every future cycle reopens the same row; summary reports pending work forever. With `max_proposals=1`, this earlier terminal row permanently occupies the only selected queue slot and starves later proposals.

Evidence: a new temporary database under the authorized owned-root received one genuine successful json.normalize run; `cycle(auto_apply=False)` validated its default recipe, and direct `apply_candidate` succeeded. Three subsequent `cycle(max_proposals=1)` calls each returned `proposal_count=0`, `has_more=True`, and `pending_proposal_count=1`; the queue retained exactly one deferred row. Probe files were automatically removed. This was a focused API probe, not a rerun of the supplied test suite.

Required correction: clear deferred_apply on terminal/reused work and exclude terminal proposals from the new-work selection budget. Cover the explicit-apply-between-cycles sequence and ensure a following candidate advances with max_proposals=1.

Known regression: `test_argument_error_fact_reaches_existing_host_failure_diagnosis` was already assigned for correction. The working diff filters observation-only host receipts and processes real host failures first; this report does not duplicate that in-flight finding or claim its regression test passed.

Declined to judge: full Hermes workflow, proposal-worker model invocation, whole-branch integration and release acceptance remain outside these maintenance modules. The review does not treat module tests as those acceptance results.

Verdict: changes required (one P2, no additional P1 findings in this bounded review).

## Scoped rereview — 608234d

Reviewed `evidence/maintenance-fix-review.diff` without rerunning tests. The terminal branch now assigns `payload['deferred_apply'] = False` before persisting done state. Thus an explicitly applied or quarantined candidate cannot be reopened on every subsequent cycle; the permanent budget starvation and non-draining summary from the original P2 are resolved. The new parametrized regression covers both applied and quarantine states and checks two following cycles. Supplied `maintenance/review-fix.log`: 47 passed in 39.37s (existing evidence, not independently rerun).

The accompanying host-error correction skips observation-only receipts and emits actual host failures before run diagnoses, preserving the first host diagnosis's error facts without weakening semantic keys, evidence persistence, cursor transactions or activation gates. No new P1/P2 issue found in this narrow fix.

Updated verdict: scoped fix approved; original P2 resolved. Terminal rows may still require one housekeeping selection, but they do not recur or permanently consume future proposal slots. This is not whole-branch or real-host acceptance.
