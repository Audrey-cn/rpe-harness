# JSON 候选自动升级与无人值守验证（1.5）

JSON 与 CSV/XLSX 共用一套执行、持久证据、独立验收和激活指针。1.5 增加 JSON 专属的代码候选与声明配方；候选不能更改业务契约或验收标准。

## 两种升级

`executor:json_executor` 保存标准库 Python 候选，入口仍为 `prepare(plan, staging_path)`，示例见 [JSON 候选](../examples/json-candidate/candidate.py)。候选读的是沙箱内冻结的输入副本，只能写自己的暂存产物；真实输出由可信父侧独立验收后发布。

`recipe:json_recipe` 保存已验证任务的 style、constraints 和固定执行步骤。默认维护循环按实际反馈整理配方与可检索经验，通过专用验收才自动激活。配方槽保持当前有效版本，其他 style 的经验和已验证提案保留；生产参数不匹配时使用内置执行器。代码候选优先，不通过切换用户 style 适配配方。

Python 代码可由外部 Agent 提案。提交来源、独立验收与实际使用分别记录；示例代码通过不代表系统已自主创造高质量算法。

## 提案、验证与回滚

下面替换为明确的规范绝对路径和已验证 JSON RUN_ID，使用独立 owned 状态库；macOS 临时路径使用 `/private/var/...`。

```sh
rpe-harness evolution propose --component executor --kind json_executor \
  --candidate /absolute/path/candidate.py --evidence-run-id RUN_ID \
  --owned-root /absolute/path/owned-state --db /absolute/path/owned-state/runs.sqlite

rpe-harness evolution cycle --owned-root /absolute/path/owned-state \
  --db /absolute/path/owned-state/runs.sqlite

rpe-harness evolution show PROPOSAL_ID --db /absolute/path/owned-state/runs.sqlite
rpe-harness evolution rollback VERSION_ID --db /absolute/path/owned-state/runs.sqlite
```

cycle 消费本地待处理 JSON 代码提案，保持原指纹及证据，通过专用验收、重新验收与 CAS 后才激活。也可使用既有 `validate`、`apply` 手工阶段。`cycle --no-auto-apply` 仅生成与验证。核心、宿主、未知能力及 verifier 更改继续形成待独立验收提案。

证据必须来自 JSON 契约的已验证运行；CSV 证据和 CSV 证书不能用于 JSON。证书绑定 kind、代码、输入/产物证据、专用 fixtures 与当前契约、writer、oracle、沙箱修订。代码改变、证据损坏、修订过期或比较基准改变都会拒绝。

每次生产仍重读并检查实际产物。错误候选被隔离，当前任务按同一冻结计划回到内置执行器；后续任务恢复到证书、代码和新鲜证据仍有效的上一版本，否则回到内置。不会在同次失败中递归尝试旧代码，也不覆盖已经开始发布的未知结果。

## 专用验收与预算

JSON 验收器使用独立手写字节真值，不调用候选或内置 writer 生成预期答案。非法输入直接测试候选自身拒绝；超时、资源超限、缺沙箱或未知异常不能当成正确解析拒绝。合法输入检查两种 style、六类顶层值、原始数字及字符串 token、顺序和预算边界。

候选代码≤64KiB、JSON输入≤1MiB、输出≤16MiB、每例≤3秒；网络和 fork 禁止。128MiB RSS 为轮询软阈值，不能保证瞬时分配峰值。代码候选需要 macOS 沙箱和资源观测；缺能力失败关闭。深度64、token100000、数字token256等原契约上限保持，不能由候选或请求提高。

维护循环为有界 batch：最多100个历史 run、32个当轮提案（包括待处理 JSON 代码）。超过历史限额在维护变更前拒绝，不静默删除旧证据。Python API 可将 `max_history_runs`、`max_proposals` 调低，不能扩大硬界限。当前版本不声称永久无界维护服务。

## 无人值守复现

安装后的 Python 可运行 [validate_v15.py](../scripts/validate_v15.py)，在新的 owned-root 内验证自动配方、外部代码提案自动验收激活、跨进程复用、激活崩溃恢复、错误产物隔离、前版本恢复及内置回退、回滚和经验查找；同时做 CSV 回归。

```sh
INSTALLED_PYTHON -B scripts/validate_v15.py --owned-root NEW_TEST_DIRECTORY \
  --candidate examples/json-candidate/candidate.py --report TEST_REPORT.json
```

driver 每步为独立进程，最多32步，每步超时最多60秒，持续保留 progress 和失败。它没有安装 daemon、定时任务或修改默认数据库。报告区分默认配方、外部注入代码与外部已持久提案；预设故障序列证明恢复机制，不是长期学习收益或公平原生比较实验。

验收脚本会保留独立测试状态供随后真实宿主使用；运行结束后按本轮 owned-root 清理，报告可单独保留。不要清空日常 Hermes 历史或共享缓存。JSON 代码/配方本轮只在本地生命周期中验证，签名 Registry 发布 ABI 继续保留既有 CSV 范围；JSON 导出仍明确拒绝。
