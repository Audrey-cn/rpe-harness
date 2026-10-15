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


def test_v113_requires_kernel_receipt_even_when_legacy_native_command_was_accepted(helper_execution_protocol):
    """No command grammar or printed exit status can replace a signed OS observation."""
    module = importlib.import_module('rpe_harness.experiments.review_native_evidence')
    evaluate = getattr(module, 'evaluate_native_review_execution_v113', None)
    assert callable(evaluate), '1.13 authenticated process exit gate is missing'
    plan, trial, session, source, command = helper_execution_protocol
    assert evaluate({'session_root': str(session)}, plan, trial)['verified'] is False


@pytest.fixture
def authenticated_native_execution(helper_execution_protocol, monkeypatch):
    """Real original helper/kernel proof; API IDs below are synthetic unit protocol."""
    from rpe_harness.integrations import hermes_native_process as process
    from rpe_harness.integrations.hermes import HostEventStore, register
    from rpe_harness.integrations.hermes_gate import digest_json
    plan, trial, session, source, command = helper_execution_protocol
    workspace = Path(trial['workspace'])
    (workspace/'output.json').unlink()
    case = next(row for row in plan['cases'] if row['case_id'] == trial['case_id'])
    config = {'workspace': str(workspace), 'helper_path': str(source.parent/'scripts/xlsx_read.py'),
        'skill_path': str(source), 'snapshot_id': plan['skill_snapshot_id'],
        'skill_id': 'productivity/xlsx', 'helper_python': plan['helper_python'],
        'helper_python_sha256': plan['environment']['helper_python_identity']['executable_sha256'],
        'skill_sha256': plan['skill_sha256'], 'script_sha256': plan['script_sha256'],
        'input_name': case['input_name'], 'input_sha256': trial['input_sha256'],
        'output_name': case['output_name'], 'parameters': case['parameters']}
    config_path = session/'native-execution-config.json'
    config_path.write_text(json.dumps(config))
    key = os.urandom(32)
    (session/'native-execution-auth.key').write_bytes(key)
    key_read, key_write = os.pipe()
    os.write(key_write, key); os.close(key_write)
    monkeypatch.setattr(process, '_SIGNING_KEY', None)
    monkeypatch.setenv('RPE_HERMES_NATIVE_EXECUTION_KEY_FD', str(key_read))
    process.install_native_execution_signing_key()
    assert 'RPE_HERMES_NATIVE_EXECUTION_KEY_FD' not in os.environ
    with pytest.raises(OSError): os.fstat(key_read)
    for name, value in {'RPE_HERMES_DB': trial['db'], 'RPE_HERMES_WORKSPACE': str(workspace),
        'RPE_HERMES_TOOL_ENABLED': '0', 'RPE_HERMES_NATIVE_EXECUTION_CONFIG': str(config_path)}.items():
        monkeypatch.setenv(name, value)
    class Context:
        def __init__(self): self.hooks = {}
        def register_hook(self, name, callback): self.hooks[name] = callback
    context = Context(); register(context)
    ids = {'session_id': 'authenticated-unit', 'task_id': 'task', 'turn_id': 'turn', 'api_request_id': 'api'}
    context.hooks['pre_api_request'](**ids)
    context.hooks['post_api_request'](**ids, response={'assistant_message': {'tool_calls': [
        {'id': 'view', 'name': 'skill_view'}, {'id': 'execute', 'name': 'terminal'}]}})
    view = {**ids, 'tool_call_id': 'view', 'tool_name': 'skill_view', 'args': {'name': 'xlsx'}}
    context.hooks['pre_tool_call'](**view)
    context.hooks['post_tool_call'](**view, status='loaded', skill_id='productivity/xlsx',
        snapshot_id=plan['skill_snapshot_id'], content_sha256=plan['skill_sha256'], skill_source_path=str(source))
    command = command.replace(' && echo "EXIT=$?" && wc', '; echo "exit=$?"; wc')
    execute = {**ids, 'tool_call_id': 'execute', 'tool_name': 'terminal', 'args': {'command': command}}
    context.hooks['pre_tool_call'](**execute)
    child = subprocess.run(['/bin/bash', '-c', command], cwd=workspace, capture_output=True, text=True,
        env={key: value for key, value in os.environ.items() if not key.startswith(('PYTHON', 'DYLD_', 'LD_'))})
    context.hooks['post_tool_call'](**execute, status='ok', result=json.dumps({'exit_code': child.returncode}))
    store = HostEventStore(trial['db'])
    result = {'session_id': ids['session_id'], 'session_root': str(session), 'events': store.list(ids['session_id']),
        'stream_tool_calls': [{'name': 'skill_view', 'input': view['args']}, {'name': 'terminal', 'input': execute['args']}]}
    from rpe_harness.experiments.review_native_evidence import evaluate_native_review_execution_v113
    baseline = evaluate_native_review_execution_v113(result, plan, trial)
    assert baseline['verified'] is True and baseline['receipt_authenticated'] is True, baseline
    return result, plan, trial, store


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
def test_v113_accepts_authenticated_kernel_exit_and_correct_original_helper_output(authenticated_native_execution):
    result, plan, trial, _store = authenticated_native_execution
    from rpe_harness.experiments.review_native_evidence import evaluate_native_review_execution_v113
    gate = evaluate_native_review_execution_v113(result, plan, trial)
    assert gate['verified'] is True and gate['receipt_authenticated'] is True, gate


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
@pytest.mark.parametrize('fault', ['forged_exit', 'wrong_call', 'wrong_receipt_args', 'wrong_input', 'wrong_output', 'wrong_config', 'wrong_key', 'business_overwrite'])
def test_v113_rejects_forged_receipts_changed_bytes_and_wrong_bindings(authenticated_native_execution, fault):
    result, plan, trial, store = authenticated_native_execution
    from rpe_harness.experiments.review_native_evidence import evaluate_native_review_execution_v113
    from rpe_harness.integrations.hermes_native_process import _digest, file_snapshot, receipt_signature
    workspace, session = Path(trial['workspace']), Path(result['session_root'])
    if fault in {'forged_exit', 'wrong_call', 'wrong_receipt_args'}:
        row = next(event for event in result['events'] if event['kind'] == 'native_helper_process_observation')
        field, value = {'forged_exit': ('process_returncode', 7), 'wrong_call': ('tool_call_id', 'other'),
                        'wrong_receipt_args': ('arguments_sha256', '0'*64)}[fault]
        with store._connect() as db:
            raw, = db.execute('SELECT metadata_json FROM hermes_host_events WHERE ingest_event_id=?', (row['ingest_event_id'],)).fetchone()
            metadata = json.loads(raw); metadata[field] = value
            db.execute('UPDATE hermes_host_events SET metadata_json=? WHERE ingest_event_id=?', (json.dumps(metadata), row['ingest_event_id']))
        result['events'] = store.list(result['session_id'])
    elif fault == 'wrong_input':
        (workspace/'input.xlsx').write_text('changed')
    elif fault == 'wrong_output':
        (workspace/'output.json').write_text('{}')
    elif fault == 'wrong_config':
        path = session/'native-execution-config.json'; config = json.loads(path.read_text()); config['parameters']['sheet'] = 'wrong'; path.write_text(json.dumps(config))
    elif fault == 'wrong_key':
        (session/'native-execution-auth.key').write_bytes(b'0'*32)
    elif fault == 'business_overwrite':
        # Even an authenticated process exit does not establish exclusive output
        # authorship. Re-signing here models a real final-byte observation of a
        # shell rewrite; the independent business oracle must still reject it.
        (workspace/'output.json').write_text('{}')
        row = next(event for event in result['events'] if event['kind'] == 'native_helper_process_observation')
        with store._connect() as db:
            raw, = db.execute('SELECT metadata_json FROM hermes_host_events WHERE ingest_event_id=?', (row['ingest_event_id'],)).fetchone()
            metadata = json.loads(raw); out = file_snapshot(workspace/'output.json')
            metadata.update(output_sha256=out['sha256'], output_size=out['size'], output_identity_sha256=_digest(out['identity']))
            metadata.pop('process_receipt_hmac_sha256')
            metadata['process_receipt_hmac_sha256'] = receipt_signature(metadata, (session/'native-execution-auth.key').read_bytes())
            db.execute('UPDATE hermes_host_events SET metadata_json=? WHERE ingest_event_id=?', (json.dumps(metadata), row['ingest_event_id']))
        result['events'] = store.list(result['session_id'])
    gate = evaluate_native_review_execution_v113(result, plan, trial)
    assert gate['verified'] is False, gate
    if fault == 'business_overwrite':
        assert gate['errors'] == ['native_process_final_output_business_incorrect'], gate


@pytest.mark.skipif(os.environ.get('RPE_RUN_INSTALLED_NATIVE_SMOKE') != '1',
                    reason='explicit provider-free installed native session smoke')
def test_v113_session_constructor_clean_sandbox_key_pipe_and_installed_terminal(helper_execution_protocol, monkeypatch, tmp_path):
    """Real launcher/sandbox/bootstrap/tools; only CLI provider dispatch is replaced.

    Direct native hooks use explicit smoke identities. This is runtime readiness,
    not an API/model success result or a formal comparison trial.
    """
    from rpe_harness.integrations import hermes_session as launcher
    plan, trial, _session, _source, _command = helper_execution_protocol
    workspace = Path(trial['workspace']); (workspace/'output.json').unlink()
    case = next(row for row in plan['cases'] if row['case_id'] == trial['case_id'])
    original_popen = subprocess.Popen
    program = r'''import importlib.util,json,os,shlex,socket,subprocess,sys
from pathlib import Path
assert importlib.util.find_spec('rpe_harness') is None,'Harness preloaded at file entry'
initial_pythonpath=os.environ.get('PYTHONPATH')
expected_json=json.loads(sys.argv[2])
key_fd=int(os.environ['RPE_HERMES_NATIVE_EXECUTION_KEY_FD'])
def deny_network(self,address): raise AssertionError('network disabled in keyed native smoke')
socket.socket.connect=deny_network
spec=importlib.util.spec_from_file_location('actual_keyed_file_bootstrap',sys.argv[1])
bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)

def smoke():
 from rpe_harness.integrations.hermes import register,HostEventStore
 from rpe_harness.integrations import hermes_native_process as process
 from tools.terminal_tool import terminal_tool
 assert process._SIGNING_KEY is not None
 assert process is sys.modules['rpe_harness.integrations.hermes_native_process']
 assert 'RPE_HERMES_NATIVE_EXECUTION_KEY_FD' not in os.environ
 assert os.environ.get('PYTHONPATH')==initial_pythonpath
 assert os.environ['RPE_HARNESS_IMPORT_ROOT'] not in sys.path
 try: os.fstat(key_fd)
 except OSError: pass
 else: raise AssertionError('key descriptor remains open')
 probe=subprocess.run([sys.executable,'-B','-c',
  "import importlib.util,os;assert importlib.util.find_spec('rpe_harness') is None;assert 'RPE_HERMES_NATIVE_EXECUTION_KEY_FD' not in os.environ"],capture_output=True,text=True,timeout=5)
 assert probe.returncode==0,probe.stderr
 config_path=Path(os.environ['RPE_HERMES_NATIVE_EXECUTION_CONFIG'])
 config=json.loads(config_path.read_text())
 key_path=config_path.parent/'native-execution-auth.key'
 for mode in ('rb','wb'):
  try:
   with key_path.open(mode): pass
  except PermissionError: pass
  else: raise AssertionError('native session can access protected receipt key')
 class Context:
  def __init__(self): self.hooks={}
  def register_hook(self,n,c): self.hooks[n]=c
 context=Context(); register(context)
 serial=0
 def invoke(command):
  nonlocal serial
  serial+=1
  ids=dict(session_id='native-process-installed',task_id='native-process-smoke',turn_id='turn',api_request_id='api',tool_call_id=str(serial),tool_name='terminal',args={'command':command})
  context.hooks['pre_tool_call'](**ids)
  raw=terminal_tool(command,task_id='native-process-smoke',timeout=20)
  context.hooks['post_tool_call'](**ids,status='ok',result=raw)
  return json.loads(raw)
 argv=[config['helper_python'],'-B',config['helper_path'],config['input_name'],'--json','--sheet',config['parameters']['sheet']]
 command=shlex.join(argv)+' > output.json'
 success=invoke(command+'; echo "exit=$?"; wc -c output.json')
 Path('output.json').unlink()
 failure=invoke('(ulimit -f 0; '+command+')')
 Path('output.json').unlink()
 masked=invoke('(ulimit -f 0; '+command+'); echo exit=0; wc -c output.json')
 Path('output.json').unlink()
 injected=Path('injected'); injected.mkdir()
 (injected/'sitecustomize.py').write_text('import os\nos._exit(0)\n')
 injection=invoke('PYTHONPATH='+shlex.quote(str(injected.resolve()))+' '+command+'; echo exit=0')
 Path('output.json').unlink()
 final=invoke(command)
 assert final['exit_code']==0,final
 assert json.loads(Path('output.json').read_text())==expected_json
 events=HostEventStore(os.environ['RPE_HERMES_DB']).list('native-process-installed')
 rows=[event for event in events if event['kind']=='native_helper_process_observation']
 assert len(rows)==5,rows
 assert rows[0]['verified'] is True and rows[0]['process_returncode']==0,rows
 assert rows[1]['verified'] is False and rows[1]['process_returncode']!=0,rows
 assert rows[2]['verified'] is False and rows[2]['process_returncode']!=0,rows
 assert rows[3]['verified'] is False,rows
 assert rows[4]['verified'] is True and rows[4]['process_returncode']==0,rows
 assert success['exit_code']==0 and failure['exit_code']!=0 and masked['exit_code']==0, [success,failure,masked]
 for row in rows:
  signature=row['process_receipt_hmac_sha256']
  body={k:v for k,v in row.items() if k not in {'kind','received_at','source_event_id','ingest_event_id','process_receipt_hmac_sha256'}}
  assert signature==process.receipt_signature(body,process._SIGNING_KEY),row
 assert 'rpe_harness.controller' not in sys.modules
 summary={'provider_calls':0,'native_success':True,'native_failure':True,'masked_failure_rejected':True,'startup_injection_rejected':True,'key_read_write_denied':True,'key_fd_consumed':True,'signed_receipts':5,'business_oracle_matched':True,'child_harness_importable':False,'kernel_returncodes':[r.get('process_returncode') for r in rows],'shell_returncodes':[success['exit_code'],failure['exit_code'],masked['exit_code'],injection['exit_code']]}
 Path('native-process-smoke-summary.json').write_text(json.dumps(summary))
 HostEventStore(os.environ['RPE_HERMES_DB']).record('on_session_end',{'session_id':'native-process-installed','completed':True})
 print(json.dumps({'type':'result','session_id':'native-process-installed','text':'provider-free native process smoke'}))
 return 0
original_import=bootstrap.importlib.import_module
def dispatch(name,package=None):
 module=original_import(name,package=package)
 if name=='hermes_cli.main':
  bootstrap.importlib.import_module=original_import
  module.main=smoke
 return module
bootstrap.importlib.import_module=dispatch
raise SystemExit(bootstrap.main(['--help']))
'''
    def launch(command, *args, **kwargs):
        if isinstance(command, list) and command[:1] == ['/usr/bin/sandbox-exec']:
            # Preserve the constructor's sandbox, environment, stdout/stderr,
            # process group and inherited key FD. Replace provider entry only.
            command = command[:5] + ['-c', program, command[5], json.dumps(case['expected'])]
        return original_popen(command, *args, **kwargs)
    monkeypatch.setattr(launcher.subprocess, 'Popen', launch)
    monkeypatch.setattr(launcher, '_read_host_settings', lambda *_: {
        'provider': 'xiaomi', 'model': 'provider-free-smoke', 'base_url': 'https://example.invalid',
        'api_key': 'unused-native-process-smoke-key'})
    options = {'skill_id': 'productivity/xlsx', 'helper_python': plan['helper_python'],
        'helper_python_sha256': plan['environment']['helper_python_identity']['executable_sha256'],
        'skill_sha256': plan['skill_sha256'], 'script_sha256': plan['script_sha256'],
        'input_name': case['input_name'], 'input_sha256': trial['input_sha256'],
        'output_name': case['output_name'], 'parameters': case['parameters']}
    result = launcher.run_hermes_session('provider-free native process constructor smoke', workspace, trial['db'],
        tool_enabled=False, owner_root=Path(plan['root'])/'sessions', hermes_root=Path.home()/'.hermes/hermes-agent',
        catalog_root=plan['catalog_root'], skill_snapshot_id=plan['skill_snapshot_id'], timeout=70,
        public_contract='clean', native_execution_evidence=options)
    session = Path(result['session_root'])
    (tmp_path/'native-process-constructor-result.json').write_text(json.dumps(result, ensure_ascii=False))
    assert result['completed'] is True, (session/'stderr.log').read_text()+'\n'+(session/'stdout.jsonl').read_text()
    summary = json.loads((workspace/'native-process-smoke-summary.json').read_text())
    assert summary['provider_calls'] == 0 and summary['signed_receipts'] == 5
    assert summary['business_oracle_matched'] is True and summary['child_harness_importable'] is False
    from rpe_harness.experiments.address_comparison import score_address_artifact
    assert score_address_artifact(plan, trial)['business_correct'] is True
    assert result['process_cleanup']['cleanup_complete'] is True, result['process_cleanup']
