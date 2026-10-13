# RPE-Harness

**面向持续学习 Agent 的状态驱动能力寻址架构**

解决 Hermes Agent 技能库膨胀后的检索退化、冷门技能失联、上下文成本膨胀问题。

## 核心概念

| 模块 | 职责 | 对应问题 |
|------|------|---------|
| **MCSI** | 最小因果状态接口 | 节点间不必传完整上下文 |
| **Capability Router** | 分层能力寻址 L0-L3 | 技能库膨胀后检索退化 |
| **Skill Consolidator** | 程序性能力沉淀 | 重复任务降 LLM 依赖 |
| **Reachability Monitor** | 能力可达性监控 | 冷门技能失联 |

## 架构原则

1. **旁路设计**——不修改 Hermes 核心代码，通过外部索引 + 路由增强
2. **状态充分性优先于上下文完整性**——只传决策所需的最小状态
3. **记忆与执行解耦**——完整记忆在存储层，执行链路只传状态
4. **能力可达性**——技能可以退出活跃记忆，但不能失去可发现性

## 快速开始

```bash
# 安装依赖
pip install -e ".[dev]"

# 运行基线测试（测量当前 Hermes 技能检索性能）
python -m rpe_harness.experiments.baseline

# 启动能力寻址层（旁路模式）
python -m rpe_harness.server --port 18400

# 运行对照实验
python -m rpe_harness.experiments.comparison
```

## 研究指标

- **有效能力利用率** = 被正确发现并成功调用的已有能力 / 当前任务所需且系统实际具备的能力
- **冷门技能恢复率** = 长期未调用但当前适用的技能被正确找到的比例
- **经验沉淀效率** = 经过验证可复用的能力增量 / 探索与学习消耗的总资源
- **Token 成本趋势** = 同类任务随经验积累的 LLM 调用成本变化

## 文档

- [架构设计](docs/architecture.md)
- [MCSI 接口规范](docs/mcsi-spec.md)
- [实验方案](docs/experiments.md)
