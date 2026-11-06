# Public Hermes XLSX test fixture

Unmodified bytes read from the installed public Nous Research XLSX skill on
2026-10-14. The installed files match the same files in Hermes Agent at local
commit `63d0b94120027d487c9a5235b9ec64b696af1b26`.

Upstream repository: https://github.com/NousResearch/hermes-agent

- `skills/productivity/xlsx/SKILL.md`: SHA256 `b302d7ac997843a757ab3c11513ea012889ebdae32f842edb44faa9a4485f19b`
- `skills/productivity/xlsx/scripts/xlsx_read.py`: SHA256 `5ab31b1b417efea11a8af7b15c6aa5f26d6d966effefaa19af604c1fa0ca0916`

Only the public instruction file, bounded read helper and upstream MIT license
are preserved. Linked helpers/references mentioned in the instruction file are
outside this fixture. Unit tests use this fixed fixture; live acceptance must
separately snapshot and verify the actual host skill.

On 2026-11-07, the installed `productivity/xlsx/SKILL.md` still matched this
fixture. The installed helper's SHA256 was
`5f8dc99437e4b65d72cac4f99ef25d383b477926d5ae31e0e8aae6fa3266d935`.
Inserting `  # noqa: BLE001` after `except Exception as exc:` reproduced
the installed bytes exactly. Both helpers produced the same Python AST.
At local Hermes commit `b200f5bc60e31588ba2cb60e288029c4a54ba5fa`, the helper
at `skills/productivity/microsoft-office/scripts/xlsx_read.py` still had the
original fixture hash. Tests reconstruct the audited installed variant from
the original fixture and check its hash.
