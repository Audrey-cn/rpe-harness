"""Independent, fixed-label routing checks for the basic local harness.

The tasks below are hand authored. Their labels are not inferred from router scores,
skill tokens, skill descriptions, or synthesized usage history. This is a routing
microbenchmark against a small fixture catalog, not a Hermes benchmark or file
execution test. Run file execution/verification acceptance separately.

    python -m rpe_harness.experiments.basic_validation --report /tmp/basic.json
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import statistics
import time
from typing import Any

from ..capability import CapabilityRouter, SkillEntry
from ..mcsi import CausalState, TaskType


@dataclass(frozen=True)
class ValidationCase:
    task_id: str
    state: CausalState
    expected_skill: str | None
    category: str


def _fixture_skills() -> list[SkillEntry]:
    """A manual catalog; no user index, configuration, or credential files are read."""
    return [
        SkillEntry(
            skill_id="builtin.csv.deduplicate",
            name="CSV duplicate remover",
            description="Remove repeated records while preserving their first occurrence.",
            task_types=[TaskType.DATA_PROCESSING],
            input_formats=["csv"],
            capabilities=["tabular.deduplicate"],
        ),
        SkillEntry(
            skill_id="builtin.xlsx.deduplicate",
            name="Workbook duplicate remover",
            description="Remove repeated records from a workbook without shifting its rows.",
            task_types=[TaskType.DATA_PROCESSING],
            input_formats=["xlsx"],
            capabilities=["tabular.deduplicate"],
        ),
        SkillEntry(
            skill_id="fixture.document.export",
            name="Approved document exporter",
            description="Export an already approved draft as a document.",
            task_types=[TaskType.DOCUMENT_WRITING],
            input_formats=["docx"],
            capabilities=["document.export"],
            preconditions=["draft_approved"],
        ),
        SkillEntry(
            skill_id="fixture.image.render",
            name="Image thumbnail renderer",
            description="Render a small image preview.",
            task_types=[TaskType.FILE_OPERATION],
            input_formats=["png"],
            capabilities=["image.thumbnail"],
            enabled=False,
        ),
        SkillEntry(
            skill_id="fixture.archive.unpack",
            name="Archive unpacker",
            description="Extract the members of a compressed archive.",
            task_types=[TaskType.FILE_OPERATION],
            input_formats=["zip"],
            capabilities=["archive.extract"],
            is_dormant=True,
        ),
    ]


def _fixed_cases() -> list[ValidationCase]:
    """Literal tasks and independently assigned literal labels, kept in a stable order."""
    return [
        ValidationCase(
            "csv-first-record",
            CausalState(
                TaskType.DATA_PROCESSING,
                "Keep the earliest customer record for each email address in this export.",
                input_format="csv", required_capability="tabular.deduplicate",
            ),
            "builtin.csv.deduplicate", "positive",
        ),
        ValidationCase(
            "xlsx-first-record",
            CausalState(
                TaskType.DATA_PROCESSING,
                "Remove later repeated order numbers from this workbook, keeping the first row.",
                input_format="xlsx", required_capability="tabular.deduplicate",
            ),
            "builtin.xlsx.deduplicate", "positive",
        ),
        ValidationCase(
            "approved-document",
            CausalState(
                TaskType.DOCUMENT_WRITING,
                "Export the signed-off meeting draft for the team.",
                input_format="docx", required_capability="document.export",
                facts=["draft_approved"],
            ),
            "fixture.document.export", "positive",
        ),
        ValidationCase(
            "unknown-request",
            CausalState(TaskType.UNKNOWN, "Organise the material for tomorrow."),
            None, "unknown",
        ),
        ValidationCase(
            "unsupported-format",
            CausalState(
                TaskType.DATA_PROCESSING,
                "Discard later repeated account numbers in this database export.",
                input_format="parquet", required_capability="tabular.deduplicate",
            ),
            None, "format_mismatch",
        ),
        ValidationCase(
            "unsupported-capability",
            CausalState(
                TaskType.DATA_PROCESSING,
                "Translate the column headings into French.",
                input_format="csv", required_capability="tabular.translate",
            ),
            None, "capability_mismatch",
        ),
        ValidationCase(
            "disabled-skill",
            CausalState(
                TaskType.FILE_OPERATION,
                "Make a small preview of the product photograph.",
                input_format="png", required_capability="image.thumbnail",
            ),
            None, "disabled",
        ),
        ValidationCase(
            "dormant-skill",
            CausalState(
                TaskType.FILE_OPERATION,
                "Extract the supplied project archive.",
                input_format="zip", required_capability="archive.extract",
            ),
            None, "dormant",
        ),
        ValidationCase(
            "missing-fact",
            CausalState(
                TaskType.DOCUMENT_WRITING,
                "Export the meeting draft after approval has been confirmed.",
                input_format="docx", required_capability="document.export",
                constraints=["draft_approved"], facts=[],
            ),
            None, "missing_precondition_fact",
        ),
        ValidationCase(
            "insufficient-signal",
            CausalState(TaskType.DATA_PROCESSING, "What is the next operation?"),
            None, "low_confidence",
        ),
        ValidationCase(
            "unsupported-task",
            CausalState(
                TaskType.WEB_ACADEMIC,
                "Find published evidence about the effects of urban noise on birds.",
                input_format="pdf", required_capability="scholarly.review",
            ),
            None, "task_mismatch",
        ),
        ValidationCase(
            "wrong-task-type",
            CausalState(
                TaskType.FILE_OPERATION,
                "Copy the received attachment into the archive folder.",
                input_format="csv", required_capability="tabular.deduplicate",
            ),
            None, "task_mismatch",
        ),
    ]


def _metadata_baseline(skills: list[SkillEntry], state: CausalState) -> str | None:
    """Simple task-type/format lookup, deliberately unaware of eligibility and facts.

    This local comparator does not call Hermes or reuse CapabilityRouter.matches_state.
    A task-type match scores one and a format match scores one; catalog order breaks ties.
    UNKNOWN treats every catalog entry as a candidate. Missing task types abstain.
    """
    eligible = [
        skill for skill in skills
        if state.task_type == TaskType.UNKNOWN or state.task_type in skill.task_types
    ]
    if not eligible:
        return None
    selected = max(
        eligible,
        key=lambda skill: int(state.task_type in skill.task_types)
        + int(bool(state.input_format) and state.input_format in skill.input_formats),
    )
    return selected.skill_id


def _latency_summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    # Nearest-rank p95, with its definition recorded in the report.
    p95_index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return {
        "mean": statistics.fmean(values) if values else 0.0,
        "p50": statistics.median(values) if values else 0.0,
        "p95": ordered[p95_index] if values else 0.0,
        "max": max(values, default=0.0),
    }


def _metrics(rows: list[dict[str, Any]], *, baseline: bool = False) -> dict[str, Any]:
    prediction_key = "baseline_top1" if baseline else "router_top1"
    correct_key = "baseline_correct" if baseline else "correct"
    latency_key = "baseline_latency_ms" if baseline else "router_latency_ms"
    positive_count = sum(row["expected_skill"] is not None for row in rows)
    negative_count = len(rows) - positive_count
    selected_count = sum(row[prediction_key] is not None for row in rows)
    correct_positive = sum(
        row[correct_key] and row["expected_skill"] is not None for row in rows
    )
    correct_rejections = sum(
        row[correct_key] and row["expected_skill"] is None for row in rows
    )
    correct = sum(row[correct_key] for row in rows)
    return {
        "sample_count": len(rows),
        "correct_tasks": correct,
        "accuracy": correct / len(rows) if rows else 0.0,
        "top1_accuracy": correct_positive / positive_count if positive_count else 0.0,
        "precision": correct_positive / selected_count if selected_count else 0.0,
        "selected_count": selected_count,
        "correct_positive_routes": correct_positive,
        "correct_rejections": correct_rejections,
        "rejection_count": len(rows) - selected_count,
        "rejection_recall": correct_rejections / negative_count if negative_count else 0.0,
        "false_accept_count": sum(
            row["expected_skill"] is None and row[prediction_key] is not None for row in rows
        ),
        "latency_ms": _latency_summary([row[latency_key] for row in rows]),
    }


def run_basic_validation(output: Path | None = None) -> dict[str, Any]:
    """Evaluate real routing on fixed truth; optionally save its inspectable JSON report."""
    skills = _fixture_skills()
    router = CapabilityRouter()
    for skill in skills:
        router.register_skill(skill)

    cases = _fixed_cases()
    rows = []
    for case in cases:
        result = router.route(case.state, top_k=3)
        top1 = result.candidates[0].skill_id if result.candidates else None
        start = time.perf_counter()
        baseline_top1 = _metadata_baseline(skills, case.state)
        baseline_latency = (time.perf_counter() - start) * 1000
        # A rejection is correct only when the router also requests escalation.
        correct = top1 == case.expected_skill and (
            case.expected_skill is not None or result.fallback_to_llm
        )
        rows.append({
            "task_id": case.task_id,
            "category": case.category,
            "state": {
                "task_type": case.state.task_type.value,
                "goal": case.state.goal,
                "input_format": case.state.input_format,
                "required_capability": case.state.required_capability,
                "constraints": list(case.state.constraints),
                "facts": list(case.state.facts),
            },
            "expected_skill": case.expected_skill,
            "router_top1": top1,
            "router_candidates": [skill.skill_id for skill in result.candidates],
            "router_level": result.level.name,
            "router_requires_reasoning": result.fallback_to_llm,
            "router_latency_ms": result.latency_ms,
            "correct": bool(correct),
            "baseline_top1": baseline_top1,
            "baseline_latency_ms": baseline_latency,
            "baseline_correct": baseline_top1 == case.expected_skill,
        })

    report = {
        "experiment": "independent_basic_routing_validation",
        "scope": "routing_microbenchmark_only",
        "task_source": "hand-authored-fixed-v1",
        "catalog_source": "manual builtin-shaped and fixture SkillEntry objects",
        "sample_count": len(rows),
        "positive_samples": sum(case.expected_skill is not None for case in cases),
        "rejection_samples": sum(case.expected_skill is None for case in cases),
        "metric_definitions": {
            "accuracy": "correct routes plus correct rejections / all tasks",
            "top1_accuracy": "correct top-1 routes / positive tasks",
            "precision": "correct positive routes / non-empty top-1 predictions",
            "rejection_recall": "correct rejections / rejection tasks",
            "latency_ms": "one local call per fixed task; excludes catalog construction and IO",
            "p95": "nearest-rank percentile",
        },
        "router": _metrics(rows),
        "baseline": {
            "name": "simple_metadata_task_type_and_format",
            "is_native_hermes": False,
            "description": "Local metadata lookup; ignores capabilities, facts, disabled and dormant state.",
            **_metrics(rows, baseline=True),
        },
        "file_execution": {
            "evaluated": False,
            "note": "This report tests routing only. CSV/XLSX file acceptance is measured separately.",
        },
        "claims": {
            "real_token_savings_measured": False,
            "automatic_learning_evaluated": False,
            "native_hermes_baseline_measured": False,
        },
        "limitations": [
            "Small manually chosen fixture catalog; no production-distribution accuracy claim.",
            "One routing call per task is a smoke microbenchmark, not stable performance evidence.",
            "No LLM, Hermes host, execution handler, or file verifier is invoked.",
            "No learning, cold-skill recovery, API cost, or token-saving gain is established.",
        ],
        "tasks": rows,
        "passed": all(row["correct"] for row in rows),
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed-label routing validation; no model calls.")
    parser.add_argument("--report", type=Path, help="Optional JSON evidence output path")
    args = parser.parse_args()
    report = run_basic_validation(args.report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
