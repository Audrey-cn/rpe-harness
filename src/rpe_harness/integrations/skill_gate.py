"""Independent final evidence gate for real discovery, native load and RPE execution."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from .hermes_gate import (_persisted_events, _same_call, _verify_run, read_run, digest_json)

IDS = ('session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id')


def _one(events, kind, native, tool=None):
    rows = [e for e in events if e['kind'] == kind and _same_call(e, native) and
            (tool is None or e.get('tool_name') == tool)]
    if len(rows) != 1:
        raise ValueError('native_call_link_missing')
    return rows[0]


def _state(database, session_id):
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute('SELECT state_json,state_sha256 FROM rpe_skill_workflows WHERE session_id=?',
                          (session_id,)).fetchall()
    if len(rows) != 1:
        raise ValueError('workflow_state_missing_or_ambiguous')
    state = json.loads(rows[0][0])
    if digest_json(state) != rows[0][1] or state.get('session_id') != session_id:
        raise ValueError('workflow_state_corrupt')
    return state


def _policy(events, state):
    from .hermes_skills import project_observer_tools
    requests = [e for e in events if e['kind'] == 'pre_api_request']
    outcomes = [e for e in events if e['kind'] in ('post_api_request', 'api_request_error')]
    counts = Counter(e.get('api_request_id') for e in requests)
    if (not requests or None in counts or any(n != 1 for n in counts.values()) or
            counts != Counter(e.get('api_request_id') for e in outcomes) or
            any(e['kind'] == 'api_request_error' for e in outcomes)):
        raise ValueError('api_observations_incomplete')
    emitted = []
    for request in requests:
        planned = state.get('policies', {}).get(request['api_request_id'])
        choice = request.get('request_tool_choice')
        if (not planned or choice != planned['choice'] or
                request.get('request_parallel_tool_calls') is not False or
                request.get('request_tool_names') != (['rpe_task'] if choice == 'none' else [choice]) or
                request.get('request_tools_sha256') != planned['tools_sha256']):
            raise ValueError('actual_policy_differs_from_workflow')
        raw_proofs = [e for e in events if e['kind'] == 'rpe_skill_api_checked' and
                      all(e.get(k) == request.get(k) for k in IDS[:-1])]
        if (len(raw_proofs) != 1 or raw_proofs[0]['ingest_event_id'] != planned.get('raw_receipt_id') or
                raw_proofs[0].get('request_tools_sha256') != digest_json(planned['tools_schema']) or
                raw_proofs[0].get('request_tools_sha256') != planned['raw_tools_sha256'] or
                raw_proofs[0].get('arguments_sha256') != planned['tools_sha256'] or
                raw_proofs[0].get('request_tool_choice') != choice or
                raw_proofs[0].get('request_parallel_tool_calls') is not False or
                raw_proofs[0].get('request_tool_names') != (['rpe_task'] if choice == 'none' else [choice]) or
                raw_proofs[0].get('entry_key') != state['entry_key'] or
                raw_proofs[0].get('status') != planned['projection_mode'] or
                digest_json(project_observer_tools(planned['tools_schema'], planned['projection_mode'])) != planned['tools_sha256']):
            raise ValueError('actual_raw_schema_or_observer_projection_invalid')
        outcome = next(e for e in outcomes if e.get('api_request_id') == request['api_request_id'])
        if any(not request.get(k) or request[k] != outcome.get(k) for k in IDS[:3]):
            raise ValueError('api_identity_mismatch')
        calls = outcome.get('model_tool_calls', [])
        if (choice == 'none' and calls) or (choice != 'none' and
                (len(calls) != 1 or calls[0].get('tool_name') != choice or not calls[0].get('tool_call_id'))):
            raise ValueError('actual_model_policy_ignored')
        for call in calls:
            native = {**{k: request[k] for k in IDS[:-1]}, **call}
            _one(events, 'pre_tool_call', native, call['tool_name'])
            _one(events, 'post_tool_call', native, call['tool_name'])
            emitted.append(native)
    posts = [e for e in events if e['kind'] == 'post_tool_call']
    pres = [e for e in events if e['kind'] == 'pre_tool_call']
    if len(pres) != len(posts) or len(posts) != len(emitted) or any(sum(_same_call(post, call) for call in emitted) != 1 for post in posts):
        raise ValueError('model_tool_observations_mismatch')
    if requests[-1].get('request_tool_choice') != 'none' or state.get('phase') != 'none':
        raise ValueError('final_policy_missing')


def evaluate_skill_session(result, db_path, workspace, *, catalog_root, required_request=None):
    """Reopen persisted identities, sources, context and artifacts; prose grants nothing."""
    verdict = {'status': 'unverified', 'verified': False, 'harness_verified': False, 'errors': []}
    try:
        if (result.get('completed') is not True or result.get('exit_code') != 0 or result.get('timed_out') or
                result.get('observer_errors', 0) or
                (result.get('process_cleanup') or {}).get('cleanup_complete') is not True):
            raise ValueError('host_or_observation_incomplete')
        events = _persisted_events(db_path, result.get('session_id'))
        if not events or result.get('events') != events:
            raise ValueError('durable_observations_mismatch')
        state = _state(db_path, result['session_id'])
        _policy(events, state)
        from ..skill_catalog import SkillCatalog
        from ..context import ContextStore
        catalog = SkillCatalog(db_path, catalog_root)
        selected = state.get('selected')
        if not selected:
            raise ValueError('matching_adapted_skill_missing')
        row = catalog.lookup(selected['skill_id'], snapshot_id=state['snapshot_id'])
        if (row.get('adapter_status') != 'trusted' or row.get('executable') is not True or
                state['capability'] not in row['capabilities'] or
                any(selected.get(k) != row.get(k) for k in
                    ('snapshot_id', 'skill_id', 'skill_sha256', 'script_sha256', 'load_name'))):
            raise ValueError('skill_source_or_adapter_changed')
        discovered = _one(events, 'rpe_skill_discovered', state['discover_native'], 'rpe_discover')
        loaded = _one(events, 'rpe_skill_loaded', state['load_native'], 'skill_view')
        if (discovered.get('status') != 'discovered' or loaded.get('status') != 'loaded' or
                loaded['ingest_event_id'] != state.get('load_receipt_id') or
                loaded.get('arguments_sha256') != digest_json({'name': selected['load_name']}) or
                any(loaded.get(k) != row.get(k) or discovered.get(k) != row.get(k) for k in
                    ('snapshot_id', 'skill_id', 'skill_sha256', 'script_sha256'))):
            raise ValueError('discovery_or_load_receipt_mismatch')
        session_root = Path(result['session_root']).resolve()
        from .hermes_skills import _read_owned_skill
        materialized_root = Path(state['materialized_root'])
        path = materialized_root / row['skill_id'] / 'SKILL.md'
        if (not Path(state['config_path']).resolve().is_relative_to(session_root) or
                not materialized_root.resolve().is_relative_to(session_root) or
                str(path) != state['materialized_path'] or
                hashlib.sha256(_read_owned_skill(path, materialized_root)).hexdigest() != row['skill_sha256'] or
                state['loaded_content_sha256'] != row['skill_sha256']):
            raise ValueError('isolated_native_load_source_changed')
        for receipt in (e for e in events if e['kind'] == 'rpe_skill_rpc_received'):
            candidates = [item for item in state.get('rpc_requests', {}).values() if
                          _same_call(item['native'], receipt) and item['native'].get('tool_name') == receipt.get('tool_name') and
                          digest_json(item['arguments']) == receipt.get('arguments_sha256')]
            if len(candidates) != 1 or receipt.get('entry_key') != state['entry_key']:
                raise ValueError('parent_rpc_request_link_missing')
        for item in state.get('rpc_requests', {}).values():
            matched = [e for e in events if e['kind'] == 'rpe_skill_rpc_received' and
                       _same_call(item['native'], e) and e.get('tool_name') == item['native']['tool_name'] and
                       e.get('arguments_sha256') == digest_json(item['arguments'])]
            if len(matched) != 1:
                raise ValueError('parent_rpc_receipt_duplicated_or_missing')
        for special in (discovered, loaded):
            rpc = next((e for e in events if e['kind'] == 'rpe_skill_rpc_received' and
                        e['ingest_event_id'] == special.get('receipt_id')), None)
            if rpc is None or not _same_call(rpc, special):
                raise ValueError('parent_rpc_receipt_link_missing')
        context = _one(events, 'rpe_context_recovered', state['context_native'], 'rpe_context')
        if context.get('load_receipt_id') != state['load_receipt_id']:
            raise ValueError('context_load_link_missing')
        context_rpc = next((e for e in events if e['kind'] == 'rpe_skill_rpc_received' and
                            e['ingest_event_id'] == context.get('receipt_id')), None)
        if context_rpc is None or not _same_call(context_rpc, context) or context_rpc.get('tool_name') != 'rpe_context':
            raise ValueError('context_parent_rpc_link_missing')
        contexts = ContextStore(db_path)
        original_context = contexts.get_recovery(context['recovery_id'])
        fresh = contexts.revalidate_recovery(state['recovery_id'], session_id=result['session_id'])
        if (original_context['session_id'] != result['session_id'] or
                original_context['capability'] != state['capability'] or
                fresh['request_sha256'] != original_context['request_sha256']):
            raise ValueError('fresh_context_not_descended_from_native_context')
        for item in state.get('rpc_requests', {}).values():
            if item['operation'] == 'revalidate':
                base = contexts.get_recovery(item['arguments']['recovery_id'])
                if base['request_sha256'] != original_context['request_sha256']:
                    raise ValueError('task_revalidation_context_link_missing')
        if not state.get('task_result'):
            if fresh['status'] != 'needs_context' or context.get('context_status') != 'needs_context':
                raise ValueError('matching_terminal_task_missing')
            verdict.update(status='accepted_rejection', recovery_id=fresh['recovery_id'],
                           rejection_reason='needs_context', missing=fresh['missing'])
            return verdict
        if fresh['status'] != 'ready':
            raise ValueError('context_no_longer_ready')
        task = state['task_result']
        task_context = _one(events, 'rpe_context_recovered', state['task_native'], 'rpe_task')
        if (task_context.get('recovery_id') != state['recovery_id'] or task_context.get('context_status') != 'ready' or
                task_context.get('load_receipt_id') != state['load_receipt_id']):
            raise ValueError('task_fresh_context_receipt_missing')
        post = _one(events, 'post_tool_call', state['task_native'], 'rpe_task')
        receipt = _one(events, 'rpe_task_received', state['task_native'], 'rpe_task')
        returned = _one(events, 'rpe_task_returned', state['task_native'], 'rpe_task')
        if (post.get('receipt_id') != receipt['ingest_event_id'] or returned.get('receipt_id') != receipt['ingest_event_id'] or
                task.get('receipt_id') != receipt['ingest_event_id'] or
                any(item.get('run_id') != task['run_id'] for item in (post, returned)) or
                receipt.get('entry_key') != state['entry_key']):
            raise ValueError('task_receipt_run_link_missing')
        run = read_run(db_path, task['run_id'])
        if run['event'].get('capability') != state['capability'] or returned.get('status') != run['status']:
            raise ValueError('task_capability_or_status_mismatch')
        for key, value in fresh['facts'].items():
            if digest_json(run['event']['execution'].get(key)) != digest_json(value):
                raise ValueError('execution_differs_from_recovered_facts')
        if state['capability'] == 'xlsx.inspect' and run['event']['execution'].get('skill_binding') != row['skill_binding']:
            raise ValueError('helper_execution_binding_mismatch')
        proof = _verify_run(run, receipt, Path(workspace).resolve(), required_request)
        verdict.update(proof, run_id=run['run_id'], recovery_id=fresh['recovery_id'],
                       skill_id=row['skill_id'], snapshot_id=row['snapshot_id'],
                       execution_kind='native_helper' if state['capability'] == 'xlsx.inspect' else 'rpe_contract')
        verdict['harness_verified'] = verdict['verified']
        verdict['cold_recovery_verified'] = row['temperature'] == 'cold' and verdict['verified']
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as error:
        verdict['errors'].append(str(error) if isinstance(error, ValueError) else type(error).__name__)
    return verdict
