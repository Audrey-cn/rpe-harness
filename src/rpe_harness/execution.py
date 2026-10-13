"""Deterministic deduplication with original preservation and exclusive publication.

CSV records are compacted. XLSX records keep their original coordinates: duplicate
rows have their contents cleared, so retained formulas never need translation.
Only explicitly supported workbooks and constraints are accepted.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


@dataclass(frozen=True)
class _StageOwnership:
    identity: tuple[int, int]
    input_path: Path
    output_path: Path
    input_sha256: str


_STAGING_FILES: dict[Path, _StageOwnership] = {}
_CONSTRAINTS = {"preserve_original", "preserve_order", "preserve_formulas"}
_CELL_REFERENCE = re.compile(r"\$?([A-Z]{1,3})\$?([1-9][0-9]*)\Z", re.IGNORECASE)
_SAFE_FUNCTIONS = {
    "ABS", "AND", "AVERAGE", "CONCAT", "CONCATENATE", "COUNT", "COUNTA", "DATE",
    "DAY", "IF", "IFERROR", "ISBLANK", "ISNUMBER", "ISTEXT", "LEFT", "LEN", "LOWER",
    "MAX", "MID", "MIN", "MONTH", "NOT", "OR", "RIGHT", "ROUND", "ROUNDDOWN",
    "ROUNDUP", "SUM", "TEXT", "TRIM", "UPPER", "VALUE", "YEAR",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_request(
    input_path: Path, output_path: Path, key_columns: list[str], keep: str,
    encoding: str, delimiter: str, constraints: list[str] | None,
) -> tuple[Path, Path, str]:
    source, output = Path(input_path), Path(output_path)
    if not source.is_absolute() or not output.is_absolute():
        raise ValueError("input_path and output_path must be absolute")
    if not isinstance(key_columns, list) or not key_columns:
        raise ValueError("key_columns must be a nonempty list")
    if any(not isinstance(column, str) or not column.strip() for column in key_columns):
        raise ValueError("key_columns must contain nonempty strings")
    if len(set(key_columns)) != len(key_columns):
        raise ValueError("key_columns must be unique")
    if keep != "first":
        raise ValueError("only keep=first is supported")
    if not isinstance(encoding, str):
        raise ValueError("encoding must be a string")
    try:
        codecs.lookup(encoding)
    except LookupError as error:
        raise ValueError("unsupported encoding") from error
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in "\r\n\x00":
        raise ValueError("delimiter must be one non-newline character")
    if constraints is not None and (
        not isinstance(constraints, list) or any(not isinstance(c, str) for c in constraints)
    ):
        raise ValueError("constraints must be a list of strings")
    unknown = set(constraints or []) - _CONSTRAINTS
    if unknown:
        raise ValueError(f"unsupported constraints: {sorted(unknown)}")
    file_format = source.suffix.lower().lstrip(".")
    if file_format not in {"csv", "xlsx"} or output.suffix.lower() != source.suffix.lower():
        raise ValueError("input and output must have the same csv or xlsx extension")
    if file_format == "csv" and "preserve_formulas" in (constraints or []):
        raise ValueError("preserve_formulas requires xlsx")
    if os.path.lexists(output):
        raise ValueError("output already exists; overwriting is forbidden")
    source = source.resolve(strict=True)
    output = output.parent.resolve(strict=True) / output.name
    if source == output or not source.is_file():
        raise ValueError("input must be a file distinct from output")
    return source, output, file_format


def _header_positions(header: list[Any], key_columns: list[str]) -> list[int]:
    if not header or any(not isinstance(value, str) or not value.strip() for value in header):
        raise ValueError("header must contain nonempty column names")
    if len(set(header)) != len(header):
        raise ValueError("header column names must be unique")
    missing = set(key_columns) - set(header)
    if missing:
        raise ValueError(f"key columns not found: {sorted(missing)}")
    return [header.index(column) for column in key_columns]


def _key(values: list[Any], positions: list[int]) -> tuple[Any, ...]:
    result = []
    for position in positions:
        value = values[position]
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("empty key values are not supported")
        result.append((type(value).__name__, value))
    return tuple(result)


def _csv_records(data: bytes, encoding: str, delimiter: str) -> list[list[str]]:
    try:
        records = list(csv.reader(io.StringIO(data.decode(encoding), newline=""),
                                  delimiter=delimiter, strict=True))
    except (UnicodeError, csv.Error) as error:
        raise ValueError("input is not a valid CSV for the requested encoding and delimiter") from error
    if not records:
        raise ValueError("CSV must contain a header")
    if any(len(row) != len(records[0]) for row in records[1:]):
        raise ValueError("CSV records must have the same width as the header")
    return records


def _check_workbook_objects(workbook: Any, data: bytes) -> None:
    with ZipFile(io.BytesIO(data)) as archive:
        unsupported = (
            "xl/drawings/", "xl/charts/", "xl/media/", "xl/pivot", "xl/embeddings/",
            "xl/externalLinks/", "xl/slicer", "xl/activeX/", "xl/ctrlProps/",
            "xl/queryTables/", "customXml/",
        )
        if any(name.startswith(unsupported) or "vbaProject" in name
               or name == "xl/connections.xml" for name in archive.namelist()):
            raise ValueError("unsupported workbook object")
        for name in archive.namelist():
            if name == "xl/sharedStrings.xml" or (
                name.startswith("xl/worksheets/") and name.endswith(".xml")
            ):
                for _, element in ElementTree.iterparse(
                    io.BytesIO(archive.read(name)), events=("start",)
                ):
                    if element.tag.rsplit("}", 1)[-1] == "r":
                        raise ValueError("unsupported rich-text workbook object")
    if workbook.defined_names:
        raise ValueError("defined names are not supported")
    for worksheet in workbook.worksheets:
        if (worksheet.merged_cells.ranges or worksheet.tables or worksheet._charts
                or worksheet._images or worksheet.conditional_formatting
                or worksheet.data_validations.count):
            raise ValueError("complex worksheet objects are not supported")


def _check_retained_formulas(workbook: Any, sheet: str, removed_rows: set[int]) -> None:
    from openpyxl.formula import Tokenizer

    for worksheet in workbook.worksheets:
        for row in worksheet:
            if worksheet.title == sheet and row[0].row in removed_rows:
                continue
            for cell in row:
                if cell.data_type != "f":
                    continue
                if not isinstance(cell.value, str):
                    raise ValueError("array and data-table formulas are not supported")
                try:
                    tokens = Tokenizer(cell.value).items
                except Exception as error:
                    raise ValueError("unsupported formula syntax") from error
                for token in tokens:
                    if token.type == "FUNC" and token.subtype == "OPEN":
                        if token.value[:-1].upper() not in _SAFE_FUNCTIONS:
                            raise ValueError("unsupported formula function")
                    if token.type == "OPERAND" and token.subtype == "ERROR":
                        raise ValueError("formulas with error operands are not supported")
                    if token.type != "OPERAND" or token.subtype != "RANGE":
                        continue
                    references = token.value.split(":")
                    if len(references) not in {1, 2}:
                        raise ValueError("unsupported formula reference")
                    matches = [_CELL_REFERENCE.fullmatch(reference) for reference in references]
                    if not all(matches):
                        raise ValueError("only unqualified A1 formula references are supported")
                    referenced_rows = [int(match.group(2)) for match in matches if match]
                    if worksheet.title == sheet and any(
                        min(referenced_rows) <= removed <= max(referenced_rows)
                        for removed in removed_rows
                    ):
                        raise ValueError("retained formula references a row that would be cleared")


def _new_staging(source: Path, output: Path, input_hash: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".rpe-", suffix=output.suffix, dir=output.parent)
    os.close(descriptor)
    path = Path(name)
    stat = path.stat()
    _STAGING_FILES[path] = _StageOwnership(
        (stat.st_dev, stat.st_ino), source, output, input_hash
    )
    return path


def prepare_deduplication(
    input_path: Path, output_path: Path, key_columns: list[str], *,
    sheet: str | None = None, keep: str = "first", encoding: str = "utf-8-sig",
    delimiter: str = ",", constraints: list[str] | None = None,
) -> dict[str, Any]:
    """Create an untrusted staging artifact. Never modify source or publish output."""
    source, output, file_format = _validate_request(
        input_path, output_path, key_columns, keep, encoding, delimiter, constraints
    )
    if sheet is not None and (not isinstance(sheet, str) or not sheet):
        raise ValueError("sheet must be a nonempty string")
    if file_format == "csv" and sheet is not None:
        raise ValueError("sheet is only supported for xlsx")
    data = source.read_bytes()
    prepared: dict[str, Any] = {
        "input_path": str(source), "output_path": str(output),
        "input_sha256": _digest(data), "format": file_format,
        "sheet": sheet, "key_columns": list(key_columns), "keep": keep,
        "encoding": encoding, "delimiter": delimiter,
    }
    workbook = None
    try:
        if file_format == "csv":
            records = _csv_records(data, encoding, delimiter)
            positions = _header_positions(records[0], key_columns)
            retained = []
            seen = set()
            for record in records[1:]:
                key = _key(record, positions)
                if key not in seen:
                    retained.append(record)
                    seen.add(key)
            prepared.update(input_rows=len(records) - 1, output_rows=len(retained))
            staging = _new_staging(source, output, prepared["input_sha256"])
            prepared["staging_path"] = str(staging)
            with staging.open("w", encoding=encoding, newline="") as handle:
                csv.writer(handle, delimiter=delimiter).writerows([records[0], *retained])
        else:
            from openpyxl import load_workbook

            try:
                workbook = load_workbook(io.BytesIO(data), data_only=False, keep_links=False)
            except (KeyError, ValueError, BadZipFile) as error:
                raise ValueError("invalid xlsx workbook") from error
            _check_workbook_objects(workbook, data)
            selected = sheet or workbook.active.title
            if selected not in workbook.sheetnames:
                raise ValueError("requested sheet does not exist")
            worksheet = workbook[selected]
            prepared["sheet"] = selected
            header = [cell.value for cell in worksheet[1]]
            positions = _header_positions(header, key_columns)
            seen = set()
            removed_rows: set[int] = set()
            input_rows = 0
            for cells in worksheet.iter_rows(min_row=2):
                values = [cell.value for cell in cells]
                if all(value is None for value in values):
                    continue
                if any(cells[position].data_type in {"f", "e"} for position in positions):
                    raise ValueError("formula and error key cells are not supported")
                key = _key(values, positions)
                input_rows += 1
                if key in seen:
                    removed_rows.add(cells[0].row)
                else:
                    seen.add(key)
            _check_retained_formulas(workbook, selected, removed_rows)
            prepared.update(input_rows=input_rows, output_rows=input_rows - len(removed_rows))
            staging = _new_staging(source, output, prepared["input_sha256"])
            prepared["staging_path"] = str(staging)
            for row in removed_rows:
                for cell in worksheet[row]:
                    cell.value = None
                    cell.hyperlink = None
                    cell.comment = None
            workbook.save(staging)
        prepared["removed_rows"] = prepared["input_rows"] - prepared["output_rows"]
        with Path(prepared["staging_path"]).open("rb") as handle:
            os.fsync(handle.fileno())
        return prepared
    except BaseException:
        cleanup_staging(prepared)
        raise
    finally:
        if workbook is not None:
            workbook.close()


def _owned_stage(prepared: dict[str, Any]) -> tuple[Path, _StageOwnership]:
    staging = Path(prepared.get("staging_path", ""))
    ownership = _STAGING_FILES.get(staging)
    if ownership is None or staging.is_symlink():
        raise ValueError("staging file is not owned by this execution")
    stat = staging.stat()
    if (stat.st_dev, stat.st_ino) != ownership.identity:
        raise ValueError("staging file was replaced")
    return staging, ownership


def publish_output(prepared: dict[str, Any]) -> None:
    """Publish a verified artifact with an atomic no-overwrite hard link."""
    staging, ownership = _owned_stage(prepared)
    source = Path(prepared["input_path"])
    output = Path(prepared["output_path"])
    if source != ownership.input_path or output != ownership.output_path:
        raise ValueError("execution paths changed after preparation")
    verified_hash = prepared.get("_verified_output_sha256")
    if not verified_hash or _digest(staging.read_bytes()) != verified_hash:
        raise ValueError("staging artifact is unverified or changed after verification")
    if _digest(source.read_bytes()) != ownership.input_sha256:
        raise ValueError("input changed after preparation")
    try:
        os.link(staging, output, follow_symlinks=False)
    except FileExistsError as error:
        raise ValueError("output already exists; overwriting is forbidden") from error
    descriptor = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cleanup_staging(prepared: dict[str, Any]) -> None:
    """Remove only this process's staging inode, leaving replacements untouched."""
    staging = Path(prepared.get("staging_path", ""))
    ownership = _STAGING_FILES.get(staging)
    if ownership is None:
        return
    try:
        stat = staging.lstat()
        if (stat.st_dev, stat.st_ino) == ownership.identity:
            staging.unlink()
    except FileNotFoundError:
        pass
    finally:
        _STAGING_FILES.pop(staging, None)
