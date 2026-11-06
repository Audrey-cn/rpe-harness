import copy
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile
from zipfile import ZIP_DEFLATED

import pytest
from openpyxl import Workbook
from test_skill_catalog import copy_xlsx_skill

NATIVE_ONLY = pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').exists(),
                                 reason='native helper requires macOS filesystem/network sandbox')


def modules():
    from rpe_harness.skill_inspection import SkillInspectionPlan, prepare_skill_inspection
    from rpe_harness.skill_verification import verify_skill_inspection
    return SkillInspectionPlan, prepare_skill_inspection, verify_skill_inspection


def request(tmp_path, mode='rows', *, reviewed_noqa=False):
    from rpe_harness.skill_catalog import SkillCatalog
    root = tmp_path / 'skills'
    copy_xlsx_skill(root / 'productivity/xlsx', reviewed_noqa=reviewed_noqa)
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
@pytest.mark.parametrize('reviewed_noqa', [False, True], ids=['original', 'reviewed-noqa'])
@NATIVE_ONLY
def test_original_helper_execution_and_independent_oracle(tmp_path, mode, expected, reviewed_noqa):
    Plan, prepare, verify = modules()
    payload = request(tmp_path, mode, reviewed_noqa=reviewed_noqa)
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


def test_trust_does_not_transfer_to_another_audited_revision(tmp_path):
    from rpe_harness.skill_catalog import SkillCatalog, validate_skill_binding
    payload = request(tmp_path)
    binding = payload['execution']['skill_binding']
    reviewed = tmp_path / 'reviewed'
    copy_xlsx_skill(reviewed, reviewed_noqa=True)
    helper = tmp_path / 'skills/productivity/xlsx/scripts/xlsx_read.py'
    helper.write_bytes((reviewed / 'scripts/xlsx_read.py').read_bytes())
    with pytest.raises(ValueError, match='original skill or helper changed'):
        validate_skill_binding(binding)

    catalog = SkillCatalog(tmp_path / 'db.sqlite', tmp_path / 'owned')
    catalog.sync(tmp_path / 'skills')
    row = catalog.lookup('productivity/xlsx')
    assert row['snapshot_id'] != binding['snapshot_id']
    assert row['adapter_status'] == 'untrusted' and row['executable'] is False


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
                from xml.etree import ElementTree as ET
                namespace = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
                root = ET.fromstring(raw)
                cell = root.find(f'.//{{{namespace}}}c[@r="B3"]')
                assert cell is not None
                formula = cell.find(f'{{{namespace}}}f')
                cached = cell.find(f'{{{namespace}}}v')
                assert formula is not None and formula.text == 'A2+1'
                assert cached is not None and cached.text in (None, '')
                cached.text = '7'
                raw = ET.tostring(root, encoding='utf-8')
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


def _inject_worksheet(path, insertion):
    with ZipFile(path) as archive:
        members = [(name, archive.read(name)) for name in archive.namelist()]
    with ZipFile(path, 'w', compression=ZIP_DEFLATED) as archive:
        for name, raw in members:
            if name == 'xl/worksheets/sheet1.xml':
                raw = raw.replace(b'</worksheet>', insertion + b'</worksheet>')
            archive.writestr(name, raw)


class MergeAllocationProbe(RuntimeError):
    pass


@pytest.mark.parametrize('path_kind', ['producer', 'oracle'])
@pytest.mark.parametrize('package_variant', ['normal', 'wrong_root', 'custom_part_wrong_type', 'custom_part_namespace_alias'])
def test_giant_merge_rejected_before_openpyxl_allocation(tmp_path, monkeypatch, path_kind, package_variant):
    Plan, _, verify = modules()
    payload = request(tmp_path, 'formulas')
    source = Path(payload['execution']['input_path'])
    _inject_worksheet(source, b'<mergeCells count="1"><mergeCell ref="A1:XFD1048576"/></mergeCells>')
    if package_variant != 'normal':
        with ZipFile(source) as archive:
            members = [(name, archive.read(name)) for name in archive.namelist()]
        with ZipFile(source, 'w', compression=ZIP_DEFLATED) as archive:
            for name, raw in members:
                if name == 'xl/worksheets/sheet1.xml':
                    raw = raw.replace(b'<worksheet ', b'<pretend ').replace(b'</worksheet>', b'</pretend>')
                    if package_variant.startswith('custom_part'):
                        name = 'xl/custom.dat'
                elif package_variant.startswith('custom_part'):
                    if name == 'xl/_rels/workbook.xml.rels':
                        raw = raw.replace(b'/xl/worksheets/sheet1.xml', b'/xl/custom.dat')
                        raw = raw.replace(b'/relationships/worksheet', b'/relationships/styles')
                    elif name == '[Content_Types].xml':
                        raw = raw.replace(b'/xl/worksheets/sheet1.xml', b'/xl/custom.dat')
                        raw = raw.replace(b'spreadsheetml.worksheet+xml', b'spreadsheetml.fake+xml')
                    elif name == 'xl/workbook.xml' and package_variant == 'custom_part_namespace_alias':
                        raw = raw.replace(b'r:id="rId1"', b'r:id="rId1" xmlns:alias="urn:alias" alias:id="rId2"')
                archive.writestr(name, raw)
    assert source.stat().st_size < 16384
    from openpyxl.worksheet.worksheet import Worksheet

    def stop_before_allocation(self, merged_range):
        raise MergeAllocationProbe('ordinary loader reached merge expansion: ' + str(merged_range))

    monkeypatch.setattr(Worksheet, '_clean_merge_range', stop_before_allocation)
    if path_kind == 'producer':
        with pytest.raises(ValueError, match='grid|worksheet'):
            Plan.from_request(payload)
    else:
        artifact = tmp_path / 'probe.json'
        artifact.write_text('{"formulas":[]}')
        prepared = {'input_path': str(source), 'output_path': payload['execution']['output_path'],
                    'input_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'format': 'xlsx',
                    'mode': 'formulas', 'sheet': 'Data', 'skill_binding': payload['execution']['skill_binding'],
                    'constraints': [], 'staging_path': str(artifact)}
        report = verify(prepared)
        assert not report['passed']
        assert 'grid' in report['error'] or 'worksheet' in report['error']
    assert not Path(payload['execution']['output_path']).exists()


@pytest.mark.parametrize('insertion', [
    b'<mergeCells count="3"><mergeCell ref="A1:J5000"/><mergeCell ref="A1:J5000"/><mergeCell ref="A1:J5000"/></mergeCells>',
    b'<hyperlinks><hyperlink ref="A1:XFD1048576" location="Data!A1"/></hyperlinks>',
    b'<c r="XFD1048576"><v>1</v></c>',
])
def test_xml_range_and_cumulative_expansion_budgets(tmp_path, insertion):
    Plan, _, _ = modules()
    payload = request(tmp_path)
    source = Path(payload['execution']['input_path'])
    _inject_worksheet(source, insertion)
    with pytest.raises(ValueError, match='grid'):
        Plan.from_request(payload)


@pytest.mark.parametrize('budget,value', [('MAX_XML_NODES', 20), ('MAX_XML_BYTES', 1024)])
def test_streamed_xml_budgets_precede_workbook_construction(tmp_path, monkeypatch, budget, value):
    Plan, _, _ = modules()
    payload = request(tmp_path)
    from rpe_harness import xlsx_resource
    import openpyxl
    monkeypatch.setattr(xlsx_resource, budget, value)

    def no_workbook_before_admission(*args, **kwargs):
        raise MergeAllocationProbe('unbounded workbook parser reached before resource admission')

    monkeypatch.setattr(openpyxl, 'load_workbook', no_workbook_before_admission)
    with pytest.raises(ValueError, match='budget'):
        Plan.from_request(payload)


def test_xml_depth_and_dtd_entities_are_rejected_without_loading(tmp_path):
    payload = request(tmp_path)
    source = Path(payload['execution']['input_path'])
    from rpe_harness.xlsx_resource import preflight_xlsx
    _inject_worksheet(source, b'<guard>' * 65 + b'</guard>' * 65)
    with pytest.raises(ValueError, match='depth'):
        preflight_xlsx(source.read_bytes())
    with ZipFile(source) as archive:
        members = [(name, archive.read(name)) for name in archive.namelist()]
    with ZipFile(source, 'w', compression=ZIP_DEFLATED) as archive:
        for name, raw in members:
            if name == 'xl/worksheets/sheet1.xml':
                raw = b'<!DOCTYPE worksheet [<!ENTITY denied SYSTEM "file:///unavailable">]>' + raw
            archive.writestr(name, raw)
    with pytest.raises(ValueError, match='DTD/entities'):
        preflight_xlsx(source.read_bytes())


def test_xml_external_relationship_is_refused_without_fetching(tmp_path):
    payload = request(tmp_path)
    source = Path(payload['execution']['input_path'])
    with ZipFile(source, 'a', compression=ZIP_DEFLATED) as archive:
        archive.writestr('other/_rels/unused.xml.rels', b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="external" Type="external" Target="https://example.invalid/no-fetch" TargetMode="External"/></Relationships>')
    from rpe_harness.xlsx_resource import preflight_xlsx
    with pytest.raises(ValueError, match='external'):
        preflight_xlsx(source.read_bytes())


def test_cumulative_workbook_grid_precedes_normal_loader(tmp_path):
    Plan, _, _ = modules()
    payload = request(tmp_path)
    source = Path(payload['execution']['input_path'])
    workbook = Workbook()
    workbook.active.title = 'Data'
    for index in range(3):
        sheet = workbook.active if index == 0 else workbook.create_sheet('Sheet' + str(index))
        sheet.cell(70000, 1, 'sparse')
    workbook.save(source)
    workbook.close()
    with pytest.raises(ValueError, match='cumulative grid'):
        Plan.from_request(payload)


@NATIVE_ONLY
def test_small_ordinary_merge_remains_supported(tmp_path):
    Plan, prepare, verify = modules()
    payload = request(tmp_path)
    _inject_worksheet(Path(payload['execution']['input_path']),
                      b'<mergeCells count="1"><mergeCell ref="A1:B2"/></mergeCells>')
    plan = Plan.from_request(payload)
    prepared = prepare(**plan.execution_kwargs())
    assert json.loads(Path(prepared['staging_path']).read_text()) == {'sheet': 'Data', 'rows': [
        ['key', None, 'date'], [None, None, '2026-10-14T12:00:00'], [2, '=A2+1', None]]}
    assert verify(prepared, plan=plan)['passed']
    from rpe_harness.execution import cleanup_staging
    cleanup_staging(prepared)


@pytest.mark.parametrize('path_kind',['producer','oracle'])
def test_shared_worksheet_part_is_rejected_before_each_instance_expands(tmp_path,monkeypatch,path_kind):
    from xml.etree import ElementTree as ET
    from openpyxl.worksheet.worksheet import Worksheet
    Plan, _, verify = modules()
    payload = request(tmp_path)
    source = Path(payload['execution']['input_path'])
    book=Workbook();book.active.title='Data';book.active.append(['id']);book.active.append([1])
    book.save(source);book.close()
    _inject_worksheet(source,b'<mergeCells count="1"><mergeCell ref="A1:A100000"/></mergeCells>')
    with ZipFile(source) as archive:
        members=[(name,archive.read(name)) for name in archive.namelist()]
    with ZipFile(source,'w',compression=ZIP_DEFLATED) as archive:
        for name,raw in members:
            if name=='xl/workbook.xml':
                tree=ET.fromstring(raw)
                sheets=tree.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets')
                first=sheets[0]
                for index in range(2):
                    alias=ET.SubElement(sheets,first.tag,dict(first.attrib))
                    alias.set('name','Alias'+str(index));alias.set('sheetId',str(index+2))
                raw=ET.tostring(tree)
            archive.writestr(name,raw)
    def stop_before_allocation(*args,**kwargs):
        raise MergeAllocationProbe('ordinary parser reached aliased worksheet expansion')
    monkeypatch.setattr(Worksheet,'_clean_merge_range',stop_before_allocation)
    if path_kind=='producer':
        with pytest.raises(ValueError,match='duplicated|shared'):
            Plan.from_request(payload)
    else:
        output=tmp_path/'probe.json';output.write_text('{"sheet":"Data","rows":[]}')
        prepared={'input_path':str(source),'output_path':payload['execution']['output_path'],
          'input_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'format':'xlsx',
          'mode':'rows','sheet':'Data','skill_binding':payload['execution']['skill_binding'],
          'constraints':[],'staging_path':str(output)}
        report=verify(prepared)
        assert not report['passed']
        assert 'duplicated' in report['error'] or 'shared' in report['error']
