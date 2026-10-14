import copy
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
from openpyxl import Workbook

XLSX_FIXTURE = Path(__file__).parent / 'fixtures/skills/productivity/xlsx'
NATIVE_ONLY = pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').exists(),
                                 reason='native helper requires macOS filesystem/network sandbox')


def modules():
    from rpe_harness.skill_inspection import SkillInspectionPlan, prepare_skill_inspection
    from rpe_harness.skill_verification import verify_skill_inspection
    return SkillInspectionPlan, prepare_skill_inspection, verify_skill_inspection


def request(tmp_path, mode='rows'):
    from rpe_harness.skill_catalog import SkillCatalog
    root = tmp_path / 'skills'
    shutil.copytree(XLSX_FIXTURE, root / 'productivity/xlsx')
    catalog = SkillCatalog(tmp_path / 'db.sqlite', tmp_path / 'owned')
    catalog.sync(root)
    binding = catalog.trust_adapter('productivity/xlsx')
    wb = Workbook()
    ws = wb.active
    ws.title = 'Data'
    ws.append(['key', 'value', 'date'])
    ws.append([1, True, datetime(2026, 10, 14, 12)])
    ws.append([2, '=A2+1', None])
    wb.create_sheet('Other').append(['untouched'])
    source = tmp_path / 'input.xlsx'
    wb.save(source)
    wb.close()
    return {'schema_version': 2, 'event_id': 'inspect-test', 'capability': 'xlsx.inspect',
            'goal': 'Inspect workbook', 'execution': {'input_path': str(source),
            'output_path': str(tmp_path / 'output.json'), 'mode': mode, 'sheet': 'Data',
            'skill_binding': binding}}


@pytest.mark.parametrize('mode, expected', [
    ('rows', {'sheet': 'Data', 'rows': [['key', 'value', 'date'], [1, True, '2026-10-14T12:00:00'], [2, '=A2+1', None]]}),
    ('formulas', {'formulas': [{'sheet': 'Data', 'cell': 'B3', 'formula': '=A2+1', 'cached': None}]}),
])
@NATIVE_ONLY
def test_original_helper_execution_and_independent_oracle(tmp_path, mode, expected):
    Plan, prepare, verify = modules()
    payload = request(tmp_path, mode)
    plan = Plan.from_request(payload)
    assert Plan.from_dict(plan.to_dict()).sha256 == plan.sha256
    prepared = prepare(**plan.execution_kwargs())
    assert json.loads(Path(prepared['staging_path']).read_text()) == expected
    report = verify(prepared, plan=plan)
    assert report['passed']
    assert not Path(plan.output_path).exists()
    from rpe_harness.execution import publish_output, cleanup_staging
    try:
        publish_output(prepared, plan=plan)
        assert json.loads(Path(plan.output_path).read_text()) == expected
    finally:
        cleanup_staging(prepared)


@NATIVE_ONLY
def test_sheets_inventory_uses_all_sheets(tmp_path):
    Plan, prepare, verify = modules()
    payload = request(tmp_path, 'sheets')
    payload['execution'].pop('sheet')
    plan = Plan.from_request(payload)
    prepared = prepare(**plan.execution_kwargs())
    content = json.loads(Path(prepared['staging_path']).read_text())
    assert [(r['name'], r['max_row'], r['max_col']) for r in content['sheets']] == [('Data', 3, 3), ('Other', 1, 1)]
    assert verify(prepared, plan=plan)['passed']
    from rpe_harness.execution import cleanup_staging
    cleanup_staging(prepared)


@NATIVE_ONLY
def test_bool_int_tampering_fails_and_cannot_publish(tmp_path):
    Plan, prepare, verify = modules()
    plan = Plan.from_request(request(tmp_path))
    prepared = prepare(**plan.execution_kwargs())
    artifact = Path(prepared['staging_path'])
    value = json.loads(artifact.read_text())
    value['rows'][1][1] = 1
    artifact.write_text(json.dumps(value))
    assert not verify(prepared, plan=plan)['passed']
    from rpe_harness.execution import publish_output, cleanup_staging
    try:
        with pytest.raises(ValueError):
            publish_output(prepared, plan=plan)
    finally:
        cleanup_staging(prepared)


@pytest.mark.parametrize('field,value', [('mode', 'shell'), ('sheet', 'Missing'), ('input_path', '/tmp/other.csv')])
def test_invalid_inspection_parameters(tmp_path, field, value):
    Plan, _, _ = modules()
    payload = request(tmp_path)
    payload['execution'][field] = value
    with pytest.raises(ValueError):
        Plan.from_request(payload)


def test_binding_tampering_and_source_change_rejected(tmp_path):
    Plan, prepare, _ = modules()
    payload = request(tmp_path)
    forged = copy.deepcopy(payload)
    forged['execution']['skill_binding']['script_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        Plan.from_request(forged)
    plan = Plan.from_request(payload)
    Path(plan.input_path).write_bytes(b'changed')
    with pytest.raises(ValueError):
        prepare(**plan.execution_kwargs())


def test_zip_bomb_declared_members_rejected(tmp_path):
    Plan, _, _ = modules()
    payload = request(tmp_path)
    with ZipFile(payload['execution']['input_path'], 'w') as archive:
        for i in range(2049):
            archive.writestr(str(i), b'x')
    with pytest.raises(ValueError):
        Plan.from_request(payload)


def test_source_helper_changed_after_sync_is_rejected(tmp_path):
    Plan, _, _ = modules()
    payload = request(tmp_path)
    helper = tmp_path / 'skills/productivity/xlsx/scripts/xlsx_read.py'
    helper.write_text(helper.read_text() + '\n# changed\n')
    with pytest.raises(ValueError):
        Plan.from_request(payload)


@pytest.mark.parametrize('mode, bad', [('formulas', {'formulas': [{'sheet': 'Data', 'cell': 'B3', 'formula': '=A2+1', 'cached': False}]}),
                                     ('sheets', {'sheets': [], 'defined_names': {}})])
@NATIVE_ONLY
def test_independent_oracle_rejects_wrong_formulas_and_inventory(tmp_path, mode, bad):
    Plan, prepare, verify = modules()
    payload = request(tmp_path, mode)
    if mode == 'sheets':
        payload['execution'].pop('sheet')
    plan = Plan.from_request(payload)
    prepared = prepare(**plan.execution_kwargs())
    Path(prepared['staging_path']).write_text(json.dumps(bad))
    assert not verify(prepared, plan=plan)['passed']
    from rpe_harness.execution import cleanup_staging
    cleanup_staging(prepared)


@NATIVE_ONLY
def test_source_changed_after_production_clears_verification_seal(tmp_path):
    Plan, prepare, verify = modules()
    plan = Plan.from_request(request(tmp_path))
    prepared = prepare(**plan.execution_kwargs())
    assert verify(prepared, plan=plan)['passed']
    Path(plan.input_path).write_bytes(b'changed')
    assert not verify(prepared, plan=plan)['passed']
    assert '_verified_output_sha256' not in prepared
    from rpe_harness.execution import cleanup_staging
    cleanup_staging(prepared)


@NATIVE_ONLY
def test_hand_written_cached_formula_fixture(tmp_path):
    Plan, prepare, verify = modules()
    payload = request(tmp_path, 'formulas')
    path = Path(payload['execution']['input_path'])
    with ZipFile(path) as archive:
        members = [(m, archive.read(m)) for m in archive.namelist()]
    with ZipFile(path, 'w') as archive:
        for name, raw in members:
            if name == 'xl/worksheets/sheet1.xml':
                assert b'<f>A2+1</f><v></v>' in raw
                raw = raw.replace(b'<f>A2+1</f><v></v>', b'<f>A2+1</f><v>7</v>')
            archive.writestr(name, raw)
    plan = Plan.from_request(payload)
    prepared = prepare(**plan.execution_kwargs())
    assert json.loads(Path(prepared['staging_path']).read_text()) == {'formulas': [{'sheet': 'Data', 'cell': 'B3', 'formula': '=A2+1', 'cached': 7}]}
    assert verify(prepared, plan=plan)['passed']
    from rpe_harness.execution import cleanup_staging
    cleanup_staging(prepared)


@NATIVE_ONLY
def test_normalized_contract_is_stable_after_publication_and_source_removal(tmp_path):
    Plan, prepare, verify = modules()
    from rpe_harness.skill_inspection import normalized_contract, contract_from_plan
    payload = request(tmp_path)
    payload['execution'].pop('sheet')
    plan = Plan.from_request(payload)
    expected = contract_from_plan(plan)
    assert normalized_contract(payload) == expected
    prepared = prepare(**plan.execution_kwargs())
    assert verify(prepared, plan=plan)['passed']
    from rpe_harness.execution import publish_output, cleanup_staging
    try:
        publish_output(prepared, plan=plan)
        Path(plan.input_path).unlink()
        assert normalized_contract(payload) == expected
        assert normalized_contract(expected) == expected
    finally:
        cleanup_staging(prepared)


def test_sparse_large_dimension_is_rejected_before_helper_or_staging(tmp_path):
    Plan, _, _ = modules()
    payload = request(tmp_path)
    source = Path(payload['execution']['input_path'])
    workbook = Workbook()
    workbook.active.title = 'Data'
    workbook.active.cell(1048576, 16384, 'edge')
    workbook.save(source)
    workbook.close()
    with pytest.raises(ValueError, match='grid'):
        Plan.from_request(payload)
    assert not Path(payload['execution']['output_path']).exists()
    assert list(tmp_path.glob('.rpe-*')) == []


def test_oracle_independently_rejects_oversized_grid_for_correct_inventory(tmp_path):
    _, _, verify = modules()
    payload = request(tmp_path, 'sheets')
    payload['execution'].pop('sheet')
    source = Path(payload['execution']['input_path'])
    workbook = Workbook()
    workbook.active.title = 'Data'
    workbook.active.cell(100001, 1, 'edge')
    workbook.save(source)
    workbook.close()
    artifact = tmp_path / 'inventory.json'
    artifact.write_text(json.dumps({'sheets': [{'name': 'Data', 'dimensions': 'A100001:A100001',
        'max_row': 100001, 'max_col': 1, 'merged': [], 'charts': 0, 'freeze_panes': None,
        'autofilter': None, 'tables': {}, 'protected': False}], 'defined_names': {}}))
    prepared = {'input_path': str(source), 'output_path': payload['execution']['output_path'],
                'input_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'format': 'xlsx',
                'mode': 'sheets', 'sheet': None, 'skill_binding': payload['execution']['skill_binding'],
                'constraints': [], 'staging_path': str(artifact)}
    report = verify(prepared)
    assert not report['passed']
    assert 'grid' in report['error']


@NATIVE_ONLY
def test_child_rejects_helper_bytes_changed_after_binding_check(tmp_path):
    modules()
    from rpe_harness.skill_inspection import _run_helper
    payload = request(tmp_path)
    # Models a snapshot replacement between validation and the final byte copy.
    # Benign replacement proves that content authenticity is checked before exec.
    with pytest.raises(ValueError, match='helper'):
        _run_helper(b'', b'print("{}")\n', payload['execution']['skill_binding'], 'rows', None)
