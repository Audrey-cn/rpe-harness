"""Hand-labelled routing counterexamples, independent of skill metadata generation."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from rpe_harness.capability import CapabilityRouter, SkillEntry
from rpe_harness.mcsi import CausalState, TaskType


@pytest.mark.parametrize(
    "state",
    [
        CausalState(TaskType.UNKNOWN, "Organise the material for tomorrow."),
        CausalState(
            TaskType.DATA_PROCESSING,
            "Remove repeated customer records from a database export.",
            input_format="parquet",
        ),
        CausalState(
            TaskType.DATA_PROCESSING,
            "Translate column names in a customer table.",
            input_format="csv",
            required_capability="tabular.translate",
        ),
    ],
    ids=["unknown-is-not-known", "unsupported-format", "unsupported-capability"],
)
def test_hand_labelled_rejection_counterexamples(state):
    """Returning the same-type skill despite a hard mismatch must fail this test."""
    router = CapabilityRouter()
    router.register_skill(SkillEntry(
        skill_id="builtin.csv.deduplicate",
        name="CSV duplicate remover",
        description="Remove repeated records while preserving their first occurrence.",
        task_types=[TaskType.DATA_PROCESSING],
        input_formats=["csv"],
        tags=["tabular.deduplicate"],
    ))

    result = router.route(state)

    assert result.candidates == []
    assert result.fallback_to_llm is True


def test_report_matches_literal_ground_truth_and_honest_baseline():
    """A wrong route, inverted rejection, or leaked-label evaluator must be visible."""
    from rpe_harness.experiments.basic_validation import run_basic_validation

    report = run_basic_validation()

    expected = {
        "csv-first-record": "builtin.csv.deduplicate",
        "xlsx-first-record": "builtin.xlsx.deduplicate",
        "approved-document": "fixture.document.export",
        "unknown-request": None,
        "unsupported-format": None,
        "unsupported-capability": None,
        "disabled-skill": None,
        "dormant-skill": None,
        "missing-fact": None,
        "insufficient-signal": None,
        "unsupported-task": None,
        "wrong-task-type": None,
    }
    assert report["sample_count"] == 12
    assert report["positive_samples"] == 3
    assert report["rejection_samples"] == 9
    assert {row["task_id"]: row["expected_skill"] for row in report["tasks"]} == expected
    assert {row["task_id"]: row["router_top1"] for row in report["tasks"]} == expected
    for row in report["tasks"]:
        assert row["correct"] is True
        if row["expected_skill"] is not None:
            assert row["expected_skill"] not in row["state"]["goal"]
        assert row["router_latency_ms"] >= 0
    assert report["router"]["correct_tasks"] == 12
    assert report["router"]["accuracy"] == 1.0
    assert report["router"]["top1_accuracy"] == 1.0
    assert report["router"]["precision"] == 1.0
    assert report["router"]["correct_rejections"] == 9
    assert report["router"]["rejection_recall"] == 1.0
    assert report["router"]["false_accept_count"] == 0
    assert report["baseline"]["correct_tasks"] == 4
    assert report["baseline"]["accuracy"] == pytest.approx(4 / 12)
    assert report["baseline"]["precision"] == pytest.approx(3 / 11)
    assert report["baseline"]["is_native_hermes"] is False
    assert report["scope"] == "routing_microbenchmark_only"
    assert report["file_execution"]["evaluated"] is False
    assert report["claims"]["real_token_savings_measured"] is False
    assert report["claims"]["automatic_learning_evaluated"] is False
    assert report["passed"] is True


def test_report_persists_same_fixed_cases_without_mutating_local_data(tmp_path):
    """Dropped JSON output or nondeterministic task labels must fail this contract."""
    from rpe_harness.experiments.basic_validation import run_basic_validation

    path = tmp_path / "evidence" / "basic.json"
    report = run_basic_validation(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    second = run_basic_validation()

    assert saved == report
    assert [row["task_id"] for row in saved["tasks"]] == [
        row["task_id"] for row in second["tasks"]
    ]
    assert [row["router_top1"] for row in saved["tasks"]] == [
        row["router_top1"] for row in second["tasks"]
    ]
    assert list(tmp_path.iterdir()) == [path.parent]


def test_module_cli_writes_requested_report_and_returns_json(tmp_path):
    """A CLI that discards --report or emits non-JSON output is unusable by the harness."""
    report_path = tmp_path / "cli.json"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    project = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(project / "src")
    result = subprocess.run(
        [sys.executable, "-m", "rpe_harness.experiments.basic_validation",
         "--report", str(report_path)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == json.loads(report_path.read_text(encoding="utf-8"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cli.json"]
