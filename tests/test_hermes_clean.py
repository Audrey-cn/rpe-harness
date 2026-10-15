"""Clean source ingress, actual understanding, and honest controller execution."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from rpe_harness.integrations import hermes
from rpe_harness.integrations.hermes_gate import digest_json
from test_hermes_address import SDK


SOURCE = json.dumps({'capability': 'tabular.deduplicate', 'constraints': ['preserve_original'],
    'execution': {'input_path': 'orders.csv', 'output_path': 'out.csv', 'key_columns': ['id']}},
    ensure_ascii=False, indent=2)


def test_clean_integration_exists():
    assert importlib.util.find_spec('rpe_harness.integrations.hermes_clean') is not None


@pytest.fixture
def clean(tmp_path, monkeypatch, request):
    api = importlib.import_module('rpe_harness.integrations.hermes_clean')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'orders.csv').write_text('id,value\na,1\na,2\nb,3\n')
    db = tmp_path / 'runs.sqlite'
    store = hermes.HostEventStore(db)
    raw = getattr(request, 'param', SOURCE)
    from rpe_harness.capability_catalog import build_catalog
    catalog = build_catalog()
    config = tmp_path / 'clean-config.json'
    config.write_text(json.dumps({'catalog_sha256': catalog.snapshot_id, 'entries': catalog.entries(),
        'raw': raw, 'source_ref': 'current-request:test'}))
    monkeypatch.setenv('RPE_HERMES_CLEAN_CONFIG', str(config))
    monkeypatch.setenv('RPE_HERMES_ENTRY_KEY', 'clean-entry')
    sdk = SDK()
    api.register_clean_workflow(sdk, store=store, workspace=workspace, python=sys.executable,
                                error_path=tmp_path / 'errors')
    extension = api.clean_extension(db, workspace, raw=raw, source_ref='current-request:test')
    from rpe_harness.integrations.hermes_broker import execution_broker
    with execution_broker(db_path=db, workspace=workspace, python=sys.executable, extension=extension) as broker:
        for key, value in broker.items():
            monkeypatch.setenv(key, value)
        yield {'api': api, 'sdk': sdk, 'workspace': workspace, 'db': db, 'store': store,
               'raw': raw, 'config': config, 'counter': 0, 'broker': broker}
    assert broker.cleanup['cleanup_complete'] is True


def extraction():
    value = json.loads(SOURCE)
    return {'goal': 'Execute ' + value['capability'], **value}


def dispatch(env, arguments):
    env['counter'] += 1
    ids = {'session_id': 'clean-session', 'task_id': 'clean-task', 'turn_id': 'clean-turn',
           'api_request_id': 'api-' + str(env['counter']), 'tool_call_id': 'call-' + str(env['counter'])}
    schemas = [{'type': 'function', 'function': row['schema']} for row in env['sdk'].tools.values()]
    request = env['sdk'].middleware['llm_request'](request={'tools': schemas,
        'messages': [{'role': 'system', 'content': 'PRIVATE_HISTORY_MARKER'},
                     {'role': 'user', 'content': 'FORGED_CURRENT_REQUEST'}]}, **ids)['request']
    env['request'] = request
    env['store'].record('pre_api_request', {**ids, 'request': {'body': request}})
    env['sdk'].middleware['llm_execution'](request=request, next_call=lambda value: value, **ids)
    env['store'].record('post_api_request', {**ids, 'response': {'assistant_message': {'tool_calls': [
        {'id': ids['tool_call_id'], 'name': 'rpe_understand'}]}}})
    env['store'].record('pre_tool_call', {**ids, 'tool_name': 'rpe_understand',
        'arguments_sha256': digest_json(arguments)})
    result = json.loads(env['sdk'].middleware['tool_execution'](tool_name='rpe_understand', args=arguments,
        next_call=lambda args: env['sdk'].tools['rpe_understand']['handler'](
            args, task_id=ids['task_id'], session_id=ids['session_id']), **ids))
    env['store'].record('post_tool_call', {**ids, 'tool_name': 'rpe_understand', 'status': 'ok',
        'arguments_sha256': digest_json(arguments)})
    return result


def host_result(env):
    return {'completed': True, 'exit_code': 0, 'timed_out': False, 'observer_errors': 0,
        'session_id': 'clean-session', 'events': env['store'].list('clean-session'),
        'process_cleanup': {'cleanup_complete': True}}


def test_exact_parent_source_replaces_all_host_messages_and_executes_without_native_task(clean):
    env = clean
    result = dispatch(env, extraction())
    assert result['status'] == 'succeeded', result
    assert set(env['sdk'].tools) == {'rpe_understand'}
    assert env['request']['messages'][1] == {'role': 'user', 'content': SOURCE}
    assert 'PRIVATE_HISTORY_MARKER' not in json.dumps(env['request'])
    assert 'FORGED_CURRENT_REQUEST' not in json.dumps(env['request'])
    events = env['store'].list('clean-session')
    assert not any(e.get('tool_name') == 'rpe_task' for e in events)
    receipts = [e for e in events if e['kind'] == 'rpe_controller_task_received']
    assert len(receipts) == 1
    assert not any(k in receipts[0] for k in ('api_request_id', 'tool_call_id', 'tool_name'))
    assert (env['workspace'] / 'out.csv').read_text(encoding='utf-8-sig') == 'id,value\na,1\nb,3\n'
    verdict = env['api'].evaluate_clean_session(host_result(env), env['db'], env['workspace'])
    assert verdict['harness_verified'] is True, verdict
    assert verdict['execution_kind'] == 'controller'
    assert verdict['native_understanding_calls'] == 1
    assert verdict['controller_executions'] == 1


@pytest.mark.parametrize('clean', ['Deduplicate orders.csv and also email the result.'], indirect=True)
def test_free_prose_is_traceable_terminal_without_execution(clean):
    result = dispatch(clean, extraction())
    assert result['status'] == 'needs_context', result
    assert 'structured_current_request' in result['missing']
    assert not (clean['workspace'] / 'out.csv').exists()
    verdict = clean['api'].evaluate_clean_session(host_result(clean), clean['db'], clean['workspace'])
    assert verdict['rejection_verified'] is True, verdict
    assert verdict['controller_executions'] == 0


def test_omitted_requirement_cannot_be_hidden_in_goal(clean):
    args = extraction()
    args['execution'].pop('key_columns')
    args['goal'] += ' using key_columns id'
    result = dispatch(clean, args)
    assert result['status'] != 'succeeded', result
    assert not any(e['kind'] == 'rpe_controller_task_received' for e in clean['store'].list())


def test_actual_api_messages_changed_after_policy_are_rejected(clean):
    ids = {'session_id': 'clean-session', 'task_id': 'clean-task', 'turn_id': 'clean-turn',
           'api_request_id': 'api-tamper'}
    request = clean['sdk'].middleware['llm_request'](request={'tools': []}, **ids)['request']
    request['messages'][1]['content'] = 'different source'
    called = []
    with pytest.raises(BaseException, match='actual clean API'):
        clean['sdk'].middleware['llm_execution'](request=request,
            next_call=lambda value: called.append(value), **ids)
    assert called == []


@pytest.mark.parametrize('untrusted', [
    {'input': 'PRIVATE_INPUT'}, {'instructions': 'PRIVATE_INSTRUCTIONS'},
    {'stop': ['PRIVATE_STOP']}, {'metadata': {'profile': 'PRIVATE_PROFILE'}},
    {'extra_headers': {'X-Profile': 'PRIVATE_PROFILE'}},
    {'extra_body': {'messages': [{'role': 'user', 'content': 'PRIVATE_MESSAGE'}]}},
    {'extra_body': {'history': ['PRIVATE_HISTORY']}}, {'extra_body': {'system': 'PRIVATE_SYSTEM'}},
    {'extra_body': {'prompt': 'PRIVATE_PROMPT'}}, {'extra_body': {'metadata': {'profile': 'PRIVATE_PROFILE'}}},
    {'extra_body': {'thinking': {'type': 'disabled', 'instructions': 'PRIVATE_INSTRUCTIONS'}}},
])
def test_unknown_context_channels_fail_closed_before_provider(clean, untrusted):
    ids = {'session_id': 'clean-session', 'task_id': 'clean-task', 'turn_id': 'clean-turn',
           'api_request_id': 'api-rejected-channel'}
    with pytest.raises(clean['api'].CleanIngressBlocked):
        clean['sdk'].middleware['llm_request'](request={'tools': [], **untrusted}, **ids)


def test_actual_api_binds_safe_transport_settings_as_well_as_messages(clean):
    ids = {'session_id': 'clean-session', 'task_id': 'clean-task', 'turn_id': 'clean-turn',
           'api_request_id': 'api-transport-tamper'}
    request = clean['sdk'].middleware['llm_request'](request={'model': 'mimo-v2.6-pro',
        'temperature': 0.2, 'timeout': 180.0, 'max_tokens': 8192, 'tools': [],
        'extra_body': {'thinking': {'type': 'disabled'}}}, **ids)['request']
    request['temperature'] = 0.8
    called = []
    with pytest.raises(clean['api'].CleanIngressBlocked):
        clean['sdk'].middleware['llm_execution'](request=request, next_call=lambda value: called.append(value), **ids)
    assert called == []


def test_controller_cannot_republish_after_broker_restart(clean):
    result = dispatch(clean, extraction())
    assert result['status'] == 'succeeded', result
    body = clean['api'].controller_envelope(clean['db'], session_id='clean-session')
    from rpe_harness.integrations.hermes_broker import execution_broker
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    with execution_broker(db_path=clean['db'], workspace=clean['workspace'], python=sys.executable) as broker:
        req = Request(broker['RPE_HERMES_BROKER_URL'], data=json.dumps(body).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + broker['RPE_HERMES_BROKER_TOKEN']})
        with pytest.raises(HTTPError) as error:
            urlopen(req, timeout=5)
        assert error.value.code == 403
    assert len([e for e in clean['store'].list() if e['kind'] == 'rpe_controller_task_received']) == 1


def test_gate_reopens_source_ledger_and_artifact(clean):
    assert dispatch(clean, extraction())['status'] == 'succeeded'
    (clean['workspace'] / 'out.csv').write_text('plausible but wrong')
    verdict = clean['api'].evaluate_clean_session(host_result(clean), clean['db'], clean['workspace'])
    assert verdict['harness_verified'] is False


def test_gate_rejects_understanding_receipt_argument_drift(clean):
    assert dispatch(clean, extraction())['status'] == 'succeeded'
    with sqlite3.connect(clean['db']) as db:
        row = db.execute("SELECT id,metadata_json FROM hermes_host_events WHERE kind='pre_tool_call'").fetchone()
        metadata = json.loads(row[1])
        metadata['arguments_sha256'] = 'a' * 64
        db.execute('UPDATE hermes_host_events SET metadata_json=? WHERE id=?', (json.dumps(metadata), row[0]))
    verdict = clean['api'].evaluate_clean_session(host_result(clean), clean['db'], clean['workspace'])
    assert verdict['harness_verified'] is False


def test_gate_rejects_fabricated_native_task_receipts(clean):
    assert dispatch(clean, extraction())['status'] == 'succeeded'
    clean['store'].record('rpe_task_received', {'session_id': 'clean-session', 'tool_name': 'rpe_task',
        'tool_call_id': 'invented-model-call'})
    verdict = clean['api'].evaluate_clean_session(host_result(clean), clean['db'], clean['workspace'])
    assert verdict['harness_verified'] is False


def test_clean_launcher_eager_profile_and_shared_public_system_contract(tmp_path):
    from rpe_harness.integrations.hermes_session import prepare_home
    from rpe_harness.integrations.hermes_clean import CLEAN_SOURCE_CONTRACT
    home = prepare_home(tmp_path / 'session', tmp_path, tmp_path / 'runs.sqlite',
        tool_enabled=True, entry_mode='clean', public_contract='clean',
        settings={'provider': 'xiaomi', 'model': 'test', 'api_key': 'no-model',
                  'base_url': 'https://example.invalid/v1'})
    config = json.loads((home / 'config.yaml').read_text())
    assert config['tools']['tool_search']['enabled'] == 'off'
    assert config['platform_toolsets']['cli'] == ['rpe_harness']
    assert config['agent']['system_prompt'] == CLEAN_SOURCE_CONTRACT.replace('{helper_python}', sys.executable)


def test_clean_rejects_hidden_contract_and_old_context_before_host_setup(tmp_path):
    from rpe_harness.integrations.hermes_session import run_hermes_session
    for arguments in ({'required_request': {'hidden': 'answer'}}, {'context_sources': [{'hidden': 'history'}]}):
        with pytest.raises(ValueError, match='clean'):
            run_hermes_session(SOURCE, tmp_path, tmp_path / 'runs.sqlite', tool_enabled=True,
                owner_root=tmp_path / 'owner', entry_mode='clean', **arguments)


def test_real_installed_hermes_sdk_loads_validates_and_invokes_clean_without_model(clean):
    env = clean
    sdk_root = Path(os.environ.get('RPE_HERMES_SOURCE', '/Users/audrey/.hermes/hermes-agent'))
    interpreter = sdk_root / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('installed Hermes SDK unavailable')
    from rpe_harness.integrations.hermes_session import prepare_home
    home = prepare_home(env['config'].parent / 'sdk-session', env['workspace'], env['db'],
        tool_enabled=True, entry_mode='clean', public_contract='clean',
        settings={'provider': 'xiaomi', 'model': 'test', 'api_key': 'fake-no-model-key',
                  'base_url': 'https://example.invalid/v1'})
    environment = {**os.environ, 'HOME': str(home), 'XDG_CACHE_HOME': str(home / 'cache'),
        'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(home / 'runtime'),
        'HERMES_SAFE_MODE': '0', 'HERMES_ENABLE_PROJECT_PLUGINS': '0',
        'HERMES_DISABLE_LAZY_INSTALLS': '1', 'RPE_HERMES_DB': str(env['db']),
        'RPE_HERMES_WORKSPACE': str(env['workspace']), 'RPE_HARNESS_PYTHON': sys.executable,
        'RPE_HERMES_TOOL_ENABLED': '1', 'RPE_HARNESS_IMPORT_ROOT': str(Path(__file__).parents[1] / 'src'),
        'RPE_HERMES_ENTRY_MODE': 'clean', 'RPE_HERMES_ERROR_PATH': str(home / 'observer-errors'),
        'PYTHONPATH': str(Path(__file__).parents[1] / 'src') + os.pathsep + str(sdk_root)}
    environment.pop('HERMES_IGNORE_USER_CONFIG', None)
    program = '''import json,sys
from types import SimpleNamespace
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.middleware import apply_llm_request_middleware,run_llm_execution_middleware
from agent.api_request_hooks import ApiRequestHooksMixin
from agent.agent_init import _load_tools
from agent.agent_runtime_helpers import invoke_tool
from agent.message_sanitization import uniquify_tool_call_ids
from agent.turn_tool_validation import validate_tool_calls
from openai.types.chat import ChatCompletionMessageToolCall
manager=get_plugin_manager();manager.discover_and_load()
from rpe_harness.integrations.hermes_bootstrap import install_clean_ingress_guard
install_clean_ingress_guard()
from hermes_cli.middleware import run_llm_execution_middleware
assert manager._plugins['rpe-harness'].enabled,manager._plugins['rpe-harness'].error
agent=SimpleNamespace(quiet_mode=True,platform='cli',enabled_toolsets=['rpe_harness'],
    _context_engine_tool_names=set(),_memory_manager=None,_uniquify_tool_call_ids=uniquify_tool_call_ids)
_load_tools(agent,['rpe_harness'],None)
assert 'rpe_understand' in agent.valid_tool_names,sorted(agent.valid_tool_names)
assert 'rpe_task' not in agent.valid_tool_names,sorted(agent.valid_tool_names)
hook=ApiRequestHooksMixin()
ids={'session_id':'clean-session','task_id':'clean-task','turn_id':'clean-turn',
     'api_request_id':'sdk-api-1','tool_call_id':'sdk-call-1'}
body=apply_llm_request_middleware({'tools':agent.tools,'messages':[{'role':'system','content':'PRIVATE_SDK_HISTORY'},
    {'role':'user','content':'FORGED_SDK_SOURCE'}]},**ids).payload
assert body['messages'][1]['content']==sys.argv[2]
assert 'PRIVATE_SDK_HISTORY' not in json.dumps(body) and 'FORGED_SDK_SOURCE' not in json.dumps(body)
manager.invoke_hook('pre_api_request',request=hook._api_request_payload_for_hook(body),**ids)
run_llm_execution_middleware(body,lambda value:value,**ids)
manager.invoke_hook('post_api_request',response={'assistant_message':{'tool_calls':[{'id':ids['tool_call_id'],'name':'rpe_understand'}]}},**ids)
args=json.loads(sys.argv[1])
message=SimpleNamespace(tool_calls=[ChatCompletionMessageToolCall(id=ids['tool_call_id'],type='function',
    function={'name':'rpe_understand','arguments':json.dumps(args)})])
validation=validate_tool_calls(agent,message,'tool_calls',messages=[],conversation_history=[],
    api_call_count=1,effective_task_id=ids['task_id'])
assert validation.action=='ok',validation.action
agent.session_id=ids['session_id'];agent._current_turn_id=ids['turn_id'];agent._current_api_request_id=ids['api_request_id']
result=json.loads(invoke_tool(agent,'rpe_understand',args,ids['task_id'],tool_call_id=ids['tool_call_id'],messages=[]))
assert result['status']=='succeeded',result
ids={'session_id':'clean-session','task_id':'clean-task','turn_id':'clean-turn','api_request_id':'sdk-api-final'}
body=apply_llm_request_middleware({'tools':agent.tools,'messages':[{'role':'user','content':'PRIVATE_SDK_HISTORY'}]},**ids).payload
assert body['tool_choice']=='none' and 'PRIVATE_SDK_HISTORY' not in json.dumps(body)
manager.invoke_hook('pre_api_request',request=hook._api_request_payload_for_hook(body),**ids)
run_llm_execution_middleware(body,lambda value:value,**ids)
manager.invoke_hook('post_api_request',response={'assistant_message':{'tool_calls':[]}},**ids)
print(json.dumps(result))
'''
    child = subprocess.run([str(interpreter), '-B', '-c', program, json.dumps(extraction()), SOURCE],
        cwd=env['workspace'], env=environment, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr[-4000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result['status'] == 'succeeded'
    verdict = env['api'].evaluate_clean_session(host_result(env), env['db'], env['workspace'])
    assert verdict['harness_verified'] is True, verdict
    assert verdict['reporting_api_requests'] == 1
    events = env['store'].list('clean-session')
    assert len([e for e in events if e['kind'] == 'pre_tool_call']) == 1
    assert not any(e.get('tool_name') == 'rpe_task' for e in events)


def test_real_sdk_failure_cannot_fall_through_to_provider(clean):
    sdk_root = Path(os.environ.get('RPE_HERMES_SOURCE', '/Users/audrey/.hermes/hermes-agent'))
    interpreter = sdk_root / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('installed Hermes SDK unavailable')
    program = '''import json,sys,os
sys.path.insert(0,sys.argv[1])
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.middleware import run_llm_execution_middleware
from rpe_harness.integrations.hermes_clean import register_clean_workflow
from rpe_harness.integrations.hermes import HostEventStore
manager=get_plugin_manager()
from hermes_cli.middleware import apply_llm_request_middleware
class Context:
    def register_tool(self,**kwargs): pass
    def register_middleware(self,kind,function): manager._middleware.setdefault(kind,[]).append(function)
register_clean_workflow(Context(),store=HostEventStore(os.environ['RPE_HERMES_DB']),
    workspace=os.environ['RPE_HERMES_WORKSPACE'],python=sys.executable,error_path=sys.argv[2])
ids={'session_id':'guard-session','task_id':'guard-task','turn_id':'guard-turn','api_request_id':'guard-api'}
body=apply_llm_request_middleware({'messages':[{'role':'user','content':'PRIVATE_HISTORY'}]},**ids).payload
body['messages'][1]['content']='PRIVATE_HISTORY'
called=[]
try: run_llm_execution_middleware(body,lambda request:called.append(request),**ids)
except BaseException: pass
assert not called,'contaminated request reached provider'
body['messages'][1]['content']=json.loads(open(os.environ['RPE_HERMES_CLEAN_CONFIG']).read())['raw']
path=os.environ['RPE_HERMES_CLEAN_CONFIG'];original=open(path).read()
try:
    open(path,'w').write(original+' ')
    try: run_llm_execution_middleware(body,lambda request:called.append(request),**ids)
    except BaseException: pass
    assert not called,'changed clean config reached provider'
finally: open(path,'w').write(original)
from rpe_harness.integrations.hermes_bootstrap import install_clean_ingress_guard
os.environ['RPE_HERMES_ENTRY_MODE']='clean'
install_clean_ingress_guard()
import hermes_cli.middleware as middleware
try: middleware.run_llm_execution_middleware(body,lambda request:called.append(request),**ids)
except BaseException: pass
assert not called,'missing clean plugin reached provider'
'''
    home = clean['config'].parent / 'guard-home'
    home.mkdir()
    environment = {**os.environ, 'HOME': str(home), 'HERMES_HOME': str(home),
        'HERMES_RUNTIME_DIR': str(home / 'runtime'), 'XDG_CACHE_HOME': str(home / 'cache'),
        'RPE_HERMES_DB': str(clean['db']), 'RPE_HERMES_WORKSPACE': str(clean['workspace']),
        'PYTHONPATH': str(Path(__file__).parents[1] / 'src') + os.pathsep + str(sdk_root)}
    child = subprocess.run([str(interpreter), '-B', '-c', program, str(sdk_root), str(clean['config'].parent / 'errors')],
        env=environment, capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr[-3000:]


def test_bootstrap_guard_denies_missing_clean_registration(monkeypatch):
    from rpe_harness.integrations import hermes_bootstrap
    monkeypatch.setenv('RPE_HERMES_ENTRY_MODE', 'clean')
    called = []
    import types
    middleware = types.SimpleNamespace(run_llm_execution_middleware=lambda *a, **kw: called.append(a))
    manager = types.SimpleNamespace(_plugins={})
    modules = {'hermes_cli.middleware': middleware,
               'hermes_cli.plugins': types.SimpleNamespace(get_plugin_manager=lambda: manager)}
    monkeypatch.setattr(hermes_bootstrap.importlib, 'import_module', modules.__getitem__)
    hermes_bootstrap.install_clean_ingress_guard()
    with pytest.raises(BaseException, match='clean plugin'):
        middleware.run_llm_execution_middleware({'messages': ['PRIVATE']}, lambda request: called.append(request))
    assert called == []


def test_real_sdk_final_http_body_has_only_clean_source_and_typed_xiaomi_settings(clean):
    sdk_root = Path(os.environ.get('RPE_HERMES_SOURCE', '/Users/audrey/.hermes/hermes-agent'))
    interpreter = sdk_root / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('installed Hermes SDK unavailable')
    from rpe_harness.integrations.hermes_session import prepare_home
    home = prepare_home(clean['config'].parent / 'wire-session', clean['workspace'], clean['db'],
        tool_enabled=True, entry_mode='clean', settings={'provider': 'xiaomi', 'model': 'test',
            'api_key': 'fake-no-model-key', 'base_url': 'https://example.invalid/v1'})
    environment = {**os.environ, 'HOME': str(home), 'HERMES_HOME': str(home),
        'HERMES_RUNTIME_DIR': str(home / 'runtime'), 'XDG_CACHE_HOME': str(home / 'cache'),
        'HERMES_SAFE_MODE': '0', 'HERMES_ENABLE_PROJECT_PLUGINS': '0', 'HERMES_DISABLE_LAZY_INSTALLS': '1',
        'RPE_HERMES_DB': str(clean['db']), 'RPE_HERMES_WORKSPACE': str(clean['workspace']),
        'RPE_HARNESS_PYTHON': sys.executable, 'RPE_HERMES_TOOL_ENABLED': '1',
        'RPE_HARNESS_IMPORT_ROOT': str(Path(__file__).parents[1] / 'src'), 'RPE_HERMES_ENTRY_MODE': 'clean',
        'RPE_HERMES_ERROR_PATH': str(home / 'errors'),
        'PYTHONPATH': str(Path(__file__).parents[1] / 'src') + os.pathsep + str(sdk_root)}
    environment.pop('HERMES_IGNORE_USER_CONFIG', None)
    program = '''import json,sys,socket
import openai,httpx
from hermes_cli.plugins import get_plugin_manager
manager=get_plugin_manager();manager.discover_and_load()
assert manager._plugins['rpe-harness'].enabled,manager._plugins['rpe-harness'].error
from rpe_harness.integrations.hermes_bootstrap import install_clean_ingress_guard
install_clean_ingress_guard()
from hermes_cli.middleware import apply_llm_request_middleware,run_llm_execution_middleware
from rpe_harness.integrations.hermes_clean import CleanIngressBlocked
network_attempts=[]
def deny_network(self,address):
    network_attempts.append(True)
    raise AssertionError('real network disabled for no-model agent construction')
socket.socket.connect=deny_network
from run_agent import AIAgent
agent=AIAgent(model='mimo-v2.6-pro',provider='xiaomi',api_key='fake-no-provider',
    base_url='https://example.invalid/v1',max_iterations=8,enabled_toolsets=['rpe_harness'],
    quiet_mode=True,platform='cli',session_id='wire-agent',skip_context_files=True)
defaults=agent._build_api_kwargs([{'role':'system','content':'PRIVATE_HOST_HISTORY'}])
assert network_attempts==[], 'agent construction attempted real network'
assert set(defaults)<= {'model','messages','tools','temperature','timeout','max_tokens','max_completion_tokens','extra_body'}
seen=[]
def fake_http(request):
    seen.append(json.loads(request.content))
    return httpx.Response(200,json={'id':'fake','object':'chat.completion','created':1,'model':'test',
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'fixture'}}]})
client=openai.OpenAI(api_key='fake-no-provider',base_url='https://example.invalid/v1',
    http_client=httpx.Client(transport=httpx.MockTransport(fake_http)))
def send(index,request,mutate=None):
    ids={'session_id':'wire-'+str(index),'task_id':'wire-task','turn_id':'wire-turn','api_request_id':'wire-api'}
    projected=apply_llm_request_middleware(request,**ids).payload
    if mutate: mutate(projected)
    return run_llm_execution_middleware(projected,lambda value:client.chat.completions.create(**value),**ids)
send(0,defaults)
send(1,{**defaults,'extra_body':{'thinking':{'type':'disabled'}},'stream':False,
    'stream_options':{'include_usage':True},'reasoning_effort':'none','temperature':0.2})
assert len(seen)==2 and seen[1]['thinking']=={'type':'disabled'}
for wire in seen:
    assert wire['messages'][1]['content']==sys.argv[2]
    assert 'PRIVATE' not in json.dumps(wire)
    assert not set(wire)&{'input','instructions','history','system','prompt','metadata','stop'}
blocked=[{'input':'PRIVATE_INPUT'},{'instructions':'PRIVATE_INSTRUCTIONS'},
    {'extra_headers':{'X-History':'PRIVATE_HISTORY'}},{'stop':['PRIVATE_STOP']},
    {'extra_body':{'messages':[{'role':'user','content':'PRIVATE_MESSAGE'}]}},
    {'extra_body':{'input':'PRIVATE_INPUT'}},{'extra_body':{'instructions':'PRIVATE_INSTRUCTIONS'}},
    {'extra_body':{'history':['PRIVATE_HISTORY']}},{'extra_body':{'system':'PRIVATE_SYSTEM'}},
    {'extra_body':{'prompt':'PRIVATE_PROMPT'}},{'extra_body':{'metadata':{'profile':'PRIVATE_PROFILE'}}},
    {'extra_body':{'thinking':{'type':'disabled','instructions':'PRIVATE_INSTRUCTIONS'}}},
    {'temperature':float('nan')},{'max_tokens':True},{'stream':'PRIVATE_STREAM'},
    {'stream_options':{'include_usage':True,'instructions':'PRIVATE_INSTRUCTIONS'}},
    {'reasoning_effort':'PRIVATE_REASONING'},{'model':'PRIVATE MODEL PROMPT'}]
for index,taint in enumerate(blocked,2):
    before=len(seen)
    try: send(index,{**defaults,**taint})
    except CleanIngressBlocked: pass
    else: raise AssertionError('untrusted kwargs were admitted')
    assert len(seen)==before,'contaminated kwargs reached mock HTTP'
before=len(seen)
try: send(100,defaults,lambda value:value.update(extra_body={'history':['PRIVATE_LATE_HISTORY']}))
except CleanIngressBlocked: pass
else: raise AssertionError('bootstrap did not reject late override')
assert len(seen)==before
client.close()
print(json.dumps({'safe_http_calls':len(seen),'rejected_before_http':len(blocked)+1,
    'actual_xiaomi_keys':sorted(defaults),'real_network_attempts':len(network_attempts),'sdk_version':openai.__version__}))
'''
    child = subprocess.run([str(interpreter), '-B', '-c', program, str(sdk_root), SOURCE],
        env=environment, cwd=clean['workspace'], capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr[-4000:]
    proof = json.loads(child.stdout.strip().splitlines()[-1])
    assert proof['safe_http_calls'] == 2
    assert proof['rejected_before_http'] == 19
    assert proof['real_network_attempts'] == 0
    (clean['config'].parent / 'wire-proof.json').write_text(json.dumps(proof, sort_keys=True))


@pytest.mark.parametrize('corruption', ['session', 'state', 'fake_native', 'source_ledger', 'input'])
def test_parent_controller_rejects_changed_authority_and_source(clean, monkeypatch, corruption):
    api = clean['api']
    exchange = api._controller_exchange
    def changed(body):
        body = dict(body)
        if corruption == 'session':
            body['session_id'] = 'another-session'
        elif corruption == 'state':
            body['state_id'] = 'a' * 64
        elif corruption == 'fake_native':
            body.update(tool_name='rpe_task', tool_call_id='invented-model-call')
        elif corruption == 'source_ledger':
            with sqlite3.connect(clean['db']) as db:
                db.execute("UPDATE rpe_source_states SET document='{}'")
        else:
            (clean['workspace'] / 'orders.csv').write_text('id,value\nx,changed\n')
        return exchange(body)
    monkeypatch.setattr(api, '_controller_exchange', changed)
    result = dispatch(clean, extraction())
    assert result['status'] == 'rejected', result
    assert not (clean['workspace'] / 'out.csv').exists()
    assert not any(e['kind'] == 'rpe_controller_task_received' for e in clean['store'].list())


def test_concurrent_identical_controller_requests_publish_once(clean, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    api = clean['api']
    exchange = api._controller_exchange
    def concurrent(body):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(exchange, [body, body]))
        assert results[0] == results[1]
        return results[0]
    monkeypatch.setattr(api, '_controller_exchange', concurrent)
    assert dispatch(clean, extraction())['status'] == 'succeeded'
    assert len([e for e in clean['store'].list() if e['kind'] == 'rpe_controller_task_received']) == 1


def test_two_brokers_atomically_claim_one_controller_authority(clean, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from rpe_harness.integrations.hermes_broker import execution_broker
    api = clean['api']
    original = api.prepare_controller_execution
    barrier = Barrier(2)
    def synchronized(*args):
        prepared = original(*args)
        barrier.wait(timeout=5)
        return prepared
    monkeypatch.setattr(api, 'prepare_controller_execution', synchronized)
    def execute_from_two_brokers(body):
        def send(broker):
            request = Request(broker['RPE_HERMES_BROKER_URL'], data=json.dumps(body).encode(), method='POST',
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + broker['RPE_HERMES_BROKER_TOKEN']})
            try:
                with urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as error:
                error.close()
                return error.code, None
        with execution_broker(db_path=clean['db'], workspace=clean['workspace'], python=sys.executable) as first:
            with execution_broker(db_path=clean['db'], workspace=clean['workspace'], python=sys.executable) as second:
                with ThreadPoolExecutor(max_workers=2) as workers:
                    responses = list(workers.map(send, [first, second]))
        assert sorted(response[0] for response in responses) == [200, 403]
        return next(value['result'] for status, value in responses if status == 200)
    monkeypatch.setattr(api, '_controller_exchange', execute_from_two_brokers)
    assert dispatch(clean, extraction())['status'] == 'succeeded'
    assert len([e for e in clean['store'].list() if e['kind'] == 'rpe_controller_task_received']) == 1


def test_parent_controller_rechecks_catalog_authority(clean, monkeypatch):
    # SourceStateStore.resolve is the actual execution admission path. Revoke
    # the catalog trust anchor after understanding, before controller admission.
    from rpe_harness.integrations.hermes_clean import CleanWorkflowStore
    api, exchange = clean['api'], clean['api']._controller_exchange
    def revoke(body):
        from rpe_harness.integrations.skill_gate import _state
        state = _state(clean['db'], 'clean-session')
        state['catalog_sha256'] = 'a' * 64
        CleanWorkflowStore(clean['db']).put(state)
        return exchange(body)
    monkeypatch.setattr(api, '_controller_exchange', revoke)
    result = dispatch(clean, extraction())
    assert result['status'] == 'rejected', result
    assert not (clean['workspace'] / 'out.csv').exists()


def test_parent_controller_rechecks_revoked_xlsx_skill_after_understanding(clean, monkeypatch):
    import shutil
    from openpyxl import Workbook
    from rpe_harness.skill_catalog import SkillCatalog
    from rpe_harness.integrations.hermes_broker import execution_broker
    from rpe_harness.integrations.hermes_address import _catalog
    env = clean
    source = env['config'].parent / 'source'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx', source / 'productivity/xlsx')
    catalog_root = env['config'].parent / 'catalog'
    catalog = SkillCatalog(env['db'], catalog_root)
    snapshot = catalog.sync(source)['snapshot_id']
    catalog.trust_adapter('productivity/xlsx', snapshot_id=snapshot)
    book = Workbook()
    book.active.append(['a', 1])
    book.save(env['workspace'] / 'book.xlsx')
    raw = json.dumps({'capability': 'xlsx.inspect', 'constraints': ['preserve_original'],
        'execution': {'input_path': 'book.xlsx', 'output_path': 'out.json', 'mode': 'rows'}})
    unified = _catalog(env['db'], catalog_root, snapshot)
    env['config'].write_text(json.dumps({'catalog_sha256': unified.snapshot_id,
        'entries': unified.entries(), 'raw': raw, 'source_ref': 'current-request:xlsx'}))
    env['sdk'] = SDK()
    env['api'].register_clean_workflow(env['sdk'], store=env['store'], workspace=env['workspace'],
        python=sys.executable, error_path=env['config'].parent / 'errors')
    extension = env['api'].clean_extension(env['db'], env['workspace'], raw=raw,
        source_ref='current-request:xlsx', catalog_root=catalog_root, snapshot_id=snapshot)
    exchange = env['api']._controller_exchange
    def revoke(body):
        (source / 'productivity/xlsx/SKILL.md').write_text('revoked after understanding')
        return exchange(body)
    monkeypatch.setattr(env['api'], '_controller_exchange', revoke)
    with execution_broker(db_path=env['db'], workspace=env['workspace'], python=sys.executable, extension=extension) as broker:
        for key, value in broker.items():
            monkeypatch.setenv(key, value)
        result = dispatch(env, {'goal': 'Execute xlsx.inspect', **json.loads(raw)})
    assert result['status'] == 'rejected', result
    assert not (env['workspace'] / 'out.json').exists()
    assert not any(e['kind'] == 'rpe_controller_task_received' for e in env['store'].list())

