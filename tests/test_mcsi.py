"""MCSI 接口测试。"""

from rpe_harness.mcsi import CausalState, StateTransition, TaskType


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
