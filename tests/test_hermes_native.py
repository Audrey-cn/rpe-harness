"""Controlled native-host experiment contracts; no model or user-home access."""
from __future__ import annotations

import csv
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook


def experiment():
    assert importlib.util.find_spec("rpe_harness.experiments.hermes_native") is not None, (
        "the genuine native-host comparison experiment is missing"
    )
    return importlib.import_module("rpe_harness.experiments.hermes_native")


def result(*, rejected=False, reason="", events=None):
    return {
        "exit_code": 0, "completed": True, "session_id": "test-session",
        "final_response": json.dumps({"status": "rejected" if rejected else "succeeded",
                                      "reason": reason}),
        "elapsed_seconds": 1.25,
        "events": events if events is not None else [
            {"kind": "post_tool_call", "tool_name": "terminal", "tool_call_id": "t1"},
            {"kind": "post_api_request", "api_request_id": "a1", "usage": None},
        ],
        "usage": {"input_tokens": None, "output_tokens": None, "cache_read_tokens": None,
                  "total_tokens": None, "reported_requests": 0, "missing_requests": 1},
        "stream_tool_calls": [{"name": "terminal", "input": {"command": "python3 -c 'import csv'"}}],
    }


def workspace_for(tmp_path, case):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / case["input_path"]).write_bytes(Path(case["source_path"]).read_bytes())
    return workspace


def write_csv_output(workspace, rows):
    with (workspace / "output.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)


CSV_FIRST_TRUTH = [
    ["编号", "名称", "备注"],
    ["001", "首条", "中文原值"],
    ["01", "独立编号", "前导零不同"],
    ["002", "最后首条", "完整记录"],
]


def test_cases_are_manual_and_have_real_byte_hashes_and_relative_paths(tmp_path):
    cases = experiment().build_cases(tmp_path)
    assert [case["id"] for case in cases] == [
        "csv_chinese", "csv_multiline", "xlsx_typed_keys", "xlsx_formulas",
        "reject_json", "reject_formula_dependency",
    ]
    assert [case["expected"] for case in cases] == ["success"] * 4 + ["rejected"] * 2
    assert cases[0]["expected_rows"] == CSV_FIRST_TRUTH
    assert cases[2]["retained_rows"] == [2, 3, 4, 6]
    assert cases[2]["removed_rows"] == [5, 7]
    for case in cases:
        assert not Path(case["input_path"]).is_absolute()
        assert not Path(case["output_path"]).is_absolute()
        assert case["source_sha256"] == hashlib.sha256(Path(case["source_path"]).read_bytes()).hexdigest()


def test_both_arms_have_explicit_dependency_ready_python_instruction(tmp_path):
    for case in experiment().build_cases(tmp_path):
        assert '"$RPE_HARNESS_PYTHON"' in case["prompt"]
        assert "已提供 csv/openpyxl" in case["prompt"]
        assert "无需安装依赖" in case["prompt"]


@pytest.mark.parametrize("command, independent", [
    ('"$RPE_HARNESS_PYTHON" -c "import openpyxl"', True),
    ('"${RPE_HARNESS_PYTHON}" -c "import csv"', True),
    ('echo "RPE_HARNESS_PYTHON=$RPE_HARNESS_PYTHON"', True),
    ('"$RPE_HARNESS_PYTHON" -c "import rpe_harness.execution"', False),
    ('"$RPE_HARNESS_PYTHON_EVALUATOR" -c "import csv"', False),
])
def test_interpreter_variable_is_not_an_implementation_import(command, independent):
    verdict = experiment()._tool_audit([{"name": "terminal", "input": {"command": command}}])
    assert verdict["native_independent"] is independent
    assert verdict["tool_input_sha256"] == [hashlib.sha256(json.dumps(
        {"command": command}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()]


def test_external_validation_accepts_manual_csv_without_using_writer(tmp_path, monkeypatch):
    case = experiment().build_cases(tmp_path / "fixtures")[0]
    workspace = workspace_for(tmp_path, case)
    write_csv_output(workspace, CSV_FIRST_TRUTH)
    import rpe_harness.execution as execution
    monkeypatch.setattr(execution, "prepare_deduplication", lambda *a, **k: pytest.fail("writer invoked"))
    verdict = experiment().validate_case(case, workspace, result())
    assert verdict["passed"] is True
    assert verdict["verification"]["passed"] is True
    assert verdict["manual_truth_ok"] is True


def test_wrong_nonkey_first_record_is_not_a_success(tmp_path):
    case = experiment().build_cases(tmp_path / "fixtures")[0]
    workspace = workspace_for(tmp_path, case)
    wrong = [row[:] for row in CSV_FIRST_TRUTH]
    wrong[1][1] = "重复版本"
    write_csv_output(workspace, wrong)
    assert experiment().validate_case(case, workspace, result())["passed"] is False


def test_manual_truth_disagrees_even_when_recomputed_oracle_passes(tmp_path):
    case = experiment().build_cases(tmp_path / "fixtures")[0]
    workspace = workspace_for(tmp_path, case)
    write_csv_output(workspace, CSV_FIRST_TRUTH)
    case["expected_rows"] = [["incorrect manual answer"]]
    verdict = experiment().validate_case(case, workspace, result())
    assert verdict["verification"]["passed"] is True
    assert verdict["manual_truth_ok"] is False
    assert verdict["passed"] is False


@pytest.mark.parametrize("corruption", ["missing", "source_changed", "output_symlink", "no_events"])
def test_missing_output_changed_source_or_unowned_output_fails(tmp_path, corruption):
    case = experiment().build_cases(tmp_path / "fixtures")[0]
    workspace = workspace_for(tmp_path, case)
    host_result = result()
    if corruption != "missing":
        write_csv_output(workspace, CSV_FIRST_TRUTH)
    if corruption == "source_changed":
        (workspace / "input.csv").write_text("changed")
    elif corruption == "output_symlink":
        outside = tmp_path / "outside.csv"
        (workspace / "output.csv").rename(outside)
        (workspace / "output.csv").symlink_to(outside)
    elif corruption == "no_events":
        host_result["events"] = []
    assert experiment().validate_case(case, workspace, host_result)["passed"] is False


def test_xlsx_formula_and_other_sheet_truth_is_checked(tmp_path):
    case = experiment().build_cases(tmp_path / "fixtures")[3]
    workspace = workspace_for(tmp_path, case)
    workbook = load_workbook(workspace / "input.xlsx", data_only=False)
    sheet = workbook["订单"]
    for row in (3, 5):
        for cell in sheet[row]:
            cell.value, cell.comment, cell.hyperlink = None, None, None
    workbook.save(workspace / "output.xlsx")
    workbook.close()
    assert experiment().validate_case(case, workspace, result())["passed"] is True
    workbook = load_workbook(workspace / "output.xlsx", data_only=False)
    workbook["订单"]["D4"] = "=C3*2"
    workbook.save(workspace / "output.xlsx")
    workbook.close()
    assert experiment().validate_case(case, workspace, result())["passed"] is False


@pytest.mark.parametrize("failure", ["wrong_reason", "not_json", "artifact_present"])
def test_rejection_needs_explicit_json_reason_and_no_output(tmp_path, failure):
    case = experiment().build_cases(tmp_path / "fixtures")[4]
    workspace = workspace_for(tmp_path, case)
    host_result = result(rejected=True, reason="unsupported format: only CSV/XLSX are supported")
    assert experiment().validate_case(case, workspace, host_result)["passed"] is True
    if failure == "wrong_reason":
        host_result["final_response"] = '{"status":"rejected","reason":"API quota exhausted"}'
    elif failure == "not_json":
        host_result["final_response"] = "I refused the unsupported format."
    else:
        (workspace / "output.json").write_text("[]")
    assert experiment().validate_case(case, workspace, host_result)["passed"] is False


def test_integrated_claim_without_linked_durable_run_does_not_pass(tmp_path):
    case = experiment().build_cases(tmp_path / "fixtures")[0]
    workspace = workspace_for(tmp_path, case)
    write_csv_output(workspace, CSV_FIRST_TRUTH)
    case.update(tool_enabled=True, db_path=str(tmp_path / "missing.sqlite"))
    host_result = result(events=[{"kind": "post_tool_call", "tool_name": "rpe_task", "run_id": "invented"}])
    verdict = experiment().validate_case(case, workspace, host_result)
    assert verdict["passed"] is False
    assert verdict["linked_run_verified"] is False


def test_integrated_success_needs_actual_durable_verification_and_artifact_hash(tmp_path):
    from rpe_harness.runtime import HarnessRuntime
    import sqlite3

    case = experiment().build_cases(tmp_path / "fixtures")[0]
    workspace = workspace_for(tmp_path, case)
    database = tmp_path / "runs.sqlite"
    runtime = HarnessRuntime(db_path=database)
    run = runtime.run({
        "schema_version": 1, "event_id": "real-tool-call", "session_id": "test-session",
        "state": {"task_type": "data.processing", "goal": "按编号保留首条", "input_format": "csv",
                  "required_capability": "tabular.deduplicate",
                  "constraints": ["preserve_original", "preserve_order"]},
        "execution": {"input_path": str(workspace / "input.csv"),
                      "output_path": str(workspace / "output.csv"), "key_columns": ["编号"]},
    })
    case.update(tool_enabled=True, db_path=str(database))
    host_result = result(events=[{"kind": "post_tool_call", "tool_name": "rpe_task", "run_id": run["run_id"]}])
    assert experiment().validate_case(case, workspace, host_result)["passed"] is True
    with sqlite3.connect(database) as db:
        db.execute("UPDATE runs SET verification_json=? WHERE run_id=?",
                   (json.dumps({"passed": False}), run["run_id"]))
    verdict = experiment().validate_case(case, workspace, host_result)
    assert verdict["verification"]["passed"] is True
    assert verdict["linked_run_verified"] is False
    assert verdict["passed"] is False


def test_all_six_manual_fixtures_match_current_runtime_acceptance_contract(tmp_path):
    from rpe_harness.runtime import HarnessRuntime

    for case in experiment().build_cases(tmp_path / "fixtures"):
        workspace = tmp_path / case["id"]
        workspace.mkdir()
        (workspace / case["input_path"]).write_bytes(Path(case["source_path"]).read_bytes())
        database = workspace / "events.sqlite"
        runtime = HarnessRuntime(db_path=database)
        execution = {"input_path": str(workspace / case["input_path"]),
                     "output_path": str(workspace / case["output_path"]),
                     "key_columns": case["key_columns"]}
        if case.get("sheet"):
            execution["sheet"] = case["sheet"]
        run = runtime.run({
            "schema_version": 1, "event_id": case["id"], "session_id": "test-session",
            "state": {"task_type": "data.processing", "goal": "按指定列保留首条",
                      "input_format": case["format"], "required_capability": "tabular.deduplicate",
                      "constraints": ["preserve_original", "preserve_order"]
                                     + (["preserve_formulas"] if case["format"] == "xlsx" else [])},
            "execution": execution,
        })
        expected_status = "succeeded" if case["expected"] == "success" else "rejected"
        assert run["status"] == expected_status, case["id"]
        final = result(rejected=expected_status == "rejected",
                       reason=(run.get("error") or {}).get("message", ""),
                       events=[{"kind": "post_tool_call", "tool_name": "rpe_task", "run_id": run["run_id"]}])
        actual_case = {**case, "tool_enabled": True, "db_path": str(database)}
        assert experiment().validate_case(actual_case, workspace, final)["passed"] is True, case["id"]


def test_native_observed_harness_import_fails_even_with_correct_output(tmp_path):
    case = experiment().build_cases(tmp_path / "fixtures")[0]
    workspace = workspace_for(tmp_path, case)
    write_csv_output(workspace, CSV_FIRST_TRUTH)
    host_result = result()
    host_result["stream_tool_calls"] = [{"name": "terminal", "input": {
        "command": "python3 -c 'from rpe_harness.execution import prepare_deduplication'"}}]
    verdict = experiment().validate_case(case, workspace, host_result)
    assert verdict["native_independent"] is False
    assert verdict["passed"] is False


def test_timeout_cannot_be_accepted_despite_a_complete_artifact(tmp_path):
    case = experiment().build_cases(tmp_path / "fixtures")[0]
    workspace = workspace_for(tmp_path, case)
    write_csv_output(workspace, CSV_FIRST_TRUTH)
    host_result = {**result(), "timed_out": True}
    assert experiment().validate_case(case, workspace, host_result)["passed"] is False


def test_usage_coverage_model_identity_and_command_hashes_survive_report(tmp_path):
    def recorded_host(*args, **kwargs):
        return {**result(), "exit_code": 2, "completed": False,
                "usage": {"api_requests": 2, "requests_with_usage": 1, "coverage_complete": False,
                          "known_total_tokens": 88, "total_tokens": None, "cache_read_tokens": None},
                "model_identity": {"provider": "xiaomi", "requested_model": "same-model",
                                   "response_models": ["same-model"]}}

    report = experiment().run_comparison(tmp_path / "owned", tmp_path / "report.json", recorded_host)
    first = report["attempts"][0]
    assert first["usage"]["api_requests"] == 2
    assert first["usage"]["requests_with_usage"] == 1
    assert first["usage"]["coverage_complete"] is False
    assert first["usage"]["known_total_tokens"] == 88
    assert first["model_identity"]["requested_model"] == "same-model"
    assert first["tool_counts"] == {"terminal": 1}
    assert len(first["tool_input_sha256"]) == 1
    assert "python3 -c" not in json.dumps(report)
    assert all(pair["model_identity_consistent"] is True for pair in report["pairs"])


def test_model_mismatch_is_recorded_and_not_hidden_by_matching_prompt(tmp_path):
    def different_host(*args, **kwargs):
        served = "model-b" if kwargs["tool_enabled"] else "model-a"
        return {**result(), "completed": False, "exit_code": 2,
                "model_identity": {"provider": "xiaomi", "requested_model": "same-model",
                                   "response_models": [served]}}

    report = experiment().run_comparison(tmp_path / "owned", tmp_path / "report.json", different_host)
    assert len(report["pairs"]) == 6
    assert all(pair["model_identity_consistent"] is False for pair in report["pairs"])


def test_process_cleanup_and_api_error_metadata_are_reported_without_private_message(tmp_path):
    def observed_host(*args, **kwargs):
        return {**result(), "completed": False, "exit_code": 2,
                "process_cleanup": {"tracked_descendants": [1234], "signaled_descendants": [1234],
                                    "remaining_descendants": []},
                "events": [{"kind": "api_request_error", "api_request_id": "failed-a1",
                            "status_code": 503, "retry_count": 1,
                            "error": {"type": "ConnectionError", "code": "upstream_unavailable",
                                      "message": "PRIVATE ERROR BODY"}}]}

    report = experiment().run_comparison(tmp_path / "owned", tmp_path / "report.json", observed_host)
    first = report["attempts"][0]
    assert first["process_cleanup"]["remaining_descendants"] == []
    assert first["process_cleanup"]["signaled_descendants"] == [1234]
    assert first["api_error_events"] == 1
    assert first["events"][0]["status_code"] == 503
    assert first["events"][0]["error"] == {"type": "ConnectionError", "code": "upstream_unavailable"}
    assert "PRIVATE ERROR" not in json.dumps(report)


def test_remaining_descendant_prevents_claim_of_completed_task(tmp_path):
    case = experiment().build_cases(tmp_path / "fixtures")[0]
    workspace = workspace_for(tmp_path, case)
    write_csv_output(workspace, CSV_FIRST_TRUTH)
    host_result = {**result(), "process_cleanup": {"tracked_descendants": [1234],
                    "signaled_descendants": [1234], "remaining_descendants": [1234]}}
    assert experiment().validate_case(case, workspace, host_result)["passed"] is False


def test_run_comparison_keeps_all_attempts_unknown_usage_and_private_text_out(tmp_path):
    def unavailable_host(prompt, workspace, db_path, *, tool_enabled, owner_root, **kwargs):
        assert Path(workspace).is_relative_to(owner_root)
        assert Path(db_path).is_relative_to(owner_root)
        return {**result(), "exit_code": 2, "completed": False,
                "final_response": "PRIVATE MODEL TRANSCRIPT MUST NOT BE SAVED"}

    path = tmp_path / "report.json"
    report = experiment().run_comparison(tmp_path / "owned", path, unavailable_host)
    assert len(report["attempts"]) == 12
    assert [(a["case_id"], a["arm"]) for a in report["attempts"][:4]] == [
        ("csv_chinese", "native"), ("csv_chinese", "integrated"),
        ("csv_multiline", "integrated"), ("csv_multiline", "native"),
    ]
    assert report["native_hermes_baseline_measured"] is False
    assert all(a["usage"]["cache_read_tokens"] is None for a in report["attempts"])
    for first, second in zip(report["attempts"][::2], report["attempts"][1::2]):
        assert first["prompt_sha256"] == second["prompt_sha256"]
        assert first["input_sha256"] == second["input_sha256"]
    saved = path.read_text()
    assert "PRIVATE MODEL" not in saved
    assert json.loads(saved)["attempts"] == report["attempts"]


def test_runner_exception_becomes_an_attempt_and_other_tasks_continue(tmp_path):
    def failing_host(*args, **kwargs):
        raise RuntimeError("PRIVATE traceback content")

    report = experiment().run_comparison(tmp_path / "owned", tmp_path / "report.json", failing_host)
    assert len(report["attempts"]) == 12
    assert all(a["validation"]["passed"] is False for a in report["attempts"])
    assert "PRIVATE" not in json.dumps(report)
