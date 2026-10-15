"""Review discovery must use source skills without granting new execution rights."""
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
import rpe_harness

from rpe_harness.capability_catalog import build_catalog
from rpe_harness.integrations import hermes
from rpe_harness.integrations.hermes_gate import digest_json
from rpe_harness.skill_catalog import SkillCatalog
from test_hermes_review import ReviewSDK, RAW, draft, host_result


@pytest.fixture
def skill_env(tmp_path, monkeypatch, request):
    source = tmp_path / 'source'
    xlsx = '.archive/xlsx' if getattr(request, 'param', None) == 'native_archive' else 'productivity/xlsx'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx',
                    source / xlsx)
    other = source / 'creative/painting/SKILL.md'
    other.parent.mkdir(parents=True)
    deps = "'deps': [rpe-review-sentinel]\n" if getattr(request, 'param', None) == 'quoted_deps' else ''
    other.write_text('---\nname: painting\ndescription: Paint watercolor landscape art.\n' + deps + '---\n# Painting\n')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'orders.csv').write_text('id,value\na,1\na,2\n')
    db, owned = tmp_path / 'runs.sqlite', tmp_path / 'owned'
    catalog = SkillCatalog(db, owned)
    catalog.sync(source)
    catalog.trust_adapter('productivity/xlsx')
    catalog.set_temperature('productivity/xlsx',
                            'warm' if getattr(request, 'param', None) == 'warm' else 'cold')
    snapshot = catalog.materialize(tmp_path / 'home/skills')
    return dict(source=source, workspace=workspace, db=db, owned=owned, catalog=catalog,
                snapshot=snapshot, home=tmp_path / 'home')


def skills_api():
    assert importlib.util.find_spec('rpe_harness.integrations.review_skills') is not None, \
        'review needs an immutable real-skill selection bridge'
    return importlib.import_module('rpe_harness.integrations.review_skills')


def test_cold_materialization_is_archived_without_touching_original(skill_env):
    api = skills_api()
    prepared = api.prepare_review_skills(skill_env['snapshot'])
    xlsx = next(row for row in prepared['entries'] if row['skill_id'] == 'productivity/xlsx')
    assert Path(xlsx['archive_path']).is_dir()
    assert not Path(xlsx['native_skill_path']).exists()
    assert (skill_env['source'] / 'productivity/xlsx/SKILL.md').is_file()
    assert not (skill_env['source'] / '.archive').exists()
    assert xlsx['native_load_name'] == 'xlsx'
    assert xlsx['skill_id'] == 'productivity/xlsx'


def test_selection_revalidates_after_temporary_home_is_removed(skill_env):
    api = skills_api()
    selected = api.selection_for(skill_env['catalog'].lookup('productivity/xlsx'))
    shutil.rmtree(skill_env['home'])
    assert api.validate_selection(skill_env['db'], skill_env['owned'], selected) == selected
    helper = skill_env['source'] / 'productivity/xlsx/scripts/xlsx_read.py'
    helper.write_bytes(helper.read_bytes() + b'\n# drift\n')
    with pytest.raises(ValueError, match='skill|selection'):
        api.validate_selection(skill_env['db'], skill_env['owned'], selected)


def test_nonadapter_metadata_cannot_be_relabelled_executable(skill_env):
    api = skills_api()
    selected = api.selection_for(skill_env['catalog'].lookup('creative/painting'))
    assert selected['executable'] is False
    assert selected['capabilities'] == []
    selected['executable'] = True
    with pytest.raises(ValueError, match='selection'):
        api.validate_selection(skill_env['db'], skill_env['owned'], selected)


@pytest.fixture
def skill_review(skill_env, monkeypatch, request):
    api = importlib.import_module('rpe_harness.integrations.hermes_review')
    bridge = skills_api()
    prepared = bridge.prepare_review_skills(skill_env['snapshot'])
    capability = build_catalog(db_path=skill_env['db'], catalog_root=skill_env['owned'],
                               skill_snapshot_id=prepared['snapshot_id'])
    config = skill_env['home'].parent / 'review.json'
    options = getattr(request, 'param', RAW)
    raw = options.get('raw', RAW) if type(options) is dict else options
    sources = options.get('sources') if type(options) is dict else None
    config.write_text(json.dumps({'catalog_sha256': capability.snapshot_id,
        'entries': capability.entries(), 'raw': raw, 'source_ref': 'request:test', 'skills': prepared,
        **({'context_sources': sources} if sources else {})}))
    monkeypatch.setenv('RPE_HERMES_REVIEW_CONFIG', str(config))
    monkeypatch.setenv('RPE_HERMES_ENTRY_KEY', 'review-entry')
    sdk, store = ReviewSDK(), hermes.HostEventStore(skill_env['db'])
    api.register_review_workflow(sdk, store=store, workspace=skill_env['workspace'],
                                python=sys.executable, error_path=config.parent / 'errors')
    extension = api.review_extension(skill_env['db'], skill_env['workspace'], raw=raw,
        source_ref='request:test', catalog_root=skill_env['owned'], snapshot_id=prepared['snapshot_id'],
        context_sources=sources)
    from rpe_harness.integrations.hermes_broker import execution_broker
    with execution_broker(db_path=skill_env['db'], workspace=skill_env['workspace'],
                         python=sys.executable, extension=extension, review_auto=True) as broker:
        for key, value in broker.items():
            monkeypatch.setenv(key, value)
        yield {**skill_env, 'sdk': sdk, 'store': store, 'api': api, 'config': config, 'counter': 0}


def step(env, name, args, *, native=None):
    env['counter'] += 1
    ids = dict(session_id='review-session', task_id='review-task', turn_id='review-turn',
               api_request_id='api-' + str(env['counter']), tool_call_id='call-' + str(env['counter']))
    request = env['sdk'].middleware['llm_request'](request={'tools': ([{'type': 'function',
        'function': {'name': 'skill_view', 'parameters': {'type': 'object',
            'properties': {'name': {'type': 'string'}}, 'required': ['name']}}}]
        if name == 'skill_view' else []),
        'messages': [{'role': 'user', 'content': 'PRIVATE_OLD_HISTORY'}]}, **ids)['request']
    assert request['tool_choice'] == 'auto'
    assert [tool['function']['name'] for tool in request['tools']] == [name]
    assert 'PRIVATE_OLD_HISTORY' not in json.dumps(request)
    env['request'] = request
    env['store'].record('pre_api_request', {**ids, 'request': {'body': request}})
    env['sdk'].middleware['llm_execution'](request=request, next_call=lambda value: value, **ids)
    env['store'].record('post_api_request', {**ids, 'response': {'assistant_message': {'tool_calls': [
        {'id': ids['tool_call_id'], 'name': name}]}}})
    env['store'].record('pre_tool_call', {**ids, 'tool_name': name, 'arguments_sha256': digest_json(args)})
    env['sdk'].hooks['pre_tool_call'](tool_name=name, args=args, **ids)
    response = env['sdk'].middleware['tool_execution'](tool_name=name, args=args,
        next_call=(native or (lambda value: env['sdk'].tools[name]['handler'](
            value, task_id=ids['task_id'], session_id=ids['session_id']))), **ids)
    env['store'].record('post_tool_call', {**ids, 'tool_name': name, 'status': 'ok',
        'arguments_sha256': digest_json(args)})
    return json.loads(response)


@pytest.mark.parametrize('skill_env', ['quoted_deps'], indirect=True)
def test_review_search_discovers_nonadapter_without_native_activation_or_confirmation(skill_review, monkeypatch):
    assert skill_review['catalog'].lookup('creative/painting')['dynamic'] is False
    monkeypatch.setattr(skill_review['api'], 'restore_review_skill',
        lambda *args: pytest.fail('untrusted discovery must not activate native skill code'))
    result = step(skill_review, 'rpe_discover', {'q': 'watercolor landscape'})
    assert [row['skill_id'] for row in result['candidates']] == ['creative/painting']
    result = step(skill_review, 'rpe_select', {'skill_id': 'creative/painting'})
    assert result['status'] == 'discovery_only'
    assert result['selection']['executable'] is False
    proposed = step(skill_review, 'rpe_propose', draft())
    assert proposed['status'] == 'unsupported', proposed
    blocked = skill_review['sdk'].middleware['tool_execution'](tool_name='skill_view', args={'name': 'painting'},
        next_call=lambda args: pytest.fail('untrusted native skill handler was called'),
        session_id='review-session', task_id='review-task', turn_id='review-turn',
        api_request_id='forged-view', tool_call_id='forged-view')
    assert json.loads(blocked)['status'] == 'rejected'
    from rpe_harness.proposal import ProposalStore
    with pytest.raises(ValueError):
        ProposalStore(skill_review['db']).confirm(proposed['proposal_id'], session_id='review-session',
            expected_proposal_sha256=proposed['proposal_sha256'], actor='simulated_fixture_reviewer')
    verdict = skill_review['api'].evaluate_review_session(host_result(skill_review), skill_review['db'],
        skill_review['workspace'], catalog_root=skill_review['owned'])
    assert verdict['proposal_verified'] is True, verdict
    assert verdict['native_skill_view_calls'] == 0


@pytest.mark.parametrize('selection', [None, '__no_skill__'])
def test_explicit_none_skips_native_view_and_keeps_review_verifiable(skill_review, selection):
    step(skill_review, 'rpe_discover', {'q': 'watercolor'})
    selected = step(skill_review, 'rpe_select', {'skill_id': selection})
    assert selected['status'] == 'selected_none'
    proposal = step(skill_review, 'rpe_propose', draft())
    assert proposal['status'] == 'awaiting_confirmation', proposal
    assert proposal['skill_selection'] == {'kind': 'none', 'snapshot_id': skill_review['snapshot']['snapshot_id']}
    verdict = skill_review['api'].evaluate_review_session(host_result(skill_review), skill_review['db'],
        skill_review['workspace'], catalog_root=skill_review['owned'])
    assert verdict['proposal_verified'] is True, verdict
    assert not (skill_review['workspace'] / 'out.csv').exists()


def test_select_rejects_a_skill_not_returned_by_discovery(skill_review):
    step(skill_review, 'rpe_discover', {'q': 'watercolor'})
    result = step(skill_review, 'rpe_select', {'skill_id': 'productivity/xlsx'})
    assert result['status'] == 'rejected'


@pytest.mark.parametrize('skill_env', ['warm'], indirect=True)
def test_native_view_with_unready_skill_cannot_advance_to_propose(skill_review):
    step(skill_review, 'rpe_discover', {'q': 'Excel'})
    step(skill_review, 'rpe_select', {'skill_id': 'productivity/xlsx'})
    path = skill_review['home'] / 'skills/productivity/xlsx/SKILL.md'
    result = step(skill_review, 'skill_view', {'name': 'xlsx'}, native=lambda args: json.dumps({
        'success': True, 'setup_needed': True, 'name': 'xlsx', '_source_path': str(path),
        'content': path.read_text()}))
    assert result['status'] == 'rejected'


def test_cold_copy_uses_real_hermes_restoration_and_original_native_view(skill_env):
    hermes_root = Path('/Users/audrey/.hermes/hermes-agent')
    runtime = hermes_root / '.venv/bin/python'
    if not runtime.is_file():
        pytest.skip('installed Hermes runtime unavailable')
    api = skills_api()
    prepared = api.prepare_review_skills(skill_env['snapshot'])
    document = skill_env['home'] / 'prepared.json'
    document.write_text(json.dumps(prepared))
    script = '''
import hashlib,json,os,sys
from pathlib import Path
sys.path[:0] = [sys.argv[1],sys.argv[2]]
snapshot = json.loads(Path(sys.argv[3]).read_text())
os.environ['HERMES_HOME'] = str(Path(snapshot['materialized_path']).parent)
from rpe_harness.integrations.review_skills import restore_review_skill
from tools.skills_tool import skill_view
row = next(row for row in snapshot['entries'] if row['skill_id']=='productivity/xlsx')
path = restore_review_skill(row,snapshot['materialized_path'])
result = json.loads(skill_view(row['native_load_name'],preprocess=False))
assert result['success'],result
print(json.dumps({'source':result['_source_path'],'restored':str(path),
 'digest':hashlib.sha256(result['content'].encode()).hexdigest(),
 'archive_exists':Path(row['archive_path']).exists()}))
'''
    result = subprocess.run([str(runtime), '-I', '-B', '-c', script,
        str(Path(rpe_harness.__file__).resolve().parent.parent), str(hermes_root), str(document)],
        capture_output=True, text=True, cwd=skill_env['home'], timeout=30)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence['source'] == evidence['restored'] == str(skill_env['home'] / 'skills/xlsx/SKILL.md')
    assert evidence['digest'] == 'b302d7ac997843a757ab3c11513ea012889ebdae32f842edb44faa9a4485f19b'
    assert evidence['archive_exists'] is False


@pytest.mark.parametrize('skill_review', ['Inspect source.xlsx as rows into output.json; preserve original.'], indirect=True)
@pytest.mark.parametrize('corruption', [None, 'restore_receipt'])
@pytest.mark.parametrize('skill_env', ['active', 'native_archive'], indirect=True)
def test_cold_skill_review_receipts_lead_to_original_helper_execution(skill_review, monkeypatch, corruption):
    from openpyxl import Workbook
    from rpe_harness.proposal import ProposalStore
    hermes_root = Path('/Users/audrey/.hermes/hermes-agent')
    runtime = hermes_root / '.venv/bin/python'
    if not runtime.is_file() or not Path('/usr/bin/sandbox-exec').exists():
        pytest.skip('installed native Hermes/macOS helper runtime unavailable')
    wb = Workbook()
    wb.active.append(['id', 'flag'])
    wb.active.append([1, True])
    wb.save(skill_review['workspace'] / 'source.xlsx')
    wb.close()
    # Only relocate the actual Hermes process boundary: production restore and
    # native skill_view both run unchanged under Hermes' installed interpreter.
    def native(action, row=None):
        payload = {'action': action, 'row': row, 'root': str(skill_review['home'] / 'skills')}
        script = '''
import json,os,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
data=json.loads(sys.stdin.read())
os.environ['HERMES_HOME']=str(Path(data['root']).parent)
if data['action']=='restore':
 from rpe_harness.integrations.review_skills import restore_review_skill
 result=str(restore_review_skill(data['row'],data['root']))
else:
 from tools.skills_tool import skill_view
 result=json.loads(skill_view('xlsx',preprocess=False))
print(json.dumps(result))
'''
        completed = subprocess.run([str(runtime), '-I', '-B', '-c', script,
            str(Path(rpe_harness.__file__).resolve().parent.parent), str(hermes_root)], input=json.dumps(payload),
            capture_output=True, text=True, cwd=skill_review['home'], timeout=30)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout.splitlines()[-1])
    monkeypatch.setattr(skill_review['api'], 'restore_review_skill', lambda row, root: Path(native('restore', row)))
    found = step(skill_review, 'rpe_discover', {'q': 'Excel spreadsheet'})
    assert any(row['skill_id'] == 'productivity/xlsx' and row['temperature'] == 'cold'
               for row in found['candidates'])
    assert step(skill_review, 'rpe_select', {'skill_id': 'productivity/xlsx'})['status'] == 'selected'
    loaded = step(skill_review, 'skill_view', {'name': 'xlsx'}, native=lambda args: json.dumps(native('view')))
    assert loaded['success'] is True
    proposal = step(skill_review, 'rpe_propose', {
        'capability': 'xlsx.inspect', 'constraints': ['preserve_original'],
        'execution': {'input_path': 'source.xlsx', 'output_path': 'output.json', 'mode': 'rows'},
        'requirements': [
            {'text': 'Inspect workbook.', 'quote': 'Inspect', 'fields': ['/capability']},
            {'text': 'Preserve original.', 'quote': 'preserve original', 'fields': ['/constraints']},
            {'text': 'Read source.', 'quote': 'source.xlsx', 'fields': ['/execution/input_path']},
            {'text': 'Write output.', 'quote': 'output.json', 'fields': ['/execution/output_path']},
            {'text': 'Inspect row values.', 'quote': 'rows', 'fields': ['/execution/mode']}],
        'unresolved': []})
    assert proposal['status'] == 'awaiting_confirmation', proposal
    report_ids = dict(session_id='review-session', task_id='review-task', turn_id='review-turn', api_request_id='api-5')
    report = skill_review['sdk'].middleware['llm_request'](request={'tools': []}, **report_ids)['request']
    assert report['tool_choice'] == 'auto'
    assert report['tools'] == []
    skill_review['store'].record('pre_api_request', {**report_ids, 'request': {'body': report}})
    skill_review['sdk'].middleware['llm_execution'](request=report, next_call=lambda value: value, **report_ids)
    skill_review['store'].record('post_api_request', {**report_ids, 'response': {'assistant_message': {'tool_calls': []}}})
    with pytest.raises(skill_review['api'].ReviewIngressBlocked):
        skill_review['sdk'].middleware['llm_request'](request={'tools': []},
            **{**report_ids, 'api_request_id': 'api-6'})
    verdict = skill_review['api'].evaluate_review_session(host_result(skill_review), skill_review['db'],
        skill_review['workspace'], catalog_root=skill_review['owned'])
    assert verdict['proposal_verified'] is True, verdict
    if corruption:
        with sqlite3.connect(skill_review['db']) as database:
            database.execute("DELETE FROM hermes_host_events WHERE kind='rpe_review_skill_restored'")
        changed = skill_review['api'].evaluate_review_session(host_result(skill_review), skill_review['db'],
            skill_review['workspace'], catalog_root=skill_review['owned'])
        assert changed['proposal_verified'] is False
        return
    proposals = ProposalStore(skill_review['db'])
    confirmed = proposals.confirm(proposal['proposal_id'], session_id='review-session',
        expected_proposal_sha256=proposal['proposal_sha256'], actor='simulated_fixture_reviewer')
    result = proposals.execute(confirmed['confirmation_id'], session_id='review-session',
        workspace=skill_review['workspace'], catalog_root=skill_review['owned'], python=sys.executable)
    assert result['status'] == 'succeeded', result
    from rpe_harness.integrations.hermes_gate import read_run
    durable = read_run(skill_review['db'], result['run_id'])
    receipts = [step['evidence']['helper_execution'] for step in durable['steps']
        if step['stage'] == 'execute' and 'helper_execution' in step.get('evidence', {})]
    assert len(receipts) == 1 and receipts[0]['helper_pid'] > 1
    assert receipts[0]['script_sha256'] == proposal['skill_selection']['script_sha256']
    assert json.loads((skill_review['workspace'] / 'output.json').read_text()) == {
        'sheet': 'Sheet', 'rows': [['id', 'flag'], [1, True]]}


@pytest.mark.parametrize('skill_review', [{'raw': RAW.replace(' by id', ''), 'sources': [
    {'source': 'attachment', 'event_id': 'business-key',
     'payload': {'text': 'The business key is id.', 'private_note': 'UNRELATED_PRIVATE_PAYLOAD'},
     'facts': {'key_columns': ['id']}}]}], indirect=True)
def test_current_session_sees_only_verified_source_facts_and_can_bind_them(skill_review):
    step(skill_review, 'rpe_discover', {'q': 'watercolor'})
    text = json.dumps(skill_review['request'])
    assert 'UNRELATED_PRIVATE_PAYLOAD' not in text
    assert 'key_columns' in text
    step(skill_review, 'rpe_select', {'skill_id': None})
    from rpe_harness.integrations.hermes_review import ReviewWorkflowStore
    state = ReviewWorkflowStore(skill_review['db']).get({'entry_key': 'review-entry',
        'session_id': 'review-session', 'turn_id': 'review-turn', 'task_id': 'review-task'})
    ref = state['review_context']['source_refs'][0]
    arguments = draft()
    row = next(row for row in arguments['requirements'] if row['fields'] == ['/execution/key_columns'])
    row.update(quote='id', source_ref=ref)
    result = step(skill_review, 'rpe_propose', arguments)
    assert result['status'] == 'awaiting_confirmation', result
    verdict = skill_review['api'].evaluate_review_session(host_result(skill_review), skill_review['db'],
        skill_review['workspace'], catalog_root=skill_review['owned'])
    assert verdict['proposal_verified'] is True, verdict


def test_existing_native_archive_row_is_not_moved_into_itself(skill_env):
    path = skill_env['source'] / '.archive/novel/SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: novel\ndescription: Write a long novel.\n---\n# Novel\n')
    skill_env['catalog'].sync(skill_env['source'])
    copy = skill_env['catalog'].materialize(skill_env['home'].parent / 'second-home/skills')
    prepared = skills_api().prepare_review_skills(copy)
    row = next(row for row in prepared['entries'] if row['name'] == 'novel')
    assert Path(row['archive_path']) == Path(prepared['materialized_path']) / '.archive/novel'
    assert Path(row['archive_path']).is_dir()
    assert not Path(row['native_skill_path']).exists()
