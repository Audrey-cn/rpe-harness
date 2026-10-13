# RPE-Harness 1.2 independent review — final

**Conclusion: all reported code findings are closed. No remaining release-blocking code defect was found in this focused review scope.** This conclusion applies to the frozen source snapshot recorded in `../recheck/reviewed-source-sha256.json`; its 31 Python file hashes remained identical before and after both final verification runs.

Product source was reviewed against baseline `e1f4cb0279d2577d8f9102e32d0f180a94aa00ef` and the 2026-10-14 v1.2 design/plan. This reviewer made no product edits, staging, commits, daily Hermes writes or model calls. All independent test artifacts are under this review root. The original findings and unsafe-behavior reproductions remain in `independent-review-initial.md` and `test_adversarial.py` for traceability.

## Closed findings and observed final behavior

| Finding | Final behavior verified independently |
| --- | --- |
| Activation certificate mutation race | A mutation at durable intent is rejected and leaves the prior pointer unchanged. The final reload/check/CAS executes under one `BEGIN IMMEDIATE`; a separate SQLite writer is actually locked during the final certificate check. |
| Malformed active certificate JSON | Candidate is quarantined, active pointer cleared, and a real next task succeeds with the registered builtin producer. |
| Corrupt rollback predecessor | Invalid predecessor is quarantined; rollback atomically restores builtin and records the invalid-target reason. |
| Incomplete self-consistent certificate | Clearing fixture checks/counts and consistently recomputing public hashes/version identity is rejected without switching. Exact expected fixture names, splits, preservation predicates and resource metadata are checked, followed by actual acceptance replay. |
| Native control observer import denial | Actual minimal-import macOS sandbox computes tool-schema metadata successfully without importing the barred gate/oracle module. |
| Installed package oracle exposure | Actual macOS profile permits a broad readable prefix but denies the package's oracle root; the read fails with `PermissionError`. |
| Fresh replay falsely rejects valid error diagnostics | A semantically valid candidate that explicitly rejects empty keys validates and activates. Replay compares stable fixture truth predicates; differing temporary traceback paths remain diagnostics. |
| Paired experiment aggregation admits incompatible pairs | Token/time paired subtotal excludes sessions unless model identity, prompt and input equality are all proven true. False and unknown proofs are excluded. |
| Strict replay metadata conflict | Rephrasing goal metadata preserves the first durable payload and reuses the same run for the same strict contract. |

Relevant final implementation: `evolution/candidates.py` complete-certificate predicates, acceptance replay, transactional activation, guarded candidate loading and rollback; `evolution/sandbox.py` explicit package-root deny and disclosed RSS monitor; `integrations/hermes.py` inline observer digest and metadata-only replay; `experiments/hermes_repeated.py` proven-compatible pair filtering.

## Verification

- **7 independent correct-expectation probes passed in 4.63 seconds**, under `../recheck/pytest-own-final`. They cover coordinated incomplete-certificate hashes, malformed JSON plus actual builtin execution, intent mutation and transactional writer exclusion, corrupt predecessor rollback, real control sandbox, real prefix/package deny, and valid explicit empty-key rejection. Source: `../recheck/test_recheck.py`.
- **17 focused project regressions passed in 4.76 seconds**, under `../recheck/pytest-targeted-final`. They include actual owner exit before and after activation commit, production-only bad candidate quarantine plus same-plan builtin verification, stale-base CAS rejection, soft RSS supervision and disclosure, strict goal rephrasing, native control sandbox, pair compatibility filtering and retention of all 36 synthetic attempts, including a failed launch, in the orchestration test.
- Final commands used the turn-owned Python venv, `PYTHONPATH=src`, `PYTHONDONTWRITEBYTECODE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `RPE_HERMES_SOURCE=/Users/audrey/.hermes/hermes-agent` and `pytest -p no:cacheprovider`. All basetemp paths were unique and inside `review/recheck`.

These passing checks establish the actual rejection, preservation, fallback and recovery behavior described above. They do not substitute for real-provider or release-package evidence.

## Remaining scope and evidence limits

The independent production artifact oracle remains the decisive acceptance layer. Fixed manual CSV truths cover first-record semantics, text/type distinctions, composite keys, order, quoting/multiline records and preservation/no-overwrite. Some invalid-input fixtures reject before executing the candidate; they test the admitted execution procedure, rather than proving candidate-local validation. The production-only bad candidate regression independently shows that passing the fixed suite does not bypass the later artifact oracle.

The SQLite database and trusted parent process are trust roots. Public content hashes and consistent redundant fields cannot authenticate arbitrary coordinated rewrites by an actor who already controls those trust roots. Candidate filesystem/network isolation now denies the actual package oracle root, but the macOS RSS monitor is a soft threshold: instantaneous allocation peaks have no hard memory guarantee. The validation certificate discloses this limitation.

No new finding requires a code release block in this focused review. Root must finish and assess its full regression, exact wheel/install verification, real provider policy evidence, final preregistered 36-session comparison, post-publication reconciliation, owned-process cleanup and daily-state checks. An aborted pilot should remain separately identified and must not be mixed into the final fixed session budget. No claim of token savings, causal-graph benefit, arbitrary self-evolution or general-agent superiority follows from these implementation/recovery tests.


## Installed wheel owner-crash verification

Additional independent release evidence: `../installed-crash/installed-crash-evidence.json` records **3/3 actual owner-crash probes passed** using the owned `release-venv/bin/python -I -B` entirely outside the checkout. Imported contract, executor and recovery module paths and SHA256 values identify the installed 1.2.0 package. The reusable fault-injection/public-CLI script is `../installed-crash/reproduce_installed_crash.py`.

After execution and after independent verification but before durable publication intent, a real `os._exit(73)` leaves an interrupted pre-publication attempt. Public `recover` succeeds under the original plan, the independently authored Chinese/leading-zero/multiline fixture is correct, repeated recovery returns the same successor, and original event replay returns its same interrupted record without rerunning work. These cases contain exactly two durable runs each.

After actual publication but before recording completion, a real `os._exit(73)` preserves the explicit interrupted/uncertain state. Public recovery is refused twice with exit code 2 and the publication-uncertainty reason. The published output hash stays unchanged and the database still contains one run. Source bytes remain unchanged in all three phases.

All 18 known children in the final probe completed; owned orphan staging files were removed only after process exit, with published-output hash preservation checked afterwards. The first local probe incorrectly expected CLI exit code 3 for replay of an interrupted record; the public interface returns 1. The assertion was corrected, the three probes rerun successfully, and the initial assertion record is retained separately as `../installed-crash/probe-initial-assertion-record.json`. This was a reviewer script issue, not an installed product defect. No model/API calls, production-source modifications or daily-state writes were performed.
