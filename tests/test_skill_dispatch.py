import hashlib
import json
import shutil
from pathlib import Path

from openpyxl import Workbook

from rpe_harness.runtime import HarnessRuntime
from rpe_harness.skill_catalog import SkillCatalog
from rpe_harness.integrations.hermes import _prepare_request, _normalize_contract


def inspection_request(tmp_path):
    fixture = Path(__file__).parent / 'fixtures/skills/productivity/xlsx'
    if not fixture.exists():
        fixture = Path('/Users/audrey/.hermes/skills/productivity/xlsx')
    skills = tmp_path / 'skill-source'
    shutil.copytree(fixture, skills / 'productivity/xlsx')
    catalog = SkillCatalog(tmp_path / 'state.sqlite', tmp_path / 'owned')
    catalog.sync(skills)
    binding = catalog.trust_adapter('productivity/xlsx')
    book = Workbook()
    sheet = book.active
    sheet.title = 'Data'
    sheet.append(['id', 'enabled'])
    sheet.append(['001', True])
    sheet.append(['002', False])
    source = tmp_path / 'source.xlsx'
    book.save(source)
    book.close()
    return {'schema_version':2,'event_id':'actual-inspection','capability':'xlsx.inspect',
            'goal':'Read workbook rows without modifying the source',
            'execution':{'input_path':str(source),'output_path':str(tmp_path/'out.json'),
                         'mode':'rows','sheet':'Data','skill_binding':binding}}


def test_runtime_dispatch_publishes_independently_verified_original_skill_output(tmp_path):
    request = inspection_request(tmp_path)
    source = Path(request['execution']['input_path'])
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    runtime = HarnessRuntime(db_path=tmp_path/'runs.sqlite')
    result = runtime.run(request)
    assert result['status'] == 'succeeded', result.get('error')
    assert result['skill_id'] == 'builtin/xlsx-inspect'
    assert result['verification']['passed'] is True
    assert json.loads(Path(request['execution']['output_path']).read_text()) == {
        'sheet':'Data','rows':[['id','enabled'],['001',True],['002',False]]}
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert runtime.run(request)['run_id'] == result['run_id']


def test_hermes_inspection_contract_is_stable_after_publication(tmp_path):
    request = inspection_request(tmp_path)
    before = _normalize_contract(_prepare_request({'request':request},tmp_path,'session'),tmp_path)
    Path(request['execution']['output_path']).write_text('{"already":"published"}')
    after = _normalize_contract(_prepare_request({'request':request},tmp_path,'session'),tmp_path)
    assert after == before
    assert after['capability'] == 'xlsx.inspect'
    assert after['execution']['skill_binding'] == request['execution']['skill_binding']

