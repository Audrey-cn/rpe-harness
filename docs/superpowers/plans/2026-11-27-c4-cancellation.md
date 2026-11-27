# C4 cancellation implementation plan

**Goal:** Implement the user's v1.16.0 public cancellation and release requirements.

**Architecture:** A run is the existing durable public task. Cancellation and
publication acquire SQLite's write transaction before deciding their outcome.
Publication records success in that same transaction; its earlier durable intent
protects the crash window where a file was linked but the transaction rolled back.
Cancellation never removes output. Existing received/running/interrupted runs can
be cancelled; succeeded/rejected/failed runs retain their terminal result.

**Tech stack:** Python, SQLite, argparse, loopback HTTP, pytest and Ruff.

**Constraints:** Stay inside this repository; exclude `RPE推进计划/`; no push.
Immutable proposal confirmations must explicitly bind their own current digest.

- [x] Add failing storage/runtime cancellation tests, including concurrency,
  publication crash boundaries, leases, reasons and existing-database migration.
- [x] Extend the status constraint with a transactional, data-preserving migration.
  Add atomic cancellation, cancellation checkpoints and publication convergence.
- [x] Add CLI/API endpoints and tests for status codes, JSON and existing POST guards.
- [x] Cover proposal digest and session isolation with dedicated regression tests.
- [x] Add MIT license, Python 3.11/3.12 CI and version 1.16.0.
- [ ] Review the combined diff, run Ruff and the full Python 3.12 pytest suite,
  and record exact results and any environment limitations.

Cross-process cancellation fences publication immediately in SQLite. A worker
already preparing an artifact cooperatively releases its lease and staging files
at its next durable checkpoint; cancellation does not terminate arbitrary processes.
An interrupted publication with an existing output, an inaccessible path or a
changed output directory returns a non-destructive conflict. Legacy publication
intent without directory identity also retains its uncertain result. A stopped
attempt with a reserved recovery successor conflicts rather than promising to
stop that separate run; reservation and cancellation share the SQLite write lock.

Ruff passes. Full-suite verification remains incomplete because this execution
environment lacks openpyxl and denies loopback listeners, process inspection and
nested macOS sandbox execution. Exact receipts are in `reports/v1.16.0/validation/`.
