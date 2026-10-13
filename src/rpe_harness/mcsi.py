"""MCSI: Minimal Causal State Interface.

节点间不传完整上下文，只传决策所需的最小充分状态。
核心思想：状态充分性优先于上下文完整性。
零外部依赖——仅用标准库。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class TaskType(str, Enum):
    FILE_OPERATION = "file.operation"
    CODE_GENERATION = "code.generation"
    CODE_REVIEW = "code.review"
    DATA_PROCESSING = "data.processing"
    WEB_RESEARCH = "web.research"
    DOCUMENT_WRITING = "document.writing"
    SYSTEM_ADMIN = "system.admin"
    CONVERSATION = "conversation"
    UNKNOWN = "unknown"


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
        return cls(
            task_type=TaskType(event.get("task_type", "unknown")),
            goal=event.get("goal", ""),
            constraints=event.get("constraints", []),
            required_capability=event.get("required_capability"),
            input_format=event.get("input_format"),
            priority=event.get("priority", 5),
            source_ref=event.get("event_id"),
            session_id=event.get("session_id"),
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
