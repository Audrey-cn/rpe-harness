"""Signed business IO evidence; local writes and durable protocol, no provider."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest


def modules():
    from rpe_harness.integrations import hermes_native, hermes_native_process
    from rpe_harness.experiments import review_native_evidence
    return hermes_native, hermes_native_process, review_native_evidence


def install_key(process, monkeypatch, key):
    monkeypatch.setattr(process, '_SIGNING_KEY', None)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, key)
    os.close(write_fd)
    monkeypatch.setenv('RPE_HERMES_NATIVE_EXECUTION_KEY_FD', str(read_fd))
    process.install_native_execution_signing_key()


def test_business_observer_accepts_only_explicit_fixed_output_names(tmp_path):
    native, _, _ = modules()
    config = native.native_evidence_config(tmp_path, {'artifact_name': 'output.csv'})
    assert config == {'workspace': str(tmp_path), 'artifact_name': 'output.csv'}
    for options in ({'artifact_name': '../output.csv'}, {'artifact_name': '/output.csv'},
                    {'artifact_name': 'input.csv'}, {'artifact_name': 'draft.json'},
                    {'artifact_name': 'output.csv', 'draft_name': 'draft.json'},
                    {'artifact_name': 'output.json', 'cold_skill_id': 'xlsx'}):
        with pytest.raises(ValueError):
            native.native_evidence_config(tmp_path, options)


@pytest.fixture
def artifact_case(tmp_path, monkeypatch):
    """Hand-checked TSV business truth around real hook IO; API IDs are unit-only."""
    from rpe_harness.integrations import hermes
    from rpe_harness.tasks import plan_from_request
    native, process, evidence = modules()
    root, workspace = tmp_path/'comparison', tmp_path/'workspace'
    workspace.mkdir()
    session = root/'sessions/native-execute/session'
    session.mkdir(parents=True)
    source = b'code\tnote\nA\tfirst\nB\tsecond\nA\tlater\n'
    output = b'code\tnote\nA\tfirst\nB\tsecond\n'
    (workspace/'input.csv').write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()
    case = {'case_id': 'tabs', 'capability': 'tabular.deduplicate', 'input_name': 'input.csv',
        'output_name': 'output.csv', 'expected_status': 'ready', 'input_sha256': digest,
        'expected_rows': [['code', 'note'], ['A', 'first'], ['B', 'second']],
        'parameters': {'key_columns': ['code'], 'delimiter': '\t', 'encoding': 'utf-8', 'keep': 'first'}}
    request = {'schema_version': 2, 'event_id': 'unit', 'capability': 'tabular.deduplicate',
        'goal': 'Deduplicate exact code strings and preserve the first row and order.',
        'constraints': ['preserve_original', 'preserve_order'],
        'execution': {'input_path': str(workspace/'input.csv'), 'output_path': str(workspace/'output.csv'),
                      **case['parameters']}}
    trial = {'case_id': 'tabs', 'workspace': str(workspace), 'db': str(tmp_path/'events.sqlite'),
        'input_sha256': digest, 'oracle_plan': plan_from_request(request).to_dict()}
    plan = {'root': str(root), 'cases': [case]}
    config = {'workspace': str(workspace), 'artifact_name': 'output.csv'}
    (session/'native-evidence-config.json').write_text(json.dumps(config))
    key = bytes(range(32))
    (session/'native-execution-auth.key').write_bytes(key)
    install_key(process, monkeypatch, key)
    monkeypatch.setenv('RPE_HERMES_DB', trial['db'])
    monkeypatch.setenv('RPE_HERMES_WORKSPACE', str(workspace))
    monkeypatch.setenv('RPE_HERMES_TOOL_ENABLED', '0')
    monkeypatch.setenv('RPE_HERMES_NATIVE_EVIDENCE_CONFIG', str(session/'native-evidence-config.json'))
    class Context:
        def __init__(self): self.hooks = {}
        def register_hook(self, name, callback): self.hooks[name] = callback
    context = Context()
    # This intentionally runs the production hook registration and event store.
    context_ready = False
    def run(tool='write_file', action='create', exit_code=0, duplicate=False):
        nonlocal context_ready
        if not context_ready:
            hermes.register(context)
            context_ready = True
        if action == 'existing':
            (workspace/'output.csv').write_bytes(output)
        ids = {'session_id': 'unit-native', 'task_id': 'task', 'turn_id': 'turn', 'api_request_id': 'api'}
        args = ({'path': 'output.csv', 'content': output.decode()} if tool == 'write_file' else
                {'mode': 'replace', 'path': 'output.csv', 'old_string': 'x', 'new_string': 'y'}
                if tool == 'patch' else {'command': 'execute prepared local job'})
        context.hooks['pre_api_request'](**ids)
        context.hooks['post_api_request'](**ids, response={'assistant_message': {
            'tool_calls': [{'id': 'write', 'name': tool}]}})
        call = {**ids, 'tool_call_id': 'write', 'tool_name': tool, 'args': args}
        context.hooks['pre_tool_call'](**call)
        if action not in {'none', 'existing'}:
            (workspace/'output.csv').write_bytes(output if action != 'wrong_business' else b'code\tnote\nA\twrong\n')
        outcome = {'exit_code': exit_code} if tool == 'terminal' else {'verified': True}
        if action == 'tool_error':
            outcome['error'] = 'failed'
        if action == 'tool_unverified':
            outcome['verified'] = False
        if action == 'tool_failed':
            outcome['success'] = False
        context.hooks['post_tool_call'](**call, status='ok', result=json.dumps(outcome))
        store = hermes.HostEventStore(trial['db'])
        rows = store.list(ids['session_id'])
        if duplicate:
            receipt = next(row for row in rows if row['kind'] == 'native_artifact_observation')
            store.record('native_artifact_observation', receipt)
        # Official SDK streams can omit stdout tool_call_id; durable hooks retain it.
        result = {'session_id': ids['session_id'], 'session_root': str(session),
            'events': store.list(ids['session_id']), 'stream_tool_calls': [{'name': tool, 'input': args}]}
        return result
    return plan, trial, session, workspace, run, evidence, process


@pytest.mark.parametrize('tool', ['write_file', 'patch', 'terminal'])
def test_signed_business_io_accepts_real_new_bytes_without_stdout_id_or_command_filename(artifact_case, tool):
    plan, trial, session, workspace, run, evidence, _ = artifact_case
    result = run(tool)
    gate = evidence.evaluate_native_review_execution_v114(result, plan, trial)
    assert gate['verified'] is True, gate
    assert gate['receipt_authenticated'] is True
    assert gate['proof_kind'] == 'authenticated_native_tool_io'
    receipt = next(row for row in result['events'] if row['kind'] == 'native_artifact_observation')
    lightweight = evidence.evaluate_artifact_receipt(receipt,
        {'workspace': str(workspace), 'artifact_name': 'output.csv'},
        (session/'native-execution-auth.key').read_bytes())
    assert lightweight['verified'] is True, lightweight


@pytest.mark.parametrize('action,tool,exit_code', [
    ('none', 'terminal', 0), ('existing', 'write_file', 0),
    ('tool_error', 'write_file', 0), ('create', 'terminal', 7),
    ('tool_unverified', 'write_file', 0), ('tool_failed', 'patch', 0),
    ('wrong_business', 'patch', 0)])
def test_business_gate_does_not_substitute_mentions_success_or_receipts_for_real_outcomes(
        artifact_case, action, tool, exit_code):
    plan, trial, _, _, run, evidence, _ = artifact_case
    result = run(tool, action, exit_code)
    assert evidence.evaluate_native_review_execution_v114(result, plan, trial)['verified'] is False


@pytest.mark.parametrize('fault', ['duplicate', 'signature', 'call', 'arguments', 'config',
    'hash', 'size', 'path', 'verified', 'overlap', 'final_bytes', 'same_bytes_rewrite',
    'output_symlink', 'output_hardlink', 'key', 'durable', 'stream'])
def test_business_gate_rejects_forged_durable_receipts_and_changed_artifacts(artifact_case, fault):
    from rpe_harness.integrations.hermes import HostEventStore
    plan, trial, session, workspace, run, evidence, _ = artifact_case
    result = run(duplicate=fault == 'duplicate')
    receipt = next(row for row in result['events'] if row['kind'] == 'native_artifact_observation')
    changes = {'signature': {'artifact_receipt_hmac_sha256': '0'*64},
        'call': {'tool_call_id': 'forged'}, 'arguments': {'arguments_sha256': '0'*64},
        'config': {'artifact_config_sha256': '0'*64}, 'hash': {'content_sha256': '0'*64},
        'size': {'bytes_written': 1}, 'path': {'target_path_sha256': '0'*64},
        'verified': {'verified': False}, 'overlap': {'overlap': True}}.get(fault)
    if changes:
        # The tool can write its DB. Keep result and durable bytes identical:
        # only the parent-private HMAC distinguishes the forged metadata.
        metadata = {key: value for key, value in receipt.items() if key not in {
            'kind', 'ingest_event_id', 'source_event_id', 'received_at'}}
        metadata.update(changes)
        with sqlite3.connect(trial['db']) as database:
            database.execute('UPDATE hermes_host_events SET metadata_json=? WHERE ingest_event_id=?',
                (json.dumps(metadata), receipt['ingest_event_id']))
        result['events'] = HostEventStore(trial['db']).list(result['session_id'])
    elif fault == 'final_bytes':
        (workspace/'output.csv').write_text('changed')
    elif fault == 'same_bytes_rewrite':
        path = workspace/'output.csv'
        path.write_bytes(path.read_bytes())
    elif fault == 'output_symlink':
        path = workspace/'output.csv'
        path.rename(workspace/'other.csv')
        path.symlink_to(workspace/'other.csv')
    elif fault == 'output_hardlink':
        os.link(workspace/'output.csv', workspace/'other.csv')
    elif fault == 'key':
        (session/'native-execution-auth.key').write_bytes(b'x'*32)
    elif fault == 'durable':
        result['events'].pop()
    elif fault == 'stream':
        result['stream_tool_calls'][0]['input']['content'] = 'different'
    assert evidence.evaluate_native_review_execution_v114(result, plan, trial)['verified'] is False


def test_business_receipt_requires_private_key(artifact_case, monkeypatch):
    native, process, _ = modules()
    _, _, _, workspace, _, _, _ = artifact_case
    monkeypatch.setattr(process, '_SIGNING_KEY', None)
    watch = native.NativeEvidenceObserver({'workspace': str(workspace), 'artifact_name': 'output.csv'})
    ids = dict(session_id='s', task_id='t', turn_id='r', api_request_id='a', tool_call_id='1',
               tool_name='terminal', arguments_sha256='a'*64)
    watch.before(ids)
    (workspace/'output.csv').write_text('actual')
    receipt, = watch.after({**ids, 'status': 'succeeded', 'result': '{"exit_code":0}'})
    assert receipt['verified'] is False
    assert 'artifact_receipt_hmac_sha256' not in receipt


def test_business_receipts_reject_actual_overlapping_and_duplicate_pre_hooks(artifact_case):
    native, _, evidence = modules()
    _, _, session, workspace, _, _, _ = artifact_case
    config = {'workspace': str(workspace), 'artifact_name': 'output.csv'}
    watch = native.NativeEvidenceObserver(config)
    first = dict(session_id='s', task_id='t', turn_id='r', api_request_id='a', tool_call_id='1',
                 tool_name='terminal', arguments_sha256='a'*64)
    second = {**first, 'tool_call_id': '2', 'tool_name': 'patch'}
    watch.before(first)
    watch.before(first)
    watch.before(second)
    (workspace/'output.csv').write_text('actual')
    rows = watch.after({**first, 'status': 'succeeded', 'result': '{"exit_code":0}'})
    rows += watch.after({**second, 'status': 'ok', 'result': '{"verified":true}'})
    assert len(rows) == 2 and not any(row['verified'] for row in rows)
    for row in rows:
        checked = evidence.evaluate_artifact_receipt(row, config, (session/'native-execution-auth.key').read_bytes())
        assert checked['receipt_authenticated'] is True and checked['verified'] is False


def test_chain_diagnostics_explains_missing_api_outcome_without_private_text(tmp_path):
    from rpe_harness.integrations.hermes import HostEventStore
    _, _, evidence = modules()
    store = HostEventStore(tmp_path/'events.sqlite')
    ids = dict(session_id='private-session', task_id='private-task', turn_id='private-turn', api_request_id='private-api')
    store.record('pre_api_request', ids)
    result = {'session_id': ids['session_id'], 'events': store.list(ids['session_id'])}
    diagnostics = evidence.native_chain_diagnostics(result, str(store.path))
    assert diagnostics['verified'] is False
    assert diagnostics['error_code'] == 'native_api_linkage_invalid'
    assert diagnostics['api_requests'] == 1 and diagnostics['api_outcomes'] == 0
    assert diagnostics['api_unmatched_requests'] == 1
    assert 'private-' not in json.dumps(diagnostics)


def test_chain_diagnostics_retains_strict_duplicate_error_and_tool_stream_checks(artifact_case):
    plan, trial, _, _, run, evidence, _ = artifact_case
    result = run()
    assert evidence.native_chain_diagnostics(result, trial['db'])['verified'] is True
    result['stream_tool_calls'] *= 2
    diagnostics = evidence.native_chain_diagnostics(result, trial['db'])
    assert diagnostics['verified'] is False
    assert diagnostics['error_code'] == 'native_stream_linkage_invalid'
    assert diagnostics['stream_calls'] == 2 and diagnostics['tool_pre_calls'] == 1


@pytest.mark.parametrize('fault', [None, 'file', 'arguments', 'exit', 'missing_api', 'wrong_tail', 'signature'])
def test_post_tool_finisher_uses_actual_signed_artifact_and_complete_current_batch(artifact_case, monkeypatch, fault):
    from types import SimpleNamespace
    from rpe_harness.integrations import hermes_native_finish
    _, _, _, workspace, run, _, _ = artifact_case
    monkeypatch.setenv('RPE_HERMES_NATIVE_FINISH_ENABLED', '1')
    run('terminal', exit_code=7 if fault == 'exit' else 0)
    finisher = hermes_native_finish._FINISHER
    agent = SimpleNamespace(session_id='unit-native', _current_turn_id='turn', _current_api_request_id='api')
    messages = [{'role': 'assistant'}, {'role': 'tool', 'tool_call_id': 'write'}]
    if fault == 'file':
        (workspace/'output.csv').write_text('changed')
    elif fault == 'arguments':
        next(row for row in finisher.rows if row['kind'] == 'post_tool_call')['arguments_sha256'] = '0'*64
    elif fault == 'missing_api':
        finisher.rows = [row for row in finisher.rows if row['kind'] != 'pre_api_request']
    elif fault == 'wrong_tail':
        messages[-1]['tool_call_id'] = 'other'
    elif fault == 'signature':
        next(row for row in finisher.rows if row['kind'] == 'native_artifact_observation')['artifact_receipt_hmac_sha256'] = '0'*64
    terminal = finisher.finish(agent, effective_task_id='task', messages=messages)
    if fault is None:
        assert terminal['status'] == 'native_artifact_recorded' and terminal['artifact_name'] == 'output.csv'
        assert terminal['accepted'] is False
    else:
        assert terminal is None


@pytest.mark.parametrize('tool', ['write_file', 'patch', 'terminal'])
def test_installed_native_tools_create_signed_business_artifacts_under_real_sandbox(artifact_case, tool):
    """Installed tools and OS sandbox are real; protocol identities are unit-only."""
    import subprocess
    from rpe_harness.integrations import hermes_session
    from rpe_harness.integrations.hermes import HostEventStore
    import rpe_harness
    plan, trial, unit_session, workspace, _, evidence, _ = artifact_case
    hermes_root = Path.home()/'.hermes/hermes-agent'
    host_python = hermes_session._installed_python(hermes_root)
    session = Path(plan['root'])/'sessions'/('installed-'+tool)
    home = hermes_session.prepare_home(session, workspace, trial['db'], tool_enabled=False,
        settings={'provider': 'xiaomi', 'model': 'provider-free',
                  'base_url': 'https://example.invalid', 'api_key': 'unused-provider-free'},
        public_contract='clean', preserve_native_tool_search=True)
    for name in ('tmp', 'runtime', 'terminal-tmp'):
        (session/name).mkdir()
    config = session/'native-evidence-config.json'
    config.write_text(json.dumps({'workspace': str(workspace), 'artifact_name': 'output.csv'}))
    key = (unit_session/'native-execution-auth.key').read_bytes()
    key_path = session/'native-execution-auth.key'
    key_path.write_bytes(key)
    source = Path(rpe_harness.__file__).resolve().parent.parent
    profile = session/'native.sb'
    profile.write_text(hermes_session.sandbox_profile([session, workspace], Path.home()/'.hermes',
        hermes_root, Path(trial['db']), [host_python.parent.parent, Path.home()/'.hermes/tools'],
        native_import_root=source, protected_write_roots=[config, key_path], protected_read_roots=[key_path]))
    program = r'''import json,os,shlex,socket,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from rpe_harness.integrations.hermes_native_process import install_native_execution_signing_key
install_native_execution_signing_key()
assert 'RPE_HERMES_NATIVE_EXECUTION_KEY_FD' not in os.environ
def deny_network(self,address): raise AssertionError('provider-free smoke forbids network')
socket.socket.connect=deny_network
from tools.file_tools import write_file_tool,patch_tool
from tools.terminal_tool import terminal_tool
from rpe_harness.integrations.hermes import register
class Context:
 def __init__(self):self.hooks={}
 def register_hook(self,n,c):self.hooks[n]=c
c=Context();register(c)
try:
 Path(sys.argv[4]).read_bytes()
 raise AssertionError('tool context read private key file')
except PermissionError:pass
tool=sys.argv[3]
content='code\tnote\nA\tfirst\nB\tsecond\n'
if tool=='write_file':
 args={'path':'output.csv','content':content}
 function=lambda:write_file_tool(**args,task_id='installed-business')
elif tool=='patch':
 args={'mode':'patch','patch':'*** Begin Patch\n*** Add File: output.csv\n'+''.join('+'+line+'\n' for line in content.splitlines())+'*** End Patch\n'}
 function=lambda:patch_tool(**args,task_id='installed-business')
else:
 Path('job.py').write_text('from pathlib import Path\nPath("output.csv").write_text('+repr(content)+')\n')
 args={'command':shlex.join([sys.executable,'-B','job.py'])}
 assert 'output.csv' not in args['command']
 function=lambda:terminal_tool(**args,task_id='installed-business',timeout=20)
ids=dict(session_id='installed-business',task_id='installed-business',turn_id='turn',api_request_id='api')
c.hooks['pre_api_request'](**ids)
c.hooks['post_api_request'](**ids,response={'assistant_message':{'tool_calls':[{'id':'actual-write','name':tool}]}})
call=dict(**ids,tool_call_id='actual-write',tool_name=tool,args=args)
c.hooks['pre_tool_call'](**call)
raw=function();out=json.loads(raw)
assert not out.get('error'),out
if tool=='terminal':assert out.get('exit_code')==0,out
c.hooks['post_tool_call'](**call,status='ok',result=raw)
assert 'rpe_harness.controller' not in sys.modules
print(json.dumps({'stream_tool_calls':[{'name':tool,'input':args}],'provider_calls':0}))
'''
    read_fd, write_fd = os.pipe()
    os.write(write_fd, key)
    os.close(write_fd)
    env = {**os.environ, 'HOME': str(home), 'HERMES_HOME': str(home),
        'HERMES_RUNTIME_DIR': str(session/'runtime'), 'PYTHONDONTWRITEBYTECODE': '1',
        'HERMES_DISABLE_LAZY_INSTALLS': '1', 'HERMES_ENABLE_PROJECT_PLUGINS': '0',
        'TMPDIR': str(session/'tmp'), 'PYTHONPATH': str(hermes_root),
        'XDG_CACHE_HOME': str(home/'.cache'), 'RPE_HARNESS_IMPORT_ROOT': str(source),
        'RPE_HERMES_TOOL_ENABLED': '0', 'RPE_HERMES_WORKSPACE': str(workspace),
        'RPE_HERMES_DB': trial['db'], 'RPE_HERMES_NATIVE_EVIDENCE_CONFIG': str(config),
        'RPE_HERMES_NATIVE_EXECUTION_KEY_FD': str(read_fd)}
    try:
        child = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), str(host_python), '-B',
            '-c', program, str(source), str(hermes_root), tool, str(key_path)], cwd=workspace,
            env=env, pass_fds=(read_fd,), capture_output=True, text=True, timeout=45)
    finally:
        os.close(read_fd)
    (session/'provider-free-result.json').write_text(json.dumps({
        'exit_code': child.returncode, 'stdout': child.stdout, 'stderr': child.stderr}))
    assert child.returncode == 0, child.stderr+'\n'+child.stdout
    result = json.loads(child.stdout)
    assert result['provider_calls'] == 0
    result.update(session_id='installed-business', session_root=str(session),
        events=HostEventStore(trial['db']).list('installed-business'))
    gate = evidence.evaluate_native_review_execution_v114(result, plan, trial)
    assert gate['verified'] is True, gate
