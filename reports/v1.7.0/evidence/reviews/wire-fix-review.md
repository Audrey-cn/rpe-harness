# Producer wire fix review

Reviewed the four-file delta from `e84729a` to `73dfa95c19a766a66890feb72cdb1e34ca231b5b`: `integrations/hermes.py`, `integrations/hermes_session.py`, `evolution/worker.py`, and `tests/test_hermes_producer.py`. HEAD advanced to `09d5b8b8ad2bfbc9b8fa28196bddf6a50a3720ed` during review; that subsequent commit does not change these four files. No source edits, model calls, dependency changes, or broad test runs were made in this review.

## Verdict

One P2 finding. The supplied isolated Xiaomi request has the correct OpenAI SDK shape and the change remains confined to the explicit producer profile. Correct the conflicting `extra_body` budget case before claiming that this middleware always imposes the reported 8192-token limit. No P1 finding.

## P2: Preserved extra_body token limits override the reported producer budget

Location: `src/rpe_harness/integrations/hermes.py:582-584` (and resulting metadata at `:130-132`).

The policy copies every existing `extra_body` key, removes only top-level `max_tokens`, and sets top-level `max_completion_tokens=8192`. The installed OpenAI SDK merges `extra_body` into the serialized JSON after the normal body; duplicate keys in `extra_body` win (`openai/_base_client.py:502-506,2154-2163`). Thus a valid incoming request containing `extra_body={"max_completion_tokens":131072}` is sent with a 131072 limit even though `pre_api_request` metadata and the worker report say 8192. An existing `extra_body.max_tokens` also survives the intended deprecated-field removal.

An offline reproduction used the installed OpenAI SDK with `httpx.MockTransport`, the real `_ProducerPolicy.policy`, and `_metadata`. It made no network request and returned:

```json
{"metadata_limit":8192,"effective_json_limit":131072,"effective_thinking_type":"disabled","original_extra_unchanged":true}
```

Copy `extra_body`, remove its conflicting `max_tokens` and `max_completion_tokens` keys (or explicitly set the canonical limit there too), and retain unrelated settings. Add a regression covering the serialized SDK JSON and matching observation for both conflicting token fields. The current isolated launcher creates a Xiaomi home without request overrides, so this edge does not establish a cause of the earlier G0 timeout or predict failure of the planned calibration.

## Other checks

- `extra_body.thinking={"type":"disabled"}` becomes the API's top-level `thinking` object under the OpenAI SDK. `max_completion_tokens` is an accepted SDK argument. Xiaomi documents disabled thinking and this completion-limit field in its [OpenAI-compatible Chat Completions API](https://mimo.mi.com/docs/en-US/api/chat/openai-api).
- The policy constructs a new request and a new extra-body object; the original supplied body is not modified. Unrelated extra-body keys are preserved.
- Installed Hermes applies `llm_request` middleware before `pre_api_request` (`agent/turn_api_request.py:143-164`), and the hook includes the rewritten SDK kwargs under `request.body` (`agent/api_request_hooks.py:133-139`). The new metadata extraction therefore matches the actual hook shape. It records request intent, not a server acknowledgement.
- The producer middleware is created only for `RPE_HERMES_ENTRY_MODE=producer` and returns from registration before the other policies (`hermes.py:710-714,773-776`). Native, strict, and optional request behavior is unchanged by this four-file delta. The added observer fields and additive worker report field do not change those requests.
- The new thinking value is restricted to a two-value enum when extracted from the request. The token count is a positive integer; failed-session diagnostics retain it through the existing bounded-number filter. Worker observations and safe diagnostics retain the two new fields.
- Independently read the owned-root evidence logs: `wire-red.log` fails for missing `max_completion_tokens`; `wire-observe-red.log` fails for missing compact `request_max_completion_tokens`; `wire-final.log` reports **162 passed in 19.43s**. These are inspected logs from the root agent's test runs, not additional reviewer test runs.

The change provides a documented nonthinking request intent. It does not prove that default thinking caused the prior timeout, that Xiaomi enforces the requested non-auto `tool_choice`, or that a real candidate was generated and accepted. The client write guard and a fresh observed calibration remain necessary evidence for those separate claims.

## Closure review: b7f3caf

Reviewed `.superpowers/sdd/2026-10-14-rpe-v17/wire-sdk-fix.diff` and commit `b7f3cafc4960fe1fb820e1d1ccc82f574f8c5eac`. The follow-up contains only the producer policy adjustment and regressions in `tests/test_hermes_producer.py`.

**P2 closed. No remaining P1 or P2 finding in this scoped wire fix.** The policy now makes a separate extra-body copy and removes `max_tokens`, `max_completion_tokens`, `tools`, `tool_choice`, and `parallel_tool_calls` from it before adding disabled thinking. Consequently, the SDK's final extra-body merge cannot replace those policy-controlled top-level fields. Unrelated extra settings remain intact, and the supplied extra-body object is not mutated. The producer registration condition and all default entry paths remain unchanged.

The added unit regression supplies all five conflicting keys and checks the sanitized extra-body result and original input. The existing installed-Hermes SDK smoke now passes such a request through real middleware and `OpenAI.chat.completions.create` with `httpx.MockTransport`. It inspects serialized HTTP JSON for the 8192 completion limit, absence of deprecated `max_tokens`, disabled thinking, the exact write-only tools, named `write_file` choice, and `parallel_tool_calls=false`. This is an offline serialization check, with no remote model or network call.

Independently inspected the root agent's owned-root logs: `wire-sdk-red.log` contains the expected failing regression before sanitization; `wire-sdk-green.log` reports **163 passed in 19.36s**, with no skipped-test result. No reviewer tests or source edits were added. This closes the serialization finding and supports proceeding to the separately required installed-package calibration; it does not establish a causal explanation for the previous timeout or a real generated-and-accepted candidate.
