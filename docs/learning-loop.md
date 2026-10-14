# 真实反馈与工具生成的使用边界

1.7将已有CSV候选管道用于真实反馈实验：指定已独立验证的运行，明确授权导出少量训练样例，生成器提出新的程序实现，再由独立验收器决定能否启用。CSV业务能力本来已有；生成新实现、反馈改善行为、复用更省是三个需要分别证明的结论。

## 训练材料

Python入口为 `build_training_packet(db_path, run_ids, authorize_data_export=True)`，默认不允许导出。必须指定1–8个真实已验证的CSV运行；源和结果重新核对，单份源/结果以及导出的文本/表格值各不超过16KiB，完整packet不超过64KiB。返回 `rpe.training-feedback/v1`，含公开业务参数、训练文本、独立正确结果和来源hash，不包含源文件路径等元数据。

训练原文会进入生成提示及本地收据，因此只导出已明确授权用于训练的材料。普通无packet调用保持结构化摘要路径。`validate_training_packet(db_path, packet)`从可信运行库重新构造并比较全部字段，单独一个packet hash不能证明来源真实。

## 生成和独立启用

已有安装环境中的命令示例；STATE、DB、PUBLIC_PACKET、G0应替换为用户明确管理的绝对路径：

```sh
rpe-harness evolution worker --owned-root STATE --db DB \
  --kind csv_executor --timeout 180 --max-turns 6 --producer-only \
  --training-packet PUBLIC_PACKET --previous-candidate G0
```

`--producer-only` 使用 Hermes 官方运行时工具集和中间件，只暴露原生 `write_file` 到唯一 `candidate.py`；公开契约与反馈直接提供，成功写入后才进入总结。它是显式选项，旧调用默认模式保留。

生成器会实际探测前一份原始代码，使用可信训练反馈修订，只提出候选。收据包含原始代码路径/hash、来源绑定、实际API/工具写入关联、已观测成本及清理状态。缺失关联保持未证明；AST变化只作诊断。超时、无效代码和观测错误不会自动变成合格升级。

Hermes CLI 可能省略工具调用 ID。producer 模式只在唯一终端参数与唯一原生写入证明的内容、路径、参数 hash 完全一致时，关联原生事件中的真实 ID；已有冲突 ID、重复记录或缺失 API 链均拒绝。`proof_source=producer_post_tool_guard` 表示原生中间件已检查实际文件，不能替代后续资格验收。

候选仍需 `evolution validate PROPOSAL_ID` 及 `evolution apply PROPOSAL_ID`，通过原有独立资格验收和激活重播。生产每次仍验收结果；坏版本隔离、回退与版本回滚保留。候选不能审批自己、改核心或放宽验证器。

## 学习实验

`python -m rpe_harness.experiments.learning_comparison --help` 提供显式pilot/formal入口。pilot只处理公开训练、真实生成、资格验收和冻结；formal才开放未见输入。预登记不可覆盖，失败和未完成的保留ID不自动重跑；代码、配置或候选变化需要另建登记。

三组为原生执行、RPE固定内置、RPE冻结候选。各组保留自己的过去工作区，不能互读；两个RPE组的工具策略相同，原生策略差异单独报告。8个未见输入各2次会话，共48次；重复会话共享状态，统计单位仍为8个输入。

账本计入实际训练、所有生成与失败、训练探测、资格验收、激活重播、维护和生产。G0既然参与G1生成，其成本也属于学习组。整轮成本与复用边际成本分开，缺测保留null与已知下界，缓存token和账单分开；软资源上限不冒充实测峰值。

完整反馈/生成/资格验收链与一次真实未见输入复用可以证明局部闭环；16/16复用、48次比较完整性、质量及累计成本改善另外展示。不能用闭环标志代替成功率，也不能用完美成功率作为反馈是否送达的证据。生成前后的样例、前代码和探索预算同时改变，G0/G1只是诊断性前后参考，不能归因于反馈单一因素。

本流程没有安装长期服务或定时任务，也未验证任意技能自动学习。工作根、历史、缓存和进程的精确清理由调用者的本轮ownership管理，长期能力资产及用户既有文件不属于测试清理范围。

当前Xiaomi生成profile显式发送thinking.type=disabled和8192输出token上限，仅作用于producer-only；正式原生/RPE生产参数不变。MiMo可能忽略非auto的tool_choice，故“写入→总结”是客户端准入约束，不能声称模型服务端必然服从。官方字段见[MiMo API](https://mimo.mi.com/docs/en-US/api/chat/openai-api)。

本机1.7真实对照已完成48次：原生16/16、内置14/16、学习15/16正确产物；学习程序完整证明的直接复用为14/16。任务约束遗漏和Tab转义错误仍会使准入拒绝；已有`required_request`绑定补验为4/6，未在三次任务尝试预算内消除Tab错误。自动化调用应显式管理结构化参数与有限纠错预算，不能把本轮结果当作稳定无人值守或学习收益证明。详见[1.7报告](../reports/v1.7.0/1.7开发与验收报告.md)。
