"""Hermes understanding followed by an immutable, parent-owned task address.

Hermes supplies natural-language extraction once. The parent owns interpretation,
capability trust, durable state and the existing independent execution oracle.
Only the compact state handoff is returned to the model.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading

from .hermes_gate import digest_json, _same_call
from .hermes_skills import (IDS, WorkflowStore, _identity, _actual_policy, _raw_policy,
                            _observer_policy, _skill_exchange)

PHASE_TO_TOOL = {'understand': 'rpe_understand', 'task': 'rpe_task', 'none': 'none'}
TASK_HINT = 'Call rpe_task once with only the state_id in this handoff. Do not reconstruct business parameters.'
FINAL_HINT = 'The task is terminal. Do not call tools. Report this durable result accurately.'


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8'))


def _minimal_messages(state):
    if state['phase'] == 'task':
        hint, value = TASK_HINT, state['handoff']
    else:
        hint, value = FINAL_HINT, state.get('task_result', state.get('terminal_result', {'status': 'rejected'}))
    return [{'role': 'system', 'content': hint},
            {'role': 'user', 'content': json.dumps(value, ensure_ascii=False, allow_nan=False,
                                                  sort_keys=True, separators=(',', ':'))}]


class AddressWorkflowStore(WorkflowStore):
    """Use the existing identity-keyed durable middleware store."""
    def get(self, native, *, create=True):
        state = super().get(native, create=create)
        if state.get('entry_mode') is None and state['phase'] == 'discover':
            state.update(entry_mode='address', phase='understand')
            self.put(state)
        if state.get('entry_mode') != 'address':
            raise ValueError('address workflow identity differs')
        return state


def _configuration():
    path = Path(os.environ.get('RPE_HERMES_ADDRESS_CONFIG', ''))
    if (not path.is_absolute() or path != path.resolve() or path.is_symlink() or not path.is_file()
            or path.stat().st_size > 8 * 1_048_576):
        raise ValueError('trusted address configuration missing')
    raw = path.read_bytes()
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {'catalog_sha256', 'entries'}
            or not isinstance(value['catalog_sha256'], str) or len(value['catalog_sha256']) != 64
            or type(value['entries']) is not list or len(value['entries']) > 4096):
        raise ValueError('invalid address configuration')
    return value, str(path), hashlib.sha256(raw).hexdigest()


def _catalog(db_path, catalog_root=None, snapshot_id=None):
    from ..capability_catalog import build_catalog
    return build_catalog(db_path=db_path if catalog_root else None, catalog_root=catalog_root,
                         skill_snapshot_id=snapshot_id)


def address_extension(db_path, workspace, *, catalog_root=None, snapshot_id=None,
                      source_ref=None, source_facts=None):
    """Trusted parent callback; the broker validates receipt identity first."""
    from .hermes import HostEventStore
    from ..intent import interpret_intent, MinimalStateStore
    directory = Path(workspace).resolve()
    events = HostEventStore(db_path)
    workflows, states = AddressWorkflowStore(db_path), MinimalStateStore(db_path)
    current = _catalog(db_path, catalog_root, snapshot_id)
    lock = threading.RLock()
    source = source_ref or 'native-user-request'
    facts = json.loads(json.dumps(source_facts, allow_nan=False)) if source_facts is not None else None

    def extension(operation, arguments, native):
        if operation not in {'understand', 'resolve_address'} or type(arguments) is not dict:
            raise ValueError('unsupported address RPC')
        native = dict(native)
        _identity(native, native.get('entry_key'))
        expected = 'rpe_understand' if operation == 'understand' else 'rpe_task'
        if native.get('tool_name') != expected or native.get('arguments_sha256') != digest_json(arguments):
            raise ValueError('address RPC identity or argument hash differs')
        _actual_policy(events, native, expected)
        with lock:
            state = workflows.get(native)
            _raw_policy(events, native, state)
            if PHASE_TO_TOOL.get(state['phase']) != expected:
                raise ValueError('address phase does not admit RPC')
            if state.get('catalog_sha256') != current.snapshot_id:
                raise ValueError('capability catalog changed')
            # Persist hashes and identity, never model arguments or chat text.
            state.setdefault('rpc_requests', {})[native['tool_call_id'] + ':' + operation] = {
                'operation': operation, 'arguments_sha256': digest_json(arguments),
                'native': {key: native[key] for key in IDS}}
            if operation == 'understand':
                result = interpret_intent(arguments, catalog=current, workspace=directory,
                    session_id=native['session_id'], source_ref=source, source_facts=facts)
                state.update(understand_native={key: native[key] for key in IDS},
                             understanding_status=result['status'])
                if result['status'] == 'ready':
                    handoff = states.capture(result, session_id=native['session_id'], source_ref=source)
                    state.update(phase='task', state_id=handoff['state_id'], capability=handoff['capability'],
                                 handoff=handoff, source_ref=source, snapshot_id=snapshot_id,
                                 catalog_root=str(Path(catalog_root).resolve()) if catalog_root else None,
                                 anchored_request_sha256=digest_json(result['request']))
                    workflows.put(state)
                    return handoff
                state['attempts'] = min(3, state['attempts'] + 1)
                # Missing business context, conflicts and unsupported capabilities are final.
                correctable = result['status'] == 'rejected' and state['attempts'] < 3
                state.update(phase='understand' if correctable else 'none', terminal_status=result['status'],
                             terminal_result=result)
                workflows.put(state)
                return result
            if set(arguments) != {'state_id'} or arguments.get('state_id') != state.get('state_id'):
                raise ValueError('task address differs from captured state')
            resolved = states.resolve(arguments['state_id'], session_id=native['session_id'],
                                      catalog_sha256=current.snapshot_id)
            if (resolved.get('status') != 'ready' or digest_json(resolved['request']) !=
                    state.get('anchored_request_sha256')):
                raise ValueError('anchored request integrity differs')
            state['execution_native'] = {key: native[key] for key in IDS}
            workflows.put(state)
            return resolved
    return extension


def validate_address_execution(db_path, workspace, payload, native):
    """Independently bind a /task payload to the parent's state, if address mode."""
    with sqlite3.connect(Path(db_path).as_uri() + '?mode=ro', uri=True) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='rpe_skill_workflows'").fetchone()
        if not exists:
            return
        rows = db.execute('SELECT state_json,state_sha256 FROM rpe_skill_workflows WHERE '
                          'entry_key=? AND session_id=? AND turn_id=?',
                          tuple(native.get(k) for k in ('entry_key', 'session_id', 'turn_id'))).fetchall()
    if not rows:
        return
    state = json.loads(rows[0][0])
    if state.get('entry_mode') != 'address':
        return
    if (digest_json(state) != rows[0][1] or state.get('phase') != 'task' or
            not _same_call(state.get('execution_native', {}), native)):
        raise PermissionError('address execution identity or state differs')
    from ..intent import MinimalStateStore
    from .hermes import _prepare_request
    # MinimalStateStore rechecks its saved catalog source/trust anchor. Do not
    # rebuild or scan unrelated skill descriptions at this execution boundary.
    resolved = MinimalStateStore(db_path).resolve(state['state_id'], session_id=native['session_id'],
                                                catalog_sha256=state['catalog_sha256'])
    expected = _prepare_request({'request': resolved['request']}, Path(workspace), native['session_id'])
    expected['event_id'] = payload.get('event_id')  # Existing strict executor binds this to entry/contract/input.
    if (expected != payload or digest_json(resolved['request']) != state.get('anchored_request_sha256')):
        raise PermissionError('executor payload differs from captured address')


def register_address_workflow(ctx, *, store, workspace, python, error_path):
    """Install official middleware without changing native learning configuration."""
    from .hermes import _CALL_CONTEXT, _execute, _mark_observer_failure
    config, config_path, config_sha = _configuration()
    entry_key = os.environ.get('RPE_HERMES_ENTRY_KEY')
    if not entry_key:
        raise ValueError('address requires a trusted entry key')
    workflows, lock = AddressWorkflowStore(store.path), threading.RLock()
    tool_schemas = {}
    required = None
    fixed = os.environ.get('RPE_HERMES_REQUIRED_REQUEST_PATH')
    if fixed:
        path = Path(fixed)
        if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 1_048_576:
            raise ValueError('invalid fixed address request')
        required = json.loads(path.read_text())

    def fresh_config():
        if _configuration()[2] != config_sha:
            raise ValueError('trusted address configuration changed')

    def fail(state, code, *, terminal=True):
        state['attempts'] = min(3, state['attempts'] + 1)
        if terminal or state['attempts'] >= 3:
            state['phase'] = 'none'
        state.update(terminal_status='rejected', terminal_result={'status': 'rejected', 'error': {'code': code}})
        workflows.put(state)
        return json.dumps(state['terminal_result'])

    def policy(request, **context):
        try:
            native = _identity(context, entry_key, api=True)
            with lock:
                state = workflows.get(native)
                state.update(config_path=config_path, config_sha256=config_sha,
                             catalog_sha256=config['catalog_sha256'])
                name = PHASE_TO_TOOL[state['phase']]
                chosen = [tool for tool in request.get('tools', []) if isinstance(tool, dict) and
                          (tool.get('function') or {}).get('name') == ('rpe_task' if name == 'none' else name)]
                if not chosen:
                    # Hermes may progressively defer plugin tools behind tool_search.
                    # This strategy admits our own trusted phase schema directly;
                    # the native comparison keeps its existing discovery policy.
                    chosen = [{'type': 'function', 'function': tool_schemas['rpe_task' if name == 'none' else name]}]
                if len(chosen) != 1:
                    raise ValueError('current address tool unavailable')
                chosen = json.loads(json.dumps(chosen, allow_nan=False))
                hint = {
                    'understand': 'Extract the user task exactly into rpe_understand: goal, capability, constraints, '
                        'execution. Do not guess missing business facts, columns, paths, modes or constraints. '
                        'Only the following catalog entries describe available capabilities. A discovery-only entry '
                        'does not grant execution.\n' + json.dumps(config['entries'], ensure_ascii=False),
                    'task': TASK_HINT, 'none': FINAL_HINT}[state['phase']]
                rewritten = dict(request)
                rewritten['messages'] = (list(request.get('messages', [])) + [{'role': 'system', 'content': hint}]
                    if state['phase'] == 'understand' else _minimal_messages(state))
                rewritten.update(tools=chosen, parallel_tool_calls=False,
                    tool_choice='none' if name == 'none' else {'type': 'function', 'function': {'name': name}})
                mode, observed_digest = _observer_policy(rewritten)
                state['policies'][native['api_request_id']] = {'choice': name, 'tools_sha256': observed_digest,
                    'raw_tools_sha256': digest_json(chosen), 'tools_schema': chosen,
                    'projection_mode': mode, 'phase': state['phase'],
                    'messages_sha256': digest_json(rewritten['messages']),
                    'messages_bytes': _json_bytes(rewritten['messages'])}
                if len(state['policies']) > 8:
                    raise ValueError('address API budget exceeded')
                workflows.put(state)
            return {'request': rewritten, 'source': 'rpe_harness', 'reason': 'address_workflow'}
        except (ValueError, TypeError, OSError, sqlite3.Error):
            _mark_observer_failure(error_path, 'address_policy')
            raise

    def api_execution(request, next_call, **context):
        native = _identity(context, entry_key, api=True)
        with lock:
            state = workflows.get(native)
            planned = state['policies'].get(native['api_request_id'])
            choice = request.get('tool_choice')
            choice = (choice.get('function') or {}).get('name') if isinstance(choice, dict) else choice
            if (not planned or choice != planned['choice'] or request.get('parallel_tool_calls') is not False
                    or digest_json(request.get('tools')) != planned['raw_tools_sha256'] or planned.get('raw_receipt_id')
                    or digest_json(request.get('messages')) != planned['messages_sha256']
                    or _json_bytes(request.get('messages')) != planned['messages_bytes']):
                raise ValueError('actual address API schema differs')
            receipt = store.record('rpe_skill_api_checked', {**native, 'request': {'body': request},
                'arguments_sha256': planned['tools_sha256'], 'status': planned['projection_mode'],
                'workflow_phase': planned['phase'], 'request_messages_sha256': planned['messages_sha256'],
                'request_messages_bytes': planned['messages_bytes']})
            planned['raw_receipt_id'] = receipt['ingest_event_id']
            workflows.put(state)
        return next_call(request)

    def handle(name, args, task_id=None, session_id=None):
        native = {**_CALL_CONTEXT.get(), 'tool_name': name, 'entry_key': entry_key}
        if native.get('session_id') != session_id or native.get('task_id') != task_id:
            return json.dumps({'status': 'rejected', 'error': {'code': 'entry_context_missing'}})
        state = workflows.get(native)
        try:
            fresh_config()
            operation = 'understand' if name == 'rpe_understand' else 'resolve_address'
            if type(args) is not dict or (name == 'rpe_task' and set(args) != {'state_id'}):
                return fail(state, 'address_arguments_invalid', terminal=name == 'rpe_task')
            result, receipt_id = _skill_exchange(operation, args, store=store, native=native)
            state = workflows.get(native)
            record = store.record('rpe_address_understood' if name == 'rpe_understand' else 'rpe_address_resolved',
                {**native, 'receipt_id': receipt_id, 'state_id': state.get('state_id'),
                 'catalog_sha256': state.get('catalog_sha256'), 'arguments_sha256': digest_json(args),
                 'workflow_phase': state['phase'], 'status': result['status']})
            if name == 'rpe_understand':
                state['understand_receipt_id'] = record['ingest_event_id']
                workflows.put(state)
                return json.dumps(result, ensure_ascii=False, allow_nan=False)
            state['resolve_receipt_id'] = record['ingest_event_id']
            workflows.put(state)
            # This request crossed only the authenticated parent RPC; it is not model output.
            task = json.loads(_execute({'request': result['request']}, store=store, workspace=Path(workspace),
                python=python, task_id=task_id, session_id=session_id, entry_key=entry_key,
                required_request=required, input_sha=result.get('input_sha256'), task_attempt=1))
            state = workflows.get(native)
            state.update(phase='none', task_result=task, task_native={key: native[key] for key in IDS},
                         terminal_status=task['status'])
            workflows.put(state)
            return json.dumps(task, ensure_ascii=False)
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
            return fail(workflows.get(native), 'address_validation_failed')

    def middleware(tool_name, args, next_call, **context):
        try:
            native = {**_identity(context, entry_key), 'tool_name': tool_name}
            with lock:
                state = workflows.get(native)
                if tool_name != PHASE_TO_TOOL[state['phase']] or tool_name == 'none':
                    return fail(state, 'workflow_phase_invalid')
                observed = _actual_policy(store, native, tool_name, require_pre=False)
                _raw_policy(store, native, state)
                planned = state['policies'].get(native['api_request_id'])
                if not planned or planned['tools_sha256'] != observed.get('request_tools_sha256'):
                    return fail(state, 'workflow_policy_invalid')
                token = _CALL_CONTEXT.set({key: native[key] for key in IDS})
                try:
                    return next_call(args)
                finally:
                    _CALL_CONTEXT.reset(token)
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
            return json.dumps({'status': 'rejected', 'error': {'code': 'workflow_identity_invalid'}})

    ctx.register_middleware('llm_request', policy)
    ctx.register_middleware('llm_execution', api_execution)
    ctx.register_middleware('tool_execution', middleware)
    execution = {'input_path': {'type': 'string'}, 'output_path': {'type': 'string'},
        'key_columns': {'type': 'array', 'items': {'type': 'string'}}, 'keep': {'type': 'string'},
        'encoding': {'type': 'string'}, 'delimiter': {'type': 'string',
            'description': 'One delimiter character. TAB or literal \\t denotes one tab.'},
        'sheet': {'type': 'string'},
        'mode': {'type': 'string', 'enum': ['rows', 'sheets', 'formulas']},
        'style': {'type': 'string', 'enum': ['pretty', 'minify']}}
    fields = {'goal': {'type': 'string'}, 'capability': {'type': 'string'},
              'constraints': {'type': 'array', 'items': {'type': 'string'}},
              'execution': {'type': 'object', 'properties': execution, 'additionalProperties': False}}
    for name, properties, required_fields, description in (
        ('rpe_understand', fields, ['goal'], 'Submit exact structured understanding for catalog validation and '
         'durable state capture. Preserve user constraints. Omit missing business fields; never guess.'),
        ('rpe_task', {'state_id': {'type': 'string'}}, ['state_id'],
         'Execute the immutable parent-owned task state and return independent verification. Only state_id is accepted.')):
        def handler(args, task_id=None, session_id=None, _name=name, **kwargs):
            return handle(_name, args, task_id=task_id, session_id=session_id)
        schema = {
            'name': name, 'description': description, 'parameters': {'type': 'object',
                'properties': properties, 'required': required_fields, 'additionalProperties': False}}
        tool_schemas[name] = schema
        ctx.register_tool(name=name, toolset='rpe_harness', handler=handler, schema=schema)


def evaluate_address_session(result, db_path, workspace, *, catalog_root=None, required_request=None):
    """Reopen durable states, actual API policies, receipts and original artifact oracle."""
    from .hermes_gate import _persisted_events, _verify_run, read_run, contract_for
    from .skill_gate import _state, _policy, _one
    from ..intent import MinimalStateStore
    verdict = {'status': 'unverified', 'verified': False, 'harness_verified': False, 'errors': []}
    try:
        if (result.get('completed') is not True or result.get('exit_code') != 0 or result.get('timed_out')
                or result.get('observer_errors', 0) or
                (result.get('process_cleanup') or {}).get('cleanup_complete') is not True):
            raise ValueError('host_or_observation_incomplete')
        events = _persisted_events(db_path, result.get('session_id'))
        if not events or events != result.get('events'):
            raise ValueError('durable_observations_mismatch')
        state = _state(db_path, result['session_id'])
        if state.get('entry_mode') != 'address':
            raise ValueError('address_workflow_missing')
        _policy(events, state)
        for request_id, planned in state['policies'].items():
            checked = [e for e in events if e['kind'] == 'rpe_skill_api_checked' and
                       e.get('api_request_id') == request_id]
            if (len(checked) != 1 or checked[0].get('request_messages_sha256') != planned['messages_sha256']
                    or checked[0].get('request_messages_bytes') != planned['messages_bytes']):
                raise ValueError('actual_address_context_proof_missing')
            if planned['phase'] == 'task':
                minimal = _minimal_messages({**state, 'phase': 'task'})
                if (digest_json(minimal) != planned['messages_sha256']
                        or _json_bytes(minimal) != planned['messages_bytes']):
                    raise ValueError('task_context_not_minimal')
                verdict.update(task_context_bytes=planned['messages_bytes'],
                               task_context_sha256=planned['messages_sha256'], context_minimized=True)
        path = Path(state['config_path'])
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != state['config_sha256']:
            raise ValueError('trusted_address_config_changed')
        current = _catalog(db_path, catalog_root, state.get('snapshot_id'))
        if current.snapshot_id != state['catalog_sha256']:
            raise ValueError('capability_catalog_changed')
        understood = _one(events, 'rpe_address_understood', state['understand_native'], 'rpe_understand')
        if understood['ingest_event_id'] != state['understand_receipt_id']:
            raise ValueError('understanding_receipt_missing')
        for special in [e for e in events if e['kind'] in {'rpe_address_understood', 'rpe_address_resolved'}]:
            receipts = [e for e in events if e['kind'] == 'rpe_skill_rpc_received' and
                        e['ingest_event_id'] == special.get('receipt_id') and _same_call(e, special) and
                        e.get('arguments_sha256') == special.get('arguments_sha256')]
            if len(receipts) != 1:
                raise ValueError('address_parent_receipt_link_missing')
        if not state.get('task_result'):
            status = state.get('terminal_status')
            if status not in {'needs_context', 'unsupported', 'conflict', 'rejected', 'ambiguous'} or state['phase'] != 'none':
                raise ValueError('matching_terminal_status_missing')
            if any(e['kind'] == 'rpe_task_received' for e in events):
                raise ValueError('unexpected_execution_after_understanding_rejection')
            terminal = state.get('terminal_result', {})
            verdict.update(status='accepted_rejection', rejection_reason=status, rejection_verified=True,
                           missing=terminal.get('missing', []), issues=terminal.get('errors', []))
            return verdict
        resolved = MinimalStateStore(db_path).resolve(state['state_id'], session_id=result['session_id'],
                                                     catalog_sha256=current.snapshot_id)
        if digest_json(resolved['request']) != state['anchored_request_sha256']:
            raise ValueError('anchored_request_changed')
        if understood.get('state_id') != state['state_id'] or understood.get('status') != 'ready':
            raise ValueError('understanding_state_link_missing')
        address = _one(events, 'rpe_address_resolved', state['task_native'], 'rpe_task')
        if (address['ingest_event_id'] != state['resolve_receipt_id'] or address.get('state_id') != state['state_id']
                or address.get('arguments_sha256') != digest_json({'state_id': state['state_id']})):
            raise ValueError('task_state_link_missing')
        task = state['task_result']
        post = _one(events, 'post_tool_call', state['task_native'], 'rpe_task')
        receipt = _one(events, 'rpe_task_received', state['task_native'], 'rpe_task')
        returned = _one(events, 'rpe_task_returned', state['task_native'], 'rpe_task')
        if (post.get('receipt_id') != receipt['ingest_event_id'] or returned.get('receipt_id') != receipt['ingest_event_id']
                or task.get('receipt_id') != receipt['ingest_event_id'] or
                any(item.get('run_id') != task.get('run_id') for item in (post, returned))
                or receipt.get('entry_key') != state['entry_key']):
            raise ValueError('task_receipt_run_link_missing')
        run = read_run(db_path, task['run_id'])
        if (contract_for(run['event'], Path(workspace)) != contract_for(resolved['request'], Path(workspace))
                or run['event'].get('goal') != resolved['request'].get('goal')
                or returned.get('status') != run['status']):
            raise ValueError('execution_differs_from_anchored_state')
        verdict.update(_verify_run(run, receipt, Path(workspace).resolve(), required_request),
            run_id=run['run_id'], state_id=state['state_id'], capability=state['capability'],
            catalog_sha256=current.snapshot_id,
            execution_kind='native_helper' if state['capability'] == 'xlsx.inspect' else 'rpe_contract')
        verdict.update(handoff_bytes=_json_bytes(state['handoff']), handoff_sha256=digest_json(state['handoff']))
        verdict['harness_verified'] = verdict['verified']
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as error:
        verdict['errors'].append(str(error) if isinstance(error, ValueError) else type(error).__name__)
    return verdict
