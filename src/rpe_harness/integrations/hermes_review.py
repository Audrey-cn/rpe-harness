"""Exact-current-prose model drafts with parent-owned, unapproved proposals.

The sole model tool is rpe_propose. Confirmation and deterministic execution are
separate trusted public operations and are never exposed to this host.
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
from .hermes_clean import _transport_parameters
from .hermes_gate import digest_json, _same_call
from .hermes_skills import (IDS, WorkflowStore, _identity, _actual_policy, _raw_policy,
                            _observer_policy, _skill_exchange, project_observer_tools)


REVIEW_CONTRACT = (
    'Natural-language request review v1: Produce a reviewable draft from the entire current request. '
    'A draft grants no execution authority. Never execute the business task or approve your own draft. '
    'Submit the draft through rpe_propose when available; otherwise save model-generated JSON as draft.json '
    'using the native tools. The human user never has to write JSON. '
    'The draft has exactly capability, constraints, execution, requirements, unresolved. '
    'capability is a string, constraints is an array of registered contract flags, execution is an object, '
    'unresolved is an array of strings describing every unsupported, conflicting or missing requirement. '
    'Each requirements row has exactly text, quote, fields. text is a human-readable requirement; '
    'quote is an exact nonempty substring of the original request, or null for an explicitly proposed default. '
    'The parent labels these source_quote and proposed_default. A quote proves provenance only, never full '
    'semantic coverage. fields is an array of JSON pointers such as /capability, /constraints, '
    '/execution/input_path. Cover every proposed machine field and retain requirements beyond the available '
    'capability in unresolved. Never claim model extraction proves completeness or human approval. '
    'Paths are workspace relative. Preserve inputs and never overwrite. '
    'Available capabilities: tabular.deduplicate accepts csv or xlsx; it keeps first and preserves row order '
    'and cells. Required execution fields: input_path, output_path, key_columns. Optional execution fields: '
    'keep (first only), encoding, delimiter, sheet. Allowed constraints: preserve_original, preserve_order, '
    'preserve_formulas. '
    'json.normalize changes structural whitespace only, preserving number spelling, escapes and object/array '
    'order. Required and only execution fields: input_path, output_path, style (minify or pretty). '
    'Allowed constraints: preserve_original, preserve_order. Output ends with one LF. '
    'xlsx.inspect reads an existing xlsx workbook through the trusted installed xlsx adapter. Required '
    'execution fields: input_path, output_path, mode (rows, sheets or formulas). Optional execution field: '
    'sheet. Allowed constraint: preserve_original. Preserve the original helper JSON structure. '
    'The parent checks actual capability and adapter availability after drafting. '
    'Do not install, train, upgrade, or read controller files/databases/other sessions/reports/RPE implementation. '
    'Only a later explicit human confirmation of the displayed proposal can authorize execution.')

_INGRESS_VALIDATOR = None


class ReviewIngressBlocked(BaseException):
    """Abort the isolated host even when Hermes swallows ordinary hook errors."""


def guard_review_ingress(request, **context):
    if _INGRESS_VALIDATOR is None:
        raise ReviewIngressBlocked('review plugin ingress registration missing')
    try:
        _INGRESS_VALIDATOR(request, **context)
    except Exception as error:
        raise ReviewIngressBlocked('actual review API ingress validation failed') from error


class ReviewWorkflowStore(WorkflowStore):
    def get(self, native, *, create=True):
        state = super().get(native, create=create)
        if state.get('entry_mode') is None and state['phase'] == 'discover':
            state.update(entry_mode='review', phase='propose')
            self.put(state)
        if state.get('entry_mode') != 'review':
            raise ValueError('review workflow identity differs')
        return state


def _configuration():
    path = Path(os.environ.get('RPE_HERMES_REVIEW_CONFIG', ''))
    if (not path.is_absolute() or path != path.resolve() or path.is_symlink() or not path.is_file()
            or path.stat().st_size > 8 * 1_048_576):
        raise ValueError('trusted review configuration missing')
    raw = path.read_bytes()
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {'catalog_sha256', 'entries', 'raw', 'source_ref'}
            or type(value['raw']) is not str or not value['raw'].strip()
            or not 0 < len(value['raw'].encode()) <= 65536
            or type(value['source_ref']) is not str or not value['source_ref']
            or type(value['catalog_sha256']) is not str or len(value['catalog_sha256']) != 64):
        raise ValueError('invalid review configuration')
    value['entries'] = _model_catalog_entries(value['entries'])
    return value, str(path), hashlib.sha256(raw).hexdigest()


def _messages(config, state):
    if state['phase'] == 'propose':
        return [{'role': 'system', 'content': REVIEW_CONTRACT}, {'role': 'user', 'content': config['raw']}]
    return [{'role': 'system', 'content': 'The proposal workflow is terminal. Do not call tools. '
            'Report the durable proposal status accurately. No human approval or execution has occurred.'},
            {'role': 'user', 'content': json.dumps(state.get('proposal_result', state.get('terminal_result')),
                ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))}]


def _proposal_identity(value):
    return {key: value[key] for key in ('status', 'proposal_id', 'proposal_sha256', 'missing', 'errors')
            if key in value}


def _argument_transform(raw, actual):
    """Recognize only the SDK's scalar-array fallback; never parse or repair text."""
    if raw == actual:
        return 'identity'
    if type(raw) is not dict or type(actual) is not dict or set(raw) != set(actual):
        raise ValueError('review_argument_transform_unknown')
    changed = {key for key in raw if raw[key] != actual[key]}
    if (not changed or not changed <= {'requirements', 'unresolved'} or
            any(type(raw[key]) is not str or actual[key] != [raw[key]] for key in changed)):
        raise ValueError('review_argument_transform_unknown')
    return 'array_string_wrapped'


def _argument_provenance(events, state, native, actual):
    from .skill_gate import _one
    raw = state['raw_arguments']
    raw_sha, actual_sha = digest_json(raw), digest_json(actual)
    received = _one(events, 'rpe_review_arguments_received', native, 'rpe_propose')
    checked = _one(events, 'rpe_review_arguments_checked', native, 'rpe_propose')
    pre = _one(events, 'pre_tool_call', native, 'rpe_propose')
    transform = _argument_transform(raw, actual)
    if (received['ingest_event_id'] != state['raw_arguments_receipt_id']
            or received.get('status') != 'raw_before_coercion'
            or received.get('arguments_sha256') != raw_sha or pre.get('arguments_sha256') != raw_sha
            or checked['ingest_event_id'] != state['arguments_receipt_id']
            or checked.get('receipt_id') != received['ingest_event_id']
            or checked.get('arguments_sha256') != actual_sha or checked.get('content_sha256') != raw_sha
            or checked.get('status') != transform
            or any(row.get('entry_key') != state['entry_key'] for row in (received, checked))):
        raise ValueError('review_argument_provenance_differs')
    return transform


def review_extension(db_path, workspace, *, raw, source_ref, catalog_root=None, snapshot_id=None):
    """Capture current prose in the parent; model arguments can supply only a draft."""
    from .hermes import HostEventStore
    from ..proposal import create_proposal
    directory = Path(workspace).resolve()
    events, workflows = HostEventStore(db_path), ReviewWorkflowStore(db_path)
    catalog = _catalog(db_path, catalog_root, snapshot_id)
    lock = threading.RLock()

    def extension(operation, arguments, native):
        if operation != 'propose' or type(arguments) is not dict:
            raise ValueError('unsupported review RPC')
        _identity(native, native.get('entry_key'))
        if native.get('tool_name') != 'rpe_propose' or native.get('arguments_sha256') != digest_json(arguments):
            raise ValueError('review RPC identity differs')
        _actual_policy(events, native, 'rpe_propose')
        with lock:
            state = workflows.get(native)
            _raw_policy(events, native, state)
            if state['phase'] != 'propose' or state.get('catalog_sha256') != catalog.snapshot_id:
                raise ValueError('review phase or catalog differs')
            transform = _argument_provenance(events.list(native['session_id']), state, native, arguments)
            result = create_proposal(raw, arguments, db_path=db_path, workspace=directory,
                session_id=native['session_id'], source_ref=source_ref,
                catalog_root=catalog_root, skill_snapshot_id=snapshot_id)
            if transform != 'identity' and result.get('status') == 'awaiting_confirmation':
                raise ValueError('review_coerced_draft_cannot_be_confirmable')
            state.update(phase='none', source_raw=raw, draft=arguments,
                proposal_native={key: native[key] for key in IDS}, proposal_result=_proposal_identity(result),
                source_ref=source_ref, snapshot_id=snapshot_id,
                catalog_root=str(Path(catalog_root).resolve()) if catalog_root else None)
            workflows.put(state)
            return result
    return extension


def register_review_workflow(ctx, *, store, workspace, python, error_path):
    config, config_path, config_sha = _configuration()
    entry_key = os.environ.get('RPE_HERMES_ENTRY_KEY')
    if not entry_key:
        raise ValueError('review requires a trusted entry key')
    workflows, lock = ReviewWorkflowStore(store.path), threading.RLock()
    string = {'type': 'string'}
    schema = {'name': 'rpe_propose', 'description': 'Draft source-anchored requirements for human review. '
        'This tool never approves or executes the task.', 'parameters': {
            'type': 'object', 'additionalProperties': False,
            'required': ['capability', 'constraints', 'execution', 'requirements', 'unresolved'],
            'properties': {'capability': string, 'constraints': {'type': 'array', 'items': string},
                'execution': {'type': 'object', 'additionalProperties': False, 'properties': {
                    **{key: string for key in ('input_path', 'output_path', 'keep', 'encoding',
                                               'delimiter', 'style', 'mode', 'sheet')},
                    'key_columns': {'type': 'array', 'items': string}}},
                'requirements': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
                    'required': ['text', 'quote', 'fields'], 'properties': {'text': string,
                        'quote': {'type': ['string', 'null']}, 'fields': {'type': 'array', 'items': string}}}},
                'unresolved': {'type': 'array', 'items': string}}}}

    def capture_arguments(tool_name, args, **context):
        if tool_name != 'rpe_propose':
            return None
        try:
            native = {**_identity(context, entry_key), 'tool_name': tool_name}
            raw = json.loads(json.dumps(args, ensure_ascii=False, allow_nan=False))
            if type(raw) is not dict or _json_bytes(raw) > 65536:
                raise ValueError('review raw arguments exceed bounds')
            with lock:
                state = workflows.get(native)
                _actual_policy(store, native, tool_name)
                _raw_policy(store, native, state)
                if state['phase'] != 'propose' or 'raw_arguments' in state:
                    raise ValueError('review raw arguments already captured')
                receipt = store.record('rpe_review_arguments_received', {**native,
                    'arguments_sha256': digest_json(raw), 'status': 'raw_before_coercion'})
                state.update(raw_arguments=raw, raw_arguments_receipt_id=receipt['ingest_event_id'])
                workflows.put(state)
        except Exception as error:
            raise ReviewIngressBlocked('review raw argument capture failed') from error
        return None

    def policy(request, **context):
        try:
            transport = _transport_parameters(request)
            native = _identity(context, entry_key, api=True)
            with lock:
                if _configuration()[2] != config_sha:
                    raise ValueError('trusted review configuration changed')
                state = workflows.get(native)
                if (state['phase'] not in ('propose', 'none') or len(state['policies']) >= 2
                        or native['api_request_id'] in state['policies']):
                    raise ValueError('review phase or API budget exceeded')
                state.update(config_path=config_path, config_sha256=config_sha,
                             catalog_sha256=config['catalog_sha256'])
                tools = [{'type': 'function', 'function': schema}]
                choice = 'rpe_propose' if state['phase'] == 'propose' else 'none'
                rewritten = {**transport, 'messages': _messages(config, state), 'tools': tools,
                    'parallel_tool_calls': False, 'tool_choice': 'none' if choice == 'none' else
                    {'type': 'function', 'function': {'name': choice}}}
                mode, observed = _observer_policy(rewritten)
                state['policies'][native['api_request_id']] = {'choice': choice, 'tools_sha256': observed,
                    'raw_tools_sha256': digest_json(tools), 'tools_schema': tools, 'projection_mode': mode,
                    'phase': state['phase'], 'messages_sha256': digest_json(rewritten['messages']),
                    'messages_bytes': _json_bytes(rewritten['messages']), 'transport': transport,
                    'payload_sha256': digest_json(rewritten)}
                workflows.put(state)
        except Exception as error:
            raise ReviewIngressBlocked('review transport request blocked before provider') from error
        return {'request': rewritten, 'source': 'rpe_harness', 'reason': 'review_workflow'}

    def validate_request(request, **context):
        _transport_parameters(request)
        native = _identity(context, entry_key, api=True)
        with lock:
            if _configuration()[2] != config_sha:
                raise ValueError('actual review API configuration differs')
            state = workflows.get(native)
            planned = state['policies'].get(native['api_request_id'])
            expected = ('none' if planned and planned['choice'] == 'none' else
                        {'type': 'function', 'function': {'name': 'rpe_propose'}})
            if (not planned or planned.get('raw_receipt_id') or request.get('tool_choice') != expected
                    or request.get('parallel_tool_calls') is not False
                    or digest_json(request.get('tools')) != planned['raw_tools_sha256']
                    or digest_json(request.get('messages')) != planned['messages_sha256']
                    or _json_bytes(request.get('messages')) != planned['messages_bytes']
                    or digest_json(request) != planned['payload_sha256']):
                raise ValueError('actual review API context or policy differs')
            return native, state, planned

    def api_execution(request, next_call, **context):
        try:
            native, state, planned = validate_request(request, **context)
            with lock:
                receipt = store.record('rpe_skill_api_checked', {**native, 'request': {'body': request},
                    'arguments_sha256': planned['tools_sha256'], 'status': planned['projection_mode'],
                    'workflow_phase': planned['phase'], 'request_messages_sha256': planned['messages_sha256'],
                    'request_messages_bytes': planned['messages_bytes'],
                    'request_payload_sha256': planned['payload_sha256']})
                planned['raw_receipt_id'] = receipt['ingest_event_id']
                workflows.put(state)
        except Exception as error:
            raise ReviewIngressBlocked('actual review API ingress validation failed') from error
        return next_call(request)

    def handle(args, task_id=None, session_id=None):
        from .hermes import _CALL_CONTEXT
        native = {**_CALL_CONTEXT.get(), 'entry_key': entry_key, 'tool_name': 'rpe_propose'}
        if native.get('session_id') != session_id or native.get('task_id') != task_id:
            return json.dumps({'status': 'rejected', 'error': {'code': 'entry_context_missing'}})
        try:
            with lock:
                state = workflows.get(native)
                transform = _argument_transform(state['raw_arguments'], args)
                receipt = store.record('rpe_review_arguments_checked', {**native,
                    'receipt_id': state['raw_arguments_receipt_id'], 'arguments_sha256': digest_json(args),
                    'content_sha256': digest_json(state['raw_arguments']), 'status': transform})
                state['arguments_receipt_id'] = receipt['ingest_event_id']
                workflows.put(state)
            result, rpc = _skill_exchange('propose', args, store=store, native=native)
            state = workflows.get(native)
            receipt = store.record('rpe_review_proposed', {**native, 'receipt_id': rpc,
                'state_id': result.get('proposal_id'), 'arguments_sha256': digest_json(args),
                'content_sha256': result.get('proposal_sha256'), 'status': result['status']})
            state['proposal_receipt_id'] = receipt['ingest_event_id']
            workflows.put(state)
            return json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
            state = workflows.get(native)
            state.update(phase='none', terminal_result={'status': 'rejected',
                'error': {'code': 'review_validation_failed'}})
            workflows.put(state)
            return json.dumps(state['terminal_result'])

    def middleware(tool_name, args, next_call, **context):
        from .hermes import _CALL_CONTEXT
        try:
            native = {**_identity(context, entry_key), 'tool_name': tool_name}
            with lock:
                state = workflows.get(native)
                if tool_name != 'rpe_propose' or state['phase'] != 'propose':
                    raise ValueError('review tool phase differs')
                _actual_policy(store, native, tool_name, require_pre=False)
                _raw_policy(store, native, state)
            token = _CALL_CONTEXT.set({key: native[key] for key in IDS})
            try:
                return next_call(args)
            finally:
                _CALL_CONTEXT.reset(token)
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
            return json.dumps({'status': 'rejected', 'error': {'code': 'review_identity_invalid'}})
    ctx.register_tool(name='rpe_propose', toolset='rpe_harness', handler=handle, schema=schema)
    ctx.register_hook('pre_tool_call', capture_arguments)
    ctx.register_middleware('llm_request', policy)
    ctx.register_middleware('llm_execution', api_execution)
    ctx.register_middleware('tool_execution', middleware)
    global _INGRESS_VALIDATOR
    _INGRESS_VALIDATOR = validate_request


def _verify_policy(events, state, config):
    from .skill_gate import _one
    requests = [e for e in events if e['kind'] == 'pre_api_request']
    outcomes = [e for e in events if e['kind'] in ('post_api_request', 'api_request_error')]
    if (not 1 <= len(requests) <= 2 or Counter(e.get('api_request_id') for e in requests) !=
            Counter(e.get('api_request_id') for e in outcomes)
            or any(e['kind'] == 'api_request_error' for e in outcomes)
            or set(state.get('policies', {})) != {e.get('api_request_id') for e in requests}):
        raise ValueError('review_api_observations_incomplete')
    phases, emitted = [], []
    for pre in requests:
        planned = state['policies'][pre['api_request_id']]
        choice = planned['choice']
        phases.append(choice)
        messages = _messages(config, {**state, 'phase': planned['phase']})
        payload = {**planned['transport'], 'messages': messages, 'tools': planned['tools_schema'],
            'parallel_tool_calls': False, 'tool_choice': 'none' if choice == 'none' else
            {'type': 'function', 'function': {'name': 'rpe_propose'}}}
        _transport_parameters(payload)
        if (pre.get('request_tool_choice') != choice or pre.get('request_tool_names') != ['rpe_propose']
                or pre.get('request_parallel_tool_calls') is not False
                or pre.get('request_tools_sha256') != planned['tools_sha256']
                or digest_json(project_observer_tools(planned['tools_schema'], planned['projection_mode'])) != planned['tools_sha256']
                or digest_json(planned['tools_schema']) != planned['raw_tools_sha256']
                or digest_json(messages) != planned['messages_sha256']
                or _json_bytes(messages) != planned['messages_bytes']
                or digest_json(payload) != planned['payload_sha256']):
            raise ValueError('review_actual_policy_or_source_context_differs')
        raw = [e for e in events if e['kind'] == 'rpe_skill_api_checked' and
               all(e.get(k) == pre.get(k) for k in IDS[:-1])]
        if (len(raw) != 1 or raw[0]['ingest_event_id'] != planned['raw_receipt_id']
                or raw[0].get('request_tools_sha256') != planned['raw_tools_sha256']
                or raw[0].get('arguments_sha256') != planned['tools_sha256']
                or raw[0].get('request_tool_choice') != choice
                or raw[0].get('request_tool_names') != ['rpe_propose']
                or raw[0].get('request_parallel_tool_calls') is not False
                or raw[0].get('entry_key') != state['entry_key']
                or raw[0].get('status') != planned['projection_mode']
                or raw[0].get('request_payload_sha256') != planned['payload_sha256']
                or raw[0].get('request_messages_sha256') != planned['messages_sha256']
                or raw[0].get('request_messages_bytes') != planned['messages_bytes']):
            raise ValueError('review_raw_API_proof_missing')
        outcome = next(e for e in outcomes if e.get('api_request_id') == pre['api_request_id'])
        if any(pre.get(k) != outcome.get(k) or not pre.get(k) for k in IDS[:3]):
            raise ValueError('review_api_identity_differs')
        calls = outcome.get('model_tool_calls', [])
        if (choice == 'none' and calls) or (choice != 'none' and
                (len(calls) != 1 or calls[0].get('tool_name') != 'rpe_propose')):
            raise ValueError('review_model_policy_ignored')
        for call in calls:
            native = {**{k: pre[k] for k in IDS[:-1]}, **call}
            for kind in ('pre_tool_call', 'post_tool_call'):
                observed = _one(events, kind, native, 'rpe_propose')
                allowed = {digest_json(state['raw_arguments'])}
                # Sequential CLI emits original post-call args; invoke_tool's
                # registry path emits effective args. Both are receipt-bound.
                if kind == 'post_tool_call':
                    allowed.add(digest_json(state['draft']))
                if observed.get('arguments_sha256') not in allowed:
                    raise ValueError('review_native_arguments_changed')
            emitted.append(native)
    if phases not in (['rpe_propose'], ['rpe_propose', 'none']):
        raise ValueError('review_phase_sequence_differs')
    for kind in ('pre_tool_call', 'post_tool_call'):
        rows = [e for e in events if e['kind'] == kind]
        if len(rows) != 1 or not _same_call(rows[0], emitted[0]) or rows[0].get('tool_name') != 'rpe_propose':
            raise ValueError('review_native_tool_observations_differ')
    return phases.count('none')


def evaluate_review_session(result, db_path, workspace, *, catalog_root=None):
    """Reopen actual host/RPC/source/draft proofs; this never attests human approval."""
    from .hermes_gate import _persisted_events
    from .skill_gate import _state, _one
    from ..proposal import ProposalStore
    verdict = {'status': 'unverified', 'verified': False, 'proposal_verified': False,
        'errors': [], 'native_proposal_calls': 0, 'proposal_executions': 0}
    try:
        if (result.get('completed') is not True or result.get('exit_code') != 0 or result.get('timed_out')
                or result.get('observer_errors', 0)
                or (result.get('process_cleanup') or {}).get('cleanup_complete') is not True):
            raise ValueError('host_or_observation_incomplete')
        events = _persisted_events(db_path, result.get('session_id'))
        if not events or events != result.get('events'):
            raise ValueError('durable_observations_mismatch')
        if any(e['kind'] in ('rpe_task_received', 'rpe_task_returned') or
               e['kind'].startswith('rpe_controller_') for e in events):
            raise ValueError('review_has_execution_receipts')
        state = _state(db_path, result['session_id'])
        if state.get('entry_mode') != 'review' or state['phase'] != 'none':
            raise ValueError('review_terminal_workflow_missing')
        path = Path(state['config_path'])
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != state['config_sha256']:
            raise ValueError('trusted_review_config_changed')
        config = json.loads(path.read_text())
        config['entries'] = _model_catalog_entries(config['entries'])
        verdict['reporting_api_requests'] = _verify_policy(events, state, config)
        transform = _argument_provenance(events, state, state['proposal_native'], state['draft'])
        if config['raw'] != state['source_raw'] or config['source_ref'] != state['source_ref']:
            raise ValueError('review_original_source_changed')
        current = _catalog(db_path, catalog_root, state.get('snapshot_id'))
        if current.snapshot_id != state['catalog_sha256'] or current.snapshot_id != config['catalog_sha256']:
            raise ValueError('review_capability_catalog_changed')
        proposal = ProposalStore(db_path).show(state['proposal_result']['proposal_id'], session_id=result['session_id'])
        if transform != 'identity' and proposal.get('status') == 'awaiting_confirmation':
            raise ValueError('review_coerced_draft_cannot_be_confirmable')
        if (proposal.get('source', {}).get('raw') != config['raw'] or proposal.get('draft') != state['draft']
                or proposal.get('source', {}).get('source_ref') != config['source_ref']
                or proposal.get('workspace') != str(Path(workspace).resolve())
                or proposal.get('catalog_sha256') != current.snapshot_id
                or _proposal_identity(proposal) != state['proposal_result']):
            raise ValueError('review_persisted_proposal_differs')
        proposed = _one(events, 'rpe_review_proposed', state['proposal_native'], 'rpe_propose')
        if (proposed['ingest_event_id'] != state['proposal_receipt_id']
                or proposed.get('arguments_sha256') != digest_json(state['draft'])
                or proposed.get('state_id') != proposal['proposal_id']
                or proposed.get('content_sha256') != proposal['proposal_sha256']
                or proposed.get('status') != proposal['status']):
            raise ValueError('review_proposal_receipt_missing')
        rpc = [e for e in events if e['kind'] == 'rpe_skill_rpc_received' and
               e['ingest_event_id'] == proposed.get('receipt_id') and _same_call(e, proposed)
               and e.get('arguments_sha256') == proposed.get('arguments_sha256')]
        if len(rpc) != 1:
            raise ValueError('review_parent_receipt_missing')
        verdict.update(status='verified_proposal', verified=True, proposal_verified=True,
            arguments_transform=transform,
            post_arguments_phase=('raw_before_coercion' if _one(events, 'post_tool_call',
                state['proposal_native'], 'rpe_propose').get('arguments_sha256') ==
                digest_json(state['raw_arguments']) else 'effective_after_coercion'),
            native_proposal_calls=1, proposal_api_requests=1, proposal_id=proposal['proposal_id'],
            proposal_sha256=proposal['proposal_sha256'], proposal_digest=proposal['proposal_sha256'],
            source_sha256=proposal['source']['source_sha256'], source_ref=config['source_ref'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, PermissionError, sqlite3.Error) as error:
        verdict['errors'].append(str(error) if isinstance(error, ValueError) else type(error).__name__)
    return verdict


def run_hermes_review(raw, workspace, db_path, *, owner_root, timeout=180, hermes_root=None,
                      catalog_root=None, skill_snapshot_id=None, max_turns=3,
                      read_isolation_root=None, deny_read_roots=()):
    """Run the installed host for a draft and return its parent-persisted proposal."""
    from .hermes_session import run_hermes_session
    from .skill_gate import _state
    from ..proposal import ProposalStore
    result = run_hermes_session(raw, workspace, db_path, tool_enabled=True, owner_root=owner_root,
        timeout=timeout, hermes_root=hermes_root, entry_mode='review', public_contract='review',
        catalog_root=catalog_root, skill_snapshot_id=skill_snapshot_id, max_turns=max_turns,
        read_isolation_root=read_isolation_root, deny_read_roots=deny_read_roots)
    result['proposal_verification'] = evaluate_review_session(result, db_path, workspace, catalog_root=catalog_root)
    result['proposal'] = None
    if result['proposal_verification']['proposal_verified']:
        state = _state(db_path, result['session_id'])
        result['proposal'] = ProposalStore(db_path).show(state['proposal_result']['proposal_id'],
                                                       session_id=result['session_id'])
    return result
