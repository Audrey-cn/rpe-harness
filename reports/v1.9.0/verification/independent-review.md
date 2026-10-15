# Independent RPE 1.9 review

Scope: base 6fd26d8e57ea4a8f44678e9ad995c379affa2463 through commit 5758be9 (including reviewed T3 commit and source work); read only. No native model calls, no children, no full suite. Reviewed strict-source ledger/CLI, clean SDK ingress and controller, comparison freeze/cost/evidence changes.

## Resolved P2 finding

[P2] Atomically consume controller authority across broker instances.

Location before fix: src/rpe_harness/integrations/hermes_broker.py:277-282; prepare_controller_execution validates the saved state, then controller_response sets controller_claimed through an unconditional WorkflowStore.put. execution_lock is local to one broker, so overlapping brokers can both read an unclaimed workflow and write the claim.

Reproduction: review-probes/test_controller_race.py starts two real execution_broker instances on the same clean workflow, puts a barrier immediately after both prepare_controller_execution calls have read/validated it, then submits the identical controller envelope to both endpoints. Result before fix: both HTTP 200; two rpe_controller_task_received receipts; the same run ID returned once as running and once as succeeded. Saved probe data is under review-race3. Existing event deduplication prevented duplicate business publication, but duplicated admission/receipts and racing terminal writes invalidate the clean identity gate (which requires exactly one controller receipt) and can expose a nonterminal run as the terminal workflow result.

Recommended change: conditional SQLite UPDATE against the expected workflow digest (or equivalent locked read/compare/write transaction), requiring exactly one changed row before recording a receipt or creating BoundExecution. Preserve the consumed claim through failures and restart.

## Checks

54 targeted tests passed in 4.97 seconds before the later argument-binding/CAS changes: tests/test_hermes_clean.py, tests/test_requirements.py, tests/test_source_cli.py. This includes installed Hermes SDK registration/dispatch without provider calls. The installed SDK imports execution middleware dynamically, consistent with the bootstrap guard installation order.

The strict source comparison rejects dropped/extra requirements, preserves exact original source, reopens source/state/ledger/catalog/input bindings, and uses distinct controller identities. No other actionable P1/P2 found in the reviewed code. Public export explicitly limits runtime identity claims to recorded checks; portable business verification is separate from provider identity authentication.

Final CAS regression recheck: PASS. Independent external forced-barrier probe returned one HTTP 200 succeeded and one HTTP 403, with exactly one controller receipt and an independently verified correct output. Evidence is retained under review-race4. The fix uses a conditional SQLite UPDATE against the prior state digest before receipt creation and bound execution.

Post-fix focused suite: 58 passed in 6.06 seconds (same three test modules, including new argument-binding and concurrent-claim regressions). No remaining actionable P1/P2 findings in this reviewed snapshot. No minor findings.

## Scoped ingress hardening re-review after 5758be9

Reviewed only the new hermes_clean.py/hermes.py/test_hermes_clean.py delta addressing alternate request context channels. The policy reconstructs requests using a fixed Xiaomi transport-key allowlist; extra_body accepts only thinking.type=enabled/disabled. Other allowed values are bounded numeric settings, booleans, enums or a bounded model identifier. Original messages/tools/control fields are replaced with the trusted clean projection. The actual execution guard repeats shape validation and compares the entire outgoing request digest; the runtime evidence gate recomputes that digest from the saved typed transport and regenerated clean messages/tools. API validation and receipt persistence errors become CleanIngressBlocked (BaseException), so the installed Hermes ordinary-Exception fallback cannot send the rejected request.

Independent targeted command: pytest tests/test_hermes_clean.py -k 'unknown_context or safe_transport or final_http_body or actual_api_messages or failure_cannot_fall or bootstrap_guard or receipt_failure' with -B, no cache provider, and owned review-ingress basetemp. Result: 16 passed, 20 deselected in 4.75 seconds. The installed SDK test assembles a real Xiaomi AIAgent request with socket connections disabled, then sends through the real middleware and OpenAI serialization into httpx.MockTransport. Both legitimate safe requests reached mock HTTP with exact trusted source and no PRIVATE marker; 19 hostile/late override cases were rejected before mock HTTP. No native model calls or real network were used. git diff --check for this scoped delta passed.

Scoped verdict: no actionable P1/P2 or minor findings. The compatibility conclusion is limited to the current configured Xiaomi chat request shape, as intended by the launcher. Final hardening review pinned to commit 634768f; its three committed files match the tested working-tree delta. Overall reviewed code range: 6fd26d8e57ea4a8f44678e9ad995c379affa2463..634768f, with the initial whole-scope review at 5758be9 and this final scoped ingress re-review.

Root review of aa083c9: clean-only timer now encloses launcher setup, session and post-run validation; original host timer retained separately. Existing address branch unchanged. Independent deterministic fake-clock regression proves 1-second host versus 5-second full-trial cost; 71 relevant tests green. No authority or execution changes after the independent ingress review at634768f.
