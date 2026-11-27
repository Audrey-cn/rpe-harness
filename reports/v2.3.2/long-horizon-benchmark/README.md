# RPE-Harness `v2.3.2` · 10-Step Long-Horizon Engineering Pipeline Benchmark

- **Model**: `qwen4-max` (OpenAI-compatible Chat API, `temperature=0`)
- **Host**: `macOS-27.2.1-arm64-arm-64bit` · Python `3.12.15`
- **Script**: [`scripts/long_horizon_benchmark.py`](../../../scripts/long_horizon_benchmark.py) (reproducible with `BENCH_API_BASE` + `BENCH_API_KEY` + `BENCH_MODEL`)
- **Raw JSON Receipt**: [`results.json`](results.json)

## Headline Summary

| Metric | **RPE-Harness `v2.3.2`** | **Native `qwen4-max` Direct** | Delta |
| :--- | :---: | :---: | :---: |
| **Paired Step Accuracy (Oracle)** | **7 / 7 (100%)** | 5 / 7 (71.4%) | **+28.6 pp (0 silent errors)** |
| **All 10 Pipeline & Safety Steps** | **10 / 10 (100%)** | N/A *(no guardrails/HITL)* | — |
| **Total Token Cost** | **709 tok** | 35,209 tok | **−98.0% (49.7× fewer tokens)** |
| **End-to-End Wall-Clock Time** | **8.95 s** | 102.93 s | **11.5× faster** |

## Step-by-Step Breakdown

| Step | Task (Larger-File Chained Pipeline) | RPE Pass | Native Pass | RPE Tokens | Native Tokens | RPE Time | Native Time |
| :-: | :--- | :-: | :-: | ---: | ---: | ---: | ---: |
| 1 | `sqlite.query_export` (300-row DB → active APAC CSV) | ✓ | ✓ | 109 | 7,581 | 1.42 s | 20.30 s |
| 2 | `tabular.deduplicate` (200-row CSV with leading zeros) | ✓ | ✗ | 144 | 6,837 | 1.67 s | 19.91 s |
| 3 | `tabular.filter` (160-row chained CSV → APAC) | ✓ | ✗ | 102 | 4,732 | 2.15 s | 12.25 s |
| 4 | `json.patch` (100-key nested JSON config) | ✓ | ✓ | 193 | 7,796 | 2.06 s | 25.62 s |
| 5 | `json.normalize` (100-key minify + lexical `1.0`) | ✓ | ✓ | 0 | 6,067 | 0.03 s | 15.53 s |
| 6 | `env.patch` (60-line `.env.production` + inline comments) | ✓ | ✓ | 161 | 2,100 | 1.32 s | 7.56 s |
| 7 | `C2 SourceResolver` conflict gate (pre-LLM block) | ✓ | ✓ | 0 | 96 | 0.01 s | 1.77 s |
| 8 | Destructive SQL write guard (`DROP TABLE` blocked) | ✓ | — | 0 | 0 | 0.01 s | — |
| 9 | Stale digest + atomic cancel fence | ✓ | — | 0 | 0 | 0.02 s | — |
| 10 | MCP `rpe_start_task` → Local Approval Inbox execute | ✓ | — | 0 | 0 | 0.27 s | — |

> **Honest Scope Note**: In the Native arm, the model processes file payloads directly via chat completion (comparing pure-LLM execution against RPE's structured-draft + deterministic-kernel architecture). As file size scales from 6 rows to 200–300 rows, RPE's token cost remains `O(1)` (~100–190 tokens per step) while Native token cost and error rates scale linearly with file size.
