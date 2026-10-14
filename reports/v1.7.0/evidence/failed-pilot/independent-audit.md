# 1.7 真实实验独立审计：live-final 中期结果

**结论：本次 pilot 未通过。G0 真实模型生成超时，没有候选原始代码或已证明的文件写入关联；没有 G1、资格验收、激活、冻结或正式评估。不能据此声称学习闭环、新程序生成、新输入复用或质量/成本收益。** 本结论只依据本次真实运行文件及只读数据库，不使用 mock 测试作为运行证据。

审计根：`live-final`；预登记 SHA256：`76a08b7ab7ea00f0b716f6074a999b9d9177cb7a7694f7bd200384b97949024c`。本审计未调用模型、重跑任务、修改产品/实验文件或读取凭据/原始聊天日志。

## 登记与证据完整性

- 独立重算预登记、19 个 compact record、19 对 reservation/outcome 的 SHA256 seal；outcome 的 reservation 绑定、attempt ID、结果状态及 usage 与 compact record 全部对应。当前没有未完成 reservation、重复 ID 或正式生产 phase。
- 预登记为 6 份训练、8 份独立未见输入、每份 2 次依赖重复、3 臂共 48 次会话；三臂各 16 次。14 份输入 hash 互不相同，同一 case/repeat 的三臂原始 prompt hash 相同。统计单位为 8 个输入，重复不独立。
- 配置使用本轮 installed Python，登记了 64 个产品源文件 hash；G0 后最多 3 次反馈生成、每次 6 turns、配置 timeout 180 秒。此次只执行 G0。实际生成阶段 wall 为 202.848 秒、worker 总 wall 为 202.901 秒，包含启动/终止等外围时间；账本没有将它压缩为配置的 180 秒。
- 目前各臂工作区没有 materialize 任何 `eval-*/input.csv`；没有 freeze 文件。48 次正式比较均未启动。

## 真实训练与 G0

- 两 RPE 臂的 SQLite 各有 6 个真实不同 run，跨臂 run ID 不同；全部 succeeded/verified，实际执行器均为 builtin。只读重算源/结果 hash，并按登记的字面 CSV 真值独立解析；12 个结果全部符合。
- 原生 6 次真实宿主训练均业务正确、源保留；独立检查实际文件的字面真值和 hash 均符合。流程关联仅 4/6 通过；`train-quoted-native` 和 `train-exact-native` 的业务正确不能补成调用关联成功。
- 原生宿主 DB 有 6 个不同 session、45 对唯一且匹配的 API pre/post 记录；独立相加 DB 中实际 provider `total_tokens` 得到 **324,522**，与六份训练记录及账本一致。六次均报告 `xiaomi / mimo-v2.6-pro`，响应模型一致。
- G0 的 6 个源证据绑定与 learned 臂真实 DB 的 input/output/plan/verification hash 全部对应，恰好覆盖该臂 6 个训练 run。G0 无 semantic training packet、无 previous code，符合登记的初始策略；不能将已有成功训练锚误称为反馈修订已经发生。
- G0 收据为 `failed / generation_timed_out / exit_code=130`；4 个 API pre、3 个 post，1 个请求没有结果观测；实际 post 工具记录为 read_file 1 次、search_files 1 次、terminal 2 次，没有 write_file。`candidate_sha256=null`、`code_artifact_path=null`、`generation_link.proven=false`、file-write observations 为 0。
- retained worker report 与 sealed generation result 的 worker 字段完全相同。G0 模型同为 `xiaomi / mimo-v2.6-pro`。其原始 host DB 已随私有 worker 根删除，因此此审计可核对三份保留记录中 17,621 的一致性，不能再从独立逐请求 usage 重算这个 G0 下界；总量必须保持未知。

## 全部已发生阶段成本

每个 reservation 仅计算一次；G0 失败属于 learned 必要探索，未从累计成本排除。以下为当前 pilot 已完成阶段，**不是生产比较**。

| 臂 | 阶段数 | 失败阶段 | 观测 API 请求 | 已知 token 下界 | 完整 token 总量 | 阶段 wall 之和（秒） |
|---|---:|---:|---:|---:|---:|---:|
| native | 6 | 0 | 45 | 324,522 | 324,522 | 340.529 |
| rpe_no_experience | 6 | 0 | 0 | 0 | 0（本地 CLI 阶段） | 0.555 |
| rpe_learned | 7 | 1 | 4 | 17,621 | 未知 | 203.514 |
| 合计 | 19 | 1 | 49 | **342,143** | **未知** | **544.597** |

登记时间至最终 outcome 的 observed makespan 为 **544.728 秒**，与阶段 wall 之和分别报告。G0 缺 1 个请求结束观测，不能把未知成本当零；cache token 不重复加入 canonical total，账单金额与资源峰值没有独立实测结论。后续诊断、修复或新登记实验若发生，必须另记成本并在最终全轮汇总中保留本次失败的 342,143 已知下界。

## 未证明事项与清理边界

反馈驱动生成、独立 13 例资格验收及激活重播、冻结后候选直接调用、builtin fallback、正式 prompt/schema/model 对齐、16 次 learned 复用和 48 次三臂质量/成本比较，均因尚未执行而未证明。当前没有可比较的 G0/G1 行为差异，更没有反馈单因素因果证据。

G0 收据报告进程清理完成、remaining descendants 为空，且实际 learned 根下已无 `proposal-worker-*` 私有目录。这里只确认该 worker 的清理事实；native 会话、实验库、全轮缓存、日常状态核对和最终精确清理由控制者继续负责，本审计未将全轮清理标为完成。

本次发现的是实际生成失败及证据缺口；未发现已完成阶段的 seal/账本重复计数、已知成本被归零或虚假通过。应保留本次失败记录；后续若改变生成策略，应使用新的预登记，不能覆盖本次结果。
