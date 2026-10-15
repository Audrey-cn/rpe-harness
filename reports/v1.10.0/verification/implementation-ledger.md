# SDD ledger — plan: docs/superpowers/plans/2026-10-15-rpe-v110.md

Spec: docs/superpowers/specs/2026-10-15-rpe-v110-design.md
Base: c6a8075066544ad01260d6af2714a00426754baa
Owned root: /Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2
Worktree: /Users/audrey/.codex/worktrees/rpe-v110/rpe-harness

|Pair/task|Producer/consumer agreement|Result|
|1/2|create_proposal(raw,draft,context) persists source separately; Hermes proposes only|Aligned; model confirmation absent|
|1/3|show/confirm/execute on exact immutable proposal; simulated reviewer never repairs|Aligned; benchmark must report simulated review|
|2/3|run_hermes_review plus actual ingress/RPC proof, same public contract native|Aligned; concrete result keys communicated during implementation|
|1/4|register_review/dispatch_review; root owns cli.py|Disjoint files; friendly output requires root dispatcher support|
|2/4|review added launcher/bootstrap/broker; root CLI entry choices|Disjoint files; wrapper used by CLI|
|3/5|freeze before model calls; bounded 21 total host sessions|Aligned; no paid calls during implementation|
|1|Bounded drafts, typed fields all mapped, unknown/missing blocks; tests match APIs|Consistent|
|2|Only proposal tool, no clean automatic controller; SDK guard tests|Consistent|
|3|Separate review comparison; original evidence untouched|Consistent|
|4|Full tests and independent review once stable; build installed wheel|Consistent|
|5|Native two-phase workflow vs one review model phase + deterministic run|Consistent; compare whole policies and include all phases|
|6|Keep public evidence; cleanup owned state only; local release/archive|Consistent|

Baseline: root clean except two existing report directories; ownership.json stores hashes and repo status before work. No daily configuration modified.
Tasks 1-3: dispatched independent implementers, no further child agents or paid model calls. Root owns integration and final full-suite run.

Baseline verification: tests/test_requirements.py + tests/test_intent.py, 61 passed in0.55s.
Root CLI integration: initial public review help tests RED (unknown command); GREEN after registration. Added execution exit-code regression, RED (failed run returned0), GREEN after limiting preparation status handler to non-run. Final root CLI + core CLI check10passed in0.51s. Root integration commit recorded in git.
Review requests during implementation: preserve failed post-claim admission terminal state; align native/review public capability guidance; count formal and whole-run costs separately and report driver overhead. Implementers handling these before freeze.

Task 1 implementation complete:89a0353,105 targeted old/new core/CLI/strictsource tests passed; independent review in progress. Contains bounded perDB worker resource cap, no new dependency. Root corrected an unsupported startup-recovery justification after inspecting storage.py: live leases are already honored; no recovery misclassification was reproduced. The serialized resource cap is a deliberate local concurrency tradeoff, not a repair to the old lease mechanism.
Task 2 implementation complete:b45b0dc,23 new tests and92 adjacent checks passed. Actual installedSDK MockTransport saw20 blocked attempts and0 real networkcalls. Independent review in progress.
Task 4 rootintegration:bda2d80,10 CLI tests passed. One external independent reviewer dispatched; wholebranchpackage review-c6a8075..89a0353.diff created.

Pre-pilot complete verification:1605pytest+3subtests,0failure/skip,359.72s. Installed76module byte equality,101targeted+2actualSDK import-location checks. Frozen registration da795cadba557f39c1baa1153b2106138fa550cb5739c9513c5e6ec4372b8bcf.
Actual pilot:2draftsessions,9logicalAPI,72390tokens; both acceptance chains failed, no execution. Native completed valid draft after patch but narrow finalwrite parser unrecognized; review wrapper/reevaluation ValueError. Original pilot evidence exported and offline verified, never rescored. Formal paused before any calls. Independent implementers diagnosing real persisted boundaries without extra model calls. Total call cap remains14draft+<=7nativeexecute across all registrations; corrected formal will use12draft only, no extra pilots.

Correction after actual trace inspection: native patch touched scratch/check_draft.py, not draft.json. The correct single draft write was1839bytes SHA79d83cd7588f9e94584a8140da4a30446861ede6a0fec9e54fc5d1c0c44ae225. stdout tool_use/result have no tool_call_id; old writer-fact collector therefore returns[] although strict durable API/tool/args links pass. Native exact typed candidate is awaiting_confirmation; original pilot score remains unchanged.
Review actual pilot: raw model requirements/unresolved were malformed array-looking strings, not valid arrays/JSON. Official Hermes coerce_tool_args wraps each into a single-element list after pre-tool observation. RawSHA679172... vs actualSHAc1d66b... caused gate review_native_arguments_changed; wrapper returnsNone, comparison show(None) obscures diagnosis with ValueError. Core correctly persisted needs_context (zero accepted requirement rows,10missing),0confirm/execute. This is not a successful business draft and must not be rescued by provenance fix.
Postpilot fix wave authorized: Hermes agent captures bounded rawpretool+actualpostcoercion digests, permits only identity or explicit requirements/unresolved str->[same_str] transforms while transformed proposals remainblocked. Pre=raw, post=raw OR receiptboundactual for two officialSDK paths. Missing/unknown provenance failsclosed. Validation agent adds review-only unique actual writer signature/result/target/bytes proof for ID-less stdout; nofakeID/patchclassifier/legacygatechange; comparison records structured gateerrors and ungraded draftfaithful=null. Independent reviewer will inspectboth. No more model calls yet.
Next: commitfixes, targeted+finalfullsuite, rebuild/reinstallfinalwheel withnewsourcehash76modules, finalscopedreview; NEW registration ownedroot/comparison-formal, run --formal ONLY (12draft), oldrootcomparison untouched. Export final public evidence separately, aggregate bothledgers with namespaced IDs for overallcosts; total model budgetstill14draft<=7nativeexecution. Finishreport, exactownedcleanup, mainlocalbranchcodex/rpe-v1.10-release, preserve olduntrackedreports/oldrepos, annotatev1.10.0, archiveworktree natively withconfirmation. Goal remainsactive.

Formal exit0:12draft,0nativeexec; 4production/arm all0complete. Native3host-ungraded(2production+1probe),review1productionungraded. Both2probes unapproved/nooutput but exactprobe0. Formal known202008tokens+32API,3missing ->totalnull; pilot+formal known274398+41API. Public export+offlineverified before sourcecorrection.
Single bounded correction:standard schema enums+shared defaults/preconditions; no parent validation/gate/scorer changes or retry/parsing/unresolved removal. Hermes agent owns source/tests. After finalQA, independent JSONpair preregistration with6publiccases+noformal sessions; postdiagnosticselectedsmoke<=2draft+1nativeexec. Original scores never rescored. Spec/plan amended. Cleanup/release remain required.

Final source2b525f5; installed release61passed,76modules equal, wheelee31c360... QA00279d0. Independentcontractreview22tests+parentprobe noP1/P2. Fullrelease1613passed+5envskipped; explicitHermesSDKsupplement5passed; all1618collected+3subtest covered,0remaining skip/failure. Finalsourcefrozen. RegisteredselectedJSONpair1c920a6ef234a80659e0d9ced7e02311cb6d593e725ffd71bc309304ca45f829,2draft<=1nativeexec,0formal sessions; startedstandardrun--max-sessions2, no futuretuning/retries.

SelectedJSONpair finished0nativeexec; bothactualdrafts gateverified/comparable/awaiting_confirmation,0unresolved/missing/errors; exactfixture rejects onlymissing redundantpreserve_original flag. Originaltaskdoesnotrequestflag; JSON executor+oracle unconditionallypreserveinput. Independentvalidation+independentreviewer confirmed this exactone-fieldsemantic-equivalence diagnosis, saved separately; originalsneverrerated/reexported. Overall16draft/46API/known304205tokens,totalnull(3oldmissing),driver1441.969s/fulltrial1429.916s. No moremodelcalls/source/scorerchanges.
Independent reviewer owns one0APIpostmeasurement trusted-app flow: SQLitebackup originalreviewDB retainsproposal ID/digest/session/workspace, originalDB/records/exports unchanged. Confirmation after semanticreview then actualdeterministicexecute onoriginalisolatedworkspace, outputonlypostmeasurement; replay/oracle/sourcepreservation/exportverification. This is notformal/nativequalitywin and nothumanconfirmation. Root waits for receipt thenexactcleanup/localrelease/archive.

Last0HermesAPIcontrolflow passed: originalproposal171678088ebf0b7b9b825698663e8eaaa0655eb6c5caa2741aa79a16e2685d02; run358f91274d8d4673949865f81b87f609, confirmed/execute/oracle/replay/primarypreservation alltrue. Controllerlocalelapsed0.571314s; devreviewcostunknown. Publicflowpurestdlibofflineverified.
Credentialscan1actualcredentialvalue/106publicfiles,0matches. Exactcleanupdry+delete successful,90562files/1273041229bytes,0liveowned/16tasksessionIDs/0dailyhistoryrows,strictpreservationtrue,4publicgroups verifiedbefore+after. Ownedrootremoved. Remainingonlylocalreleasebranch+tag andnativemanagedworktreearchiveconfirmation; no furthermodels/source/tests.

Final maincheckout codex/rpe-v1.10-release verified: CLI1.10.0/review4commands, all76source/wheel modules equal,4publicgroups offlineverified afterowneddelete. Nativearchive confirmed archived_worktree attachment01a13d57-1289-7793-80ff-1b178e6ba302; physicalworktree absent; gitworktree listmainonly. Oldtwo repos clean/originalHEAD and4preexistingreporthashes unchanged. Localtagv1.10.0 willpointfinaldeliverycommit; no required work remains afterthisfinalmetadata commit.
