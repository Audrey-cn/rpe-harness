"""Observer-only native evidence; all tests and installed smoke are provider-free."""
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def observer(tmp_path):
    module = importlib.import_module('rpe_harness.integrations.hermes_native')
    workspace = tmp_path/'workspace'
    workspace.mkdir()
    return module.NativeEvidenceObserver({'workspace': str(workspace), 'draft_name': 'draft.json'}), workspace


def call(number='1', name='terminal'):
    return {'session_id': 'unit-session', 'task_id': 'task', 'turn_id': 'turn',
            'api_request_id': 'api', 'tool_call_id': number, 'tool_name': name,
            'arguments_sha256': 'a'*64, 'status': 'succeeded', 'tool_exit_code': 0,
            'result': '{"exit_code":0}'}


def test_native_receipt_requires_actual_new_regular_bytes(tmp_path):
    watch, workspace = observer(tmp_path)
    payload = call()
    watch.before(payload)
    assert watch.after(payload) == []  # successful command mentioning a file proves no write
    watch.before(call('2'))
    (workspace/'draft.json').write_text('{"draft":true}')
    receipt, = watch.after(call('2'))
    assert receipt['kind'] == 'native_artifact_observation'
    assert receipt['status'] == 'created' and receipt['verified'] is True
    assert receipt['bytes_written'] == 14 and receipt['tool_call_id'] == '2'
    assert len(receipt['content_sha256']) == 64


def test_native_receipt_rejects_concurrent_writes_and_symlinks(tmp_path):
    watch, workspace = observer(tmp_path)
    watch.before(call('1'))
    watch.before(call('2'))
    (workspace/'draft.json').write_text('{}')
    assert watch.after(call('1'))[0]['verified'] is False
    assert watch.after(call('2'))[0]['verified'] is False
    (workspace/'draft.json').unlink()
    (workspace/'other.json').write_text('{}')
    (workspace/'draft.json').symlink_to(workspace/'other.json')
    watch.before(call('3'))
    assert watch.after(call('3'))[0]['status'] == 'invalid'


def test_native_receipt_records_rewrite_even_when_bytes_are_identical(tmp_path):
    watch, workspace = observer(tmp_path)
    (workspace/'draft.json').write_text('{}')
    watch.before(call())
    (workspace/'draft.json').write_text('{}')
    receipt, = watch.after(call())
    assert receipt['status'] == 'modified'


def test_native_receipt_does_not_call_failed_tool_io_verified(tmp_path):
    watch, workspace = observer(tmp_path)
    payload = {**call(name='write_file'), 'status': 'ok', 'result': '{"error":"write failed"}'}
    watch.before(payload)
    (workspace/'draft.json').write_text('{}')
    assert watch.after(payload)[0]['verified'] is False


def test_config_rejects_arbitrary_paths(tmp_path):
    module = importlib.import_module('rpe_harness.integrations.hermes_native')
    with pytest.raises(ValueError):
        module.NativeEvidenceObserver({'workspace': str(tmp_path), 'draft_name': '../answer.json'})


def test_cold_configuration_cannot_expand_to_arbitrary_snapshot_paths(tmp_path):
    module = importlib.import_module('rpe_harness.integrations.hermes_native')
    snapshot = {'snapshot_id': 's', 'materialized_path': str(tmp_path/'home/skills'), 'entries': [
        {'skill_id': 'test', 'native_load_name': 'xlsx', 'archive_path': '/unrelated/.archive/xlsx',
         'native_skill_path': '/unrelated/xlsx/SKILL.md', 'skill_sha256': 'a'*64, 'script_sha256': 'b'*64}]}
    with pytest.raises(ValueError):
        module.native_evidence_config(tmp_path, {'draft_name': 'draft.json', 'cold_skill_id': 'test'}, snapshot)


def test_native_prompt_publishes_original_anchors_and_support_boundary():
    module = importlib.import_module('rpe_harness.experiments.review_native_evidence')
    from rpe_harness.review_quotes import request_quote_anchors
    raw = '请只读检查 input.xlsx，保存 output.json。'
    prompt = module.native_review_prompt(raw, cold_skill=True, host_python='/host/python', hermes_root='/host/hermes')
    assert prompt.startswith(raw)
    assert 'support_metadata' in prompt and 'original_request' in prompt
    assert all(anchor['quote_ref'] in prompt for anchor in request_quote_anchors(raw))


def test_native_trace_resolves_explicit_anchor_but_never_support_text():
    module = importlib.import_module('rpe_harness.experiments.review_native_evidence')
    from rpe_harness.review_quotes import request_quote_anchors
    raw = 'input.csv 去重并另存 output.csv'
    draft = {'capability': 'tabular.deduplicate', 'constraints': [], 'execution': {},
             'requirements': [{'text': 'scope', 'quote_ref': request_quote_anchors(raw)[0]['quote_ref'],
                               'fields': ['/capability', '/constraints']}]}
    assert module.native_draft_trace(raw, draft)
    draft['requirements'][0]['quote'] = 'skill supply instruction'
    assert not module.native_draft_trace(raw, draft)


def test_cold_proof_missing_observations_has_explicit_diagnostic():
    module = importlib.import_module('rpe_harness.experiments.review_native_evidence')
    result = module.native_cold_proof({}, {}, {'db': '/not-a-db'})
    assert result['verified'] is False and result['restore_calls'] == 0
    assert result['native_view_calls'] == 0 and result['errors']


@pytest.mark.parametrize('wrapper,expected', [('env', True), ('cd_env', True),
    ('wrong_cwd', False), ('wrong_env', False), ('shell_tail', False)])
def test_cold_proof_exact_workspace_wrapper_precedes_exact_env(tmp_path, wrapper, expected):
    """Synthetic durable protocol fixture for parsing only; never runtime evidence."""
    import hashlib
    import shlex
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.integrations.hermes_gate import digest_json
    from rpe_harness.skill_catalog import XLSX_SKILL_ID
    module = importlib.import_module('rpe_harness.experiments.review_native_evidence')
    workspace, hermes_root = tmp_path/'workspace', tmp_path/'hermes'
    workspace.mkdir()
    command = shlex.join(['env', 'PYTHONPATH='+str(hermes_root), sys.executable,
        '-B', '-m', 'hermes_cli.main', 'curator', 'restore', 'xlsx'])
    if wrapper in {'cd_env', 'wrong_cwd'}:
        cwd = workspace if wrapper == 'cd_env' else tmp_path
        command = 'cd '+shlex.quote(str(cwd))+' && '+command
    if wrapper == 'wrong_env':
        command = command.replace('PYTHONPATH='+str(hermes_root), 'PYTHONPATH=/unapproved')
    if wrapper == 'shell_tail':
        command += ' ; true'
    restore_args, view_args = {'command': command}, {'name': 'xlsx'}
    store = HostEventStore(tmp_path/'events.sqlite')
    ids = {'session_id': 'unit-session', 'task_id': 'task', 'turn_id': 'turn', 'api_request_id': 'api'}
    restore = {**ids, 'tool_call_id': 'restore', 'tool_name': 'terminal',
               'arguments_sha256': digest_json(restore_args)}
    view = {**ids, 'tool_call_id': 'view', 'tool_name': 'skill_view',
            'arguments_sha256': digest_json(view_args)}
    skill = {'skill_id': XLSX_SKILL_ID, 'snapshot_id': 'snapshot', 'skill_sha256': 'a'*64,
             'script_sha256': 'b'*64}
    store.record('pre_api_request', ids)
    store.record('post_api_request', {**ids, 'response': {'assistant_message': {'tool_calls': [
        {'id': 'restore', 'name': 'terminal'}, {'id': 'view', 'name': 'skill_view'}]}}})
    store.record('pre_tool_call', restore)
    store.record('post_tool_call', {**restore, 'status': 'succeeded', 'tool_exit_code': 0})
    store.record('native_cold_observation', {**restore, **skill, 'status': 'restored', 'verified': True})
    store.record('pre_tool_call', view)
    store.record('post_tool_call', {**view, **skill, 'status': 'loaded', 'content_sha256': 'a'*64})
    result = {'session_id': ids['session_id'], 'events': store.list(ids['session_id']),
              'stream_tool_calls': [{'name': 'terminal', 'input': restore_args},
                                    {'name': 'skill_view', 'input': view_args}]}
    plan = {'hermes_root': str(hermes_root), 'skill_snapshot_id': 'snapshot',
        'skill_sha256': 'a'*64, 'script_sha256': 'b'*64, 'environment': {'host_runtime': {
            'selected_python': {'executable_path_sha256': hashlib.sha256(sys.executable.encode()).hexdigest()}}}}
    proof = module.native_cold_proof(result, plan, {'db': str(store.path), 'workspace': str(workspace)})
    assert proof['verified'] is expected, proof


def _observed_result(tmp_path, monkeypatch, commands):
    """Synthetic API identities around real local IO, for gate unit tests only."""
    integration = importlib.import_module('rpe_harness.integrations.hermes')
    workspace = tmp_path/'workspace'
    workspace.mkdir()
    db = tmp_path/'events.sqlite'
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'workspace': str(workspace), 'draft_name': 'draft.json'}))
    monkeypatch.setenv('RPE_HERMES_DB', str(db))
    monkeypatch.setenv('RPE_HERMES_WORKSPACE', str(workspace))
    monkeypatch.setenv('RPE_HERMES_TOOL_ENABLED', '0')
    monkeypatch.setenv('RPE_HERMES_NATIVE_EVIDENCE_CONFIG', str(config))
    class Context:
        def __init__(self): self.hooks = {}
        def register_hook(self, name, callback): self.hooks[name] = callback
    ctx = Context()
    integration.register(ctx)
    identity = {key: call()[key] for key in ('session_id', 'task_id', 'turn_id', 'api_request_id')}
    ctx.hooks['pre_api_request'](**identity)
    ctx.hooks['post_api_request'](**identity, response={'assistant_message': {'tool_calls': [
        {'id': str(index), 'name': 'terminal'} for index in range(len(commands))]}})
    streams = []
    for index, function in enumerate(commands):
        args = {'command': 'unit command draft.json'}
        payload = {**identity, 'tool_call_id': str(index), 'tool_name': 'terminal', 'args': args}
        ctx.hooks['pre_tool_call'](**payload)
        function(workspace)
        ctx.hooks['post_tool_call'](**payload, status='ok', result='{"exit_code":0}')
        streams.append({'name': 'terminal', 'input': args})
    return {'session_id': identity['session_id'], 'events': integration.HostEventStore(db).list(identity['session_id']),
            'stream_tool_calls': streams}, {'db': str(db), 'workspace': str(workspace)}


def test_strict_gate_accepts_actual_native_io_and_detects_final_tamper(tmp_path, monkeypatch):
    module = importlib.import_module('rpe_harness.experiments.review_native_evidence')
    result, trial = _observed_result(tmp_path, monkeypatch, [lambda w: (w/'draft.json').write_text('{}')])
    assert module.evaluate_native_review_draft(result, {}, trial)['verified'] is True
    (Path(trial['workspace'])/'draft.json').write_text('{"fake":true}')
    assert module.evaluate_native_review_draft(result, {}, trial)['errors'] == ['native_draft_final_bytes_changed']


@pytest.mark.parametrize('commands', [[lambda w: None],
    [lambda w: (w/'draft.json').write_text('{}'), lambda w: (w/'draft.json').write_text('{}')]])
def test_strict_gate_rejects_mention_only_and_duplicate_writes(tmp_path, monkeypatch, commands):
    module = importlib.import_module('rpe_harness.experiments.review_native_evidence')
    result, trial = _observed_result(tmp_path, monkeypatch, commands)
    result['final_response'] = '{}'
    assert module.evaluate_native_review_draft(result, {}, trial)['verified'] is False


@pytest.fixture
def helper_execution_protocol(tmp_path):
    """Real local original-helper bytes with synthetic, explicitly unit-only API identities."""
    import shlex
    from rpe_harness.experiments.address_comparison import preregister_address_comparison
    from rpe_harness.skill_catalog import SkillCatalog
    skills = tmp_path/'skills'
    shutil.copytree(Path(__file__).parent/'fixtures/skills/productivity/xlsx', skills/'productivity/xlsx')
    plan = preregister_address_comparison(tmp_path/'comparison', skill_root=skills,
        hermes_root=tmp_path/'absent-host', timeout=60, max_turns=6)
    trial = next(t for t in plan['sessions'] if t['arm'] == 'native' and t['case_id'] == 'xlsx-original')
    session = Path(plan['root'])/'sessions/helper-execution-unit'
    materialized = SkillCatalog(trial['db'], plan['catalog_root']).materialize(
        session/'home/skills', plan['skill_snapshot_id'])
    source = Path(materialized['materialized_path'])/'productivity/xlsx/SKILL.md'
    argv = [plan['helper_python'], str(source.parent/'scripts/xlsx_read.py'),
            'input.xlsx', '--json', '--sheet', '明细']
    command = ('cd '+shlex.quote(trial['workspace'])+' && '+shlex.join(argv)+
               ' > output.json && echo "EXIT=$?" && wc -c output.json')
    executed = subprocess.run(command, shell=True, cwd=trial['workspace'], capture_output=True, text=True, check=True)
    assert executed.stdout.startswith('EXIT=0\n')
    return plan, trial, session, source, command


@pytest.mark.parametrize('fault', [None, 'other_output', 'extra_code', 'substitution',
    'wrong_sheet', 'stream_tamper', 'view_after_execute'])
def test_native_execution_accepts_only_exact_readonly_diagnostic_suffix(helper_execution_protocol, fault):
    import copy
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.integrations.hermes_gate import digest_json
    module = importlib.import_module('rpe_harness.experiments.review_native_evidence')
    assert callable(getattr(module, 'evaluate_native_review_execution', None))
    plan, trial, session, source, command = helper_execution_protocol
    if fault == 'other_output':
        command = command.removesuffix('wc -c output.json')+'wc -c other.json'
    elif fault == 'extra_code':
        command += ' && touch output.json'
    elif fault == 'substitution':
        command = command.replace('EXIT=$?', 'EXIT=$(true)')
    elif fault == 'wrong_sheet':
        command = command.replace('明细', '错误')
    load_args, execute_args = {'name': 'xlsx'}, {'command': command}
    store = HostEventStore(trial['db'])
    ids = {'session_id': 'helper-unit', 'task_id': 'task', 'turn_id': 'turn', 'api_request_id': 'api'}
    store.record('pre_api_request', ids)
    store.record('post_api_request', {**ids, 'response': {'assistant_message': {'tool_calls': [
        {'id': 'view', 'name': 'skill_view'}, {'id': 'execute', 'name': 'terminal'}]}}})
    load = {**ids, 'tool_call_id': 'view', 'tool_name': 'skill_view', 'arguments_sha256': digest_json(load_args)}
    execute = {**ids, 'tool_call_id': 'execute', 'tool_name': 'terminal', 'arguments_sha256': digest_json(execute_args)}
    calls = [(load, {**load, 'status': 'loaded', 'skill_id': 'productivity/xlsx',
        'snapshot_id': plan['skill_snapshot_id'], 'skill_source_path': str(source), 'content_sha256': plan['skill_sha256']}),
        (execute, {**execute, 'status': 'succeeded', 'tool_exit_code': 0})]
    for pre, post in calls if fault != 'view_after_execute' else reversed(calls):
        store.record('pre_tool_call', pre)
        store.record('post_tool_call', post)
    result = {'session_id': ids['session_id'], 'session_root': str(session), 'events': store.list(ids['session_id']),
              'stream_tool_calls': [{'name': 'skill_view', 'input': load_args}, {'name': 'terminal', 'input': execute_args}]}
    if fault == 'stream_tamper':
        result['stream_tool_calls'][-1]['input'] = {'command': command+' && true'}
    before = copy.deepcopy(result)
    gate = module.evaluate_native_review_execution(result, plan, trial)
    assert gate['verified'] is (fault is None), gate
    assert result == before  # bind original full command; never rewrite model arguments to fit legacy parser


@pytest.mark.skipif(os.environ.get('RPE_RUN_INSTALLED_NATIVE_SMOKE') != '1',
                    reason='explicit provider-free installed Hermes smoke')
def test_installed_native_file_terminal_and_cold_restore_under_real_sandbox(tmp_path):
    """Invoke installed tools directly; this establishes IO readiness, never model success."""
    from rpe_harness.integrations.hermes_session import _installed_python, prepare_home, sandbox_profile
    from rpe_harness.integrations.hermes_native import native_evidence_config
    from rpe_harness.integrations.review_skills import prepare_review_skills
    from rpe_harness.skill_catalog import SkillCatalog, XLSX_SKILL_ID
    hermes_root = Path.home()/'.hermes/hermes-agent'
    host_python = _installed_python(hermes_root)
    workspace, session = tmp_path/'workspace', tmp_path/'session'
    workspace.mkdir()
    (workspace/'input.csv').write_text('id,value\n001,first\n')
    home = prepare_home(session, workspace, tmp_path/'events.sqlite', tool_enabled=False,
        settings={'provider': 'xiaomi', 'model': 'provider-free-smoke',
                  'base_url': 'https://example.invalid', 'api_key': 'unused-provider-free'},
        public_contract='review', preserve_native_tool_search=True)
    for name in ('tmp', 'runtime', 'terminal-tmp'):
        (session/name).mkdir()
    skills = tmp_path/'source-skills'
    shutil.copytree(Path(__file__).parent/'fixtures/skills/productivity/xlsx', skills/'productivity/xlsx')
    catalog = SkillCatalog(tmp_path/'catalog.sqlite', tmp_path/'catalog')
    snapshot = catalog.sync(skills)['snapshot_id']
    catalog.set_temperature(XLSX_SKILL_ID, 'cold')
    materialized = prepare_review_skills(catalog.materialize(home/'skills', snapshot_id=snapshot))
    (home/'.no-bundled-skills').touch()
    config = session/'native-evidence.json'
    config.write_text(json.dumps(native_evidence_config(workspace,
        {'draft_name': 'draft.json', 'cold_skill_id': XLSX_SKILL_ID}, materialized)))
    source = Path(__import__('rpe_harness').__file__).resolve().parent.parent
    profile = session/'native.sb'
    profile.write_text(sandbox_profile([session, workspace], Path.home()/'.hermes', hermes_root,
        tmp_path/'events.sqlite', [host_python.parent.parent, Path.home()/'.hermes/tools'], native_import_root=source,
        protected_write_roots=[config]))
    program = '''import json,sys,shlex
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from tools.file_tools import read_file_tool,write_file_tool
from tools.terminal_tool import terminal_tool
from tools.skills_tool import skill_view
from rpe_harness.integrations.hermes import register,HostEventStore
class Context:
 def __init__(self): self.hooks={}
 def register_hook(self,n,c): self.hooks[n]=c
c=Context();register(c)
serial=0
def invoke(name,args,function):
 global serial
 serial+=1
 ids=dict(session_id='installed-smoke',task_id='smoke',turn_id='turn',api_request_id='api',tool_call_id=str(serial),tool_name=name,args=args)
 c.hooks['pre_tool_call'](**ids)
 raw=function();out=json.loads(raw)
 c.hooks['post_tool_call'](**ids,status='ok',result=raw)
 return out
read=read_file_tool('input.csv',task_id='smoke')
assert '001,first' in read,read
file=invoke('write_file',dict(path='draft.json',content='{}'),lambda:write_file_tool('draft.json','{}',task_id='smoke'))
assert file.get('verified') is True,file
Path('draft.json').unlink()
command=shlex.join([sys.executable,'-B','-c',"from pathlib import Path; Path('draft.json').write_text('{}')"])
terminal=invoke('terminal',dict(command=command),lambda:terminal_tool(command,task_id='smoke',timeout=20))
assert terminal.get('exit_code')==0,terminal
command=shlex.join(['env','PYTHONPATH='+sys.argv[2],sys.executable,'-B','-m','hermes_cli.main','curator','restore','xlsx'])
restore=invoke('terminal',dict(command=command),lambda:terminal_tool(command,task_id='smoke',timeout=20))
assert restore.get('exit_code')==0,restore
view=json.loads(skill_view('xlsx',preprocess=False,task_id='smoke'))
assert view.get('success') is True and view.get('_source_path')==str(Path(sys.argv[3])/'skills/xlsx/SKILL.md'),view
events=HostEventStore(sys.argv[4]).list('installed-smoke')
assert len([e for e in events if e['kind']=='native_artifact_observation' and e.get('verified')])==2,events
assert len([e for e in events if e['kind']=='native_cold_observation' and e.get('verified')])==1,events
assert 'rpe_harness.controller' not in sys.modules
print(json.dumps(dict(provider_calls=0,read=True,file_write=True,terminal_write=True,cold_restore=True,native_skill_view=True,receipt_events=len(events))))
'''
    env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(session/'runtime'),
        'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_DISABLE_LAZY_INSTALLS': '1',
        'HERMES_ENABLE_PROJECT_PLUGINS': '0', 'TMPDIR': str(session/'tmp'),
        'PYTHONPATH': str(hermes_root), 'RPE_HARNESS_IMPORT_ROOT': str(source),
        'RPE_HERMES_TOOL_ENABLED': '0', 'RPE_HERMES_WORKSPACE': str(workspace),
        'RPE_HERMES_DB': str(tmp_path/'events.sqlite'), 'RPE_HERMES_NATIVE_EVIDENCE_CONFIG': str(config)}
    child = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), str(host_python), '-B', '-c', program,
        str(source), str(hermes_root), str(home), str(tmp_path/'events.sqlite')],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=55)
    (tmp_path/'installed-smoke-result.json').write_text(json.dumps({
        'exit_code': child.returncode, 'stdout': child.stdout, 'stderr': child.stderr}, ensure_ascii=False))
    assert child.returncode == 0, child.stderr+'\n'+child.stdout
    assert json.loads(child.stdout)['provider_calls'] == 0
