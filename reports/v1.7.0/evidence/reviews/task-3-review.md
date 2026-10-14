# Task 3 spec and quality review

Reviewed `90ec3bb..dd651c9` (873 insertions, 3 files), the Task 3 brief/report, and the updated 1.7 design. Read the provided diff once; inspected focused dependencies for the worker, fresh packet/probe, host isolation and receipt contracts. Cost-ledger implementation was not rescanned/reviewed. No model calls, broad regression, product edits or index mutations.

**Verdict: changes requested.** Two concrete failure-path issues were reproduced in isolated temporary directories below this ignored review workspace. A third finding concerns the explicitly requested mechanism-versus-quality distinction.

## P2 — Preserve the returned generation observations when fresh evidence validation fails

**Location:** `src/rpe_harness/experiments/learning_comparison.py:477-481`, with the exception reduction at `:181-184`.

`generation()` receives the full worker report, then calls `validate_training_packet()` again before returning it. If an input/result/DB evidence change is detected by this required fresh validation, `_phase()` catches the exception and replaces the report with only `status=failed` and `error_code=ValueError`. Already observed API/token usage, session/model identity, original code path and provenance no longer reach the phase ledger or compact record. The pilot then reports `G0_original_code_missing`, obscuring the actual evidence failure. This is the exact path where failed generation costs must remain attributable. A worker-side retained report is not incorporated into `summarize_learning`, so the comparison loses its known cost lower bound even though those observations exist.

Focused reproduction replaced only the host/worker boundaries and training seeding, returned a worker report with `known_total_tokens=123`, a session identity and retained-code path, then raised on the second fresh validation. The saved G0 result was exactly `{"error_code":"ValueError","status":"failed"}`; ledger usage was null and the pilot reason was `G0_original_code_missing`. No model or subprocess training was invoked.

Keep the returned report in a failure result when post-generation validation rejects it, preserving all observed usage/provenance while recording a specific fresh-evidence rejection. Test this post-return failure path separately from a worker that fails before returning observations.

## P2 — Validate the experiment root ancestry before creating experiment files

**Location:** `src/rpe_harness/experiments/learning_comparison.py:116-119`.

`Path(root).absolute()` does not remove traversal or reject symlinked ancestors, and `root.is_symlink()` checks only the final component. `mkdir(parents=True)` then follows a symlinked parent before any authority check. An operator selecting a cache/output path containing an existing symlink can therefore create the preregistration and arm/ledger/evaluator hierarchy outside the intended lexical owned root, before the stricter worker/probe validation is reached.

Focused reproduction created `review-temp/link -> review-temp/outside` and registered `review-temp/link/experiment`; it succeeded and wrote `review-temp/outside/experiment/preregistration.json`. Both locations were under the isolated temporary review directory and were removed afterward. This requires no adversarial concurrent mutation.

Use the same validated no-traversal, no-symlink directory authority approach already adopted for the Task 1 probe: verify the existing parent hierarchy before any writes, then create/open the final directory through that retained authority. Cover the symlink-parent rejection and absence of outside side effects.

## P2 — Separate mechanism evidence from perfect production performance

**Location:** `src/rpe_harness/experiments/learning_comparison.py:601-604`.

`mechanism_closed_loop_verified` requires 16 direct learned-candidate successes and model identity coverage across all 48 comparison sessions. A real, fully linked generated/qualified/frozen candidate that is directly reused successfully on 15 new-input sessions and fails on one is consequently labelled mechanism-unverified. Likewise, a native baseline API failure with no returned model identity can clear the mechanism flag despite all learned candidate invocations succeeding. The updated design explicitly separates existence of the real feedback→generation→qualification→freeze→new-input reuse mechanism from quality/cost benefit; it does not require universal production success to prove the mechanism exists.

Derive the mechanism flag from the complete pilot provenance plus at least one genuinely fresh, independently verified frozen-candidate reuse. Keep full-design completion, complete identity/comparability coverage and 16/16 direct success as separately named acceptance metrics. Preserve partial failures and measured rates; do not turn this change into a learning-benefit claim.

## Checked behavior and scope limits

- Six public training inputs and eight literal holdout truths are distinct; holdouts are materialized only at their scheduled session. Production registers 8×2×3=48 attempts and explicitly calls repeats dependent.
- Both RPE arms actually execute six CLI runs and rebuild fresh semantic packets; native training uses six actual host-boundary calls. No copied DB/fake-run seeding was found.
- G0 receives no semantic packet, its actual original code is probed, and feedback revisions receive packet plus previous code. Qualification results are not passed as model feedback. G0 and its probes are operational learned-arm costs.
- RPE production uses the same launcher/prompt/budget path; candidate success checks the strict durable receipt/run chain, literal output truth, product oracle, frozen executor version/hash, no fallback and fresh session/run identity.
- Original code/candidate/package environment checks are present; wheel installation should use a freshly registered environment because source/Python hashes are deliberately frozen.
- The 12 reported tests were read, not rerun. Their main orchestration test replaces the external host and intentionally has no valid RPE receipts; it does not establish an actual model-driven closed loop. The targeted reproductions above exercise failure paths absent from those tests.
- No claim of a proven learning benefit is introduced; AST difference and behavior improvement are kept separate. Actual model pilot/formal execution and the controller-owned full regression remain outside this review.

## Fix 1 and integration re-review — e208aef

**Verdict: approved for this scoped review; no remaining P1/P2 findings in the supplied fix.** Reviewed `task-3-fix1.diff` once, the Fix 1/integration report, the relevant worker provenance and qualification-return interfaces, and the existing final verification log. No model calls, new test runs, product edits or index mutations.

- The post-worker freshness rejection now preserves returned usage/code/provenance, records a specific evidence failure and stops the pilot before further generation/freezing.
- Registration validates absolute, traversal-free ancestry before writes, using the existing no-follow parent authority and final-component descriptor operations. The prior symlink-parent reproduction is covered by a no-side-effects regression.
- Loop verification now requires the linked G0/feedback/revision/qualification/activation evidence and at least one fresh aligned holdout reuse; complete comparison coverage and all 16 learned successes are separate metrics. New negative fixtures cover broken provenance, wrong candidate/model, missing feedback/qualification/activation and nonfresh/nonholdout reuse.
- Direct host observation failures retain the sanitized carrier's measured usage/identities while forcibly clearing invocation and candidate-success flags. The added tests cover both arms and missing/present observed usage.
- Native source exclusions now include development/primary checkout roots and preserve the minimal observer imports; the real sandbox regression verifies the separate-checkout case with the actual workspace cwd. Strict RPE imports retain their necessary trusted package access.

Read verified log: `<owned-test-root>/comparison-integration-final.log`: **31 passed in 8.23s**. The requested `evidence/task3/` subdirectory was not present; the report's root-level log path was present and was the evidence inspected. This approval concerns implementation and scoped regression evidence, not real-model pilot/formal completion.
