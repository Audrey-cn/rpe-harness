"""MCSI 桥接：Hermes 会话事件流 → 最小因果状态（CausalState）。

Hermes 状态库（~/.hermes/state.db）只有 messages / sessions 表，并无独立 events 表；
事件流以 messages 行的 JSON 载荷形式存在。本模块接收由该事件流导出的 JSON 事件
（dict、JSON 数组、JSONL），按 MCSI 原则构造 CausalState。

核心原则：
- 状态充分性优先于上下文完整性：白名单提取，决策无关字段一律丢弃。
- 溯源：每个 CausalState.source_ref 指向原始事件 ID；无 ID 时用事件内容哈希兜底。
- 容错：字段命名风格不一（snake_case / camelCase / 嵌套 payload / 旧分类）均能提取。
- 零外部依赖：仅标准库 + dataclass。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .mcsi import CausalState, TaskType

# 决策所需最小字段白名单 → 各格式下的候选键名（按优先级排列）。
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "task_type": ("task_type", "taskType", "type", "task", "category", "kind"),
    "goal": ("goal", "objective", "intent", "description", "title", "prompt", "query", "content", "text"),
    "constraints": ("constraints", "constraint", "limits", "restrictions"),
    "facts": ("facts", "confirmed_facts", "confirmedFacts", "environment_facts", "environmentFacts"),
    "input_format": ("input_format", "inputFormat", "format", "input_type", "inputType"),
    "required_capability": ("required_capability", "requiredCapability", "capability", "skill"),
    "priority": ("priority", "prio", "urgency"),
    "source_ref": ("source_ref", "sourceRef", "event_id", "eventId", "event_uid", "message_uid", "messageId", "uid", "id"),
    "session_id": ("session_id", "sessionId", "session", "conversation_id", "conversationId"),
    "created_at": ("created_at",),
    "context_tokens_saved": ("context_tokens_saved",),
}

# 事件可能把字段包在这些容器里（Hermes 消息行常见 metadata/payload 包装）。
_NESTED_CONTAINERS: tuple[str, ...] = (
    "payload", "data", "metadata", "event", "body", "context", "attributes", "state",
)
_MISSING = object()


class EventToStateConverter:
    """从 JSON 会话事件中提取最小因果状态。"""

    def __init__(self, extra_aliases: dict[str, Iterable[str]] | None = None) -> None:
        self._aliases = {k: tuple(v) for k, v in _FIELD_ALIASES.items()}
        if extra_aliases:
            for key, names in extra_aliases.items():
                self._aliases[key] = tuple(names) + self._aliases.get(key, ())

    # ---------- 单事件提取 ----------

    def extract_minimal(self, event: dict[str, Any]) -> dict[str, Any]:
        """白名单提取：只保留决策所需的最小字段，其余全部丢弃。"""
        if not isinstance(event, dict):
            raise TypeError(f"event 必须是 dict，得到 {type(event).__name__}")
        sources = self._candidate_sources(event)
        out: dict[str, Any] = {}
        for field_name, aliases in self._aliases.items():
            raw = self._lookup(sources, aliases, preserve_none=field_name == "priority")
            if raw is _MISSING:
                continue
            value = self._normalize(field_name, raw)
            if value not in (None, "", [], 0) or field_name in ("priority", "created_at", "context_tokens_saved"):
                out[field_name] = value
        if "source_ref" not in out:
            out["source_ref"] = self._fallback_ref(event)
        return out

    def convert(self, event: dict[str, Any]) -> CausalState:
        """单个事件 → CausalState。"""
        minimal = self.extract_minimal(event)
        return CausalState.from_event(minimal)

    def convert_all(self, events: Iterable[dict[str, Any]]) -> list[CausalState]:
        """事件序列 → 状态序列（保持原顺序）。"""
        return [self.convert(ev) for ev in events]

    # ---------- 批量文件读取 ----------

    def load_events(self, path: str | Path) -> list[dict[str, Any]]:
        """读取事件文件。支持：JSON 数组、{"events": [...]} 包装、JSONL（每行一个 JSON）。"""
        text = Path(path).read_text(encoding="utf-8").strip()
        if not text:
            return []
        if text.startswith("["):
            data = json.loads(text)
            return [self._as_event(ev, path, i) for i, ev in enumerate(data)]
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return self._load_jsonl(text, path)  # 多行 JSON 对象 → JSONL
            if isinstance(data, dict):
                data = data.get("events", [data])
            if not isinstance(data, list):
                raise ValueError(f"{path}: 顶层 JSON 必须是数组或含 events 数组的对象")
            return [self._as_event(ev, path, i) for i, ev in enumerate(data)]
        return self._load_jsonl(text, path)

    @staticmethod
    def _load_jsonl(text: str, path: str | Path) -> list[dict[str, Any]]:
        events = []
        for i, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            events.append(EventToStateConverter._as_event(json.loads(line), path, i))
        return events

    def convert_file(self, path: str | Path) -> list[CausalState]:
        """批量事件文件 → 状态序列。"""
        return self.convert_all(self.load_events(path))

    # ---------- 状态序列输出 ----------

    @staticmethod
    def states_to_dicts(states: Iterable[CausalState]) -> list[dict[str, Any]]:
        """状态序列 → 可 JSON 序列化的 dict 列表（task_type 落为枚举值）。"""
        return [st.to_dict() for st in states]

    def dump_states(self, states: Iterable[CausalState], path: str | Path | None = None) -> str:
        """状态序列 → JSON 文本；给了 path 就同时写文件。"""
        text = json.dumps(self.states_to_dicts(states), ensure_ascii=False, indent=2)
        if path is not None:
            Path(path).write_text(text, encoding="utf-8")
        return text

    # ---------- 内部工具 ----------

    @staticmethod
    def _candidate_sources(event: dict[str, Any]) -> list[dict[str, Any]]:
        sources = [event]
        for key in _NESTED_CONTAINERS:
            nested = event.get(key)
            if isinstance(nested, dict):
                sources.append(nested)
        return sources

    @staticmethod
    def _lookup(sources: list[dict[str, Any]], aliases: Iterable[str], *, preserve_none: bool = False) -> Any:
        # 别名优先级主导：主键名（如 goal）即使在嵌套容器里，
        # 也优先于外层的兜底别名（如 content）。
        for name in aliases:
            for source in sources:
                if name in source and (source[name] is not None or preserve_none):
                    return source[name]
        return _MISSING

    @staticmethod
    def _normalize(field_name: str, raw: Any) -> Any:
        if field_name == "task_type":
            return EventToStateConverter._normalize_task_type(raw)
        if field_name in ("constraints", "facts"):
            return EventToStateConverter._normalize_constraints(raw)
        if field_name == "priority":
            if type(raw) is int:
                return raw
            if isinstance(raw, str) and raw.strip().isdecimal():
                return int(raw)
            raise TypeError("priority 必须是整数")
        if field_name in ("created_at", "context_tokens_saved"):
            return raw
        if field_name in ("source_ref", "session_id") and type(raw) is int:
            return str(raw)
        if not isinstance(raw, str):
            raise TypeError(f"{field_name} 必须是字符串")
        return raw.strip()

    @staticmethod
    def _normalize_task_type(raw: Any) -> TaskType:
        return TaskType.parse(raw)

    @staticmethod
    def _normalize_constraints(raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.replace("；", ";").replace("、", ",").replace("\n", ",").replace(";", ",").split(",")]
            return [p for p in parts if p]
        if isinstance(raw, (list, tuple, set)):
            if any(not isinstance(x, str) for x in raw):
                raise TypeError("约束和事实必须是字符串列表")
            return [x.strip() for x in raw if x.strip()]
        raise TypeError("约束和事实必须是字符串列表")

    @staticmethod
    def _fallback_ref(event: dict[str, Any]) -> str:
        """无事件 ID 时用规范化内容哈希兜底，保证 source_ref 可追溯且稳定。"""
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        return "sha1:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _as_event(obj: Any, path: str | Path, index: int) -> dict[str, Any]:
        if not isinstance(obj, dict):
            raise ValueError(f"{path}: 第 {index} 条事件不是 JSON 对象")
        return obj
