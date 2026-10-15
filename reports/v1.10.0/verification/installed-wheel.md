# Installed wheel verification — RPE 1.10.0

Passed against a non-editable wheel installation from an owned directory outside the checkout. No models, package installs, or full test suite were run in this check.

| Check | Result |
| --- | --- |
| Package and metadata version | `1.10.0` |
| Package import | Installed `site-packages/rpe_harness/__init__.py` |
| Source / wheel / installed Python modules | All **76** files byte-identical |
| Direct runtime requirements | `openpyxl>=3.1.5,<3.2` only; dev extras remain optional |
| Targeted tests | **101 passed**, 0 failures/errors/skips, 10.810 s |
| Installed SDK provenance subset | **2 passed**, 0 failures/errors/skips, 6.866 s |
| SDK subprocess imports | Both subprocesses imported the installed package; startup/exit module records were all installed |
| Real model calls / network attempts | 0 / 0 in the existing SDK MockTransport check |

The targeted files cover proposals, public proposal CLI, review entry CLI, Hermes review, existing strict-source requirements, and source CLI. Tests ran with the installed interpreter, `-B -p no:cacheprovider`, owned `TMPDIR`/`--basetemp`, and the actual installed Hermes SDK at `/Users/audrey/.hermes/hermes-agent`.

A temporary QA plugin asserted installed module provenance throughout pytest. The existing Hermes SDK test helper normally hardcodes the checkout's `src`; the plugin changed only its import paths to installed site-packages. An additional two-test pass recorded SDK subprocess startup/exit provenance. Production and test sources were unchanged.

Source commit: `e2eb78a6780442cd3c4b0cf583e90f96cf81df99`. Wheel: `rpe_harness-1.10.0-py3-none-any.whl` (359025 bytes).

- Wheel SHA256: `56752defccc0507613e02ca0d41dc60668ca48e074a8b5b6bea93bf11c88ed52`
- Complete 76-file module-manifest SHA256: `2e7898e3590b4c96d78fa38408bc42c6c15263ab82179e74d7b79b398535d3db`
- Details and per-file hashes: [installed-wheel-qa.json](installed-wheel-qa.json)
- Primary JUnit: [installed-wheel-junit.xml](installed-wheel-junit.xml)
- SDK provenance JUnit: [installed-wheel-sdk-junit.xml](installed-wheel-sdk-junit.xml)

This verifies packaging and the named tests against installed code. It is separate from the source full-suite result and from real-model natural-language acceptance.
