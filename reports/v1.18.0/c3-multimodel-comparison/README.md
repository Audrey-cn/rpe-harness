# RPE-Harness v1.18.0 · C3 四大家族多模型真实对照实证报告

- **日期**：2026-11-27
- **对照模型（4 个模型家族 / 两种运行形态）**：
  1. **`mimo-v2.6-pro`**（Xiaomi MiMo · 原始 Chat Completions API）
  2. **`deepseek-chat`**（DeepSeek · 原始 Chat Completions API）
  3. **`gpt-6.1-sol`**（OpenAI · 本机 `codex-cli` Agent CLI 外壳）
  4. **`Gemini`**（Google · Antigravity 隔离 `research` 子代理）
- **原始数据**：[`results.json`](results.json)
- **对照方法**：同一组 4 项受控任务（2 项正向确定性执行 + 2 项负例边界探针），比较 **RPE `v1.18.0`**（C2 `SourceResolver` 窄授权补取 + C4 `HostAPI` digest 绑定确认 + `HarnessRuntime` 确定性执行与独立 Oracle 验收）与 **原生直出执行**。

## 四大家族汇总结果

| 模型 | 接入形态 | RPE 通过率 | 原生通过率 | RPE 总开销 | 原生总开销 | 降幅 |
| :--- | :--- | :---: | :---: | ---: | ---: | ---: |
| **`mimo-v2.6-pro`** | 原始 API | **4/4** | 4/4 | **309 tok** | 869 tok | **−64.4%** |
| **`deepseek-chat`** | 原始 API | **4/4** | 4/4 | **219 tok** | 541 tok | **−59.5%** |
| **`gpt-6.1-sol`** | `codex-cli` Agent 外壳 | **4/4** | 4/4 | **28,156 tok** | 88,086 tok | **−68.0%** |
| **`Gemini`** | Antigravity 子代理 | **4/4** | 4/4 | **159 chars** *(仅 2 次调用)* | 203 chars *(4 次调用)* | **−21.7% 输出字符** *(未暴露底层 token)* |

## 关键发现

1. **跨四大家族 100% 行为一致性（16/16 全绿）**：无论底层模型是 MiMo、DeepSeek、GPT 还是 Gemini，只要接上 RPE `v1.18.0` 的窄起草契约 + 确定性执行器 + 独立 Oracle，4 项任务全部通过独立验收。
2. **越重的 Agent 外壳，RPE C2 前置拦截的收益越大**：
   - 在轻量原始 API（MiMo / DeepSeek）上，RPE 减少 **59.5%–64.4%** token；
   - 在真实重型 Agent CLI 外壳（`codex-cli`）上，因为每次启动都自带系统/工具提示词开销（约 `11.7k–25.5k tok/次`），C2 `SourceResolver` 在 `1–2 ms` 内零 token 拦截掉 `pending-account-key` 与 `conflicting-sources` 两项负例，直接省去 **~3.7 万 tokens 与 81 秒墙钟等待**，整轮降幅达到 **−68.0%**。
3. **隐私字段天然隔离**：在 `dispatch-tabs` 中，C2 `SourceResolver` 仅透传已授权字段 `key_columns`，自动过滤掉源记录里的隐私噪声 `CONFIDENTIAL_SALARY_99999`。

## 诚实边界说明（Caveats）

1. **口径透明**：`codex-cli` 的 token 计数包含完整的 CLI Agent 系统提示与工具定义开销，不可与原始 Chat Completions API 绝对值横向混比，仅在同一外壳内做 RPE vs 原生纵向对比；Antigravity Gemini 子代理未暴露底层 `usage.total_tokens`，故诚实报告输出字符数与调用次数差值，不估算伪造 token。
2. 本轮为每模型 4 项受控小文件任务（6 行 CSV / 7 行 JSON），不直接外推大规模多小时无人值守基准；未核对服务商实际计费账单。
