"""Independent artifact verification: reopen files and derive first-record truth.

This module does not import the writer's record-selection or formula helpers.
Formula text and coordinates are checked, never their calculated values.
"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


def _read_csv(data: bytes, encoding: str, delimiter: str) -> list[list[str]]:
    rows = list(csv.reader(io.StringIO(data.decode(encoding), newline=""),
                           delimiter=delimiter, strict=True))
    if not rows or any(len(row) != len(rows[0]) for row in rows[1:]):
        raise ValueError("malformed CSV artifact")
    return rows


def _first_record_truth(
    records: list[tuple[int, list[Any]]], header: list[Any], columns: list[str],
) -> tuple[list[tuple[int, list[Any]]], set[int]]:
    if not header or len(set(header)) != len(header):
        raise ValueError("source header is invalid")
    positions = [header.index(column) for column in columns]
    first_index: dict[tuple[Any, ...], int] = {}
    record_keys = []
    for index, row in records:
        parts = []
        for position in positions:
            value = row[position]
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError("source has empty key values")
            parts.append((type(value).__name__, value))
        key = tuple(parts)
        first_index.setdefault(key, index)
        record_keys.append(key)
    retained = [record for record, key in zip(records, record_keys)
                if first_index[key] == record[0]]
    removed = {record[0] for record, key in zip(records, record_keys)
               if first_index[key] != record[0]}
    return retained, removed


def _csv_checks(source: bytes, artifact: bytes, prepared: dict[str, Any]) -> dict[str, Any]:
    original = _read_csv(source, prepared["encoding"], prepared["delimiter"])
    result = _read_csv(artifact, prepared["encoding"], prepared["delimiter"])
    retained, removed = _first_record_truth(
        list(enumerate(original[1:], start=2)), original[0], prepared["key_columns"]
    )
    expected = [row for _, row in retained]
    header_ok = result[0] == original[0]
    exact = result[1:] == expected
    return {
        "checks": {"headers_preserved": header_ok, "first_records_preserved": exact,
                   "record_order_preserved": exact, "duplicates_removed": exact},
        "input_rows": len(original) - 1, "output_rows": len(expected),
        "removed_rows": len(removed),
    }


def _cell_signature(cell: Any) -> tuple[Any, ...]:
    return (
        type(cell.value).__name__, cell.value, cell.data_type,
        tuple(cell._style) if cell.has_style else None,
        (cell.comment.text, cell.comment.author) if cell.comment else None,
        (cell.hyperlink.target, cell.hyperlink.location) if cell.hyperlink else None,
    )


def _xlsx_checks(source: bytes, artifact: bytes, prepared: dict[str, Any]) -> dict[str, Any]:
    from openpyxl import load_workbook

    # Inspect raw source and artifact XML before openpyxl can flatten rich runs.
    # Scan all sheets and shared strings independently of the writer's checks.
    for data in (source, artifact):
        with ZipFile(io.BytesIO(data)) as archive:
            string_parts = (
                name for name in archive.namelist()
                if name == "xl/sharedStrings.xml"
                or (name.startswith("xl/worksheets/") and name.endswith(".xml"))
            )
            for name in string_parts:
                for _, element in ElementTree.iterparse(
                    io.BytesIO(archive.read(name)), events=("start",)
                ):
                    if element.tag.rsplit("}", 1)[-1] == "r":
                        raise ValueError("unsupported rich-text workbook object")
    original = load_workbook(io.BytesIO(source), data_only=False, keep_links=False)
    result = None
    try:
        result = load_workbook(io.BytesIO(artifact), data_only=False, keep_links=False)
        selected = prepared["sheet"]
        source_sheet = original[selected]
        result_sheet = result[selected]
        header = [cell.value for cell in source_sheet[1]]
        records = [
            (row[0].row, [cell.value for cell in row])
            for row in source_sheet.iter_rows(min_row=2)
            if any(cell.value is not None for cell in row)
        ]
        retained, removed = _first_record_truth(records, header, prepared["key_columns"])
        checks = {
            "headers_preserved": [cell.value for cell in result_sheet[1]] == header,
            "first_records_preserved": True, "record_order_preserved": True,
            "duplicates_removed": True, "formulas_preserved": True,
            "non_target_sheets_preserved": original.sheetnames == result.sheetnames,
            "retained_metadata_preserved": True,
        }
        for name in original.sheetnames:
            if name not in result.sheetnames:
                continue
            before, after = original[name], result[name]
            for row in range(1, max(before.max_row, after.max_row) + 1):
                for column in range(1, max(before.max_column, after.max_column) + 1):
                    source_cell, result_cell = before.cell(row, column), after.cell(row, column)
                    if name == selected and row in removed:
                        cleared = (result_cell.value is None and result_cell.comment is None
                                   and result_cell.hyperlink is None)
                        checks["duplicates_removed"] &= cleared
                        continue
                    same = _cell_signature(source_cell) == _cell_signature(result_cell)
                    if name == selected:
                        checks["first_records_preserved"] &= same
                        checks["record_order_preserved"] &= same
                    else:
                        checks["non_target_sheets_preserved"] &= same
                    checks["retained_metadata_preserved"] &= (
                        source_cell._style == result_cell._style
                    )
                    if source_cell.data_type == "f" or result_cell.data_type == "f":
                        checks["formulas_preserved"] &= (
                            source_cell.value == result_cell.value
                            and source_cell.data_type == result_cell.data_type
                        )
        return {"checks": checks, "input_rows": len(records), "output_rows": len(retained),
                "removed_rows": len(removed)}
    finally:
        original.close()
        if result is not None:
            result.close()


def verify_deduplication(prepared: dict[str, Any]) -> dict[str, Any]:
    """Return evidence, and seal only an artifact verified from reopened files."""
    prepared.pop("_verified_output_sha256", None)
    source = Path(prepared["input_path"]).read_bytes()
    artifact = Path(prepared["staging_path"]).read_bytes()
    input_hash = hashlib.sha256(source).hexdigest()
    output_hash = hashlib.sha256(artifact).hexdigest()
    report: dict[str, Any] = {
        "passed": False, "input_sha256": input_hash, "output_sha256": output_hash,
        "checks": {"source_unchanged": input_hash == prepared["input_sha256"]},
    }
    if not report["checks"]["source_unchanged"]:
        return report
    try:
        if prepared["format"] == "csv":
            evidence = _csv_checks(source, artifact, prepared)
        elif prepared["format"] == "xlsx":
            evidence = _xlsx_checks(source, artifact, prepared)
        else:
            raise ValueError("unsupported verification format")
        report["checks"].update(evidence.pop("checks"))
        report.update(evidence)
    except (ValueError, KeyError, UnicodeError, csv.Error, BadZipFile, TypeError) as error:
        report["checks"]["artifact_readable"] = False
        report["error"] = str(error)
        return report
    report["passed"] = all(report["checks"].values())
    if report["passed"]:
        prepared["_verified_output_sha256"] = output_hash
    return report
