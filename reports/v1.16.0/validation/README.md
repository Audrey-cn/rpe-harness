# RPE-Harness v1.16.0 review receipt

Implementation is present on `codex/rpe-v1.16.0`, uncommitted. Release validation
is incomplete: the full suite is not green, and zero regressions are not certified.
No push was run and `RPE推进计划/` was not modified.

## Changes and decisions

- `storage.py`: auditable, idempotent cancellation; transactional migration of the
  existing status CHECK while preserving records, indexes, triggers and inbound
  foreign keys; coherent status/audit snapshots; safe abandoned-lease cleanup;
  attempted cancellation retains usage accounting.
- `runtime.py`: owner cancellation checkpoints; no-overwrite publication and its
  success receipt share a SQLite write transaction with cancellation. A durable
  publication intent survives owner crashes. Complete receipts, legacy/uncertain
  publication, inaccessible outputs and changed output-directory identity conflict
  without relabelling or removing published artifacts.
- `recovery.py`: retry reservation and cancellation share SQLite admission fencing.
  A reserved successor produces `recovery_started` conflict on the parent.
- `cli.py`, `server.py`: `runs cancel RUN_ID --db DB [--reason TEXT]` and
  `POST /runs/RUN_ID/cancel`. Success/idempotency is CLI 0 / HTTP 200; conflict is
  CLI 2 / HTTP 409; unknown runs are CLI JSON errors / HTTP 404. Existing POST
  JSON, Origin, length, size and deadline guards are reused. Cancelled execution
  replays return CLI 1 / HTTP 409.
- `proposal.py`, Hermes and broker decoders: propagate cancelled/exit 1, reject
  contradictory exits, and close strict native turns after durable cancellation.
  Optional mode and retries of failures retain their existing behavior.
- Existing immutable proposal confirmation already requires the explicitly
  accepted current proposal SHA256 and validates session and current source.
  Dedicated tests exercise stale/cross-proposal digests, changed sources, missing
  acceptance, cross-session use and retargeting. No mutable job abstraction added.
- Root MIT `LICENSE` (2026 Audrey-cn), Ubuntu Python 3.11/3.12 CI, both versions
  set to 1.16.0, and README lifecycle documentation.
- Required CI exposed pre-existing Ruff violations. Mechanical cleanup removes
  unused imports/bindings (retaining call side effects), expands inline statements
  and lambdas, and explicitly aliases imported pytest fixtures. Statement splits
  were checked for identical ASTs, and 123 affected fixture tests were collected
  with fixture resolution checked. Ruff's original E4/E7/E9/F selection is explicit
  so newer Ruff default rule changes do not expand this release's lint policy.

Cancellation is cooperative for already executing work: the database immediately
fences publication; the owner releases its lock and staging files at the next
checkpoint. Live foreign-process lease locks are preserved, and cancelled owners'
abandoned locks are reaped on restart. Cleanup failure cannot skip lease release.

## Verification

Python 3.12.15, pytest 9.1.1, Ruff 0.17.6. Commands ran with
`PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1`; no pytest cache provider.
The repository had no existing venv. Cached tools were copied into repository
`.venv`; installing dependencies failed DNS resolution for both the configured
package mirror and pypi.org. OpenPyXL remains unavailable.

| Check | Exit | Exact final output | Receipt |
|---|---:|---|---|
| `.venv/bin/ruff check src tests` | 0 | `All checks passed!` | [ruff.log](ruff.log) |
| Focused lifecycle/CLI/handler/protocol/runtime/recovery regressions | 0 | `156 passed, 30 deselected in 7.51s` | [pytest-lifecycle.log](pytest-lifecycle.log) |
| Confirmation rejection checks (worker success cases excluded) | 0 | `8 passed, 3 deselected in 0.14s` | [pytest-confirmation.log](pytest-confirmation.log) |
| `.venv/bin/python -m pytest -p no:cacheprovider` | 2 | `============================== 4 errors in 0.82s ===============================` | [pytest-full.log](pytest-full.log) |
| Additional diagnostic run with `--continue-on-collection-errors --tb=short` | 1 | `===== 170 failed, 1597 passed, 7 skipped, 459 errors in 144.91s (0:02:24) ======` | [pytest-available.log](pytest-available.log) |

Focused command:

```sh
PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_cancellation.py tests/test_cancel_interfaces.py tests/test_cancel_handler.py \
  tests/test_cancel_worker_protocol.py tests/test_runtime.py tests/test_recovery.py \
  tests/test_cli.py tests/test_json_runtime.py tests/test_hermes_broker_client.py \
  -q -k 'not test_http_'
```

The 30 deselected cases require actual loopback listeners; 30 separate raw HTTP
handler cases execute real parsing/dispatch/serialization over an in-memory byte
transport. Protocol tests run real fixed CLI cancellation replays, with only
process inventory and broker byte transport replaced; this does not verify real
process-descendant cleanup in this environment.

Confirmation command:

```sh
PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_proposal_confirmation_boundaries.py -q \
  -k 'not changed_review and not another_session'
```

The standard full command stops collecting `test_execution.py`,
`test_hermes_native.py`, `test_skill_dispatch.py` and `test_skill_inspection.py`
because OpenPyXL cannot be imported. The diagnostic run records missing OpenPyXL,
denied loopback binds, denied `/bin/ps`, denied nested `sandbox-exec`, dependent
assertion failures, and existing macOS temporary-directory assumptions. Its full
failure/error names and traces are retained, not reported as passing regressions.
It was started before the final strict-turn follow-up; the final focused receipt
includes that follow-up. GitHub CI and Python 3.11 have not been executed here.
The connected workspace verification service was also unavailable (HTTP 404).

A fully provisioned environment permitting these existing integration operations
must rerun the standard full suite before this release can be certified. No tests
were disabled to conceal these failures. `git diff --check` passed.

## Complete changed-file inventory

- `.github/workflows/ci.yml`
- `LICENSE`
- `README.md`
- `docs/superpowers/plans/2026-11-27-c4-cancellation.md`
- `pyproject.toml`
- `reports/v1.16.0/validation/README.md`
- `reports/v1.16.0/validation/pytest-available.log`
- `reports/v1.16.0/validation/pytest-confirmation.log`
- `reports/v1.16.0/validation/pytest-full.log`
- `reports/v1.16.0/validation/pytest-lifecycle.log`
- `reports/v1.16.0/validation/ruff.log`
- `src/rpe_harness/__init__.py`
- `src/rpe_harness/causal_graph.py`
- `src/rpe_harness/cli.py`
- `src/rpe_harness/evolution/worker.py`
- `src/rpe_harness/experiments/baseline.py`
- `src/rpe_harness/experiments/review_comparison.py`
- `src/rpe_harness/experiments/stress_test.py`
- `src/rpe_harness/integrations/hermes.py`
- `src/rpe_harness/integrations/hermes_broker.py`
- `src/rpe_harness/integrations/hermes_native_process.py`
- `src/rpe_harness/integrations/hermes_session.py`
- `src/rpe_harness/json_contract.py`
- `src/rpe_harness/proposal.py`
- `src/rpe_harness/recovery.py`
- `src/rpe_harness/runtime.py`
- `src/rpe_harness/server.py`
- `src/rpe_harness/skill_inspection.py`
- `src/rpe_harness/storage.py`
- `tests/test_asset_cli.py`
- `tests/test_asset_lifecycle.py`
- `tests/test_cancel_handler.py`
- `tests/test_cancel_interfaces.py`
- `tests/test_cancel_worker_protocol.py`
- `tests/test_cancellation.py`
- `tests/test_candidates.py`
- `tests/test_capability_catalog.py`
- `tests/test_cli.py`
- `tests/test_evolution.py`
- `tests/test_hermes_address.py`
- `tests/test_hermes_broker.py`
- `tests/test_hermes_gate.py`
- `tests/test_hermes_integration.py`
- `tests/test_hermes_json.py`
- `tests/test_hermes_producer.py`
- `tests/test_hermes_review_diagnostics.py`
- `tests/test_hermes_review_finish.py`
- `tests/test_hermes_review_recovery.py`
- `tests/test_hermes_skills.py`
- `tests/test_intent.py`
- `tests/test_json_candidates.py`
- `tests/test_json_runtime.py`
- `tests/test_learning_comparison.py`
- `tests/test_native_artifact_evidence.py`
- `tests/test_native_process_evidence.py`
- `tests/test_native_skill_receipts.py`
- `tests/test_native_staged_process_evidence.py`
- `tests/test_native_staged_review_evidence.py`
- `tests/test_path_binding.py`
- `tests/test_proposal_cli_integration.py`
- `tests/test_proposal_confirmation_boundaries.py`
- `tests/test_review_native_evidence.py`
- `tests/test_skill_catalog_archive.py`
- `tests/test_skill_inspection.py`
- `tests/test_skill_rpc.py`
- `tests/test_training_feedback.py`
- `tests/test_v115_native_finish.py`
- `tests/test_v16_cli_integration.py`
- `tests/test_v16_directed_probes.py`
- `tests/test_v16_entry.py`
