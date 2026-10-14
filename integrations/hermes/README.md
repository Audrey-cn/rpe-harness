# Hermes 1.4 接入

推荐使用仓库或已安装 wheel 的 `rpe-harness hermes run`。它按次加载原生插件，结束后清理临时宿主 home，不需要长期安装到日常 Hermes 配置。

原生插件模板在 `rpe-harness/`：`plugin.yaml`、`__init__.py`、`register(ctx)`。工具是 `rpe_task`，toolset 为 `rpe_harness`；观察器在原生对照臂也运行，只关闭这个工具。`pre/post_tool_call`、`pre/post_api_request`、LLM 与 session 事件同步保存元数据。工具执行前持久保存 receipt，执行使用独立兼容 Python，不要求 Hermes 自身环境安装 openpyxl。

隔离启动器的集成臂在可信父进程中创建按次认证的回环执行入口，随后才启动处于文件沙箱内的 Hermes。插件通过该入口提交业务 JSON；外部父侧核对固定工作区、真实 session/task/turn/API/tool call IDs、SQLite receipt、契约和新鲜输入 hash，再启动固定 Harness CLI。严格请求还需与稳定 event ID 和实际指定工具策略匹配，同轮已有持久终态不能再启动任务。客户端只连接 `127.0.0.1` 的固定 `/task` 路由，不使用代理或跟随重定向；入口限制 JSON 大小并用随机会话凭据认证。

Hermes 沙箱保持启用；候选在宿主沙箱外的 Harness 子进程中使用自己的单层沙箱，避免 macOS 的 nested `sandbox_apply` 限制。入口不接受任意命令或代码参数；模型仍不能改变注册能力、冻结计划或独立 oracle。固定 CLI 执行预算不超过 120 秒；会话退出、超时或取消时关闭入口，回收执行进程组与登记后代，清理结果并入 `process_cleanup`。启动器不安装常驻服务或定时任务；原生对照臂不创建这个执行入口。

1.3 将源文件和输出目录按无符号链接遍历打开，描述符传给固定 CLI，读取绑定源文件、暂存在可信数据库父目录，并以输出目录描述符独占发布。已覆盖验证后、准备阶段和最后发布检查附近的路径替换；不会沿替换后的符号链接向外生成产物。暂存/输出必须处于同一文件系统，系统不支持绑定时拒绝。信任边界仍包含父进程、SQLite/其父目录和候选 owned-root，不能描述为对任意同 UID 进程的完整文件隔离。发布后最后检查失败时，原绑定目录可保留已验证文件，运行保持失败，需人工核查而不能自动重试。1.2 的 [原始证据](../../reports/v1.2.0/evidence/review/external-executor/README.md) 是保留的历史缺口快照。

1.2 的隔离命令默认严格入口：关闭延迟搜索，直接暴露扁平业务参数 schema，使用官方 `llm_request` middleware 指定 `rpe_task`，得到匹配的持久结果后指定 `none`。官方 `tool_execution` middleware 保留真实 call/turn/API IDs。执行准入核对实际 API 策略；`none` 或不完整策略不能启动任务，同轮持久终态后不再受理新调用。原调用的有效重放只返回持久结果，新 turn 可以进入新任务。最终 gate 独立检查实际策略、调用链和文件；provider 不支持或忽略指定策略时明确返回 failed/unverified。`--request request.json` 可锚定用户参数和源 hash。

## 1.4 JSON 入口

`rpe_task` 使用明确能力参数；JSON 的扁平调用如下，路径相对于本次 workspace：

```json
{
  "capability": "json.normalize",
  "input_path": "source.json",
  "output_path": "result.json",
  "style": "pretty",
  "goal": "调整空白并保留字符串与数字原始词法"
}
```

`style` 必填，只能是 `pretty` 或 `minify`；JSON 不接受 key_columns、sheet、keep、encoding、delimiter。缺少 capability 仍按 tabular.deduplicate 处理并要求 key_columns。goal、输入和新输出路径对两种能力都必填。可选 JSON constraints 仅支持 preserve_original/preserve_order，强制保留行为不依赖这两个选项是否写出。版本化 JSON 请求只支持 schema_version=2、contract_version=1；固定请求可由 [示例](../../examples/json-normalize-v2.json) 修改得到。

JSON 只改字符串外空白，保留成员/数组顺序、字符串转义和数字 token。pretty 两空格缩进，空容器同行；两种样式都以恰好一个 LF 结尾。固定预算为输入 1 MiB、输出 16 MiB、容器深度 64、token 数 100000、数字 token 长度 256。严格 UTF-8 无 BOM，允许顶层标量；重复解码后键、NaN/Infinity、孤立代理码位、非法语法/UTF-8、尾随内容和超预算拒绝。输入/输出必须是 .json，输出不得存在；参数不能扩大预算。字节预算适用于 receipt probing、绑定描述符读取、最终验收、发布、恢复和反馈。

receipt、稳定 entry ID 和最终 gate 都保留 style，pretty 与 minify 是不同契约身份。未知字段或缺必填项返回可纠正的 invalid_task_arguments，并持久记录元数据；这类无 run 拒绝不会解除真实调用与结果验收要求。只有后来获得匹配的持久终态，严格入口才能允许总结。最终 `harness_verified=true` 仍要求重新验证真实产物；业务拒绝保持 verified=false。

调用隔离启动器可传 `--request /absolute/path/request.json` 锚定用户契约，也可只传任务提示。JSON 返回字节指标和 root_type，保留原计划的恢复、幂等和状态记录。新增 JSON 内置能力不会导入旧 JSON gene，不支持 JSON 候选自动激活；已有 CSV 候选使用其原独立套件。

手动加载插件时设置 `RPE_HERMES_DB`、`RPE_HERMES_WORKSPACE`、`RPE_HARNESS_PYTHON`、`RPE_HERMES_TOOL_ENABLED=1`，并使 `rpe_harness` 包可被宿主 Python 导入。插件启用配置为 `plugins.enabled: [rpe-harness]`。执行 Python 需安装 Harness 及其 openpyxl 依赖；默认手动路径直接启动该 Python 的 Harness 子进程，不管理回环入口，也不自动创建启动器的严格会话配置。

`rpe-harness hermes run` 自动管理包路径、严格配置和外部入口生命周期。`RPE_HERMES_BROKER_URL`/`RPE_HERMES_BROKER_TOKEN` 是可信启动器传给插件的临时配置；不要把它们当成需要长期手工安装的服务设置。手动插件若处于已有 macOS 沙箱内，直接路径无法再为候选施加新沙箱，应使用隔离启动器管理外部执行。日常插件配置无需为一次测试修改，测试历史留在指定临时 home。

候选 RSS 由外部可信执行父侧通过标准库 ctypes/libproc 读取，以 128 MiB 软阈值、20ms 轮询监督；不再启动 `/bin/ps`。读取失败关闭，只有进程已消失返回空值。该监督不保证瞬时分配峰值；候选的网络、文件、fork、CPU 和输出限制，以及每次生产独立验收仍各自生效。已有 CSV 契约的配方/代码可按证据自动激活；核心、宿主、新能力和 verifier 仍可形成提案，等待与变更匹配的可信验收集，不能由候选降低标准自行放行。

复现实验可在已安装项目的 Python 环境执行：

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from rpe_harness.experiments.hermes_repeated import run_repeated_comparison
from rpe_harness.integrations.hermes_session import run_hermes_session
from rpe_harness.integrations.hermes_gate import evaluate_session

def session(prompt, workspace, db_path, *, tool_enabled, owner_root):
    host = run_hermes_session(prompt, workspace, db_path,
        tool_enabled=tool_enabled, owner_root=owner_root,
        entry_mode='strict' if tool_enabled else 'optional')
    if tool_enabled:
        host['entry_validation'] = evaluate_session(host, db_path, workspace)
    return host

with TemporaryDirectory(prefix="rpe-hermes-validation-") as owned:
    report = run_repeated_comparison(Path(owned)/"pairs", Path("comparison.json").resolve(),
                                     session, repetitions=3)
print(report["summary"])
```

六个手工任务、两臂、三次重复，共36个独立新会话，使用同一输入字节和同一原始任务提示，顺序交替；这不是连续多轮对话记忆实验。原生臂仅使用 Hermes 文件/终端工具，自行编写处理代码，沙箱禁止读取 Harness 执行器和验收答案；集成臂使用专用工具及其强制入口策略。工具 schema/请求选择策略是产品差异，不宣称两臂 API 请求完全相同。外部验收重新读取实际产物，再比对手工真值，拒绝任务要求原文件不变、无输出且理由正确。报告保留所有失败和超时。

用量从真实 API 事件的字段存在性读取：缺失值为 null。原始 total 优先，否则仅在输入和输出都已报告时派生；reasoning 已包含在 output 中，不再相加。缓存字段独立报告。宿主自造的 `stream-*` response ID 只表示宿主响应身份。未记录费用金额，不能由 token 数直接推出订阅账单变化。

测试进程通过独立进程组、直接 spawn 登记、祖先跟踪和身份复查回收。登记范围是宿主直接创建的进程；不将它描述为任意恶意脱离进程的完整安全边界。正式测试使用本次专属工作区，并在清理后复查存活进程。Hermes 宿主文件沙箱只允许指定工作区、临时会话目录和数据库及其已知侧文件写入，不能清空共享缓存、全局 home 或日常网关；这不等于外部 CLI 获得了同等文件隔离。
