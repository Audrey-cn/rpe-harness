"""MCSI 桥接测试：5 种不同格式的会话事件 → 5 个字段完整的 CausalState。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rpe_harness.bridge import EventToStateConverter
from rpe_harness.mcsi import CausalState, TaskType


def make_events() -> list[dict]:
    """5 个不同格式的会话事件：规范 snake_case / camelCase / 嵌套 payload /
    Hermes 消息行（metadata 包装）/ 旧分类 + 单字符串约束。"""
    return [
        # 1. 规范 snake_case
        {
            "event_id": "evt-0001",
            "session_id": "sess-A",
            "task_type": "file.operation",
            "goal": "读取并清洗 Excel 报表",
            "constraints": ["preserve_formulas", "no_data_loss"],
            "input_format": "xlsx",
            "required_capability": "spreadsheet.cleaning",
            "priority": 3,
            # 噪声字段——应被过滤
            "timestamp": 1760000000.0,
            "tool_calls": [{"name": "read_file", "args": {"path": "a.xlsx"}}],
            "raw_payload": "x" * 5000,
        },
        # 2. camelCase
        {
            "eventId": "evt-0002",
            "sessionId": "sess-B",
            "taskType": "code.generation",
            "objective": "生成数据处理脚本",
            "constraint": "stdlib_only",
            "inputFormat": "python",
            "requiredCapability": "code.gen",
        },
        # 3. 嵌套 payload
        {
            "event_id": "evt-0003",
            "session_id": "sess-C",
            "payload": {
                "type": "web.search",
                "goal": "检索 RPE 相关论文",
                "constraints": ["recency_30d", "english_only"],
                "input_format": "query",
                "capability": "web.search",
            },
            "trace": {"span": "abc", "duration_ms": 12},
        },
        # 4. Hermes 消息行（metadata 包装 + 大量噪声）
        {
            "message_uid": "evt-0004",
            "session_id": "sess-D",
            "role": "user",
            "content": "（对话正文，非决策字段）请帮我写文档……",
            "timestamp": 1760000100.0,
            "token_count": 2048,
            "metadata": {
                "task_type": "document.writing",
                "goal": "撰写架构设计文档",
                "constraints": ["markdown", "cite_sources"],
                "input_format": "markdown",
                "required_capability": "doc.writing",
                "priority": 7,
            },
        },
        # 5. 旧分类 + 单字符串约束 + 别名键
        {
            "id": "evt-0005",
            "session": "sess-E",
            "type": "web.research",
            "description": "调研竞品定价策略",
            "constraints": "中文来源; 带引用",
            "format": "json",
        },
    ]


EXPECTED = [
    {
        "task_type": TaskType.FILE_OPERATION,
        "goal": "读取并清洗 Excel 报表",
        "constraints": ["preserve_formulas", "no_data_loss"],
        "input_format": "xlsx",
        "required_capability": "spreadsheet.cleaning",
        "priority": 3,
        "source_ref": "evt-0001",
        "session_id": "sess-A",
    },
    {
        "task_type": TaskType.CODE_GENERATION,
        "goal": "生成数据处理脚本",
        "constraints": ["stdlib_only"],
        "input_format": "python",
        "required_capability": "code.gen",
        "priority": 5,
        "source_ref": "evt-0002",
        "session_id": "sess-B",
    },
    {
        "task_type": TaskType.WEB_SEARCH,
        "goal": "检索 RPE 相关论文",
        "constraints": ["recency_30d", "english_only"],
        "input_format": "query",
        "required_capability": "web.search",
        "priority": 5,
        "source_ref": "evt-0003",
        "session_id": "sess-C",
    },
    {
        "task_type": TaskType.DOCUMENT_WRITING,
        "goal": "撰写架构设计文档",
        "constraints": ["markdown", "cite_sources"],
        "input_format": "markdown",
        "required_capability": "doc.writing",
        "priority": 7,
        "source_ref": "evt-0004",
        "session_id": "sess-D",
    },
    {
        "task_type": TaskType.WEB_SEARCH,  # 旧分类 web.research 映射
        "goal": "调研竞品定价策略",
        "constraints": ["中文来源", "带引用"],
        "input_format": "json",
        "required_capability": None,
        "priority": 5,
        "source_ref": "evt-0005",
        "session_id": "sess-E",
    },
]


class TestEventToStateConverter(unittest.TestCase):
    def setUp(self) -> None:
        self.converter = EventToStateConverter()

    def assert_state_complete(self, state: CausalState, exp: dict) -> None:
        self.assertIsInstance(state, CausalState)
        self.assertEqual(state.task_type, exp["task_type"])
        self.assertEqual(state.goal, exp["goal"])
        self.assertEqual(state.constraints, exp["constraints"])
        self.assertEqual(state.input_format, exp["input_format"])
        self.assertEqual(state.required_capability, exp["required_capability"])
        self.assertEqual(state.priority, exp["priority"])
        self.assertEqual(state.source_ref, exp["source_ref"])
        self.assertEqual(state.session_id, exp["session_id"])

    def test_five_event_formats_to_five_states(self) -> None:
        """核心用例：5 个不同格式事件 → 5 个字段完整的 CausalState。"""
        states = self.converter.convert_all(make_events())
        self.assertEqual(len(states), 5)
        for state, exp in zip(states, EXPECTED):
            self.assert_state_complete(state, exp)
        self.assertEqual(len({s.source_ref for s in states}), 5, "source_ref 必须唯一可追溯")

    def test_minimal_filtering_drops_noise(self) -> None:
        """MCSI 原则：非必要字段被过滤，只留决策所需最小信息。"""
        minimal = self.converter.extract_minimal(make_events()[0])
        for noise in ("timestamp", "tool_calls", "raw_payload"):
            self.assertNotIn(noise, minimal)
        self.assertEqual(
            set(minimal),
            {"task_type", "goal", "constraints", "input_format",
             "required_capability", "priority", "source_ref", "session_id"},
        )
        for state in self.converter.convert_all(make_events()):
            self.assertTrue(state.is_minimal(), "CausalState 应保持最小化")

    def test_batch_file_json_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            path.write_text(json.dumps(make_events(), ensure_ascii=False), encoding="utf-8")
            states = self.converter.convert_file(path)
        self.assertEqual(len(states), 5)
        for state, exp in zip(states, EXPECTED):
            self.assert_state_complete(state, exp)

    def test_batch_file_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                "\n".join(json.dumps(ev, ensure_ascii=False) for ev in make_events()),
                encoding="utf-8",
            )
            states = self.converter.convert_file(path)
        self.assertEqual(len(states), 5)
        self.assertEqual([s.source_ref for s in states],
                         [e["source_ref"] for e in EXPECTED])

    def test_dump_state_sequence(self) -> None:
        states = self.converter.convert_all(make_events())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "states.json"
            text = self.converter.dump_states(states, out)
            loaded = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(loaded), 5)
        self.assertEqual(loaded[0]["task_type"], "file.operation")
        self.assertEqual(json.loads(text)[3]["source_ref"], "evt-0004")

    def test_missing_event_id_gets_traceable_fallback_ref(self) -> None:
        state = self.converter.convert({"task_type": "conversation", "goal": "闲聊"})
        self.assertIsNotNone(state.source_ref)
        self.assertTrue((state.source_ref or "").startswith("sha1:"))
        # 同一事件内容得到同一引用，可重复追溯
        again = self.converter.convert({"goal": "闲聊", "task_type": "conversation"})
        self.assertEqual(state.source_ref, again.source_ref)

    def test_unknown_task_type_degrades_to_unknown(self) -> None:
        state = self.converter.convert({"event_id": "e", "task_type": "quantum.teleport", "goal": "x"})
        self.assertEqual(state.task_type, TaskType.UNKNOWN)
        self.assertEqual(state.source_ref, "e")

    def test_dumped_standard_state_keeps_source_reference(self) -> None:
        state = self.converter.convert({"event_id": "original", "goal": "清理订单"})
        dumped = json.loads(self.converter.dump_states([state]))[0]
        again = self.converter.convert(dumped)
        self.assertEqual(again.source_ref, "original")

    def test_standard_source_ref_and_facts_take_precedence(self) -> None:
        state = self.converter.convert({
            "source_ref": "stored:event", "event_id": "ignored", "goal": "清理订单",
            "constraints": ["preserve_formulas"], "facts": ["file_exists"],
            "metadata": {"sourceRef": "nested:ignored"},
        })
        self.assertEqual(state.source_ref, "stored:event")
        self.assertEqual(state.constraints, ["preserve_formulas"])
        self.assertEqual(state.facts, ["file_exists"])

    def test_camel_case_confirmed_facts_are_normalized(self) -> None:
        state = self.converter.convert({"goal": "清理订单", "confirmedFacts": ["file_exists"]})
        self.assertEqual(state.facts, ["file_exists"])

    def test_invalid_priority_is_not_silently_defaulted(self) -> None:
        for priority in (99, "urgent", True):
            with self.subTest(priority=priority), self.assertRaises((TypeError, ValueError)):
                self.converter.convert({"goal": "清理订单", "priority": priority})

    def test_null_priority_is_not_silently_defaulted(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            self.converter.convert({"goal": "清理订单", "priority": None})

    def test_zero_creation_timestamp_survives_standard_roundtrip(self) -> None:
        state = CausalState(TaskType.CONVERSATION, "问候", source_ref="original", created_at=0.0)
        dumped = json.loads(self.converter.dump_states([state]))[0]
        self.assertEqual(self.converter.convert(dumped).created_at, 0.0)

    def test_ignored_metadata_priority_cannot_override_standard_priority(self) -> None:
        state = self.converter.convert({"goal": "清理订单", "priority": 3, "metadata": {"priority": None}})
        self.assertEqual(state.priority, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
