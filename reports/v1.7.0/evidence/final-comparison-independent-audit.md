# Independent audit of the completed v1.7 comparison

The actual closed loop is supported: native G0 and G1 writes, six trusted training runs and a prior-candidate preprobe, 13 dedicated qualification cases, activation of the exact G1 bytes, and a frozen candidate directly verified in 14 fresh sessions on previously unseen inputs. The original 48-session comparison completed. Two learned sessions did not meet its full acceptance chain, so all-evaluation-reuse is false. This establishes the mechanism; it does not establish a quality benefit or perfect unattended operation.

| Arm | Literal business truth | Verified workflow | Verified direct candidate | Physical frozen candidate execution |
|---|---:|---:|---:|---:|
| Native | 16/16 | 11/16 | — | — |
| Fixed builtin | 14/16 | 13/16 | — | — |
| Learned | 15/16 | 14/16 | 14/16 | 15/16 |

The design has six training inputs, eight distinct unseen input units, two dependent repeats, and three arms. The 16 arm sessions are not 16 independent statistical units. All 48 actual sessions are fresh, use Xiaomi MiMo v2.6 Pro, and retain their planned original prompts. Main actual prompt hashes equal original prompt hashes in every session. Corresponding RPE tool schemas match between the two arms. All 48 input source files independently retain their registered bytes.

G0 is 5,639 bytes, SHA-256 `bba5267b2efdb5df7b14b347ed3304c6493b3e54e05d6c29ea7ad3c97ca8d339`; G1 is 5,679 bytes, SHA-256 `3afd6be78a69caf7b055ae5afb8a69ef0deb7b542593dfd47f7838153cab9c43`. Retained model/API, native pre/post tool guard, argument digest, and verified physical write observations link each original file to its actual model session. G0's simple candidate.py/content argument reconstruction also matches exactly. G1's retained native argument digests match one another, but that simple reconstruction differs; deleted raw arguments prevent reconstructing the complete original G1 JSON. The actual physical content hash and byte count still match the exact retained original code.

G1's sealed training packet binds exactly six current CLI run IDs to their input, literal output, plan and verification hashes, with no holdout input hash. Both public training probes passed 6/6. Training behavior stayed 6→6; a new implementation of the existing tabular.deduplicate CSV ABI is not evidence of a new ability or feedback-driven quality improvement. Qualification's 13 named fixtures and splits match the dedicated source fixture list and database validation seal. The applied proposal, activation, active slot, freeze, and final stored bytes all bind version `adc6c2804d9d6356cef4ed40a28b2b552201f634e6a699eefcf09ca60b4f182c` to G1's exact hash. No production fallback occurred. Classification uses actual executor kind/version/hash; the legacy executor_id string alone is insufficient.

The five original failed scores are preserved:

- Learned Unicode r1 physically executed the frozen candidate successfully and produced correct business output. Its normalized constraints were empty, so the exact required contract chain failed. This is a missing workflow binding, not a candidate execution failure.
- Fixed CRLF r2 also produced correct builtin output with empty normalized constraints and failed the required contract chain.
- Learned Tab r1, fixed Tab r1 and fixed Tab r2 supplied literal backslash+t, code points [92,116], instead of TAB [9]. Each stored run rejected validation before any executor or artifact. Their source hashes are unchanged. An initialized source_preserved=false after absent output is not evidence of source mutation.

| Arm | Production tokens | All measured phase tokens | Production API requests | All measured API requests |
|---|---:|---:|---:|---:|
| Native | 924,040 | 1,178,963 | 124 | 162 |
| Fixed builtin | 134,259 | 134,259 | 37 | 37 |
| Learned | 123,501 | 152,218 | 35 | 39 |

All 74 measured phases have outcomes: 5 failed, 0 incomplete, 238 API requests, and **1,465,440 tokens**. Summed phase time is 2,182.928 seconds; observed makespan is 2,184.678 seconds. The learned arm uses 10,758 fewer production tokens but 17,959 more all-phase tokens than the fixed arm. Its one additional business/workflow success in this eight-unit sample, dependent repeats, and caller/policy/state differences do not establish benefit.

The auditor recomputed numeric provider usage and per-request model identities for the 48 production sessions and six native training sessions: 234 unique API requests. Four generator requests have deleted private databases; their retained aggregate arithmetic, records, ledger and actual API identities agree, but independent raw per-request recomputation is unavailable. Cache tokens are already included in prompt tokens and were not added again. The bounded live-generation snapshot contains no events and adds no coverage. Both post-production maintenance cycles used auto_apply=false and preserved their executor pointer and activation count; they reported remaining bounded maintenance work, so this is not exhaustive maintenance certification.

Earlier three attempts contribute a **771,888-token lower bound**, including the recovered 39,579-token interrupted call exactly once. Separate calibration adds 12,345; the main run brings the known sum to 2,249,673. The later binding supplement adds 187,870, giving **2,437,543 known tokens** for the round. The true whole-round total remains null because earlier unfinished/error observations are incomplete. Provider-billed money and development/agent token costs are unknown.

The retained exact-source regression is **1,324 passed plus 3 subtests**, no failures/errors/skips, in 297.21 seconds. Its log and XML agree. All 64 production Python files match source, installed package, wheel and commit `ef92f6b1b9f6bb70e8cad3957f98d59245429125`. Wheel SHA-256 is `12725219b551812367caff210eeceeffdfd3dcbcafd3152f56d71b7895ca0d26`. The auditor did not rerun the suite or execute/qualify/activate a candidate.

The main PID snapshot covers only its 54 retained session roots, excluding later supplement directories: 1,460 registered child birth identities and 1,515 queried child/parent/supervisor PIDs, none present. The final cleanup audit separately reports 2,212 registered identities, no surviving owned prefixes, no registry errors, and no experiment session IDs in the daily database. Deleted private-worker registries limit exact reconstruction.

Initial daily config SHA-256 was `763671d25a311906c1c071001298d75aac37b4cfed07070281b9b84778f5070f`; final preregistration, freeze, supplement registration, cleanup-after and current config all match `304448f85550e981e5cfb68498f3a14ed2692993e60ffe306c18b2101625cf97`. Thus hash checkpoints locate drift between the initial snapshot and final registration; they show no detected config drift across the registered final interval and do not continuously monitor it. All 48 actual model identities remain aligned. Daily .env matches its initial hash; config.yaml, auth.json and state.db differ from the initial snapshot. No cause is attributed and no global file was restored or modified by this audit.

This supports a local EXPERIMENTAL release with 14 verified new-input reuses. It does not support automatic promotion to daily use, a quality benefit, or a claim that the existing fixed-request API resolves Tab dispatch. Original failed/interrupted records remain unchanged.

Sources: [machine-readable independent audit](<owned-test-root>/evidence/final-comparison-independent-audit.json), [original final report](<owned-test-root>/evidence/final-formal-final.json), [binding supplement audit](<owned-test-root>/evidence/binding-supplement-independent-audit.json), [whole-round cost](<owned-test-root>/evidence/whole-round-independent-cost.json), [cleanup audit](<owned-test-root>/evidence/final-cleanup-audit.json).

