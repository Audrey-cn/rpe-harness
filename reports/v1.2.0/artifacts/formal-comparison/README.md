# 正式原生对照的可审核业务数据

本目录按 `round-N/case_id/arm` 保留正式 3 轮 × 6 场景 × 2 组的 36 场实际合成业务输入、当时存在的输出，以及 integrated 会话关联的安全 Harness 运行记录。数据来源为真实原生会话；业务数据本身是人工合成夹具，不是日常用户任务。

来源与每份文件 SHA256、原始临时路径、36 场索引见 [保留清单](retention-manifest.json)；宿主事件和计费观察见 [原正式总报告](../../evidence/hermes-repeated-comparison.json)。此处只保留白名单文件：`input.csv/xlsx/json`、`output.csv/xlsx` 和审核过字段结构的业务 `run.json`。没有额外复制模型脚本、Hermes home、`state.db`、聊天 stdout/stderr、skills、缓存或凭据，也没有读取 `.env`。

该 36 场观察对应旧候选 wheel SHA256 `bee526c7cb37844d2036602a3732b678c1d365f6e832d33c936733afbc47e1fc`。后来补齐的入口 guard 与运行时父目录沙箱读取修复不在该快照内；这份记录不能作为最终代码的 36 场复验。复制过程没有再次运行业务、调用模型或请求 API。

## 完成与观察边界

- `completed=true` 表示所有 36 个预算尝试已记录，包括超时，不表示 36 场宿主都成功完成。native 实际完成 12/18、超时 6；integrated 完成 18/18、超时 0。
- 原报告接受 native 12/18（支持任务成功 9、正确拒绝 3）、integrated 15/18（支持任务成功 12、正确拒绝 3）。这些是旧快照在本组固定任务上的描述，不是一般 Agent 胜负结论。
- 原报告 `native_hermes_baseline_measured=false`，兼容且成功的配对小计仅 9 对；部分 native usage 未知。不能把完整 usage 子集当完整费用，不能从这份数据泛化 token 节省、成本优势、因果图收益或持续学习收益。
- 20 条 durable run 关联到 18 个 integrated 会话；前两轮 `reject_json` 会话各出现两条 run。它们分别保留在 `runs/RUN_ID/run.json`，没有合并或隐藏额外调用。

原绝对临时路径会在清理后失效；`retention-manifest.json` 把原路径映射到复制数据。原计划和收据 hash 按原路径/原字节保留，不能直接把路径改成复制目录后声称计划 hash 未变。审计可重新读取本目录数据并对照 manifest/input/output hash；重做任务应创建新的路径、计划和运行身份。

缺少 output 表示当时没有白名单业务输出（例如拒绝或超时），没有用空文件补齐。数据 hash 或数字不一致会原样记入 manifest 的 `data_and_count_discrepancies`，不会改写原观察。
