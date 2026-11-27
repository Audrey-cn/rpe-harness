# RPE-Harness `v2.1.0` · 120-Iteration Fault-Injection Soak Receipt

- **Script**: [`scripts/soak_test.py`](../../../scripts/soak_test.py)
- **Raw JSON Receipt**: [`soak-report.json`](soak-report.json)
- **Host**: `macOS-27.2.1-arm64-arm-64bit` · Python `3.12.15` · Sandbox `macos-sandbox-exec`
- **Result**: **120 / 120 PASSED** (`3.535 s`, `33.95 ops/sec`, **0 failures**)

## Mixed Workload & Fault Injection Breakdown

| Scenario | Iterations | Outcome |
| :--- | :---: | :--- |
| `tabular.deduplicate` (CSV, leading-zero preservation) | 12 | 12/12 succeeded |
| `tabular.filter` (CSV predicate filter + oracle) | 12 | 12/12 succeeded |
| `json.normalize` (lexical-preserving minify) | 12 | 12/12 succeeded |
| `json.patch` (deterministic set/delete + strict-type oracle) | 12 | 12/12 succeeded |
| `StdioMCPServer` proposal → HostAPI confirm → execute | 12 | 12/12 succeeded |
| Cross-host `handoff_task` (prior confirmation invalidated) | 12 | 12/12 invalidated |
| Mid-flight `cancel_task` fence (no output published) | 12 | 12/12 cancelled |
| Stale digest confirmation probe | 12 | 12/12 rejected (`stale`) |
| Wrong-owner `show_task` probe | 12 | 12/12 blocked (`forbidden`) |
| Tampered plugin output vs independent oracle | 12 | 12/12 blocked (no publish) |

## Post-Soak Invariants

- `zero_failures`: `true`
- `zero_leaked_staging_files`: `true` (`[]`)
- `all_sources_byte_intact`: `true`
- `sqlite_integrity_check`: `"ok"`
- `zero_stuck_running_runs`: `true`

> **Honest Scope Note**: This is a 120-iteration deterministic local soak covering mixed positive executions and injected boundary faults; it is not a multi-day distributed chaos test.
