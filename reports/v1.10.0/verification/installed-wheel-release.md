# Release wheel verification — RPE 1.10.0

Rebuilt the release wheel from `2b525f5`, installed it offline with `--force-reinstall --no-deps --no-index` into the separate installed environment, and verified the named tests against installed code. Earlier initial/final QA evidence is unchanged. No source edits, model calls, or full-suite run occurred in this check; build/install caches and temporary files stayed in the owned directory.

| Check | Result |
| --- | --- |
| Source commit | `2b525f590e8d61c49f06c15f080bb159fcd259e9` |
| Package and metadata version | `1.10.0` |
| Package import | Installed `site-packages/rpe_harness/__init__.py` |
| Source / wheel / installed Python modules | All **76** files byte-identical |
| Direct runtime requirements | `openpyxl>=3.1.5,<3.2` only |
| Targeted installed tests | **61 passed**, 0 failures/errors/skips, 22.994 s |
| Actual installed Hermes SDK imports | All six subprocess startup/exit records use the installed package |
| Real model calls / network attempts | 0 / 0 in the existing MockTransport check |

Coverage: Hermes review/schema/coercion (30 tests), comparison evidence gates (21), public review entry CLI (3), and proposal CLI (7). The real SDK-to-MockTransport test confirms that exact capability/flag/pointer and option enums reach the HTTP body. Hermes does not enforce enum semantics itself; parent admission still blocks capability descriptions, invalid pointers, invalid XLSX flags, and malformed array strings. A valid typed proposal remains confirmable, and blocked cases produce no business output.

Tests ran from the owned directory with installed Python, `-B -p no:cacheprovider`, owned `TMPDIR`/`--basetemp`, and the actual installed Hermes SDK. A temporary QA plugin asserted installed imports and changed only the existing SDK test helper's import paths from checkout `src` to installed site-packages. Production and test sources were unchanged.

Wheel retained at `/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/built-release/rpe_harness-1.10.0-py3-none-any.whl` (362483 bytes).

- Wheel SHA256: `ee31c36019bf41b0b6ff68350209244e6d5c6fcce270ab2f60a2e3f19d6c6cd8`
- Complete 76-file module-manifest SHA256: `0408977a9a3fce5e22e2fad1cd5d9f32885f191630edb81dfa53b3bad6eeea71`
- Per-file hashes, build/install facts, test counts, SDK provenance: [installed-wheel-release-qa.json](installed-wheel-release-qa.json)
- JUnit: [installed-wheel-release-junit.xml](installed-wheel-release-junit.xml)

This verifies packaging and the named installed-code tests. It does not replace the separate source full-suite result or prove natural-language model success; the bounded smoke remains separate evidence.
