# 最终安装包补充证据的保留范围

本目录保留最终安装包的本地六类契约、真实 Hermes 固定契约六场、同一 Agent 两轮续接、真实已验收宿主反馈绑定的离线升级周期，以及三个真实 owner-crash 阶段。业务文件是人工合成夹具；报告中的 accepted/passed/cleanup 字段原样保留，复制不把失败改成通过。运行/模型调用是否通过应按各原报告读取，文件保留本身不是发布无阻断结论。

最终 wheel SHA256：`73632f86e221f19b58021c74f1b32e470f9292d9ec24d78d9a7774e6693bc3b7`。32 份生产 Python 源逐字节匹配 wheel、工作树和安装目录；[身份记录](source-identity.json) 保留精确 hash。此前正式36场使用 bee526；b9ab 与 d495 为后续历史快照，977为参数反馈修复前的中间快照，原六场4/6与同源另两场诊断2/2均独立保留。各旧快照不能当作当前包的最终验证。原历史报告、失败启动记录和36场业务数据未覆盖。

[原临时路径到保留文件的索引](fixture-index.json) 相对于 reports/v1.2.0；[业务数据目录](../../artifacts/release-extras/) 不保留 SQLite、Hermes home、raw stdout/stderr、聊天历史、缓存、skills、配置或凭据。RunStore 记录通过 mode=ro/query_only 的 get 读取，跳过建库构造器；数据库和合成业务请求的原路径不改写。临时路径清理后可能失效；改路径重做任务会产生新的计划 hash，不能冒充原执行。

复现辅助源在 helpers/，保持原始字节。先在新的仓库外 owned root/release-venv 安装指定 wheel，再复制 helpers 到 owned root，entry_reset_parent.py 与 entry_reset_sdk.py 放 owned root/probes，crash 脚本放 owned root/review/installed-crash-release。前三个 check 脚本有原 owned root 字面路径，必须在复制件中替换为新 owned root；保留原件勿修改。使用 release-venv/bin/python -I -B。entry_reset_parent.py 显式提供 --owner-root 和 --hermes-root；其 SDK 子脚本只能由父脚本启动。各 check 依次使用新的 installed-contract-release，生成器候选放 codex-candidate-2/workspace/candidate.py 且其 hash 需匹配身份记录。crash 从脚本目录运行。本目录不提供自动模型重跑；真实 Hermes check/SDK 复现会使用操作者自己的已授权本机宿主配置。

保留脚本只在全部探针结束后由 Root 执行，要求 --ready-after-root-probes --wheel-sha256 HASH；它不发起模型/API，不重跑探针，不删除 owned 文件或数据库，不修改生产源/主报告/文档。每份保留文件大小和 SHA256 见 retention-manifest.json；manifest 不自包含自己的 hash。
