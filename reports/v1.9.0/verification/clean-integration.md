# T2 clean ingress and deterministic execution

Commit: `5758be9` (`Add clean Hermes ingress and source-bound controller execution`).

Implemented only the assigned integration/test files plus root-authorized `hermes_bootstrap.py`. No model/provider calls, child agents, CLI edits, or daily Hermes configuration changes were performed.

## Behavior

- `run_hermes_session(..., entry_mode='clean')` captures the exact bounded original source in a parent-created hashed configuration and in the parent callback. It rejects hidden `required_request` contracts and old `context_sources` before host setup.
- The sole real native tool is `rpe_understand`. Its actual outgoing messages are rebuilt from the trusted exact source, fixed public contract, and bounded capability catalog; upstream messages/history never supply understanding input.
- The official raw API execution middleware checks source-message digest/bytes, tool policy/schema, configuration identity, and shadow `extra_body` overrides before provider dispatch. Validation/persistence errors raise a dedicated `BaseException` because installed Hermes intentionally falls through ordinary middleware exceptions. A process-local bootstrap guard independently blocks calls when the clean plugin/guard did not register.
- Ready understanding invokes the existing `/task` broker through a distinct controller envelope. Parent admission resolves `SourceStateStore`, rechecks source requirements/state/catalog/input, and consumes authority with an atomic SQLite compare-and-swap. It then shares the existing `BoundExecution`, fixed Harness CLI, worker timeout/process cleanup, artifact publication and independent oracle.
- Controller receipt types are `rpe_controller_task_received` and `rpe_controller_task_returned`. They contain controller/source/state/requirements identity and the triggering understanding receipt, without invented model `api_request_id`, `tool_call_id`, `tool_name` or native `rpe_task` events. Original `/task` native admission remains unchanged.
- `evaluate_clean_session` independently reopens durable source/state, actual SDK projected/raw policies, native understanding argument hashes, controller receipts/run, and the original artifact oracle. It rejects fake native task receipts. Reporting requests are separately counted; at most one toolless final reporting request is admitted.
- `public_contract='clean'` supplies the same constant through Hermes's existing `agent.system_prompt` configuration in either arm. Launcher results and safe failed observations carry `public_contract_sha256`. The raw user prompt remains unchanged.

## Verification

Final targeted command used the owned venv, `-B`, `-p no:cacheprovider`, and `--basetemp ROOT/t2`:

`pytest tests/test_requirements.py tests/test_source_cli.py tests/test_hermes_clean.py tests/test_hermes_address.py tests/test_hermes_session.py tests/test_hermes_broker.py tests/test_hermes_bootstrap.py -q`

Result: **151 passed in 25.11s**, including **23 clean integration tests**. JUnit: `ROOT/t2-junit.xml`. `git diff --check` passed. Root requested one later whole-branch full suite; it was deliberately not run here.

Observed RED/GREEN checks included missing clean module/launcher support; actual installed SDK middleware falling through to a provider spy on bad ingress; missing bootstrap registration guard; independent gate accepting altered native argument hashes/fake task receipts; and two independent brokers consuming the same controller authority.

The real installed Hermes SDK no-model preflight loads the actual plugin manager/toolset, calls `_load_tools`, validates the actual sole tool through `validate_tool_calls`, invokes it through `invoke_tool`, exercises official API middleware and raw observer projection, executes a real bounded Harness worker, checks actual output, and passes the independent clean gate. Provider responses/tool arguments are injected fixtures, not model inference. A fake key and `example.invalid` endpoint are used; no provider client/request is executed.

Additional admission tests reject cross-session/state requests, fabricated native identity fields, tampered source ledger/input, changed catalog identity and revoked XLSX skill source. Concurrent same-broker requests are idempotent. A forced simultaneous-prepare two-broker test first produced `[200,200]` (RED), then `[200,403]` with exactly one controller receipt after the database CAS fix (GREEN). The independent review agent separately reproduced and verified that fix and reported no remaining actionable P1/P2 findings.

## Interfaces for T3/root

- Public legend: `hermes_clean.CLEAN_SOURCE_CONTRACT.replace('{helper_python}', sys.executable)`.
- Launcher: `run_hermes_session(raw, ..., entry_mode='clean', public_contract='clean')`.
- Gate: `evaluate_clean_session(result, db_path, workspace, *, catalog_root=None, required_request=None)`; new clean launcher forbids `required_request`, while this optional gate parameter remains an existing oracle check.
- Ready verdict includes `state_id`, `source_state_id`, `requirements_sha256`, `source_sha256`, `original_source_sha256`, `source_ref`, `catalog_sha256`, `controller_id`, `understand_receipt_id`, `controller_receipt_id`, `execution_kind='controller'`, `native_understanding_calls`, `controller_executions`, `understanding_api_requests`, `reporting_api_requests`, and `source_coverage_verified`.
- Refusal verdict includes source identity/requirements digest and `rejection_verified`, with zero controller executions. Refusals are reinterpreted from the stored exact source/extraction at the gate; they do not claim a business artifact.

## Limits retained

Strict structured source is the only automatically executable authorization. Free prose returns traceable `needs_context/structured_current_request`. No claim is made about arbitrary language semantic completeness, model quality, provider cost, a real Hermes pilot, or release acceptance. Hashes detect altered linked records; they are not independent third-party authentication.

## Final wire ingress check

Additional scoped commit: `634768f` (`Constrain clean request wire fields before provider dispatch`), only `hermes_clean.py`, its tests, and the HostEventStore allowlist for `request_payload_sha256`.

A no-network experiment using installed OpenAI SDK **2.24.0** and `httpx.MockTransport` confirmed that SDK `extra_body` wins the final body merge. The prior clean implementation already removed the six protected overrides, but arbitrary `history/system/prompt/metadata` extra fields still reached the mocked HTTP body. Twelve focused tests first failed on this gap or on transport-setting mutation (RED).

The clean request is now reconstructed from a typed top-level allowlist: `model`, `temperature`, `max_tokens`, `max_completion_tokens`, `timeout`, `stream`, `stream_options`, `reasoning_effort`, and `extra_body`, plus the four parent-controlled fields (`messages`, `tools`, `tool_choice`, `parallel_tool_calls`). Model names are bounded identifiers; numeric settings are finite/bounded; streaming values are booleans; reasoning effort is an enum. `extra_body` accepts only `thinking` with exactly `type=enabled|disabled`. Unknown or malformed channels raise `CleanIngressBlocked` before provider dispatch, including through Hermes's fail-open request middleware. The entire effective kwargs payload is hash-bound, recorded by raw execution observation, and independently reconstructed at the gate.

The final HTTP regression constructs the **actual installed `AIAgent`** with the isolated launcher's fake settings and calls its real `_build_api_kwargs`. Real socket connections are hard-denied for the subprocess. Its observed default keys were **messages, model, timeout, tools**; no extra cache/session/reasoning key was emitted. These actual defaults pass the clean boundary. Explicit valid thinking/streaming/reasoning/numeric settings also pass. Two calls reached only `httpx.MockTransport`; **19 contaminated cases were rejected before mocked HTTP**, and **real network/provider calls were zero**. The real SDK bootstrap guard is installed in this test.

Final scoped regression: **69 passed in 14.68s** across `test_hermes_clean.py`, `test_hermes_address.py`, and `test_hermes_bootstrap.py`; clean tests now total **36** (13 added). JUnit: `ROOT/t2-ingress-junit.xml`. Measured proof: `ROOT/t2-ingress/test_real_sdk_final_http_body_0/wire-proof.json`. `git diff --check` passed. No full suite or paid/model request was run by this task; root owns the final whole-branch validation and fresh preregistration.
