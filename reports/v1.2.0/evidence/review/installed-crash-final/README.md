# 安装后崩溃复跑：b9ab 历史包，待最终包另行复验

此处保留较新安装候选的 3/3 owner-crash 复跑证据及原样复现脚本。实用 wheel SHA256 为 `b9ab6a7e5ec85d59c544f813239e90fabb2178e2c1c1594f083b9f10f53517a7`，由 `installed-contract-closed/report.json` 的安装身份与 `wheel-closed` 的实际字节 hash 交叉确认。

**范围为 pre-runtime-sandbox-fix package，不是最终版本验收。** 后续真实补测发现 launcher owner-read scope 挡住父 owned root 内的 release venv，`hermes_session.py` 的修复仍会改变最终 wheel。根任务将再验最终包的三个 crash 阶段，并保存新的证据；本目录带 b9ab 文件名的历史记录不得覆盖成最终包结果。

- [b9ab 三阶段证据](installed-crash-evidence-b9ab-pre-runtime-sandbox-fix.json)
- [对应复现脚本原件](reproduce_installed_crash-b9ab-pre-runtime-sandbox-fix.py)
- [该快照的保留清单](retention-manifest-b9ab-pre-runtime-sandbox-fix.json)

脚本只作为审核源保留，整理过程未运行它。复现需把脚本复制到仓库外 owned root 的 `review/installed-crash/`，在同一 owned root 的 `release-venv/` 安装要测的确切 wheel，再用该 Python `-I -B` 从复制脚本目录运行；不能从本证据目录直接执行。绝对路径为历史归属信息，清理后可能失效。未复制 crash 夹具目录、SQLite、聊天记录或缓存。
