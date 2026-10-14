# Task 2 上下文恢复审查

范围：隔离工作树的 `src/rpe_harness/context.py`、`tests/test_context.py`；依据 `docs/superpowers/specs/2026-10-14-rpe-v16-design.md` 的上下文恢复要求。初审基线 `643e1f1`，复审依据 `evidence/context-fix-review.diff` 及当前文件。未审查其他并行任务。

## 初次发现

P2：原 `context.py:385–393` 的 `get_recovery()` 仅验证恢复记录自身的哈希与身份，未重新读取来源或检查鲜度。首次恢复为 ready 后，来源删除、篡改或过期，重启读取仍返回旧 ready。因此该方法只能读取历史，不能单独用于冻结计划前的当前有效性复查。

初审其余项通过：capture 同源身份不可变、完整来源哈希绑定正文/事实/session/时间、显式 refs 与会话隔离、不从正文推断字段、冲突明确返回、8 refs / 256 KiB 来源 / 16 KiB facts/result / 32 required 字段及深度和非有限数限制。

## 修复复审

P2 已修复。`context.py:403–438` 新增 `revalidate_recovery(recovery_id, session_id=...)`：先校验持久 request/result 的哈希及身份，再检查可信调用方 session、原请求 schema、原 requirement 的字段类型/版本/鲜度要求和引用限制；随后将原始当前 facts 与原 refs 交回 recover，从实时来源重新执行 hash/session/freshness 校验，并保存新的恢复证据。它没有把历史已恢复 facts 提升成当前事实，也没有用当前默认 requirement 替换原鲜度要求。

`get_recovery()` 保留历史读取语义，文档明确当前授权前应使用 revalidate_recovery。此前调用方只读历史的行为保持兼容。新增测试覆盖新进程下来源删除、事实篡改、来源 session 改变、过期、调用 session 不符、正常重验的新记录、请求/结果篡改及 requirement schema/version 校验。

Spec compliance：PASS（Task 2 范围）。

Code quality：PASS；修复范围内未发现新的事实问题。CLI/Hermes 将新复查接口接入冻结计划前的 gate 属于后续集成审查范围，本结论不声称该端到端路径已经通过。

证据：读取初审 `context/green.log`，41 passed；读取复审 `context/revalidate-green.log`，56 passed in 0.49s。审查采用静态代码与既有日志核对，未重复测试、未启动模型、未修改生产或测试代码。
