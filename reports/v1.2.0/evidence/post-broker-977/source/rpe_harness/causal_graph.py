"""Causal Graph — 技能因果关系图。

记录技能间的前置条件、依赖关系、失败模式和替代路径。
解决：冷门技能失联、技能组合、失败降级。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mcsi import CausalState, TaskType


@dataclass
class CausalEdge:
    """因果边——技能间的关系。"""

    source: str  # 源技能 ID
    target: str  # 目标技能 ID
    relation: str  # depends_on / replaces / fallback_for / composes_with
    condition: str = ""  # 触发条件描述
    confidence: float = 1.0  # 置信度 0-1
    evidence_count: int = 0  # 历史验证次数
    created_at: float = field(default_factory=time.time)


@dataclass
class SkillProfile:
    """技能档案——完整的因果画像。"""

    skill_id: str
    preconditions: list[str] = field(default_factory=list)  # 前置条件
    postconditions: list[str] = field(default_factory=list)  # 后置效果
    failure_modes: list[str] = field(default_factory=list)  # 失败模式
    alternatives: list[str] = field(default_factory=list)  # 替代技能
    composed_with: list[str] = field(default_factory=list)  # 常组合技能
    usage_contexts: list[str] = field(default_factory=list)  # 历史使用场景


class CausalGraph:
    """技能因果关系图。

    节点 = 技能，边 = 因果关系。
    支持：前置条件检查、失败降级、组合推荐、冷门技能发现。
    """

    def __init__(self) -> None:
        self.edges: list[CausalEdge] = []
        self.profiles: dict[str, SkillProfile] = {}
        self._adjacency: dict[str, list[CausalEdge]] = {}

    def add_edge(self, edge: CausalEdge) -> None:
        self.edges.append(edge)
        self._adjacency.setdefault(edge.source, []).append(edge)

    def set_profile(self, profile: SkillProfile) -> None:
        self.profiles[profile.skill_id] = profile

    def get_dependents(self, skill_id: str) -> list[str]:
        """获取依赖此技能的下游技能。"""
        return [e.target for e in self.edges if e.source == skill_id and e.relation == "depends_on"]

    def get_dependencies(self, skill_id: str) -> list[str]:
        """获取此技能依赖的上游技能。"""
        return [e.source for e in self.edges if e.target == skill_id and e.relation == "depends_on"]

    def get_fallbacks(self, skill_id: str) -> list[str]:
        """获取失败时的替代技能。"""
        return [e.target for e in self.edges if e.source == skill_id and e.relation == "fallback_for"]

    def get_companions(self, skill_id: str) -> list[str]:
        """获取常组合使用的技能。"""
        result = []
        for e in self.edges:
            if e.source == skill_id and e.relation == "composes_with":
                result.append(e.target)
            elif e.target == skill_id and e.relation == "composes_with":
                result.append(e.source)
        return result

    def check_preconditions(self, skill_id: str, state: CausalState) -> tuple[bool, list[str]]:
        """检查技能前置条件是否满足。返回 (是否满足, 未满足的条件列表)。"""
        profile = self.profiles.get(skill_id)
        if not profile or not profile.preconditions:
            return True, []

        satisfied = set(state.facts)
        missing = [p for p in profile.preconditions if p not in satisfied]
        return len(missing) == 0, missing

    def find_path(self, from_skill: str, to_skill: str, max_depth: int = 5) -> list[str] | None:
        """BFS 查找技能间路径——用于组合推荐。"""
        if from_skill == to_skill:
            return [from_skill]
        visited = {from_skill}
        queue = [[from_skill]]
        for _ in range(max_depth):
            next_queue = []
            for path in queue:
                current = path[-1]
                for edge in self._adjacency.get(current, []):
                    if edge.target not in visited:
                        new_path = path + [edge.target]
                        if edge.target == to_skill:
                            return new_path
                        visited.add(edge.target)
                        next_queue.append(new_path)
            queue = next_queue
        return None

    def recommend_chain(self, state: CausalState, primary_skill: str) -> list[str]:
        """推荐技能链——主技能 + 常组合技能。"""
        known = set(self.profiles) | {sid for edge in self.edges for sid in (edge.source, edge.target)}
        if primary_skill not in known:
            return []
        chain = [primary_skill]
        companions = self.get_companions(primary_skill)
        for c in companions:
            if c not in chain:
                chain.append(c)
        return chain

    def discover_cold_skills(self, state: CausalState, all_skills: list[str], dormant: set[str], *, min_score: float = 0.6) -> list[dict]:
        """发现休眠但可能适用的技能——冷门技能恢复。"""
        state.validate()
        if type(min_score) not in (int, float) or not 0 < min_score <= 1:
            raise ValueError("min_score 必须在 (0, 1] 之间")
        results = []
        known = set(all_skills)
        for skill_id in sorted(dormant & known):
            profile = self.profiles.get(skill_id)
            if not profile:
                continue
            # 一个充分的独立场景即可触发；同源分组数量不会增加分数。
            context_score = max((self._scenario_score(state.goal, ctx) for ctx in profile.usage_contexts), default=0.0)

            # 2. 关联发现（弱信号，封顶 0.3，且要求任务类型相关）
            relation_score = 0.0
            if profile.usage_contexts or state.task_type.value != "unknown":
                for active in all_skills:
                    if active in dormant:
                        continue
                    path = self.find_path(active, skill_id)
                    if path and len(path) <= 3:
                        relation_score = 0.1  # 只辅助排序，不单独触发。
                        break

            match_score = context_score + relation_score
            # 关联分不能单独触发——必须有场景匹配，且总分够高
            if context_score >= min_score and match_score >= min_score:
                results.append({
                    "skill_id": skill_id,
                    "match_score": round(min(match_score, 1.0), 3),
                    "reason": "场景匹配" if context_score > 0.2 else "关联发现",
                })
        return sorted(results, key=lambda x: (-x["match_score"], x["skill_id"]))

    @staticmethod
    def _scenario_score(goal: str, context: str) -> float:
        generic = {"a", "an", "the", "to", "for", "and", "of", "from", "with", "in", "on", "data", "research", "code", "file", "files", "task", "handle"}
        def tokens(text: str) -> set[str]:
            return set(re.findall(r"[a-z0-9][a-z0-9_-]*|[\u4e00-\u9fff]+", text.lower())) - generic
        goal_tokens, context_tokens = tokens(goal), tokens(context)
        if len(goal_tokens & context_tokens) >= 2:
            return 0.7
        if context_tokens and goal.strip().lower() == context.strip().lower():
            return 0.7
        if len(context_tokens) == 1:
            phrase = next(iter(context_tokens))
            if re.fullmatch(r"[\u4e00-\u9fff]{4,}", phrase) and phrase in goal:
                return 0.7
        return 0.0

    def validate_skill_ids(self, known_ids: set[str]) -> list[str]:
        """返回无法在索引中解析的引用，包含档案中的替代和组合引用。"""
        references = {sid for edge in self.edges for sid in (edge.source, edge.target)}
        references.update(self.profiles)
        for profile in self.profiles.values():
            references.add(profile.skill_id)
            references.update(profile.alternatives)
            references.update(profile.composed_with)
        return sorted(references - known_ids)

    def get_stats(self) -> dict[str, Any]:
        relation_counts = {}
        for e in self.edges:
            relation_counts[e.relation] = relation_counts.get(e.relation, 0) + 1
        return {
            "nodes": len(self.profiles),
            "edges": len(self.edges),
            "relations": relation_counts,
        }

    def save(self, path: Path) -> None:
        data = {
            "edges": [
                {"source": e.source, "target": e.target, "relation": e.relation,
                 "condition": e.condition, "confidence": e.confidence, "evidence_count": e.evidence_count}
                for e in self.edges
            ],
            "profiles": {
                sid: {
                    "skill_id": p.skill_id,
                    "preconditions": p.preconditions,
                    "postconditions": p.postconditions,
                    "failure_modes": p.failure_modes,
                    "alternatives": p.alternatives,
                    "composed_with": p.composed_with,
                    "usage_contexts": p.usage_contexts,
                }
                for sid, p in self.profiles.items()
            },
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> "CausalGraph":
        graph = cls()
        data = json.loads(path.read_text())
        for e in data.get("edges", []):
            graph.add_edge(CausalEdge(**e))
        for sid, p in data.get("profiles", {}).items():
            if p.get("skill_id") != sid:
                raise ValueError(f"档案键与 skill_id 不一致: {sid}")
            graph.set_profile(SkillProfile(**p))
        return graph
