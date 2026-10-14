# RPE-Harness 1.7

本地能力路由与可验证任务执行器。1.0 支持 CSV/XLSX 按明确关键列去重，独立检查产物、保留原文件、禁止覆盖输出，并把事件、执行阶段、验证和结果持久保存到 SQLite。

1.2 增加明确能力契约、冻结执行计划、Hermes 强制工具入口，以及持久反馈、升级提案、自动候选验证和回滚。现有 CSV 契约的配方与 Python 执行器候选可自动激活；更广的核心代码与新能力升级保存为提案，等待相应可信验收集。持续学习收益与因果图收益仍需独立验证。

1.3 将阿卡夏的能力包、内容寻址、本地 Registry 获取与来源签名纳入同一产品。执行、独立验证、反馈、激活与回滚仍由 RPE 统一管理。旧 Protocol 不作为运行依赖，旧 Registry 继续作为资源仓；导入的旧基因默认只供发现。运行依赖仍仅 openpyxl，资产与来源层使用标准库。融合设计、迁移边界和命令见 [能力资产与融合](docs/fusion.md)，开发进度见 [todo](docs/superpowers/plans/2026-10-14-akasha-fusion.md)。

1.4 增加 `json.normalize/v1` 明确契约、双签名本地 Registry 发布包与离线发布前验收。JSON 和已有 CSV/XLSX 共用持久执行、独立验证、无覆盖发布、恢复与反馈；JSON 候选仍需专门可信验收集，不能借 CSV 证书自动激活。版本计划见 [1.4 todo](docs/superpowers/plans/2026-10-14-rpe-v14.md)，本地发布包命令见 [Registry 发布前验收](docs/registry-prepublish.md)。

1.5 建立 JSON 候选专用验收和独立版本槽，维护循环自动整理已验证 JSON 配方，并处理外部代码提案；生产失败隔离并恢复有效前版本或内置执行器。无人值守验证覆盖跨进程升级、崩溃、错误产物与回滚，自动操作仍受专用证书和固定预算约束。见 [JSON 升级说明](docs/json-upgrades.md)与[1.5 todo](docs/superpowers/plans/2026-10-14-rpe-v15.md)。

1.6 增加真实技能快照与原生 XLSX helper 适配、发现/加载/恢复/调用的独立验收、按来源恢复必要上下文，以及可超过100条历史运行的持久维护。升级生成器沿用既有 ABI，只产生待验收提案。使用与边界见 [真实技能和维护](docs/real-skills.md)，执行 todo 见 [1.6 计划](docs/superpowers/plans/2026-10-14-rpe-v16.md)。

1.7 增加授权的真实训练材料导出、前候选实际探测与语义反馈生成，以及预登记的三组学习对照。新代码仍须独立资格验收和激活重播，冻结后才在未见输入复用。生成、失败、探测、验收和生产开销均计入账本；闭环成立与质量、总成本改善分别判断。当前扩展已有 CSV 去重 ABI 的实现，尚不代表任意新能力可自动学习。见 [真实反馈与生成](docs/learning-loop.md)与[1.7 todo](docs/superpowers/plans/2026-10-14-rpe-v17.md)。

## 本机快速使用

仓库提供 `scripts/rpe`，自动寻找具备 openpyxl 的 Python >=3.10，包括本机 Codex bundled runtime。它不安装软件、不修改全局配置，也不生成 Python 缓存。

~~~sh
cd /Users/audrey/项目/rpe-harness
./scripts/rpe --version
./scripts/rpe benchmark --report /tmp/rpe-basic-validation.json
~~~

复制 `examples/deduplicate-v2.json` 或 [JSON 示例](examples/json-normalize-v2.json)，将输入、输出改为真实绝对路径；输出文件必须不存在。原 tabular V1 示例仍兼容，JSON 要求 V2。然后执行和查询：

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

wheel 自带 CSV 去重、XLSX 去重和 JSON 空白规范化三个内置执行器，不依赖源码目录外的 `data/`。需要使用本机技能索引时显式追加 `--data-dir /Users/audrey/项目/rpe-harness/data`；这些外部技能只供路由推荐，不能自动执行其 SKILL.md 或脚本。

## 文件语义

- 显式 key_columns、keep=first，保留首次出现记录及原顺序；不修剪字符串、不合并大小写、不把 CSV 前导零转为数字。空关键列、缺列、重复列名及不支持的选项会拒绝。
- CSV 为紧凑输出，默认 UTF-8 BOM、逗号分隔，可指定 encoding/delimiter。
- XLSX 保留工作表的原行坐标，清空重复记录的单元格内容，因此可能留下空行；保留记录、公式及受支持样式保持原位置。可指定 sheet；复杂对象和无法证明安全的公式明确拒绝。
- 独立验证器重新读输入与暂存产物；检查第一条记录、非关键列、顺序、公式及输入 hash。验证通过后以独占原子操作发布，既有文件和 broken symlink 均不得覆盖。
- openpyxl 不计算公式。验证证明的是公式结构与引用保留，不能证明 Excel 计算值。支持的具体公式/工作簿边界见执行器错误信息与 [设计](docs/superpowers/specs/2026-10-13-rpe-v1-design.md)。

## JSON 契约

请求显式声明 `capability="json.normalize"`、`schema_version=2`、`contract_version=1`。`execution` 只接受绝对 `input_path`、新 `output_path` 和必填 `style`；输入输出扩展名均须为 `.json`。路径各级禁止符号链接，请使用规范绝对路径，macOS 临时路径可使用 `/private/var/...`。

| 参数或边界 | 行为 |
|---|---|
| `style="pretty"` | 固定两空格缩进、LF；空对象/数组同行；恰好一个末尾 LF |
| `style="minify"` | 移除字符串外空白；恰好一个末尾 LF |
| 词法保留 | 对象成员/数组顺序、字符串转义和数字 token 原样保留；`1`、`1.0`、`1e0`、`-0` 不互换 |
| 固定预算 | 输入 ≤1 MiB、输出 ≤16 MiB、容器深度 ≤64、token 数 ≤100000、单个数字 token ≤256 字符 |
| 输入 | 严格 UTF-8、无 BOM；允许对象、数组、字符串、数字、布尔、null 顶层值 |
| 拒绝 | 重复解码后对象键、NaN/Infinity、非法 UTF-8/语法、孤立代理码位、尾随内容、超预算、覆盖输出 |

预算不能由请求扩大，token 包括结构标点。可选 `constraints` 仅支持 `preserve_original`、`preserve_order`；即使不写 constraints，词法和原输入保留仍是强制行为。独立 oracle 重新读取源与产物，检查非空白 token、精确样式、原 hash 和执行/验证修订；准入、发布、恢复和反馈读取也受对应字节预算限制。JSON 返回 `input_bytes`、`output_bytes`、`root_type` 等指标，不产生表格行计数。它只处理空白，不排序键、不转码字符串、不转换数值，也不提供路径提取或 schema 修复。完整设计见 [1.4 设计](docs/superpowers/specs/2026-10-14-rpe-v14-design.md)。

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

旁路协议与原生插件共享执行和幂等机制。幂等键为 `(source,event_id)`；同源同 ID 同内容返回原运行，同 ID 不同内容冲突。执行成功只有在产物验证、无覆盖发布、SQLite 提交都完成后成立；进程意外退出留下的运行在后续启动时按文件租约标 interrupted，不依赖 PID 是否已被复用。发布后、提交成功前崩溃也保留 interrupted，发布意图和 hash 留在 steps；不会自动补记成功。

`./scripts/rpe recover RUN_ID --db /absolute/path/runs.sqlite` 只恢复已停止、尚未进入发布阶段的失败尝试，原输入 hash 和执行修订必须一致，输出必须不存在。重复恢复返回同一个关联后继；默认总尝试预算为 3。发布后的未知结果需要显式检查产物和记录，不能自动重试。

## 真实 Hermes 会话

当前隔离启动器支持 macOS、本机 Hermes 0.21.7 的插件 API，以及现有 Xiaomi API key 配置。它读取当前选用的模型和计划端点，为每次调用创建独立 home，关闭记忆、外部插件、MCP、辅助模型请求和自动安装。只复制必要 API key，不复制 OAuth 状态，也不改日常配置或重启网关。

~~~sh
./scripts/rpe hermes run --workspace /absolute/path/workspace \
  --prompt-file /absolute/path/task.txt --request /absolute/path/request.json \
  --db /absolute/path/runs.sqlite
~~~

默认严格入口直接暴露 `rpe_task`，通过官方 middleware 发送指定工具选择，获得匹配的持久结果后才允许总结。执行准入还会阻止 `none` 或不完整策略的调用，以及同轮终态后的新任务；有效原调用只读重放，新 turn 可重新进入。工具使用输入/输出路径和目标等业务参数，内部生成 V2 请求。JSON 显式传 `capability="json.normalize"` 和 `style`；不传 capability 按原 tabular 契约处理，必须传 key_columns。路径必须位于指定工作区，禁止符号链接。`--request` 是可选的固定契约锚，能检测模型改变关键列、JSON style、路径和选项；不提供时仍验证产物符合模型抽取的契约，但自然语言意图抽取需业务验收。`--entry-mode optional` 保留可选工具模式用于对照，不会自动降级严格策略。

集成模式的启动器父进程按次创建带随机认证凭据的回环执行入口，在 Hermes 沙箱之外启动固定 Harness CLI。Hermes 保留原文件沙箱；CSV 候选由外部 Harness 子进程施加自己的单层沙箱，避免 macOS 拒绝重复 `sandbox_apply`。入口核对固定工作区、真实调用 ID、持久 receipt、契约和当前输入 hash；它不接受任意 shell 或 Python 程序。执行有超时，退出时关闭入口并回收执行进程组和登记的后代，不安装常驻服务或调度任务。严格准入与最终 artifact gate 仍独立检查真实模型策略、调用链和产物。

1.3 的外部入口按无符号链接目录遍历打开源文件和输出目录，把描述符传给固定 CLI；读取原文件使用源描述符，暂存使用可信数据库父目录，发布以输出目录描述符执行独占硬链接。路径被替换时会拒绝；即使替换发生在最后一次路径检查之后，也不会沿新符号链接向外写出。数据库、其父目录、候选 owned-root 和父进程仍是信任边界；这是目录对象绑定，不是对任意同 UID 进程的完整 OS 隔离。暂存与输出必须位于同一文件系统，缺少所需系统接口时拒绝。硬链接已成功而最后检查失败时，原绑定目录可能保留已验证产物，运行保持失败/未知发布结果，不能自动重试或补记成功。1.2 的 [原缺口证据](reports/v1.2.0/evidence/review/external-executor/README.md) 保留为历史快照。

会话临时聊天历史、凭据副本和缓存在命令返回后清理；产物和指定数据库保留。`completed` 表示宿主完成，`harness_verified` 才表示最终 Harness 运行已验证。未调用 Harness 的完成会话返回 unverified，退出码 1。拒绝返回 2。真实宿主事件保存在 `hermes_host_events`；原生 tool_call_id/api_request_id/turn_id 与 Harness run_id 分开保存，缺失原生 event_id 保留 null。事件记录失败会产生独立失败标记并阻止验收通过。

普通 XLSX 注释保留文本和作者，重复行注释清空；不保证注释框尺寸或绘图外观。控件、图片和其他复杂绘图仍拒绝。

手动加载插件默认使用配置 Python 的直接子进程，不会创建上述入口；已有外层 macOS 沙箱时，候选可能无法再次施加沙箱，优先使用隔离启动器。配置和模式区别见 [Hermes 接入说明](integrations/hermes/README.md)。历史真实验收见 [1.1 报告](reports/v1.1.0/1.1开发与验收报告.md)。

`/usage` 是外部未验证报告，和内部 verified_success_count 分开。`/skills/state` 可手动启用/禁用或休眠，重启与 reload 均保留。

## 无人值守升级周期

```sh
./scripts/rpe evolution cycle --owned-root /absolute/path/harness-state \
  --db /absolute/path/harness-state/runs.sqlite
./scripts/rpe evolution list --db /absolute/path/harness-state/runs.sqlite
./scripts/rpe evolution experiences --db /absolute/path/harness-state/runs.sqlite
./scripts/rpe evolution show PROPOSAL_ID --db /absolute/path/harness-state/runs.sqlite
```

数据库必须位于声明的 owned-root 内，候选、验证临时目录和版本证据也保存在这里。周期重新检查持久运行和实际产物，整理紧凑经验，记录带证据的升级提案，并可自动验证/激活已有契约的配方。`--no-auto-apply` 只生成和验证；程序本身不创建定时任务。可由现有任务调度器定期调用。

代码候选通过 `evolution propose --candidate candidate.py --component executor --kind csv_executor --evidence-run-id RUN_ID --owned-root ... --db ...` 提交；验证和激活必须有非空、仍可复查的持久已验证运行证据，并通过独立的 CSV 验收集。1.2 的实际代码生成演示绑定了两次 CSV 运行，这不是 API 强制的次数门槛。随后 `evolution validate PROPOSAL_ID`、`evolution apply PROPOSAL_ID`、`evolution rollback VERSION_ID` 均接受 `--db`。代码 ABI 为 `prepare(plan: dict, staging_path: str) -> None`，只产生暂存文件，不能修改源文件或直接发布。

候选生成器与验收器分离；默认周期无需模型，也支持注入本机 Agent 的提案。代码候选在 macOS 子进程沙箱内验证，限制网络、文件写入、派生进程、CPU 时间和输出体积；生产任务仍通过原计划的独立验证。内存使用标准库 ctypes 调用 Darwin libproc 读取 RSS，以 128 MiB 软阈值、20ms 轮询监督；不可观测结果失败关闭，仅目标进程已消失时返回空值。它不能保证瞬时分配峰值，不能描述为任意恶意代码的完整资源隔离。候选失效会隔离并回退内置执行器，原版本快照可回滚。休眠、隔离经验保留索引和来源。

JSON 的已验证文件结果进入专属配方和经验，外部 `json_executor` 提案经过专用41例验收可自动激活；CSV证据及证书不能混用。候选失效时当前任务按同plan内置回退，后续任务恢复有效前版本或内置。维护硬预算为32提案/100历史run，更多使用方式见 [JSON升级](docs/json-upgrades.md)。Registry可执行发布包ABI仍仅支持已验证CSV执行器/配方。跨输入、进程重启、隔离、回滚及休眠查找只证明机制可观察地工作，不代表长期学习或token节省。

## 评测和开发路线

`benchmark` 使用固定人工真值与明确命名的简单元数据基线，只测路由。文件闭环、重启、服务和 wheel 由测试与发布报告验证；不能据此推出真实 Hermes token 节省或长期学习收益。

旧 `experiments.baseline` 只是读取 Hermes 目录后调用 Harness Router 的诊断；`cold_skill_test` 和 `stress_test` 为旧合成实验/微基准，不代表独立真实任务效果。

1.1：真实 Hermes 事件和工具接入、原生基线、可复现端到端验证。
1.2：明确契约、可靠工具调用、真实故障恢复、重复原生对照、证据驱动提案与已有契约执行器演进。
后续：扩展新能力和其他宿主、独立可信升级验收集、持续学习收益对照，再研究因果图与更广的程序化沉淀。总体设计见 [架构与演进](docs/architecture.md)。

审核证据见 [完整审核](reports/audit-2026-10-13/项目完整审核报告.md)。1.0 设计和任务计划位于 `docs/superpowers/`，正式验证和清理记录位于 `reports/v1.0.0/`。
