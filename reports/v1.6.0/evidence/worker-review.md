# Task 4 proposal worker — static review

Scope: `src/rpe_harness/evolution/worker.py` and `tests/test_worker.py` introduced in `608234d`, plus inspection of the invoked Hermes launcher's sandbox boundary. No tests rerun and no model calls. Supplied `worker/final.log`: 20 passed in 0.68s.

Spec compliance: the worker independently revalidates real matching CSV/JSON feedback, supplies at most eight bounded public summaries, skips the model without fresh evidence, provides existing producer ABI guidance, gates proposal creation on host completion and complete process/workspace cleanup, rechecks evidence after generation, and only calls propose_candidate. It neither validates nor applies the generated code. Candidate bytes are bounded and regular-file checked. Failure summaries avoid saving raw model prose or credential-bearing exceptions; finally removes the exact mkdtemp-owned workspace.

Code quality: generation, cleanup, post-generation evidence recheck and proposal persistence are ordered clearly. The missing isolation boundary below prevents accepting the claimed anonymous/no-heldout model-input scope.

## P1 — Bound model reads by the real owned-root, not just the worker subdirectory

Primary location: `src/rpe_harness/evolution/worker.py:219-221` (worker_root is allocated at `:204`). Supporting launcher locations: `src/rpe_harness/integrations/hermes_session.py:416-421` and `:158-170`.

The worker passes `owner_root=worker_root`, where worker_root is a new child of the real owned-root containing the feedback database. The launcher builds an allow-default sandbox and only denies unlisted reads within its supplied owner_root. Consequently the real feedback database and other evidence under the parent owned-root fall outside that deny rule. Native terminal/file tools remain enabled: from the workspace the model can inspect `../..`, open the real runs database, and read source/output paths and non-anonymous feedback. Copies of oracle/heldout sources under the outer owned-root (for example the baseline checkout in this task's existing root) are also outside the current-repository native_import_root denial. The prompt's request not to read other project files does not enforce this boundary.

Evidence: direct static dataflow from `root` -> `tempfile.mkdtemp(dir=root)` -> launcher owner_root, and launcher's `(allow default)` plus `(deny file-read-data (require-all (subpath owner_root) ...))`. Its credential deny applies to `.hermes`, and its native source deny applies only to the current import repository, so neither closes the parent owned-root access. No model or sandbox process was invoked for this finding.

Required correction: keep exact temporary ownership/cleanup while supplying a separate read isolation boundary covering the entire real owned-root (including feedback database, baselines and evidence), with only worker workspace/session/runtime files allowed. Ensure runtime allowlists do not re-allow the real evidence directory. Add a no-model sandbox regression that attempting to read a marker/database or heldout copy at the real owned-root fails, while feedback.json and candidate output remain usable.

Declined to judge: actual generated-code quality, provider behavior/cost, and the required real generation attempt remain separate acceptance work. Other general sandbox hardening outside this newly introduced worker boundary was not reviewed.

Verdict: changes required (one P1; no other P1/P2 findings in this bounded static review).

## Scoped read-isolation-fix rereview

Reviewed worker `read_isolation_root=root` and `deny_read_roots` plumbing plus the launcher profile construction. Worker ownership remains the unique temporary child for session creation and exact finally cleanup; the independent read boundary now covers the outer real owned-root. The actual feedback database and baseline/evidence siblings are outside the allowlist. Explicit denies additionally cover current runtime/source and the configured primary project source, preserving the minimal bootstrap imports. Proposal/cleanup/fresh-feedback gates are unchanged.

Inspected the real no-model sandbox test and supplied RED/GREEN evidence. RED allowed all four private reads; GREEN denied the real database, outer-root marker, baseline heldout marker and explicit separate source marker, while permitting public feedback reads and candidate writes. The same test records zero API requests, process cleanup completion and deletion of the exact session workspace. `maintenance/worker-isolation-green.log`: 21 passed in 0.92s. These tests were not rerun.

Updated verdict: scoped P1 fix approved; original worker read-isolation finding resolved, no new P1/P2 found in this scoped rereview. This remains a boundary review, not evidence of a real model generation attempt or generated-code quality. Arbitrary unrelated files elsewhere on the user's machine remain outside the reviewed isolation claim.
