"""Registered contracts share the real host/MCP approval and worker pipeline."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import pickle
import subprocess
import sys
from textwrap import dedent

import pytest

from rpe_harness.contract_sdk import ContractPlugin, DEFAULT_REGISTRY
from rpe_harness.host_api import HostAPI, TrustedHostAction
from rpe_harness.mcp_server import StdioMCPServer


def _custom_validate(options, raw):
    if set(options) - {'mode', 'suffix'} or options.get('mode') != 'uppercase':
        raise ValueError('custom contract requires mode uppercase and an optional suffix')
    suffix = options.get('suffix', '!')
    if not isinstance(suffix, str):
        raise ValueError('suffix must be a string')
    raw.decode('utf-8')
    return {'mode': 'uppercase', 'suffix': suffix}


def _custom_execute(plan, raw):
    return (raw.decode('utf-8').upper() + plan.options['suffix']).encode('utf-8'), {}


def _custom_verify(plan, raw, staged):
    expected = (raw.decode('utf-8').upper() + plan.options['suffix']).encode('utf-8')
    return {'passed': staged == expected, 'checks': {'uppercase_and_suffix': staged == expected}}


def _corrupt_execute(plan, raw):
    return b'corrupt output', {}


class _UnserializableExecutor:
    def __call__(self, plan, raw):
        return _custom_execute(plan, raw)

    def __reduce_ex__(self, protocol):
        raise pickle.PicklingError('callback deliberately refuses serialization')


class _TouchOnUnpickle:
    def __init__(self, marker):
        self.marker = marker

    def __reduce__(self):
        return Path.touch, (self.marker,)


@pytest.fixture
def custom_plugin():
    plugin = ContractPlugin(
        capability='acme.proposal-uppercase', executor_id='acme/uppercase',
        input_formats=('txt',), contract_version=2,
        validate_options=_custom_validate, execute=_custom_execute, verify=_custom_verify,
        description='Uppercase UTF-8 text and append the reviewed suffix.',
    )
    DEFAULT_REGISTRY.register(plugin)
    try:
        yield plugin
    finally:
        DEFAULT_REGISTRY.unregister(plugin.capability)


@pytest.fixture(params=['host', 'mcp'])
def start_task(request, tmp_path):
    """Use both public starts; privileged transitions always belong to the host."""
    api = HostAPI(tmp_path / 'runs.sqlite')
    server = StdioMCPServer(db_path=api.db_path, owner='alice', session_id='plugin-session',
                            workspace=tmp_path)

    def start(draft):
        raw = 'Review this explicit contract and all its parameters before execution.'
        if request.param == 'host':
            operation = api.start_task({'task': raw, 'draft': draft}, {
                'owner': 'alice', 'session_id': 'plugin-session', 'workspace': str(tmp_path),
            })
        else:
            response = server.handle_message({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'rpe_start_task', 'arguments': {'task': raw, 'draft': draft}},
            })
            assert 'error' not in response, response
            assert response['result']['isError'] is False, response
            operation = response['result']['structuredContent']
        return api, operation

    return start


def _draft(capability, execution, constraints=None):
    return {
        'capability': capability, 'constraints': constraints or [], 'execution': execution,
        'requirements': [{
            'text': 'Review the explicit capability, constraints and execution parameters.',
            'quote': None,
            'fields': ['/capability', '/constraints', *('/execution/' + key for key in execution)],
        }],
        'unresolved': [],
    }


def _builtin_draft(tmp_path, capability):
    if capability == 'tabular.filter':
        source = tmp_path / 'input.csv'
        source.write_bytes(b'id,status,code\n001,active,0042\n002,inactive,0007\n003,active,0099\n')
        return _draft(capability, {
            'input_path': 'input.csv', 'output_path': 'output.csv',
            'column': 'status', 'operator': 'equals', 'value': 'active',
        }, ['preserve_original', 'preserve_order'])
    (tmp_path / 'input.json').write_bytes(
        b'{"debug":false,"legacy":"drop","nested":{"keep":1},"untouched":["001",null]}\n')
    return _draft(capability, {
        'input_path': 'input.json', 'output_path': 'output.json',
        'operations': [
            {'op': 'set', 'key': 'debug', 'value': True},
            {'op': 'set', 'key': 'nested.added', 'value': [1, 2]},
            {'op': 'delete', 'key': 'legacy'},
        ],
    }, ['preserve_original'])


def _confirm(api, operation, **changes):
    values = {
        'owner': 'alice', 'operation_id': operation['operation_id'],
        'generation': operation['generation'], 'digest': operation['digest'],
    }
    values.update(changes)
    return api.confirm_task(operation['operation_id'], values['digest'], TrustedHostAction(**values))


def _execute(api, operation, confirmation):
    return api.execute_task(operation['operation_id'], 'alice',
                            confirmation_id=confirmation['confirmation_id'])


def _assert_review(operation, tmp_path, draft):
    assert operation['status'] == 'awaiting_confirmation', operation['proposal']
    proposal = operation['proposal']
    assert proposal['errors'] == []
    assert proposal['missing'] == []
    assert proposal['draft'] == draft
    assert proposal['machine_contract']['capability'] == draft['capability']
    for name in ('input_path', 'output_path'):
        absolute = str(tmp_path / draft['execution'][name])
        assert proposal['machine_contract']['execution'][name] == absolute
    assert draft['capability'] in proposal['summary']
    assert draft['execution']['output_path'] in proposal['summary']
    assert operation['confirmation_id'] is None
    assert operation['run_id'] is None
    assert not (tmp_path / draft['execution']['output_path']).exists()


def _assert_success(api, operation, executed):
    assert executed['status'] == 'succeeded', executed.get('execution_result')
    receipt = executed['execution_result']
    assert receipt['verification']['passed'] is True
    assert receipt['worker_cleanup']['cleanup_complete'] is True
    assert executed['run_id']
    assert executed['artifact']['plan_sha256'] == receipt['verification']['plan_sha256']
    replay = api.execute_task(operation['operation_id'], 'alice',
                              confirmation_id=executed['confirmation_id'])
    assert replay['status'] == 'succeeded'
    assert replay['run_id'] == executed['run_id']
    assert replay['execution_result'] == receipt


def test_tabular_filter_full_host_and_mcp_loop(start_task, tmp_path):
    draft = _builtin_draft(tmp_path, 'tabular.filter')
    original = (tmp_path / 'input.csv').read_bytes()
    api, operation = start_task(draft)
    _assert_review(operation, tmp_path, draft)
    assert operation['proposal']['machine_contract']['execution']['encoding'] == 'utf-8'
    assert 'utf-8' in operation['proposal']['summary']
    confirmed = _confirm(api, operation)
    assert confirmed['status'] == 'confirmed', confirmed
    assert not (tmp_path / 'output.csv').exists()
    executed = _execute(api, operation, confirmed)
    _assert_success(api, operation, executed)
    assert (tmp_path / 'input.csv').read_bytes() == original
    assert (tmp_path / 'output.csv').read_bytes() == (
        b'id,status,code\n001,active,0042\n003,active,0099\n')
    assert executed['artifact']['input_rows'] == 3
    assert executed['artifact']['output_rows'] == 2
    assert executed['artifact']['removed_rows'] == 1


def test_json_patch_full_host_and_mcp_loop(start_task, tmp_path):
    draft = _builtin_draft(tmp_path, 'json.patch')
    original = (tmp_path / 'input.json').read_bytes()
    api, operation = start_task(draft)
    _assert_review(operation, tmp_path, draft)
    assert operation['proposal']['machine_contract']['execution']['indent'] == 2
    assert 'nested.added' in operation['proposal']['summary']
    confirmed = _confirm(api, operation)
    assert confirmed['status'] == 'confirmed', confirmed
    executed = _execute(api, operation, confirmed)
    _assert_success(api, operation, executed)
    assert (tmp_path / 'input.json').read_bytes() == original
    assert json.loads((tmp_path / 'output.json').read_text(encoding='utf-8')) == {
        'debug': True, 'nested': {'keep': 1, 'added': [1, 2]}, 'untouched': ['001', None],
    }


def test_dynamically_registered_plugin_full_host_and_mcp_loop(start_task, tmp_path, custom_plugin):
    (tmp_path / 'input.txt').write_text('hello RPE 世界', encoding='utf-8')
    draft = _draft(custom_plugin.capability, {
        'input_path': 'input.txt', 'output_path': 'output.txt', 'mode': 'uppercase',
    }, ['preserve_original'])
    api, operation = start_task(draft)
    _assert_review(operation, tmp_path, draft)
    contract = operation['proposal']['machine_contract']
    assert contract['contract_version'] == 2
    assert contract['execution']['suffix'] == '!'
    assert 'suffix' in operation['proposal']['summary']
    confirmed = _confirm(api, operation)
    assert confirmed['status'] == 'confirmed', confirmed
    executed = _execute(api, operation, confirmed)
    _assert_success(api, operation, executed)
    assert (tmp_path / 'input.txt').read_text(encoding='utf-8') == 'hello RPE 世界'
    assert (tmp_path / 'output.txt').read_text(encoding='utf-8') == 'HELLO RPE 世界!'
    assert executed['execution_result']['verification']['checks']['uppercase_and_suffix'] is True


def test_dynamic_plugin_local_lambda_closures_full_loop(start_task, tmp_path):
    prefix = b'local closure: '
    plugin = ContractPlugin(
        capability='acme.local-closure', executor_id='acme/local-closure', input_formats=('txt',),
        validate_options=lambda options, raw: {},
        execute=lambda plan, raw: (prefix + raw.upper(), {}),
        verify=lambda plan, raw, staged: {
            'passed': staged == prefix + raw.upper(),
            'checks': {'closure_prefix_and_uppercase': staged == prefix + raw.upper()},
        },
        description='Prefix uppercase text with a locally captured value.',
    )
    DEFAULT_REGISTRY.register(plugin)
    try:
        (tmp_path / 'input.txt').write_bytes(b'hello')
        draft = _draft(plugin.capability, {'input_path': 'input.txt', 'output_path': 'output.txt'})
        api, operation = start_task(draft)
        _assert_review(operation, tmp_path, draft)
        confirmed = _confirm(api, operation)
        assert confirmed['status'] == 'confirmed', confirmed
        executed = _execute(api, operation, confirmed)
        _assert_success(api, operation, executed)
        assert (tmp_path / 'input.txt').read_bytes() == b'hello'
        assert (tmp_path / 'output.txt').read_bytes() == b'local closure: HELLO'
        assert executed['execution_result']['verification']['checks']['closure_prefix_and_uppercase']
    finally:
        DEFAULT_REGISTRY.unregister(plugin.capability)


def test_dynamic_plugin_transport_failure_consumes_terminal_receipt(start_task, tmp_path, custom_plugin):
    DEFAULT_REGISTRY.register(replace(custom_plugin, execute=_UnserializableExecutor(),
                                      executor_revision='c' * 64), replace=True)
    (tmp_path / 'input.txt').write_bytes(b'hello')
    draft = _draft(custom_plugin.capability, {
        'input_path': 'input.txt', 'output_path': 'output.txt', 'mode': 'uppercase',
    })
    api, operation = start_task(draft)
    _assert_review(operation, tmp_path, draft)
    confirmed = _confirm(api, operation)
    assert confirmed['status'] == 'confirmed', confirmed
    failed = _execute(api, operation, confirmed)
    assert failed['status'] == 'failed', failed
    assert failed['execution_result']['status'] == 'failed'
    assert failed['run_id'] is None
    replay = _execute(api, operation, confirmed)
    assert replay['status'] == 'failed'
    assert replay['execution_result'] == failed['execution_result']
    assert replay['run_id'] is None
    assert (tmp_path / 'input.txt').read_bytes() == b'hello'
    assert not (tmp_path / 'output.txt').exists()


def test_private_plugin_worker_rejects_transport_tampering_before_unpickling(tmp_path):
    marker = tmp_path / 'unpickle-side-effect'
    serialized = pickle.dumps(_TouchOnUnpickle(marker))
    transport = tmp_path / 'contract.pickle'
    # Pickle permits trailing bytes. Without the digest check, this payload
    # would still unpickle successfully and create the marker.
    transport.write_bytes(serialized + b'tampered transport')
    result = subprocess.run([
        sys.executable, '-B', '-m', 'rpe_harness.proposal_worker', str(transport),
        hashlib.sha256(serialized).hexdigest(), '--help',
    ], capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode != 0
    assert 'registered plugin transport changed before launch' in result.stderr
    assert not marker.exists()


def test_plugin_entrypoint_can_import_host_api_during_discovery():
    script = dedent('''
        from importlib import metadata
        import json

        class EntryPoint:
            name = 'host-aware-contract'

            def load(self):
                from rpe_harness.host_api import HostAPI
                from rpe_harness.contract_sdk import ContractPlugin
                assert callable(HostAPI.start_task)
                return ContractPlugin(
                    capability='acme.entrypoint-host', executor_id='acme/entrypoint-host',
                    input_formats=('txt',), validate_options=lambda options, raw: {},
                    execute=lambda plan, raw: (raw, {}),
                    verify=lambda plan, raw, staged: {'passed': staged == raw},
                )

        def entry_points(*, group):
            return [EntryPoint()] if group == 'rpe_harness.contracts' else []

        metadata.entry_points = entry_points
        import rpe_harness.proposal
        from rpe_harness.contract_sdk import DEFAULT_REGISTRY
        plugin = DEFAULT_REGISTRY.get('acme.entrypoint-host')
        assert plugin is not None, DEFAULT_REGISTRY.load_errors
        print(json.dumps({'capability': plugin.capability, 'load_errors': DEFAULT_REGISTRY.load_errors}))
    ''')
    result = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True,
                            text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'capability': 'acme.entrypoint-host', 'load_errors': []}


def test_registered_plugin_intent_validates_options_and_source_facts(tmp_path, custom_plugin):
    from rpe_harness.capability_catalog import build_catalog
    from rpe_harness.intent import interpret_intent

    original = b'hello'
    (tmp_path / 'input.txt').write_bytes(original)
    extracted = {
        'goal': 'Uppercase text', 'capability': custom_plugin.capability,
        'constraints': ['preserve_original'],
        'execution': {'input_path': 'input.txt', 'output_path': 'output.txt', 'mode': 'uppercase'},
    }
    arguments = {
        'catalog': build_catalog(), 'workspace': tmp_path,
        'session_id': 'intent-session', 'source_ref': 'intent-source',
    }
    ready = interpret_intent(extracted, source_facts={'execution': {'mode': 'uppercase'}}, **arguments)
    assert ready['status'] == 'ready', ready
    assert ready['request']['capability'] == custom_plugin.capability
    assert ready['request']['contract_version'] == 2
    assert ready['request']['execution'] == {
        'input_path': str(tmp_path / 'input.txt'), 'output_path': str(tmp_path / 'output.txt'),
        'mode': 'uppercase',
    }
    assert ready['input_sha256'] == hashlib.sha256(original).hexdigest()
    conflict = interpret_intent(extracted, source_facts={'execution': {'mode': 'lowercase'}}, **arguments)
    assert conflict['status'] == 'conflict'
    invalid = {**extracted, 'execution': {**extracted['execution'], 'suffix': False}}
    rejected = interpret_intent(invalid, **arguments)
    assert rejected['status'] == 'rejected'
    assert rejected['errors']
    assert (tmp_path / 'input.txt').read_bytes() == original
    assert not (tmp_path / 'output.txt').exists()


@pytest.mark.parametrize('capability', ['tabular.filter', 'json.patch'])
def test_plugin_unconfirmed_and_stale_digest_rejections(start_task, tmp_path, capability):
    draft = _builtin_draft(tmp_path, capability)
    api, operation = start_task(draft)
    _assert_review(operation, tmp_path, draft)
    denied = api.execute_task(operation['operation_id'], 'alice', confirmation_id=operation['digest'])
    assert denied['status'] == 'forbidden'
    with pytest.raises(ValueError, match='confirmation not found'):
        api.proposals.execute(operation['proposal_id'], session_id='plugin-session', workspace=tmp_path)
    assert _confirm(api, operation, digest='0' * 64)['status'] == 'stale'
    assert _confirm(api, operation, generation=operation['generation'] + 1)['status'] == 'stale'
    with pytest.raises(ValueError, match='digest'):
        api.proposals.confirm(operation['proposal_id'], session_id='plugin-session',
                              expected_proposal_sha256='0' * 64, actor='human')
    shown = api.show_task(operation['operation_id'], 'alice')
    assert shown['confirmation_id'] is None
    assert shown['run_id'] is None
    assert not (tmp_path / draft['execution']['output_path']).exists()


@pytest.mark.parametrize('capability', ['tabular.filter', 'json.patch'])
@pytest.mark.parametrize('stage', ['before_confirm', 'after_confirm'])
def test_plugin_stale_input_cannot_publish(start_task, tmp_path, capability, stage):
    draft = _builtin_draft(tmp_path, capability)
    api, operation = start_task(draft)
    _assert_review(operation, tmp_path, draft)
    confirmed = _confirm(api, operation) if stage == 'after_confirm' else None
    source = tmp_path / draft['execution']['input_path']
    source.write_bytes(source.read_bytes() + b' ')
    if stage == 'before_confirm':
        assert _confirm(api, operation)['status'] == 'stale'
    else:
        assert confirmed['status'] == 'confirmed'
        with pytest.raises(ValueError):
            _execute(api, operation, confirmed)
    assert not (tmp_path / draft['execution']['output_path']).exists()
    assert api.show_task(operation['operation_id'], 'alice')['run_id'] is None


@pytest.mark.parametrize('capability', ['tabular.filter', 'json.patch'])
@pytest.mark.parametrize('stage', ['before_start', 'before_confirm', 'after_confirm'])
def test_plugin_existing_output_is_never_overwritten(start_task, tmp_path, capability, stage):
    draft = _builtin_draft(tmp_path, capability)
    output = tmp_path / draft['execution']['output_path']
    if stage == 'before_start':
        output.write_bytes(b'keep existing output')
    api, operation = start_task(draft)
    if stage == 'before_start':
        assert operation['status'] != 'awaiting_confirmation'
        assert _confirm(api, operation)['status'] == 'stale'
    else:
        _assert_review(operation, tmp_path, draft)
        confirmed = _confirm(api, operation) if stage == 'after_confirm' else None
        output.write_bytes(b'keep existing output')
        if stage == 'before_confirm':
            assert _confirm(api, operation)['status'] == 'stale'
        else:
            assert confirmed['status'] == 'confirmed'
            with pytest.raises(ValueError):
                _execute(api, operation, confirmed)
    assert output.read_bytes() == b'keep existing output'
    assert api.show_task(operation['operation_id'], 'alice')['run_id'] is None


@pytest.mark.parametrize('capability,changes', [
    ('tabular.filter', {'column': 'missing'}),
    ('tabular.filter', {'operator': 'unsupported'}),
    ('tabular.filter', {'unknown_option': True}),
    ('json.patch', {'operations': []}),
    ('json.patch', {'operations': [{'op': 'delete', 'key': 'missing'}]}),
    ('json.patch', {'unknown_option': True}),
])
def test_plugin_invalid_options_fail_cleanly(start_task, tmp_path, capability, changes):
    draft = _builtin_draft(tmp_path, capability)
    execution = {**draft['execution'], **changes}
    api, operation = start_task(_draft(capability, execution, draft['constraints']))
    assert operation['status'] == 'needs_context', operation['proposal']
    assert operation['proposal']['errors']
    assert _confirm(api, operation)['status'] == 'stale'
    assert not (tmp_path / execution['output_path']).exists()


@pytest.mark.parametrize('capability', [None, '', [], {}], ids=['null', 'empty', 'array', 'object'])
def test_invalid_draft_capability_fails_cleanly(start_task, tmp_path, capability):
    draft = _builtin_draft(tmp_path, 'tabular.filter')
    draft['capability'] = capability
    api, operation = start_task(draft)
    assert operation['status'] in {'unsupported', 'needs_context'}, operation['proposal']
    assert operation['proposal']['errors']
    assert operation['confirmation_id'] is None
    assert operation['run_id'] is None
    assert _confirm(api, operation)['status'] == 'stale'
    assert not (tmp_path / 'output.csv').exists()


@pytest.mark.parametrize('capability', ['tabular.filter', 'json.patch'])
@pytest.mark.parametrize('field', ['input_path', 'output_path'])
@pytest.mark.parametrize('escape', ['traversal', 'outside_absolute'])
def test_plugin_workspace_escape_fails_cleanly(start_task, tmp_path, capability, field, escape):
    draft = _builtin_draft(tmp_path, capability)
    suffix = '.csv' if capability == 'tabular.filter' else '.json'
    execution = dict(draft['execution'])
    execution[field] = ('../outside' + suffix if escape == 'traversal' else
                        str(tmp_path.parent / ('outside' + suffix)))
    api, operation = start_task(_draft(capability, execution, draft['constraints']))
    assert operation['status'] == 'needs_context', operation['proposal']
    assert operation['proposal']['errors']
    assert _confirm(api, operation)['status'] == 'stale'
    assert api.show_task(operation['operation_id'], 'alice')['run_id'] is None
    assert not (tmp_path / draft['execution']['output_path']).exists()


def test_dynamic_plugin_oracle_rejects_bad_executor_before_publication(start_task, tmp_path, custom_plugin):
    DEFAULT_REGISTRY.register(replace(custom_plugin, execute=_corrupt_execute,
                                      executor_revision='a' * 64), replace=True)
    (tmp_path / 'input.txt').write_bytes(b'hello')
    draft = _draft(custom_plugin.capability, {
        'input_path': 'input.txt', 'output_path': 'output.txt', 'mode': 'uppercase',
    })
    api, operation = start_task(draft)
    _assert_review(operation, tmp_path, draft)
    confirmed = _confirm(api, operation)
    assert confirmed['status'] == 'confirmed', confirmed
    executed = _execute(api, operation, confirmed)
    assert executed['status'] == 'failed', executed.get('execution_result')
    assert executed['execution_result']['verification']['passed'] is False
    assert executed['execution_result']['worker_cleanup']['cleanup_complete'] is True
    assert (tmp_path / 'input.txt').read_bytes() == b'hello'
    assert not (tmp_path / 'output.txt').exists()


@pytest.mark.parametrize('stage', ['before_confirm', 'after_confirm'])
def test_dynamic_plugin_revision_change_invalidates_review(start_task, tmp_path, custom_plugin, stage):
    (tmp_path / 'input.txt').write_bytes(b'hello')
    draft = _draft(custom_plugin.capability, {
        'input_path': 'input.txt', 'output_path': 'output.txt', 'mode': 'uppercase',
    })
    api, operation = start_task(draft)
    _assert_review(operation, tmp_path, draft)
    confirmed = _confirm(api, operation) if stage == 'after_confirm' else None
    DEFAULT_REGISTRY.register(replace(custom_plugin, executor_revision='b' * 64), replace=True)
    if stage == 'before_confirm':
        assert _confirm(api, operation)['status'] == 'stale'
    else:
        assert confirmed['status'] == 'confirmed'
        with pytest.raises(ValueError):
            _execute(api, operation, confirmed)
    assert not (tmp_path / 'output.txt').exists()
