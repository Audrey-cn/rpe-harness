# Darwin RSS 观察器修复的范围

本证据只证明 RSS 观察器修复，不是整个真实 Hermes 候选执行链通过的结论。

在实际 macOS 外层 `(version 1)(allow default)` 沙箱中，原 `/bin/ps` 子进程读取 RSS 以 `PermissionError: [Errno 1] Operation not permitted` 失败。初次测试 5 failed、1 passed；修复后的 RSS 专项 6 passed。随后 RSS、直接 candidate/evolution 共 **40 passed、无 skip，13.55s**。安全摘要见 [RED/GREEN](red-green-summary.json) 与 [focused 结果](focused-result.json)。根任务统一运行全套，本子任务未重复完整 suite，也未调用模型/API。

修复使用标准库 ctypes 调用 Darwin libproc 的 `proc_pidinfo(PROC_PIDTASKINFO)`，读取 `pti_resident_size`，不再创建 setuid ps 进程。ABI 来自本机 Apple SDK `sys/proc_info.h:124–143,721–722` 与 `libproc.h:96`：6 个 uint64_t 加 12 个 int32_t、结构大小 96 字节、resident 字段偏移 8 字节且单位为 bytes。见 [SDK ABI 记录](sdk-abi.json)。

只有完整结构返回才接受 RSS；零字节且 errno=ESRCH 返回 None，表示目标已消失；权限错误、无 errno 的空结果和部分结构均抛 OSError，不能当作低 RSS 放行。真实外层沙箱/进程退出使用实际 OS；难以稳定制造的部分响应/权限错误在 libproc 边界注入。128 MiB RSS 软上限、20ms 轮询、超时及候选权限均未调整。RSS 仍是软监督，不能保证瞬时分配峰值。

- [审查的 sandbox.py 原样副本](sandbox-reviewed.py)
- [RSS 测试原样副本](test_candidate_rss.py)
- [SHA256 与范围清单](retention-manifest.json)

Hermes worker 后续实际测试另复现 macOS **nested sandbox_apply** 拒绝：最小外层 allow-default → 内层 allow-default → true 也 exit 71。其 [原始安全证据](candidate-nested-os-limit.json) 与 [保留清单](nested-retention-manifest.json) 原样保留，记载两个目录布局均隔离候选并回退 builtin，原 session 范围为 41 passed、2 failed。这是 RSS 修复之后、外部执行边界实现之前的历史观察。

根任务已经授权将执行移到宿主沙箱外的认证临时执行器，后续实现与验收另行记录。RSS 的 6/40 GREEN 不证明候选已穿过该宿主边界；也不能把旧 nested 失败解释成后续实现永远不支持候选。真实 candidate 两布局和 session 全文件的最终结果由对应 worker/root 另外记录。本目录不作为整链路 GREEN 或最终发布验收。
