# Unicode contract diagnostic

For `eval-unicode-r1-rpe_learned`, the sole normalized contract mismatch is `constraints`: the actual succeeded run recorded `[]`, while the required request contains `preserve_original` and `preserve_order` (normalized order: `preserve_order`, `preserve_original`). The CSV is correct, and the actual frozen learned candidate did run. The original workflow score remains failed.

This diagnostic read the active database through SQLite `mode=ro` in a read transaction. It did not invoke models, change source, mutate the database, alter qualification, or modify the original evaluation record. The JSON companion contains metadata, hashes, public column literals, and verification checks; it contains no raw chat or private provider payload.

## Literal CSV and hash check

Both files are stable regular files with a UTF-8 BOM. The literal header is `描述|索引`; the exact key column is `索引`, delimiter is `|`, and encoding is `utf-8-sig`.

| File | Bytes | Data rows | SHA-256 | Check |
| --- | --- | --- | --- | --- |
| Input | 47 | 3 | `26aa47515eeae7b49991d4ef10ba499d4a6efe9e1bf5dd22319c7fd0afc5a17b` | Matches registered public fixture |
| Output | 39 | 2 | `24919c76f81cfae9a12582d5d12b5c7123f3366d7ae7d7e47ea7cfa794c7e7f3` | Rows exactly match public expected rows |

The durable product verification passed all recorded checks: duplicate removal, first-record preservation, headers, record order, request binding, and source unchanged. Input/output paths, exact key-column literals, `keep=first`, encoding, delimiter, and implicit `sheet=null` all agree between actual and required normalized contracts.

## Actual execution and exact mismatch

The native return and post-tool event link to real succeeded run `ab082cae48c2488689895abb8e244c2d` in session `20261014_230010_aa0b62`. Its artifact was published. Artifact and execute-step `actual_executor` receipts agree:

- `kind=candidate`
- `code_sha256=3afd6be78a69caf7b055ae5afb8a69ef0deb7b542593dfd47f7838153cab9c43`
- `candidate_version_id=adc6c2804d9d6356cef4ed40a28b2b552201f634e6a699eefcf09ca60b4f182c`

Both identities match the frozen candidate. The plan's `executor_id=builtin/csv-deduplicate` is a declared ABI label and does not establish built-in execution; the actual executor receipts establish candidate invocation.

The actual normalized contract hash and native receipt hash are both `e79d265e8fb7efd2f33b1e90a7eb97411b0a9953890c58ad5bb6588865e07f8f`. The required contract hash is `115d7106235f4795fd4ab535df88895fbd83a1deca4c0f2f4f5783109e50d8b6`. A recursive comparison finds exactly one differing field:

```json
{"path":"constraints","actual":[],"required":["preserve_order","preserve_original"]}
```

`hermes_gate._verify_run` checks this equality before returning an accepted run. Its `required_contract_mismatch` is therefore correct for the contract the experiment required; neither Unicode decoding nor key-column selection caused this failure.

## Contract exposure and interpretation

The registered `_materialize` request contains both preservation constraints. The shared `_prompt` states preservation behaviors naturally, but its parameter JSON contains only key columns, delimiter, encoding, and keep. It does not contain a `constraints` key or either structured constraint literal. The formal `_host_session` launcher call uses strict entry mode without passing `required_request`, so no fixed contract is injected or bound at admission.

Reconstructing the shared prompt gives SHA-256 `68cb848714058c633f9209a7a366d1bdf17d5f9a629adb4dd28084942fe4ed0f`, equal to the record's expected, original, and actual prompt hashes. All four reviewed source files match their registered source hashes.

The observed behavior is structured contract noncompliance accompanied by an experiment contract-exposure gap. Metadata cannot prove intentional model deviation. The gate does not miscompute the comparison: it correctly rejects the empty constraint list required by the declared acceptance rule. The output and candidate execution still provide separate positive business/runtime evidence.

Because the gate withheld an accepted `run_id`, `_host_session` persisted `run_id=null`; the production freshness check then set `fresh_run=false` and `error_code=fresh_session_or_execution_missing`. Those fields describe accepted-workflow evidence, not absence of a physical execution. The native events and durable artifact independently establish the execution above. The original row remains `workflow_verified=false`, `candidate_direct_success=false`, and failed; this diagnostic does not retroactively award either metric.

## Valid future supplement

Preserve the original registration, rows, scores, candidate freeze, and cumulative cost evidence. For a separate, transparently registered diagnostic supplement, expose the same explicit public constraint JSON to every arm, use fresh session/workspace/attempt identities, and retain the same frozen candidate hash. First verify contract forwarding and admission with local checks. If Harness arms also use `required_request`, preregister that intervention and record both original and injected prompt hashes.

Report supplemental outcomes and their additional costs separately. Do not rewrite the original arguments or durable event, silently default empty constraints to the required list, remove constraints from the original gate, or rescore the original workflow as successful. A successful supplement would test the task-presentation hypothesis; it would not change the original formal result or establish learning benefit by itself.

Full structured evidence is in `final-contract-diagnostic.json` beside this file.
