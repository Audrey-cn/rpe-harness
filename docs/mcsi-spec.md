# MCSI 接口规范 v0.1

## 概述

Minimal Causal State Interface（最小因果状态接口）定义了 Agent 执行链路中节点间传递的结构化状态格式。

## 核心类型

### CausalState

| 字段 | 类型 | 必需 | 说明 |
|------|------|:----:|------|
| task_type | TaskType | ✅ | 任务类型，L0 路由依据 |
| goal | str | ✅ | 当前目标，一句话描述 |
| constraints | list[str] | | 约束条件 |
| required_capability | str | | 所需能力标识 |
| input_format | str | | 输入格式 |
| priority | int | | 优先级 1-10 |
| source_ref | str | | 来源事件 ID，用于追溯 |
| session_id | str | | 会话 ID |

### TaskType 枚举

```
FILE_OPERATION      文件操作
CODE_GENERATION     代码生成
CODE_REVIEW         代码审查
DATA_PROCESSING     数据处理
WEB_RESEARCH        网络研究
DOCUMENT_WRITING    文档写作
SYSTEM_ADMIN        系统管理
CONVERSATION        对话
UNKNOWN             未知
```

## 使用方式

```python
from rpe_harness.mcsi import CausalState, TaskType

# 从完整事件构造最小状态
state = CausalState.from_event({
    "task_type": "data.processing",
    "goal": "清理 Excel 重复订单",
    "constraints": ["preserve_formulas"],
    "input_format": "xlsx",
    "event_id": "evt-001",
})

# 生成路由键
key = state.to_routing_key()  # "data.processing:xlsx"

# 检查是否足够小
assert state.is_minimal()
```

## 设计约束

1. 单个 CausalState 序列化后 < 500 字符
2. 不包含完整历史上下文
3. 必须保留 source_ref 以便回溯
4. 下游可请求补充上下文（通过 source_ref）
