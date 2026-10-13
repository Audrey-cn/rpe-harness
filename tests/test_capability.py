"""能力路由器测试。"""

from rpe_harness.capability import CapabilityRouter, SkillEntry
from rpe_harness.mcsi import CapabilityLevel, CausalState, TaskType


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
