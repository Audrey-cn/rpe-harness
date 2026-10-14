# Tab-case contract diagnostic

Both `eval-tab-case-r1` RPE arms submitted an incorrectly escaped delimiter. The required delimiter is one TAB character, length 1, Unicode code point 9. Both actual durable requests used two characters: backslash followed by `t`, length 2, code points 92 and 116. The validator correctly rejected these requests before execution and produced no requested output.

The sources were not shown to have changed. Independent read-only checks found both source files to be stable regular files, 64 bytes each, with SHA-256 `e5de589799df43d63f9c0faedc0eab76f395fe10441c9843252b669428e5f93d`, exactly matching the public registered fixture. The literal header parsed using a real TAB is `item`, `label`, `payload`, with four data rows.

## Actual outcomes

| Arm | Durable run | Outcome | Additional contract difference |
| --- | --- | --- | --- |
| rpe_learned | `218c2a7149964524be143d0ad77a5700` | Rejected at validate; `invalid_request` | `constraints=[]` instead of both required preservation constraints |
| rpe_no_experience | `73cd9e7926024c49a3a23341c663aea2` | Rejected at validate; `invalid_request` | Preservation constraints are correct |

Each durable error is `delimiter must be a single non-newline character`. Each run contains only the validate step, no execute step, and no artifact. Candidate execution was therefore not reached in the learned arm; this does not establish a fault in the learned CSV producer. Paths match the requested `eval-tab-case-r1/input.csv` and `output.csv`; key columns are exactly `['label','item']`, encoding is `utf-8`, and keep is `first`.

The native receipt contract hashes match each actual durable contract. Both requested output paths are absent, including checks for broken symlinks. Later model calls were denied after the durable terminal rejection with `entry_policy_none`; the learned session also reached `task_attempt_limit`. Those denied calls created no replacement durable run or artifact.

## Why the original source flag is false

`_host_session` initializes `source_preserved=false`, then tries to read the requested output before checking the source. A missing output raises before the source measurement is reached. Its persisted false value is therefore unmeasured in this path; it is not evidence of actual source mutation. The independent source hash checks above establish current agreement with the original fixture. The original records and scores were not changed.

These failures differ from the Unicode diagnostic: Unicode produced correct output through the frozen candidate but failed the exact constraint gate. Tab-case failed request validation before any CSV executor could run.

## Supplement condition

The planned separate two-case, three-arm, six-session supplement can test fixed-contract presentation using the existing `run_hermes_session(required_request=request)` path for every arm while retaining frozen production source and candidate bytes. A real TAB must remain one character after JSON decoding; it must not become backslash-plus-`t` in tool arguments. Preservation constraints must remain explicit.

That condition differs from the original natural-language prompt experiment. Preserve all original failures and costs, register new attempt/session/workspace identities, record both original and actual prompt hashes, and report supplemental outcomes and additional costs separately. Measure the source independently of output existence. Supplemental success cannot retroactively repair the original formal scores or establish a learning benefit by itself.

No live calls, source edits, database writes, qualification changes, or original-record edits occurred. Full metadata is in `final-tab-contract-diagnostic.json`; the earlier Unicode diagnostic remains unchanged.
