# Skill comparison driver — independent static review

Scope: `src/rpe_harness/experiments/skill_comparison.py` and `tests/test_skill_comparison.py`, against the 1.6 experiment acceptance paragraph. No model launched and no suite repeated. Existing six no-model tests were inspected, not rerun.

Strengths: identical pair prompts/input hashes and skill snapshot, explicit policy differences, frozen preregistration, pre-invocation reservation preserving interrupted trials, individual durable records, exact typed/ordered artifact comparison, source-preservation check and usage retained on malformed output. Unknown token/cost data stays unknown. Supplying both arms the same already-installed openpyxl interpreter is an appropriate fairness correction: dependency installation is not the behavior being compared. The earlier 82-second dependency pilot may remain separate, visibly labeled and excluded from formal paired totals, provided the revised formal preregistration is frozen before starting either formal arm.

## P2 — Counterbalance paired arm order before treating timing/token differences as comparable

Location: `skill_comparison.py:180-184`; `tests/test_skill_comparison.py:37` explicitly fixes `['native', 'rpe'] * 12`.

Every case/repeat executes native first and RPE second. Interleaving ABAB never reverses order within matched pairs, so warm caches, provider conditions and setup order remain systematically associated with the RPE arm. The spec calls for alternating arm order. Freeze a counterbalanced schedule (for example AB in one repeat and BA in the other), retain both arms' identical pair prompt/input hashes, and update the test to verify counterbalancing. Until then these records are descriptive fixed-order trials; they cannot support an unqualified comparative efficiency claim.

## P2 — Include the specified refusal/necessary-context cases in the formal matrix

Location: `skill_comparison.py:91-93`, `:120-130`, and `:258-263`.

All six cases are fully specified successful XLSX reads (rows, sheet inventory or formulas); the runner always passes `context_sources=[]`. A cold lookup case exists, but there is no unsupported/refusal case and no task that requires source-backed missing-context recovery. Therefore 24 completed sessions would not satisfy the design's six-category comparison covering supported, cold and refusal/necessary-context scenarios. Add preregistered cases with independent expected outcome states and the required context inputs, or explicitly narrow the report to a supported-inspection microbenchmark and leave the broader acceptance requirement outstanding.

## P2 — Native acceptance must verify the requested skill/helper execution

Location: `skill_comparison.py:265-280` (especially `:279-280`).

The native gate only requires the string `skill_view` to occur among post_tool_call names. It does not check tool success, requested skill identity, source/content hash, paired pre/post call identity, or actual use of the original xlsx_read.py. A failed/unrelated skill_view followed by a correctly hand-written output satisfies this gate when the remaining result metadata matches. The benchmark prompt specifically requires loading the original XLSX skill and executing its original helper, and the reported task is a real-skill comparison; output equality alone cannot certify that behavior. Bind native successful loading to the selected snapshot skill and retain/verifiably inspect actual helper command/path/hash evidence. Add a no-model test with correct output and a failed/wrong skill_view event; it must remain unaccepted.

Bias assessment: the common dependency interpreter and explicit fixed-mode prompts are acceptable for a fixed-contract workflow benchmark. Mandatory RPE discovery/load/context/task versus optional native terminal/file is an intentional intervention, but must stay separately disclosed and cannot establish free-form intent accuracy or learned skill benefit. Catalog-cold has no native analogue, so it only proves RPE reachability and is not a symmetric cold-recovery comparison. The order imbalance, omitted outcome categories and insufficient native invocation gate above block using this driver as complete 1.6 experimental acceptance.

Additional frozen-source limitation: `_source_hashes()` currently does not include `xlsx_resource.py` (or a complete package manifest). If resource admission changes during formal collection, its change is not itself detected by the driver's frozen-source check. Freeze/verify the installed production package as part of experiment launch or include this dependency; do not describe the current selected-module map as a complete source freeze.

Declined to judge: real model behavior, actual provider usage completeness and runtime experiment outcomes have not been observed in this review.

Verdict: changes required for full experiment acceptance; three P2 findings, no P1 finding in the reviewed comparison code.
