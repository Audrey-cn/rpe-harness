# 统一能力目录和必要状态交接

1.8 提供一条有限的快速寻址路径：Hermes 理解当前请求，父进程校验能力与业务参数，保存必要状态；执行阶段只提交状态 ID，仍由原有执行器和独立验证器处理。

目前严格执行覆盖表格去重、JSON 空白规范化和已绑定原生 XLSX helper 的只读检查。普通技能描述只供发现，不会注册执行器。旧 `/route` 和 `/skills` 保留兼容；新入口使用同一 CapabilityCatalog。

## 查看能力

```sh
./scripts/rpe capabilities list
./scripts/rpe capabilities resolve --capability json.normalize --input-format json --constraint preserve_original
```

没有可信 XLSX 技能绑定时，`xlsx.inspect` 会显示不可执行。使用既有技能快照时，显式添加同一套 `--db`、`--catalog-root` 和可选 `--skill-snapshot-id`。信任或当前快照变化会使旧目录失效，需要重新构造或明确 reload；过时目录不能继续授权。

## 结构化任务准备

调用方已给出明确参数时无需模型。将下列 JSON 保存为 `intent.json`，并在一个专用工作目录中准备 `input.json`；输出必须尚不存在：

```json
{
  "goal": "压缩 JSON 外部空白，保留原文件、值与对象顺序",
  "capability": "json.normalize",
  "constraints": ["preserve_original", "preserve_order"],
  "execution": {
    "input_path": "input.json",
    "output_path": "output.json",
    "style": "minify"
  }
}
```

```sh
./scripts/rpe intent prepare --db /ABSOLUTE/OWNED/state.sqlite --workspace /ABSOLUTE/WORKSPACE --session-id task-a --source-ref request-a --extracted /ABSOLUTE/intent.json
```

成功只返回 `state_id`、`capability`、`constraints`、`catalog_sha256`、`status` 五个字段。它表示参数已准备，尚未执行。`intent show STATE_ID --db ... --session-id task-a` 仅查看交接，不重新解释用户语义。

缺字段返回 `needs_context`；显式来源冲突返回 `conflict`；无注册能力或适配返回 `unsupported`；无效参数和超预算返回拒绝。来源事实只能约束提取，不能悄悄补成标准答案。新意图入口明确支持 `TAB`、字面 `\\t` 到单个制表符的别名规范化，保留转换记录；旧精确执行契约不变。Tab 内容仍使用 `.csv` 路径，本版本不扩展 `.tsv` 后缀。

## 自然请求交给 Hermes

```sh
./scripts/rpe hermes run --entry-mode address --db /ABSOLUTE/OWNED/state.sqlite --workspace /ABSOLUTE/WORKSPACE --prompt-file /ABSOLUTE/task.txt --catalog-root /ABSOLUTE/OWNED/catalog
```

无技能快照、仅使用 CSV/JSON 时可以不传 `--catalog-root`。真实 XLSX 检查须先使用既有 `skills sync` 和 `skills trust` 管理原始 helper 来源。

原生工具链为 `rpe_understand → rpe_task(state_id) → 终态`。理解阶段保留当前原始请求；任务阶段实际模型消息只包含固定说明和必要交接，不重复携带原始任务及理解工具历史。官方执行 middleware 记录消息摘要和字节数，最终 gate 复核这些证据以及实际产物。RPC 载荷大小与实际 API 消息大小分别报告。

理解错误最多纠正三次，业务执行最多一次。第二阶段不能改写父进程保存的参数。输入变化、跨会话状态、篡改、目录失效或信任变化会拒绝。旧 `context_sources` 多轮来源协议仅供 skills 模式；address 传入非空列表会明确拒绝，不能把它当成已支持多轮自动事实恢复。

## 宿主学习和验收范围

插件不新增经验训练循环、技能整理器或调度器。工具结果提供真实验证摘要和失败代码，普通经验沉淀由 Hermes 现有机制处理。为冻结对照，隔离测试 profile 关闭未登记的自动变更；这不构成长期飞轮效果实测。

本版本的正式对照仅覆盖四种公开生产任务和两个拒识探针，模型和工具策略范围见对应验收报告。能力数、单次函数耗时和结构化交接，不代表已经证明大规模技能库或任意自然任务的普遍收益。
