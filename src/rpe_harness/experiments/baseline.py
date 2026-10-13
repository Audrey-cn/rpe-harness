"""基线测试——测量当前 Hermes 技能检索性能。

在做任何改造之前，先建立参照数据。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..capability import CapabilityRouter, SkillEntry
from ..mcsi import CausalState, TaskType


@dataclass
class BaselineResult:
    """单次基线测试结果。"""

    task_id: str
    expected_skill: str
    found_skill: str | None
    latency_ms: float
    tokens_used: int
    success: bool
    level: str


@dataclass
class BaselineReport:
    """基线测试汇总报告。"""

    total_tasks: int = 0
    correct_routing: int = 0
    avg_latency_ms: float = 0.0
    avg_tokens: float = 0.0
    cold_skill_recovery: int = 0
    cold_skill_total: int = 0
    results: list[BaselineResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct_routing / self.total_tasks if self.total_tasks else 0.0

    @property
    def cold_recovery_rate(self) -> float:
        return self.cold_skill_recovery / self.cold_skill_total if self.cold_skill_total else 0.0

    def to_dict(self) -> dict:
        return {
            "total_tasks": self.total_tasks,
            "correct_routing": self.correct_routing,
            "accuracy": round(self.accuracy, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "avg_tokens": round(self.avg_tokens, 1),
            "cold_skill_recovery_rate": round(self.cold_recovery_rate, 4),
        }


def load_hermes_skills(skills_dir: Path) -> list[SkillEntry]:
    """从 Hermes 技能目录加载技能元数据。"""
    entries = []
    for skill_file in skills_dir.rglob("SKILL.md"):
        content = skill_file.read_text()[:2000]  # 只读前 2000 字符
        # 简单解析 frontmatter
        name = skill_file.parent.name
        description = ""
        if content.startswith("---"):
            try:
                fm_end = content.index("---", 3)
                fm = content[3:fm_end]
                for line in fm.split("\n"):
                    if line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip('"')
            except ValueError:
                pass

        entries.append(SkillEntry(
            skill_id=name,
            name=name,
            description=description[:200],
            task_types=[TaskType.UNKNOWN],  # 初始为未知，需标注
            path=str(skill_file),
        ))
    return entries


def run_baseline(
    skills_dir: Path,
    test_tasks: list[dict] | None = None,
) -> BaselineReport:
    """运行基线测试。"""
    router = CapabilityRouter()
    skills = load_hermes_skills(skills_dir)
    for skill in skills:
        router.register_skill(skill)

    if test_tasks is None:
        test_tasks = _generate_default_tasks()

    report = BaselineReport()
    latencies = []
    tokens_list = []

    for task in test_tasks:
        state = CausalState(
            task_type=TaskType(task.get("task_type", "unknown")),
            goal=task.get("goal", ""),
            constraints=task.get("constraints", []),
            input_format=task.get("input_format"),
            required_capability=task.get("required_capability"),
        )

        result = router.route(state, top_k=3)
        expected = task.get("expected_skill", "")
        found = result.candidates[0].skill_id if result.candidates else None

        r = BaselineResult(
            task_id=task.get("id", ""),
            expected_skill=expected,
            found_skill=found,
            latency_ms=result.latency_ms,
            tokens_used=result.tokens_used,
            success=(found == expected),
            level=result.level.name,
        )
        report.results.append(r)
        report.total_tasks += 1
        if r.success:
            report.correct_routing += 1
        latencies.append(r.latency_ms)
        tokens_list.append(r.tokens_used)

        if task.get("is_cold", False):
            report.cold_skill_total += 1
            if r.success:
                report.cold_skill_recovery += 1

    report.avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0
    report.avg_tokens = sum(tokens_list) / len(tokens_list) if tokens_list else 0

    return report


def _generate_default_tasks() -> list[dict]:
    """生成默认测试任务集。"""
    return [
        {"id": "t01", "task_type": "file.operation", "goal": "读取 Excel 文件", "input_format": "xlsx", "expected_skill": "xlsx", "is_cold": False},
        {"id": "t02", "task_type": "code.generation", "goal": "写 Python 脚本", "input_format": "python", "expected_skill": "python-debugpy", "is_cold": False},
        {"id": "t03", "task_type": "web.research", "goal": "搜索网页信息", "expected_skill": "search-layer", "is_cold": False},
        {"id": "t04", "task_type": "document.writing", "goal": "生成 Word 文档", "input_format": "docx", "expected_skill": "docx", "is_cold": True},
        {"id": "t05", "task_type": "data.processing", "goal": "数据分析", "input_format": "csv", "expected_skill": "xlsx", "is_cold": False},
    ]


if __name__ == "__main__":
    import sys

    skills_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".hermes" / "skills"
    report = run_baseline(skills_dir)
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
