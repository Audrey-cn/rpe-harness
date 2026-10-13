"""Capability Addressing Layer — 分层能力寻址。

从确定性分类到语义路由、关系检索和复杂推理，
逐级升级处理成本，避免每次都让 LLM 搜索完整技能库。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mcsi import CapabilityLevel, CausalState, TaskType


@dataclass
class SkillEntry:
    """技能索引条目。"""

    skill_id: str
    name: str
    description: str
    task_types: list[TaskType] = field(default_factory=list)
    input_formats: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    path: str = ""
    last_used: float = 0.0
    use_count: int = 0
    success_rate: float = 1.0
    is_dormant: bool = False  # 休眠但可发现

    def matches_state(self, state: CausalState) -> float:
        """计算与给定状态的匹配度（0.0-1.0）。"""
        score = 0.0

        # 任务类型匹配（权重最高）
        if state.task_type in self.task_types:
            score += 0.4
        elif state.task_type == TaskType.UNKNOWN:
            score += 0.1  # 未知任务给低分

        # 输入格式匹配
        if state.input_format and state.input_format in self.input_formats:
            score += 0.2

        # 能力标识匹配
        if state.required_capability and state.required_capability in self.tags:
            score += 0.2

        # 前置条件满足
        if state.constraints:
            satisfied = sum(1 for c in state.constraints if c in self.preconditions)
            if self.preconditions:
                score += 0.1 * (satisfied / len(self.preconditions))

        # 新鲜度加成（近期使用过的技能略优先）
        if self.last_used > 0:
            age_hours = (time.time() - self.last_used) / 3600
            if age_hours < 24:
                score += 0.05
            elif age_hours > 24 * 30:  # 超过30天未用，轻微降权
                score -= 0.05

        return min(max(score, 0.0), 1.0)


@dataclass
class RoutingResult:
    """路由结果。"""

    level: CapabilityLevel
    candidates: list[SkillEntry]
    latency_ms: float
    tokens_used: int = 0
    fallback_to_llm: bool = False


class CapabilityRouter:
    """分层能力路由器。

    L0: 确定性路由（文件类型/事件/固定触发）→ 不调 LLM
    L1: 能力分类（轻量分类器）→ 缩小搜索范围
    L2: 关系与因果检索（前置条件/依赖/历史）→ 找可执行技能
    L3: 开放式推理（复杂/未知任务）→ 交给强模型
    """

    def __init__(self, index_path: Path | None = None):
        self.skills: dict[str, SkillEntry] = {}
        self._deterministic_rules: dict[str, str] = {}  # routing_key -> skill_id
        self.index_path = index_path
        if index_path and index_path.exists():
            self._load_index(index_path)

    def _load_index(self, path: Path) -> None:
        """从 JSON 文件加载技能索引。"""
        data = json.loads(path.read_text())
        for entry in data.get("skills", []):
            skill = SkillEntry(**entry)
            self.skills[skill.skill_id] = skill
        self._deterministic_rules = data.get("deterministic_rules", {})

    def save_index(self, path: Path) -> None:
        """保存技能索引到 JSON 文件。"""
        data = {
            "skills": [
                {
                    "skill_id": s.skill_id,
                    "name": s.name,
                    "description": s.description,
                    "task_types": [t.value for t in s.task_types],
                    "input_formats": s.input_formats,
                    "preconditions": s.preconditions,
                    "tags": s.tags,
                    "path": s.path,
                    "last_used": s.last_used,
                    "use_count": s.use_count,
                    "success_rate": s.success_rate,
                    "is_dormant": s.is_dormant,
                }
                for s in self.skills.values()
            ],
            "deterministic_rules": self._deterministic_rules,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def register_skill(self, skill: SkillEntry) -> None:
        """注册技能到索引。"""
        self.skills[skill.skill_id] = skill
        # 自动生成确定性路由规则
        for tt in skill.task_types:
            for fmt in skill.input_formats or ["*"]:
                key = f"{tt.value}:{fmt}"
                if key not in self._deterministic_rules:
                    self._deterministic_rules[key] = skill.skill_id

    def route(self, state: CausalState, top_k: int = 5) -> RoutingResult:
        """执行分层能力寻址。"""
        start = time.perf_counter()

        # L0: 确定性路由
        routing_key = state.to_routing_key()
        if routing_key in self._deterministic_rules:
            skill_id = self._deterministic_rules[routing_key]
            if skill_id in self.skills:
                skill = self.skills[skill_id]
                if not skill.is_dormant:
                    return RoutingResult(
                        level=CapabilityLevel.DETERMINISTIC,
                        candidates=[skill],
                        latency_ms=(time.perf_counter() - start) * 1000,
                    )

        # L1: 能力分类（基于 task_type 缩小范围）
        candidates = [
            s for s in self.skills.values()
            if state.task_type in s.task_types or state.task_type == TaskType.UNKNOWN
        ]

        if not candidates:
            # L3: 无匹配，交给 LLM
            return RoutingResult(
                level=CapabilityLevel.REASONING,
                candidates=[],
                latency_ms=(time.perf_counter() - start) * 1000,
                fallback_to_llm=True,
            )

        # L2: 关系与因果检索（打分排序）
        scored = [(s.matches_state(state), s) for s in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [s for _, s in scored[:top_k]]

        level = (
            CapabilityLevel.CLASSIFICATION
            if len(top) == 1 and scored[0][0] > 0.8
            else CapabilityLevel.RELATIONAL
        )

        return RoutingResult(
            level=level,
            candidates=top,
            latency_ms=(time.perf_counter() - start) * 1000,
        )

    def find_dormant(self, state: CausalState) -> list[SkillEntry]:
        """查找休眠但匹配的技能——冷门技能恢复。"""
        return [
            s for s in self.skills.values()
            if s.is_dormant and s.matches_state(state) > 0.3
        ]

    def update_usage(self, skill_id: str, success: bool) -> None:
        """更新技能使用统计。"""
        if skill_id in self.skills:
            skill = self.skills[skill_id]
            skill.last_used = time.time()
            skill.use_count += 1
            # 指数移动平均更新成功率
            skill.success_rate = 0.9 * skill.success_rate + 0.1 * (1.0 if success else 0.0)

    def get_stats(self) -> dict[str, Any]:
        """获取路由器统计信息。"""
        active = sum(1 for s in self.skills.values() if not s.is_dormant)
        dormant = sum(1 for s in self.skills.values() if s.is_dormant)
        return {
            "total_skills": len(self.skills),
            "active": active,
            "dormant": dormant,
            "deterministic_rules": len(self._deterministic_rules),
        }
