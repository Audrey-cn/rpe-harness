# Task 1 independent review

Scope: commit `3e102c6`, catalog/inspection/oracle only, against `docs/superpowers/specs/2026-10-14-rpe-v16-design.md`. Read-only checkout review; no production changes or model calls. The existing `discovery-ready.log` records 190 passed; those tests were not rerun.

Spec compliance: snapshot/source hashes, explicit pinned adapter, advisory lookup, original helper bytes, fixed argv, source preservation, separate staging, and independent typed-value oracle are present. Resource-bound acceptance is incomplete due to the finding below.

Code quality: responsibilities are separated and failure paths generally fail closed. Independent producer/oracle implementations correctly avoid trusting producer summaries. Both implementations nevertheless place the expensive parser before their resource guard.

## P1 — Reject merged-cell expansion before loading the workbook

Location: `src/rpe_harness/skill_inspection.py:55-59`; independent counterpart `src/rpe_harness/skill_verification.py:58-63`.

Both functions call non-read-only `load_workbook` before checking the worksheet grid. During this call, openpyxl expands merged ranges into `MergedCell` objects. A roughly 4.8 KB valid workbook containing `<mergeCell ref="A1:XFD1048576"/>` passes the ZIP/member/byte guards and attempts to expand over 17 billion cells in the parent process before the 100,000-cell check is reached. The helper's subprocess timeout/RSS monitor cannot contain either plan construction or oracle loading. This can exhaust memory or hang the harness on an input that should be rejected promptly.

Evidence: a pure in-memory, bounded probe injected the above merge range into a generated XLSX, then replaced `Worksheet._clean_merge_range` with a sentinel exception at its entry to prevent allocation. Output: `zip_bytes 4814`; both producer and oracle reached `merged range expansion reached: A1:XFD1048576`. No dangerous expansion was executed. Existing sparse-cell tests only exercise large dimensions after ordinary cell loading and miss merged-range expansion.

Required correction: enforce expanded grid/merged-range limits before non-read-only workbook construction, or contain all parsing in a bounded process while retaining an independent oracle boundary. Add a regression proving rejection before merge allocation in both paths.

Declined to judge: native Hermes discovery/skill_view identity and end-to-end workflow are outside this module review and still under integration; this report does not accept them. Broad real-skill search quality and 24-session host experiments also remain separate acceptance work.

Verdict: changes required (one P1, no additional P2 findings in this bounded review).

## Scoped resource-fix rereview

Reviewed the new `xlsx_resource.py`, both preflight call sites, revision bindings, spec boundary update and adversarial tests. Supplied `discovery-p1-final.log`: 207 passed in 3.82s; no suite rerun. The original single-range expansion is now checked before openpyxl. XML byte/node/depth limits, DTD/entity rejection, external relationship rejection, custom-part discovery and namespace ambiguity guards are present. Producer and verifier revisions both incorporate RESOURCE_REVISION, so changing resource admission invalidates old plan/certificate revisions consistently. Sharing a resource-only guard does not couple the independent business-result oracle to helper output.

### P1 remains — repeated sheet aliases bypass cumulative expansion admission

Primary location: `src/rpe_harness/xlsx_resource.py:194-205`, together with target sets at `:107-109` and cumulative accounting at `:264-269`.

Workbook sheet entries may use the same relationship ID or different relationships that point to the same worksheet part. The guard records worksheet targets in a set and charges the physical ZIP part once. Openpyxl iterates the workbook's sheet entries and independently loads/expands that part for each sheet instance. Thus a 100,000-cell merged part can be referenced thousands of times while admission still reports a single 100,000-cell worksheet. The post-load grid check again happens only after the cumulative parent-process allocation.

Bounded proof: a 4,828-byte in-memory XLSX contained three named sheet instances sharing the same r:id/part and an A1:A100000 merge. `preflight_xlsx` passed and returned worksheets=1, grid_cells=100000, expanded_range_cells=100000. A spy replacing `_clean_merge_range` suppressed all allocations while recording three separate A1:A100000 expansion calls from `inspect_xlsx_bytes`; the loader returned sheets S0, S1, S2. This demonstrates the accounting mismatch without allocating the merged cells. Increasing the sheet-instance count remains below the XML node/byte budget but can exhaust the parent process before rejection.

Required correction: reject duplicate worksheet-part references for this bounded contract, or charge the full per-part grid/expansion for every workbook sheet instance before normal loading. Cover both shared r:id and distinct relationship IDs targeting the same part, in producer and oracle.

Updated verdict: changes still required; one P1 blocking resource admission. No additional P2 found in the scoped fix. The original direct giant-merge case is fixed, but the cumulative alias case is not.

## Final scoped alias-fix rereview

The guard now keeps `workbook_sheet_targets` and rejects any repeated canonical worksheet target before ordinary parsing (`xlsx_resource.py:200-203`). This covers both a reused relationship ID and different IDs resolving to the same target.

Re-executed the original bounded pure-memory alias probe against current source: the small ZIP had three named sheet instances pointing to the same A1:A100000 merged part. All three entrypoints (preflight, producer, independent oracle) raised `ValueError: workbook worksheet target is duplicated/shared`; the merge-allocation spy recorded `[]`. No merged-cell allocation occurred. The two new producer/oracle tests now use the relevant single-column workbook, avoiding accidental early rejection from an unrelated existing third column.

Updated verdict: Task1 P1 resolved; scoped resource fix approved. No additional P1/P2 found in this rereview. No full suite or model was run.
