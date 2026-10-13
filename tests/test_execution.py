"""File execution contracts, checked against literal records and real files."""

from __future__ import annotations

import csv
import hashlib
import importlib
import importlib.util
import os
import stat
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill


def execution():
    assert importlib.util.find_spec("rpe_harness.execution") is not None, (
        "the deterministic execution module is missing"
    )
    return importlib.import_module("rpe_harness.execution")


def verification():
    assert importlib.util.find_spec("rpe_harness.verification") is not None, (
        "the independent verification module is missing"
    )
    return importlib.import_module("rpe_harness.verification")


def csv_source(tmp_path: Path) -> Path:
    path = tmp_path / "订单.csv"
    path.write_text(
        '订单号,名称,备注\r\n001,首条,"中文,备注\n第二行"\r\n'
        '001,重复,不应保留\r\n002,末条,完整\r\n',
        encoding="utf-8-sig",
        newline="",
    )
    return path


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def xlsx_source(tmp_path: Path) -> Path:
    path = tmp_path / "订单.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet.append(["订单号", "数量", "单价", "总额"])
    sheet.append(["001", 2, 10, "=B2*C2"])
    sheet.append(["001", 999, 9, "=B3*C3"])
    sheet.append(["002", 3, 20, "=SUM(B4:C4)"])
    sheet["A4"].fill = PatternFill("solid", fgColor="00FF00")
    other = workbook.create_sheet("说明")
    other["A1"] = "保留中文说明"
    other["B2"] = "=1+2"
    workbook.save(path)
    workbook.close()
    return path


def test_standard_cell_comments_are_preserved_and_duplicate_comments_cleared(tmp_path):
    from openpyxl.comments import Comment
    source = xlsx_source(tmp_path)
    workbook = load_workbook(source)
    workbook['订单']['A2'].comment = Comment('首条中文注释', 'Auditor')
    workbook['订单']['A3'].comment = Comment('重复注释应清空', 'Auditor')
    workbook['说明']['A1'].comment = Comment('其他工作表注释', 'Auditor')
    workbook.save(source)
    workbook.close()
    prepared = execution().prepare_deduplication(source, tmp_path/'comments.xlsx', ['订单号'])
    try:
        assert verification().verify_deduplication(prepared)['passed']
        output = load_workbook(prepared['staging_path'])
        assert output['订单']['A2'].comment.text == '首条中文注释'
        assert output['订单']['A3'].comment is None
        assert output['说明']['A1'].comment.author == 'Auditor'
        output.close()
    finally:
        execution().cleanup_staging(prepared)


def test_non_note_vml_shapes_are_still_rejected(tmp_path):
    from openpyxl.comments import Comment
    source = xlsx_source(tmp_path)
    workbook = load_workbook(source)
    workbook['订单']['A2'].comment = Comment('note', 'Auditor')
    workbook.save(source)
    workbook.close()
    replaced = tmp_path/'button.xlsx'
    with ZipFile(source) as original, ZipFile(replaced, 'w') as changed:
        for entry in original.infolist():
            data = original.read(entry.filename)
            if entry.filename.endswith('.vml'):
                data = data.replace(b'ObjectType="Note"', b'ObjectType="Button"')
            changed.writestr(entry, data)
    with pytest.raises(ValueError, match='unsupported workbook object'):
        execution().prepare_deduplication(replaced, tmp_path/'out.xlsx', ['订单号'])


def test_csv_keeps_first_complete_record_with_leading_zero_and_multiline_text(tmp_path):
    source = csv_source(tmp_path)
    original = source.read_bytes()
    prepared = execution().prepare_deduplication(
        source, tmp_path / "结果.csv", ["订单号"]
    )
    try:
        assert read_csv(Path(prepared["staging_path"])) == [
            ["订单号", "名称", "备注"],
            ["001", "首条", "中文,备注\n第二行"],
            ["002", "末条", "完整"],
        ]
        assert source.read_bytes() == original
        assert prepared["input_sha256"] == hashlib.sha256(original).hexdigest()
        assert (prepared["input_rows"], prepared["output_rows"], prepared["removed_rows"]) == (
            3, 2, 1
        )
        assert not (tmp_path / "结果.csv").exists()
    finally:
        execution().cleanup_staging(prepared)


def test_verified_csv_publishes_without_modifying_source(tmp_path):
    source = csv_source(tmp_path)
    original = source.read_bytes()
    output = tmp_path / "结果.csv"
    prepared = execution().prepare_deduplication(source, output, ["订单号"])
    try:
        report = verification().verify_deduplication(prepared)
        assert report["passed"] is True
        assert all(report["checks"].values())
        execution().publish_output(prepared)
        assert output.read_bytes() == Path(prepared["staging_path"]).read_bytes()
        assert report["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
        assert source.read_bytes() == original
    finally:
        execution().cleanup_staging(prepared)
    assert not Path(prepared["staging_path"]).exists()
    assert output.exists()


@pytest.mark.parametrize("existing", ["file", "symlink", "broken_symlink"])
def test_existing_output_is_rejected_without_overwrite(tmp_path, existing):
    source = csv_source(tmp_path)
    output = tmp_path / "结果.csv"
    sentinel = tmp_path / "existing.txt"
    sentinel.write_text("keep")
    if existing == "file":
        output.write_text("existing output")
    elif existing == "symlink":
        output.symlink_to(sentinel)
    else:
        output.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError):
        execution().prepare_deduplication(source, output, ["订单号"])
    assert sentinel.read_text() == "keep"
    assert output.is_symlink() or output.read_text() == "existing output"


def test_output_created_after_verification_is_not_overwritten(tmp_path):
    source = csv_source(tmp_path)
    output = tmp_path / "结果.csv"
    prepared = execution().prepare_deduplication(source, output, ["订单号"])
    try:
        assert verification().verify_deduplication(prepared)["passed"]
        output.write_text("concurrent writer")
        with pytest.raises(ValueError):
            execution().publish_output(prepared)
        assert output.read_text() == "concurrent writer"
    finally:
        execution().cleanup_staging(prepared)


@pytest.mark.parametrize(
    "changes",
    [
        {"key_columns": []},
        {"key_columns": ["订单号", "订单号"]},
        {"key_columns": ["missing"]},
        {"keep": "last"},
        {"constraints": ["unknown_constraint"]},
        {"constraints": ["preserve_formulas"]},
        {"delimiter": "||"},
        {"sheet": "订单"},
    ],
)
def test_unsupported_csv_request_is_rejected(tmp_path, changes):
    source = csv_source(tmp_path)
    options = {"key_columns": ["订单号"], **changes}
    with pytest.raises(ValueError):
        execution().prepare_deduplication(source, tmp_path / "结果.csv", **options)
    assert list(tmp_path.glob(".rpe-*")) == []


@pytest.mark.parametrize(
    "body",
    ["订单号,名称\n,空key\n", "订单号,名称\n  ,空key\n", "订单号,名称\n001\n"],
)
def test_empty_keys_and_incomplete_csv_records_are_rejected(tmp_path, body):
    source = tmp_path / "bad.csv"
    source.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        execution().prepare_deduplication(source, tmp_path / "结果.csv", ["订单号"])
    assert list(tmp_path.glob(".rpe-*")) == []


def test_csv_verifier_does_not_trust_prepare_counts(tmp_path):
    source = csv_source(tmp_path)
    prepared = execution().prepare_deduplication(source, tmp_path / "结果.csv", ["订单号"])
    try:
        prepared.update(input_rows=999, output_rows=999, removed_rows=0)
        report = verification().verify_deduplication(prepared)
        assert report["passed"]
        assert (report["input_rows"], report["output_rows"], report["removed_rows"]) == (3, 2, 1)
    finally:
        execution().cleanup_staging(prepared)


@pytest.mark.parametrize(
    "body",
    [
        "订单号,名称,备注\n001,重复,不应保留\n002,末条,完整\n",
        '订单号,名称,备注\n002,末条,完整\n001,首条,"中文,备注\n第二行"\n',
        '订单号,名称\n001,首条\n002,末条\n',
    ],
)
def test_csv_verifier_rejects_wrong_record_order_or_lost_nonkey_content(tmp_path, body):
    source = csv_source(tmp_path)
    prepared = execution().prepare_deduplication(source, tmp_path / "结果.csv", ["订单号"])
    try:
        Path(prepared["staging_path"]).write_text(body, encoding="utf-8-sig")
        assert verification().verify_deduplication(prepared)["passed"] is False
        with pytest.raises(ValueError):
            execution().publish_output(prepared)
    finally:
        execution().cleanup_staging(prepared)


def test_changed_original_file_fails_verification_and_publication(tmp_path):
    source = csv_source(tmp_path)
    output = tmp_path / "结果.csv"
    prepared = execution().prepare_deduplication(source, output, ["订单号"])
    try:
        source.write_text("用户已更新\n", encoding="utf-8")
        assert verification().verify_deduplication(prepared)["passed"] is False
        with pytest.raises(ValueError):
            execution().publish_output(prepared)
        assert not output.exists()
    finally:
        execution().cleanup_staging(prepared)


def test_staging_change_after_good_verification_cannot_be_published(tmp_path):
    source = csv_source(tmp_path)
    output = tmp_path / "结果.csv"
    prepared = execution().prepare_deduplication(source, output, ["订单号"])
    try:
        assert verification().verify_deduplication(prepared)["passed"]
        Path(prepared["staging_path"]).write_text("corrupt")
        with pytest.raises(ValueError):
            execution().publish_output(prepared)
        assert not output.exists()
    finally:
        execution().cleanup_staging(prepared)


def test_xlsx_preserves_coordinates_and_formula_text_and_clears_duplicate_row(tmp_path):
    source = xlsx_source(tmp_path)
    original = source.read_bytes()
    prepared = execution().prepare_deduplication(
        source, tmp_path / "结果.xlsx", ["订单号"], sheet="订单",
        constraints=["preserve_original", "preserve_formulas", "preserve_order"],
    )
    try:
        workbook = load_workbook(prepared["staging_path"], data_only=False)
        sheet = workbook["订单"]
        assert [sheet.cell(3, col).value for col in range(1, 5)] == [None] * 4
        assert [sheet.cell(2, col).value for col in range(1, 5)] == ["001", 2, 10, "=B2*C2"]
        assert [sheet.cell(4, col).value for col in range(1, 5)] == ["002", 3, 20, "=SUM(B4:C4)"]
        assert sheet["D4"].data_type == "f"
        assert sheet["A4"].fill.fgColor.rgb == "0000FF00"
        assert workbook["说明"]["A1"].value == "保留中文说明"
        assert workbook["说明"]["B2"].value == "=1+2"
        workbook.close()
        assert source.read_bytes() == original
        assert verification().verify_deduplication(prepared)["passed"] is True
    finally:
        execution().cleanup_staging(prepared)


@pytest.mark.parametrize("formula", ["=B3", "=SUM(B2:B4)", '=INDIRECT("B3")', "='说明'!B2"])
def test_xlsx_unsafe_formula_is_rejected_before_staging(tmp_path, formula):
    source = xlsx_source(tmp_path)
    workbook = load_workbook(source)
    workbook["订单"]["D4"] = formula
    workbook.save(source)
    workbook.close()
    original = source.read_bytes()
    with pytest.raises(ValueError):
        execution().prepare_deduplication(source, tmp_path / "结果.xlsx", ["订单号"], sheet="订单")
    assert source.read_bytes() == original
    assert list(tmp_path.glob(".rpe-*")) == []


@pytest.mark.parametrize("problem", ["empty_key", "formula_key", "merge", "table", "missing_sheet"])
def test_xlsx_invalid_keys_or_complex_objects_are_rejected(tmp_path, problem):
    source = xlsx_source(tmp_path)
    workbook = load_workbook(source)
    sheet = workbook["订单"]
    if problem == "empty_key":
        sheet["A4"] = None
    elif problem == "formula_key":
        sheet["A4"] = '="002"'
    elif problem == "merge":
        sheet.merge_cells("B2:C2")
    elif problem == "table":
        from openpyxl.worksheet.table import Table

        sheet.add_table(Table(displayName="Orders", ref="A1:D4"))
    workbook.save(source)
    workbook.close()
    with pytest.raises(ValueError):
        execution().prepare_deduplication(
            source, tmp_path / "结果.xlsx", ["订单号"],
            sheet="missing" if problem == "missing_sheet" else "订单",
        )


@pytest.mark.parametrize("mutation", ["formula", "value", "uncleared", "other_sheet"])
def test_xlsx_verifier_rejects_changed_formula_value_duplicate_or_other_sheet(tmp_path, mutation):
    source = xlsx_source(tmp_path)
    prepared = execution().prepare_deduplication(
        source, tmp_path / "结果.xlsx", ["订单号"], sheet="订单"
    )
    try:
        workbook = load_workbook(prepared["staging_path"])
        if mutation == "formula":
            workbook["订单"]["D4"] = "=B2*C2"
        elif mutation == "value":
            workbook["订单"]["B2"] = 999
        elif mutation == "uncleared":
            workbook["订单"]["A3"] = "001"
        else:
            workbook["说明"]["A1"] = "damaged"
        workbook.save(prepared["staging_path"])
        workbook.close()
        assert verification().verify_deduplication(prepared)["passed"] is False
    finally:
        execution().cleanup_staging(prepared)


def test_xlsx_save_failure_removes_only_its_own_staging_file(tmp_path, monkeypatch):
    source = xlsx_source(tmp_path)
    sentinel = tmp_path / ".rpe-someone-else.xlsx"
    sentinel.write_text("keep")

    def fail_save(self, path):
        raise OSError("disk failure")

    monkeypatch.setattr(Workbook, "save", fail_save)
    with pytest.raises(OSError, match="disk failure"):
        execution().prepare_deduplication(source, tmp_path / "结果.xlsx", ["订单号"], sheet="订单")
    assert list(tmp_path.glob(".rpe-*")) == [sentinel]
    assert sentinel.read_text() == "keep"


def test_cleanup_does_not_delete_unowned_or_replaced_path(tmp_path):
    source = csv_source(tmp_path)
    prepared = execution().prepare_deduplication(source, tmp_path / "结果.csv", ["订单号"])
    unrelated = tmp_path / "unrelated.csv"
    unrelated.write_text("keep")
    altered = dict(prepared, staging_path=str(unrelated))
    execution().cleanup_staging(altered)
    assert unrelated.read_text() == "keep"
    staging = Path(prepared["staging_path"])
    replacement = tmp_path / "replacement.csv"
    replacement.write_text("replacement")
    replacement.replace(staging)
    execution().cleanup_staging(prepared)
    assert staging.read_text() == "replacement"


def test_malformed_xlsx_is_a_request_rejection_and_creates_no_staging(tmp_path):
    source = tmp_path / "broken.xlsx"
    source.write_bytes(b"this is not an OOXML archive")
    with pytest.raises(ValueError):
        execution().prepare_deduplication(source, tmp_path / "结果.xlsx", ["订单号"])
    assert list(tmp_path.glob(".rpe-*")) == []


def test_publish_cannot_succeed_when_directory_sync_fails(tmp_path, monkeypatch):
    source = csv_source(tmp_path)
    output = tmp_path / "结果.csv"
    prepared = execution().prepare_deduplication(source, output, ["订单号"])
    real_fsync = os.fsync

    def reject_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            assert output.exists(), "the output directory must be synced after publication"
            raise OSError("directory sync failed")
        return real_fsync(descriptor)

    try:
        assert verification().verify_deduplication(prepared)["passed"]
        monkeypatch.setattr(os, "fsync", reject_directory_sync)
        with pytest.raises(OSError, match="directory sync failed"):
            execution().publish_output(prepared)
    finally:
        execution().cleanup_staging(prepared)


def rich_text_source(tmp_path: Path, location: str, representation: str) -> Path:
    from io import BytesIO

    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    path = tmp_path / "富文本.xlsx"
    workbook = Workbook()
    orders = workbook.active
    orders.title = "订单"
    orders.append(["订单号", "备注"])
    orders.append(["001", "首条"])
    orders.append(["001", "重复"])
    orders.append(["002", "普通"])
    notes = workbook.create_sheet("说明")
    notes["A1"] = "普通说明"
    rich = CellRichText(
        TextBlock(InlineFont(b=True), "加粗"),
        TextBlock(InlineFont(i=True), "斜体"),
    )
    if location == "first_record":
        orders["B2"] = rich
        xml_path, coordinate = "xl/worksheets/sheet1.xml", "B2"
    else:
        notes["A1"] = rich
        xml_path, coordinate = "xl/worksheets/sheet2.xml", "A1"
    workbook.save(path)
    workbook.close()
    if representation == "shared":
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        with ZipFile(path) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        worksheet = ElementTree.fromstring(parts[xml_path])
        cell = worksheet.find(f".//{{{namespace}}}c[@r='{coordinate}']")
        inline = cell.find(f"{{{namespace}}}is")
        shared = ElementTree.Element(f"{{{namespace}}}sst", count="1", uniqueCount="1")
        item = ElementTree.SubElement(shared, f"{{{namespace}}}si")
        for run in list(inline):
            item.append(run)
        cell.remove(inline)
        cell.set("t", "s")
        ElementTree.SubElement(cell, f"{{{namespace}}}v").text = "0"
        parts[xml_path] = ElementTree.tostring(worksheet)
        parts["xl/sharedStrings.xml"] = ElementTree.tostring(shared)
        types_namespace = "http://schemas.openxmlformats.org/package/2006/content-types"
        types = ElementTree.fromstring(parts["[Content_Types].xml"])
        ElementTree.SubElement(
            types, f"{{{types_namespace}}}Override", PartName="/xl/sharedStrings.xml",
            ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",
        )
        parts["[Content_Types].xml"] = ElementTree.tostring(types)
        relations_namespace = "http://schemas.openxmlformats.org/package/2006/relationships"
        relations = ElementTree.fromstring(parts["xl/_rels/workbook.xml.rels"])
        ElementTree.SubElement(
            relations, f"{{{relations_namespace}}}Relationship", Id="rIdRichText",
            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings",
            Target="sharedStrings.xml",
        )
        parts["xl/_rels/workbook.xml.rels"] = ElementTree.tostring(relations)
        rewritten = BytesIO()
        with ZipFile(rewritten, "w") as archive:
            for name, data in parts.items():
                archive.writestr(name, data)
        path.write_bytes(rewritten.getvalue())
    return path


@pytest.mark.parametrize("location", ["first_record", "other_sheet"])
@pytest.mark.parametrize("representation", ["inline", "shared"])
def test_rich_text_in_retained_record_or_other_sheet_is_rejected(
    tmp_path, location, representation,
):
    source = rich_text_source(tmp_path, location, representation)
    original = source.read_bytes()
    workbook = load_workbook(source, rich_text=True)
    cell = workbook["订单"]["B2"] if location == "first_record" else workbook["说明"]["A1"]
    assert str(cell.value) == "加粗斜体"
    assert cell.value[0].font.b is True
    assert cell.value[1].font.i is True
    workbook.close()
    with pytest.raises(ValueError, match="rich"):
        execution().prepare_deduplication(
            source, tmp_path / "结果.xlsx", ["订单号"], sheet="订单"
        )
    assert source.read_bytes() == original
    assert not (tmp_path / "结果.xlsx").exists()
    assert list(tmp_path.glob(".rpe-*")) == []


def test_independent_verifier_rejects_loss_of_rich_text_styles(tmp_path):
    source = rich_text_source(tmp_path, "first_record", "inline")
    staging = tmp_path / "lost-rich-styles.xlsx"
    workbook = load_workbook(source, rich_text=False)
    workbook["订单"]["A3"] = None
    workbook["订单"]["B3"] = None
    workbook.save(staging)
    workbook.close()
    prepared = {
        "input_path": str(source), "staging_path": str(staging), "format": "xlsx",
        "sheet": "订单", "key_columns": ["订单号"],
        "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    report = verification().verify_deduplication(prepared)
    assert report["passed"] is False
    assert "_verified_output_sha256" not in prepared
