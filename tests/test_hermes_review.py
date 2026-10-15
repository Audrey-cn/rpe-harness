"""Current prose produces a reviewable draft, never model execution authority."""
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


RAW = ('Deduplicate orders.csv by id, keep first, save out.csv, preserve original, '
       'use utf-8 encoding and comma delimiter.')


class ReviewSDK(SDK):
    def __init__(self):
        super().__init__()
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def draft():
    return {'capability': 'tabular.deduplicate', 'constraints': ['preserve_original'],
        'execution': {'input_path': 'orders.csv', 'output_path': 'out.csv',
            'key_columns': ['id'], 'keep': 'first', 'encoding': 'utf-8', 'delimiter': ','},
        'requirements': [
            {'text': 'Deduplicate rows.', 'quote': 'Deduplicate', 'fields': ['/capability']},
            {'text': 'Preserve the original.', 'quote': 'preserve original', 'fields': ['/constraints']},
            {'text': 'Read orders.csv.', 'quote': 'orders.csv', 'fields': ['/execution/input_path']},
            {'text': 'Write out.csv.', 'quote': 'out.csv', 'fields': ['/execution/output_path']},
            {'text': 'Match the id column.', 'quote': 'by id', 'fields': ['/execution/key_columns']},
            {'text': 'Keep the first row.', 'quote': 'keep first', 'fields': ['/execution/keep']},
            {'text': 'Read utf-8.', 'quote': 'utf-8', 'fields': ['/execution/encoding']},
            {'text': 'Use comma separator.', 'quote': 'comma delimiter', 'fields': ['/execution/delimiter']},
        ], 'unresolved': []}


def test_review_integration_has_no_execution_entry():
    assert importlib.util.find_spec('rpe_harness.integrations.hermes_review') is not None
    api = importlib.import_module('rpe_harness.integrations.hermes_review')
    assert callable(api.run_hermes_review)


def test_review_launcher_and_native_arm_share_public_draft_contract(tmp_path):
    from rpe_harness.integrations.hermes_review import REVIEW_CONTRACT
    from rpe_harness.integrations.hermes_session import prepare_home
    settings = {'provider': 'xiaomi', 'model': 'test', 'api_key': 'no-model',
                'base_url': 'https://example.invalid/v1'}
    review = prepare_home(tmp_path / 'review', tmp_path, tmp_path / 'runs.sqlite',
        tool_enabled=True, entry_mode='review', public_contract='review', settings=settings)
    native = prepare_home(tmp_path / 'native', tmp_path, tmp_path / 'native.sqlite',
        tool_enabled=False, public_contract='review', preserve_native_tool_search=True, settings=settings)
    review_config, native_config = [json.loads((home / 'config.yaml').read_text()) for home in (review, native)]
    assert review_config['agent']['system_prompt'] == native_config['agent']['system_prompt'] == REVIEW_CONTRACT
    assert review_config['platform_toolsets']['cli'] == ['rpe_harness']
    assert review_config['tools']['tool_search']['enabled'] == 'off'
    assert 'tools' not in native_config


def test_review_rejects_hidden_contract_and_history_before_host_setup(tmp_path):
    from rpe_harness.integrations.hermes_session import run_hermes_session
    for arguments in ({'required_request': {'hidden': 'answer'}}, {'context_sources': [{'hidden': 'history'}]}):
        with pytest.raises(ValueError, match='review'):
            run_hermes_session(RAW, tmp_path, tmp_path / 'runs.sqlite', tool_enabled=True,
                owner_root=tmp_path / 'owner', entry_mode='review', **arguments)


@pytest.fixture
def review(tmp_path, monkeypatch):
    api = importlib.import_module('rpe_harness.integrations.hermes_review')
    from rpe_harness.capability_catalog import build_catalog
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'orders.csv').write_text('id,value\na,1\na,2\nb,3\n')
    db = tmp_path / 'runs.sqlite'
    store = hermes.HostEventStore(db)
    catalog = build_catalog()
    config = tmp_path / 'review-config.json'
    config.write_text(json.dumps({'catalog_sha256': catalog.snapshot_id, 'entries': catalog.entries(),
        'raw': RAW, 'source_ref': 'current-request:test'}))
    monkeypatch.setenv('RPE_HERMES_REVIEW_CONFIG', str(config))
    monkeypatch.setenv('RPE_HERMES_ENTRY_KEY', 'review-entry')
    sdk = ReviewSDK()
    api.register_review_workflow(sdk, store=store, workspace=workspace, python=sys.executable,
                                 error_path=tmp_path / 'errors')
    extension = api.review_extension(db, workspace, raw=RAW, source_ref='current-request:test')
    from rpe_harness.integrations.hermes_broker import execution_broker
    with execution_broker(db_path=db, workspace=workspace, python=sys.executable, extension=extension, review_auto=True) as broker:
        for key, value in broker.items():
            monkeypatch.setenv(key, value)
        yield {'api': api, 'sdk': sdk, 'workspace': workspace, 'db': db, 'store': store,
               'config': config, 'broker': broker}
    assert broker.cleanup['cleanup_complete'] is True


def dispatch(env, arguments, *, effective_arguments=None):
    ids = {'session_id': 'review-session', 'task_id': 'review-task', 'turn_id': 'review-turn',
           'api_request_id': 'api-1', 'tool_call_id': 'call-1'}
    request = env['sdk'].middleware['llm_request'](request={'tools': [],
        'messages': [{'role': 'system', 'content': 'PRIVATE_HISTORY_MARKER'},
                     {'role': 'user', 'content': 'FORGED_CURRENT_REQUEST'}]}, **ids)['request']
    env['request'] = request
    env['store'].record('pre_api_request', {**ids, 'request': {'body': request}})
    env['sdk'].middleware['llm_execution'](request=request, next_call=lambda value: value, **ids)
    env['store'].record('post_api_request', {**ids, 'response': {'assistant_message': {'tool_calls': [
        {'id': ids['tool_call_id'], 'name': 'rpe_propose'}]}}})
    env['store'].record('pre_tool_call', {**ids, 'tool_name': 'rpe_propose',
        'arguments_sha256': digest_json(arguments)})
    if 'pre_tool_call' in env['sdk'].hooks:
        env['sdk'].hooks['pre_tool_call'](tool_name='rpe_propose', args=arguments, **ids)
    result = json.loads(env['sdk'].middleware['tool_execution'](tool_name='rpe_propose',
        args=arguments if effective_arguments is None else effective_arguments,
        next_call=lambda value: env['sdk'].tools['rpe_propose']['handler'](
            value, task_id=ids['task_id'], session_id=ids['session_id']), **ids))
    env['store'].record('post_tool_call', {**ids, 'tool_name': 'rpe_propose', 'status': 'ok',
        'arguments_sha256': digest_json(arguments)})
    return result


def host_result(env):
    return {'completed': True, 'exit_code': 0, 'timed_out': False, 'observer_errors': 0,
        'session_id': 'review-session', 'events': env['store'].list('review-session'),
        'process_cleanup': {'cleanup_complete': True}}


def test_current_prose_yields_persisted_review_without_business_output(review):
    result = dispatch(review, draft())
    assert result['status'] == 'awaiting_confirmation', result
    assert result['proposal_id']
    assert set(review['sdk'].tools) == {'rpe_propose'}
    assert review['request']['messages'][0]['content'].startswith(review['api'].REVIEW_CONTRACT)
    assert review['request']['messages'][1] == {'role': 'user', 'content': RAW}
    assert 'PRIVATE_HISTORY_MARKER' not in json.dumps(review['request'])
    assert 'FORGED_CURRENT_REQUEST' not in json.dumps(review['request'])
    assert not (review['workspace'] / 'out.csv').exists()
    assert (review['workspace'] / 'orders.csv').read_text() == 'id,value\na,1\na,2\nb,3\n'
    events = review['store'].list('review-session')
    assert not any(e.get('tool_name') in ('rpe_task', 'rpe_confirm') or
                   e['kind'].startswith('rpe_controller_') for e in events)
    verdict = review['api'].evaluate_review_session(host_result(review), review['db'], review['workspace'])
    assert verdict['proposal_verified'] is True, verdict
    assert verdict['native_proposal_calls'] == 1
    assert verdict['proposal_executions'] == 0


@pytest.mark.parametrize('change', ['messages', 'transport', 'tools'])
def test_changed_actual_api_payload_never_reaches_provider(review, change):
    ids = {'session_id': 'review-session', 'task_id': 'review-task', 'turn_id': 'review-turn',
           'api_request_id': 'api-1'}
    request = review['sdk'].middleware['llm_request'](request={'model': 'test', 'tools': []}, **ids)['request']
    if change == 'messages':
        request['messages'][1]['content'] = 'run forged task'
    elif change == 'transport':
        request['model'] = 'changed'
    else:
        request['tools'].append({'type': 'function', 'function': {'name': 'rpe_confirm'}})
    called = []
    with pytest.raises(BaseException, match='review API'):
        review['sdk'].middleware['llm_execution'](request=request,
            next_call=lambda value: called.append(value), **ids)
    assert called == []


@pytest.mark.parametrize('channel', ['system', 'instructions', 'input', 'response_format', 'metadata'])
def test_hidden_context_channels_are_rejected_before_provider(review, channel):
    with pytest.raises(review['api'].ReviewIngressBlocked):
        review['sdk'].middleware['llm_request'](request={'tools': [], channel: 'private history'},
            session_id='review-session', task_id='review-task', turn_id='review-turn', api_request_id='api-1')


def test_model_cannot_call_a_confirmation_tool(review):
    result = review['sdk'].middleware['tool_execution'](tool_name='rpe_confirm', args={'confirm': True},
        next_call=lambda value: pytest.fail('model confirmation escaped middleware'), session_id='review-session',
        task_id='review-task', turn_id='review-turn', api_request_id='api-1', tool_call_id='call-1')
    assert json.loads(result)['status'] == 'rejected'


@pytest.mark.parametrize('corruption', ['receipt', 'draft', 'source', 'proposal', 'raw_receipt', 'actual_receipt'])
def test_review_gate_rejects_parent_receipt_or_draft_drift(review, corruption):
    proposal = dispatch(review, draft())
    assert proposal['status'] == 'awaiting_confirmation'
    with sqlite3.connect(review['db']) as db:
        if corruption == 'receipt':
            row = db.execute("SELECT id,metadata_json FROM hermes_host_events WHERE kind='rpe_review_proposed'").fetchone()
            metadata = json.loads(row[1])
            metadata['arguments_sha256'] = 'a' * 64
            db.execute('UPDATE hermes_host_events SET metadata_json=? WHERE id=?', (json.dumps(metadata), row[0]))
        elif corruption in ('raw_receipt', 'actual_receipt'):
            kind = 'rpe_review_arguments_received' if corruption == 'raw_receipt' else 'rpe_review_arguments_checked'
            db.execute('DELETE FROM hermes_host_events WHERE kind=?', (kind,))
        elif corruption == 'proposal':
            db.execute('UPDATE rpe_proposals SET document=? WHERE proposal_id=?', (b'{}', proposal['proposal_id']))
        else:
            state = json.loads(db.execute('SELECT state_json FROM rpe_skill_workflows').fetchone()[0])
            if corruption == 'source':
                state['source_raw'] = 'a different task'
            else:
                state['draft']['execution']['output_path'] = 'changed.csv'
            db.execute('UPDATE rpe_skill_workflows SET state_json=?,state_sha256=?',
                       (json.dumps(state), digest_json(state)))
    result = host_result(review)
    verdict = review['api'].evaluate_review_session(result, review['db'], review['workspace'])
    assert verdict['proposal_verified'] is False


def test_unknown_argument_transform_is_blocked_before_parent_rpc(review):
    original, actual = draft(), draft()
    actual['execution']['output_path'] = 'different.csv'
    result = dispatch(review, original, effective_arguments=actual)
    assert result['status'] == 'rejected'
    assert not any(event['kind'] == 'rpe_skill_rpc_received' for event in review['store'].list())
    assert not (review['workspace'] / 'out.csv').exists()
    assert not (review['workspace'] / 'different.csv').exists()


def test_unresolved_business_clause_is_a_verified_blocked_draft(review):
    arguments = draft()
    arguments['unresolved'] = ['Also email the result; no email capability is available.']
    result = dispatch(review, arguments)
    assert result['status'] == 'needs_context', result
    assert result['missing']
    assert not (review['workspace'] / 'out.csv').exists()
    verdict = review['api'].evaluate_review_session(host_result(review), review['db'], review['workspace'])
    assert verdict['proposal_verified'] is True, verdict
    assert verdict['proposal_executions'] == 0


def test_two_api_budget_blocks_a_third_request(review):
    assert dispatch(review, draft())['status'] == 'awaiting_confirmation'
    ids = {'session_id': 'review-session', 'task_id': 'review-task', 'turn_id': 'review-turn',
           'api_request_id': 'api-report'}
    report = review['sdk'].middleware['llm_request'](request={'tools': []}, **ids)['request']
    assert report['tool_choice'] == 'auto'
    assert report['tools'] == []
    with pytest.raises(review['api'].ReviewIngressBlocked):
        review['sdk'].middleware['llm_request'](request={'tools': []}, **{**ids, 'api_request_id': 'api-third'})


def test_bootstrap_guard_denies_missing_review_registration(monkeypatch):
    from types import SimpleNamespace
    from rpe_harness.integrations import hermes_bootstrap
    monkeypatch.setenv('RPE_HERMES_ENTRY_MODE', 'review')
    middleware = SimpleNamespace(run_llm_execution_middleware=lambda *a, **k: pytest.fail('provider reached'))
    plugins = SimpleNamespace(get_plugin_manager=lambda: SimpleNamespace(_plugins={}))
    original_import = hermes_bootstrap.importlib.import_module
    monkeypatch.setattr(hermes_bootstrap.importlib, 'import_module', lambda name:
        {'hermes_cli.middleware': middleware, 'hermes_cli.plugins': plugins}.get(name) or original_import(name))
    hermes_bootstrap.install_clean_ingress_guard()
    with pytest.raises(BaseException, match='review plugin'):
        middleware.run_llm_execution_middleware({}, lambda value: pytest.fail('provider reached'))


def test_request_identity_cannot_reset_actual_api_budget(review):
    ids = {'session_id': 'review-session', 'task_id': 'review-task', 'turn_id': 'review-turn',
           'api_request_id': 'api-1'}
    review['sdk'].middleware['llm_request'](request={'tools': []}, **ids)
    with pytest.raises(review['api'].ReviewIngressBlocked):
        review['sdk'].middleware['llm_request'](request={'tools': []}, **ids)


def _sdk_home(env, name):
    sdk_root = Path(os.environ.get('RPE_HERMES_SOURCE', '/Users/audrey/.hermes/hermes-agent'))
    interpreter = sdk_root / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('installed Hermes SDK unavailable')
    from rpe_harness.integrations.hermes_session import prepare_home
    import rpe_harness
    package_import_root = str(Path(rpe_harness.__file__).resolve().parent.parent)
    home = prepare_home(env['config'].parent / name, env['workspace'], env['db'],
        tool_enabled=True, entry_mode='review', public_contract='review',
        settings={'provider': 'xiaomi', 'model': 'test', 'api_key': 'fake-no-model-key',
                  'base_url': 'https://example.invalid/v1'})
    environment = {**os.environ, 'HOME': str(home), 'XDG_CACHE_HOME': str(home / 'cache'),
        'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(home / 'runtime'),
        'HERMES_SAFE_MODE': '0', 'HERMES_ENABLE_PROJECT_PLUGINS': '0',
        'HERMES_DISABLE_LAZY_INSTALLS': '1', 'RPE_HERMES_DB': str(env['db']),
        'RPE_HERMES_WORKSPACE': str(env['workspace']), 'RPE_HARNESS_PYTHON': sys.executable,
        'RPE_HERMES_TOOL_ENABLED': '1', 'RPE_HARNESS_IMPORT_ROOT': package_import_root,
        'RPE_HERMES_ENTRY_MODE': 'review', 'RPE_HERMES_ERROR_PATH': str(home / 'observer-errors'),
        'RPE_HERMES_REVIEW_DIAGNOSTICS': str(home / 'review-diagnostics.jsonl'),
        'PYTHONPATH': package_import_root + os.pathsep + str(sdk_root)}
    environment.pop('HERMES_IGNORE_USER_CONFIG', None)
    return interpreter, environment


@pytest.mark.parametrize('argument_case,expected_status', [
    ('valid', 'awaiting_confirmation'), ('malformed_arrays', 'needs_context'),
    ('capability_description', 'unsupported'), ('illegal_pointer', 'needs_context'),
    ('xlsx_wrong_flag', 'unsupported')])
def test_installed_sdk_loads_validates_and_invokes_proposal_without_network(review, argument_case, expected_status):
    interpreter, environment = _sdk_home(review, 'sdk-session')
    program = '''import json,sys,socket
from types import SimpleNamespace
from hermes_cli.plugins import get_plugin_manager
from agent.api_request_hooks import ApiRequestHooksMixin
from agent.agent_init import _load_tools
from agent.agent_runtime_helpers import invoke_tool
from agent.message_sanitization import uniquify_tool_call_ids
from agent.turn_tool_validation import validate_tool_calls
from openai.types.chat import ChatCompletionMessageToolCall
manager=get_plugin_manager();manager.discover_and_load()
from rpe_harness.integrations.hermes_bootstrap import install_clean_ingress_guard
install_clean_ingress_guard()
from hermes_cli.middleware import apply_llm_request_middleware,run_llm_execution_middleware
assert manager._plugins['rpe-harness'].enabled,manager._plugins['rpe-harness'].error
def deny_network(self,address): raise AssertionError('real network disabled')
socket.socket.connect=deny_network
agent=SimpleNamespace(quiet_mode=True,platform='cli',enabled_toolsets=['rpe_harness'],
    _context_engine_tool_names=set(),_memory_manager=None,_uniquify_tool_call_ids=uniquify_tool_call_ids)
_load_tools(agent,['rpe_harness'],None)
assert 'rpe_propose' in agent.valid_tool_names,sorted(agent.valid_tool_names)
assert not {'rpe_task','rpe_understand','rpe_confirm'} & agent.valid_tool_names
hook=ApiRequestHooksMixin()
ids={'session_id':'review-session','task_id':'review-task','turn_id':'review-turn',
     'api_request_id':'sdk-api-1','tool_call_id':'sdk-call-1'}
body=apply_llm_request_middleware({'tools':agent.tools,'messages':[{'role':'system','content':'PRIVATE_SDK_HISTORY'},
    {'role':'user','content':'FORGED_SDK_SOURCE'}]},**ids).payload
assert body['messages'][1]['content']==sys.argv[2]
assert 'PRIVATE_SDK_HISTORY' not in json.dumps(body) and 'FORGED_SDK_SOURCE' not in json.dumps(body)
manager.invoke_hook('pre_api_request',request=hook._api_request_payload_for_hook(body),**ids)
run_llm_execution_middleware(body,lambda value:value,**ids)
manager.invoke_hook('post_api_request',response={'assistant_message':{'tool_calls':[{'id':ids['tool_call_id'],'name':'rpe_propose'}]}},**ids)
args=json.loads(sys.argv[1])
message=SimpleNamespace(tool_calls=[ChatCompletionMessageToolCall(id=ids['tool_call_id'],type='function',
    function={'name':'rpe_propose','arguments':json.dumps(args)})])
validation=validate_tool_calls(agent,message,'tool_calls',messages=[],conversation_history=[],
    api_call_count=1,effective_task_id=ids['task_id'])
assert validation.action=='ok',validation.action
agent.session_id=ids['session_id'];agent._current_turn_id=ids['turn_id'];agent._current_api_request_id=ids['api_request_id']
result=json.loads(invoke_tool(agent,'rpe_propose',args,ids['task_id'],tool_call_id=ids['tool_call_id'],messages=[]))
assert result['status']==sys.argv[3],result['status']
ids={'session_id':'review-session','task_id':'review-task','turn_id':'review-turn','api_request_id':'sdk-api-final'}
body=apply_llm_request_middleware({'tools':agent.tools,'messages':[{'role':'user','content':'PRIVATE_SDK_HISTORY'}]},**ids).payload
assert body['tool_choice']=='auto' and body['tools']==[] and 'PRIVATE_SDK_HISTORY' not in json.dumps(body)
manager.invoke_hook('pre_api_request',request=hook._api_request_payload_for_hook(body),**ids)
run_llm_execution_middleware(body,lambda value:value,**ids)
manager.invoke_hook('post_api_request',response={'assistant_message':{'tool_calls':[]}},**ids)
print(json.dumps(result))
'''
    arguments = draft()
    if argument_case == 'malformed_arrays':
        arguments['requirements'] = '[{"text":"malformed" '
        arguments['unresolved'] = '["malformed" '
    elif argument_case == 'capability_description':
        arguments['capability'] = 'tabular.deduplicate — deduplicate the source rows'
    elif argument_case == 'illegal_pointer':
        arguments['requirements'][0]['fields'] = ['/unresolved']
    elif argument_case == 'xlsx_wrong_flag':
        arguments['capability'] = 'xlsx.inspect'
        arguments['constraints'] = ['preserve_original', 'preserve_order']
        arguments['execution'] = {'input_path': 'orders.csv', 'output_path': 'out.csv', 'mode': 'rows'}
    child = subprocess.run([str(interpreter), '-B', '-c', program, json.dumps(arguments), RAW, expected_status],
        cwd=review['workspace'], env=environment, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr[-4000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result['status'] == expected_status
    assert not (review['workspace'] / 'out.csv').exists()
    verdict = review['api'].evaluate_review_session(host_result(review), review['db'], review['workspace'])
    assert verdict['proposal_verified'] is True, verdict
    assert verdict['reporting_api_requests'] == 1
    if argument_case == 'malformed_arrays':
        assert result['requirements'] == []
        assert verdict['arguments_transform'] == 'array_string_wrapped'
        assert verdict['post_arguments_phase'] == 'effective_after_coercion'
        assert result['draft']['requirements'] == [arguments['requirements']]
        with sqlite3.connect(review['db']) as db:
            assert db.execute('SELECT count(*) FROM rpe_confirmations').fetchone()[0] == 0


def test_mimo_auto_wire_policy_keeps_single_dispatch_guard(review):
    interpreter, environment = _sdk_home(review, 'wire-session')
    program = '''import json,sys,socket,os
import openai,httpx
from hermes_cli.plugins import get_plugin_manager
manager=get_plugin_manager();manager.discover_and_load()
assert manager._plugins['rpe-harness'].enabled,manager._plugins['rpe-harness'].error
from rpe_harness.integrations.hermes_bootstrap import install_clean_ingress_guard
install_clean_ingress_guard()
from hermes_cli.middleware import apply_llm_request_middleware,run_llm_execution_middleware
from rpe_harness.integrations.hermes_review import ReviewIngressBlocked
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
from agent.sdk_transform_bypass import bypass_chat_sdk_request_transform
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
    return run_llm_execution_middleware(projected,lambda value:client.chat.completions.create(**bypass_chat_sdk_request_transform(value,client)),**ids)
send(0,defaults)
send(1,{**defaults,'extra_body':{'thinking':{'type':'disabled'}},'stream':False,
    'stream_options':{'include_usage':True},'reasoning_effort':'none','temperature':0.2})
assert len(seen)==2 and seen[1]['thinking']=={'type':'disabled'}
for wire in seen:
    assert wire['messages'][1]['content']==sys.argv[1]
    assert 'PRIVATE' not in json.dumps(wire)
    assert wire['tool_choice']=='auto'
    assert 'parallel_tool_calls' not in wire
    assert [tool['function']['name'] for tool in wire['tools']]==['rpe_propose']
    params=wire['tools'][0]['function']['parameters']['properties']
    assert params['capability']['enum']==['json.normalize','tabular.deduplicate','xlsx.inspect','unsupported']
    assert params['constraints']['items']['enum']==['preserve_formulas','preserve_order','preserve_original']
    execution=params['execution']['properties']
    assert execution['keep']['enum']==['first']
    assert execution['style']['enum']==['minify','pretty']
    assert execution['mode']['enum']==['formulas','rows','sheets']
    rows=params['requirements']
    assert rows['minItems']==1 and rows['maxItems']==32
    pointers=rows['items']['properties']['fields']['items']['enum']
    assert pointers==['/capability','/constraints','/execution/delimiter','/execution/encoding',
        '/execution/input_path','/execution/keep','/execution/key_columns','/execution/mode',
        '/execution/output_path','/execution/sheet','/execution/style']
    assert '/unresolved' not in pointers
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
    except ReviewIngressBlocked: pass
    else: raise AssertionError('untrusted kwargs were admitted')
    assert len(seen)==before,'contaminated kwargs reached mock HTTP'
before=len(seen)
try: send(100,defaults,lambda value:value.update(extra_body={'history':['PRIVATE_LATE_HISTORY']}))
except ReviewIngressBlocked: pass
else: raise AssertionError('bootstrap did not reject late override')
assert len(seen)==before
plugin=manager._plugins['rpe-harness'];plugin.enabled=False
try: send(101,defaults)
except BaseException: pass
else: raise AssertionError('missing plugin reached provider')
assert len(seen)==before
client.close()
print(json.dumps({'safe_http_calls':len(seen),'rejected_before_http':len(blocked)+2,
    'actual_xiaomi_keys':sorted(defaults),'real_network_attempts':len(network_attempts),'sdk_version':openai.__version__}))
'''
    child = subprocess.run([str(interpreter), '-B', '-c', program, RAW], env=environment,
        cwd=review['workspace'], capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr[-4000:]
    proof = json.loads(child.stdout.strip().splitlines()[-1])
    assert proof['safe_http_calls'] == 2
    assert proof['rejected_before_http'] == 20
    assert proof['real_network_attempts'] == 0
