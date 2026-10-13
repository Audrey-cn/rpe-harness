"""Six controlled, real-host comparisons with external artifact acceptance.

Fixture truth is literal and never derived by the Harness writer. The injected
session runner owns model calls; importing this module opens no database or home.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from rpe_harness.verification import verify_deduplication


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prompt(case: dict[str, Any]) -> str:
    return (
        f"处理当前工作目录的 {case['input_path']}，输出为新的 {case['output_path']}。\n"
        f"按列 {json.dumps(case['key_columns'], ensure_ascii=False)} 的完整值去重，保留首次出现的完整记录及原顺序。"
        "键值不去除空格、不忽略大小写、不强制转数字；保留中文、前导零和所有非键列原值。\n"
        "CSV 使用 utf-8-sig 编码、逗号分隔，正确处理引号和字段内换行。"
        "XLSX 处理订单工作表：只清空重复数据行的值、注释和超链接，不能删除或移动行；"
        "保留其余单元格的原坐标、公式文本、样式、注释和超链接，以及其他工作表和隐藏状态、列宽。\n"
        '执行 Python 时使用环境变量指定的解释器："$RPE_HARNESS_PYTHON"，其中已提供 csv/openpyxl，'
        "无需安装依赖；默认 python/python3 可能是另一环境。\n"
        "原文件不可修改；目标已存在时必须拒绝覆盖。仅支持 CSV/XLSX，其他格式必须拒绝。"
        "合并单元格等复杂对象不支持；若保留公式引用会被清空的重复行，也必须拒绝，不能产生输出。\n"
        "使用当前可用工具完成：若有专门的任务工具可以使用，否则使用原生文件/terminal工具"
        "自行编写并执行 csv/openpyxl 处理。不得通过终端或文件工具读取/导入 Harness 项目实现、"
        "调用 Harness CLI/HTTP 或读取/运行验收器和答案。无需询问。\n"
        "最终仅输出一个 JSON 对象，status 为 succeeded 或 rejected，包含 output_path；"
        "拒绝时还必须有非空 reason，说明具体格式或公式依赖限制。"
    )


def build_cases(root: Path | str) -> list[dict[str, Any]]:
    """Create owned fixtures and explicit, hand-checked expected records/cells."""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import PatternFill

    root = Path(root)
    if root.is_symlink():
        raise ValueError("fixture root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    first_rows = [
        ["编号", "名称", "备注"], ["001", "首条", "中文原值"],
        ["01", "独立编号", "前导零不同"], ["002", "最后首条", "完整记录"],
    ]
    multiline_rows = [
        ["客户", "地区", "备注"], ["李,明", "华东", '首条"引号"\n第二行'],
        ["周华", "华北", "另一区域"], ["李,明", "华北", "复合键不同"],
    ]
    cases: list[dict[str, Any]] = [
        {"id": "csv_chinese", "format": "csv", "key_columns": ["编号"],
         "expected": "success", "expected_rows": first_rows},
        {"id": "csv_multiline", "format": "csv", "key_columns": ["客户", "地区"],
         "expected": "success", "expected_rows": multiline_rows},
        {"id": "xlsx_typed_keys", "format": "xlsx", "key_columns": ["编号", "地区"],
         "expected": "success", "sheet": "订单", "retained_rows": [2, 3, 4, 6],
         "removed_rows": [5, 7], "expected_cells": {"订单": {
             "A2": 1, "B2": "北", "C2": "数字首条", "A3": "1", "B3": "北", "C3": "文字首条",
             "A4": "001", "B4": "北", "C4": "前导零首条", "A6": 1, "B6": "南", "C6": "地区不同",
         }}},
        {"id": "xlsx_formulas", "format": "xlsx", "key_columns": ["编号"],
         "expected": "success", "sheet": "订单", "retained_rows": [2, 4, 6],
         "removed_rows": [3, 5], "expected_cells": {
             "订单": {"A2": "001", "C2": 2, "D2": "=C2*2", "A4": "002", "C4": 3,
                      "D4": "=C4*2", "A6": "003", "C6": 7, "D6": "=SUM(C2,C4)"},
             "说明": {"A1": "只读中文说明", "B2": "=1+2"}},
         "expected_sheet_states": {"订单": "visible", "说明": "hidden"},
         "expected_column_widths": {"订单": {"A": 19}}},
        {"id": "reject_json", "format": "json", "key_columns": ["编号"],
         "expected": "rejected", "expected_reason": "unsupported_format"},
        {"id": "reject_formula_dependency", "format": "xlsx", "key_columns": ["编号"],
         "expected": "rejected", "sheet": "订单", "expected_reason": "formula_dependency"},
    ]
    for case in cases:
        directory = root / case["id"]
        directory.mkdir()
        case.update(input_path=f"input.{case['format']}", output_path=f"output.{case['format']}",
                    encoding="utf-8-sig", delimiter=",")
        source = directory / case["input_path"]
        if case["format"] == "csv":
            if case["id"] == "csv_chinese":
                original = [first_rows[0], first_rows[1], ["001", "重复版本", "不能保留"],
                            first_rows[2], first_rows[3], ["002", "末条重复", "不同备注"]]
            else:
                original = [multiline_rows[0], multiline_rows[1], ["李,明", "华东", "重复不同备注"],
                            multiline_rows[2], multiline_rows[3]]
            with source.open("x", encoding="utf-8-sig", newline="") as handle:
                csv.writer(handle).writerows(original)
        elif case["format"] == "json":
            source.write_text('[{"编号":"001"},{"编号":"001"}]\n', encoding="utf-8")
        else:
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "订单"
            if case["id"] == "xlsx_typed_keys":
                rows = [["编号", "地区", "说明"], [1, "北", "数字首条"], ["1", "北", "文字首条"],
                        ["001", "北", "前导零首条"], ["001", "北", "重复文本"],
                        [1, "南", "地区不同"], [1, "北", "重复数字"]]
            elif case["id"] == "xlsx_formulas":
                rows = [["编号", "名称", "数量", "总额"], ["001", "首条", 2, "=C2*2"],
                        ["001", "重复", 999, "=C3*2"], ["002", "另一首条", 3, "=C4*2"],
                        ["002", "重复", 999, "=C5*2"], ["003", "末条", 7, "=SUM(C2,C4)"]]
            else:
                rows = [["编号", "名称", "数量", "总额"], ["001", "首条", 2, "=C3*2"],
                        ["001", "重复", 99, "=C3*2"]]
            for row in rows:
                sheet.append(row)
            if case["id"] == "xlsx_formulas":
                sheet["B2"].comment = Comment("保留首条注释", "Fixture")
                sheet["B4"].hyperlink = "https://example.test/order/002"
                sheet["A4"].fill = PatternFill("solid", fgColor="00FF00")
                sheet.column_dimensions["A"].width = 19
                other = workbook.create_sheet("说明")
                other["A1"], other["B2"], other.sheet_state = "只读中文说明", "=1+2", "hidden"
            workbook.save(source)
            workbook.close()
        case["source_path"] = str(source)
        case["source_sha256"] = _sha(source.read_bytes())
        case["prompt"] = _prompt(case)
    return cases


def _metadata(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return {**event, **payload} if isinstance(payload, dict) else event


def _owned_path(workspace: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("case path must be relative and contained")
    candidate = workspace / path
    current = workspace
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("case paths must not traverse symlinks")
    if not candidate.resolve().is_relative_to(workspace):
        raise ValueError("case path escaped workspace")
    return candidate


def _manual_truth(case: dict[str, Any], output: Path) -> bool:
    if case["format"] == "csv":
        with output.open(encoding=case["encoding"], newline="") as handle:
            return list(csv.reader(handle, delimiter=case["delimiter"], strict=True)) == case["expected_rows"]
    from openpyxl import load_workbook

    workbook = load_workbook(output, data_only=False, keep_links=False)
    try:
        for sheet_name, cells in case["expected_cells"].items():
            sheet = workbook[sheet_name]
            for coordinate, expected in cells.items():
                actual = sheet[coordinate].value
                if type(actual) is not type(expected) or actual != expected:
                    return False
        for row in case["removed_rows"]:
            if any(cell.value is not None or cell.comment is not None or cell.hyperlink is not None
                   for cell in workbook[case["sheet"]][row]):
                return False
        for sheet_name, state in case.get("expected_sheet_states", {}).items():
            if workbook[sheet_name].sheet_state != state:
                return False
        for sheet_name, columns in case.get("expected_column_widths", {}).items():
            if any(workbook[sheet_name].column_dimensions[column].width != width
                   for column, width in columns.items()):
                return False
        return True
    finally:
        workbook.close()


def _rejection_summary(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        return {}
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    try:
        summary = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return summary if isinstance(summary, dict) else {}


def _reason_matches(case: dict[str, Any], reason: Any) -> bool:
    if not isinstance(reason, str) or not reason.strip():
        return False
    text = reason.lower()
    if case["expected_reason"] == "unsupported_format":
        return "格式" in text or "format" in text or ("csv" in text and "xlsx" in text)
    return ("formula" in text or "公式" in text) and any(
        word in text for word in ("clear", "row", "reference", "depend", "清空", "行", "引用", "依赖")
    )


def _tool_audit(calls: Any) -> dict[str, Any]:
    """Audit observed inputs; retain hashes, never commands or file contents."""
    if not isinstance(calls, list):
        return {"native_independent": None, "tool_counts": {}, "tool_input_sha256": []}
    counts: dict[str, int] = {}
    hashes = []
    independent = True
    allowed = {"terminal", "process_manage", "read_file", "write_file", "patch", "search_files"}
    forbidden = ("rpe_harness", "rpe-harness", "verification.py", "hermes_native.py")
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            independent = False
            continue
        name = call["name"]
        counts[name] = counts.get(name, 0) + 1
        raw = json.dumps(call.get("input"), ensure_ascii=False, sort_keys=True, default=str)
        hashes.append(_sha(raw.encode()))
        # This exact, case-sensitive environment identifier (including echoed
        # labels) grants no implementation access. Keep the original input hash.
        audited = re.sub(r"\bRPE_HARNESS_PYTHON\b", "COMPATIBLE_PYTHON", raw)
        if name not in allowed or any(marker in audited.lower() for marker in forbidden):
            independent = False
    return {"native_independent": independent, "tool_counts": counts, "tool_input_sha256": hashes}


def _process_cleanup(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    evidence = {}
    for key in ("tracked_descendants", "signaled_descendants", "remaining_descendants"):
        item = value.get(key)
        if type(item) is int and item >= 0:
            evidence[key] = item
        elif isinstance(item, list) and all(type(pid) is int and pid > 0 for pid in item):
            evidence[key] = item
    if type(value.get("cleanup_complete")) is bool:
        evidence["cleanup_complete"] = value["cleanup_complete"]
    return evidence


def _linked_run(case: dict[str, Any], events: list[dict[str, Any]], output: Path,
                verification: dict[str, Any]) -> tuple[bool, list[str]]:
    """Read owned durable runs; never create a DB to validate a claimed link."""
    path = Path(case.get("db_path", ""))
    links = list(dict.fromkeys(event.get("run_id") or event.get("linked_run_id") for event in events
                              if event.get("kind") == "post_tool_call"
                              and (event.get("tool_name") or event.get("name")) == "rpe_task"
                              and isinstance(event.get("run_id") or event.get("linked_run_id"), str)))
    if not path.is_file() or path.is_symlink() or not links:
        return False, links
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as database:
            database.row_factory = sqlite3.Row
            for run_id in links:
                row = database.execute(
                    "SELECT r.*,e.payload_json FROM runs r JOIN events e ON e.id=r.event_fk WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    continue
                request = json.loads(row["payload_json"])
                execution = request.get("execution", {})
                if (Path(execution.get("input_path", "")).resolve() != output.parent / case["input_path"]
                        or Path(execution.get("output_path", "")).resolve() != output):
                    continue
                if case["expected"] == "rejected":
                    error = json.loads(row["error_json"] or "{}")
                    if row["status"] == "rejected" and _reason_matches(case, error.get("message")):
                        return True, links
                else:
                    stored_verification = json.loads(row["verification_json"] or "{}")
                    artifact = json.loads(row["artifact_json"] or "{}")
                    if (row["status"] == "succeeded" and row["verification_ok"] == 1
                            and stored_verification.get("passed") is True
                            and artifact.get("published") is True
                            and Path(artifact.get("path", "")).resolve() == output
                            and artifact.get("sha256") == verification.get("output_sha256")
                            and stored_verification.get("input_sha256") == case["source_sha256"]):
                        return True, links
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        pass
    return False, links


def validate_case(case: dict[str, Any], workspace: Path | str, result: dict[str, Any]) -> dict[str, Any]:
    """Independently reopen artifacts; model text alone never proves success."""
    verdict: dict[str, Any] = {"passed": False, "expected": case["expected"],
                              "source_unchanged": False, "manual_truth_ok": False,
                              "verification": None, "linked_run_verified": False, "run_ids": []}
    events = [_metadata(event) for event in result.get("events", []) if isinstance(event, dict)]
    verdict["observations_present"] = bool(events)
    verdict["host_completed"] = (result.get("exit_code") == 0 and result.get("completed") is True
                                 and not result.get("timed_out", False))
    cleanup = _process_cleanup(result.get("process_cleanup"))
    if cleanup.get("cleanup_complete") is False or cleanup.get("remaining_descendants") not in (None, 0, []):
        verdict["host_completed"] = False
    verdict["native_independent"] = (_tool_audit(result.get("stream_tool_calls"))["native_independent"]
                                     if not case.get("tool_enabled", False) else None)
    verdict["used_rpe_task"] = any((e.get("tool_name") or e.get("name")) == "rpe_task" for e in events)
    verdict["native_tool_executed"] = any(
        e.get("kind") == "post_tool_call" and (e.get("tool_name") or e.get("name")) in
        {"terminal", "read_file", "write_file", "patch", "search_files", "process_manage"} for e in events
    )
    try:
        workspace = Path(workspace)
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("unowned workspace")
        workspace = workspace.resolve()
        source = _owned_path(workspace, case["input_path"])
        output = _owned_path(workspace, case["output_path"])
        verdict["source_unchanged"] = source.is_file() and _sha(source.read_bytes()) == case["source_sha256"]
        verdict["output_exists"] = os.path.lexists(output)
        if case["expected"] == "rejected":
            summary = _rejection_summary(result.get("final_response"))
            reason = summary.get("reason")
            verdict["rejection_reason_present"] = isinstance(reason, str) and bool(reason.strip())
            verdict["rejection_reason_correct"] = _reason_matches(case, reason)
            verdict["rejection_reason_code"] = case["expected_reason"] if verdict["rejection_reason_correct"] else None
            artifact_ok = (summary.get("status") == "rejected" and verdict["rejection_reason_correct"]
                           and not verdict["output_exists"])
        else:
            if not output.is_file():
                raise ValueError("missing artifact")
            prepared = {"input_path": str(source), "staging_path": str(output),
                        "input_sha256": case["source_sha256"], "format": case["format"],
                        "key_columns": case["key_columns"], "sheet": case.get("sheet"),
                        "encoding": case["encoding"], "delimiter": case["delimiter"]}
            verified = verify_deduplication(prepared)
            verdict["verification"] = {key: value for key, value in verified.items() if key != "error"}
            verdict["manual_truth_ok"] = _manual_truth(case, output)
            artifact_ok = verified.get("passed") is True and verdict["manual_truth_ok"]
        if case.get("tool_enabled", False):
            linked, run_ids = _linked_run(case, events, output, verdict["verification"] or {})
            verdict.update(linked_run_verified=linked, run_ids=run_ids)
            artifact_ok = artifact_ok and verdict["used_rpe_task"] and linked
        elif case["expected"] == "success":
            artifact_ok = artifact_ok and verdict["native_tool_executed"] and not verdict["used_rpe_task"]
        elif verdict["used_rpe_task"]:
            artifact_ok = False
        if not case.get("tool_enabled", False):
            artifact_ok = artifact_ok and verdict["native_independent"] is True
        verdict["passed"] = bool(artifact_ok and verdict["source_unchanged"]
                                 and verdict["observations_present"] and verdict["host_completed"])
    except Exception as error:
        # File contents and arbitrary exception messages are intentionally excluded.
        verdict["validation_error_type"] = type(error).__name__
    return verdict


def _usage_for_report(value: Any) -> dict[str, Any]:
    numeric_fields = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                      "reasoning_tokens", "prompt_tokens", "total_tokens", "api_calls",
                      "api_requests", "requests_with_usage",
                      "api_errors",
                      "reported_requests", "missing_requests", "observed_requests", "api_attempts",
                      "reported_attempts", "missing_attempts", "known_input_tokens", "known_output_tokens",
                      "known_cache_read_tokens", "known_cache_write_tokens", "known_total_tokens"}
    report = {key: None for key in ("input_tokens", "output_tokens", "cache_read_tokens",
                                  "cache_write_tokens", "total_tokens")}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in numeric_fields and (item is None or (type(item) in (int, float)
                                                          and math.isfinite(item) and item >= 0)):
                report[key] = item
            elif key in {"complete", "usage_complete", "coverage_complete"} and type(item) is bool:
                report[key] = item
        # These provider provenance fields have already passed the observer's
        # allowlist. Apply that allowlist again so injected runners are also safe.
        from rpe_harness.integrations.hermes import _usage_metadata
        provenance = _usage_metadata(value)
        for key in ("raw_provider_usage", "present_fields", "response_identity"):
            if key in provenance:
                report[key] = provenance[key]
    return report


def _event_evidence(events: Any) -> list[dict[str, Any]]:
    allowed = {"kind", "ingest_event_id", "source_event_id", "session_id", "task_id", "turn_id",
               "api_request_id", "tool_call_id", "tool_name", "run_id", "model", "provider", "api_mode",
               "response_model", "status", "completed", "failed", "interrupted", "api_call_count",
               "api_duration", "started_at", "ended_at", "received_at", "error_type", "error_code",
               "status_code", "retry_count", "max_retries", "retryable", "receipt_id",
               "contract_sha256", "plan_sha256", "input_sha256", "request_tool_choice",
               "request_parallel_tool_calls", "request_tools_sha256"}
    evidence = []
    for raw in events if isinstance(events, list) else []:
        if not isinstance(raw, dict):
            continue
        event = _metadata(raw)
        clean = {key: value for key, value in event.items() if key in allowed
                 and (value is None or type(value) in (str, int, float, bool))}
        names = event.get('request_tool_names')
        if isinstance(names, list) and all(isinstance(name, str) for name in names):
            clean['request_tool_names'] = names
        calls = event.get('model_tool_calls')
        if isinstance(calls, list):
            clean['model_tool_calls'] = [{key: call[key] for key in ('tool_call_id', 'tool_name')
                if isinstance(call.get(key), str)} for call in calls if isinstance(call, dict)]
        if "usage" in event:
            clean["usage"] = None if event["usage"] is None else _usage_for_report(event["usage"])
        if isinstance(event.get("error"), dict):
            clean["error"] = {key: value for key, value in event["error"].items()
                              if key in {"type", "code"} and isinstance(value, str)}
        evidence.append(clean)
    return evidence


def _save_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _identity_for_report(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    report = {key: item for key, item in value.items()
              if key in {"provider", "requested_model", "api_mode", "reasoning", "base_url"}
              and (item is None or isinstance(item, str))}
    response_models = value.get("response_models")
    if isinstance(response_models, list):
        report["response_models"] = sorted(set(model for model in response_models if isinstance(model, str)))
    return report


def _pair_evidence(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs = []
    for first, second in zip(attempts[::2], attempts[1::2]):
        identities = [first["model_identity"], second["model_identity"]]
        requested = [{key: value for key, value in identity.items() if key != "response_models"}
                     for identity in identities]
        requested_equal = requested[0] == requested[1] if all(requested) else None
        models = [identity.get("response_models") for identity in identities]
        responses_equal = models[0] == models[1] if all(models) else None
        consistent = (False if requested_equal is False or responses_equal is False else
                      True if requested_equal is True and responses_equal is True else None)
        pairs.append({"case_id": first["case_id"],
                      "prompt_identical": first["prompt_sha256"] == second["prompt_sha256"],
                      "input_identical": first["input_sha256"] == second["input_sha256"],
                      "requested_identity_equal": requested_equal, "response_models_equal": responses_equal,
                      "model_identity_consistent": consistent})
    return pairs


def run_comparison(root: Path | str, report_path: Path | str,
                   session_runner: Callable[..., dict[str, Any]], *,
                   cases: list[dict[str, Any]] | None = None, arm_offset: int = 0,
                   checkpoint: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Run all pairs, alternating order, retaining outcomes without private text."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError("comparison root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    comparison_root = Path(tempfile.mkdtemp(prefix="comparison-", dir=root))
    cases = build_cases(comparison_root / "fixtures") if cases is None else cases
    report: dict[str, Any] = {
        "experiment": "hermes-native-v1.1", "case_count": len(cases), "attempts": [],
        "native_hermes_baseline_measured": False,
        "interpretation": "Six controlled paired tasks; no claim of general-agent or causal-graph superiority.",
        "owned_state_root": str(comparison_root),
    }
    for index, case in enumerate(cases):
        for arm in (("native", "integrated") if (index + arm_offset) % 2 == 0 else ("integrated", "native")):
            workspace = comparison_root / "workspaces" / case["id"] / arm
            workspace.mkdir(parents=True)
            source_data = Path(case["source_path"]).read_bytes()
            (workspace / case["input_path"]).write_bytes(source_data)
            db_path = comparison_root / "sessions" / case["id"] / arm / "events.sqlite"
            db_path.parent.mkdir(parents=True)
            started = time.monotonic()
            try:
                host = session_runner(case["prompt"], workspace, db_path, tool_enabled=arm == "integrated",
                                      owner_root=comparison_root)
                if not isinstance(host, dict):
                    raise TypeError("session runner must return a dictionary")
            except Exception as error:
                host = {"exit_code": None, "completed": False, "events": [], "usage": {},
                        "elapsed_seconds": time.monotonic() - started,
                        "runner_error_type": type(error).__name__}
            actual_case = {**case, "tool_enabled": arm == "integrated", "db_path": str(db_path)}
            validation = validate_case(actual_case, workspace, host)
            entry = host.get("entry_validation")
            if arm == "integrated" and isinstance(entry, dict):
                entry_ok = (entry.get("harness_verified") is True if case["expected"] == "success"
                            else entry.get("status") == "rejected" and not entry.get("errors"))
                validation["required_entry_passed"] = entry_ok
                validation["passed"] = validation["passed"] and entry_ok
            audit = _tool_audit(host.get("stream_tool_calls"))
            attempt = {
                "case_id": case["id"], "arm": arm, "expected": case["expected"],
                "prompt_sha256": _sha(case["prompt"].encode()), "input_sha256": _sha(source_data),
                "exit_code": host.get("exit_code"), "completed": host.get("completed") is True,
                "timed_out": bool(host.get("timed_out", False)),
                "elapsed_seconds": host.get("elapsed_seconds"), "session_id": host.get("session_id"),
                "events": _event_evidence(host.get("events")), "usage": _usage_for_report(host.get("usage")),
                "validation": validation,
                "model_identity": _identity_for_report(host.get("model_identity")),
                "tool_counts": audit["tool_counts"], "tool_input_sha256": audit["tool_input_sha256"],
                "process_cleanup": _process_cleanup(host.get("process_cleanup")),
                "api_error_events": sum(_metadata(e).get("kind") == "api_request_error"
                                        for e in host.get("events", []) if isinstance(e, dict)),
            }
            if "runner_error_type" in host:
                attempt["runner_error_type"] = host["runner_error_type"]
            if isinstance(entry, dict):
                attempt["entry_validation"] = {key: value for key, value in entry.items()
                    if key in {"status", "verified", "harness_verified", "errors", "run_id",
                               "receipt_id", "contract_sha256", "plan_sha256"}}
            report["attempts"].append(attempt)
            _save_report(Path(report_path), report)
            if checkpoint is not None:
                checkpoint(report)
    report["summary"] = {
        arm: {"attempts": sum(a["arm"] == arm for a in report["attempts"]),
              "supported_successes": sum(a["arm"] == arm and a["expected"] == "success"
                                         and a["validation"]["passed"] for a in report["attempts"]),
              "correct_rejections": sum(a["arm"] == arm and a["expected"] == "rejected"
                                        and a["validation"]["passed"] for a in report["attempts"])}
        for arm in ("native", "integrated")
    }
    report["pairs"] = _pair_evidence(report["attempts"])
    native = [a for a in report["attempts"] if a["arm"] == "native"]
    report["native_hermes_baseline_measured"] = len(native) == len(cases) and all(
        a["completed"] and a["exit_code"] == 0 and not a["timed_out"]
        and a["validation"]["native_independent"] is True
        and any(e.get("kind") == "post_api_request" for e in a["events"]) for a in native
    ) and not any(pair["model_identity_consistent"] is False for pair in report["pairs"])
    _save_report(Path(report_path), report)
    return report
