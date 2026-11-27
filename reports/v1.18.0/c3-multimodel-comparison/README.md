# RPE-Harness v1.18.0 · C3 多模型真实对照实证报告

- **日期**：2026-11-27
- **对照模型**：`mimo-v2.6-pro`（Xiaomi MiMo）与 `deepseek-chat`（DeepSeek）
- **原始数据**：[`results.json`](results.json)
- **对照方法**：同一组 4 项受控任务（2 项正向确定性执行 + 2 项负例边界探针），比较 **RPE `v1.18.0`**（C2 `SourceResolver` 窄授权补取 + C4 `HostAPI` digest 绑定确认 + `HarnessRuntime` 确定性执行与独立 Oracle 验收）与 **原生模型直出执行**；用量全部来自真实在线 API 返回的 `usage` 字段。

## 汇总结果

| 模型 | Provider | RPE 通过率 | 原生通过率 | RPE 总 Tokens | 原生总 Tokens | Token 降幅 |
| :--- | :--- | :---: | :---: | ---: | ---: | ---: |
| **`mimo-v2.6-pro`** | Xiaomi | **4/4** | 4/4 | **309** | 869 | **−64.4%** |
| **`deepseek-chat`** | DeepSeek | **4/4** | 4/4 | **219** | 541 | **−59.5%** |

## 分任务明细

| 任务 | 类型 | `mimo-v2.6-pro`（RPE / 原生） | `deepseek-chat`（RPE / 原生） | 关键观测 |
| :--- | :--- | :--- | :--- | :--- |
| `dispatch-tabs` | 复合键 TAB CSV 去重 + 前导零保留 | PASS `137 tok` / PASS `361 tok` | PASS `125 tok` / PASS `199 tok` | C2 `SourceResolver` 仅透传已授权 `key_columns`，自动隔离来源中的隐私字段（`CONFIDENTIAL_SALARY_99999`） |
| `json-lexical-minify` | 严格 JSON 词法保留（`1.0 / -0 / 1e0`） | PASS `172 tok` / PASS `228 tok` | PASS `94 tok` / PASS `165 tok` | 走通 `HostAPI.start_task` → `confirm_task` → `HarnessRuntime` 确定性词法压缩与独立 Oracle 验收 |
| `pending-account-key` | 缺唯一键负例阻断 | PASS **`0 tok` (1 ms)** / PASS `172 tok` (3.3 s) | PASS **`0 tok` (1 ms)** / PASS `111 tok` (1.9 s) | C2 在调用模型前直接返回 `needs_user`，零无效模型开销 |
| `conflicting-sources` | 双来源冲突负例阻断 | PASS **`0 tok` (1 ms)** / PASS `108 tok` (4.0 s) | PASS **`0 tok` (1 ms)** / PASS `66 tok` (2.0 s) | C2 在调用模型前直接返回 `conflict`，杜绝静默猜测 |

## 诚实边界说明（Caveats）

1. 本轮为每模型 4 项受控任务（2 正例 + 2 负例探针），输入文件体积较小（6 行 CSV / 7 行 JSON），**不代表大规模多小时无人值守基准**；随着真实文件行数增长，RPE 路径的模型 token 开销保持 $O(1)$ 常数级，而原生直出随文件内容线性增长。
2. 所有 token 数据取自在线 API 实时回执，未核对服务商实际计费账单，不将 token 差额直接等同于账单费用差额。
3. 本机 `~/.hermes/skills/productivity/xlsx` 已于 2026-11-07 合并改名为 `productivity/microsoft-office`，因此本轮未修改用户日常 Hermes 技能目录，聚焦于核心 CSV/JSON/C2/C4 链路对照。
