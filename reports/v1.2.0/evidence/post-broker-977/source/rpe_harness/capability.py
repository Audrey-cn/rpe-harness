"""Capability Addressing Layer — 分层能力寻址。

从确定性分类到语义路由、关系检索和复杂推理，
逐级升级处理成本，避免每次都让 LLM 搜索完整技能库。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .mcsi import CapabilityLevel, CausalState, TaskType

if TYPE_CHECKING:
    from .causal_graph import CausalGraph


def _metadata_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9][a-z0-9_-]*|[\u4e00-\u9fff]+", text.lower()))
    return tokens - {"a", "an", "the", "to", "for", "and", "of", "from", "with", "data", "file", "files", "research"}


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
    priority: int = 5  # 1-10, 同类冲突时高优先赢
    enabled: bool = True
    capabilities: list[str] = field(default_factory=list)

    def matches_state(self, state: CausalState) -> float:
        """计算与给定状态的匹配度（0.0-1.0）。"""
        score = 0.0

        # 任务类型匹配（权重最高）
        if state.task_type in self.task_types:
            score += 0.4

        # 输入格式匹配
        if state.input_format and (state.input_format in self.input_formats or "*" in self.input_formats):
            score += 0.2

        # 能力标识匹配
        if state.required_capability and state.required_capability in (self.capabilities or self.tags):
            score += 0.2

        # 独立目标词证据最多贡献 0.2；使用频率和优先级不能补足相关性。
        metadata = " ".join([self.name, self.description, *self.tags, *self.capabilities])
        overlap = _metadata_tokens(state.goal) & _metadata_tokens(metadata)
        score += min(len(overlap), 2) * 0.1

        return min(max(score, 0.0), 1.0)


@dataclass
class RoutingResult:
    """路由结果。"""

    level: CapabilityLevel
    candidates: list[SkillEntry]
    latency_ms: float
    tokens_used: int = 0
    fallback_to_llm: bool = False
    confidence: float = 0.0
    reason: str = ""
    missing_preconditions: dict[str, list[str]] = field(default_factory=dict)


class CapabilityRouter:
    """分层能力路由器。

    L0: 确定性路由（文件类型/事件/固定触发）→ 不调 LLM
    L1: 能力分类（轻量分类器）→ 缩小搜索范围
    L2: 关系与因果检索（前置条件/依赖/历史）→ 找可执行技能
    L3: 开放式推理（复杂/未知任务）→ 交给强模型
    """

    def __init__(self, index_path: Path | None = None, *, graph: "CausalGraph | None" = None, min_confidence: float = 0.6):
        if type(min_confidence) not in (int, float) or not 0 < min_confidence <= 1:
            raise ValueError("min_confidence 必须在 (0, 1] 范围内")
        self.skills: dict[str, SkillEntry] = {}
        self._deterministic_rules: dict[str, str] = {}  # routing_key -> skill_id
        self.index_path = index_path
        self.graph = graph
        self.min_confidence = min_confidence
        if index_path and index_path.exists():
            self._load_index(index_path)

    def _load_index(self, path: Path) -> None:
        """从 JSON 文件加载技能索引。"""
        data = json.loads(path.read_text())
        for raw in data.get("skills", []):
            entry = dict(raw)
            entry["task_types"] = [TaskType.parse(t) for t in entry.get("task_types", [])]
            skill = SkillEntry(**entry)
            self.skills[skill.skill_id] = skill
        self.rebuild_rules()

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
                    "priority": s.priority,
                    "enabled": s.enabled,
                    "capabilities": s.capabilities,
                }
                for s in self.skills.values()
            ],
            "deterministic_rules": self._deterministic_rules,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def register_skill(self, skill: SkillEntry) -> None:
        """注册技能到索引。"""
        self.skills[skill.skill_id] = skill
        self.rebuild_rules()

    def rebuild_rules(self) -> None:
        """从当前声明重建规则，清除旧分类和标注变更留下的通配路由。"""
        self._deterministic_rules = {}
        for skill in sorted(self.skills.values(), key=lambda s: (-s.priority, s.skill_id)):
            self._register_rules(skill)

    def _register_rules(self, skill: SkillEntry) -> None:
        # 生成确定性路由规则（高优先才覆盖）
        for tt in skill.task_types:
            if tt == TaskType.UNKNOWN:
                continue
            for fmt in skill.input_formats or ["*"]:
                key = f"{tt.value}:{fmt}"
                self._deterministic_rules.setdefault(key, skill.skill_id)
                for capability in skill.capabilities or skill.tags:
                    self._deterministic_rules.setdefault(f"{key}:{capability}", skill.skill_id)

    def _eligibility(self, skill: SkillEntry, state: CausalState, *, allow_dormant: bool = False) -> tuple[bool, list[str]]:
        if not skill.enabled or (skill.is_dormant and not allow_dormant):
            return False, []
        if state.task_type not in skill.task_types and TaskType.UNKNOWN not in skill.task_types:
            return False, []
        if state.input_format and state.input_format not in skill.input_formats and "*" not in skill.input_formats:
            return False, []
        if state.required_capability and state.required_capability not in (skill.capabilities or skill.tags):
            return False, []
        required = list(skill.preconditions)
        if self.graph and skill.skill_id in self.graph.profiles:
            required.extend(self.graph.profiles[skill.skill_id].preconditions)
        missing = [fact for fact in dict.fromkeys(required) if fact not in state.facts]
        return not missing, missing

    def route(self, state: CausalState, top_k: int = 5) -> RoutingResult:
        """执行分层能力寻址。"""
        start = time.perf_counter()
        state.validate()
        if type(top_k) is not int:
            raise TypeError("top_k 必须是整数")
        if not 1 <= top_k <= 100:
            raise ValueError("top_k 必须在 1–100 之间")

        def finish(level: CapabilityLevel, candidates: list[SkillEntry], *, confidence: float = 0.0, reason: str = "", missing: dict[str, list[str]] | None = None) -> RoutingResult:
            return RoutingResult(level=level, candidates=candidates,
                latency_ms=(time.perf_counter() - start) * 1000,
                fallback_to_llm=level == CapabilityLevel.REASONING,
                confidence=round(confidence, 6), reason=reason,
                missing_preconditions=missing or {})

        if state.task_type == TaskType.UNKNOWN:
            return finish(CapabilityLevel.REASONING, [], reason="unknown_task")

        # L0: 确定性路由（精确匹配 → 通配匹配）
        keys = [state.to_routing_key()]
        if state.input_format:
            keys.append(f"{state.task_type.value}:{state.input_format}")
        if state.required_capability:
            keys.append(f"{state.task_type.value}:*:{state.required_capability}")
        keys.append(f"{state.task_type.value}:*")
        blocked: dict[str, list[str]] = {}
        for key in dict.fromkeys(keys):
            skill_id = self._deterministic_rules.get(key)
            skill = self.skills.get(skill_id) if skill_id else None
            if not skill:
                continue
            eligible, missing = self._eligibility(skill, state)
            if missing:
                blocked[skill.skill_id] = missing
            confidence = skill.matches_state(state)
            if eligible and confidence >= self.min_confidence:
                return finish(CapabilityLevel.DETERMINISTIC, [skill], confidence=confidence, reason="exact_rule")

        # L1: 能力分类（基于 task_type 缩小范围）
        candidates = []
        for skill in self.skills.values():
            eligible, missing = self._eligibility(skill, state)
            if eligible:
                candidates.append(skill)
            elif missing:
                blocked[skill.skill_id] = missing
        if not candidates:
            reason = "missing_facts" if blocked else "no_match"
            return finish(CapabilityLevel.REASONING, [], reason=reason, missing=blocked)

        # L2: 关系与因果检索（打分排序，priority 作 tiebreaker）
        scored = [(s.matches_state(state), s) for s in candidates]
        scored.sort(key=lambda x: (-x[0], -x[1].priority, -x[1].success_rate, x[1].skill_id))
        if scored[0][0] < self.min_confidence:
            return finish(CapabilityLevel.REASONING, [], confidence=scored[0][0], reason="low_confidence", missing=blocked)
        top = [s for score, s in scored if score >= self.min_confidence][:top_k]

        level = (
            CapabilityLevel.CLASSIFICATION
            if len(top) == 1 and scored[0][0] > 0.8
            else CapabilityLevel.RELATIONAL
        )

        return finish(level, top, confidence=scored[0][0], reason="metadata_match")

    def find_dormant(self, state: CausalState) -> list[SkillEntry]:
        """查找休眠但匹配的技能——冷门技能恢复。"""
        state.validate()
        if state.task_type == TaskType.UNKNOWN:
            return []
        return [
            s for s in self.skills.values()
            if s.is_dormant and self._eligibility(s, state, allow_dormant=True)[0]
            and s.matches_state(state) >= self.min_confidence
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
        active = sum(1 for s in self.skills.values() if s.enabled and not s.is_dormant)
        dormant = sum(1 for s in self.skills.values() if s.enabled and s.is_dormant)
        return {
            "total_skills": len(self.skills),
            "active": active,
            "dormant": dormant,
            "disabled": sum(1 for s in self.skills.values() if not s.enabled),
            "deterministic_rules": len(self._deterministic_rules),
        }
