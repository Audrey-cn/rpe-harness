# 真实技能和持续维护（1.6）

RPE 是独立 Harness，Hermes 是首个宿主。目录负责发现，冻结契约、独立 oracle 和真实事件负责验收。技能正文、排名或模型的成功描述不能授予执行权限。

## 技能闭环

`skills sync` 将显式来源的原字节复制到不可变快照；查询包含冷技能，但符号链接、特殊文件、变化的来源或没有适配的技能不能执行。首个可信适配是本机 `productivity/xlsx` 的原始 `xlsx_read.py`，支持 `xlsx.inspect/v1` 的 rows、sheets、formulas。其他目录项只供发现。

```sh
scripts/rpe skills sync --db "$PWD/run-state.sqlite" --owned-root "$PWD/owned" --skill-root /Users/audrey/.hermes/skills
scripts/rpe skills trust productivity/xlsx --db "$PWD/run-state.sqlite" --owned-root "$PWD/owned"
scripts/rpe skills query --query workbook --capability xlsx.inspect --db "$PWD/run-state.sqlite" --owned-root "$PWD/owned"
scripts/rpe hermes run --entry-mode skills --catalog-root "$PWD/owned" --db "$PWD/run-state.sqlite" --workspace "$PWD/workspace" --prompt-file "$PWD/task.txt"
```

工作区须已存在。上述数据库和 owned 目录应放在专用工作目录；使用 `scripts/rpe hermes run --help` 查看参数。首次信任仅接受固定技能和 helper 的已审核 hash，修改后的脚本需新适配及独立验收。

宿主阶段是 `rpe_discover → skill_view → rpe_context → rpe_task → none`。官方请求与工具 middleware 强制每阶段只暴露对应工具；终态保留 rpe_task schema 并指定 tool_choice=none，执行入口拒绝继续调用。父进程认证入口核对实际身份、请求策略、持久 receipt、来源 hash 和上下文。最终 gate 重读所有证据，并以独立 oracle 检查文件。实际调用原生 helper 的模式是 xlsx.inspect；加载 xlsx 技能后运行去重仍是 RPE 的去重契约。快照及其可写祖先受沙箱写保护，不能通过修改、替换或移动父目录改变被执行的 helper。

输入 ≤16MiB，ZIP 展开总量 ≤64MiB、成员 ≤2048。流式 XML 预检限制节点、深度、坐标和合并区域，每表 ≤100000 单元格、工作簿 ≤200000；拒绝 DTD、实体和外部关系。helper 固定 argv、5 秒预算、禁止网络/派生进程/额外写入，RSS 监督是软上限。独立 oracle 核对类型、顺序、日期和公式字符串；cached 值是源文件中的缓存，未计算公式。

## 上下文恢复

`context capture/recover/show/revalidate` 使用显式数据库。来源引用绑定 source、event_id、完整 envelope hash 和 session_id；能力只恢复必需字段。正文不是事实来源，缺字段返回 needs_context，冲突返回 conflict，跨会话或内容变化拒绝。`show` 是历史记录；执行前必须 `revalidate`，重新检查来源和时效。

Hermes 启动器可接受 `--context-sources` JSON 文件，其中 `sources` 数组成员为 `{source,event_id,payload,facts}`。可信调用方负责将原始明确事实写入 facts；最多八个来源。父进程按真实会话绑定来源，模型可以补充明确参数，但不能猜测冲突字段。

## 持续维护与升级

`evolution cycle --db ... --owned-root ...` 每批扫描至多100条运行、处理至多32个提案，游标、waiting/pending 队列和语义诊断证据账本持久保存。历史总量超过100不再阻断维护；旧活动证据仍被独立重新检查。`maintenance.has_more` 表示还有工作，`busy` 表示有其他租约持有者。可以由既有调度器调用，本版本不安装长期服务。

`evolution worker --db ... --owned-root ... --kind json_executor --timeout 120` 显式启动一次真实 Hermes 候选生成。它只获得至多八条 fresh 反馈摘要，无法读取真实反馈数据库、核心源码和保留验收材料；输出沿用既有 CSV/JSON ABI，仅保存提案。随后 cycle 独立验证和决定激活，失败保留，worker 无权审批。当前只有已建立专用验收的 CSV/JSON 槽可自动升级和回滚；其他能力需新增可信验收机制。

## 证据范围

目录条数不等于已执行技能数，离线回归不等于真实宿主验收。原生对照按相同模型、输入、原始 prompt 和技能快照记录全部失败、请求数、耗时及完整 token 观测；缺失字段保持 unknown。token 观测不是计费账单，也不能证明长期学习或因果图收益。最终结果以 [1.6 报告](../reports/v1.6.0/1.6开发与验收报告.md) 为准。
