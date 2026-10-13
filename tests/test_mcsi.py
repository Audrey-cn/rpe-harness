"""MCSI 接口测试。"""

from rpe_harness.mcsi import CausalState, StateTransition, TaskType
import pytest


def test_causal_state_creation():
    state = CausalState(
        task_type=TaskType.FILE_OPERATION,
        goal="读取 Excel 文件",
        constraints=["preserve_formulas"],
        input_format="xlsx",
        required_capability="spreadsheet.cleaning",
    )
    assert state.task_type == TaskType.FILE_OPERATION
    assert state.goal == "读取 Excel 文件"
    assert state.is_minimal()


def test_routing_key():
    state = CausalState(
        task_type=TaskType.CODE_GENERATION,
        goal="写脚本",
        input_format="python",
        required_capability="code.gen",
    )
    key = state.to_routing_key()
    assert "code.generation" in key
    assert "python" in key


def test_from_event():
    event = {
        "task_type": "web.research",
        "goal": "搜索信息",
        "constraints": [],
        "event_id": "evt-001",
        "session_id": "sess-001",
    }
    state = CausalState.from_event(event)
    assert state.task_type == TaskType.WEB_RESEARCH
    assert state.source_ref == "evt-001"


def test_state_transition():
    s1 = CausalState(task_type=TaskType.CONVERSATION, goal="测试")
    s2 = CausalState(task_type=TaskType.CONVERSATION, goal="测试")
    t = StateTransition(prev_state=s1, action="respond", observation="ok", next_state=s2)
    assert t.is_valid()


def test_unknown_task_text_is_preserved_as_an_unknown_state():
    state = CausalState.from_event({"task_type": "quantum.teleport", "goal": "发送消息"})
    assert state.task_type == TaskType.UNKNOWN
    assert state.goal == "发送消息"


@pytest.mark.parametrize("priority", [0, 11, 99, True, "urgent"])
def test_invalid_priority_is_rejected(priority):
    with pytest.raises((TypeError, ValueError)):
        CausalState(task_type=TaskType.DATA_PROCESSING, goal="去重", priority=priority)


@pytest.mark.parametrize("field,value", [("goal", {}), ("constraints", [3]), ("facts", {})])
def test_invalid_state_field_types_are_rejected(field, value):
    event = {"task_type": "data.processing", "goal": "去重", field: value}
    with pytest.raises((TypeError, ValueError)):
        CausalState.from_event(event)


def test_oversized_state_is_rejected_without_truncating_constraints():
    with pytest.raises(ValueError):
        CausalState(task_type=TaskType.FILE_OPERATION, goal="正文" * 10000)


def test_standard_state_roundtrip_preserves_reference_facts_and_constraints():
    state = CausalState.from_event({
        "task_type": "file.operation", "goal": "清理订单",
        "source_ref": "event:original", "event_id": "event:ignored",
        "session_id": "session-A", "constraints": ["preserve_formulas"],
        "facts": ["file_exists"], "created_at": 100.0, "context_tokens_saved": 12,
    })
    again = CausalState.from_event(state.to_dict())
    assert again.source_ref == "event:original"
    assert again.session_id == "session-A"
    assert again.constraints == ["preserve_formulas"]
    assert again.facts == ["file_exists"]
    assert again.created_at == 100.0
    assert again.context_tokens_saved == 12


def test_route_validation_detects_invalid_mutated_state():
    state = CausalState(task_type=TaskType.DATA_PROCESSING, goal="去重")
    state.priority = 99
    with pytest.raises(ValueError):
        state.validate()


@pytest.mark.parametrize("reference", [0, ""])
def test_invalid_explicit_source_ref_cannot_be_hidden_by_an_event_id(reference):
    with pytest.raises((TypeError, ValueError)):
        CausalState.from_event({"goal": "清理订单", "source_ref": reference, "event_id": "valid-id"})
