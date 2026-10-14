# Cost ledger independent review

Reviewed commit range `5a90d4d..a5a0f0f`: `src/rpe_harness/experiments/cost_ledger.py` and `tests/test_cost_ledger.py` only. Reviewed the supplied report/diff, the four ledger RED/GREEN logs, and the stable `aggregate_usage` implementation in `integrations/hermes_session.py`. Developing experiment and worker implementations were not reviewed or modified.

**Spec verdict: changes required. Quality verdict: changes required. No P1 findings; two P2 findings.** Reservations/receipts and the main token lower-bound path work as intended, but two uncovered accounting edges permit definite zero values where the operator should see uncertainty or validation failure.

## Findings

### [P2] Keep wholly unobserved timed-out request totals unknown

`src/rpe_harness/experiments/cost_ledger.py:218` (output at line 230).

For an API-expected phase that times out before any usable observer events reach the ledger, the normal Hermes `aggregate_usage([])` returns `api_requests=0`, `coverage_complete=False`, `total_tokens=None`, and `known_total_tokens=0`. The ledger currently treats the integer request count as complete and reports `api_requests=0`. An operator reading a complete request denominator can infer that the failed attempt made no calls, even though this is an absence of request observations. Only `known_api_requests=0` is justified; `api_requests` should remain null until the caller can establish complete request observation. Preserve the distinction from a pre-request event with no post-event: that observed request is a known request and must remain included in the lower bound.

The focused probe reproduced the normal producer shape, without model calls: `timed_out` + `aggregate_usage([])` reports `api_requests=0`, `known_api_requests=0`, and token total null. Normal complete usage and a partial trace containing a second pre-request continue to account correctly (`20 tokens / 1 request`, and `null tokens / known 20 / 2 observed requests`). A request-observation completeness signal or conservative normalization at the caller boundary would resolve this while retaining known request counts.

### [P2] Reject contradictory token usage in local-only receipts before it is discarded

`src/rpe_harness/experiments/cost_ledger.py:144-145` (discard at lines 211-212).

The local-only guard checks only whether `api_requests` is truthy. It accepts the malformed receipt `api_expected=False`, `api_requests=0`, `coverage_complete=True`, `total_tokens=20`, `known_total_tokens=20`, `missing_observations=0`. Summarization then skips the entire usage object and reports `total_tokens=0`, `known_total_tokens=0`, and `usage_complete=True`. Operators expect contradictory stats to be rejected by the strict public receipt interface; silently converting explicitly observed nonzero costs into complete zero costs breaks the lower-bound invariant. Reject nonzero token/cache/reasoning or request/error/missing-observation counts on local-only phases (or prohibit their model usage objects), so accidental producer mismatches cannot produce reported savings.

## Verified behavior and evidence

- Reservation and outcome writes use `O_EXCL`, so duplicate reservations and duplicate `finish` calls cannot replace existing files. Outcomes bind to the reservation digest, and duplicate attempt IDs are rejected by summarization.
- Missing outcome files become incomplete records; pending/model phases and unknown token usage retain null totals with known lower bounds. Failed and timed-out records contribute observed tokens and time.
- Cache read/write, prompt/input/output, and reasoning fields are reported separately; cache buckets are not added to the canonical total. Missing cache-write observations remain null.
- `billing_verified_cost` always remains null. RSS is a maximum of explicitly supplied observations, with coverage recorded; no sandbox limit is inferred as measured RSS.
- Directory walking rejects symlinks; names are bounded identifiers; reads and writes are bounded to 64 KiB; file modes are private; arbitrary usage/outcome payload fields are rejected. Model identities admit only the declared public identifiers, so callers must remove host `base_url` before `finish`.
- The ledger sums each supplied attempt once. Operational/research filtering and actual arm ownership remain caller responsibilities. The asserted six CLI calls per RPE arm were not independently verified because the developing driver is outside this review's scope; no phantom/shared allocation is performed by this module.

Existing evidence, read rather than rerun: `ledger-red.log` = 11 failed; `ledger-green.log` = 11 passed; `ledger-red2.log` = 2 failed / 11 passed; `ledger-green2.log` = 13 passed. These tests do not cover the two findings above.

One focused diagnostic execution created only owned test artifacts and exercised complete Hermes usage, partial Hermes usage, wholly unobserved timeout, and the local-only contradictory receipt. Evidence: `<owned-test-root>/evidence/ledger-review-focused.json`; receipt files: `ledger-review-focused/` under the same owned root. No broad suite, model call, product edit, or index mutation was performed.

## Fix 1 scoped re-review — commit `90ec3bb`

**Updated spec verdict: approved for the cost-ledger portion. Updated quality verdict: approved. Both P2 findings are closed; no remaining P1/P2 findings within this review's scope.** This verdict supersedes the original changes-required verdict above.

Reviewed only `cost-ledger-fix1.diff`, the two-file commit summary, and owned evidence `ledger-review-red.log` / `ledger-review-green.log`. The unobserved timeout now yields a null request total while retaining known zero, with `unknown_usage_phases=1`. Positive observed request counts remain included. `missing_observations` now stays a per-request observation counter rather than mixing in a missing phase. Local-only receipts reject any positive integer usage counter, covering canonical tokens, cache buckets, output tokens, known totals, and request/error/missing counts; contradictory known costs therefore cannot be silently skipped as zero.

The four added RED cases reproduce the two findings (`4 failed, 13 passed`). GREEN records `29 passed in 8.04s`, comprising the reported 17 ledger and 12 comparison cases. The added tests directly cover the actual normal Hermes empty-aggregate shape and three distinct nonzero local-only token buckets. No additional test run, agent, product edit, developing-driver inspection, or index mutation was performed for this re-review.
