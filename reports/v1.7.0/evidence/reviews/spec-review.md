# 1.7 design review — read-only, 2026-10-14

Scope: design, implementation plan, and SDD interface/rulings only. No product source audit, model launches, or test execution. User authorization stands; this review adds no permission gate. Findings below are specification ambiguities, not verified implementation defects.

The design correctly limits the claim to a new implementation of an existing CSV contract. It explicitly separates mechanism completion from quality/cost improvement, preserves original failures, freezes before evaluation, distinguishes fallback from candidate execution, and prohibits generator access to qualification/evaluation materials. The existing correct builtin therefore does not block this experiment, and G1 need not outperform G0 to report generation and reuse.

## P1 — G0/G1 is not an isolated feedback ablation

Clauses: design line 9 (G0 without semantic feedback followed by G1), line 21 (G0 control), line 29 ("G0/G1训练差异另作反馈消融"); plan line 37 ("G0无训练示例"), line 47; progress lines 25–26 (G1 receives examples/expected rows plus actual probe outcomes and previous code).

G0 and G1 differ in training examples, expected outputs, actual execution feedback, previous implementation, and an extra generation opportunity. A better G1 training score cannot identify execution feedback as the cause. Moreover, neither the builtin-versus-learned production comparison nor a G0/G1 training comparison demonstrates feedback-driven generalization. Selecting the first qualified G1 is legitimate eligibility selection, but does not repair this attribution problem.

Minimal correction: call the existing G0/G1 result a **diagnostic before/after reference**, not an isolated feedback ablation. Report exactly which inputs changed and restrict conclusions to a feedback-bearing generation process followed by qualified reuse. The formal learned-versus-builtin production comparison remains a same-strategy tool-state comparison; its cumulative cost view additionally includes preparation. Do not claim causal feedback benefit or unseen-input G1 improvement over G0. No extra model arm is required for this 1.7 delivery.

## P1 — Shared-training allocation leaves the cost comparison underdetermined

Clauses: design lines 29–31 (three arms, "每臂以相同训练及生产工作量归集；共同训练成本清楚分摊", research ablations separate but included in whole-run totals); plan lines 45 and 55; progress line 17.

The ledger phase inventory is good, but it does not state whether every strategy receives the full common acquisition cost in its own deployable-policy total, or whether one real acquisition run is divided across arms. It also does not distinguish G0 as a required precursor to the chosen learned strategy from additional research-only control work. Dividing necessary learning costs among the controls, excluding necessary G0 work as research, or counting a shared run repeatedly in the physical whole-run total can manufacture or obscure a cost advantage. An arm label on reserve() alone cannot resolve this.

Minimal correction before real execution: freeze a small phase-to-beneficiary/allocation table. Preserve one physical attempt ID per actual execution and count shared training once in the whole-run total. Define per-strategy cumulative totals separately: charge all necessary generation, G0 probing, failed retries, qualification, activation, and recovery to learned; state explicitly any counterfactual allocation of shared work in arm comparisons. Mark only genuinely additional research attempts as research overhead. Expose physical-run, per-strategy and production marginal views without summing incompatible views. Missing usage remains null with an observed lower bound. Native must execute its own actual training sequence; shared Harness/oracle training expense cannot stand in for native training.

## P2 — Retained native workspace makes repetition and novelty ambiguous

Clauses: design line 17 (8 unseen inputs, each with two host sessions, current input only), line 29 (native may retain prior workspace artifacts; 48 sessions; interleaved order); plan lines 15, 47, 54.

New host sessions do not imply fresh workspaces. If native keeps artifacts across the two repetitions, the second appearance of the same input is already known to that strategy. If only native retains results, repeat effects and persistent-state policy differ across arms. The current text also leaves the six training opportunities available to native unspecified. These conditions are acceptable as a declared strategy comparison, but cannot silently be presented as two independent fresh unseen-input replicates.

Minimal correction, as confirmed by the controller: all arms retain only their own chronologically accumulated workspace artifacts, cannot read each other's workspaces, and start fresh host sessions with unique input/output event IDs and paths. Preregister exact case/repeat order and training visibility. Explicitly label sequential reuse and distinguish first from repeat exposures; limit "unseen" to what was withheld from the generator. Treat the eight distinct inputs as sample units; two repetitions are repeat sessions, not statistically independent samples. This needs no increase in run count.

## P2 — Code difference is not evidence of changed behavior or feedback use

Clauses: design lines 7, 21 (same AST/comment-only changes cannot count as different behavior), 25, 35; plan line 37 (code/AST receipts); progress line 26 (actual feedback enters generation).

Different ASTs can compute exactly the same function. Prompt delivery plus new code hashes establishes exposure and provenance, not that feedback changed behavior. Conversely, G0 and G1 may both implement this already-supported CSV contract correctly. Rejecting that outcome as a failed mechanism solely because outputs do not differ would incentivize manufactured G0 defects, which the design rightly prohibits.

Minimal correction: use separate result fields for (a) feedback delivered and bound to a generation event, (b) a newly generated implementation, (c) observed G0/G1 behavioral differences on registered training probes, and (d) independent qualification plus actual frozen-candidate reuse. Only claim behavioral correction when a concrete same-input execution witness supports it. With no such witness, report feedback exposure, generated implementation and reuse, with no demonstrated behavioral change or feedback benefit. Preserve original candidate bytes and all probe outcomes; AST inequality alone cannot set a behavioral-change flag.

## Acceptance interpretation

The mechanism evidence chain should link real verified run IDs → exact authorized packet/hash → actual G0 probe facts → G1 generation receipt/code hash → independent qualification certificate → frozen version/hash → actual executor/candidate version on each current evaluation input → independent artifact truth. Distinguish direct candidate success, fallback success, business failure, and missing process evidence. Existing clauses largely supply this chain; the four clarifications above constrain its interpretation and accounting. They can be incorporated without extra model calls, additional experimental arms, a new execution authority, or a new approval gate.

Controller concurrence received during review: adopt diagnostic G0/G1 framing, equal within-arm retention with cross-arm isolation, eight independent input units, one physical common-cost entry plus explicit counterfactual allocation, and genuine native training. These rulings resolve the interpretation risks once transcribed into the cited design/plan clauses and enforced by the experiment driver; implementation was not inspected in this review.
