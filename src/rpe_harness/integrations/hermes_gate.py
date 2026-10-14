"""Host-independent final acceptance of a real Hermes task-entry chain.

Only compact, persisted observations and the owned durable execution record grant
acceptance. Assistant prose never supplies an outcome or fills missing evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def contract_for(payload, workspace):
    """Normalize execution semantics, excluding host identity and goal prose."""
    from .hermes import _normalize_contract
    return _normalize_contract(payload, workspace)


def request_input_sha(payload, workspace):
    contract = contract_for(payload, workspace)
    source = Path(contract['execution']['input_path'])
    if not source.is_file() or source.is_symlink():
        raise ValueError('input must be a regular owned file')
    if contract['capability'] in ('json.normalize','xlsx.inspect'):
        from ..path_binding import read_source
        limit = 16*1_048_576 if contract['capability']=='xlsx.inspect' else 1_048_576
        return hashlib.sha256(read_source(source, max_bytes=limit)).hexdigest()
    return hashlib.sha256(source.read_bytes()).hexdigest()


def read_run(path, run_id):
    """Read without creating/migrating a database or recovering active runs."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError('durable database missing')
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT r.*,e.payload_json FROM runs r JOIN events e ON e.id=r.event_fk '
                         'WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('durable run missing')
        result = dict(row)
        for key, target in [('payload_json', 'event'), ('verification_json', 'verification'),
                            ('artifact_json', 'artifact'), ('error_json', 'error')]:
            result[target] = json.loads(result.pop(key) or 'null')
        result['steps'] = [{'stage': step['stage'], 'evidence': json.loads(step['evidence_json'])}
                          for step in db.execute('SELECT stage,evidence_json FROM steps WHERE run_id=? '
                                                'ORDER BY id', (run_id,))]
        return result


def _persisted_events(path, session_id):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return []
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as db:
        rows = db.execute('SELECT ingest_event_id,source_event_id,kind,received_at,metadata_json '
                          'FROM hermes_host_events WHERE session_id=? ORDER BY id', (session_id,)).fetchall()
    return [{'ingest_event_id': row[0], 'source_event_id': row[1], 'kind': row[2],
             'received_at': row[3], **json.loads(row[4])} for row in rows]


def _same_call(left, right):
    return all(isinstance(left.get(key), str) and left[key] and left[key] == right.get(key)
               for key in ('session_id', 'task_id', 'turn_id', 'tool_call_id', 'api_request_id'))


def _check_policy(events, errors, admitted_posts):
    requests = [e for e in events if e['kind']=='pre_api_request']
    outcomes = [e for e in events if e['kind'] in ('post_api_request', 'api_request_error')]
    request_ids = Counter(e.get('api_request_id') for e in requests)
    outcome_ids = Counter(e.get('api_request_id') for e in outcomes)
    if (not requests or any(not key for key in request_ids) or request_ids != outcome_ids
            or any(count != 1 for count in request_ids.values())):
        errors.append('api_observations_incomplete')
    if any(e['kind']=='api_request_error' for e in outcomes):
        errors.append('provider_request_error')
    emitted = []
    admitted, last_choices = set(), {}
    for event in events:
        turn = (event.get('session_id'), event.get('turn_id'))
        if event.get('ingest_event_id') in admitted_posts:
            admitted.add(turn)
        if event['kind'] == 'pre_api_request':
            choice = event.get('request_tool_choice')
            if choice != ('none' if turn in admitted else 'rpe_task'):
                errors.append('policy_phase_invalid')
            last_choices[turn] = choice
    if any(choice != 'none' or turn not in admitted for turn, choice in last_choices.items()):
        errors.append('final_policy_missing')
    for request in requests:
        choice = request.get('request_tool_choice')
        names = request.get('request_tool_names')
        if (choice not in ('rpe_task','none') or request.get('request_parallel_tool_calls') is not False
                or names != ['rpe_task'] or not request.get('request_tools_sha256')):
            errors.append('required_policy_missing')
        matching = [e for e in outcomes if e.get('api_request_id')==request.get('api_request_id')]
        if any(any(e.get(key) != request.get(key) or not request.get(key)
                   for key in ('session_id','task_id','turn_id')) for e in matching):
            errors.append('api_pair_mismatch')
        calls = [call for outcome in matching for call in outcome.get('model_tool_calls', [])]
        if choice=='rpe_task' and (len(calls)!=1 or calls[0].get('tool_name')!='rpe_task'):
            errors.append('required_policy_ignored')
        if choice=='none' and calls:
            errors.append('final_policy_ignored')
        emitted.extend({**call, 'api_request_id':request.get('api_request_id'),
                        'turn_id':request.get('turn_id'), 'session_id':request.get('session_id'),
                        'task_id':request.get('task_id')}
                       for call in calls)
    return emitted


def _verify_run(run, receipt, workspace, required_request):
    from ..tasks import plan_from_dict, contract_from_plan, verify_plan
    contract = contract_for(run['event'], workspace)
    contract_sha = digest_json(contract)
    if contract_sha != receipt.get('contract_sha256'):
        raise ValueError('receipt_contract_mismatch')
    if required_request is not None and contract != contract_for(required_request, workspace):
        raise ValueError('required_contract_mismatch')
    source = Path(contract['execution']['input_path'])
    output = Path(contract['execution']['output_path'])
    source_sha = request_input_sha(run['event'], workspace)
    if source_sha != receipt.get('input_sha256'):
        raise ValueError('input_changed')
    entry_key = receipt.get('entry_key')
    if entry_key and run['event'].get('event_id') != 'entry-'+digest_json([entry_key,contract_sha,source_sha]):
        raise ValueError('entry_key_mismatch')
    if run['status']=='rejected':
        if os.path.lexists(output) or not (run.get('error') or {}).get('code'):
            raise ValueError('rejection_artifact_or_reason_invalid')
        return {'status':'rejected', 'verified':False, 'contract_sha256':contract_sha,
                'error':run['error']}
    if run['status']!='succeeded':
        raise ValueError('run_not_verified')
    artifact, verification = run.get('artifact') or {}, run.get('verification') or {}
    plan_evidence = next((s['evidence'] for s in run['steps']
                          if s['stage'] in ('plan','validate') and s['evidence'].get('plan')), None)
    if plan_evidence is None:
        raise ValueError('plan_missing')
    plan = plan_from_dict(plan_evidence['plan'])
    planned = contract_from_plan(plan)
    # The contract permits the workbook's active sheet to be selected implicitly.
    if plan.capability == 'tabular.deduplicate' and contract['execution']['sheet'] is None and plan.input_format == 'xlsx':
        from openpyxl import load_workbook
        book = load_workbook(source, read_only=True, data_only=False, keep_links=False)
        try:
            contract = {**contract,'execution':{**contract['execution'],'sheet':book.active.title}}
        finally:
            book.close()
    if planned != contract or plan.input_sha256 != source_sha:
        raise ValueError('plan_contract_mismatch')
    plan_sha = plan.sha256
    if (plan_sha != plan_evidence.get('plan_sha256') or plan_sha != verification.get('plan_sha256')
            or plan_sha != artifact.get('plan_sha256') or source_sha != verification.get('input_sha256')
            or artifact.get('published') is not True or verification.get('passed') is not True
            or Path(artifact.get('path','')).resolve()!=output or not output.is_file()):
        raise ValueError('plan_or_artifact_proof_invalid')
    if plan.capability in ('json.normalize','xlsx.inspect'):
        from ..path_binding import read_source
        output_data = read_source(output, max_bytes=16*1_048_576)
    else:
        output_data = output.read_bytes()
    current_sha = hashlib.sha256(output_data).hexdigest()
    if current_sha != artifact.get('sha256') or current_sha != verification.get('output_sha256'):
        raise ValueError('artifact_changed')
    prepared = {**plan.verification_parameters(), 'staging_path':str(output)}
    checked = verify_plan(prepared, plan)
    if checked.get('passed') is not True:
        raise ValueError('independent_verification_failed')
    return {'status':'succeeded', 'verified':True, 'contract_sha256':contract_sha,
            'plan_sha256':plan_sha, 'verification':checked, 'artifact':artifact}


def evaluate_session(result, db_path, workspace, required_request=None, require_policy=True):
    """Fail closed on policy, native call linkage, immutable plan and fresh artifact."""
    verdict = {'status':'failed' if not result.get('completed') else 'unverified',
               'verified':False, 'harness_verified':False, 'errors':[]}
    errors = verdict['errors']
    try:
        if (result.get('completed') is not True or result.get('exit_code')!=0 or result.get('timed_out')
                or result.get('observer_errors',0) or
                (result.get('process_cleanup') or {}).get('cleanup_complete') is not True):
            errors.append('host_or_observation_incomplete')
        events = result.get('events') or []
        stored = _persisted_events(db_path, result.get('session_id'))
        if not events or events != stored:
            errors.append('durable_observations_mismatch')
        pres = [e for e in events if e['kind']=='pre_tool_call' and e.get('tool_name')=='rpe_task']
        posts = [e for e in events if e['kind']=='post_tool_call' and e.get('tool_name')=='rpe_task']
        receipts = [e for e in events if e['kind']=='rpe_task_received']
        returns = [e for e in events if e['kind']=='rpe_task_returned']
        if not posts or len(pres)!=len(posts):
            errors.append('tool_observations_incomplete')
        accepted = []
        for post in posts:
            if sum(_same_call(post, pre) for pre in pres)!=1:
                errors.append('tool_pair_mismatch')
                continue
            receipt = next((e for e in receipts if e['ingest_event_id']==post.get('receipt_id')
                            and _same_call(post,e)), None)
            returned = next((e for e in returns if e.get('receipt_id')==post.get('receipt_id')
                             and _same_call(post,e) and e.get('run_id')==post.get('run_id')), None)
            if receipt is None or returned is None:
                errors.append('receipt_run_link_missing')
                continue
            if not post.get('run_id'):
                if returned.get('error_code') not in ('invalid_task_arguments','workspace_violation','request_contract_mismatch',
                                                     'input_changed','task_attempt_limit','entry_context_missing'):
                    errors.append('durable_run_missing')
                continue
            run = read_run(db_path, post['run_id'])
            if returned.get('status')!=run.get('status'):
                errors.append('durable_status_mismatch')
                continue
            if run.get('status') == 'rejected' and returned.get('error_code') != (run.get('error') or {}).get('code'):
                errors.append('durable_rejection_reason_mismatch')
                continue
            accepted.append((_verify_run(run, receipt, workspace, required_request), post, receipt))
        emitted = _check_policy(events, errors, {post['ingest_event_id'] for _,post,_ in accepted}) if require_policy else []
        if require_policy and (len(emitted)!=len(posts) or any(
                sum(_same_call(post, call) for call in emitted)!=1 for post in posts)):
            errors.append('model_tool_link_missing')
        if not accepted:
            errors.append('matching_terminal_run_missing')
        if not errors and accepted:
            proof, post, receipt = accepted[-1]
            verdict.update(proof, run_id=post['run_id'], receipt_id=receipt['ingest_event_id'])
            verdict['harness_verified'] = verdict['verified']
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append(str(exc) if isinstance(exc,ValueError) and str(exc) in {
            'receipt_contract_mismatch','required_contract_mismatch','input_changed','entry_key_mismatch','run_not_verified',
            'plan_missing','plan_contract_mismatch','plan_or_artifact_proof_invalid','artifact_changed',
            'independent_verification_failed','rejection_artifact_or_reason_invalid'} else type(exc).__name__)
    verdict['errors'] = list(dict.fromkeys(errors))
    return verdict
