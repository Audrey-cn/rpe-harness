# RPE-Harness 1.0

本地能力路由与可验证任务执行器。1.0 支持 CSV/XLSX 按明确关键列去重，独立检查产物、保留原文件、禁止覆盖输出，并把事件、执行阶段、验证和结果持久保存到 SQLite。

研究原型已具备 MCSI、能力索引、图与 HTTP 接口。1.0 将基础闭环做实；持续学习收益、自动生成技能及真实 Hermes 插件接入仍属于后续工作。

## 本机快速使用

仓库提供 `scripts/rpe`，自动寻找具备 openpyxl 的 Python >=3.10，包括本机 Codex bundled runtime。它不安装软件、不修改全局配置，也不生成 Python 缓存。

~~~sh
cd /Users/audrey/项目/rpe-harness
./scripts/rpe --version
./scripts/rpe benchmark --report /tmp/rpe-basic-validation.json
~~~

复制 `examples/deduplicate.json`，将输入、输出改为真实绝对路径；输出文件必须不存在。然后执行和查询：

~~~sh
./scripts/rpe run --request request.json --db /absolute/path/runs.sqlite
./scripts/rpe runs list --db /absolute/path/runs.sqlite
./scripts/rpe runs show RUN_ID --db /absolute/path/runs.sqlite
~~~

标准输出是 JSON。执行成功退出码 0；拒绝或非法请求 2；执行失败或中断 1；仍在运行的幂等重放 3。runs show/list 查询成功返回 0，记录自身 status 可为失败或拒绝。运行结果包含 run_id、status、stage、原始事件、状态、验证、产物和错误。默认数据库为 `~/.local/share/rpe-harness/runs.sqlite`，可通过 `--db` 或 `RPE_STATE_DIR` 指定。测试请显式使用临时数据库。

## 常规安装

~~~sh
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/rpe-harness --version
~~~

运行依赖仅 openpyxl 3.1.x；核心路由、SQLite、CLI、HTTP 使用标准库。1.0 面向 macOS/Linux，进程租约使用 POSIX 文件锁。开发测试：

~~~sh
.venv/bin/python -m pip install '.[dev]'
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider
~~~

wheel 自带两个内置执行能力，不依赖源码目录外的 `data/`。需要使用本机技能索引时显式追加 `--data-dir /Users/audrey/项目/rpe-harness/data`；这些外部技能只供路由推荐，不能自动执行其 SKILL.md 或脚本。

## 文件语义

- 显式 key_columns、keep=first，保留首次出现记录及原顺序；不修剪字符串、不合并大小写、不把 CSV 前导零转为数字。空关键列、缺列、重复列名及不支持的选项会拒绝。
- CSV 为紧凑输出，默认 UTF-8 BOM、逗号分隔，可指定 encoding/delimiter。
- XLSX 保留工作表的原行坐标，清空重复记录的单元格内容，因此可能留下空行；保留记录、公式及受支持样式保持原位置。可指定 sheet；复杂对象和无法证明安全的公式明确拒绝。
- 独立验证器重新读输入与暂存产物；检查第一条记录、非关键列、顺序、公式及输入 hash。验证通过后以独占原子操作发布，既有文件和 broken symlink 均不得覆盖。
- openpyxl 不计算公式。验证证明的是公式结构与引用保留，不能证明 Excel 计算值。支持的具体公式/工作簿边界见执行器错误信息与 [设计](docs/superpowers/specs/2026-10-13-rpe-v1-design.md)。

## 服务与 Agent 旁路

~~~sh
./scripts/rpe serve --host 127.0.0.1 --port 18400 --db /absolute/path/runs.sqlite
# 原模块入口仍可用：python -m rpe_harness.server --port 18400 --db ...
~~~

GET：`/health`、`/stats`、`/skills`、`/skills/ID`、`/runs`、`/runs/RUN_ID`。
POST：`/route`、`/run`、`/events`、`/usage`、`/skills/state`、`/reload`、`/causal/discover`、`/causal/chain`。

请求体只接受 JSON 对象，最多 1 MiB；POST 要求 application/json，拒绝外部 Origin；请求接收有总时限、并发数有上限。默认回环监听。`/route` 只给推荐，不执行任务；UNKNOWN、缺事实、不兼容或低匹配会返回升级信号，未内置自动 LLM 升级器。`constraints` 是用户要求，`facts` 是已确认条件；执行路径重新探测文件事实。

Hermes 或其他 Agent 可以发送同一 JSON 协议：

~~~sh
./scripts/rpe events submit --event event.json --db /absolute/path/runs.sqlite
# 支持 --event - 从 stdin 读取
~~~

这是已验证的旁路协议，不表示已经安装实时 Hermes 宿主插件。幂等键为 `(source,event_id)`；同源同 ID 同内容返回原运行，同 ID 不同内容冲突。执行成功只有在产物验证、无覆盖发布、SQLite 提交都完成后成立；进程意外退出留下的运行在后续启动时按文件租约标 interrupted，不依赖 PID 是否已被复用。发布后、提交成功前崩溃也保留 interrupted，发布意图和 hash 留在 steps；1.0 不自动补记成功。如需重试应使用新 event_id 和新输出路径。

`/usage` 是外部未验证报告，和内部 verified_success_count 分开。`/skills/state` 可手动启用/禁用或休眠，重启与 reload 均保留；1.0 不自动学习或生成技能。

## 评测和开发路线

`benchmark` 使用固定人工真值与明确命名的简单元数据基线，只测路由。文件闭环、重启、服务和 wheel 由测试与发布报告验证；不能据此推出真实 Hermes token 节省或长期学习收益。

旧 `experiments.baseline` 只是读取 Hermes 目录后调用 Harness Router 的诊断；`cold_skill_test` 和 `stress_test` 为旧合成实验/微基准，不代表独立真实任务效果。

1.1：真实 Hermes 事件接入与原生基线、更多明确执行器、生命周期反馈。
1.2：独立冷门场景、多会话持续收益、真实 token/成本记录。
2.0：Consolidator 与因果图学习，在证据、回滚和人工控制机制建立后推进。

审核证据见 [完整审核](reports/audit-2026-10-13/项目完整审核报告.md)。1.0 设计和任务计划位于 `docs/superpowers/`，正式验证和清理记录位于 `reports/v1.0.0/`。
