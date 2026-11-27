"""Explicit producer sessions constrain actual writes, without approving code."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rpe_harness.integrations import hermes, hermes_bootstrap, hermes_session
from test_hermes_integration import configured_plugin


IDS = {'session_id': 'session', 'task_id': 'task', 'turn_id': 'turn', 'api_request_id': 'api'}
TOOLS = [{'type': 'function', 'function': {'name': 'write_file', 'parameters': {'type': 'object'}}}]
CODE = '# exact UTF-8 café\ndef prepare(plan, staging_path):\n    pass\n'


def producer(tmp_path, monkeypatch):
    monkeypatch.setenv('RPE_HERMES_ENTRY_MODE', 'producer')
    context, workspace, db = configured_plugin(tmp_path, monkeypatch, enabled=False)
    return context, workspace, db


def api(context, identity=IDS):
    rewritten = context.middleware['llm_request']({'tools': TOOLS}, **identity)['request']
    context.hooks['pre_api_request'](**identity, request={'body': rewritten})
    context.hooks['post_api_request'](**identity, response={'assistant_message': {
        'tool_calls': [{'id': 'call', 'name': 'write_file'}]}})
    return rewritten


def write(context, workspace, *, result=None, identity=None, args=None):
    identity = identity or {**IDS, 'tool_call_id': 'call'}
    args = args or {'path': 'candidate.py', 'content': CODE}
    def dispatch(actual):
        context.hooks['pre_tool_call'](**identity, tool_name='write_file', args=actual)
        (workspace / actual['path']).write_bytes(actual['content'].encode())
        return json.dumps(result if result is not None else {
            'verified': True, 'bytes_written': len(actual['content'].encode()),
            'dirs_created': False, 'resolved_path': str(workspace / actual['path'])})
    raw = context.middleware['tool_execution']('write_file', args, dispatch, **identity)
    context.hooks['post_tool_call'](**identity, tool_name='write_file', args=args, result=raw, status='ok')
    return raw


def test_producer_forces_real_write_then_closes_after_verified_matching_post_hook(tmp_path, monkeypatch):
    context, workspace, db = producer(tmp_path, monkeypatch)
    rewritten = api(context)
    assert rewritten['tool_choice'] == {'type': 'function', 'function': {'name': 'write_file'}}
    assert rewritten['parallel_tool_calls'] is False
    assert context.tools == {}
    raw = write(context, workspace)
    assert json.loads(raw)['verified'] is True
    choice = context.middleware['llm_request']({'tools': TOOLS}, **{**IDS, 'api_request_id': 'next'})['request']
    assert choice['tool_choice'] == 'none'
    rows = hermes.HostEventStore(db).list('session')
    post = next(row for row in rows if row['kind'] == 'post_tool_call')
    assert post['verified'] is True and post['bytes_written'] == len(CODE.encode())
    assert post['content_sha256'] == __import__('hashlib').sha256(CODE.encode()).hexdigest()
    assert CODE not in json.dumps(rows)


@pytest.mark.parametrize('result', [
    {'verified': False, 'bytes_written': len(CODE.encode())},
    {'verified': True, 'bytes_written': 1},
    {'status': 'ok'}, {'verified': True, 'bytes_written': len(CODE.encode()), 'error': 'bad'},
])
def test_success_shaped_result_cannot_close_producer_without_write_proof(tmp_path, monkeypatch, result):
    context, workspace, _ = producer(tmp_path, monkeypatch)
    api(context)
    write(context, workspace, result=result)
    rewritten = context.middleware['llm_request']({'tools': TOOLS}, **{**IDS, 'api_request_id': 'next'})['request']
    assert rewritten['tool_choice']['function']['name'] == 'write_file'


@pytest.mark.parametrize('damage', ['elsewhere', 'symlink', 'fifo', 'existing', 'tool', 'ids', 'api', 'extra', 'oversize'])
def test_producer_denies_unsafe_calls_without_dispatch(tmp_path, monkeypatch, damage):
    context, workspace, _ = producer(tmp_path, monkeypatch)
    api(context)
    args = {'path': 'candidate.py', 'content': CODE}
    identity = {**IDS, 'tool_call_id': 'call'}
    tool = 'write_file'
    candidate = workspace / 'candidate.py'
    if damage == 'elsewhere':
        args['path'] = '../other.py'
    elif damage == 'symlink':
        candidate.symlink_to(workspace / 'other.py')
    elif damage == 'fifo':
        os.mkfifo(candidate)
    elif damage == 'existing':
        candidate.write_text('existing')
    elif damage == 'tool':
        tool = 'terminal'
    elif damage == 'ids':
        identity['tool_call_id'] = ''
    elif damage == 'api':
        identity['api_request_id'] = 'unobserved'
    elif damage == 'extra':
        args['cross_profile'] = True
    else:
        args['content'] = 'x' * 65537
    raw = context.middleware['tool_execution'](tool, args,
        lambda actual: pytest.fail('unsafe call reached tool'), **identity)
    assert json.loads(raw)['error']['code'].startswith('producer_')
    assert CODE not in raw


def test_producer_policy_fails_closed_on_invalid_identity_or_extra_tool_surface(tmp_path, monkeypatch):
    context, _, _ = producer(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        context.middleware['llm_request']({'tools': TOOLS}, **{**IDS, 'turn_id': ''})
    with pytest.raises(ValueError):
        context.middleware['llm_request']({'tools': TOOLS + [
        {'type': 'function', 'function': {'name': 'terminal'}}]}, **IDS)


@pytest.mark.parametrize('damage', ['file_changed', 'post_args', 'post_ids', 'pre_missing', 'api_duplicate'])
def test_producer_post_hook_cannot_close_from_unmatched_or_changed_evidence(tmp_path, monkeypatch, damage):
    context, workspace, db = producer(tmp_path, monkeypatch)
    api(context)
    identity = {**IDS, 'tool_call_id': 'call'}
    args = {'path': 'candidate.py', 'content': CODE}
    def dispatch(actual):
        if damage != 'pre_missing':
            context.hooks['pre_tool_call'](**identity, tool_name='write_file', args=actual)
        (workspace / 'candidate.py').write_bytes(CODE.encode())
        return json.dumps({'verified': True, 'bytes_written': len(CODE.encode())})
    raw = context.middleware['tool_execution']('write_file', args, dispatch, **identity)
    if damage == 'file_changed':
        (workspace / 'candidate.py').write_text('changed')
    elif damage == 'post_args':
        args = {**args, 'content': 'changed'}
    elif damage == 'post_ids':
        identity = {**identity, 'tool_call_id': 'other'}
    elif damage == 'api_duplicate':
        context.hooks['post_api_request'](**IDS, response={'assistant_message': {'tool_calls': [{'id': 'call', 'name': 'write_file'}]}})
    context.hooks['post_tool_call'](**identity, tool_name='write_file', args=args, result=raw, status='ok')
    rewritten = context.middleware['llm_request']({'tools': TOOLS}, **{**IDS, 'api_request_id': 'next'})['request']
    assert rewritten['tool_choice']['function']['name'] == 'write_file'
    assert all(row.get('verified') is not True for row in hermes.HostEventStore(db).list('session'))


def test_existing_candidate_never_counts_as_a_write_or_reopens_after_success(tmp_path, monkeypatch):
    context, workspace, _ = producer(tmp_path, monkeypatch)
    api(context)
    write(context, workspace)
    assert context.middleware['llm_request']({'tools': TOOLS}, **{**IDS, 'api_request_id': 'summary'})['request']['tool_choice'] == 'none'
    denied = context.middleware['tool_execution']('write_file', {'path': 'candidate.py', 'content': 'changed'},
        lambda args: pytest.fail('second write reached dispatcher'), **{**IDS, 'tool_call_id': 'second'})
    assert json.loads(denied)['error']['code'] == 'producer_call_denied'
    assert (workspace / 'candidate.py').read_bytes() == CODE.encode()
    other = context.middleware['llm_request']({'tools': TOOLS}, **{**IDS, 'turn_id': 'other', 'api_request_id': 'other'})['request']
    assert other['tool_choice']['function']['name'] == 'write_file'


def test_producer_home_has_only_write_toolset_and_disables_search(tmp_path):
    home = hermes_session.prepare_home(tmp_path / 'session', tmp_path, tmp_path / 'db.sqlite',
        tool_enabled=False, entry_mode='producer', settings={'provider': 'xiaomi', 'model': 'test',
            'base_url': 'https://example.invalid/v1', 'api_key': 'test'})
    config = json.loads((home / 'config.yaml').read_text())
    assert config['platform_toolsets']['cli'] == ['rpe_producer_only']
    assert config['tools']['tool_search']['enabled'] == 'off'


@pytest.mark.parametrize('options', [dict(tool_enabled=True), dict(tool_enabled=False, catalog_root='/unused'),
    dict(tool_enabled=False, skill_snapshot_id='snapshot'), dict(tool_enabled=False, required_request={})])
def test_producer_rejects_incompatible_profiles_before_launch(tmp_path, monkeypatch, options):
    monkeypatch.setattr(hermes_session, '_read_host_settings', lambda *a: pytest.fail('invalid profile read settings'))
    with pytest.raises(ValueError):
        hermes_session.run_hermes_session('produce', tmp_path, tmp_path / 'db.sqlite',
            owner_root=tmp_path, entry_mode='producer', **options)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not os.environ.get('RPE_HERMES_SOURCE'), reason='set RPE_HERMES_SOURCE for installed toolset SDK')
def test_bootstrap_registers_custom_toolset_through_actual_installed_sdk_only_for_producer(tmp_path, monkeypatch):
    source = Path(os.environ['RPE_HERMES_SOURCE']) / 'toolsets.py'
    spec = importlib.util.spec_from_file_location('producer_sdk_toolsets', source)
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    monkeypatch.setattr(hermes_bootstrap.importlib, 'import_module', lambda name: sdk if name == 'toolsets' else pytest.fail(name))
    monkeypatch.setenv('RPE_HERMES_ENTRY_MODE', 'optional')
    hermes_bootstrap.install_producer_toolset()
    assert 'rpe_producer_only' not in sdk.TOOLSETS
    monkeypatch.setenv('RPE_HERMES_ENTRY_MODE', 'producer')
    hermes_bootstrap.install_producer_toolset()
    assert sdk.validate_toolset('rpe_producer_only') is True
    assert sdk.TOOLSETS['rpe_producer_only']['tools'] == ['write_file']
    assert sdk.TOOLSETS['rpe_producer_only']['includes'] == []


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(), reason='macOS isolated launcher')
@pytest.mark.parametrize('entry_mode', ['optional', 'producer'])
def test_isolated_launcher_selects_exact_toolset_and_keeps_observer_only_scope(tmp_path, monkeypatch, entry_mode):
    runtime = tmp_path / 'runtime/lib/python/site-packages'
    package = runtime / 'rpe_harness'
    integrations = package / 'integrations'
    integrations.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (integrations / '__init__.py').write_text('')
    shutil.copyfile(hermes.__file__, integrations / 'hermes.py')
    (package / 'contracts.py').write_text('ORACLE')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    bootstrap = integrations / 'hermes_bootstrap.py'
    bootstrap.write_text('''import json,os,sys
from pathlib import Path
sys.path.insert(0, IMPORT_ROOT)
from rpe_harness.integrations.hermes import HostEventStore
try: (Path(IMPORT_ROOT)/'rpe_harness/contracts.py').read_text()
except PermissionError: pass
else: raise AssertionError('oracle readable')
HostEventStore(os.environ['RPE_HERMES_DB']).record('on_session_end',{'session_id':'launch','completed':True})
print(json.dumps({'type':'result','session_id':'launch','text':json.dumps({'tools':sys.argv[sys.argv.index('--toolsets')+1], 'entry_mode':os.environ['RPE_HERMES_ENTRY_MODE']})}))
'''.replace('IMPORT_ROOT', repr(str(runtime))))
    owner = Path(sys.executable).absolute().parent.parent.parent
    assert tmp_path.is_relative_to(owner)
    monkeypatch.setattr(hermes_session, '__file__', str(integrations / 'hermes_session.py'))
    monkeypatch.setattr(hermes_session, '_installed_python', lambda root: Path(sys.executable).resolve())
    monkeypatch.setattr(hermes_session, '_read_host_settings', lambda *args: {'provider': 'xiaomi',
        'model': 'test', 'base_url': 'https://example.invalid/v1', 'api_key': 'owned-test'})
    result = hermes_session.run_hermes_session('launcher only', workspace, tmp_path / 'db.sqlite',
        tool_enabled=False, owner_root=owner, hermes_root=tmp_path / 'fake-hermes', timeout=10, entry_mode=entry_mode)
    try:
        assert result['completed'], (result, Path(result['session_root'], 'stderr.log').read_text())
        payload = json.loads(result['final_response'])
        assert payload['tools'] == ('rpe_producer_only' if entry_mode == 'producer' else 'terminal,file')
        assert payload['entry_mode'] == entry_mode
        assert result['usage']['api_requests'] == 0
        assert result['process_cleanup']['cleanup_complete'] is True
    finally:
        shutil.rmtree(result['session_root'])


@pytest.mark.skipif(not os.environ.get('RPE_HERMES_SOURCE'), reason='set RPE_HERMES_SOURCE for installed producer SDK smoke')
def test_installed_sdk_toolset_middleware_and_actual_filewrite_close_producer(tmp_path):
    installed = Path(os.environ['RPE_HERMES_SOURCE']).resolve()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    home = hermes_session.prepare_home(tmp_path / 'session', workspace, tmp_path / 'db.sqlite',
        tool_enabled=False, entry_mode='producer', settings={'provider': 'xiaomi', 'model': 'test',
            'base_url': 'https://example.invalid/v1', 'api_key': 'owned-test'})
    project = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'HOME': str(tmp_path), 'HERMES_HOME': str(home),
        'HERMES_RUNTIME_DIR': str(tmp_path / 'runtime'), 'HERMES_DISABLE_LAZY_INSTALLS': '1',
        'HERMES_ENABLE_PROJECT_PLUGINS': '0', 'HERMES_SAFE_MODE': '0', 'PYTHONDONTWRITEBYTECODE': '1',
        'RPE_HARNESS_IMPORT_ROOT': str(project / 'src'), 'RPE_HERMES_WORKSPACE': str(workspace),
        'RPE_HERMES_DB': str(tmp_path / 'db.sqlite'), 'RPE_HERMES_TOOL_ENABLED': '0',
        'RPE_HERMES_ENTRY_MODE': 'producer', 'RPE_HERMES_ERROR_PATH': str(tmp_path / 'errors.jsonl'),
        'PYTHONPATH': str(project / 'src') + os.pathsep + str(installed),
        'XDG_CACHE_HOME': str(tmp_path / 'cache'), 'XDG_CONFIG_HOME': str(tmp_path / 'config'),
        'XDG_DATA_HOME': str(tmp_path / 'data')}
    env.pop('HERMES_IGNORE_USER_CONFIG', None)
    program = '''import json,sys
from pathlib import Path
from rpe_harness.integrations.hermes_bootstrap import install_producer_toolset
install_producer_toolset()
import toolsets
assert toolsets.validate_toolset('rpe_producer_only')
assert toolsets.resolve_toolset('rpe_producer_only') == ['write_file']
from hermes_cli.plugins import get_plugin_manager
manager = get_plugin_manager()
manager.discover_and_load()
assert manager._plugins['rpe-harness'].enabled, manager._plugins['rpe-harness'].error
from hermes_cli.middleware import apply_llm_request_middleware
from tools.file_tools import WRITE_FILE_SCHEMA
assert set(WRITE_FILE_SCHEMA['parameters']['properties']) == {'path','content'}
identity = {'session_id':'sdk-session','task_id':'sdk-task','turn_id':'sdk-turn','api_request_id':'sdk-api'}
request = {'tools':[{'type':'function','function':WRITE_FILE_SCHEMA}]}
rewritten = apply_llm_request_middleware(request, **identity).payload
assert rewritten['tool_choice'] == {'type':'function','function':{'name':'write_file'}}
assert rewritten['parallel_tool_calls'] is False
# Installed OpenAI SDK serialization must agree with observer kwargs, including
# conflicting extra-body overrides; no network is involved.
import httpx
from openai import OpenAI
poisoned = {'tools':request['tools'], 'max_tokens':131072, 'extra_body':{
    'max_completion_tokens':131072,'max_tokens':131072,'tools':[], 'tool_choice':'none','parallel_tool_calls':True}}
bounded = apply_llm_request_middleware(poisoned, **identity).payload
captured = []
def transport(req):
    captured.append(json.loads(req.content))
    return httpx.Response(200,json={'id':'sdk-offline','model':'test','object':'chat.completion','created':0,'choices':[]})
client=OpenAI(api_key='owned-test',base_url='https://example.invalid/v1',http_client=httpx.Client(transport=httpx.MockTransport(transport)))
client.chat.completions.create(model='test',messages=[{'role':'user','content':'public fixture'}],**bounded)
assert captured[0]['max_completion_tokens']==8192 and 'max_tokens' not in captured[0]
assert captured[0]['thinking']=={'type':'disabled'}
assert captured[0]['tools']==request['tools'] and captured[0]['parallel_tool_calls'] is False
assert captured[0]['tool_choice']=={'type':'function','function':{'name':'write_file'}}
manager.invoke_hook('pre_api_request',request={'body':rewritten},**identity)
manager.invoke_hook('post_api_request',response={'assistant_message':{'tool_calls':[{'id':'sdk-write','name':'write_file'}]}},**identity)
from model_tools import handle_function_call
code = 'def prepare(plan, staging_path):\\n    pass\\n'
result = handle_function_call('write_file',{'path':'candidate.py','content':code},tool_call_id='sdk-write',**identity)
assert json.loads(result)['verified'] is True, result
assert Path('candidate.py').read_bytes() == code.encode()
final = apply_llm_request_middleware(request, **{**identity,'api_request_id':'sdk-summary'}).payload
assert final['tool_choice'] == 'none', final
from rpe_harness.integrations.hermes import HostEventStore
post = [row for row in HostEventStore(Path(sys.argv[1])).list('sdk-session') if row['kind']=='post_tool_call']
assert len(post)==1 and post[0]['verified'] is True
assert post[0]['bytes_written']==len(code.encode())
assert 'rpe_harness.integrations.hermes_gate' not in sys.modules
print(json.dumps({'verified':True,'tools':toolsets.resolve_toolset('rpe_producer_only'),'closed':True}))
'''
    child = subprocess.run([str(installed / '.venv/bin/python'), '-B', '-c', program, str(tmp_path / 'db.sqlite')],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=45)
    assert child.returncode == 0, child.stderr + child.stdout
    assert json.loads(child.stdout.strip().splitlines()[-1]) == {'verified': True, 'tools': ['write_file'], 'closed': True}


def test_producer_wire_budget_disables_mimo_thinking_without_changing_native(tmp_path, monkeypatch):
    context,workspace,db=producer(tmp_path,monkeypatch)
    body={'tools':TOOLS,'max_tokens':131072,'extra_body':{'existing_setting':True}}
    result=context.middleware['llm_request'](body,**IDS)['request']
    assert result['max_completion_tokens']==8192
    assert 'max_tokens' not in result
    assert result['extra_body']=={'existing_setting':True,'thinking':{'type':'disabled'}}
    assert 'thinking' not in body['extra_body']
    context.hooks['pre_api_request'](**IDS,request={'body':result})
    row=next(row for row in hermes.HostEventStore(db).list('session') if row['kind']=='pre_api_request')
    assert row['request_thinking_type']=='disabled'
    assert row['request_max_completion_tokens']==8192

    from rpe_harness.evolution.worker import _observations
    assert _observations([row])[0]['request_max_completion_tokens']==8192


def test_producer_cannot_be_overridden_by_sdk_extra_body(tmp_path,monkeypatch):
    context,_,_=producer(tmp_path,monkeypatch)
    extra={'max_completion_tokens':131072,'max_tokens':131072,'tools':[{'type':'function','function':{'name':'terminal'}}],
           'tool_choice':'none','parallel_tool_calls':True,'existing_setting':True}
    body={'tools':TOOLS,'extra_body':extra}
    result=context.middleware['llm_request'](body,**IDS)['request']
    assert result['extra_body']=={'existing_setting':True,'thinking':{'type':'disabled'}}
    assert result['max_completion_tokens']==8192
    assert extra['max_completion_tokens']==131072
