# 1.10 公开起草合同修正的独立审阅

审阅范围严格为 `f64eabe5ccee06ccf976fb04e4b8ff02c28bb730..2b525f590e8d61c49f06c15f080bb159fcd259e9`：`hermes_review.py`、对应既有测试及 `docs/task-review.md`。未重新审核或改写正式试验结果，未修改生产代码或实现者测试。

**结论：本次差异内，没有发现尚未解决的 P1/P2 问题。** 新 schema 和说明文字不构成授权，也没有证明修正后真实模型会正确遵守格式。

## 判断

- **执行边界保持。** 新增的 `unsupported` 是非执行哨兵；父层没有为它注册能力。确认、执行、原始/有效参数出处、RPC、持久状态及未解条件的验证代码均未改变。合法 capability 仍须使用精确机器 ID，不支持的动作必须保留在 unresolved，不可替换成相近业务。
- **枚举是输入指引。** 能力、约束与字段指针从固定能力定义生成；keep/style/mode 和 1–32 行约束在 schema 中明确。Hermes 官方 SDK 本身不执行 enum 语义验证，不能把出站 schema 当成宿主强制保证。逐能力合法 flags、当前草案字段、缺项、行数与可确认状态仍由父层检查。
- **真实业务条件没有被豁免。** 说明区分业务缺项、冲突、不支持动作与后续人工确认、既定默认值、父层运行检查；它仍要求前一组完整保留并阻止执行，不允许清空 unresolved 或推断未定业务键。不能把这段文字解释成遇到用户明确未决定的业务参数就擅自采用默认值。
- **默认值与代码一致。** 表格执行器实际默认 `keep=first`、`encoding=utf-8-sig`、`delimiter=','`；显式用户值保留，真实 TAB 不会被逗号覆盖。JSON 实现严格 UTF-8 解码、只改结构空白并输出一个末尾 LF；没有新增 encoding 参数。XLSX inspection 只有 `preserve_original` flag，原有行序和 JSON 类型属于能力本身；新说明没有添加实际不可执行的 `preserve_order`。
- **运行前提仍会阻断。** 文件、输出缺席、路径、格式、选定 sheet 与 helper/adapter 信任由父层检查；文案明确不准模型声称检查已通过。说明它们不属于缺失业务决定，不代表可以绕过检查。默认值仍展示在机器合同内并纳入人工确认。
- **没有新增目录信息输出。** 新 schema 只从静态定义投影公共 ID、flag、选项名与字段名，不携带实际目录源、helper 路径、skill binding 或私有配置。共享说明不包含本机绝对目录；本次差异没有将配置中的实际目录条目加入模型消息。两臂继续使用同一 `REVIEW_CONTRACT`。

## 针对性验证

仅运行一轮适用的现有 SDK 与父层检查：

```bash
PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/review-independent \
/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/venv/bin/python -B -m pytest \
  -p no:cacheprovider \
  --basetemp=/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2/review-independent/contract-pytest \
  tests/test_hermes_review.py::test_installed_sdk_loads_validates_and_invokes_proposal_without_network \
  tests/test_hermes_review.py::test_installed_sdk_final_http_body_and_fail_closed_override_guard \
  tests/test_proposal.py::test_incomplete_unsupported_or_model_authorized_drafts_cannot_be_confirmed \
  tests/test_proposal.py::test_implicit_executor_defaults_are_shown_for_review \
  tests/test_proposal.py::test_bounds_and_invalid_rows_are_visible_without_becoming_confirmable \
  tests/test_proposal.py::test_invalid_constraint_types_are_visible_nonconfirmable_reviews -q
```

结果：**22 passed in 17.57s，退出码 0，无 skip。** 包含实际已安装 SDK 的五种合法/非法参数情形及最终 HTTP MockTransport schema 检查。非法 capability 说明字符串、非法 pointer 和 XLSX 错误 flag 由父层阻断；不是 SDK 自动验证 enum。合法草案仍为待确认，不自动执行。

另在 `review-independent/probe_contract.py` 做一次不同于实现者测试的父层探针，所有文件在本轮 owned-root：

| 实际条件 | 结果 |
|---|---|
| 带 UTF-8 BOM 的 CSV，省略可选编码/分隔符/keep | 实际探测成功；完整合同为 `utf-8-sig`、逗号、first，默认展示供审阅，尚无确认或输出 |
| 显式 UTF-8 与真实 TAB 分隔 | 合同保留显式值，未被新默认说明覆盖 |
| `unsupported` 哨兵并报告求和动作 | unsupported；求和条件原样保留；确认被拒 |
| 合法去重加额外求和动作 | needs_context；额外动作原样保留；确认被拒 |
| 业务唯一键尚未决定 | needs_context；未定条件原样保留；确认被拒 |

这些探针证明已报告的 unresolved 被保留和阻断，不证明模型没有漏报其他自然语言条件。

没有复制实现者测试、调用新模型、访问网络 provider、执行全回归或重新评分。`git diff f64eabe..2b525f5 -- reports/v1.10.0` 为空，既有正式与 pilot 负结果保持不变。

## 固定文件摘要

| 文件 | SHA256 |
|---|---|
| `src/rpe_harness/integrations/hermes_review.py` | `4d22d03aafd652d0c552bd15b415d0512aac4a670699d8b8d588bc4e1393b627` |
| `tests/test_hermes_review.py` | `c0edb553a0468916331bccdbd4e61a234fe4e3332903ad800944544fbd36bd6f` |
| `docs/task-review.md` | `ccbb458ec28038786257aa4248fcb58e637f9c3fdbbd66c639755ce45f09509d` |

本报告只支持该合同修正没有放宽应用授权边界且说明与现有执行行为一致。它不证明模型已改善，不消除人工语义审阅，也不取代根代理的全量、wheel 与最终交付核验。
