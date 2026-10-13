"""能力路由器测试。"""

from rpe_harness.capability import CapabilityRouter, SkillEntry
from rpe_harness.mcsi import CapabilityLevel, CausalState, TaskType
from rpe_harness.causal_graph import CausalGraph, SkillProfile
import pytest


def make_router() -> CapabilityRouter:
    router = CapabilityRouter()
    router.register_skill(SkillEntry(
        skill_id="xlsx",
        name="xlsx",
        description="Excel 处理",
        task_types=[TaskType.DATA_PROCESSING, TaskType.FILE_OPERATION],
        input_formats=["xlsx", "csv"],
        tags=["spreadsheet"],
    ))
    router.register_skill(SkillEntry(
        skill_id="code-gen",
        name="code-gen",
        description="代码生成",
        task_types=[TaskType.CODE_GENERATION],
        input_formats=["python"],
        tags=["code"],
    ))
    return router


def test_deterministic_routing():
    router = make_router()
    state = CausalState(
        task_type=TaskType.DATA_PROCESSING,
        goal="处理数据",
        input_format="xlsx",
    )
    result = router.route(state)
    assert result.level == CapabilityLevel.DETERMINISTIC
    assert result.candidates[0].skill_id == "xlsx"


def test_relational_routing():
    router = make_router()
    state = CausalState(
        task_type=TaskType.CODE_GENERATION,
        goal="写代码",
        input_format="python",
    )
    result = router.route(state)
    assert result.candidates[0].skill_id == "code-gen"


def test_dormant_skill_discovery():
    router = make_router()
    router.skills["xlsx"].is_dormant = True
    state = CausalState(
        task_type=TaskType.DATA_PROCESSING,
        goal="处理数据",
        input_format="xlsx",
    )
    dormant = router.find_dormant(state)
    assert len(dormant) == 1
    assert dormant[0].skill_id == "xlsx"


def test_usage_tracking():
    router = make_router()
    router.update_usage("xlsx", success=True)
    assert router.skills["xlsx"].use_count == 1
    assert router.skills["xlsx"].last_used > 0


def test_stats():
    router = make_router()
    stats = router.get_stats()
    assert stats["total_skills"] == 2
    assert stats["active"] == 2


def test_unknown_task_does_not_use_a_catch_all_rule():
    router = CapabilityRouter()
    router.register_skill(SkillEntry("notes", "Notes", "记录便签", task_types=[TaskType.UNKNOWN]))
    result = router.route(CausalState(TaskType.UNKNOWN, "清理 Excel 重复订单"))
    assert result.candidates == []
    assert result.level == CapabilityLevel.REASONING
    assert result.fallback_to_llm


def test_capability_does_not_bypass_the_exact_format_rule():
    router = CapabilityRouter()
    router.register_skill(SkillEntry("sheet", "Sheet", "表格", task_types=[TaskType.FILE_OPERATION], input_formats=["xlsx", "*"], tags=["spreadsheet"], priority=9))
    router.register_skill(SkillEntry("pdf", "PDF", "PDF文档", task_types=[TaskType.FILE_OPERATION], input_formats=["pdf"], tags=["document"], priority=8))
    result = router.route(CausalState(TaskType.FILE_OPERATION, "读取文档", input_format="pdf", required_capability="document"))
    assert [s.skill_id for s in result.candidates] == ["pdf"]


def test_declared_capability_is_required_for_a_deterministic_match():
    router = make_router()
    result = router.route(CausalState(TaskType.DATA_PROCESSING, "处理数据", input_format="xlsx", required_capability="document"))
    assert result.candidates == []
    assert result.fallback_to_llm


def test_known_format_without_a_declaration_is_rejected():
    router = CapabilityRouter()
    router.register_skill(SkillEntry("generic", "Generic", "处理表格", task_types=[TaskType.DATA_PROCESSING]))
    result = router.route(CausalState(TaskType.DATA_PROCESSING, "处理表格", input_format="csv"))
    assert result.candidates == []
    assert result.fallback_to_llm


@pytest.mark.parametrize("status", ["dormant", "disabled"])
def test_unavailable_skill_is_not_returned_by_metadata_fallback(status):
    router = make_router()
    router._deterministic_rules.clear()  # Force the public metadata fallback path.
    if status == "dormant":
        router.skills["xlsx"].is_dormant = True
    else:
        router.skills["xlsx"].enabled = False
    result = router.route(CausalState(TaskType.DATA_PROCESSING, "处理表格", input_format="xlsx"))
    assert result.candidates == []
    assert result.fallback_to_llm


def test_task_type_alone_has_insufficient_confidence():
    router = make_router()
    result = router.route(CausalState(TaskType.DATA_PROCESSING, "整理订单"))
    assert result.candidates == []
    assert result.fallback_to_llm
    assert result.reason == "low_confidence"


def test_user_constraint_cannot_supply_an_environment_fact():
    router = make_router()
    router.skills["xlsx"].preconditions = ["file_exists"]
    result = router.route(CausalState(TaskType.DATA_PROCESSING, "处理表格", constraints=["file_exists"], input_format="xlsx"))
    assert result.candidates == []
    assert result.missing_preconditions == {"xlsx": ["file_exists"]}


def test_confirmed_fact_satisfies_index_and_graph_preconditions():
    graph = CausalGraph()
    graph.set_profile(SkillProfile("sheet", preconditions=["file_exists"]))
    router = CapabilityRouter(graph=graph)
    router.register_skill(SkillEntry("sheet", "Sheet", "表格", task_types=[TaskType.DATA_PROCESSING], input_formats=["csv"], preconditions=["readable"], capabilities=["tabular.deduplicate"]))
    result = router.route(CausalState.from_event({"task_type": "data.processing", "goal": "去重", "input_format": "csv", "required_capability": "tabular.deduplicate", "facts": ["file_exists", "readable"]}))
    assert [s.skill_id for s in result.candidates] == ["sheet"]
    assert not result.fallback_to_llm


def test_metadata_can_select_a_declared_capability_without_a_rule():
    router = CapabilityRouter()
    router.register_skill(SkillEntry("dedup", "Deduplication", "remove duplicate records", task_types=[TaskType.UNKNOWN], input_formats=["csv"], capabilities=["tabular.deduplicate"]))
    result = router.route(CausalState.from_event({"task_type": "data.processing", "goal": "remove duplicate records", "input_format": "csv", "required_capability": "tabular.deduplicate"}))
    assert [s.skill_id for s in result.candidates] == ["dedup"]
    assert result.level == CapabilityLevel.RELATIONAL
    assert result.confidence >= 0.6


@pytest.mark.parametrize("top_k", [0, -1, 101, True, "5"])
def test_invalid_top_k_is_rejected(top_k):
    with pytest.raises((TypeError, ValueError)):
        make_router().route(CausalState(TaskType.DATA_PROCESSING, "处理数据", input_format="xlsx"), top_k=top_k)


def test_skill_capabilities_and_enabled_survive_index_roundtrip(tmp_path):
    router = CapabilityRouter()
    router.register_skill(SkillEntry("dedup", "Dedup", "去重", task_types=[TaskType.DATA_PROCESSING], input_formats=["csv"], enabled=False, capabilities=["tabular.deduplicate"]))
    path = tmp_path / "index.json"
    router.save_index(path)
    loaded = CapabilityRouter(path)
    assert loaded.skills["dedup"].enabled is False
    assert loaded.skills["dedup"].capabilities == ["tabular.deduplicate"]
