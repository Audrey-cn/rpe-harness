# RPE-Harness v2.3.1 hardening validation

Validated on macOS with CPython 3.12.15 in `/private/tmp/codex-v231-venv`.
The environment was created outside the repository and removed after validation.
No commit, push, release, or upload was performed.

## Audit coverage

All regression cases live in `tests/test_v231_hardening.py`.

| Finding | Implementation and regression coverage |
| --- | --- |
| 1 | Exact bound loopback hostname/port on GET and POST; same-origin Origin; Fetch Metadata; missing/duplicate headers and rejected-request state preservation. |
| 2 | CSP `frame-ancestors 'none'`, X-Frame-Options DENY, nosniff on successful, rejected, missing-route, and stdlib error responses. |
| 3 | SQLite execution and verification consume supplied bytes through separate private read-only snapshots with encoded file URIs; source mutation and special-character paths tested. |
| 4 | Combined network/PID namespace support probe and optional PID fallback; descendant RSS sum; capabilities disclose fallback and soft RSS limits. |
| 5 | Suspended Windows launch, assign-before-resume, explicit kill and three-second wait on failed assignment; resume/cleanup errors; base interpreter avoids venv redirector/process-limit conflict. |
| 6 | Reserved built-in capabilities rejected by direct registration, replacement and entry-point loading. |
| 7 | Inline dotenv comments preserved; an independently implemented verifier rejects a producer deliberately dropping comments. |
| 8 | Release checks all three versions against the tag, then runs Ruff and core pytest before release/upload. The workflow's exact version guard is executed with matching and mismatched tags. |
| 9 | Bounded stdin chunks, UTF-8 size limit, oversized-line draining, JSON decode/recursion/integer failures and continued service; lone-surrogate IDs cannot break UTF-8 output. |
| 10 | Registered plugin context definitions resolve through the capability catalog; plugin proposals, context conflicts, and unknown-capability validation errors covered. |
| 11 | Actual shipped JavaScript exercised in Node for failed HTTP responses and recent errors; confirmed/failed operation visibility and owner filtering covered. |
| 12 | Trusted catalog/snapshot constructor and CLI options; real trusted XLSX proposal activation; model-supplied catalog overrides rejected. |
| 13 | Multi-attempt recovery reflected through HostAPI/MCP show/reconcile; pre-plan rejection and unfinished durable retry links; cycles rejected. |
| 14 | Cross-format plugin plans/artifacts report CSV; same-format default; saved-plan compatibility and legacy retry digests; persisted format-label tampering rejected. |
| 15 | `.env`, `.env.*`, and `*.env` exercised through runtime, intent and proposal paths. |
| 16 | SQL strings, quoted identifiers and comments masked before policy checks; valid keyword/semicolon literals allowed; writes and multiple statements rejected. |
| 17 | IPv6 address family and bracketed listening URL; IPv6 Host handling. |

The hardening file is included in Linux, macOS and Windows CI jobs.

## Passing checks

```text
python -m pytest -p no:cacheprovider -q tests/test_v231_hardening.py
  tests/test_contract_sdk.py tests/test_dev_contracts.py tests/test_recovery.py
162 passed (142 hardening regressions and 20 existing tests)

python -m ruff check src tests
All checks passed!

git diff --check
clean
```

Wheel and source distribution built successfully with offline Hatchling/uv:

```text
/private/tmp/codex-v231-dist/rpe_harness-2.3.1-py3-none-any.whl
/private/tmp/codex-v231-dist/rpe_harness-2.3.1.tar.gz
```

Verified wheel metadata and Python version are 2.3.1; source distribution has
the matching package version and includes the new hardening tests.

## Broader checks and limits

The broad affected suite ran 33 files, including host/MCP/proposal workflows,
cancellation, runtime/JSON/recovery, contracts, intent/context, inbox,
XLSX inspection, candidate execution and sandbox tests. Result:
**710 passed, 66 failed, 39 setup errors, 1 skipped**. This run collected the
137 hardening cases available before the final five HTTP-header cases were
added; all final 142 cases passed in the focused check above.

The initial full source run completed with **2109 passed, 173 failed,
268 setup errors, 8 skipped, 3 subtests passed**. It preceded the last review
fixes and final test additions, and is not a claim that the full suite is green.

Observed execution restrictions include `socket.bind: Operation not permitted`,
`/bin/ps: Operation not permitted`, and
`sandbox-exec: sandbox_apply: Operation not permitted`. These prevent real HTTP
listeners, process cleanup inspection and native sandbox helpers from working
inside this managed session. An independent unmodified v2.3.0 checkout reproduced
the exact same 20 failures in the narrower 13-file affected comparison.
The broader baseline comparison then finished with **573 passed, 66 failed,
39 setup errors, 1 skipped**: its failure/error name sets exactly match the
modified tree, with no modified-only failures. The modified run adds 137 passing
hardening cases to that comparison; the final five were verified separately.

The full and broad failure/setup-error names are retained in
[`pytest-failures.txt`](pytest-failures.txt). Raw local receipts:

- `/private/tmp/v231-full-pytest.log`
- `/private/tmp/v231-final-affected-pytest.log`
- `/private/tmp/v231-baseline-affected-pytest.log`
- `/private/tmp/v231-baseline-wide-pytest.log`

HTTP regressions use the actual stdlib request parser with in-memory connections.
Windows Job Object and Linux bubblewrap boundaries are exercised with portable
controlled OS-call substitutes; native Windows/Linux containment and the changed
GitHub Actions workflows have not been executed here. The Windows base-interpreter
choice was checked against CPython's venv and launcher implementation.
