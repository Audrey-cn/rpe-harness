# Hermes 1.1 接入

推荐使用仓库或已安装 wheel 的 `rpe-harness hermes run`。它按次加载原生插件，结束后清理临时宿主 home，不需要长期安装到日常 Hermes 配置。

原生插件模板在 `rpe-harness/`：`plugin.yaml`、`__init__.py`、`register(ctx)`。工具是 `rpe_task`，toolset 为 `rpe_harness`；观察器在原生对照臂也运行，只关闭这个工具。`pre/post_tool_call`、`pre/post_api_request`、LLM 与 session 事件同步保存元数据。工具执行前持久保存 receipt，执行使用独立兼容 Python，不要求 Hermes 自身环境安装 openpyxl。

直接使用插件时必须明确设置 `RPE_HERMES_DB`、`RPE_HERMES_WORKSPACE`、`RPE_HARNESS_PYTHON`、`RPE_HERMES_TOOL_ENABLED=1`，并使 `rpe_harness` 包可被宿主 Python 导入。插件启用配置为 `plugins.enabled: [rpe-harness]`。隔离启动器自动配置包路径；本次交付未修改日常插件配置。优先使用隔离启动器，避免把测试历史写入日常 home。

复现实验可在已安装项目的 Python 环境执行：

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from rpe_harness.experiments.hermes_native import run_comparison
from rpe_harness.integrations.hermes_session import run_hermes_session

with TemporaryDirectory(prefix="rpe-hermes-validation-") as owned:
    report = run_comparison(Path(owned), Path("comparison.json").resolve(), run_hermes_session)
print(report["summary"])
```

六个手工任务、两个新会话分别使用同一输入字节和同一任务提示；顺序交替。原生臂仅使用 Hermes 文件/终端工具，自行编写处理代码，沙箱禁止读取 Harness 执行器和验收答案；集成臂增加专用工具。外部验收重新读取实际产物，再比对手工真值，拒绝任务要求原文件不变、无输出且理由正确。报告逐次保留成败。

用量从真实 API 事件的字段存在性读取：缺失值为 null。原始 total 优先，否则仅在输入和输出都已报告时派生；reasoning 已包含在 output 中，不再相加。缓存字段独立报告。宿主自造的 `stream-*` response ID 只表示宿主响应身份。未记录费用金额，不能由 token 数直接推出订阅账单变化。

测试进程通过独立进程组、直接 spawn 登记、祖先跟踪和身份复查回收。登记范围是宿主直接创建的进程；不将它描述为任意恶意脱离进程的完整安全边界。正式测试使用本次专属工作区，并在清理后复查存活进程。文件沙箱只允许指定工作区、临时会话目录和数据库及其已知侧文件写入，不能清空共享缓存、全局 home 或日常网关。
