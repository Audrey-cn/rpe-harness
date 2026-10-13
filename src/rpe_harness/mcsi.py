"""MCSI: Minimal Causal State Interface.

节点间不传完整上下文，只传决策所需的最小充分状态。
核心思想：状态充分性优先于上下文完整性。
零外部依赖——仅用标准库。
"""

from __future__ import annotations

import json
import hashlib
import math
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class TaskType(str, Enum):
    FILE_OPERATION = "file.operation"
    CODE_GENERATION = "code.generation"
    CODE_REVIEW = "code.review"
    DATA_PROCESSING = "data.processing"
    WEB_SEARCH = "web.search"
    WEB_ACADEMIC = "web.academic"
    WEB_DEEP_RESEARCH = "web.deep_research"
    DOCUMENT_WRITING = "document.writing"
    SYSTEM_ADMIN = "system.admin"
    CONVERSATION = "conversation"
    UNKNOWN = "unknown"
    WEB_RESEARCH = "web.search"  # 旧枚举名保留为 WEB_SEARCH 的别名。

    # 兼容旧分类——映射到新子类
    @classmethod
    def from_legacy(cls, value: str) -> "TaskType":
        return cls.parse(value)

    @classmethod
    def parse(cls, value: "TaskType | str | None") -> "TaskType":
        if value is None:
            return cls.UNKNOWN
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("task_type 必须是字符串")
        value = value.strip().lower()
        if value == "web.research":
            return cls.WEB_SEARCH
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class CapabilityLevel(int, Enum):
    DETERMINISTIC = 0
    CLASSIFICATION = 1
    RELATIONAL = 2
    REASONING = 3


@dataclass
class CausalState:
    """最小因果状态——在执行链路中传递的结构化信息。"""

    task_type: TaskType
    goal: str
    constraints: list[str] = field(default_factory=list)
    required_capability: str | None = None
    input_format: str | None = None
    priority: int = 5
    source_ref: str | None = None
    session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    context_tokens_saved: int = 0
    facts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """验证接口类型和有界负载；不推断缺失事实或截断约束。"""
        self.task_type = TaskType.parse(self.task_type)
        if not isinstance(self.goal, str):
            raise TypeError("goal 必须是字符串")
        if not self.goal.strip():
            raise ValueError("goal 不能为空")
        for name in ("constraints", "facts"):
            values = getattr(self, name)
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise TypeError(f"{name} 必须是字符串列表")
            if any(not v.strip() for v in values):
                raise ValueError(f"{name} 不能包含空字符串")
        for name in ("required_capability", "input_format", "source_ref", "session_id"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} 必须是字符串或 None")
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{name} 不能为空字符串")
        if type(self.priority) is not int:
            raise TypeError("priority 必须是整数")
        if not 1 <= self.priority <= 10:
            raise ValueError("priority 必须在 1–10 之间")
        if type(self.created_at) not in (int, float) or not math.isfinite(self.created_at):
            raise ValueError("created_at 必须是有限时间戳")
        if type(self.context_tokens_saved) is not int or self.context_tokens_saved < 0:
            raise ValueError("context_tokens_saved 必须是非负整数")
        if self.input_format:
            self.input_format = self.input_format.strip().lower().lstrip(".")
            if not self.input_format:
                raise ValueError("input_format 不能为空")
        if self.required_capability:
            self.required_capability = self.required_capability.strip()
        if len(json.dumps(self.to_dict(), ensure_ascii=False).encode("utf-8")) > 16384:
            raise ValueError("状态超过 16 KiB；请保存正文并使用来源引用")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["task_type"] = self.task_type.value
        return data

    def to_routing_key(self) -> str:
        parts = [self.task_type.value]
        if self.input_format:
            parts.append(self.input_format)
        if self.required_capability:
            parts.append(self.required_capability)
        return ":".join(parts)

    def is_minimal(self) -> bool:
        return len(json.dumps(asdict(self), default=str)) < 500

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> "CausalState":
        if not isinstance(event, dict):
            raise TypeError("event 必须是 dict")
        source_ref = event.get("source_ref")
        if source_ref is None:
            source_ref = event.get("event_id")
        if source_ref is None:
            canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
            source_ref = "sha1:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
        return cls(
            task_type=TaskType.parse(event.get("task_type")),
            goal=event.get("goal", ""),
            constraints=event.get("constraints", []),
            required_capability=event.get("required_capability"),
            input_format=event.get("input_format"),
            priority=event.get("priority", 5),
            source_ref=source_ref,
            session_id=event.get("session_id"),
            created_at=event.get("created_at", time.time()),
            context_tokens_saved=event.get("context_tokens_saved", 0),
            facts=event.get("facts", []),
        )


@dataclass
class StateTransition:
    prev_state: CausalState
    action: str
    observation: str
    next_state: CausalState
    timestamp: float = field(default_factory=time.time)

    def is_valid(self) -> bool:
        return self.prev_state.goal == self.next_state.goal or bool(self.action)
