# 1.9 干净来源与确定性执行

clean 是新增的严格入口；原有 address/skills/strict 可继续使用。三个能力仍为表格保留首条去重、JSON空白规范化、已审核原生XLSX只读检查。它不负责训练新分类器或替代Hermes技能/记忆生命周期。

当前可自动执行的完整请求必须是一份JSON对象，例如：

```json
{
  "capability": "tabular.deduplicate",
  "constraints": ["preserve_original", "preserve_order"],
  "execution": {
    "input_path": "input.csv",
    "output_path": "output.csv",
    "key_columns": ["编号"],
    "keep": "first",
    "encoding": "utf-8",
    "delimiter": ","
  }
}
```

全部路径限定在工作目录内。要求通过capability、constraints、execution表达，不能在goal/note里附加其他业务条件；未知字段、重复键、缺少业务字段或不支持的约束都会阻断。自由文字返回needs_context，要求提交/确认结构化当前请求；1.9不宣称能证明任意自然语言没有漏项。可先用旧address生成候选，再由用户确认成完整来源，不能将候选自动当作用户授权。

```sh
scripts/rpe source prepare --source task.json --workspace /absolute/workspace \
  --db /absolute/state.sqlite --session-id example --source-ref current-request
scripts/rpe source show STATE_ID --db /absolute/state.sqlite --session-id example
scripts/rpe hermes run --entry-mode clean --prompt-file task.json \
  --workspace /absolute/workspace --db /absolute/state.sqlite
```

prepare/show不调用模型。Hermes clean使用原生rpe_understand；父进程以保存的完整当前原文与固定目录重建实际API消息，不把宿主聊天历史、全局记忆或上轮工具内容传进理解节点。后续由可信代码解析来源、比对提取字段、生成逐项要求账本并锁定执行参数。ready后控制器自动调用既有执行器与独立oracle；模型不再负责复述state_id。最终汇报可以继续调用模型，其消耗照常计入。

要求账本覆盖来源中的显式字段，使用原文SHA、JSON pointer和字段值摘要追溯。它证明这份严格来源未漏字段，不证明用户在其他聊天里曾经想过的条件全部包含。每次任务的会话、来源、目录和输入摘要都重新核对；state_id不授予权限。

控制器执行收据与模型工具收据分别记录。只有rpe_understand是真实模型工具调用，不能把控制器事件包装成模型调用过rpe_task。执行继续复用路径绑定、独立worker、原子发布、oracle、超时与清理。消息与状态隔离不等于每个逻辑节点拥有独立OS进程。

干净入口在真实API发送前再检查来源、工具和阶段。插件未成功加载、消息投影被修改或可信配置变化时，隔离宿主停止发送；不能依赖普通中间件异常，因为宿主可能跳过异常回调继续请求。此保护只安装在本轮隔离的clean进程中。

1.9不改变日常Hermes配置、不创建常驻服务，也不运行另一套学习飞轮。运行评价须分别查看业务产物正确、来源要求覆盖、真实执行链和全部成本；通过测试或减少token都不自动等于优于原生。
