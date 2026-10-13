import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rpe_harness.storage import RunStore


def integration():
    return importlib.import_module('rpe_harness.integrations.hermes')


class PluginContext:
    def __init__(self):
        self.tools = {}
        self.hooks = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def configured_plugin(tmp_path, monkeypatch, enabled=True):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    db = tmp_path / 'state.sqlite'
    monkeypatch.setenv('RPE_HERMES_DB', str(db))
    monkeypatch.setenv('RPE_HERMES_WORKSPACE', str(workspace))
    monkeypatch.setenv('RPE_HARNESS_PYTHON', sys.executable)
    monkeypatch.setenv('RPE_HERMES_TOOL_ENABLED', '1' if enabled else '0')
    context = PluginContext()
    integration().register(context)
    return context, workspace, db


def request(workspace, event_id='order-task'):
    (workspace / 'orders.csv').write_text('id,value\na,10\nb,20\na,99\n')
    return {'schema_version': 1, 'event_id': event_id,
            'state': {'task_type': 'data.processing', 'goal': 'retain first order',
                      'input_format': 'csv', 'required_capability': 'tabular.deduplicate',
                      'constraints': ['preserve_original', 'preserve_order']},
            'execution': {'input_path': 'orders.csv', 'output_path': 'out.csv',
                          'key_columns': ['id']}}


def test_metadata_is_durable_and_does_not_persist_private_payload(tmp_path):
    store = integration().HostEventStore(tmp_path / 'runs.sqlite')
    store.record('post_api_request', {
        'session_id': 'native-session', 'task_id': 'native-task', 'turn_id': 'native-turn',
        'api_request_id': 'native-api', 'model': 'actual-model', 'api_duration': .5,
        'usage': {'input_tokens': 12, 'output_tokens': 3, 'total_tokens': 15,
                  'raw_usage': {'secret': 'DO_NOT_PERSIST'}},
        'user_message': 'DO_NOT_PERSIST', 'conversation_history': ['DO_NOT_PERSIST'],
        'args': {'api_key': 'DO_NOT_PERSIST'}, 'result': 'DO_NOT_PERSIST',
        'error_message': 'DO_NOT_PERSIST', 'error': {'type': 'provider_error',
                                                  'message': 'DO_NOT_PERSIST'},
    })
    recovered = integration().HostEventStore(tmp_path / 'runs.sqlite').list('native-session')
    assert len(recovered) == 1
    row = recovered[0]
    assert row['source_event_id'] is None
    assert row['ingest_event_id']
    assert row['api_request_id'] == 'native-api'
    assert row['usage'] == {'input_tokens': 12, 'output_tokens': 3, 'total_tokens': 15}
    assert 'DO_NOT_PERSIST' not in json.dumps(recovered)
    assert store.list('other-session') == []


def test_native_event_id_remains_distinct_from_ingestion_id(tmp_path):
    store = integration().HostEventStore(tmp_path / 'runs.sqlite')
    row = store.record('post_tool_call', {'session_id': 's', 'event_id': 'source-42',
                                        'tool_call_id': 'tool-42', 'usage': None})
    assert row['source_event_id'] == 'source-42'
    assert row['ingest_event_id'] != 'source-42'
    assert store.list()[0]['usage'] is None


def test_usage_provenance_preserves_reported_presence_without_private_fields(tmp_path):
    store = integration().HostEventStore(tmp_path / 'state.sqlite')
    store.record('post_api_request', {'session_id': 's', 'usage': {
        'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
        'raw_provider_usage': {'prompt_tokens': 0, 'completion_tokens': 0,
                               'prompt_tokens_details': {'cached_tokens': 0},
                               'secret': 'PRIVATE'},
        'present_fields': ['prompt_tokens', 'completion_tokens'],
        'response_identity': {'id': 'provider-response', 'model': 'actual-model',
                              'provider': 'actual-provider', 'api_key': 'PRIVATE'},
    }})
    usage = store.list()[0]['usage']
    assert usage['raw_provider_usage'] == {'prompt_tokens': 0, 'completion_tokens': 0,
                                          'prompt_tokens_details': {'cached_tokens': 0}}
    assert usage['present_fields'] == ['prompt_tokens', 'completion_tokens']
    assert usage['response_identity']['id'] == 'provider-response'
    assert 'PRIVATE' not in json.dumps(usage)


def test_provider_cache_provenance_remains_available_to_coverage_checks(tmp_path):
    store = integration().HostEventStore(tmp_path / 'state.sqlite')
    store.record('post_api_request', {'usage': {
        'raw_provider_usage': {'prompt_cache_hit_tokens': 12, 'prompt_cache_miss_tokens': 3,
                               'cache_creation': {'ephemeral_5m_input_tokens': 2,
                                                  'ephemeral_1h_input_tokens': 4}},
        'present_fields': ['prompt_cache_hit_tokens', 'prompt_cache_miss_tokens',
                           'cache_creation.ephemeral_5m_input_tokens',
                           'cache_creation.ephemeral_1h_input_tokens'],
    }})
    usage = store.list()[0]['usage']
    assert usage['raw_provider_usage']['prompt_cache_hit_tokens'] == 12
    assert usage['raw_provider_usage']['cache_creation']['ephemeral_1h_input_tokens'] == 4
    assert 'cache_creation.ephemeral_5m_input_tokens' in usage['present_fields']


def test_concurrent_events_and_runs_share_db_without_loss(tmp_path):
    path = tmp_path / 'state.sqlite'
    RunStore(path)
    store = integration().HostEventStore(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: store.record('post_tool_call', {'session_id': 's',
                      'tool_call_id': f'call-{i}'}), range(24)))
    assert len(store.list()) == 24
    assert len({row['ingest_event_id'] for row in store.list()}) == 24
    assert RunStore(path).list() == []


def test_observers_remain_active_when_native_arm_disables_tool(tmp_path, monkeypatch):
    context, _, db = configured_plugin(tmp_path, monkeypatch, enabled=False)
    assert context.tools == {}
    context.hooks['pre_llm_call'](session_id='s', turn_id='turn', user_message='PRIVATE')
    context.hooks['post_api_request'](session_id='s', api_request_id='api', usage=None)
    context.hooks['on_session_end'](session_id='s', completed=True, failed=False)
    rows = integration().HostEventStore(db).list('s')
    assert [row['kind'] for row in rows] == ['pre_llm_call', 'post_api_request', 'on_session_end']
    assert rows[1]['usage'] is None
    assert rows[2]['completed'] is True
    assert 'PRIVATE' not in json.dumps(rows)


def test_model_facing_schema_requires_goal_and_constraint_array(tmp_path, monkeypatch):
    context, _, _ = configured_plugin(tmp_path, monkeypatch)
    envelope = context.tools['rpe_task']['schema']['parameters']['properties']['request']
    assert envelope['properties']['schema_version']['const'] == 1
    assert envelope['properties']['event_id']['minLength'] == 1
    assert set(envelope['required']) == {'schema_version', 'event_id', 'state', 'execution'}
    state = envelope['properties']['state']
    assert {'task_type', 'goal', 'input_format', 'required_capability'} <= set(state['required'])
    assert state['properties']['goal'] == {'type': 'string', 'minLength': 1}
    assert state['properties']['task_type']['enum'] == ['data.processing', 'file.operation']
    assert state['properties']['required_capability']['const'] == 'tabular.deduplicate'
    constraints = state['properties']['constraints']
    assert constraints['type'] == 'array'
    assert constraints['items']['enum'] == ['preserve_original', 'preserve_order', 'preserve_formulas']
    execution = envelope['properties']['execution']
    assert set(execution['required']) == {'input_path', 'output_path', 'key_columns'}
    assert execution['properties']['key_columns']['type'] == 'array'
    assert execution['properties']['key_columns']['minItems'] == 1
    assert execution['properties']['key_columns']['items']['type'] == 'string'
    assert execution['properties']['keep']['enum'] == ['first']
    assert execution['properties']['sheet']['type'] == 'string'


def test_real_subprocess_runs_request_and_native_post_hook_links_run(tmp_path, monkeypatch):
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    original = (workspace / 'orders.csv').read_bytes()
    unrelated_cwd = tmp_path / 'unrelated'
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    result = json.loads(context.tools['rpe_task']['handler'](
        {'request': payload}, task_id='host-task', session_id='host-session'))
    assert result['status'] == 'succeeded', result
    assert result['verification']['passed'] is True
    assert (workspace / 'out.csv').read_text(encoding='utf-8-sig') == 'id,value\na,10\nb,20\n'
    assert (workspace / 'orders.csv').read_bytes() == original
    assert 'state' not in result and 'steps' not in result
    saved = RunStore(db).get(result['run_id'])
    assert saved['state']['session_id'] == 'host-session'
    assert saved['status'] == 'succeeded'
    context.hooks['post_tool_call'](
        tool_name='rpe_task', session_id='host-session', task_id='host-task',
        tool_call_id='actual-call', turn_id='actual-turn', api_request_id='actual-api',
        result=json.dumps(result), args={'request': payload}, duration_ms=10, status='ok')
    linked = integration().HostEventStore(db).list('host-session')[-1]
    assert linked['run_id'] == result['run_id']
    assert linked['tool_call_id'] == 'actual-call'
    assert linked['turn_id'] == 'actual-turn'
    assert linked['api_request_id'] == 'actual-api'
    assert 'result' not in linked and 'args' not in linked


@pytest.mark.parametrize('mutation', ['escape_input', 'absolute_escape', 'input_symlink',
                                     'output_symlink', 'output_parent_symlink'])
def test_workspace_escapes_and_symlinks_rejected_before_execution(tmp_path, monkeypatch, mutation):
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    outside = tmp_path / 'outside'
    outside.mkdir()
    source = outside / 'secret.csv'
    source.write_text('id,value\nx,PRIVATE\n')
    if mutation == 'escape_input':
        payload['execution']['input_path'] = '../outside/secret.csv'
    elif mutation == 'absolute_escape':
        payload['execution']['input_path'] = str(source)
    elif mutation == 'input_symlink':
        (workspace / 'link.csv').symlink_to(workspace / 'orders.csv')
        payload['execution']['input_path'] = 'link.csv'
    elif mutation == 'output_symlink':
        (workspace / 'out.csv').symlink_to(outside / 'missing.csv')
    else:
        (workspace / 'link').symlink_to(workspace, target_is_directory=True)
        payload['execution']['output_path'] = 'link/out.csv'
    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    assert result['status'] == 'rejected'
    assert result['error']['code'] == 'workspace_violation'
    assert RunStore(db).list() == []
    assert source.read_text() == 'id,value\nx,PRIVATE\n'
    assert not (outside / 'missing.csv').exists()
    assert integration().HostEventStore(db).list('s')[0]['kind'] == 'rpe_task_received'


def test_runtime_rejection_is_returned_and_durable(tmp_path, monkeypatch):
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    payload['execution']['key_columns'] = ['missing']
    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    assert result['status'] == 'rejected'
    assert result['error']['code'] == 'invalid_request'
    assert RunStore(db).get(result['run_id'])['status'] == 'rejected'
    assert not (workspace / 'out.csv').exists()


def test_receipt_persisted_before_child_and_timeout_is_recorded(tmp_path, monkeypatch):
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)

    def timeout_child(argv, **kwargs):
        rows = integration().HostEventStore(db).list('s')
        assert rows[0]['kind'] == 'rpe_task_received'
        assert json.loads(kwargs['input'])['execution']['input_path'] == str(workspace / 'orders.csv')
        assert argv[:4] == [sys.executable, '-B', '-m', 'rpe_harness']
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])

    monkeypatch.setattr(subprocess, 'run', timeout_child)
    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    assert result['status'] == 'failed'
    assert result['error']['code'] == 'harness_timeout'
    assert integration().HostEventStore(db).list('s')[-1]['error_code'] == 'harness_timeout'


def test_observer_storage_error_remains_visible(tmp_path, monkeypatch, caplog):
    context, _, db = configured_plugin(tmp_path, monkeypatch, enabled=False)
    db.unlink()
    db.mkdir()
    with pytest.raises(sqlite3.Error):
        context.hooks['post_llm_call'](session_id='s', turn_id='t')
    assert 'observer persistence failed' in caplog.text


@pytest.mark.skipif(not os.environ.get('RPE_HERMES_SOURCE'),
                    reason='set RPE_HERMES_SOURCE for native fail-open observation coverage')
def test_native_fail_open_hook_failure_leaves_private_free_durable_marker(tmp_path):
    hermes = Path(os.environ['RPE_HERMES_SOURCE']).resolve()
    project = Path(__file__).resolve().parents[1]
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    home = tmp_path / 'owned-home'
    shutil.copytree(project / 'integrations' / 'hermes' / 'rpe-harness',
                    home / 'plugins' / 'rpe-harness')
    (home / 'config.yaml').write_text('plugins:\n  enabled: [rpe-harness]\n  disabled: []\n')
    marker = tmp_path / 'observer-errors.jsonl'
    env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(tmp_path / 'runtime'),
           'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_SAFE_MODE': '0',
           'HERMES_ENABLE_PROJECT_PLUGINS': '0', 'RPE_HERMES_DB': str(tmp_path / 'state.sqlite'),
           'RPE_HERMES_WORKSPACE': str(workspace), 'RPE_HERMES_TOOL_ENABLED': '0',
           'RPE_HERMES_ERROR_PATH': str(marker),
           'PYTHONPATH': str(project / 'src') + os.pathsep + str(hermes)}
    env.pop('HERMES_IGNORE_USER_CONFIG', None)
    program = '''import json,logging,sqlite3
from hermes_cli.plugins import get_plugin_manager
manager = get_plugin_manager()
manager.discover_and_load()
assert manager._plugins['rpe-harness'].enabled
from rpe_harness.integrations.hermes import HostEventStore
original = HostEventStore.record
def fail_one(self, kind, payload):
    if kind == 'post_llm_call':
        raise sqlite3.OperationalError('PRIVATE_DATABASE_FAULT')
    return original(self, kind, payload)
HostEventStore.record = fail_one
logging.disable(logging.CRITICAL)
manager.invoke_hook('post_api_request', session_id='s', api_request_id='a', usage=None)
result = manager.invoke_hook('post_llm_call', session_id='s', turn_id='t')
print(json.dumps({'returned_normally': True, 'hook_result': result}))
'''
    child = subprocess.run([str(hermes / '.venv' / 'bin' / 'python'), '-B', '-c', program],
                           cwd=workspace, env=env, capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout.strip().splitlines()[-1])['returned_normally'] is True
    rows = [json.loads(line) for line in marker.read_text().splitlines()]
    assert rows[0]['kind'] == 'post_llm_call'
    assert isinstance(rows[0]['time'], float)
    assert set(rows[0]) == {'kind', 'time'}
    assert marker.stat().st_mode & 0o777 == 0o600
    assert 'PRIVATE_DATABASE_FAULT' not in marker.read_text()


def test_observer_failure_marker_has_store_owned_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv('RPE_HERMES_ERROR_PATH', raising=False)
    context, _, db = configured_plugin(tmp_path, monkeypatch, enabled=False)
    db.unlink()
    db.mkdir()
    with pytest.raises(sqlite3.Error):
        context.hooks['post_tool_call'](session_id='s', tool_call_id='t')
    marker = Path(str(db) + '.observer-errors')
    assert json.loads(marker.read_text())['kind'] == 'post_tool_call'


def test_malformed_subprocess_response_does_not_claim_success(tmp_path, monkeypatch):
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, 'unstructured private response', 'private stderr'))
    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    assert result['status'] == 'failed'
    assert result['error']['code'] == 'harness_protocol_error'
    assert 'private' not in json.dumps(integration().HostEventStore(db).list())


@pytest.mark.parametrize('response', [{'status': 'succeeded'},
                                     {'status': 'succeeded', 'run_id': 'r',
                                      'verification': {'passed': False}}])
def test_unverified_child_success_is_a_protocol_failure(tmp_path, monkeypatch, response):
    context, workspace, _ = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, json.dumps(response), ''))
    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    assert result['status'] == 'failed'
    assert result['error']['code'] == 'harness_protocol_error'


def test_changed_request_event_id_conflict_is_structured_and_observed(tmp_path, monkeypatch):
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    handler = context.tools['rpe_task']['handler']
    first = json.loads(handler({'request': payload}, session_id='s'))
    assert first['status'] == 'succeeded'
    payload['execution']['output_path'] = 'changed.csv'
    conflict = json.loads(handler({'request': payload}, session_id='s'))
    assert conflict['status'] == 'rejected'
    assert conflict['error']['code'] == 'event_conflict'
    assert not (workspace / 'changed.csv').exists()
    assert integration().HostEventStore(db).list('s')[-1]['error_code'] == 'event_conflict'


@pytest.mark.skipif(not os.environ.get('RPE_HERMES_SOURCE'),
                    reason='set RPE_HERMES_SOURCE for installed native host import coverage')
def test_native_hermes_loader_dispatches_shipped_plugin_and_persists_real_ids(tmp_path):
    hermes = Path(os.environ['RPE_HERMES_SOURCE']).resolve()
    project = Path(__file__).resolve().parents[1]
    home = tmp_path / 'owned-home'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    payload = request(workspace)
    plugin = home / 'plugins' / 'rpe-harness'
    shutil.copytree(project / 'integrations' / 'hermes' / 'rpe-harness', plugin)
    (home / 'config.yaml').write_text('plugins:\n  enabled: [rpe-harness]\n  disabled: []\n')
    db = tmp_path / 'state.sqlite'
    env = {**os.environ, 'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(tmp_path / 'runtime'),
           'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_SAFE_MODE': '0',
           'HERMES_ENABLE_PROJECT_PLUGINS': '0', 'RPE_HERMES_DB': str(db),
           'RPE_HERMES_WORKSPACE': str(workspace), 'RPE_HARNESS_PYTHON': sys.executable,
           'RPE_HERMES_TOOL_ENABLED': '1',
           'PYTHONPATH': str(project / 'src') + os.pathsep + str(hermes)}
    env.pop('HERMES_IGNORE_USER_CONFIG', None)
    program = '''import json,sys
from hermes_cli.plugins import get_plugin_manager
manager = get_plugin_manager()
manager.discover_and_load()
loaded = manager._plugins['rpe-harness']
assert loaded.enabled, loaded.error
from model_tools import handle_function_call
result = handle_function_call('rpe_task', {'request': json.loads(sys.argv[1])},
    task_id='native-task', session_id='native-session', tool_call_id='native-call',
    turn_id='native-turn', api_request_id='native-api')
print(result)
'''
    child = subprocess.run([str(hermes / '.venv' / 'bin' / 'python'), '-B', '-c', program,
                            json.dumps(payload)], cwd=workspace, env=env,
                           capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result['status'] == 'succeeded', result
    rows = integration().HostEventStore(db).list('native-session')
    assert [row['kind'] for row in rows] == ['pre_tool_call', 'rpe_task_received',
                                           'rpe_task_returned', 'post_tool_call']
    linked = rows[-1]
    assert linked['run_id'] == result['run_id']
    assert linked['tool_call_id'] == 'native-call'
    assert linked['api_request_id'] == 'native-api'
    assert linked['telemetry_schema_version'] == 'hermes.observer.v1'
    assert RunStore(db).get(result['run_id'])['verification']['passed'] is True
