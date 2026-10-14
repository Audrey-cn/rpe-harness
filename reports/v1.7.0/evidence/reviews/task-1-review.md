# Task 1 review — 828ffc3..5a90d4d

**Spec verdict:** Core export/probe contract satisfied; root-path safety needs correction before acceptance.
**Quality verdict:** Request changes for one P2. No P1 findings.

Read the supplied diff exactly once, plus the brief, report, and directly relevant dependencies. Saved logs confirm the reported 32 focused and 68 dependency passes; no tests were rerun. Exact packet/probe fields match the report. Explicit authorization, selected 1–8 runs, fresh trusted-DB rebuild, rehashed-spoof rejection, raw/text/rows/packet budgets, BOM reconstruction, composite multiline truth, and failure categories without diagnostic text are implemented. Duplicate headers, empty keys, missing references and malformed parameters fail closed. Probe authenticity remains the caller's obligation through `validate_training_packet`; structural/hash validation alone does not establish authenticity. Candidate hashes and successful training probes do not establish learning or held-out generalization.

## Finding

- **[P2] Validate the entire probe-root path before creating directories** — `src/rpe_harness/evolution/training_feedback.py:205-206`. `root.is_symlink()` checks only the final component, then `mkdir(parents=True)` follows any symlinked parent before `_directory(root)` rejects it. A nonexistent root below a symlinked ancestor therefore creates a directory at the ancestor's target even though the probe fails. Relative/traversal roots can similarly create directories before rejection. Validate absolute/traversal rules and existing ancestors first, then create missing components through retained nofollow directory descriptors. Add focused coverage asserting rejected roots leave both alias targets and relative paths untouched.

## Fix round 1 re-review — 53595b2

**Updated spec verdict: Pass. Updated quality verdict: Approve. No open P1/P2 findings.** Reviewed only `task-1-fix1.diff` and the appended implementation report. The P2 is closed: absolute/traversal validation and the existing-parent nofollow walk now precede writes; final-component creation/opening uses the retained parent descriptor, and descriptors close on both success and failure. Missing ancestors are rejected without creation. Public signatures and packet/probe fields are unchanged. Saved evidence confirms four root-regression RED failures followed by 37 focused passes, including the new real macOS sandbox positive case. No suite rerun. The existing trusted-owned-directory assumption against arbitrary same-UID directory movement remains explicit.
