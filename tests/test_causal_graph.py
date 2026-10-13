"""图前置事实、合法引用和独立场景的行为回归。"""

from pathlib import Path
import json
import pytest

from rpe_harness.capability import CapabilityRouter
from rpe_harness.causal_graph import CausalEdge, CausalGraph, SkillProfile
from rpe_harness.mcsi import CausalState, TaskType


def test_preconditions_require_facts_rather_than_user_constraints():
    graph = CausalGraph()
    graph.set_profile(SkillProfile("sheet", preconditions=["file_exists"]))
    state = CausalState(TaskType.FILE_OPERATION, "清理表格", constraints=["file_exists"])
    assert graph.check_preconditions("sheet", state) == (False, ["file_exists"])


def test_single_sufficient_scenario_does_not_need_a_graph_edge():
    graph = CausalGraph()
    graph.set_profile(SkillProfile("sheet", usage_contexts=["spreadsheet duplicate cleaning records"]))
    state = CausalState(TaskType.DATA_PROCESSING, "Remove duplicate records from the spreadsheet")
    found = graph.discover_cold_skills(state, ["sheet"], {"sheet"})
    assert [r["skill_id"] for r in found] == ["sheet"]


def test_one_generic_word_is_not_a_cold_skill_match():
    graph = CausalGraph()
    graph.set_profile(SkillProfile("sheet", usage_contexts=["data analysis records", "data cleaning spreadsheet"]))
    state = CausalState(TaskType.UNKNOWN, "data storage backup")
    assert graph.discover_cold_skills(state, ["sheet"], {"sheet"}) == []


def test_a_graph_path_alone_does_not_trigger_discovery():
    graph = CausalGraph()
    graph.set_profile(SkillProfile("cold"))
    graph.add_edge(CausalEdge("active", "cold", "composes_with"))
    state = CausalState(TaskType.CODE_GENERATION, "实现订单函数")
    assert graph.discover_cold_skills(state, ["active", "cold"], {"cold"}) == []


def test_discovery_cannot_return_an_unregistered_skill():
    graph = CausalGraph()
    graph.set_profile(SkillProfile("ghost", usage_contexts=["spreadsheet duplicate records", "spreadsheet duplicate cleaning"]))
    state = CausalState(TaskType.DATA_PROCESSING, "Remove duplicate records from spreadsheet")
    assert graph.discover_cold_skills(state, ["registered"], {"ghost"}) == []


def test_all_shipped_graph_references_resolve_in_the_skill_index():
    root = Path(__file__).resolve().parents[1]
    graph = CausalGraph.load(root / "data" / "causal_graph.json")
    router = CapabilityRouter(root / "data" / "skill_index.json")
    assert graph.validate_skill_ids(set(router.skills)) == []


def test_unknown_primary_cannot_be_a_skill_chain():
    assert CausalGraph().recommend_chain(CausalState(TaskType.UNKNOWN, "未知任务"), "ghost") == []


def test_load_rejects_a_profile_key_that_disagrees_with_its_id(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text(json.dumps({"profiles": {"sheet": {"skill_id": "wrong"}}}))
    with pytest.raises(ValueError):
        CausalGraph.load(path)
