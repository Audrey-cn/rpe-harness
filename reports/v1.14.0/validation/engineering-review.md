# 1.14 工程审阅记录

本记录是最终冻结前的代码与离线工程审阅，不是真实供应商模型验收、真实业务收益、独立 CLI 语义审阅或最终安装包验收。审阅者实现了 request-budget 模块及其测试；对该模块的验证属于自检，对 root 与其他 agent 的接线、原生证据、共享 runner 和 CLI 改动进行了独立阅读。完整 pytest、pilot、最终真实模型入口、正式四对试验与独立 CLI 运行由 root 另行记录。

## 已检查范围

阅读了 `docs/superpowers/specs/2026-10-15-rpe-v114-design.md` 与对应 implementation plan，并检查当前生产 diff：

- `hermes_request_budget.py`、`hermes_session.py`、`hermes_bootstrap.py`、`hermes_review.py`、`proposal_cli.py`：显式预算文件、原生 provider timeout 配置、当前进程 builder 适配、公开 CLI 参数、native transient import 与 sandbox 最小可读文件。
- `hermes.py`、`hermes_native.py`、`hermes_native_process.py`、`hermes_native_finish.py`：真实事件持久化后的私有收据副本、完整当前工具尾部、API/tool/arguments 链、固定目标文件、签名业务 IO 收据和复用 Hermes 原生 post-tool end_turn。
- `review_native_evidence.py`：CSV/JSON 业务产物的签名收据及完整链检查；XLSX 沿用 1.13 的原 helper、解释器、来源、实际内核退出、当前产物与独立业务 oracle 检查。
- `review_v114_comparison.py`、`review_v111_comparison.py`、`review_v111_aggregate.py`：新 profile 的有限样本、显式预算、两臂接线、UTF-8 编码别名处理、旧 profile 条件隔离、导出预算和失败/重复/缺失诊断。
- `agent_audit.py`：legacy 合同保留；structured-v2 的独立 schema 副本、完整 ID 集合、短理由、请求的 CLI 限额、隔离 HOME/cache/tmp、真实进程回收与脱敏终态诊断。
- `pyproject.toml` 与包 `__init__.py` 的 1.14.0 版本；另检查 `reports/v1.14.0/validation/run-final-native-entry.py` 的实际调用接口。

当前未发现上述已审阅生产接线中的未解决阻断问题；辅助执行脚本发现的失败产物保全问题也已修复，见下文。此结论限于阅读与下面实际执行的测试，不替尚未运行的模型、CLI 或包证明背书。

## 实际执行的离线验证

所有下列测试的 basetemp/JUnit、宿主 HOME/runtime/cache/tmp、SQLite 和临时配置均在本轮 OWNED 下。测试不使用真实凭据、不修改已安装 Hermes 源码，也不调用真实模型。HTTPX 末端返回的 SSE、usage 与工具调用是明确标注的合成测试输入，不能计为真实 API、供应商 usage 或真实成本。

1. `tests/test_hermes_request_budget.py`：26 项通过，11.14 秒。JUnit：`OWNED/runtime-review/request-budget-bootstrap-green.xml`。
   - 初始 22 项在模块缺失时 RED；配置 FIFO 在普通阻塞 open 时单独 RED，随后改为非阻塞打开并拒绝特殊文件，GREEN。
   - 无配置不导入或修改宿主；拒绝未知字段、重复 JSON 字段、链接、错误路由/模型、未实际配置的 provider I/O 限额及运行中换 profile。
   - 保留消息、工具、tool_choice 和 parallel_tool_calls；移除冲突输出 cap。即便原生参数或长度恢复设置了 131072/262144 等更高 cap，最终 SDK JSON 仍只有 `max_completion_tokens=2400` 与 `thinking.type=disabled`。
   - 两臂环境分别经过已安装 Hermes Python 3.14 的真实 AIAgent、原生 streaming 与现有 OpenAI SDK。实际 HTTPX request.extensions 为 connect/pool=60、read/write=90 秒；最终 JSON 为 streaming=true、2400 输出上限、thinking disabled。
   - 实际 file-bootstrap/sandbox/完整 CLI 路径通过；入口不预加载 run_agent/OpenAI，PYTHONPATH 仅含 Hermes，适配后不把 Harness 路径遗留给原生子进程。fresh child 无法 import Harness。
2. `tests/test_native_finish_file_bootstrap.py`：2 项通过，15.58 秒。JUnit：`OWNED/runtime-review/native-finish-signed.xml`。
   - 使用实际 file-bootstrap、完整 native CLI、现有真实 streaming SDK、真实 terminal 工具、真实文件写入与持久 observer。仅模型响应由末端离线 SSE 替代。
   - draft.json 和 output.csv 两条路径均恰有一个合成模型请求，一个真实 pre/post tool 对，一个真实产物收据和 on_session_end。确定性终态为 `native_artifact_recorded` 且 `accepted=false`，不是业务通过。
   - CSV 使用实际 pipe FD 向 bootstrap 交付 32 字节签名密钥；bootstrap 消费后移除该环境项，真实 observer 签名并由生产 `evaluate_artifact_receipt` 鉴权通过。fresh terminal child 无 Harness import 路径或密钥 FD 环境项。
   - 草稿路径不产生 output.csv。固定文件内容和 SHA256 与手工测试输入一致；没有调用 RPE 业务控制器。
   - 初始化 terminal/file 工具时另观察到 `/v1/models` GET 与 `/api/show` POST 元数据探测。它们也被离线 transport 返回空 JSON，并在证明中单列为 ancillary requests；不冒充模型 API，也没有对外网络。
3. 执行过 `git diff --check`，当时无格式错误。没有在本分工内运行完整 pytest 或真实供应商调用。

## 审阅发现及处理

- 已修复：native draft finisher 原先在安全 snapshot 后使用 `path.read_bytes()` 普通重开，存在 FIFO/链接/文件增大竞态导致阻塞或无界读取的风险。root 改为 `O_NOFOLLOW|O_NONBLOCK`、regular-file/65536 字节限制、有限读取及收据 hash 核对。修复后继续通过真实 native file-bootstrap 草稿路径；本记录不将正常路径测试说成全部对抗竞态均已实测。
- 已修复：额外最终原生入口脚本原先没有向 `run_hermes_session` 传 request_budget/native_finish_enabled。root 已显式传入，两臂预算和结束机制不再只存在于 paired runner。
- 已修复：该脚本原先用 `ProposalStore._get()` 后读取不存在的 proposal_sha256，会在模型执行后 KeyError。root 改为带来源 session 检查的 `show()`，并将预算字段核对改为实际接口 `conformant`。修复已复读；没有运行该真实模型脚本。
- 已由 root 修复：该脚本原先以 `output.is_file()` 决定 `read_bytes()`/`shutil.copyfile()`，会在 gate 拒绝后继续跟随输出链接并无界读取或保留目标。root已改用既有 `read_regular_file(..., max_bytes=1MiB)`，只保留那份已安全读取的有限 bytes；特殊文件或链接留空 SHA 且不复制。此项属于辅助证明脚本的失败保全边界。

## 验证边界与剩余实测事项

- 1.13 cold/probe 的缺 API outcome 与工具后额外汇报是不同问题。旧结果仍是不完整链，不能因新 end_turn 重评分。
- 90 秒是原生 I/O/request-stale 配置，不是整个模型请求的绝对 wall-clock 限额。外层宿主 deadline 仍负责最终终止与子进程清理。
- `api_max_retries=1`、SDK `max_retries=0` 与 `auto_recovery_cycles=0` 不能证明一次物理尝试。离线失败探针实际观察到同一 API ID 的重复 pre/error；已安装 Hermes 在 retry 耗尽后仍可经 `turn_api_error.py:363-376` 触发一次 primary transport recovery。因此正式试验仍可能接近外层限时并缺 outcome，必须如实保留失败、重复与未知账单。
- 当前真实 wire 证明来自上述离线 SDK 路径。正式模型记录的 cap/thinking 来自持久 pre-request 观察；不能把配置值填成未经采集的每次物理请求 timeout。builder 适配发生于请求 middleware 之前，producer 已在 launcher 明确拒绝携带该预算，其他后续 middleware 仍必须由正式观察检查参数是否一致。
- end_turn 不读 oracle、不决定业务正确性。业务产物或草稿结束后仍需独立来源/语义、确认、业务 oracle 和 XLSX 实际退出 gate。单次候选/执行与不事后择优修稿的策略应保留在新 profile 的解释范围内。
- 旧 profile 的新行为有显式 `review-v114` 条件；编码别名等价只用于新 profile，不改原稿、执行参数或旧分数。legacy CLI schema/system 默认路径保留；structured-v2 真实 CLI 的可用性、实际轮数、内部恢复、估价与输出质量仍待 root 的预登记运行确认。
- 最终完整回归、wheel 安装、源码与最终模型入口 hash 一致、pilot/正式有限预算、额外 XLSX 原生入口及独立 CLI 结果，均不在本报告中预先判定通过。
