# 1.7 真实实验独立审计：live-producer-3 失败结论

**结论：新登记的有界监督实验仍在 G0 失败。真实 API 请求在 180 秒配置预算后超时，没有原始候选代码或成功写入关联；没有 G1、资格验收、激活、冻结或正式比较。不能声明 1.7 学习闭环或质量／成本收益已验收。**

本审计只读检查真实文件与 SQLite 数字元数据；未调用模型、重播阶段、修改产品或账本、删除路径、读取凭据，未导出原始聊天／工具参数。机器明细为 `final-live-independent-audit.json`；旧中断实验恢复费用明细为 `interrupted-pilot-audit.json`。

## 预登记、源代码与 seal

- 根 `live-producer-3`，预登记 SHA256 `cd7e3a3f2abafeff6806a51151f1e5fc961bbcea93bc7d30bd9d5b9755622cca`。独立重算 19 个 compact record、19 对 reservation/outcome seal、attempt ID、结果状态、reservation 绑定与 usage 一致；本轮未完成 reservation 为 0。
- 6 份训练与 8 份留出输入共 14 个不同 hash；三臂各 16 次、共 48 次计划。每份输入重复 2 次，重复不独立，统计单位为 8 份输入。16 个 case/repeat 组三臂原始 prompt hash 相同；独立重算全部 48 个计划 prompt hash及六个已运行 native 原始 prompt hash。尚无正式实际 prompt/schema/model 对齐结果。
- 登记的 64 个产品 Python 文件在源码、installed 包及 current `wheels-producer` wheel 中逐字节 hash 相同；wheel SHA256 `795680f023aefeef15c109466090c18cf4be3e1fda66a11e840a7e56741f24ec`。本审计未重读日常 host config 内容。测试与 wheel 对齐仅证明实现／包装状态，不证明下面未运行的闭环。

## 真实训练与 G0

- 两 RPE 臂各有 6 个不同真实 SQLite run，跨臂 12 个 ID 互不相同，全部 succeeded/verified，实际 executor.kind 是 builtin。独立读取 input/output，12/12 符合登记 CSV 字面真值，源文件均未改变。
- Native 6 个真实不同宿主会话中 5/6 输出符合真值；`train-compound-native` 输出不存在，保留失败及两个 API error 观察。六个源文件当前 hash 均未改变。失败 compact record 的 `source_preserved=false` 是输出读取失败前保留的默认值，不是源被改写的独立证据。
- 原生流程关联只有 `train-order-native`、`train-delimiter-native` 的 2/6 通过。其余业务正确不会补成调用关联证明。
- Native DB 有 40 条 pre API 观察，但只有 39 个不同 API ID：compound 的 `api:5` 同时重复于两条 pre／两条 error，开始时间相同、接收／结束时间不同。无法仅从该 ID 证明它们代表两个不同付费调用。38 个成功 post/usage ID 全部唯一，raw provider prompt/completion/total/cache/reasoning 与规范字段逐请求相符，独立相加已知 268,390 token。两条 error 无用量，完整总量 null。
- 原生实际请求均 `xiaomi / mimo-v2.6-pro`，已有响应同为 `mimo-v2.6-pro`。G0 只有请求模型身份，没有响应模型观测，不能把 requested model 当作已观测响应。
- G0 六个 source evidence 绑定与 learned 臂真实训练的 input/output/plan/verification hash 全部对应。G0 没有 semantic training packet、previous candidate 或 preprobe，符合初始策略；真实训练锚不代替后续反馈迭代证明。
- G0 session `20261014_215443_e7cc70`：实际 API pre 仅提供 `write_file`、固定 `tool_choice=write_file`、`parallel_tool_calls=false`，schema hash `db15794eab53ec1aa46ce129ff0a2039fe6046900314b41a4032b9823487f21c`。1 pre、0 post、0 file-write observation，candidate hash/code path 为 null，generation link 不成立。
- G0 收据 `failed / generation_timed_out / exit_code=130`。generation wall 202.316501 秒，worker wall 202.372 秒，账本 phase wall 202.432840 秒，均保留启动／终止外围时间。1 个 API 结束观测缺失，已知 token 下界 0，完整总量 null；0 不表示免费调用。retained worker report 与 sealed phase worker 字段完全一致。
- 没有 freeze、eval input 物化或正式 phase。G1/G2/G3、可信 semantic packet 后续导出／preprobe、独立 13 例资格验收、激活重播、candidate version/hash freeze、留出输入直接 candidate 与 fallback 核验均未发生。G0/G1 原本登记为诊断性前后比较，也不是隔离反馈变量的因果消融。

## 正式比较与 replacement pilot 费用

**48 次正式会话计划，0 次启动，0 次完成。** 无正式质量率、复用率、fallback 率或三臂收益结论。正式 phase 中观测 API／token 为 0 仅表示未启动，不是完整比较的估计成本。下表是已发生的训练与 G0，失败均保留。

| 臂 | 阶段数 | 失败 | API 观察计数 | 已知 token 下界 | 完整 token 总量 | 阶段 wall 和（秒） |
|---|---:|---:|---:|---:|---:|---:|
| native | 6 | 1 | 40 | 268,390 | null | 381.114 |
| rpe_learned | 7 | 1 | 1 | 0 | null | 203.038 |
| rpe_no_experience | 6 | 0 | 0 | 0 | 0 | 0.589 |
| 合计 | 19 | 2 | 41 | **268,390** | **null** | 584.741 |

本次 observed makespan 584.893 秒，与阶段 wall 和分别报告。41 个 API 观察计数包含 compound 重复的 error ID；本次独立去重 ID 为 40（native 39 加 G0 1）。成功用量每个请求只加一次，cache 已包含在 canonical prompt/total，没有再次加到 total。账单金额、错误请求未知用量和资源峰值未核实。

## 所有真实尝试累计下界

| 实验 | 结果 | 账本 API 观察计数 | 已知 token 下界 | 完整总量 |
|---|---|---:|---:|---|
| live-final | 第一轮 G0 失败 | 49 | 342,143 | null |
| live-producer | controller 中断，旧阶段未补完 | 25 | 161,355 | null |
| live-producer-3 | 有界 replacement G0 失败 | 41 | 268,390 | null |
| 合计 | 保留失败与中断 | **115** | **771,888** | **null** |

跨三次尝试独立去重的观察 API ID 为 **114**；账本观察计数 115 与它不同的原因是 replacement compound 重复的 error ID。不能把观察条数宣称为唯一请求数或实际计费请求数。

- 中断实验原有前三个 native 已入账：19 次 API／121,776 token。本审计只增加 unmatched `train-delimiter-native` 的 6 个不同 API／39,579 token，一次且只有一次；raw provider 数字与终端工具 ID 的模型 emission/pre/post 一一对应。cache-write 因 provider raw 没有报告仍为 null，不能用 stdout 的 0 填补。
- 中断旧 reservation 仍 incomplete，compact record/outcome 仍不存在。stdout exit 0 证明宿主退出，不补成任务完成；没有重播。完整实验 API／token 总量仍 null。
- 第一轮 G0 的 17,621 下界由保留 worker／compact record／账本一致性核对；私有 host DB 已删除，无法重新逐请求独立相加。第一轮缺失 API 与 replacement 缺失 API／error 用量均保留未知。

## 进程核对与清理限度

- Supervisor PID 58893 的登记身份 `Wed Oct 14 21:48:18 2026`在运行中与 kernel ps lstart 匹配；最终 job `failed / G0_original_code_missing`，该 PID 已不存在。
- 本次 218 个注册 child start identity，加注册 parent／supervisor，共 225 个特定 PID，独立最终查询均不存在。G0 私有根已删除，retained receipt 报告 descendants／外部 executor children 清理完成。
- 旧中断实验 144 个 child start identity、148 个 child/parent PID 均独立检查不存在。原 controller PID/lstart 未记录，仅有失效 exec handle 2751，不编造或认证未记录身份。
- 上述进程事实不等于全轮缓存／日常 state/auth 清理证明；本审计未改变日常状态，最终精确清理由控制者处理。实现测试通过、包装字节相同与运行闭环是否成功分别判断。本次实际失败及证据缺口已足以限定结论。

