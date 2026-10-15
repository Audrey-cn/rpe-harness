# T3 clean comparison and portable evidence

Implemented only the owned experiment module, v19 script, and clean comparison tests. No model calls, daily Hermes changes, or old report changes were made.

## Behavior

- Default address registration and 1.8 APIs remain available. Clean profile uses six fixed pairs plus one separate pilot pair, maximum 14 host sessions. Reservations, errors, interrupted attempts and missing usage remain in the ledger and cannot replay.
- Four production sources are exact strict JSON roots with capability/constraints/execution; no generic instructions or oracle answers are appended. Two separate probes cover omitted keys and free text with an additional business condition.
- Both arms receive identical original bytes, files, skill snapshot and the same public capability contract legend. The exact formatted legend SHA is frozen and required for comparable observations. The helper must be the launcher interpreter.
- Clean state source/ledger is read from SourceStateStore and regenerated. Business truth, complete-chain acceptance, source coverage, native invocation and controller execution remain separate fields.
- Public export carries only public sources, regenerated ledgers, selected reverified artifacts, compact gate metadata and all cost phases. Offline verification uses no original DB/home/history and explicitly describes runtime identity as recorded checks rather than third-party cryptographic attestation.
- The v19 CLI rejects an old address registration instead of accidentally operating on its scores.

## RED/GREEN evidence

- Initial new clean tests failed because preregistration did not accept profile='clean'.
- Source-proof regression failed when a proof retained another state's original source after a mismatch; fixed to retain only the current public source.
- CLI boundary regression failed because the v19 command accepted an old address registration; fixed before model launch.
- Interrupted-attempt export regression failed because an incomplete record has no fingerprint field; fixed while preserving unknown cost and the reserved identity.
- A transient early fixture freeze failure was caused by concurrent source edits. Clean unit fixtures now freeze the real initial external metadata boundary; actual source-fingerprint behavior remains covered in the existing comparison suite.
- Final command: ROOT/venv/bin/python -B -m pytest tests/test_clean_comparison.py tests/test_address_comparison.py tests/test_cost_ledger.py -q -p no:cacheprovider --basetemp ROOT/t3
- Final result: 70 passed in 31.71 seconds. git diff --check passed. No full suite was run by this subagent, per root instruction.

## APIs

preregister_address_comparison(root, *, skill_root, hermes_root=None, helper_python=None, timeout=120, max_turns=8, profile='clean')

run_address_comparison(root, *, timeout=None, formal=False, max_sessions=None, runner=None)

export_address_evidence(root, destination)

verify_clean_evidence(destination)

scripts/v19_clean_comparison.py exposes preregister, run, summary, export and verify. Registration, summary, export and verify are no-model operations; only run invokes the supplied host runner.

## Scope limits

The public verifier recomputes source ledgers and business truth and checks recorded metadata links. It cannot authenticate a third-party runtime from locally generated hashes, reproduce private provider calls, or establish general semantic correctness for free text. The selected public fixtures are a bounded directed comparison, not a population estimate. Existing 1.8 records and scores are not replaced or merged.

## Pre-freeze wall-cost correction

Clean records now preserve the launcher's narrower duration as session_elapsed_seconds and set elapsed_seconds to the complete _observe monotonic duration, including launcher setup and post-session gate/oracle/source checks. The phase ledger therefore sums complete trial observation wall time. Common preregistration, development and testing remain outside this trial metric and are labelled for separate reporting. Original address timing behavior is unchanged.

A no-sleep fake-clock regression first failed with reported elapsed_seconds=1 although the complete trial took 5 seconds. After the fix it verifies session_elapsed_seconds=1, elapsed_seconds=5, and ledger elapsed_seconds_sum=5. An intermediate targeted run had 70 passes and one expected source-freeze failure because the final compatibility edit overlapped that run; the fresh stable rerun passed all 71 tests in 31.14 seconds.

Command: ROOT/venv/bin/python -B -m pytest tests/test_clean_comparison.py tests/test_address_comparison.py tests/test_cost_ledger.py -q -p no:cacheprovider --basetemp ROOT/t3-wall-final. No full suite or model calls were run for this correction.
