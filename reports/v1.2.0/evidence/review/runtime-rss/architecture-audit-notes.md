# 架构与资源限制的一致性检查笔记

这是只读检查后的报告整合建议，未修改生产源、架构正文或主报告，也不提前宣布尚在实现的外部执行边界通过。

## 已核查事实

- 1.2 的冻结计划、持久状态、独立 verifier 和版本记录属于独立 Harness，Hermes 为宿主；这与原始“模型可替换、能力保留在 Harness”的方向一致。把可信执行父进程置于宿主沙箱外，属于部署边界的显式实现，不改变候选不能自批 oracle 的原则。
- 实际 RSS 观察器已从创建 `/bin/ps` 进程改为标准库 ctypes/libproc `proc_pidinfo(PROC_PIDTASKINFO)`。ABI 为 Apple SDK 定义的 96 字节结构，resident 偏移 8、单位 bytes。完整响应才采用；仅零响应/ESRCH 返回 None，其余不可观测结果失败关闭。
- 限额仍为 128 MiB RSS 软监督、20ms 轮询；代码 64 KiB、输入/输出各 16 MiB、FD 64、每项验收 3 秒及原候选权限不变。RSS polling 不是硬内存封顶，不保证瞬时峰值；官方 proc_pidinfo 也不使软监控变成硬限额。
- 原 macOS 双重 sandbox_apply 失败由独立 OS 最小复现及两目录布局证据确认。那两场业务 succeeded 的 actual executor 是 builtin，不能写成 candidate 实际执行成功。[安全旧证据](candidate-nested-os-limit.json)保留这种区别。

## 待最终边界验收后写入架构/报告的内容

应明确模型/宿主、认证临时执行入口、可信执行父进程、候选子进程与独立 artifact verifier 的职责。外部入口应保持原固定工作区、契约/计划绑定、幂等和 durable 收据语义，不能变成接受任意 shell/Python 的通用执行权限。认证、请求预算、权限与临时生命周期需按最后实现和独立复核描述，不能仅凭设计意图宣称安全。

只有实际两布局运行的记录显示 `actual_executor.kind=candidate`、candidate version/code SHA、原 plan SHA 和独立验收都匹配，才能写“真实 Hermes 入口已使用候选”。业务 succeeded 但 writer=builtin 只证明安全回退。保留旧 nested RED，与外部执行边界后的 GREEN 分开。

## 报告口径

RSS 专项 6 passed、直接 candidate/evolution 加 RSS 40 passed 仅属于该局部源码修复；根统一全套、外部边界复审、真宿主和最后安装包验收应各自指向精确源码/wheel hash。

正式 36 场保留在 [业务审计数据](../../../artifacts/formal-comparison/README.md)，属于旧 bee526 候选。b9ab 三阶段 crash 属于运行时沙箱修复前包。两者可以保留为历史结果，不能替代 RSS/外部执行边界变更后最终包的验收，也不能泛化成本、持续学习或因果图收益。最终版本报告中不要继续把 beta/旧快照“无阻断”结论当当前发布结论。

目前 README 的内存条款引用版本报告、未声称硬封顶；architecture.md 的独立 Harness 和原计划验收原则一致。最终整合时需要补清楚物理执行边界，并采用上述实际资源约束和实际 executor 身份口径。
