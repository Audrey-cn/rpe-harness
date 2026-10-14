"""Independent XLSX inspection oracle. Never imports or executes the skill helper."""
from __future__ import annotations

from datetime import date, datetime, time
import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile

from .path_binding import read_source
from .skill_catalog import _read, validate_skill_binding

VERIFIER_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_SHEET_GRID = 100000
MAX_WORKBOOK_GRID = 200000


def _value(value):
    return value.isoformat() if isinstance(value, (datetime, date, time)) else value


def _same(left, right):
    # JSON numbers have one numeric type, but bool and null must never equal int.
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if type(left) is list:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if type(left) is dict:
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    return left == right


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('inspection JSON contains duplicate object keys')
        value[key] = item
    return value


def _nonfinite(value):
    raise ValueError('nonfinite inspection values are forbidden')


def _expected(source, mode, sheet, actual):
    # Separate ZIP guard and separate oracle workbook, no producer-parser import.
    with ZipFile(io.BytesIO(source)) as archive:
        entries = archive.infolist()
        if len(entries) > 2048 or sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
            raise ValueError('oracle XLSX resource budget exceeded')
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(source), data_only=False, keep_links=False)
    cached = None
    try:
        grids = [ws.max_row * ws.max_column for ws in book.worksheets]
        if any(grid > MAX_SHEET_GRID for grid in grids) or sum(grids) > MAX_WORKBOOK_GRID:
            raise ValueError('oracle workbook grid exceeds its independent cell budget')
        if mode == 'rows':
            ws = book[sheet or book.active.title]
            row_count = ws.max_row if ws._cells else 0
            if (type(actual) is not dict or set(actual) != {'sheet', 'rows'}
                    or type(actual['rows']) is not list or len(actual['rows']) != row_count
                    or any(type(row) is not list or len(row) != ws.max_column for row in actual['rows'])):
                raise ValueError('inspection rows shape differs from workbook')
            # A huge sparse dimension cannot make a tiny hostile artifact expand the oracle.
            # Access existing cells without iter_rows(), which creates blank Cell objects.
            rows = []
            for row_index in range(1, row_count + 1):
                row = []
                for column_index in range(1, ws.max_column + 1):
                    cell = ws._cells.get((row_index, column_index))
                    row.append(_value(cell.value) if cell is not None else None)
                rows.append(row)
            return {'sheet': ws.title, 'rows': rows}
        if mode == 'sheets':
            rows = []
            for ws in book.worksheets:
                rows.append({'name': ws.title, 'dimensions': ws.calculate_dimension(),
                    'max_row': ws.max_row, 'max_col': ws.max_column,
                    'merged': [str(r) for r in ws.merged_cells.ranges], 'charts': len(ws._charts),
                    'freeze_panes': ws.freeze_panes, 'autofilter': ws.auto_filter.ref,
                    'tables': {table.displayName: table.ref for table in ws.tables.values()},
                    'protected': bool(ws.protection.sheet)})
            return {'sheets': rows, 'defined_names': {name: entry.attr_text for name, entry in book.defined_names.items()}}
        if mode == 'formulas':
            cached = load_workbook(io.BytesIO(source), data_only=True, keep_links=False)
            result = []
            for title in [sheet] if sheet is not None else book.sheetnames:
                for cell in sorted(book[title]._cells.values(), key=lambda cell: (cell.row, cell.column)):
                    if cell.data_type == 'f':
                        result.append({'sheet': title, 'cell': cell.coordinate,
                                       'formula': cell.value, 'cached': _value(cached[title][cell.coordinate].value)})
            return {'formulas': result}
        raise ValueError('oracle mode is unregistered')
    finally:
        book.close()
        if cached is not None:
            cached.close()


def verify_skill_inspection(prepared, plan=None):
    prepared.pop('_verified_output_sha256', None)
    prepared.pop('_verified_plan_sha256', None)
    report = {'passed': False, 'checks': {}}
    if plan is not None:
        from .skill_inspection import current_revisions
        revisions = current_revisions()
        report['plan_sha256'] = plan.sha256
        report['checks'].update(executor_revision=plan.executor_revision == revisions['executor_revision'],
            verifier_revision=plan.verifier_revision == VERIFIER_REVISION,
            request_bound=plan.matches_prepared(prepared))
        if not all(report['checks'].values()):
            return report
    parameters = plan.verification_parameters() if plan is not None else prepared
    try:
        if parameters['format'] != 'xlsx':
            raise ValueError('inspection oracle requires XLSX format')
        binding = validate_skill_binding(parameters['skill_binding'])
        report['checks']['skill_binding'] = True
        source = read_source(parameters['input_path'], max_bytes=MAX_INPUT_BYTES)
        output = _read(prepared['staging_path'], MAX_OUTPUT_BYTES)
        report.update(input_sha256=hashlib.sha256(source).hexdigest(), output_sha256=hashlib.sha256(output).hexdigest(),
                      input_bytes=len(source), output_bytes=len(output), skill_sha256=binding['skill_sha256'],
                      script_sha256=binding['script_sha256'], snapshot_id=binding['snapshot_id'],
                      mode=parameters['mode'], root_type='object')
        report['checks']['source_unchanged'] = report['input_sha256'] == parameters['input_sha256']
        if not report['checks']['source_unchanged']:
            return report
        actual = json.loads(output.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_nonfinite)
        expected = _expected(source, parameters['mode'], parameters.get('sheet'), actual)
        report['checks']['independent_workbook_result'] = _same(actual, expected)
    except (ValueError, TypeError, OSError, KeyError, UnicodeError, RecursionError) as error:
        report['error'] = str(error)
        return report
    report['passed'] = all(report['checks'].values())
    if report['passed']:
        prepared['_verified_output_sha256'] = report['output_sha256']
        if plan is not None:
            prepared['_verified_plan_sha256'] = plan.sha256
    return report
