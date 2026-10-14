# Whole-branch review — 56b4451..35aeb4a

**Verdict: changes required before the real pilot/formal acceptance run. One P1 and one P2 remain.** This reviews implementation contracts, not a validated 1.7 release or demonstrated learning benefit.

Reviewed the design, plan, progress ledger/rulings, all four earlier review closures, and the supplied whole-branch package in bounded source/dependency passes. The review followed authenticated training export → original model code/receipt → training probe → independent qualification/activation → frozen production reuse, plus isolation and full-cost failure paths. Previous per-task findings are not reopened here.

## Findings

### [P1] Bound postlaunch reads so a changed native input cannot hang the entire experiment

**Location:** `src/rpe_harness/experiments/learning_comparison.py:391-395` (primary blocking read at line 391).

The native arm can write its workspace, including the input. After the host returns, `_host_session()` verifies the source with `source.read_bytes()` after checking only the final path's symlink status. If a failed or unsafe native command replaces `input.csv` with a FIFO, the parent opens it synchronously and blocks forever after the launcher's timeout/process cleanup has already finished. The exception handler cannot handle a read that never returns. The production phase never reaches its compact receipt or `ledger.finish`, so the already returned usage is not added to the known cost lower bound, subsequent trials do not run, and normal evidence export/cleanup cannot proceed. A large regular source is similarly unbounded; output hashing at line 393 also performs an unbounded read even when `artifact_matches()` has already rejected the output's 16 MiB size.

**Focused confirmation:** one no-model reproduction replaced only the host boundary. The stub returned a successful completion/cleanup structure and an 11-token usage fixture after replacing the source with a FIFO; actual `_host_session()` then failed to return within an independent three-second supervisor timeout despite its configured one-second host budget. The supervisor killed and waited for that exact child. This proves the postlaunch blocking path; it does not represent paid API tokens or a real model session. Evidence and the small reproducer are under `<owned-test-root>/review-whole/`, including `source_fifo_result.json` and `source_fifo_probe.py`.

**Minimal fix:** use the existing no-follow, nonblocking regular-file reader with explicit size bounds for source/output checks, and do not hash or run the product oracle on an already structurally invalid artifact. Read/size/type rejection should return a failed record carrying the host's usage/identity/cleanup facts. Add a narrow source-FIFO/no-side-effects regression, and cover an oversized output without reading its full bytes.

### [P2] Accept the actual verified native file-write event contract

**Location:** `src/rpe_harness/experiments/learning_comparison.py:342-343`.

`_native_links()` supports an output-producing `write_file` call but requires its persisted post-call status to be `succeeded`, `success`, or `completed`. The actual native observer leaves a successful `write_file` event as `status='ok'`; `integrations/hermes.py:568-571` translates that status only for terminal exit codes. Therefore a native session that reads the tiny CSV and directly writes the correct output with the file tool is classified as `invocation_evidence=False` / `workflow_verified=False` even when its API/pre/post IDs, argument digest, stream call and verified file-write result are all present. This systematically understates native workflow success for a permitted execution strategy. Business correctness is separately retained, which limits the damage but does not make the workflow classification accurate.

**Minimal fix:** recognize the real `ok` post-call contract for `write_file` together with the launcher's existing verified file-write observations (matching tool identity, arguments/content and target); retain terminal exit-code validation and reject missing or failed write proof. A focused observer-shaped test should cover successful and failed direct file writes. This finding follows the current producer/consumer source contracts; no second reproduction or model invocation was run.

## Contracts that held in this review

- The packet is freshly rebuilt from explicit verified run IDs, uses bounded public business fields and independent first-record truth, and rechecks evidence around generation. Original source/output paths and the real DB are denied to the generator.
- G0 and feedback generations retain original bytes, packet/previous-code bindings, actual training probes and conservative API/file-write provenance. Missing provenance remains unproven; an AST change does not become a behavioral or causal improvement claim.
- Candidate qualification and activation remain with the existing independent certificate/replay/CAS authority. Formal production checks exact frozen code/version and records fallback separately; quarantine does not authorize replacing the frozen candidate.
- Six native training attempts, six actual CLI runs for each RPE arm, and all 48 registered production attempts are structurally represented. Both RPE arms use the same strict launcher path; native policy differences and dependent repeats are explicit.
- Native/generator source exclusions, evaluator/records/ledger/registration isolation and one-at-a-time input materialization preserve the intended separation in the inspected launch path. Each arm retains its own prior workspace.
- Immutable phase reservations preserve failures and prevent silent retry replacement. Known usage remains separate from null totals, cache buckets, unverified billing and RSS. The prior terminal-session mismatch and safe diagnostic-carrier fixes are present.
- The final acceptance function separates one proven fresh unseen-input reuse from all 16 direct successes and 48-session/model alignment, while requiring the pilot provenance chain. The full formal schedule does not stop after one successful reuse.

## Scope and evidence limits

No product/index edits, model calls, broad tests, daily-file writes, private-key reads, new dependencies, remote actions or subagents were performed. The only execution was the bounded no-model postlaunch-read reproducer described above; its supervisor cleaned up the child process. Test counts and earlier focused pass reports were read as supporting evidence, not rerun or treated as real-model acceptance. The controller owns the ongoing full-source regression and installed-wheel/real-model gates.

The actual model-generated original program, delivered semantic feedback, independent qualification/activation, installed production bytes, full 48-session outcomes, final total costs and precise end-of-run cleanup still require execution evidence. No release validation or causal learning benefit is inferred from the source or focused tests. New hosts, multi-day maintenance and additional business capabilities are outside this release review; no style or architectural rescope requests are made.

## Scoped fix re-review — 00a6100

**Updated verdict: approved for the whole-branch implementation gate. Both findings above are closed; no remaining concrete P1/P2 findings in the supplied fix.** This supersedes the original changes-required verdict, without changing the real-model acceptance boundary.

Reviewed only `whole-fix.diff` and the Task 3 report's whole-branch follow-up. No tests, models, new reproductions, product/index edits or agents were run for this re-review.

- **P1 closed:** source and output are read through the existing bounded, no-follow/nonblocking regular-file reader. The source budget is the registered original byte length, and output is capped at 16 MiB. Rejected file types/sizes leave a failed record with returned usage/identity/cleanup facts. Only a bounded, literally correct output beside an unchanged source reaches the product oracle. The supplied supervised FIFO test checks prompt failure and retention of the 11-token fixture; the oversized sparse-file test checks that neither an unbounded read nor the oracle is reached.
- **P2 closed:** native `write_file` now accepts the actual `ok` status only with one verified write observation bound to the emitted/persisted call, argument hash, exact current output target, byte count and actual output bytes/content hash. Missing, failed, duplicate or mismatched proofs stay unverified. Terminal success additionally requires its observed zero exit code. The ten supplied observer-shaped cases exercise the real durable event store and write-result extractor, including the positive `ok` contract.
- **Business scoring is consistent:** primary `artifact_correct` now requires source preservation, literal row truth and product-oracle success. `artifact_rows_correct` remains a separate raw diagnostic. Invocation proof remains independent, so an otherwise correct native result without receipt linkage is not mislabelled business-incorrect, while source mutation cannot count as primary success. Both paths retain measured cost.

The implementation report records 45 focused passes in 8.64 seconds; that is reported verification evidence, not a rerun in this review. The controller still owns post-fix full regression, fresh installed-package checks, actual model provenance, all formal sessions, total-cost reconciliation and final cleanup. No release-validation or causal-benefit claim follows from this approval.
