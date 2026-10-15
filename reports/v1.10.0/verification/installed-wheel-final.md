# Final installed wheel verification — RPE 1.10.0

Passed against the rebuilt, non-editable wheel installation. This check preserves the earlier installed-wheel report and tests the final post-pilot provenance changes. No source edits, model calls, package installs, or full-suite run occurred in this check.

| Check | Result |
| --- | --- |
| Final source commit | `aa5d9a5` |
| Package and metadata version | `1.10.0` |
| Package import | Installed `site-packages/rpe_harness/__init__.py` |
| Source / wheel / installed Python modules | All **76** files byte-identical |
| Direct runtime requirements | `openpyxl>=3.1.5,<3.2` only |
| Targeted installed tests | **58 passed**, 0 failures/errors/skips, 17.098 s |
| Actual installed Hermes SDK provenance | Three subprocesses; startup/exit package-module records all installed |
| Real model calls / network attempts | 0 / 0 in the existing MockTransport check |

The selected files cover Hermes review and malformed-array coercion, comparison evidence gates, public review entry CLI, and proposal CLI. The actual SDK malformed-array test remains `needs_context` with zero accepted requirement rows, no confirmation, and no business output; raw/effective argument provenance is verified rather than repairing model text.

Tests ran from the owned directory with installed Python, `-B -p no:cacheprovider`, owned `TMPDIR`/`--basetemp`, and `RPE_HERMES_SOURCE=/Users/audrey/.hermes/hermes-agent`. A temporary QA plugin asserts installed imports and changes only the existing SDK helper's import paths from checkout `src` to installed site-packages. Test and production sources were unchanged.

Wheel: `rpe_harness-1.10.0-py3-none-any.whl` (361434 bytes).

- Wheel SHA256: `61e9a9cede4b99d3fd784892dbba75ab0bd2944c4dfcc7f036cf773d4b4e48f2`
- Complete 76-file module-manifest SHA256: `fd92ab91948fd5d9ba67c85247bfd0c7a5ed26e6a77c9e63a2b5113ea996843d`
- Per-file hashes, test counts, and SDK provenance: [installed-wheel-final-qa.json](installed-wheel-final-qa.json)
- JUnit: [installed-wheel-final-junit.xml](installed-wheel-final-junit.xml)

This verifies the named tests and packaging against final installed code. It does not replace the independent source full-suite result or real-model acceptance evidence.
