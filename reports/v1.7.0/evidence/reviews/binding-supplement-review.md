# Fixed-request supplement helper review

Reviewed the owned `run_v17_binding_supplement.py` before model calls. Final reviewed SHA-256: `e213e5b9727879ee4a050a250d2843afa0e64a70d59c69a4da5bb548368b70f1`. Production HEAD is `ef92f6b1b9f6bb70e8cad3957f98d59245429125`; all 64 production Python source hashes independently match the original registration. The helper is outside those production sources.

**No open P1 or P2 findings. Clear for the six-session supplement once the original acceptance job has completed.** Review performed no model calls, API calls, database writes, qualification changes, source edits, or broad test runs. Only this review artifact was added.

## Findings closed during review

1. The initial learned-arm success predicate did not require its computed direct-candidate evidence. The helper now requires `candidate_direct_success` for `rpe_learned`; that value includes exact frozen candidate code hash and version, verified workflow and business truth, and no candidate fallback. Wrong executor, wrong candidate identity, or fallback cannot yield learned success.
2. The initial aggregate `all(status...)` predicate could pass an empty or undersized trial set. The helper now rejects anything except the two exact case IDs and six unique trial IDs before calls. Final success requires exactly six records, the exact preregistered attempt-ID set, and all six statuses succeeded.

The native policy was clarified explicitly in preregistration, rather than treating an unverified native tool link as verified. Native success requires a fresh completed real model session, matching original prompt and frozen model identity, fixed-contract injection and input hash, and independently verified source/literal/product truth. Native invocation linkage is reported separately. RPE success additionally requires the exact required-contract gate and a fresh accepted run; learned success additionally requires the frozen candidate directly. The supplement can support six correct business outputs, four fully bound RPE workflows, and two direct frozen-candidate executions. It cannot claim six verified strict workflows solely from its aggregate passed field.

## Scope and control checks

- Before creating supplement state or making calls, `main` requires the original owned acceptance job to be completed. The reviewed original controller requires its formal comparison and real maintenance to finish before that status.
- A fresh supplement root rejects reruns. New labels create new per-arm workspaces; attempts enter a separate ledger with at-most-once phase reservations. Fresh session IDs and RPE run IDs are checked against both original and supplemental records. Existing original results are read, not replaced or rescored.
- Preregistration is sealed before the first API call and records helper SHA, environment, source registration/freeze hashes, known cases, exact six trials, fixed-request treatment for all arms, arm-specific success policies, timeout/attempt limits, and separate cost interpretation. It explicitly marks these as known-input retests, not new unseen-input evidence.
- Every actual launcher call passes the existing `required_request=request` API for all three arms. The binding check uses the launcher's actual `input_sha256` field. Original and actual prompt hashes are retained, so fixed-contract injection remains visible.
- The helper checks the existing freeze before each session and again on the acceptance path afterward. Source or frozen candidate changes and incompatible active-pointer changes reject acceptance through the existing freeze checker; it never substitutes, requalifies, or activates another candidate.
- Source measurement precedes output reading, avoiding the original missing-output ambiguity. Bounded regular-file reads, literal expected rows, source hash, and product verification are independently required. Exact Harness acceptance uses persisted native observations, the requested contract, and the durable run. Actual executor receipts, rather than the plan's ABI label, supply candidate evidence.
- Failed session-observation carriers retain safe observed usage and process cleanup metadata. Every supplement phase is API-expected and enters its own cost ledger; unavailable usage is not silently reported as zero. The final report retains the separate supplement cost and leaves original comparison scores intact.
- Each session is bounded at 180 seconds, with three task attempts. The helper has an 1800-second alarm, PID/start-identity state, and cooperative signal interruption. The existing launcher stops its owned host group and descendants in `finally`, including interruption paths.
- Results include hashes and whitelisted observer metadata, not raw chats, prompts, tool argument bodies, private provider payloads, or secrets.

## Offline verification

AST-only checks evaluated the final helper's acceptance expression and failure handler without importing/running its controller or mocking any API:

- learned direct-candidate success passes;
- wrong executor/fallback, missing output, missing verified workflow, and nonfresh run fail;
- native business success with invocation linkage false is accepted under its explicitly declared separate policy;
- a post-session freeze-check exception changes an otherwise successful result to failed and clears direct-candidate success.

The final aggregate predicate passed six correct records and rejected five records, zero records, one failed record, and a wrong attempt identity. **All 12 control assertions passed.** This verifies decision logic and reviewed byte identity, not actual runtime outcomes. Six real session records, their measured costs, and frozen-state checks still need inspection after execution; supplemental results must remain separate from the original natural-prompt comparison.
