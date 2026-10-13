# 宿主 error_code 诊断遗漏的最小修复

[RED/GREEN紧凑结果](summary.json)：实际HostEventStore保存rpe_task_returned/error_code-only失败元数据，无durable run。旧逻辑遗漏该事件，RED为1 failed、1 passed；修复后evolution全13 passed、无skip，1.20秒。真实本地CSV执行成功且无error_code的返回不进入host_errors；未虚构verified成功、未调用模型或重跑安全实验。

只按现存结构字段保存code、可用type、event ID与metadata/evidence hash；缺少type保持空，不做关键词分类或根因推断，不扩展verified feedback或自动升级范围。当前源码快照在 [cycle-reviewed.py](cycle-reviewed.py)，测试在 [test_evolution.py](test_evolution.py)，[保留清单](retention-manifest.json) 给出SHA。

此局部GREEN不替代根任务最终全套或wheel验证，也不修复 [外部执行目录替换RED](../external-executor/README.md)。本记录仅证明诊断字段采集修复；原安全证据和历史版本报告保持原样。
