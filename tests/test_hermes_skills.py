"""Real SQLite/HTTP/files with SDK and model calls supplied at their slow boundary."""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import subprocess

import pytest

from rpe_harness.integrations import hermes
from rpe_harness.integrations.hermes_gate import digest_json
from rpe_harness.skill_catalog import SkillCatalog


class SDK:
    def __init__(self):
        self.tools = {}
        self.middleware = {}

    def register_tool(self, **value):
        self.tools[value['name']] = value

    def register_middleware(self, name, function):
        self.middleware[name] = function


@pytest.fixture
def workflow_api():
    assert importlib.util.find_spec('rpe_harness.integrations.hermes_skills'), 'skills workflow missing'
    return importlib.import_module('rpe_harness.integrations.hermes_skills')


@pytest.fixture
def configured(tmp_path, monkeypatch, workflow_api):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'orders.csv').write_text('id,value\na,1\na,2\nb,3\n')
    source = tmp_path / 'sources'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx', source / 'productivity/xlsx')
    db = tmp_path / 'db.sqlite'
    catalog_root = tmp_path / 'catalog'
    catalog = SkillCatalog(db, catalog_root)
    snapshot = catalog.sync(source)['snapshot_id']
    catalog.trust_adapter('productivity/xlsx', snapshot_id=snapshot)
    material = catalog.materialize(tmp_path / 'home/skills', snapshot_id=snapshot)
    config = tmp_path / 'skills-config.json'
    config.write_text(json.dumps({'catalog_root': str(catalog_root), 'snapshot_id': snapshot,
                                  'materialized_root': material['materialized_path'],
                                  'entries': material['entries']}))
    monkeypatch.setenv('RPE_HERMES_SKILL_CONFIG', str(config))
    monkeypatch.setenv('RPE_HERMES_ENTRY_KEY', 'skills-entry-test')
    monkeypatch.delenv('RPE_HERMES_REQUIRED_REQUEST_PATH', raising=False)
    monkeypatch.delenv('RPE_HERMES_INPUT_SHA256', raising=False)
    monkeypatch.setattr(hermes, '_TEXT_FIELDS', hermes._TEXT_FIELDS | {
        'snapshot_id', 'skill_id', 'skill_sha256', 'script_sha256', 'load_receipt_id',
        'recovery_id', 'context_status', 'workflow_phase', 'arguments_sha256'})
    store = hermes.HostEventStore(db)
    sources = [{'source': 'prior-user', 'event_id': 'prior-context',
                'payload': {'text': 'original statement'}, 'facts': {'key_columns': ['id']}}]
    extension = workflow_api.skill_extension(db, workspace, catalog_root=catalog_root,
                                             snapshot_id=snapshot, context_sources=sources)
    sdk = SDK()
    workflow_api.register_skill_workflow(sdk, store=store, workspace=workspace, python=sys.executable,
                                         error_path=tmp_path / 'observer-errors')

    from rpe_harness.integrations.hermes_broker import execution_broker
    with execution_broker(db_path=db, workspace=workspace, python=sys.executable, extension=extension) as broker:
        for name, value in broker.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
        yield {'sdk': sdk, 'workspace': workspace, 'db': db, 'catalog': catalog,
               'catalog_root': catalog_root, 'store': store, 'extension': extension, 'config': config,
               'source': source, 'material': material, 'counter': 0, 'api': workflow_api}
    assert broker.cleanup['cleanup_complete'] is True


def schemas(env):
    values = [{'type': 'function', 'function': value['schema']} for value in env['sdk'].tools.values()]
    values.append({'type': 'function', 'function': {'name': 'skill_view', 'parameters': {
        'type': 'object', 'properties': {'name': {'type': 'string'}}}}})
    return values


def identity(env):
    env['counter'] += 1
    return {'session_id': 'skills-session', 'task_id': 'skills-task', 'turn_id': 'skills-turn',
            'api_request_id': f'api-{env["counter"]}', 'tool_call_id': f'call-{env["counter"]}'}


def dispatch(env, name, args, *, native_result=None, actual_choice=None):
    ids = identity(env)
    body = {'tools': schemas(env)}
    rewritten = env['sdk'].middleware['llm_request'](request=body, **ids)['request']
    if actual_choice is not None:
        rewritten['tool_choice'] = actual_choice
    env['store'].record('pre_api_request', {**ids, 'request': {'body': rewritten}})
    execution = env['sdk'].middleware.get('llm_execution')
    if execution:
        try:
            execution(request=rewritten, next_call=lambda value: value, **ids)
        except ValueError:
            if actual_choice is None:
                raise
            # Official SDK execution middleware is fail-open; downstream entry
            # must still refuse a request with no immutable raw API proof.
    env['store'].record('post_api_request', {**ids, 'response': {'assistant_message': {
        'tool_calls': [{'id': ids['tool_call_id'], 'name': name}]}}})
    env['store'].record('pre_tool_call', {**ids, 'tool_name': name})

    def next_call(value):
        if name == 'skill_view':
            if native_result is not None:
                return json.dumps(native_result)
            row = env['material']['entries'][0]
            path = Path(row['skill_path'])
            return json.dumps({'success': True, 'setup_needed': False, 'name': row['name'],
                               '_source_path': str(path), 'content': path.read_text()})
        return env['sdk'].tools[name]['handler'](value, task_id=ids['task_id'], session_id=ids['session_id'])

    raw = env['sdk'].middleware['tool_execution'](tool_name=name, args=args, next_call=next_call, **ids)
    result = json.loads(raw)
    linked = {key: result[key] for key in ('run_id', 'receipt_id', 'contract_sha256', 'input_sha256') if key in result}
    env['store'].record('post_tool_call', {**ids, 'tool_name': name, 'status': 'ok', **linked})
    return result


def discover_and_load(env):
    found = dispatch(env, 'rpe_discover', {'q': 'xlsx', 'capability': 'tabular.deduplicate'})
    assert found['status'] == 'discovered'
    loaded = dispatch(env, 'skill_view', {'name': found['selected']['load_name']})
    assert loaded['success'] is True
    return found


def final_result(env):
    ids = identity(env)
    rewritten = env['sdk'].middleware['llm_request'](request={'tools': schemas(env)}, **ids)['request']
    env['store'].record('pre_api_request', {**ids, 'request': {'body': rewritten}})
    execution = env['sdk'].middleware.get('llm_execution')
    if execution:
        execution(request=rewritten, next_call=lambda value: value, **ids)
    env['store'].record('post_api_request', {**ids, 'response': {'assistant_message': {'tool_calls': []}}})
    return {'completed': True, 'exit_code': 0, 'timed_out': False, 'observer_errors': 0,
            'session_root': str(env['config'].parent),
            'process_cleanup': {'cleanup_complete': True}, 'session_id': 'skills-session',
            'events': env['store'].list('skills-session')}


def gate(env, result):
    module = importlib.import_module('rpe_harness.integrations.skill_gate')
    return module.evaluate_skill_session(result, env['db'], env['workspace'], catalog_root=env['catalog_root'])


def test_policy_exposes_only_current_native_tool_schema(configured):
    env = configured
    found = dispatch(env, 'rpe_discover', {'q': 'xlsx', 'capability': 'tabular.deduplicate'})
    assert found['status'] == 'discovered'
    events = env['store'].list()
    pre = next(e for e in events if e['kind'] == 'pre_api_request')
    assert pre['request_tool_names'] == ['rpe_discover']
    assert pre['request_tool_choice'] == 'rpe_discover'
    assert pre['request_parallel_tool_calls'] is False


def test_full_discovery_native_load_context_and_real_contract_outcome(configured):
    env = configured
    discover_and_load(env)
    restored = dispatch(env, 'rpe_context', {'facts': {'input_path': 'orders.csv', 'output_path': 'out.csv'}})
    assert restored['status'] == 'ready'
    assert restored['facts']['key_columns'] == ['id']
    result = dispatch(env, 'rpe_task', {'goal': 'retain first orders'})
    assert result['status'] == 'succeeded'
    assert (env['workspace'] / 'out.csv').read_text(encoding='utf-8-sig') == 'id,value\na,1\nb,3\n'
    verdict = gate(env, final_result(env))
    assert verdict['harness_verified'] is True, verdict
    assert verdict['execution_kind'] == 'rpe_contract'


@pytest.mark.parametrize('fault', ['wrong_path', 'wrong_content', 'setup_needed', 'failed'])
def test_native_load_return_cannot_fake_source_identity(configured, fault):
    env = configured
    found = dispatch(env, 'rpe_discover', {'q': 'xlsx', 'capability': 'tabular.deduplicate'})
    row = env['material']['entries'][0]
    value = {'success': True, 'setup_needed': False, 'name': row['name'],
             '_source_path': row['skill_path'], 'content': Path(row['skill_path']).read_text()}
    if fault == 'wrong_path':
        value['_source_path'] = str(env['source'] / 'productivity/xlsx/SKILL.md')
    elif fault == 'wrong_content':
        value['content'] = 'fake successful skill'
    elif fault == 'setup_needed':
        value['setup_needed'] = True
    else:
        value['success'] = False
    loaded = dispatch(env, 'skill_view', {'name': found['selected']['load_name']}, native_result=value)
    assert loaded['status'] == 'rejected'
    assert not any(e['kind'] == 'rpe_skill_loaded' for e in env['store'].list())


def test_actual_none_policy_cannot_authorize_discovery(configured):
    result = dispatch(configured, 'rpe_discover', {'q': 'xlsx'}, actual_choice='none')
    assert result['status'] == 'rejected'
    assert not any(e['kind'] == 'rpe_skill_discovered' for e in configured['store'].list())


def test_missing_context_is_explicit_terminal_rejection(configured):
    env = configured
    discover_and_load(env)
    result = dispatch(env, 'rpe_context', {'facts': {}, 'source_refs': []})
    assert result['status'] == 'needs_context'
    verdict = gate(env, final_result(env))
    assert verdict['status'] == 'accepted_rejection'
    assert verdict['harness_verified'] is False
    assert not (env['workspace'] / 'out.csv').exists()


def test_source_changed_after_context_cannot_reach_executor(configured):
    env = configured
    discover_and_load(env)
    restored = dispatch(env, 'rpe_context', {'facts': {'input_path': 'orders.csv', 'output_path': 'out.csv'}})
    with sqlite3.connect(env['db']) as db:
        db.execute('UPDATE context_sources SET facts_json=?', ('{"key_columns":["email"]}',))
    result = dispatch(env, 'rpe_task', {'goal': 'deduplicate'})
    assert result['status'] == 'rejected'
    assert not (env['workspace'] / 'out.csv').exists()


def test_business_arguments_cannot_replace_recovered_key_columns(configured):
    env = configured
    discover_and_load(env)
    dispatch(env, 'rpe_context', {'facts': {'input_path': 'orders.csv', 'output_path': 'out.csv'}})
    result = dispatch(env, 'rpe_task', {'goal': 'deduplicate', 'key_columns': ['email']})
    assert result['status'] == 'rejected'
    assert not (env['workspace'] / 'out.csv').exists()


def test_post_terminal_policy_cannot_reopen_workflow(configured):
    env = configured
    discover_and_load(env)
    dispatch(env, 'rpe_context', {'facts': {}, 'source_refs': []})
    result = dispatch(env, 'rpe_discover', {'q': 'xlsx'})
    assert result['status'] == 'rejected'


def test_gate_rejects_prose_without_actual_native_chain(configured):
    verdict = gate(configured, final_result(configured))
    assert verdict['harness_verified'] is False
    assert verdict['errors']


def test_native_view_cannot_read_linked_file_or_unknown_name(configured):
    env = configured
    found = dispatch(env, 'rpe_discover', {'q': 'xlsx', 'capability': 'tabular.deduplicate'})
    result = dispatch(env, 'skill_view', {'name': found['selected']['load_name'], 'file_path': '../../secret'})
    assert result['status'] == 'rejected'


def test_invalid_parameters_are_bounded_at_three_attempts(configured):
    env = configured
    for _ in range(3):
        assert dispatch(env, 'rpe_discover', {'q': 'xlsx', 'shell': 'forbidden'})['status'] == 'rejected'
    ids = identity(env)
    body = env['sdk'].middleware['llm_request'](request={'tools': schemas(env)}, **ids)['request']
    assert body['tool_choice'] == 'none'


def test_wrong_actual_model_call_id_cannot_supply_discovery_receipt(configured):
    env = configured
    ids = identity(env)
    body = env['sdk'].middleware['llm_request'](request={'tools': schemas(env)}, **ids)['request']
    env['store'].record('pre_api_request', {**ids, 'request': {'body': body}})
    env['sdk'].middleware['llm_execution'](request=body, next_call=lambda value: value, **ids)
    env['store'].record('post_api_request', {**ids, 'response': {'assistant_message': {
        'tool_calls': [{'id': 'different-real-provider-call', 'name': 'rpe_discover'}]}}})
    env['store'].record('pre_tool_call', {**ids, 'tool_name': 'rpe_discover'})
    raw = env['sdk'].middleware['tool_execution'](tool_name='rpe_discover', args={'q': 'xlsx'},
        next_call=lambda args: env['sdk'].tools['rpe_discover']['handler'](args,
                    task_id=ids['task_id'], session_id=ids['session_id']), **ids)
    assert json.loads(raw)['status'] == 'rejected'
    assert not any(e['kind'] == 'rpe_skill_discovered' for e in env['store'].list())


def complete(env):
    discover_and_load(env)
    dispatch(env, 'rpe_context', {'facts': {'input_path': 'orders.csv', 'output_path': 'out.csv'}})
    assert dispatch(env, 'rpe_task', {'goal': 'deduplicate'})['status'] == 'succeeded'


@pytest.mark.parametrize('fault', ['materialized', 'original_source', 'context_source', 'output', 'rpc_duplicate'])
def test_final_gate_reopens_all_current_evidence(configured, fault):
    env = configured
    complete(env)
    if fault == 'materialized':
        path = Path(env['material']['entries'][0]['skill_path'])
        path.chmod(0o600)
        path.write_text('changed materialized source')
    elif fault == 'original_source':
        (env['source'] / 'productivity/xlsx/SKILL.md').write_text('changed original source')
    elif fault == 'context_source':
        with sqlite3.connect(env['db']) as db:
            db.execute('DELETE FROM context_sources')
    elif fault == 'output':
        (env['workspace'] / 'out.csv').write_text('id,value\na,999\n')
    else:
        rpc = next(e for e in env['store'].list() if e['kind'] == 'rpe_skill_rpc_received')
        env['store'].record('rpe_skill_rpc_received', rpc)
    verdict = gate(env, final_result(env))
    assert verdict['harness_verified'] is False, verdict
    assert verdict['errors']


def test_parameter_retry_keeps_parent_rpc_audit_evidence(configured):
    env = configured
    assert dispatch(env, 'rpe_discover', {'q': 'xlsx', 'unknown': 1})['status'] == 'rejected'
    complete(env)
    verdict = gate(env, final_result(env))
    assert verdict['harness_verified'] is True, verdict


def test_restarting_plugin_preserves_closed_workflow(configured):
    env = configured
    discover_and_load(env)
    dispatch(env, 'rpe_context', {'facts': {}, 'source_refs': []})
    replacement = SDK()
    env['api'].register_skill_workflow(replacement, store=env['store'], workspace=env['workspace'],
        python=sys.executable, error_path=env['config'].parent / 'observer-errors')
    body = replacement.middleware['llm_request'](request={'tools': schemas(env)}, **identity(env))['request']
    assert body['tool_choice'] == 'none'


@pytest.mark.skipif(sys.platform != 'darwin', reason='native helper uses macOS sandbox')
def test_native_xlsx_helper_through_real_parent_broker_and_independent_gate(configured):
    from openpyxl import Workbook
    env = configured
    book = Workbook()
    book.active.title = 'Data'
    book.active.append(['id', 'value'])
    book.active.append([1, True])
    book.save(env['workspace'] / 'book.xlsx')
    book.close()
    found = dispatch(env, 'rpe_discover', {'q': 'xlsx', 'capability': 'xlsx.inspect'})
    dispatch(env, 'skill_view', {'name': found['selected']['load_name']})
    context = dispatch(env, 'rpe_context', {'facts': {'input_path': 'book.xlsx',
                       'output_path': 'inspect.json', 'mode': 'rows'}})
    assert context['status'] == 'ready'
    result = dispatch(env, 'rpe_task', {'goal': 'inspect workbook', 'sheet': 'Data'})
    assert result['status'] == 'succeeded', result
    assert json.loads((env['workspace'] / 'inspect.json').read_text()) == {
        'sheet': 'Data', 'rows': [['id', 'value'], [1, True]]}
    verdict = gate(env, final_result(env))
    assert verdict['harness_verified'] is True, verdict
    assert verdict['execution_kind'] == 'native_helper'


@pytest.mark.parametrize('before_load', [True, False])
def test_materialized_parent_symlink_cannot_alias_another_skill_tree(configured, before_load):
    env = configured
    if before_load:
        found = dispatch(env, 'rpe_discover', {'q': 'xlsx', 'capability': 'tabular.deduplicate'})
    else:
        complete(env)
    materialized = Path(env['material']['materialized_path'])
    parent = materialized / 'productivity'
    moved = env['config'].parent / 'aliased-tree'
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    if before_load:
        result = dispatch(env, 'skill_view', {'name': found['selected']['load_name']})
        assert result['status'] == 'rejected'
    else:
        verdict = gate(env, final_result(env))
        assert verdict['harness_verified'] is False


def test_gate_requires_fresh_recovery_to_descend_from_actual_context_call(configured):
    from rpe_harness.context import ContextStore, requirements_for
    env = configured
    complete(env)
    workflow = env['api'].WorkflowStore(env['db'])
    native = {'entry_key': 'skills-entry-test', 'session_id': 'skills-session',
              'turn_id': 'skills-turn', 'task_id': 'skills-task'}
    state = workflow.get(native)
    unrelated = ContextStore(env['db']).recover(requirement=requirements_for('tabular.deduplicate'),
        session_id='skills-session', source_refs=[], facts={'input_path': str(env['workspace'] / 'orders.csv'),
        'output_path': str(env['workspace'] / 'out.csv'), 'key_columns': ['id']})
    state['recovery_id'] = unrelated['recovery_id']
    workflow.put(state)
    assert gate(env, final_result(env))['harness_verified'] is False


def test_gate_requires_context_rpc_receipt_to_match_actual_context_native_call(configured):
    env = configured
    complete(env)
    discovery = next(e for e in env['store'].list() if e['kind'] == 'rpe_skill_discovered')
    with sqlite3.connect(env['db']) as db:
        row = db.execute("SELECT id,metadata_json FROM hermes_host_events WHERE kind='rpe_context_recovered' "
                         "AND json_extract(metadata_json,'$.tool_name')='rpe_context'").fetchone()
        metadata = json.loads(row[1])
        metadata['receipt_id'] = discovery['receipt_id']
        db.execute('UPDATE hermes_host_events SET metadata_json=? WHERE id=?', (json.dumps(metadata), row[0]))
    assert gate(env, final_result(env))['harness_verified'] is False


def test_real_sdk_hook_projection_and_pre_tool_ordering_without_model(configured):
    env = configured
    sdk_root = Path('/Users/audrey/.hermes/hermes-agent')
    interpreter = sdk_root / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('installed Hermes SDK is unavailable')
    home = env['config'].parent / 'sdk-home'
    plugin = home / 'plugins/rpe-harness'
    plugin.mkdir(parents=True)
    (home / 'config.yaml').write_text('plugins:\n  enabled: [rpe-harness]\n  disabled: []\n')
    (plugin / 'plugin.yaml').write_text('name: rpe-harness\nversion: 1.6.0\n')
    (plugin / '__init__.py').write_text('from rpe_harness.integrations.hermes import register\n')
    environment = {**os.environ, 'HOME': str(home), 'XDG_CACHE_HOME': str(home / 'cache'),
        'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(home / 'runtime'),
        'HERMES_SAFE_MODE': '0', 'HERMES_ENABLE_PROJECT_PLUGINS': '0',
        'RPE_HERMES_DB': str(env['db']), 'RPE_HERMES_WORKSPACE': str(env['workspace']),
        'RPE_HARNESS_PYTHON': sys.executable, 'RPE_HERMES_TOOL_ENABLED': '1',
        'RPE_HERMES_ENTRY_MODE': 'skills', 'RPE_HERMES_ERROR_PATH': str(home / 'observer-errors'),
        'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src') + os.pathsep + str(sdk_root)}
    environment.pop('HERMES_IGNORE_USER_CONFIG', None)
    program = '''import json,sys
from types import SimpleNamespace
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.middleware import apply_llm_request_middleware,run_llm_execution_middleware,run_tool_execution_middleware
from agent.api_request_hooks import ApiRequestHooksMixin
from model_tools import handle_function_call
manager=get_plugin_manager();manager.discover_and_load()
assert manager._plugins['rpe-harness'].enabled, manager._plugins['rpe-harness'].error
ids={'session_id':'sdk-no-model','task_id':'sdk-task','turn_id':'sdk-turn','api_request_id':'sdk-api','tool_call_id':'sdk-call'}
body=apply_llm_request_middleware({'tools':json.loads(sys.argv[1])},**ids).payload
hook=ApiRequestHooksMixin()
manager.invoke_hook('pre_api_request',request=hook._api_request_payload_for_hook(body),**ids)
run_llm_execution_middleware(body,lambda value:value,**ids)
manager.invoke_hook('post_api_request',response={'assistant_message':{'tool_calls':[{'id':'sdk-call','name':'rpe_discover'}]}},**ids)
result=run_tool_execution_middleware('rpe_discover',{'q':'xlsx','capability':'tabular.deduplicate'},
    lambda args:handle_function_call('rpe_discover',args,skip_tool_execution_middleware=True,skip_tool_request_middleware=True,**ids),**ids)
print(json.dumps({'result':json.loads(result)}))
'''
    child = subprocess.run([str(interpreter), '-B', '-c', program, json.dumps(schemas(env))],
                           cwd=env['workspace'], env=environment, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr[-3000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])['result']
    assert result['status'] == 'discovered', result
    rows = env['store'].list('sdk-no-model')
    assert any(e['kind'] == 'pre_tool_call' and e['tool_call_id'] == 'sdk-call' for e in rows)
    assert any(e['kind'] == 'rpe_skill_rpc_received' for e in rows)


def test_changed_raw_api_schema_cannot_get_immutable_execution_proof(configured):
    env = configured
    ids = identity(env)
    body = env['sdk'].middleware['llm_request'](request={'tools': schemas(env)}, **ids)['request']
    body['tools'][0]['function']['parameters']['properties']['capability']['enum'].append('shell.exec')
    with pytest.raises(ValueError, match='schema'):
        env['sdk'].middleware['llm_execution'](request=body,
            next_call=lambda _: pytest.fail('changed schema reached provider'), **ids)
    assert not any(e['kind'] == 'rpe_skill_api_checked' for e in env['store'].list())


def test_gate_requires_immutable_raw_api_proof_even_when_observed_policy_matches(configured):
    env = configured
    complete(env)
    with sqlite3.connect(env['db']) as db:
        db.execute("DELETE FROM hermes_host_events WHERE kind='rpe_skill_api_checked'")
    verdict = gate(env, final_result(env))
    assert verdict['harness_verified'] is False
    assert 'actual_raw_schema_or_observer_projection_invalid' in verdict['errors']


def test_context_api_exposes_exact_capability_facts_and_typed_refs(configured):
    env = configured
    found = dispatch(env, 'rpe_discover', {'q': 'xlsx', 'capability': 'xlsx.inspect'})
    dispatch(env, 'skill_view', {'name': found['selected']['load_name']})
    body = env['sdk'].middleware['llm_request'](request={'tools': schemas(env)}, **identity(env))['request']
    parameters = body['tools'][0]['function']['parameters']['properties']
    facts = parameters['facts']['properties']
    assert set(facts) == {'input_path', 'output_path', 'mode'}
    assert facts['mode']['enum'] == ['rows', 'sheets', 'formulas']
    assert set(parameters['source_refs']['items']['properties']) == {'source', 'event_id', 'sha256'}


def test_malformed_context_parameter_can_be_corrected_within_three_attempts(configured):
    env = configured
    discover_and_load(env)
    rejected = dispatch(env, 'rpe_context', {'facts': {'workbook': 'orders.csv'}, 'source_refs': ['skill-hash']})
    assert rejected['status'] == 'rejected'
    restored = dispatch(env, 'rpe_context', {'facts': {'input_path': 'orders.csv', 'output_path': 'out.csv'}})
    assert restored['status'] == 'ready', restored
    assert dispatch(env, 'rpe_task', {'goal': 'deduplicate'})['status'] == 'succeeded'
    assert gate(env, final_result(env))['harness_verified'] is True


def test_terminal_keeps_one_known_schema_with_none_and_explicit_final_instruction(configured):
    env = configured
    complete(env)
    body = env['sdk'].middleware['llm_request'](request={'tools': schemas(env)}, **identity(env))['request']
    assert body['tool_choice'] == 'none'
    assert [item['function']['name'] for item in body['tools']] == ['rpe_task']
    assert 'final reply' in body['messages'][-1]['content']


def test_450_declared_synthetic_config_entries_register_below_eight_mib(tmp_path, monkeypatch, workflow_api):
    materialized = tmp_path / 'home/skills'
    materialized.mkdir(parents=True)
    entries = [{'skill_id': f'synthetic/{index}', 'load_name': f'synthetic-{index}',
                'skill_path': str(materialized / f'synthetic/{index}/SKILL.md'),
                'skill_sha256': '0' * 64, 'script_path': None, 'script_sha256': None,
                'description': 'declared synthetic registration budget fixture ' + 'x' * 3000}
               for index in range(450)]
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'materialized_root': str(materialized), 'entries': entries,
                                 'catalog_root': str(tmp_path / 'catalog'), 'snapshot_id': '0' * 64}))
    assert 1_048_576 < config.stat().st_size < 8 * 1_048_576
    monkeypatch.setenv('RPE_HERMES_SKILL_CONFIG', str(config))
    monkeypatch.setenv('RPE_HERMES_ENTRY_KEY', 'synthetic-registration')
    monkeypatch.delenv('RPE_HERMES_REQUIRED_REQUEST_PATH', raising=False)
    sdk = SDK()
    store = hermes.HostEventStore(tmp_path / 'state.sqlite')
    workflow_api.register_skill_workflow(sdk, store=store, workspace=tmp_path, python=sys.executable,
                                         error_path=tmp_path / 'observer-errors')
    assert set(sdk.tools) == {'rpe_discover', 'rpe_context', 'rpe_task'}


@pytest.mark.skipif(sys.platform != 'darwin', reason='requires real macOS sandbox')
def test_real_sandbox_descriptor_hash_keeps_private_files_unreadable(configured):
    from openpyxl import Workbook
    from rpe_harness.integrations.hermes_session import sandbox_profile
    env = configured
    root = env['config'].parent
    session = root / 'sandbox-session'
    session.mkdir()
    private = root / 'private.txt'
    private.write_text('PRIVATE-MARKER')
    outside_db = root / 'outside.sqlite'
    outside_db.write_bytes(b'PRIVATE-DATABASE')
    book = Workbook()
    book.active.append(['id', 'value'])
    book.active.append([1, True])
    source = env['workspace'] / 'book.xlsx'
    book.save(source)
    book.close()
    source_root = Path(__file__).resolve().parents[1] / 'src'
    profile = session / 'scope.sb'
    profile.write_text(sandbox_profile([session, env['workspace']], Path('/Users/audrey/.hermes'),
        database=env['db'], owner_read_scope=(root, [session, env['workspace'], source_root, env['db']])))
    program = '''import hashlib,json,sys
from pathlib import Path
from rpe_harness.path_binding import read_source
source=Path(sys.argv[1]);expected=sys.argv[2]
assert hashlib.sha256(read_source(source)).hexdigest()==expected
denied=[]
for name in sys.argv[3:]:
    try: Path(name).read_bytes()
    except PermissionError: denied.append(name)
    else: raise AssertionError('private regular file became readable')
print(json.dumps({'verified':True,'denied_count':len(denied)}))
'''
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    child = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), sys.executable, '-B', '-c',
        program, str(source), digest, str(private), str(outside_db)], cwd=session,
        env={**os.environ, 'PYTHONPATH': str(source_root)}, capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == {'verified': True, 'denied_count': 2}
