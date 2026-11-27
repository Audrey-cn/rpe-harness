# RPE-Harness `v2.3.2` · 5-Family 10-Step Long-Horizon Engineering Pipeline Benchmark

- **Date**: `2026-11-27` · **Host**: `macOS-27.2.1-arm64-arm-64bit` · **Python**: `3.12.15`
- **Workload**: 10-step chained pipeline across **300-row SQLite DB, 200-row leading-zero CSV, 160-row chained filter, 100-key nested JSON, 60-line `.env.production` with inline comments, C2 conflict gate, destructive SQL guard, atomic cancel fence, and MCP → Local Approval Inbox execution**.
- **Reproducible Script**: [`scripts/long_horizon_benchmark.py`](../../../scripts/long_horizon_benchmark.py)

## 5-Family Head-to-Head Summary

| Model Family | Harness Mode | RPE Pass (All 10 / Paired 7) | Native Pass (Paired 7) | RPE Total Cost | Native Total Cost | Cost Reduction | Speedup | Raw Receipt |
| :--- | :--- | :---: | :---: | ---: | ---: | ---: | ---: | :--- |
| **`qwen4-max`** (Alibaba) | Raw Chat API | **10/10 (7/7)** | 5/7 *(dedup & filter failed)* | **709 tok** | 35,209 tok | **−98.0%** | **11.5×** | [`results.json`](results.json) |
| **`mimo-v2.6-pro`** (Xiaomi) | Raw Chat API | **10/10 (7/7)** | 4/7 *(dedup, filter & .env failed)* | **727 tok** | 33,819 tok | **−97.9%** | **13.0×** | [`results-mimo-v2.6-pro.json`](results-mimo-v2.6-pro.json) |
| **`deepseek-chat`** (DeepSeek) | Raw Chat API | **10/10 (7/7)** | 5/7 *(dedup & filter failed)* | **494 tok** | 23,119 tok | **−97.9%** | **6.8×** | [`results-deepseek-chat.json`](results-deepseek-chat.json) |
| **`gpt-6.1-sol`** (OpenAI) | `codex-cli` Agent CLI | **10/10 (7/7)** | 5/7 *(dedup & filter failed)* | **75,232 tok** | 112,981 tok | **−33.4%** *(incl. CLI system prompt)* | **2.6×** | [`results-gpt-6.1-sol.json`](results-gpt-6.1-sol.json) |
| **`Gemini`** (Google) | Antigravity Subagent | **10/10 (7/7)** | 5/7 *(dedup & filter failed)* | **632 chars** | 22,482 chars | **−97.2% chars** *(tokens unexposed)* | — | [`results-gemini.json`](results-gemini.json) |

## Key Findings

1. **50/50 vs 24/35 (100% vs 68.6%)**: Every single model family failed on direct 200-row CSV deduplication and 160-row chained filtering when processing raw data in-context (`mimo-v2.6-pro` also dropped/altered the 60-line `.env.production` inline comments). Under RPE-Harness, all 5 families achieved **10/10 byte-exact Oracle pass**.
2. **`O(1)` vs `O(N × Steps)` Cost Scaling**: Across all 3 raw-API families, RPE cut total tokens by **97.9%–98.0%** (`~494–727` vs `~23k–35k` tokens) and ran **6.8×–13.0× faster**.
3. **Honest Harness Accounting**: `codex-cli` counts include ~15k tokens of fixed CLI system/tool prompt per fresh `codex exec` invocation (still **−33.4%** fewer tokens and **2.6×** faster overall while lifting accuracy from `5/7` to `7/7`). Antigravity's `Gemini` subagent does not expose raw provider tokens, so exact output characters (`632` vs `22,482`, **−97.2%**) are reported instead.
