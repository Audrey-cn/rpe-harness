# Producer provenance and owned controller review

Reviewed commit `ef92f6b1b9f6bb70e8cad3957f98d59245429125` against `b7f3caf`, limited to `src/rpe_harness/evolution/worker.py` and `tests/test_worker_provenance.py`. Also reviewed the explicitly requested owned controller `<owned-test-root>/run_v17_final.py`. No source edits, remote calls, dependency changes, or additional broad test runs were made.

## Verdict

No open P1 or P2 finding in the committed producer provenance change or the corrected owned controller. The controller completion-status P2 described below was found and closed during review; closure evidence is appended at the end.

## P2: The controller ignores failed maintenance outcomes (closed)

Location: owned `run_v17_final.py:79-85`.

The controller runs two real maintenance phases through `_phase`, but discards their return values. Its final completed/failed decision checks only `comparison_complete` and `learning_loop_verified`. A busy maintenance lease, a maintenance exception, or a changed frozen pointer can therefore produce failed maintenance records while the overall job still exits successfully and records `status=completed`. This prevents that job status from proving the required real maintenance ran successfully and preserved the frozen executor pointers.

An offline AST-only replay of the actual `main` function supplied a valid frozen pilot, 48 completed formal sessions, a final verified comparison, and two failed maintenance outcomes. The replay performed no controller imports, model calls, database writes, or filesystem writes. It returned:

```json
{"exit_code":0,"job_status":"completed","maintenance_statuses":["failed","failed"]}
```

Capture both maintenance `_phase` outcomes and require `status=succeeded` and `frozen_executor_pointer_preserved=true` for each before setting the overall state to completed. Preserve the failed result in the ledger and report the job as failed when this required evidence is missing.

The initial preliminary concern about the `busy` field was withdrawn: the real `cycle` busy return includes both `state=busy` and `busy=true` (`evolution/cycle.py:174-183`), so the controller's maintenance helper detects contention correctly. The finding is specifically that `main` ignores that helper's failed outcome.

## Provenance checks

- The fallback is enabled only when the actual result has `entry_mode=producer`. Other entry modes still require the original CLI file-write observation linked by the supplied tool-call ID. Their verification conditions are unchanged; `proof_source` is an additive result field.
- The producer path requires exactly one stream argument record matching the candidate target and original code bytes, and exactly one native post-tool record matching all physical proof fields. Boolean `verified=true`, an integer byte count, and exact content, arguments, and target-path hashes remain mandatory.
- A missing CLI tool ID is resolved from that unique native record. Supplied conflicting or empty IDs fail; the implementation never creates an ID. The actual producer guard validates complete native identities before emitting the verified physical-write metadata.
- The selected native post still requires all five nonempty native IDs, one matching pre-tool hook with the same arguments hash, one matching post-tool hook, exactly one matching API pre/post pair, no matching API error, and exactly one matching model emission for that native tool ID.
- The producer API request observation must expose exactly `write_file`, named `write_file` choice, and `parallel_tool_calls=false`. Missing or conflicting request-policy evidence cannot close the link.
- Actual `_ProducerPolicy` proof reads a bounded regular file, matches its UTF-8 bytes with the requested content, checks the verified tool byte count, and checks file identity/stability. `observe_write` adds those fields only after its pending physical proof and unique hook chain agree. The new regression helper exercises this middleware, the physical candidate file, hooks, and durable metadata; it transparently represents the upstream model response locally.
- The new rejection cases cover absent mode, wrong/empty CLI IDs, changed stream bytes/path, duplicate streams, invalid verification and byte-count types, all three wrong hashes, missing native ID, wrong session, tool failure, duplicate/ambiguous native posts, missing/duplicate/pre-hash-mismatched hooks, missing/duplicate APIs, missing/duplicate model emission, API errors, and conflicting request controls.

## Verification evidence

Independently inspected the root-owned logs. `evidence/provenance-red.log` shows both intended positive cases failing before the change while 32 negative cases pass. `evidence/provenance-green.log` reports **148 passed in 10.23s**, with no skipped-test outcome. This is the implementer's inspected focused-run evidence, not a new reviewer test run. The root supplied the final commit identity before the final review.

The committed provenance fix is suitable for the next installed-package calibration. It restores linkage from independently verified native events when CLI IDs are absent; it does not itself establish a real model generation, candidate acceptance, or learning benefit. The owned controller P2 should be closed before treating a final completed job status as full acceptance.

## Controller closure verification

The root agent corrected the owned controller to retain both maintenance `_phase` outcomes, require `status=succeeded` and `frozen_executor_pointer_preserved=true` from each, and include the resulting `maintenance_verified` value in both the saved final report and owned job state. Overall completion now also requires this value. Independently reviewed controller SHA-256: `74c6dadc1d0fa4294cf4be324a13734cd7297a76a35de62a5952e1247e4771ab`. Production HEAD remained `ef92f6b1b9f6bb70e8cad3957f98d59245429125`.

Replayed the updated actual `main` AST offline with a valid pilot and 48-session verified comparison, using no imports, model calls, database writes, or filesystem writes. All four assertions passed:

| Maintenance results | Exit | Job status | State and final maintenance_verified |
| --- | --- | --- | --- |
| Both succeed and preserve pointers | 0 | completed | true |
| Both fail | 1 | failed | false |
| One fails | 1 | failed | false |
| One reports a changed pointer | 1 | failed | false |

**Controller P2 closed. No remaining P1 or P2 findings in this scoped review.** The reviewed commit and corrected controller are clear to proceed to installed-package calibration and real acceptance. The offline evidence verifies provenance checks and controller decisions; real model generation, all production sessions, and successful maintenance still require their actual runtime records.
