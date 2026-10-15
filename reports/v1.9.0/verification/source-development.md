# T1 source requirements implementation

Commit: b43b34a (four owned files only).

Added strict full-source JSON parsing, deterministic pointer/value/source anchors,
fixed generated goal, complete extraction comparison, and a source-ledger store
whose public state hash binds the raw original document and unchanged inner v1.8
minimal state. Source and ledger are regenerated on capture/show/resolve; resolve
retains existing catalog trust, session and input-byte checks. Different original
whitespace creates a different clean identity without overwriting the first state.

CLI module supplies source prepare/show only; no second execution authorization.
Source file reads use the existing bounded nofollow regular-file reader and refuse
FIFOs without blocking. Root owns registration in cli.py.

API: source_document(raw, *, session_id, source_ref); interpret_source_intent(
extracted, *, source_document, catalog, workspace, session_id, source_ref);
SourceStateStore(db).capture(source, result, *, session_id, source_ref),
resolve(state_id, *, session_id, catalog_sha256), show(state_id, *, session_id).
Resolved metadata adds source, requirements and requirements_sha256. The source
object holds schema_version/raw/source_sha256/session_id/source_ref. Each requirement
has requirement_id/pointer/value/value_sha256/source_sha256/acceptance_field.
requirements_sha256 hashes canonical {source, requirements}.

Verification: initial RED was 26 missing-feature failures before production files.
Additional malformed-record and FIFO regression RED was 5 failures; final focused
GREEN: 68 passed (test_requirements.py, test_source_cli.py, test_intent.py,
test_address_cli.py), Python -B, pytest -p no:cacheprovider, owned t1 basetemp.
git diff --cached --check passed before commit.

At root's request stopped only own full-suite PID 63906/session 11839 with SIGINT.
Partial result: 751 passed, 5 skipped, 6 failed. Freeze checks failed while source
files were being edited in test_address_comparison.py:
test_bounded_launches_keep_failures_and_never_receive_standard_answer,
test_summary_reports_each_arm_and_only_comparable_completed_pairs,
test_complete_formal_budget_cannot_launch_a_thirteenth_session; and in
test_clean_comparison.py:
test_clean_budget_keeps_every_failure_without_retry_or_oracle_injection.
Two then-unfinished T2 tests failed:
test_clean_launcher_eager_profile_and_shared_public_system_contract
(prepare_home did not yet accept public_contract),
test_clean_rejects_hidden_contract_and_old_context_before_host_setup
(existing strict-entry error rather than expected clean-entry error).
Root will run one integrated full suite after parallel edits stabilize.

Limits: completeness is only for the entire supplied strict JSON document and
registered capability vocabulary. Arbitrary prose remains needs_context with the
original text preserved. Raw source is bounded to 64 KiB, source document/ledger
to 192 KiB; existing ready minimal state remains 16 KiB. Hashes provide immutable
record checks, not third-party authorship. No model/native run was performed.
