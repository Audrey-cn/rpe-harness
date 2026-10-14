"""Real parent broker, immutable states and executor; only the SDK/model is simulated."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys

import pytest

from rpe_harness.integrations import hermes
from rpe_harness.integrations.hermes_gate import digest_json


class SDK:
    def __init__(self):
        self.tools, self.middleware = {}, {}

    def register_tool(self, **value):
        self.tools[value['name']] = value

    def register_middleware(self, name, function):
        self.middleware[name] = function


@pytest.fixture
def address_api():
    assert importlib.util.find_spec('rpe_harness.integrations.hermes_address'), 'address workflow missing'
    return importlib.import_module('rpe_harness.integrations.hermes_address')


@pytest.fixture
def configured(tmp_path, monkeypatch, address_api, request):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'orders.csv').write_text('id,value\na,1\na,2\nb,3\n')
    (workspace / 'raw.json').write_text('{"z":1,"a":[2,3]}')
    db = tmp_path / 'db.sqlite'
    store = hermes.HostEventStore(db)
    catalog_root, snapshot_id, skill_catalog, source = None, None, None, None
    if getattr(request, 'param', None) == 'trusted':
        from rpe_harness.skill_catalog import SkillCatalog
        source = tmp_path / 'source'
        shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx', source / 'productivity/xlsx')
        catalog_root = tmp_path / 'catalog'
        skill_catalog = SkillCatalog(db, catalog_root)
        snapshot_id = skill_catalog.sync(source)['snapshot_id']
        skill_catalog.trust_adapter('productivity/xlsx', snapshot_id=snapshot_id)
    extension = address_api.address_extension(db, workspace, source_ref='prompt:public-orders',
        catalog_root=catalog_root, snapshot_id=snapshot_id,
        source_facts=request.param if isinstance(getattr(request, 'param', None), dict) else None)
    from rpe_harness.capability_catalog import build_catalog
    catalog = build_catalog(db_path=db if catalog_root else None, catalog_root=catalog_root,
                            skill_snapshot_id=snapshot_id)
    config = tmp_path / 'address.json'
    config.write_text(json.dumps({'catalog_sha256': catalog.snapshot_id, 'entries': catalog.entries()}))
    monkeypatch.setenv('RPE_HERMES_ADDRESS_CONFIG', str(config))
    monkeypatch.setenv('RPE_HERMES_ENTRY_KEY', 'address-entry-test')
    monkeypatch.delenv('RPE_HERMES_REQUIRED_REQUEST_PATH', raising=False)
    monkeypatch.delenv('RPE_HERMES_INPUT_SHA256', raising=False)
    sdk = SDK()
    address_api.register_address_workflow(sdk, store=store, workspace=workspace, python=sys.executable,
                                         error_path=tmp_path / 'observer-errors')
    from rpe_harness.integrations.hermes_broker import execution_broker
    with execution_broker(db_path=db, workspace=workspace, python=sys.executable, extension=extension) as broker:
        for key, value in broker.items():
            monkeypatch.setenv(key, value)
        yield {'sdk': sdk, 'store': store, 'db': db, 'workspace': workspace,
               'counter': 0, 'api': address_api, 'config': config, 'session': 'address-session',
               'catalog_root': catalog_root, 'skill_catalog': skill_catalog, 'source': source}
    assert broker.cleanup['cleanup_complete'] is True


def dispatch(env, name, arguments, *, actual_choice=None):
    env['counter'] += 1
    ids = {'session_id': env['session'], 'task_id': 'address-task', 'turn_id': 'address-turn',
           'api_request_id': f'api-{env["counter"]}', 'tool_call_id': f'call-{env["counter"]}'}
    schemas = [{'type': 'function', 'function': row['schema']} for row in env['sdk'].tools.values()]
    request = env['sdk'].middleware['llm_request'](request={'tools': schemas}, **ids)['request']
    if actual_choice is not None:
        request['tool_choice'] = actual_choice
    env['store'].record('pre_api_request', {**ids, 'request': {'body': request}})
    try:
        env['sdk'].middleware['llm_execution'](request=request, next_call=lambda value: value, **ids)
    except ValueError:
        if actual_choice is None:
            raise
    env['store'].record('post_api_request', {**ids, 'response': {'assistant_message': {'tool_calls': [
        {'id': ids['tool_call_id'], 'name': name}]}}})
    env['store'].record('pre_tool_call', {**ids, 'tool_name': name, 'args': arguments})
    def next_call(value):
        return env['sdk'].tools[name]['handler'](value, task_id=ids['task_id'], session_id=ids['session_id'])
    result = json.loads(env['sdk'].middleware['tool_execution'](
        tool_name=name, args=arguments, next_call=next_call, **ids))
    env['store'].record('post_tool_call', {**ids, 'tool_name': name, 'status': 'ok', 'args': arguments,
        **{key: result[key] for key in ('run_id', 'receipt_id', 'contract_sha256', 'input_sha256') if key in result}})
    return result


def final_result(env):
    env['counter'] += 1
    ids = {'session_id': env['session'], 'task_id': 'address-task', 'turn_id': 'address-turn',
           'api_request_id': f'api-{env["counter"]}'}
    schemas = [{'type': 'function', 'function': row['schema']} for row in env['sdk'].tools.values()]
    request = env['sdk'].middleware['llm_request'](request={'tools': schemas}, **ids)['request']
    env['store'].record('pre_api_request', {**ids, 'request': {'body': request}})
    env['sdk'].middleware['llm_execution'](request=request, next_call=lambda value: value, **ids)
    env['store'].record('post_api_request', {**ids, 'response': {'assistant_message': {'tool_calls': []}}})
    return {'completed': True, 'exit_code': 0, 'timed_out': False, 'observer_errors': 0,
            'session_id': env['session'], 'session_root': str(env['config'].parent),
            'process_cleanup': {'cleanup_complete': True}, 'events': env['store'].list(env['session'])}


def extracted(**updates):
    value = {'goal': 'Retain first order per id', 'capability': 'tabular.deduplicate',
             'constraints': ['preserve_original', 'preserve_order'],
             'execution': {'input_path': 'orders.csv', 'output_path': 'out.csv', 'key_columns': ['id']}}
    value.update(updates)
    return value


def gate(env):
    return env['api'].evaluate_address_session(final_result(env), env['db'], env['workspace'],
                                              catalog_root=env['catalog_root'])


def test_state_only_handoff_executes_real_csv_and_independent_gate(configured):
    env = configured
    handoff = dispatch(env, 'rpe_understand', extracted())
    assert set(handoff) == {'state_id', 'capability', 'constraints', 'catalog_sha256', 'status'}
    assert handoff['status'] == 'ready'
    result = dispatch(env, 'rpe_task', {'state_id': handoff['state_id']})
    assert result['status'] == 'succeeded', result
    assert (env['workspace'] / 'out.csv').read_text(encoding='utf-8-sig') == 'id,value\na,1\nb,3\n'
    verdict = gate(env)
    assert verdict['harness_verified'] is True, verdict
    assert verdict['capability'] == 'tabular.deduplicate'
    assert not any('orders.csv' in json.dumps(e) or 'key_columns' in json.dumps(e) for e in env['store'].list())


@pytest.mark.parametrize('case,expected', [('missing', 'needs_context'), ('unsupported', 'unsupported')])
def test_missing_and_unsupported_are_durable_terminal_rejections(configured, case, expected):
    env = configured
    args = extracted()
    if case == 'missing':
        del args['execution']['key_columns']
    else:
        args['capability'] = 'tabular.forecast'
    result = dispatch(env, 'rpe_understand', args)
    assert result['status'] == expected
    verdict = gate(env)
    assert verdict['status'] == 'accepted_rejection', verdict
    assert verdict['rejection_reason'] == expected
    assert not (env['workspace'] / 'out.csv').exists()
    assert not any(e['kind'] == 'rpe_task_received' for e in env['store'].list())


def test_task_cannot_rewrite_anchored_business_arguments(configured):
    env = configured
    handoff = dispatch(env, 'rpe_understand', extracted())
    result = dispatch(env, 'rpe_task', {'state_id': handoff['state_id'], 'key_columns': ['value']})
    assert result['status'] == 'rejected'
    assert not (env['workspace'] / 'out.csv').exists()


@pytest.mark.parametrize('fault', ['changed_input', 'different_state', 'changed_catalog'])
def test_invalidated_state_never_reaches_executor(configured, fault):
    env = configured
    handoff = dispatch(env, 'rpe_understand', extracted())
    if fault == 'changed_input':
        (env['workspace'] / 'orders.csv').write_text('id,value\nc,9\n')
    elif fault == 'different_state':
        handoff['state_id'] = 'state-from-another-session'
    else:
        config = json.loads(env['config'].read_text())
        config['catalog_sha256'] = '0' * 64
        env['config'].write_text(json.dumps(config))
    result = dispatch(env, 'rpe_task', {'state_id': handoff['state_id']})
    assert result['status'] == 'rejected'
    assert not (env['workspace'] / 'out.csv').exists()
    assert not any(e['kind'] == 'rpe_task_received' for e in env['store'].list())


def test_only_three_invalid_understanding_attempts_admitted(configured):
    env = configured
    for _ in range(3):
        value = dispatch(env, 'rpe_understand', extracted(unrecognized='model error'))
        assert value['status'] == 'rejected'
    value = dispatch(env, 'rpe_understand', extracted())
    assert value['status'] == 'rejected'
    assert not (env['workspace'] / 'out.csv').exists()


def test_actual_provider_policy_cannot_be_replaced(configured):
    env = configured
    result = dispatch(env, 'rpe_understand', extracted(), actual_choice='none')
    assert result['status'] == 'rejected'
    assert not any(e['kind'] == 'rpe_address_understood' for e in env['store'].list())


def test_json_normalization_uses_same_state_entry(configured):
    env = configured
    handoff = dispatch(env, 'rpe_understand', extracted(capability='json.normalize', constraints=['preserve_original'],
        execution={'input_path': 'raw.json', 'output_path': 'out.json', 'style': 'pretty'}))
    assert handoff['status'] == 'ready', handoff
    task = dispatch(env, 'rpe_task', {'state_id': handoff['state_id']})
    assert task['status'] == 'succeeded', task
    assert json.loads((env['workspace'] / 'out.json').read_text()) == {'a': [2, 3], 'z': 1}
    assert gate(env)['harness_verified'] is True


def test_xlsx_without_trusted_helper_is_not_executable(configured):
    env = configured
    from openpyxl import Workbook
    book = Workbook()
    book.save(env['workspace'] / 'data.xlsx')
    result = dispatch(env, 'rpe_understand', extracted(capability='xlsx.inspect', constraints=['preserve_original'],
        execution={'input_path': 'data.xlsx', 'output_path': 'out.json', 'mode': 'rows'}))
    assert result['status'] in {'unsupported', 'rejected'}, result
    assert 'no_adapter' in json.dumps(result)
    assert not (env['workspace'] / 'out.json').exists()


def test_assistant_success_prose_without_native_chain_does_not_pass(configured):
    env = configured
    result = final_result(env)
    result['final_response'] = 'Successfully deduplicated and verified.'
    verdict = env['api'].evaluate_address_session(result, env['db'], env['workspace'])
    assert verdict['harness_verified'] is False
    assert verdict['errors']


def test_isolated_address_home_and_native_control_preserve_search(tmp_path):
    from rpe_harness.integrations.hermes_session import prepare_home
    settings = {'provider': 'xiaomi', 'model': 'test', 'base_url': 'https://example.invalid/v1', 'api_key': 'test-key'}
    for entry_mode, enabled in [('address', True), ('optional', False)]:
        home = prepare_home(tmp_path / entry_mode, tmp_path, tmp_path / 'runs.sqlite', tool_enabled=enabled,
                            settings=settings, entry_mode=entry_mode, preserve_native_tool_search=True)
        config = json.loads((home / 'config.yaml').read_text())
        assert config.get('tools', {}).get('tool_search', {}).get('enabled') != 'off'
        assert config['curator']['enabled'] is False


def test_task_stage_discards_original_task_and_understanding_history(configured):
    env = configured
    handoff = dispatch(env, 'rpe_understand', extracted())
    schemas = [{'type': 'function', 'function': row['schema']} for row in env['sdk'].tools.values()]
    request = env['sdk'].middleware['llm_request'](request={'tools': schemas, 'messages': [
        {'role': 'system', 'content': 'UNRELATED_PRIVATE_HISTORY'},
        {'role': 'user', 'content': 'orders.csv key_columns id'},
        {'role': 'assistant', 'content': 'previous understanding'}]}, session_id=env['session'],
        task_id='address-task', turn_id='address-turn', api_request_id='api-minimal')['request']
    encoded = json.dumps(request['messages'])
    assert 'UNRELATED_PRIVATE_HISTORY' not in encoded
    assert 'orders.csv' not in encoded
    assert 'key_columns' not in encoded
    assert handoff['state_id'] in encoded
    request['messages'].append({'role': 'user', 'content': 'injected after request middleware'})
    with pytest.raises(ValueError, match='actual address API schema'):
        env['sdk'].middleware['llm_execution'](request=request, next_call=lambda value: value,
            session_id=env['session'], task_id='address-task', turn_id='address-turn', api_request_id='api-minimal')


def test_address_phase_uses_its_trusted_schema_when_native_search_defers_plugin_tools(configured):
    env = configured
    bridge = {'type': 'function', 'function': {'name': 'tool_search', 'parameters': {'type': 'object'}}}
    request = env['sdk'].middleware['llm_request'](request={'tools': [bridge]},
        session_id=env['session'], task_id='address-task', turn_id='address-turn', api_request_id='api-search')['request']
    assert [tool['function']['name'] for tool in request['tools']] == ['rpe_understand']


def test_cross_session_state_id_cannot_replace_this_sessions_handoff(configured):
    env = configured
    a = dispatch(env, 'rpe_understand', extracted())
    env['session'] = 'second-session'
    b = dispatch(env, 'rpe_understand', extracted(execution={
        'input_path': 'orders.csv', 'output_path': 'second.csv', 'key_columns': ['id']}))
    assert a['state_id'] != b['state_id']
    result = dispatch(env, 'rpe_task', {'state_id': a['state_id']})
    assert result['status'] == 'rejected'
    assert not (env['workspace'] / 'out.csv').exists()
    assert not (env['workspace'] / 'second.csv').exists()


@pytest.mark.parametrize('configured', [{'execution': {'key_columns': ['value']}}], indirect=True)
def test_explicit_source_conflict_is_terminal_without_parameter_repair(configured):
    env = configured
    result = dispatch(env, 'rpe_understand', extracted())
    assert result['status'] == 'conflict'
    verdict = gate(env)
    assert verdict['rejection_verified'] is True, verdict
    assert verdict['rejection_reason'] == 'conflict'
    assert not (env['workspace'] / 'out.csv').exists()


def test_tampered_persisted_state_cannot_execute(configured):
    env = configured
    handoff = dispatch(env, 'rpe_understand', extracted())
    with sqlite3.connect(env['db']) as db:
        db.execute("UPDATE rpe_minimal_states SET document=replace(CAST(document AS TEXT),'Retain first','Retain last')")
    result = dispatch(env, 'rpe_task', {'state_id': handoff['state_id']})
    assert result['status'] == 'rejected'
    assert not (env['workspace'] / 'out.csv').exists()


@pytest.mark.parametrize('configured', ['trusted'], indirect=True)
def test_trusted_xlsx_helper_executes_and_gate_rechecks_binding(configured):
    env = configured
    from openpyxl import Workbook
    book = Workbook()
    book.active.title = 'Data'
    book.active.append(['id', 'value'])
    book.active.append([1, True])
    book.save(env['workspace'] / 'data.xlsx')
    book.close()
    handoff = dispatch(env, 'rpe_understand', extracted(capability='xlsx.inspect', constraints=['preserve_original'],
        execution={'input_path': 'data.xlsx', 'output_path': 'out.json', 'mode': 'rows', 'sheet': 'Data'}))
    assert handoff['status'] == 'ready', handoff
    result = dispatch(env, 'rpe_task', {'state_id': handoff['state_id']})
    assert result['status'] == 'succeeded', result
    assert json.loads((env['workspace'] / 'out.json').read_text()) == {
        'sheet': 'Data', 'rows': [['id', 'value'], [1, True]]}
    verdict = gate(env)
    assert verdict['harness_verified'] is True, verdict
    assert verdict['execution_kind'] == 'native_helper'


@pytest.mark.parametrize('configured', ['trusted'], indirect=True)
def test_trusted_skill_source_change_revokes_prepared_address(configured):
    env = configured
    from openpyxl import Workbook
    book = Workbook()
    book.save(env['workspace'] / 'data.xlsx')
    book.close()
    handoff = dispatch(env, 'rpe_understand', extracted(capability='xlsx.inspect', constraints=['preserve_original'],
        execution={'input_path': 'data.xlsx', 'output_path': 'out.json', 'mode': 'sheets'}))
    (env['source'] / 'productivity/xlsx/SKILL.md').write_text('changed after understanding')
    result = dispatch(env, 'rpe_task', {'state_id': handoff['state_id']})
    assert result['status'] == 'rejected'
    assert not (env['workspace'] / 'out.json').exists()


def test_real_hermes_sdk_binds_both_native_calls_and_minimal_messages_without_model(configured):
    env = configured
    sdk_root = Path('/Users/audrey/.hermes/hermes-agent')
    interpreter = sdk_root / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('installed Hermes SDK unavailable')
    home = env['config'].parent / 'sdk-home'
    plugin = home / 'plugins/rpe-harness'
    plugin.mkdir(parents=True)
    (home / 'config.yaml').write_text('plugins:\n  enabled: [rpe-harness]\n  disabled: []\n')
    (plugin / 'plugin.yaml').write_text('name: rpe-harness\nversion: 1.8.0\n')
    (plugin / '__init__.py').write_text('from rpe_harness.integrations.hermes import register\n')
    environment = {**os.environ, 'HOME': str(home), 'XDG_CACHE_HOME': str(home / 'cache'),
        'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(home / 'runtime'),
        'HERMES_SAFE_MODE': '0', 'HERMES_ENABLE_PROJECT_PLUGINS': '0',
        'RPE_HERMES_DB': str(env['db']), 'RPE_HERMES_WORKSPACE': str(env['workspace']),
        'RPE_HARNESS_PYTHON': sys.executable, 'RPE_HERMES_TOOL_ENABLED': '1',
        'RPE_HERMES_ENTRY_MODE': 'address', 'RPE_HERMES_ERROR_PATH': str(home / 'observer-errors'),
        'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src') + os.pathsep + str(sdk_root)}
    environment.pop('HERMES_IGNORE_USER_CONFIG', None)
    program = '''import json,sys
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.middleware import apply_llm_request_middleware,run_llm_execution_middleware,run_tool_execution_middleware
from agent.api_request_hooks import ApiRequestHooksMixin
from model_tools import handle_function_call
manager=get_plugin_manager();manager.discover_and_load()
assert manager._plugins['rpe-harness'].enabled,manager._plugins['rpe-harness'].error
hook=ApiRequestHooksMixin()
bridge={'type':'function','function':{'name':'tool_search','parameters':{'type':'object'}}}
def call(index,name,args):
    ids={'session_id':'sdk-address','task_id':'sdk-task','turn_id':'sdk-turn',
         'api_request_id':'sdk-api-'+str(index),'tool_call_id':'sdk-call-'+str(index)}
    body=apply_llm_request_middleware({'tools':[bridge],'messages':[{'role':'user','content':'PRIVATE_UPSTREAM'}]},**ids).payload
    if index>1: assert 'PRIVATE_UPSTREAM' not in json.dumps(body['messages'])
    manager.invoke_hook('pre_api_request',request=hook._api_request_payload_for_hook(body),**ids)
    run_llm_execution_middleware(body,lambda value:value,**ids)
    manager.invoke_hook('post_api_request',response={'assistant_message':{'tool_calls':[{'id':ids['tool_call_id'],'name':name}]}},**ids)
    return json.loads(run_tool_execution_middleware(name,args,
        lambda value:handle_function_call(name,value,skip_tool_execution_middleware=True,skip_tool_request_middleware=True,**ids),**ids))
handoff=call(1,'rpe_understand',json.loads(sys.argv[1]))
assert handoff['status']=='ready',handoff
result=call(2,'rpe_task',{'state_id':handoff['state_id']})
ids={'session_id':'sdk-address','task_id':'sdk-task','turn_id':'sdk-turn','api_request_id':'sdk-api-final'}
body=apply_llm_request_middleware({'tools':[bridge],'messages':[{'role':'user','content':'PRIVATE_UPSTREAM'}]},**ids).payload
assert 'PRIVATE_UPSTREAM' not in json.dumps(body['messages'])
manager.invoke_hook('pre_api_request',request=hook._api_request_payload_for_hook(body),**ids)
run_llm_execution_middleware(body,lambda value:value,**ids)
manager.invoke_hook('post_api_request',response={'assistant_message':{'tool_calls':[]}},**ids)
print(json.dumps(result))
'''
    child = subprocess.run([str(interpreter), '-B', '-c', program, json.dumps(extracted())],
        cwd=env['workspace'], env=environment, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr[-3000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result['status'] == 'succeeded', result
    assert (env['workspace'] / 'out.csv').read_text(encoding='utf-8-sig') == 'id,value\na,1\nb,3\n'
    events = env['store'].list('sdk-address')
    assert len([e for e in events if e['kind'] == 'pre_tool_call']) == 2
    raw = [e for e in events if e['kind'] == 'rpe_skill_api_checked']
    assert len(raw) == 3
    assert all(e['request_messages_bytes'] > 0 and len(e['request_messages_sha256']) == 64 for e in raw)
    verdict = env['api'].evaluate_address_session({'completed': True, 'exit_code': 0, 'observer_errors': 0,
        'session_id': 'sdk-address', 'session_root': str(home.parent), 'events': events,
        'process_cleanup': {'cleanup_complete': True}}, env['db'], env['workspace'])
    assert verdict['harness_verified'] is True, verdict
    assert verdict['context_minimized'] is True
