"""Exact current-source understanding and parent-owned deterministic execution.

The sole native call is rpe_understand. Controller receipts deliberately have no
API request ID, model tool-call ID, or rpe_task tool identity.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading

from .hermes_address import _catalog, _model_catalog_entries, _json_bytes
from .hermes_gate import digest_json, _same_call
from .hermes_skills import (IDS, WorkflowStore, _identity, _actual_policy, _raw_policy,
                            _observer_policy, _skill_exchange, project_observer_tools)

CONTROLLER = 'rpe.clean.controller/v1'
_INGRESS_VALIDATOR = None


class CleanIngressBlocked(BaseException):
    """Abort this isolated host; Hermes intentionally skips ordinary hook errors."""


def guard_clean_ingress(request, **context):
    if _INGRESS_VALIDATOR is None:
        raise CleanIngressBlocked('clean plugin ingress registration missing')
    try:
        _INGRESS_VALIDATOR(request, **context)
    except Exception as error:
        raise CleanIngressBlocked('actual clean API ingress validation failed') from error


CLEAN_SOURCE_CONTRACT = (
    'Current request authorization v1: Only a strict JSON root with exactly capability, constraints, '
    'execution authorizes execution. Do not add missing fields or interpret prose as authorization. '
    'Non-JSON/free text: return needs_context with missing ["structured_current_request"]. '
    'Missing required execution field: return needs_context listing execution.FIELD. '
    'When using native terminal/file tools publish refusal as terminal.json; the dedicated understanding '
    'entry returns its durable refusal itself. Preserve inputs and never overwrite. Paths are workspace relative. '
    'tabular.deduplicate: exact string keys, keep first unless declared otherwise, preserve row order and all '
    'cells, declared delimiter/encoding. json.normalize minify/pretty: change structural whitespace only, '
    'preserve numeric spelling, escapes, object/array order; end with one LF. xlsx.inspect: native agents '
    'read the installed xlsx skill through skill_view, then execute its original scripts/xlsx_read.py with '
    '{helper_python}; rows mode uses --json and optional --sheet SHEET, save original helper JSON structure. '
    'The dedicated understanding entry selects its reviewed adapter deterministically. Python with openpyxl: '
    '{helper_python}. Use terminal/file/native skill tools or the dedicated understanding entry; do not '
    'install/train/upgrade or read controller files/databases/other sessions/reports/RPE implementation. '
    'No business output on refusal.')


class CleanWorkflowStore(WorkflowStore):
    def get(self, native, *, create=True):
        state = super().get(native, create=create)
        if state.get('entry_mode') is None and state['phase'] == 'discover':
            state.update(entry_mode='clean', phase='understand')
            self.put(state)
        if state.get('entry_mode') != 'clean':
            raise ValueError('clean workflow identity differs')
        return state

    def claim_controller(self, state):
        """Consume authority atomically across independent live broker instances."""
        previous = digest_json(state)
        claimed = {**state, 'controller_claimed': True}
        raw = json.dumps(claimed, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        if state.get('controller_claimed') or len(raw.encode()) > 256 * 1024:
            raise PermissionError('clean controller authority already consumed or oversized')
        with sqlite3.connect(self.path, timeout=10) as db:
            changed = db.execute('UPDATE rpe_skill_workflows SET state_json=?,state_sha256=? WHERE '
                'entry_key=? AND session_id=? AND turn_id=? AND state_sha256=?',
                (raw, digest_json(claimed), *(state[k] for k in ('entry_key', 'session_id', 'turn_id')), previous)).rowcount
            if changed != 1:
                raise PermissionError('clean controller authority changed or consumed')
        state.update(controller_claimed=True)


def _configuration():
    path = Path(os.environ.get('RPE_HERMES_CLEAN_CONFIG', ''))
    if (not path.is_absolute() or path != path.resolve() or path.is_symlink() or not path.is_file()
            or path.stat().st_size > 8 * 1_048_576):
        raise ValueError('trusted clean configuration missing')
    raw = path.read_bytes()
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {'catalog_sha256', 'entries', 'raw', 'source_ref'}
            or not isinstance(value['raw'], str) or not 0 < len(value['raw'].encode()) <= 65536
            or not isinstance(value['source_ref'], str) or not value['source_ref']
            or not isinstance(value['catalog_sha256'], str) or len(value['catalog_sha256']) != 64):
        raise ValueError('invalid clean configuration')
    value['entries'] = _model_catalog_entries(value['entries'])
    return value, str(path), hashlib.sha256(raw).hexdigest()


def _messages(config, state):
    if state['phase'] == 'understand':
        instruction = (CLEAN_SOURCE_CONTRACT.replace('{helper_python}', state['helper_python']) +
            '\nExtract exactly goal, capability, constraints and execution into rpe_understand. '
            'The goal must be exactly "Execute " followed by capability. Do not guess omitted values. '
            'For unstructured source submit only a concise goal; the parent returns needs_context. '
            'Available contracts:\n' + json.dumps(config['entries'], ensure_ascii=False, sort_keys=True))
        return [{'role': 'system', 'content': instruction}, {'role': 'user', 'content': config['raw']}]
    return [{'role': 'system', 'content': 'The workflow is terminal. Do not call tools. Report this durable result accurately.'},
            {'role': 'user', 'content': json.dumps(state.get('task_result', state.get('terminal_result')),
                ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))}]


def clean_extension(db_path, workspace, *, raw, source_ref, catalog_root=None, snapshot_id=None):
    """Parent callback consumes its captured source; model arguments cannot replace it."""
    from .hermes import HostEventStore
    from ..requirements import source_document, interpret_source_intent, SourceStateStore
    directory = Path(workspace).resolve()
    events, workflows = HostEventStore(db_path), CleanWorkflowStore(db_path)
    states = SourceStateStore(db_path)
    catalog = _catalog(db_path, catalog_root, snapshot_id)
    lock = threading.RLock()

    def extension(operation, arguments, native):
        if operation != 'understand' or type(arguments) is not dict:
            raise ValueError('unsupported clean RPC')
        _identity(native, native.get('entry_key'))
        if native.get('tool_name') != 'rpe_understand' or native.get('arguments_sha256') != digest_json(arguments):
            raise ValueError('clean RPC identity differs')
        _actual_policy(events, native, 'rpe_understand')
        with lock:
            state = workflows.get(native)
            _raw_policy(events, native, state)
            if state['phase'] != 'understand' or state.get('catalog_sha256') != catalog.snapshot_id:
                raise ValueError('clean phase or catalog differs')
            source = source_document(raw, session_id=native['session_id'], source_ref=source_ref)
            result = interpret_source_intent(arguments, source_document=source, catalog=catalog,
                workspace=directory, session_id=native['session_id'], source_ref=source_ref)
            state.update(source_raw=raw, extraction=arguments, understanding_result=result,
                understand_native={key: native[key] for key in IDS},
                source_ref=source_ref, snapshot_id=snapshot_id,
                catalog_root=str(Path(catalog_root).resolve()) if catalog_root else None)
            if result['status'] == 'ready':
                handoff = states.capture(source, result, session_id=native['session_id'], source_ref=source_ref)
                state.update(phase='task', handoff=handoff, state_id=handoff['state_id'],
                    requirements_sha256=handoff['requirements_sha256'], capability=handoff['capability'],
                    anchored_request_sha256=digest_json(result['request']))
                workflows.put(state)
                return handoff
            state.update(phase='none', terminal_status=result['status'], terminal_result=result)
            workflows.put(state)
            return result
    return extension


def controller_envelope(db_path, *, session_id):
    from .skill_gate import _state
    state = _state(db_path, session_id)
    if state.get('entry_mode') != 'clean':
        raise ValueError('clean workflow missing')
    return {'controller': CONTROLLER, 'state_id': state['state_id'],
            'understand_receipt_id': state['understand_receipt_id'],
            'session_id': state['session_id'], 'entry_key': state['entry_key']}


def prepare_controller_execution(db_path, workspace, body):
    """Read-only admission; called while the broker holds its execution lock."""
    from .hermes import HostEventStore, _prepare_request
    from .hermes_gate import contract_for, request_input_sha
    from .skill_gate import _state, _one
    from ..requirements import SourceStateStore
    if (type(body) is not dict or set(body) != {'controller', 'state_id', 'understand_receipt_id', 'session_id', 'entry_key'}
            or body.get('controller') != CONTROLLER
            or any(type(body.get(k)) is not str or not body[k] for k in body)):
        raise PermissionError('invalid clean controller envelope')
    state = _state(db_path, body['session_id'])
    if (state.get('entry_mode') != 'clean' or state.get('phase') != 'task' or
            any(body.get(k) != state.get(k) for k in ('state_id', 'session_id', 'entry_key', 'understand_receipt_id'))
            or state.get('controller_claimed')):
        raise PermissionError('clean controller authority missing or consumed')
    store = HostEventStore(db_path)
    events = store.list(body['session_id'])
    native = {**state['understand_native'], 'entry_key': state['entry_key']}
    _actual_policy(store, native, 'rpe_understand')
    _raw_policy(store, native, state)
    understood = _one(events, 'rpe_clean_understood', native, 'rpe_understand')
    if understood['ingest_event_id'] != body['understand_receipt_id'] or understood.get('state_id') != body['state_id']:
        raise PermissionError('clean understanding receipt differs')
    pre = _one(events, 'pre_tool_call', native, 'rpe_understand')
    if pre.get('arguments_sha256') != digest_json(state['extraction']):
        raise PermissionError('clean native understanding arguments differ')
    rpc = [e for e in events if e['kind'] == 'rpe_skill_rpc_received' and
           e['ingest_event_id'] == understood.get('receipt_id') and _same_call(e, understood) and
           e.get('arguments_sha256') == digest_json(state['extraction'])]
    if len(rpc) != 1:
        raise PermissionError('clean parent RPC receipt missing')
    resolved = SourceStateStore(db_path).resolve(body['state_id'], session_id=body['session_id'],
                                               catalog_sha256=state['catalog_sha256'])
    if (digest_json(resolved['request']) != state['anchored_request_sha256'] or
            resolved['requirements_sha256'] != state['requirements_sha256']):
        raise PermissionError('clean source-state binding differs')
    payload = _prepare_request({'request': resolved['request']}, Path(workspace), body['session_id'])
    contract_sha, input_sha = digest_json(contract_for(payload, Path(workspace))), request_input_sha(payload, Path(workspace))
    payload['event_id'] = 'entry-' + digest_json([body['entry_key'], contract_sha, input_sha])
    controller = {key: state[key] for key in ('entry_key', 'session_id', 'task_id', 'turn_id', 'state_id', 'requirements_sha256')}
    controller.update(controller_id=CONTROLLER, understand_receipt_id=body['understand_receipt_id'],
        source_sha256=resolved['source']['source_sha256'], contract_sha256=contract_sha, input_sha256=input_sha)
    return payload, controller, state


def finish_controller_execution(db_path, state, receipt, result):
    from .hermes import HostEventStore, _compact
    task = _compact(result)
    task.update(receipt_id=receipt['ingest_event_id'], controller_id=CONTROLLER,
        **{k: receipt[k] for k in ('contract_sha256', 'input_sha256')})
    HostEventStore(db_path).record('rpe_controller_task_returned', {
        **{k: v for k, v in receipt.items() if k not in ('kind', 'ingest_event_id', 'received_at', 'source_event_id')},
        'receipt_id': receipt['ingest_event_id'], 'run_id': task.get('run_id'), 'status': task.get('status')})
    state.update(phase='none', task_result=task, controller_receipt_id=receipt['ingest_event_id'],
                 terminal_status=task['status'])
    CleanWorkflowStore(db_path).put(state)
    return task


def _controller_exchange(body):
    from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener
    from urllib.parse import urlsplit
    from urllib.error import HTTPError
    url, token = os.environ.get('RPE_HERMES_BROKER_URL', ''), os.environ.get('RPE_HERMES_BROKER_TOKEN', '')
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path != '/task'
            or parsed.query or parsed.fragment or parsed.username or parsed.password or not parsed.port or not token):
        raise ValueError('invalid clean controller endpoint')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = Request(url, data=json.dumps(body, allow_nan=False).encode(), method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=125) as response:
            raw = response.read(1_048_577)
            if response.status != 200 or len(raw) > 1_048_576:
                raise ValueError('invalid clean controller response')
    except HTTPError as error:
        error.close()
        raise ValueError('clean controller rejected execution') from None
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {'result'} or type(value['result']) is not dict:
        raise ValueError('invalid clean controller result')
    return value['result']


def register_clean_workflow(ctx, *, store, workspace, python, error_path):
    config, config_path, config_sha = _configuration()
    entry_key = os.environ.get('RPE_HERMES_ENTRY_KEY')
    if not entry_key:
        raise ValueError('clean requires a trusted entry key')
    workflows, lock = CleanWorkflowStore(store.path), threading.RLock()
    schema = {'name': 'rpe_understand', 'description': 'Extract the complete current request exactly. Never guess omitted facts.',
        'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['goal'],
            'properties': {'goal': {'type': 'string'}, 'capability': {'type': 'string'},
                'constraints': {'type': 'array', 'items': {'type': 'string'}},
                'execution': {'type': 'object', 'additionalProperties': False, 'properties': {
                    **{k: {'type': 'string'} for k in ('input_path', 'output_path', 'keep', 'encoding', 'delimiter', 'style', 'mode', 'sheet')},
                    'key_columns': {'type': 'array', 'items': {'type': 'string'}}}}}}}

    def policy(request, **context):
        native = _identity(context, entry_key, api=True)
        with lock:
            if _configuration()[2] != config_sha:
                raise ValueError('trusted clean configuration changed')
            state = workflows.get(native)
            if state['phase'] not in ('understand', 'none'):
                raise ValueError('controller execution is still pending')
            if len(state['policies']) >= 2:
                raise ValueError('clean API budget exceeded')
            state.update(config_path=config_path, config_sha256=config_sha, catalog_sha256=config['catalog_sha256'], helper_python=str(python))
            tools = [{'type': 'function', 'function': schema}]
            choice = 'rpe_understand' if state['phase'] == 'understand' else 'none'
            rewritten = {**request, 'messages': _messages(config, state), 'tools': tools,
                'parallel_tool_calls': False, 'tool_choice': 'none' if choice == 'none' else
                {'type': 'function', 'function': {'name': choice}}}
            # Providers may merge extra_body after named SDK arguments. Never
            # permit that second channel to replace the admitted context/tools.
            if isinstance(rewritten.get('extra_body'), dict):
                rewritten['extra_body'] = {k: v for k, v in rewritten['extra_body'].items()
                    if k not in {'messages', 'input', 'instructions', 'tools', 'tool_choice', 'parallel_tool_calls'}}
            mode, observed = _observer_policy(rewritten)
            state['policies'][native['api_request_id']] = {'choice': choice, 'tools_sha256': observed,
                'raw_tools_sha256': digest_json(tools), 'tools_schema': tools, 'projection_mode': mode,
                'phase': state['phase'], 'messages_sha256': digest_json(rewritten['messages']),
                'messages_bytes': _json_bytes(rewritten['messages'])}
            workflows.put(state)
        return {'request': rewritten, 'source': 'rpe_harness', 'reason': 'clean_workflow'}

    def validate_request(request, **context):
        native = _identity(context, entry_key, api=True)
        with lock:
            if _configuration()[2] != config_sha:
                raise ValueError('actual clean API configuration differs')
            state = workflows.get(native)
            planned = state['policies'].get(native['api_request_id'])
            expected_choice = ('none' if planned and planned['choice'] == 'none' else
                               {'type': 'function', 'function': {'name': 'rpe_understand'}})
            if (not planned or planned.get('raw_receipt_id') or request.get('tool_choice') != expected_choice
                    or request.get('parallel_tool_calls') is not False
                    or digest_json(request.get('tools')) != planned['raw_tools_sha256']
                    or digest_json(request.get('messages')) != planned['messages_sha256']
                    or _json_bytes(request.get('messages')) != planned['messages_bytes']):
                raise ValueError('actual clean API context or policy differs')
            extra = request.get('extra_body')
            if isinstance(extra, dict) and set(extra) & {'messages', 'input', 'instructions', 'tools', 'tool_choice', 'parallel_tool_calls'}:
                raise ValueError('actual clean API has a shadow context')
            return native, state, planned

    def api_execution(request, next_call, **context):
        try:
            native, state, planned = validate_request(request, **context)
            with lock:
                receipt = store.record('rpe_skill_api_checked', {**native, 'request': {'body': request},
                    'arguments_sha256': planned['tools_sha256'], 'status': planned['projection_mode'],
                    'workflow_phase': planned['phase'], 'request_messages_sha256': planned['messages_sha256'],
                    'request_messages_bytes': planned['messages_bytes']})
                planned['raw_receipt_id'] = receipt['ingest_event_id']
                workflows.put(state)
        except Exception as error:
            raise CleanIngressBlocked('actual clean API ingress validation failed') from error
        return next_call(request)

    def handle(args, task_id=None, session_id=None):
        from .hermes import _CALL_CONTEXT
        native = {**_CALL_CONTEXT.get(), 'entry_key': entry_key, 'tool_name': 'rpe_understand'}
        if native.get('session_id') != session_id or native.get('task_id') != task_id:
            return json.dumps({'status': 'rejected', 'error': {'code': 'entry_context_missing'}})
        try:
            result, rpc = _skill_exchange('understand', args, store=store, native=native)
            state = workflows.get(native)
            record = store.record('rpe_clean_understood', {**native, 'receipt_id': rpc,
                'state_id': state.get('state_id'), 'requirements_sha256': state.get('requirements_sha256'),
                'arguments_sha256': digest_json(args), 'status': result['status']})
            state['understand_receipt_id'] = record['ingest_event_id']
            workflows.put(state)
            if result['status'] == 'ready':
                result = _controller_exchange(controller_envelope(store.path, session_id=session_id))
            return json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
            state = workflows.get(native)
            state.update(phase='none', terminal_status='rejected',
                terminal_result={'status': 'rejected', 'error': {'code': 'clean_validation_failed'}})
            workflows.put(state)
            return json.dumps(state['terminal_result'])

    def middleware(tool_name, args, next_call, **context):
        from .hermes import _CALL_CONTEXT
        try:
            native = {**_identity(context, entry_key), 'tool_name': tool_name}
            with lock:
                state = workflows.get(native)
                if tool_name != 'rpe_understand' or state['phase'] != 'understand':
                    raise ValueError('clean tool phase differs')
                _actual_policy(store, native, tool_name, require_pre=False)
                _raw_policy(store, native, state)
            token = _CALL_CONTEXT.set({key: native[key] for key in IDS})
            try:
                return next_call(args)
            finally:
                _CALL_CONTEXT.reset(token)
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
            return json.dumps({'status': 'rejected', 'error': {'code': 'clean_identity_invalid'}})
    ctx.register_tool(name='rpe_understand', toolset='rpe_harness', handler=handle, schema=schema)
    ctx.register_middleware('llm_request', policy)
    ctx.register_middleware('llm_execution', api_execution)
    ctx.register_middleware('tool_execution', middleware)
    global _INGRESS_VALIDATOR
    _INGRESS_VALIDATOR = validate_request


def _verify_policy(events, state, config):
    """Check observed SDK projection and raw outgoing bytes independently."""
    from .skill_gate import _one
    requests = [e for e in events if e['kind'] == 'pre_api_request']
    outcomes = [e for e in events if e['kind'] in ('post_api_request', 'api_request_error')]
    if (not 1 <= len(requests) <= 2 or Counter(e.get('api_request_id') for e in requests) !=
            Counter(e.get('api_request_id') for e in outcomes)
            or any(e['kind'] == 'api_request_error' for e in outcomes)
            or set(state.get('policies', {})) != {e.get('api_request_id') for e in requests}):
        raise ValueError('clean_api_observations_incomplete')
    phases = []
    emitted = []
    for pre in requests:
        planned = state.get('policies', {}).get(pre.get('api_request_id'))
        if not planned:
            raise ValueError('clean_policy_missing')
        choice = planned['choice']
        phases.append(choice)
        expected = _messages(config, {**state, 'phase': planned['phase']})
        if (pre.get('request_tool_choice') != choice or pre.get('request_tool_names') != ['rpe_understand']
                or pre.get('request_parallel_tool_calls') is not False
                or pre.get('request_tools_sha256') != planned['tools_sha256']
                or digest_json(project_observer_tools(planned['tools_schema'], planned['projection_mode'])) != planned['tools_sha256']
                or digest_json(planned['tools_schema']) != planned['raw_tools_sha256']
                or digest_json(expected) != planned['messages_sha256'] or _json_bytes(expected) != planned['messages_bytes']):
            raise ValueError('clean_actual_policy_or_source_context_differs')
        raw = [e for e in events if e['kind'] == 'rpe_skill_api_checked' and
               all(e.get(k) == pre.get(k) for k in IDS[:-1])]
        if (len(raw) != 1 or raw[0]['ingest_event_id'] != planned['raw_receipt_id']
                or raw[0].get('request_tools_sha256') != planned['raw_tools_sha256']
                or raw[0].get('arguments_sha256') != planned['tools_sha256']
                or raw[0].get('request_tool_choice') != choice
                or raw[0].get('request_tool_names') != ['rpe_understand']
                or raw[0].get('request_parallel_tool_calls') is not False
                or raw[0].get('entry_key') != state['entry_key']
                or raw[0].get('status') != planned['projection_mode']
                or raw[0].get('request_messages_sha256') != planned['messages_sha256']
                or raw[0].get('request_messages_bytes') != planned['messages_bytes']):
            raise ValueError('clean_raw_API_proof_missing')
        outcome = next(e for e in outcomes if e.get('api_request_id') == pre.get('api_request_id'))
        if any(pre.get(k) != outcome.get(k) or not pre.get(k) for k in IDS[:3]):
            raise ValueError('clean_api_identity_differs')
        calls = outcome.get('model_tool_calls', [])
        if (choice == 'none' and calls) or (choice != 'none' and
                (len(calls) != 1 or calls[0].get('tool_name') != 'rpe_understand')):
            raise ValueError('clean_model_policy_ignored')
        for call in calls:
            native = {**{k: pre[k] for k in IDS[:-1]}, **call}
            for kind in ('pre_tool_call', 'post_tool_call'):
                call_event = _one(events, kind, native, 'rpe_understand')
                if call_event.get('arguments_sha256') != digest_json(state['extraction']):
                    raise ValueError('clean_native_arguments_changed')
            emitted.append(native)
    if phases not in (['rpe_understand'], ['rpe_understand', 'none']):
        raise ValueError('clean_phase_sequence_differs')
    for kind in ('pre_tool_call', 'post_tool_call'):
        rows = [e for e in events if e['kind'] == kind]
        if len(rows) != 1 or not _same_call(rows[0], emitted[0]) or rows[0].get('tool_name') != 'rpe_understand':
            raise ValueError('clean_native_tool_observations_differ')
    return phases.count('none')


def evaluate_clean_session(result, db_path, workspace, *, catalog_root=None, required_request=None):
    from .hermes_gate import _persisted_events, _verify_run, read_run, contract_for
    from .skill_gate import _state, _one
    from ..requirements import source_document, interpret_source_intent, SourceStateStore
    verdict = {'status': 'unverified', 'verified': False, 'harness_verified': False, 'errors': [],
        'execution_kind': 'controller', 'native_understanding_calls': 0, 'controller_executions': 0}
    try:
        if (result.get('completed') is not True or result.get('exit_code') != 0 or result.get('timed_out')
                or result.get('observer_errors', 0) or (result.get('process_cleanup') or {}).get('cleanup_complete') is not True):
            raise ValueError('host_or_observation_incomplete')
        events = _persisted_events(db_path, result.get('session_id'))
        if not events or events != result.get('events'):
            raise ValueError('durable_observations_mismatch')
        if any(e['kind'] in ('rpe_task_received', 'rpe_task_returned') for e in events):
            raise ValueError('clean_has_fabricated_native_task_receipt')
        state = _state(db_path, result['session_id'])
        if state.get('entry_mode') != 'clean' or state['phase'] != 'none':
            raise ValueError('clean_terminal_workflow_missing')
        path = Path(state['config_path'])
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != state['config_sha256']:
            raise ValueError('trusted_clean_config_changed')
        config = json.loads(path.read_text())
        config['entries'] = _model_catalog_entries(config['entries'])
        verdict['reporting_api_requests'] = _verify_policy(events, state, config)
        verdict.update(native_understanding_calls=1, understanding_api_requests=1,
            original_source_sha256=hashlib.sha256(config['raw'].encode()).hexdigest(),
            source_ref=config['source_ref'], controller_id=CONTROLLER)
        source = source_document(config['raw'], session_id=result['session_id'], source_ref=config['source_ref'])
        verdict['requirements_sha256'] = source.get('requirements_sha256')
        if config['raw'] != state['source_raw']:
            raise ValueError('original_source_document_changed')
        current = _catalog(db_path, catalog_root, state.get('snapshot_id'))
        if current.snapshot_id != state['catalog_sha256']:
            raise ValueError('capability_catalog_changed')
        understood = _one(events, 'rpe_clean_understood', state['understand_native'], 'rpe_understand')
        if (understood['ingest_event_id'] != state['understand_receipt_id'] or
                understood.get('arguments_sha256') != digest_json(state['extraction']) or
                understood.get('status') != state['understanding_result']['status']):
            raise ValueError('clean_understanding_receipt_missing')
        rpc = [e for e in events if e['kind'] == 'rpe_skill_rpc_received' and
               e['ingest_event_id'] == understood.get('receipt_id') and _same_call(e, understood)
               and e.get('arguments_sha256') == understood.get('arguments_sha256')]
        if len(rpc) != 1:
            raise ValueError('clean_parent_receipt_missing')
        received = [e for e in events if e['kind'] == 'rpe_controller_task_received']
        if not state.get('task_result'):
            repeated = interpret_source_intent(state['extraction'], source_document=source, catalog=current,
                workspace=workspace, session_id=result['session_id'], source_ref=config['source_ref'])
            if (received or repeated != state['understanding_result'] or repeated['status'] == 'ready'
                    or state.get('terminal_result') != repeated):
                raise ValueError('clean_terminal_rejection_differs')
            verdict.update(status='accepted_rejection', rejection_reason=repeated['status'], rejection_verified=True,
                missing=repeated.get('missing', []), issues=repeated.get('errors', []),
                source_sha256=source['source']['source_sha256'], source_coverage_verified=True)
            return verdict
        resolved = SourceStateStore(db_path).resolve(state['state_id'], session_id=result['session_id'],
                                                    catalog_sha256=current.snapshot_id)
        if (digest_json(resolved['request']) != state['anchored_request_sha256'] or len(received) != 1
                or received[0]['ingest_event_id'] != state.get('controller_receipt_id')):
            raise ValueError('clean_controller_state_link_missing')
        receipt, task = received[0], state['task_result']
        expected = {k: state[k] for k in ('entry_key', 'session_id', 'task_id', 'turn_id', 'state_id', 'requirements_sha256')}
        if (any(receipt.get(k) != value for k, value in expected.items()) or
                receipt.get('controller_id') != CONTROLLER or receipt.get('source_sha256') != resolved['source']['source_sha256']
                or receipt.get('understand_receipt_id') != understood['ingest_event_id']
                or any(k in receipt for k in ('api_request_id', 'tool_call_id', 'tool_name'))):
            raise ValueError('clean_controller_authority_differs')
        returned = [e for e in events if e['kind'] == 'rpe_controller_task_returned']
        if (len(returned) != 1 or returned[0].get('receipt_id') != receipt['ingest_event_id']
                or returned[0].get('run_id') != task.get('run_id') or task.get('receipt_id') != receipt['ingest_event_id']
                or any(returned[0].get(k) != receipt.get(k) for k in (*expected, 'controller_id',
                    'source_sha256', 'understand_receipt_id', 'contract_sha256', 'input_sha256'))
                or any(k in returned[0] for k in ('api_request_id', 'tool_call_id', 'tool_name'))):
            raise ValueError('clean_controller_result_link_missing')
        run = read_run(db_path, task['run_id'])
        if (run['event'].get('goal') != resolved['request'].get('goal') or
                contract_for(run['event'], Path(workspace)) != contract_for(resolved['request'], Path(workspace))
                or returned[0].get('status') != run['status']):
            raise ValueError('clean_execution_differs_from_source')
        verdict.update(_verify_run(run, receipt, Path(workspace).resolve(), required_request),
            controller_executions=1, run_id=run['run_id'], state_id=state['state_id'], source_state_id=state['state_id'],
            requirements_sha256=resolved['requirements_sha256'], source_sha256=resolved['source']['source_sha256'],
            catalog_sha256=current.snapshot_id, capability=state['capability'], source_coverage_verified=True,
            controller_receipt_id=receipt['ingest_event_id'])
        verdict['understand_receipt_id'] = understood['ingest_event_id']
        verdict['harness_verified'] = verdict['verified']
    except (OSError, ValueError, TypeError, KeyError, AttributeError, PermissionError, sqlite3.Error) as error:
        verdict['errors'].append(str(error) if isinstance(error, ValueError) else type(error).__name__)
    return verdict
