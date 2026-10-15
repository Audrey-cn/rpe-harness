"""Bounded NL draft, simulated review and execution comparison.

Registration, summary, public export and offline verification never call a model.
Only ``run_review_comparison`` invokes Hermes. A fixture reviewer approves the
exact submitted candidate or rejects it; it never repairs or supplies a gold
request. This experiment does not demonstrate real human confirmation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import csv
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

from .address_comparison import (
    MAX_ARTIFACT_BYTES, _environment, _export_bytes, _fixtures, _json,
    _native_calls, _observed_identity, _public_business_correct, _read,
    _safe_path, _save, _sha, _trial, _unique_pairs, evaluate_native_address_session,
    score_address_artifact,
)
from .cost_ledger import PhaseLedger, summarize_phases
from .skill_comparison import exact_json
from ..path_binding import read_regular_file
from ..skill_catalog import SkillCatalog, XLSX_SKILL_ID
from ..storage import canonical_json

_MARKER = '.rpe-review-comparison-owned.json'
_ARMS = ('native', 'review')
_REVIEWER = 'simulated_fixture_reviewer'
_PHASES = ('draft', 'review', 'execute', 'verification')


def _contract():
    from ..integrations.hermes_review import REVIEW_CONTRACT
    return REVIEW_CONTRACT


def _execution_contract(helper_python):
    from ..integrations.hermes_clean import CLEAN_SOURCE_CONTRACT
    return CLEAN_SOURCE_CONTRACT.replace('{helper_python}', helper_python)


def preregister_review_comparison(root, *, skill_root, hermes_root=None,
                                  helper_python=None, timeout=120, max_turns=8):
    """Freeze six public NL pairs and one pilot pair, without model calls."""
    started = time.monotonic()
    if type(timeout) is not int or not 1 <= timeout <= 180:
        raise ValueError('timeout must be an integer between 1 and 180')
    if type(max_turns) is not int or not 1 <= max_turns <= 8:
        raise ValueError('max_turns must be an integer between 1 and 8')
    root, skill_root = _safe_path(root), _safe_path(skill_root)
    hermes_root = _safe_path(hermes_root or Path.home()/'.hermes/hermes-agent')
    helper_python = str(Path(helper_python or sys.executable).expanduser().absolute())
    if helper_python != sys.executable:
        raise ValueError('review comparison helper must be the launcher interpreter')
    arguments = dict(root=str(root), skill_root=str(skill_root), hermes_root=str(hermes_root),
                     helper_python=helper_python, timeout=timeout, max_turns=max_turns)
    if (root/'preregistration.json').exists():
        plan = _load(root)
        if any(plan.get(k)!=v for k,v in arguments.items()):
            raise ValueError('frozen arguments differ; register a new root')
        _check_freeze(plan)
        return plan
    if root.exists() and any(root.iterdir()):
        raise ValueError('comparison root must be empty or already owned')
    if root in {Path(root.anchor), Path.home(), skill_root} or root in skill_root.parents:
        raise ValueError('comparison requires a dedicated owned root')
    root.mkdir(parents=True, exist_ok=True)
    _save(root/_MARKER, {'schema_version':'rpe.review-comparison-owner/v1','root':str(root)})
    for name in ('fixtures','records','workspaces','sessions','catalog','databases','drivers'):
        (root/name).mkdir()
    ledger = PhaseLedger(root/'ledger')
    ledger.reserve('shared', 'preregistration', 'shared-preregistration', api_expected=False)
    database = root/'runs.sqlite'
    catalog = SkillCatalog(database, root/'catalog')
    snapshot = catalog.sync(skill_root)['snapshot_id']
    binding = catalog.trust_adapter(XLSX_SKILL_ID, snapshot)
    skill = catalog.lookup(XLSX_SKILL_ID, snapshot)
    cases = _fixtures(root/'fixtures')
    # This probe adds a real unsupported requirement to an otherwise supported
    # operation. Silently dropping the sum must never count as a correct draft.
    cases[-1]['task'] = ('请按 input.csv 的地区保留首次出现的整行去重，保持原顺序，'
        'UTF-8逗号CSV另存 output.csv，保留原文件。还必须把每个地区的金额求和后写入结果；'
        '本次只授权保留首条去重、JSON空白规范化和XLSX只读检查，不可新增求和执行器。')
    cases[-1].update(capability='tabular.deduplicate',
        constraints=['preserve_original','preserve_order'],
        parameters={'key_columns':['地区'],'delimiter':',','encoding':'utf-8','keep':'first'},
        blocked_terms=['求和','合计','sum','aggregate'])
    cases[-2]['parameters'] = {'delimiter':',','encoding':'utf-8','keep':'first'}
    sessions, pilots = [], []
    for index, case in enumerate(cases):
        for arm in _ARMS if index%2==0 else tuple(reversed(_ARMS)):
            sessions.append(_new_trial(root, case, arm, 'formal', helper_python, binding))
    for arm in _ARMS:
        pilots.append(_new_trial(root, cases[0], arm, 'pilot', helper_python, binding))
    for trial in sessions+pilots:
        trial['db'] = str(root/'databases'/(trial['trial_id']+'.sqlite'))
        with sqlite3.connect(database) as source, sqlite3.connect(trial['db']) as target:
            source.backup(target)
    contract = _contract()
    plan = {**arguments, 'schema_version':'rpe.review-comparison/v1', 'profile':'review',
        'db':str(database), 'catalog_root':str(root/'catalog'), 'skill_snapshot_id':snapshot,
        'skill_sha256':skill['skill_sha256'], 'script_sha256':skill['script_sha256'],
        'environment':_environment(helper_python, hermes_root), 'cases':cases,
        'sessions':sessions, 'pilots':pilots, 'formal_session_limit':12,
        'pilot_session_limit':2, 'max_draft_sessions':14, 'max_native_execution_sessions':7,
        'max_host_sessions':21, 'max_task_attempts':1,
        'public_contract':contract, 'public_contract_sha256':_sha(contract.encode()),
        'execution_public_contract_sha256':_sha(_execution_contract(helper_python).encode()),
        'native_execution_source':'exact approved candidate serialized as strict three-field JSON; not user-authored JSON or controller truth',
        'reviewer_kind':_REVIEWER, 'human_confirmation_verified':False,
        'comparison_normalization':'constraint flags and composite-key columns require unique same-membership values; only their order is ignored for scoring; candidates and approvals are never rewritten',
        'failure_policy':'one candidate; accept or reject only; no repair, replay, replacement or gold request to a model',
        'cost_policy':'all observed phases including setup, pilots, failures and verification; missing usage remains unknown; no billing estimate',
        'coverage_limits':[
            'four fixed NL production tasks and two directed probes; not a population estimate',
            'review is a deterministic fixture simulation, never proof of human understanding or confirmation',
            'same original NL, files, public draft schema, model configuration, interpreter and installed skill snapshot',
            'native uses its own tools for drafting and execution; review uses one proposal tool then approved deterministic execution',
            'compare whole strategies; do not attribute differences to one module',
            'up to 14 draft sessions and 7 native execution sessions; each has the frozen timeout and at most 8 turns',
            'whole driver time includes freeze/orchestration; full trial timers include all gates and verification; phase elapsed sums are narrower and never added to these overlapping timers',
            'preregistration has a shared setup phase; development, tests, export and receipt writes are outside operational timers',
            'human review time and billing cost are unknown; simulated local review time is measured separately',
            'runtime identities are recorded checks, not independent third-party cryptographic attestations',
            'prior 1.8 and 1.9 observations are unchanged and are not merged or rescored']}
    ledger.finish('shared-preregistration', {'status':'completed',
        'elapsed_seconds':time.monotonic()-started})
    return _save(root/'preregistration.json', plan)


def _new_trial(root, case, arm, phase, helper_python, binding):
    trial = _trial(root, case, arm, phase, helper_python, binding)
    # Both arms get only the literal NL source. The common schema/legend is
    # delivered independently through the host's public_contract='review'.
    (Path(trial['workspace'])/'prompt.txt').write_text(case['task'], encoding='utf-8')
    trial['prompt_sha256'] = _sha(case['task'].encode())
    return trial


def _load(root):
    root = _safe_path(root)
    if _read(root/_MARKER).get('root')!=str(root):
        raise ValueError('owned root marker differs')
    plan = _read(root/'preregistration.json')
    if plan.get('profile')!='review' or plan['root']!=str(root):
        raise ValueError('review command requires a review registration')
    return plan


def _check_freeze(plan):
    if _environment(plan['helper_python'], plan['hermes_root'])!=plan['environment']:
        raise ValueError('frozen source, host configuration or dependencies changed')
    if _sha(_contract().encode())!=plan['public_contract_sha256']:
        raise ValueError('frozen public review contract changed')
    if _sha(_execution_contract(plan['helper_python']).encode())!=plan['execution_public_contract_sha256']:
        raise ValueError('frozen public execution contract changed')
    for case in plan['cases']:
        if _sha(read_regular_file(_safe_path(case['source_path']), max_bytes=MAX_ARTIFACT_BYTES))!=case['input_sha256']:
            raise ValueError('frozen public input changed')
    for trial in plan['sessions']+plan['pilots']:
        workspace = _safe_path(trial['workspace'])
        if not workspace.is_relative_to(plan['root']):
            raise ValueError('frozen workspace escaped owned root')
        if _sha(read_regular_file(workspace/'prompt.txt', max_bytes=65536))!=trial['prompt_sha256']:
            raise ValueError('frozen original NL changed')
        row = SkillCatalog(trial['db'], plan['catalog_root']).lookup(XLSX_SKILL_ID, plan['skill_snapshot_id'])
        if not row['executable'] or row['skill_sha256']!=plan['skill_sha256'] or row['script_sha256']!=plan['script_sha256']:
            raise ValueError('frozen skill snapshot or trust changed')


def _denied(plan, trial):
    from .address_comparison import _denied as old_denied
    return old_denied(plan, trial)+[Path(__file__).resolve()]


def _local_phase(ledger, trial, phase, function):
    attempt = trial['trial_id']+'-'+phase
    ledger.reserve(trial['arm'], phase, attempt, api_expected=False)
    started = time.monotonic()
    try:
        value = function()
    except Exception as error:
        ledger.finish(attempt, {'status':'failed', 'elapsed_seconds':time.monotonic()-started,
                               'error_code':type(error).__name__})
        raise
    ledger.finish(attempt, {'status':'completed', 'elapsed_seconds':time.monotonic()-started})
    return value


def _session_phase(plan, trial, ledger, runner, *, execution=False, prompt=None):
    from ..integrations.hermes_session import SessionObservationError
    phase = 'execute' if execution else 'draft'
    attempt = trial['trial_id']+'-'+phase
    ledger.reserve(trial['arm'], phase, attempt)
    started = time.monotonic()
    result, error_code = {}, None
    kwargs = dict(owner_root=Path(plan['root'])/'sessions'/attempt, timeout=plan['timeout'],
        hermes_root=plan['hermes_root'], catalog_root=plan['catalog_root'],
        skill_snapshot_id=plan['skill_snapshot_id'], max_turns=plan['max_turns'],
        read_isolation_root=Path(plan['root']), deny_read_roots=_denied(plan, trial))
    if trial['arm']=='native':
        kwargs.update(tool_enabled=False, preserve_native_tool_search=True, max_task_attempts=1,
                      public_contract='clean' if execution else 'review')
    try:
        result = runner(prompt if prompt is not None else (Path(trial['workspace'])/'prompt.txt').read_text(),
                        Path(trial['workspace']), trial['db'], **kwargs)
        if not isinstance(result, dict):
            raise TypeError('session result must be structured')
    except SessionObservationError as error:
        result, error_code = error.diagnostics, 'SessionObservationError'
    except Exception as error:
        error_code = type(error).__name__
    ledger.finish(attempt, {'status':('timed_out' if result.get('timed_out') else
        'failed' if error_code or not _host_ok(result) else 'completed'),
        'elapsed_seconds':time.monotonic()-started, 'usage':result.get('usage'),
        'model_identity':_public_identity(result.get('model_identity')),
        **({'error_code':error_code} if error_code else {})})
    return result, error_code


def _host_ok(result):
    return bool(result.get('completed') is True and result.get('exit_code')==0
        and not result.get('timed_out') and not result.get('observer_errors')
        and (result.get('process_cleanup') or {}).get('cleanup_complete') is True)


def _public_identity(identity):
    return ({key:identity[key] for key in ('provider','requested_model','response_models') if key in identity}
            if isinstance(identity, dict) else None)


def _observe(plan, trial, ledger, runner, review_runner):
    """One candidate, one simulated review, then at most one approved execution."""
    started = time.monotonic()
    record = {key:trial[key] for key in ('trial_id','phase','arm','case_id','category')}
    record.update(fingerprint=plan['sha256'], status='failed', approved=False,
        draft_faithful=None, draft_invocation_verified=False, business_correct=False,
        product_oracle_passed=False, execution_verified=False, accepted=False,
        reviewer_kind=_REVIEWER, human_confirmation_verified=False)
    result, error_code = _session_phase(plan, trial, ledger,
        runner if trial['arm']=='native' else review_runner)
    record.update(session_id=result.get('session_id'), model_identity=_public_identity(result.get('model_identity')),
                  source_sha256=trial['prompt_sha256'])
    if error_code:
        record['error_code'] = error_code
    if not error_code:
        try:
            _review_and_execute(plan, trial, ledger, result, record, runner)
        except Exception as error:
            record['error_code'] = type(error).__name__
    def verify():
        source = read_regular_file(Path(trial['workspace'])/trial['input_name'], max_bytes=MAX_ARTIFACT_BYTES)
        record['source_preserved'] = _sha(source)==trial['input_sha256']
        if trial['category']=='production':
            record.update(score_address_artifact(plan, trial))
        if not record['approved']:
            record['no_business_output'] = not any(p.name.startswith('output') for p in Path(trial['workspace']).iterdir())
        record['accepted'] = bool(record.get('comparable') and record['draft_invocation_verified']
            and record['source_preserved'] and (record.get('probe_correct') and record.get('no_business_output') if trial['category']=='contract_probe'
                else record['approved'] and record['business_correct'] and record['product_oracle_passed']
                and record['execution_verified']))
        record['status'] = 'completed' if record['accepted'] else 'failed'
    try:
        _local_phase(ledger, trial, 'verification', verify)
    except Exception as error:
        record.update(status='failed', accepted=False, error_code=type(error).__name__)
    record['full_trial_elapsed_seconds'] = time.monotonic()-started
    return record


def _review_and_execute(plan, trial, ledger, result, record, runner):
    from ..proposal import ProposalStore, create_proposal
    case = next(c for c in plan['cases'] if c['case_id']==trial['case_id'])
    workspace = Path(trial['workspace'])
    record['comparable'] = bool(_host_ok(result)
        and result.get('original_prompt_sha256')==trial['prompt_sha256']
        and result.get('prompt_sha256')==trial['prompt_sha256']
        and result.get('catalog_snapshot_id')==plan['skill_snapshot_id']
        and result.get('public_contract_sha256')==plan['public_contract_sha256']
        and _observed_identity(record['model_identity']))
    if trial['arm']=='review':
        from ..integrations.hermes_review import evaluate_review_session
        gate = evaluate_review_session(result, trial['db'], workspace, catalog_root=plan['catalog_root'])
        record['draft_invocation_verified'] = bool(gate.get('verified') and gate.get('proposal_verified'))
        record['draft_gate'] = _compact_draft_gate(gate)
        proposal_id = gate.get('proposal_id')
        if not record['draft_invocation_verified'] or not isinstance(proposal_id, str):
            return
        proposal = ProposalStore(trial['db']).show(proposal_id, session_id=result['session_id'])
    else:
        # Verify a real native publication of these draft bytes, separately from
        # the later business execution (XLSX loading is required only then).
        gate = evaluate_native_review_draft(result, plan, trial)
        record['draft_invocation_verified'] = gate.get('verified') is True
        record['draft_gate'] = _compact_draft_gate(gate)
        if not record['draft_invocation_verified']:
            return
        draft = json.loads(read_regular_file(workspace/'draft.json', max_bytes=65536),
                           object_pairs_hook=_unique_pairs)
        proposal = create_proposal(case['task'], draft, db_path=trial['db'], workspace=workspace,
            session_id=result['session_id'], source_ref='user_prompt:'+trial['prompt_sha256'],
            catalog_root=plan['catalog_root'], skill_snapshot_id=plan['skill_snapshot_id'])
    # A finished business file before approval is a workflow violation even if
    # the final bytes happen to be correct. No approval is issued in that case.
    premature = any(p.name.startswith('output') for p in workspace.iterdir())
    record['no_business_output_before_approval'] = not premature
    if premature or not record['comparable'] or not record['draft_invocation_verified']:
        return
    review = _local_phase(ledger, trial, 'review', lambda:review_fixture_proposal(plan, trial, proposal))
    record.update(review)
    if not record['approved']:
        return
    store = ProposalStore(trial['db'])
    confirmation = review['approval']
    # Revalidation happens before either execution strategy. The model receives
    # only the exact accepted candidate, never controller truth or a repaired one.
    resolved = store.resolve(confirmation['confirmation_id'], session_id=result['session_id'],
                             catalog_sha256=proposal['catalog_sha256'])
    if trial['arm']=='review':
        execution = _local_phase(ledger, trial, 'execute', lambda:store.execute(
            confirmation['confirmation_id'], session_id=result['session_id'], workspace=workspace,
            catalog_root=plan['catalog_root'], skill_snapshot_id=plan['skill_snapshot_id'],
            timeout=min(120, plan['timeout']), python=plan['helper_python']))
        from ..integrations.hermes_gate import read_run
        from ..tasks import contract_from_plan, plan_from_dict
        durable = read_run(trial['db'], execution.get('run_id'))
        event = durable['event'].get('request', durable['event'])
        expected_event = {**resolved['request'], 'event_id':'review-'+confirmation['confirmation_id']}
        plans = [step['evidence']['plan'] for step in durable['steps']
                 if step['stage']=='plan' and isinstance(step.get('evidence'), dict) and 'plan' in step['evidence']]
        record['execution_verified'] = bool(execution.get('status')=='succeeded'
            and execution.get('confirmation_id')==confirmation['confirmation_id']
            and execution.get('proposal_id')==proposal['proposal_id']
            and (execution.get('worker_cleanup') or {}).get('cleanup_complete') is True
            and durable['status']=='succeeded'
            and exact_json(event, expected_event) and len(plans)==1
            and contract_from_plan(plan_from_dict(plans[0]))==proposal['machine_contract'])
        record['execution_kind'] = 'approved_controller'
        record['execution_run_id'] = execution.get('run_id')
    else:
        candidate = {key:copy.deepcopy(resolved['draft'][key]) for key in ('capability','constraints','execution')}
        execution_prompt = _json(candidate)
        executed, error_code = _session_phase(plan, trial, ledger, runner,
                                              execution=True, prompt=execution_prompt)
        native_gate = evaluate_native_address_session(executed, plan, trial)
        record['execution_model_identity'] = _public_identity(executed.get('model_identity'))
        record['execution_public_contract_sha256'] = executed.get('public_contract_sha256')
        record['execution_verified'] = bool(not error_code and _host_ok(executed)
            and executed.get('original_prompt_sha256')==_sha(execution_prompt.encode())
            and executed.get('prompt_sha256')==_sha(execution_prompt.encode())
            and executed.get('catalog_snapshot_id')==plan['skill_snapshot_id']
            and executed.get('public_contract_sha256')==plan['execution_public_contract_sha256']
            and record['execution_model_identity']==record['model_identity']
            and native_gate.get('verified') is True)
        record['execution_kind'] = 'approved_native'


def _compact_draft_gate(gate):
    def code(value):
        return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) else 'redacted_diagnostic'
    return {'verified':gate.get('verified') is True, 'status':code(gate['status']) if gate.get('status') else None,
        'proof_kind':code(gate['proof_kind']) if gate.get('proof_kind') else None,
        'error_count':len(gate.get('errors') or []),
        'error_codes':[code(value) for value in (gate.get('errors') or [])[:16]]}


def evaluate_native_review_draft(result, plan, trial):
    """Review-only native publication proof for ID-less official SDK streams.

    IDs remain mandatory in durable API/tool observations. The supplemental
    proof names a unique write signature, never invents a stdout tool-call ID.
    The 1.8/1.9 native gate and business-execution gates are unchanged.
    """
    from ..integrations.hermes_gate import digest_json
    case = next(c for c in plan['cases'] if c['case_id']==trial['case_id'])
    draft_case = {**case,'capability':'review.draft','expected_status':'ready','output_name':'draft.json'}
    legacy = evaluate_native_address_session(result, {**plan,'cases':[draft_case]}, trial)
    if legacy.get('verified') is True:
        return {**legacy, 'proof_kind':('native_write_id' if result.get('file_write_observations')
                                       else 'native_terminal_command')}
    if legacy.get('errors')!=['native_output_invocation_missing']:
        return legacy
    verdict = {'verified':False,'status':'unverified','errors':[]}
    try:
        posts, streams = _native_calls(result, trial['db'])
        session = _safe_path(result['session_root'])
        owner = _safe_path(Path(plan['root'])/'sessions'/(trial['trial_id']+'-draft'))
        if not session.is_relative_to(owner) or session==owner:
            raise ValueError('native_write_log_outside_owned_session')
        raw = read_regular_file(session/'stdout.jsonl', max_bytes=MAX_ARTIFACT_BYTES)
        records = [json.loads(line,object_pairs_hook=_unique_pairs) for line in raw.decode('utf-8').splitlines()]
        uses = [row for row in records if type(row) is dict and row.get('type')=='tool_use']
        signature = lambda row:(row.get('name'), digest_json(row.get('input')))
        if Counter(map(signature, uses))!=Counter(map(signature, streams)):
            raise ValueError('native_write_log_stream_changed')
        workspace, target = _safe_path(trial['workspace']), _safe_path(Path(trial['workspace'])/'draft.json')
        def target_write(row):
            args = row.get('input')
            if row.get('name')!='write_file' or type(args) is not dict or not isinstance(args.get('path'),str):
                return False
            path = Path(args['path'])
            return _safe_path(path if path.is_absolute() else workspace/path)==target
        matches = [row for row in uses if target_write(row)]
        if len(matches)!=1:
            raise ValueError('native_write_signature_ambiguous')
        selected = matches[0]
        args = selected['input']
        final = read_regular_file(target, max_bytes=65536)
        if not isinstance(args.get('content'),str) or args['content'].encode()!=final:
            raise ValueError('native_write_final_bytes_changed')
        digest = digest_json(args)
        durable = [post for post in posts if post.get('tool_name')=='write_file'
                   and post.get('arguments_sha256')==digest and post.get('status') in {'ok','succeeded'}]
        if len(durable)!=1:
            raise ValueError('native_write_durable_signature_ambiguous')
        # Official streams omit IDs. Pair only a single outstanding write at a
        # time; concurrent, duplicate, unmatched or partial records are refused.
        pending, verified_pairs = None, []
        for index, row in enumerate(records):
            if type(row) is not dict or row.get('name')!='write_file':
                continue
            if row.get('type')=='tool_use':
                if pending is not None:
                    raise ValueError('native_write_result_ambiguous')
                pending = (index, row)
            elif row.get('type')=='tool_result':
                if pending is None:
                    raise ValueError('native_write_result_ambiguous')
                use_index, use = pending
                pending = None
                if use is not selected:
                    continue
                if use.get('tool_call_id') is not None or row.get('tool_call_id') is not None:
                    raise ValueError('native_write_partial_id_unsupported')
                output = json.loads(row.get('output',''), object_pairs_hook=_unique_pairs)
                if (row.get('is_error') is not False or type(output) is not dict
                    or output.get('verified') is not True or output.get('error')
                    or type(output.get('bytes_written')) is not int or output['bytes_written']!=len(final)
                    or output.get('resolved_path')!=str(target)):
                    raise ValueError('native_write_result_target_or_bytes_changed')
                verified_pairs.append((use_index,index))
        if pending is not None or len(verified_pairs)!=1:
            raise ValueError('native_write_result_ambiguous')
        verdict.update(verified=True,status='verified_native_draft',proof_kind='unique_native_write_signature',
            arguments_sha256=digest,content_sha256=_sha(final),target_path_sha256=_sha(str(target).encode()),
            stdout_record_indices=list(verified_pairs[0]))
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,RecursionError,sqlite3.Error) as error:
        verdict['errors'].append(str(error) if isinstance(error,ValueError) else type(error).__name__)
    return verdict


def _expected_candidate(case):
    return {'capability':case['capability'], 'constraints':case['constraints'],
        'execution':{'input_path':case['input_name'], 'output_path':case['output_name'],
                     **case.get('parameters', {})}}


def _candidate_matches(candidate, expected):
    def comparison(value):
        normalized = copy.deepcopy(value)
        flags = normalized.get('constraints')
        if type(flags) is not list or any(type(flag) is not str for flag in flags) or len(set(flags))!=len(flags):
            return None
        normalized['constraints'] = sorted(flags)
        execution = normalized.get('execution')
        if type(execution) is not dict:
            return None
        if 'key_columns' in execution:
            keys = execution['key_columns']
            if (type(keys) is not list or any(type(key) is not str for key in keys)
                    or len(set(keys))!=len(keys)):
                return None
            execution['key_columns'] = sorted(keys)
        return normalized
    left, right = comparison(candidate), comparison(expected)
    return left is not None and right is not None and exact_json(left, right)


def _decision(case, proposal):
    draft = proposal.get('draft') or {}
    candidate = {key:draft.get(key) for key in ('capability','constraints','execution')}
    expected = _expected_candidate(case)
    if case['category']=='production':
        faithful = (proposal.get('status')=='awaiting_confirmation'
                    and _candidate_matches(candidate, expected) and draft.get('unresolved')==[])
        return bool(faithful), False
    if case['case_id']=='missing-keys':
        correct = (proposal.get('status')!='awaiting_confirmation'
            and _candidate_matches(candidate, expected) and 'execution.key_columns' in proposal.get('missing', []))
    else:
        unresolved = draft.get('unresolved')
        correct = (proposal.get('status')!='awaiting_confirmation' and _candidate_matches(candidate, expected)
            and isinstance(unresolved, list) and any(isinstance(value, str)
                and any(term.casefold() in value.casefold() for term in case['blocked_terms']) for value in unresolved))
    return bool(correct), bool(correct)


def _public_proposal(proposal):
    """Keep exact public anchors/typed values; arbitrary generated prose is hashed."""
    candidate = {key:copy.deepcopy(proposal['draft'][key]) for key in ('capability','constraints','execution')}
    requirements = [{key:copy.deepcopy(row[key]) for key in
        ('requirement_id','quote','fields','span','origin')} | {'text_sha256':_sha(row['text'].encode())}
        for row in proposal['requirements']]
    public = {'proposal_id':proposal['proposal_id'], 'proposal_sha256':proposal['proposal_sha256'],
        'source':copy.deepcopy(proposal['source']), 'status':proposal['status'],
        'candidate':candidate, 'candidate_sha256':_sha(canonical_json(candidate).encode()),
        'requirements':requirements,
        'unresolved_sha256':[_sha(value.encode()) for value in proposal['draft']['unresolved']],
        'original_review_sha256':_sha(proposal['summary'].encode()),
        'original_draft_sha256':_sha(canonical_json(proposal['draft']).encode())}
    public['summary'] = _public_summary(public)
    return {**public, 'public_proof_sha256':_sha(canonical_json(public).encode())}


def _public_summary(public):
    return _json({'original_request':public['source']['raw'], 'proposed_machine_fields':public['candidate'],
        'source_quotes_and_explicit_defaults':[{k:v for k,v in row.items() if k!='text_sha256'}
                                             for row in public['requirements']],
        'unresolved_count':len(public['unresolved_sha256']), 'reviewer_kind':_REVIEWER,
        'semantic_coverage':'reviewer simulation only; quotes prove provenance, not completeness'})


def review_fixture_proposal(plan, trial, proposal):
    """Controller-only fixture review; reject without editing an incorrect draft."""
    from ..proposal import ProposalStore
    case = next(c for c in plan['cases'] if c['case_id']==trial['case_id'])
    source = proposal.get('source') or {}
    result = {'approved':False, 'draft_faithful':False, 'probe_correct':False,
        'approval':None, 'public_proposal':None, 'reviewer_kind':_REVIEWER,
        'human_confirmation_verified':False}
    if source.get('raw')!=case['task'] or source.get('source_sha256')!=trial['prompt_sha256']:
        return result
    # Reopen the actual immutable candidate instead of accepting caller-shaped
    # dictionaries or a newer candidate with an old displayed identity.
    saved = ProposalStore(trial['db']).show(proposal['proposal_id'], session_id=source['session_id'])
    if saved!=proposal or saved['workspace']!=trial['workspace']:
        return result
    faithful, probe = _decision(case, saved)
    result.update(draft_faithful=faithful, probe_correct=probe)
    if faithful:
        result['public_proposal'] = _public_proposal(saved)
    if faithful and case['category']=='production':
        result['approval'] = ProposalStore(trial['db']).confirm(saved['proposal_id'],
            session_id=source['session_id'], expected_proposal_sha256=saved['proposal_sha256'], actor=_REVIEWER)
        result['approved'] = True
    return result


def _run_trials(plan, *, formal=False, max_sessions=None, timeout=None,
                runner=None, review_runner=None):
    if timeout is not None and timeout!=plan['timeout']:
        raise ValueError('frozen timeout cannot change')
    limit = 12 if formal else 2
    max_sessions = limit if max_sessions is None else max_sessions
    if type(max_sessions) is not int or not 0 <= max_sessions <= limit:
        raise ValueError('max_sessions exceeds the frozen trial budget')
    _check_freeze(plan)
    if runner is None:
        from ..integrations.hermes_session import run_hermes_session
        runner = run_hermes_session
    if review_runner is None:
        from ..integrations.hermes_review import run_hermes_review
        review_runner = run_hermes_review
    ledger = PhaseLedger(Path(plan['root'])/'ledger')
    reserved = {r['attempt_id'] for r in ledger.records()}
    launched = 0
    for trial in plan['sessions'] if formal else plan['pilots']:
        if trial['trial_id']+'-draft' in reserved or launched>=max_sessions:
            continue
        _check_freeze(plan)
        record = _observe(plan, trial, ledger, runner, review_runner)
        _save(Path(plan['root'])/'records'/(trial['trial_id']+'.json'), record)
        launched += 1


def run_review_comparison(root, *, formal=False, max_sessions=None, timeout=None,
                          runner=None, review_runner=None):
    """Run unreserved trials and record the complete driver timer separately.

    Driver duration includes freeze/orchestration and nested phases. It must not
    be added to phase durations, which would count the same work twice.
    """
    started, wall_started = time.monotonic(), time.time()
    plan = _load(root)
    invocation = {'schema_version':'rpe.review-driver/v1','invocation_id':uuid.uuid4().hex,
        'fingerprint':plan['sha256'],'started_at':wall_started, 'formal':formal,
        'max_sessions':max_sessions if type(max_sessions) is int or max_sessions is None else None,
        'status':'failed'}
    try:
        _run_trials(plan, formal=formal, max_sessions=max_sessions, timeout=timeout,
                    runner=runner, review_runner=review_runner)
        # Include normal aggregation in the driver timer as well.
        summarize_review_comparison(root)
        invocation['status'] = 'completed'
    except BaseException as error:
        invocation['error_code'] = type(error).__name__
        raise
    finally:
        invocation.update(finished_at=time.time(), elapsed_seconds=time.monotonic()-started)
        _save(Path(plan['root'])/'drivers'/(invocation['invocation_id']+'.json'), invocation)
    return summarize_review_comparison(root)


def summarize_review_comparison(root):
    plan = _load(root)
    phases = PhaseLedger(Path(plan['root'])/'ledger').records()
    known = {t['trial_id']:t for t in plan['sessions']+plan['pilots']}
    allowed = {'shared-preregistration', *(key+'-'+phase for key in known for phase in _PHASES)}
    if any(p['attempt_id'] not in allowed for p in phases):
        raise ValueError('unregistered comparison phase')
    records = []
    for phase in phases:
        if phase['phase']!='draft':
            continue
        identity = phase['attempt_id'].removesuffix('-draft')
        trial = known[identity]
        if phase['arm']!=trial['arm']:
            raise ValueError('phase arm differs from frozen trial')
        path = Path(plan['root'])/'records'/(identity+'.json')
        if path.exists():
            record = _read(path)
            if record['fingerprint']!=plan['sha256']:
                raise ValueError('record registration changed')
        else:
            record = {k:trial[k] for k in ('trial_id','phase','arm','case_id','category')}
            record.update(status='incomplete', approved=False, accepted=False,
                          error_code='reserved_attempt_not_replayed')
        records.append(record)
    formal = [r for r in records if r['phase']=='formal']
    by_arm = {}
    for arm in _ARMS:
        rows = [r for r in formal if r['arm']==arm]
        by_arm[arm] = {'formal_attempts':len(rows),
            'production_business_correct':sum(r.get('business_correct') is True for r in rows if r['category']=='production'),
            'production_complete_chain':sum(r.get('accepted') is True for r in rows if r['category']=='production'),
            'draft_faithful':sum(r.get('draft_faithful') is True for r in rows),
            'probe_correct':sum(r.get('probe_correct') is True for r in rows if r['category']=='contract_probe'),
            'costs':summarize_phases(p for p in phases if p['arm']==arm),
            'formal_costs':summarize_phases(p for p in phases if p['arm']==arm and any(
                p['attempt_id'].startswith(t['trial_id']+'-') for t in plan['sessions'])),
            'full_trial_elapsed_seconds_sum':_full_trial_wall([r for r in records if r['arm']==arm]),
            'formal_full_trial_elapsed_seconds_sum':_full_trial_wall(rows),
            'cost_population':'all reserved arm phases including pilots; formal_costs is a separate subset'}
    pairs = []
    for case in plan['cases']:
        rows = {r['arm']:r for r in formal if r['case_id']==case['case_id']}
        comparable = bool(set(rows)==set(_ARMS) and all(r.get('comparable') for r in rows.values())
            and rows['native'].get('model_identity')==rows['review'].get('model_identity'))
        pairs.append({'case_id':case['case_id'], 'comparable':comparable,
            **{arm+'_accepted':rows[arm].get('accepted') if arm in rows else None for arm in _ARMS}})
    drivers = [_read(path) for path in sorted((Path(plan['root'])/'drivers').glob('*.json'))]
    return {'schema_version':'rpe.review-comparison-summary/v1','profile':'review',
        'fingerprint':plan['sha256'],'formal_attempts':len(formal),
        'pilot_attempts':len(records)-len(formal),'by_arm':by_arm,'pairs':pairs,
        'comparison_ready':len(formal)==12 and all(p['comparable'] for p in pairs),
        'native_execution_sessions':sum(p['phase']=='execute' and p['arm']=='native' for p in phases),
        'reviewer_kind':_REVIEWER,'human_confirmation_verified':False,'human_review_seconds':None,
        'runtime_identity_attestation':'recorded_runtime_checks_only',
        'driver_invocations':len(drivers),
        'driver_elapsed_seconds_sum':sum(d['elapsed_seconds'] for d in drivers),
        'driver_timer_definition':'whole run invocation through aggregation, including freeze and nested phases; excludes its receipt write and final summary read; never add to phase sums',
        'driver_records':drivers,
        'full_trial_elapsed_seconds_sum':_full_trial_wall(records),
        'formal_full_trial_elapsed_seconds_sum':_full_trial_wall(formal),
        'elapsed_metric_definitions':{
            'phase_elapsed':'only explicit phase regions; excludes some gates and orchestration',
            'full_trial_elapsed':'complete per-arm trial including source, gates, approval, execution and final scoring',
            'driver_elapsed':'whole run including freeze, trial orchestration and aggregation; overlaps trial and phase timers',
            'aggregation':'never add overlapping timers; incomplete whole-trial values remain null'},
        'costs':summarize_phases(phases),
        'shared_costs':summarize_phases(p for p in phases if p['arm']=='shared'),
        'formal_costs':summarize_phases(p for p in phases if any(p['attempt_id'].startswith(t['trial_id']+'-') for t in plan['sessions'])),
        'pilot_costs':summarize_phases(p for p in phases if any(p['attempt_id'].startswith(t['trial_id']+'-') for t in plan['pilots'])),
        'coverage_limits':plan['coverage_limits'],'records':records}


def _full_trial_wall(records):
    values = [r.get('full_trial_elapsed_seconds') for r in records]
    return (sum(values) if all(type(value) in {int,float} and math.isfinite(value) and value>=0
                               for value in values) else None)


def export_review_evidence(root, destination):
    """Public fixtures and bounded evidence only; no raw host/model payloads."""
    plan, summary = _load(root), summarize_review_comparison(root)
    destination = _safe_path(destination)
    if destination.is_relative_to(plan['root']) or Path(plan['root']).is_relative_to(destination):
        raise ValueError('public export must be outside owned root')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('public export destination must be empty')
    destination.mkdir(parents=True, exist_ok=True)
    private = {'root','skill_root','hermes_root','db','catalog_root','helper_python','cases','sessions','pilots','sha256'}
    public = {k:v for k,v in plan.items() if k not in private}
    public['cases'] = [{k:v for k,v in c.items() if k!='source_path'} for c in plan['cases']]
    for key in ('sessions','pilots'):
        public[key] = [{k:v for k,v in t.items() if k not in {'workspace','db','oracle_plan'}} for t in plan[key]]
    public['controller_registration_sha256'] = plan['sha256']
    _save(destination/'preregistration.json', public)
    _save(destination/'summary.json', summary)
    _save(destination/'cost-phases.json', {'records':PhaseLedger(Path(plan['root'])/'ledger').records()})
    (destination/'fixtures').mkdir()
    (destination/'prompts').mkdir()
    for case in plan['cases']:
        raw = read_regular_file(_safe_path(case['source_path']), max_bytes=MAX_ARTIFACT_BYTES)
        if _sha(raw)!=case['input_sha256']:
            raise ValueError('public fixture changed')
        (destination/'fixtures'/Path(case['source_path']).name).write_bytes(raw)
        (destination/'prompts'/(case['case_id']+'.txt')).write_text(case['task'], encoding='utf-8')
    trials = {t['trial_id']:t for t in plan['sessions']+plan['pilots']}
    cases = {c['case_id']:c for c in plan['cases']}
    for record in summary['records']:
        trial = trials[record['trial_id']]
        case = cases[trial['case_id']]
        folder = destination/'artifacts'/trial['trial_id']
        folder.mkdir(parents=True)
        raw, metadata = _export_bytes(Path(trial['workspace'])/case['output_name'], case['output_name'])
        fresh = score_address_artifact(plan, trial) if case['category']=='production' else {}
        verified = bool(raw is not None and fresh.get('business_correct') is True
            and fresh.get('product_oracle_passed') is True and metadata['sha256']==fresh['artifact_sha256'])
        metadata.update(raw_exported=verified,
            reason='reverified_public_business_truth' if verified else 'unverified_or_absent_artifact')
        _save(folder/'manifest.json', {'artifacts':[metadata]})
        if verified:
            (folder/case['output_name']).write_bytes(raw)
    return summary


def verify_review_evidence(destination):
    """Recompute portable public checks, never infer a real human approval."""
    result = {'schema_version':'rpe.review-public-verification/v1','verified':False,
        'source_documents_verified':0,'business_artifacts_verified':0,
        'approved_proposals_verified':0,
        'reviewer_kind':_REVIEWER,'human_confirmation_verified':False,
        'runtime_identity_attestation':'recorded_runtime_checks_only','errors':[]}
    try:
        root = _safe_path(destination)
        plan, summary = _read(root/'preregistration.json'), _read(root/'summary.json')
        if plan.get('profile')!='review' or summary.get('profile')!='review':
            raise ValueError('not_review_public_evidence')
        if summary['fingerprint']!=plan['controller_registration_sha256']:
            raise ValueError('registration_summary_link_changed')
        if summary['reviewer_kind']!=_REVIEWER or summary['human_confirmation_verified'] is not False:
            raise ValueError('simulated_review_label_changed')
        phases = _read(root/'cost-phases.json')['records']
        if summarize_phases(phases)!=summary['costs']:
            raise ValueError('cost_summary_changed')
        _verify_phase_records(plan, summary, phases)
        drivers = summary.get('driver_records', [])
        if (len(drivers)!=summary['driver_invocations'] or
                sum(driver['elapsed_seconds'] for driver in drivers)!=summary['driver_elapsed_seconds_sum']):
            raise ValueError('driver_timer_summary_changed')
        seen_drivers = set()
        for driver in drivers:
            if (driver['fingerprint']!=summary['fingerprint'] or driver['invocation_id'] in seen_drivers
                or driver['elapsed_seconds']<0 or driver['status'] not in {'completed','failed'}
                or driver['sha256']!=_sha(_json({k:v for k,v in driver.items() if k!='sha256'}).encode())):
                raise ValueError('driver_receipt_changed')
            seen_drivers.add(driver['invocation_id'])
        if len(plan['cases'])!=6:
            raise ValueError('public_source_coverage_incomplete')
        for case in plan['cases']:
            raw = read_regular_file(_public_path(root, 'prompts', case['case_id']+'.txt'), max_bytes=65536)
            if raw!=case['task'].encode():
                raise ValueError('public_source_changed')
            source = _public_path(root, 'fixtures', case['case_id']+Path(case['input_name']).suffix)
            if _sha(read_regular_file(source, max_bytes=MAX_ARTIFACT_BYTES))!=case['input_sha256']:
                raise ValueError('public_fixture_changed')
            result['source_documents_verified'] += 1
        trials = {t['trial_id']:t for t in plan['sessions']+plan['pilots']}
        cases = {c['case_id']:c for c in plan['cases']}
        for record in summary['records']:
            trial = trials[record['trial_id']]
            case = cases[trial['case_id']]
            proof = record.get('public_proposal')
            if proof is not None:
                _verify_public_proposal(proof, case, record)
            if record.get('approved') is True:
                _verify_approval(record.get('approval'), proof, record)
                result['approved_proposals_verified'] += 1
            elif record.get('approval') is not None:
                raise ValueError('unapproved_confirmation_exported')
            folder = _public_path(root, 'artifacts', trial['trial_id'])
            manifest = _read(folder/'manifest.json')
            exported_business = False
            for artifact in manifest['artifacts']:
                if artifact['name']!=case['output_name']:
                    raise ValueError('unapproved_public_artifact')
                path = _public_path(root, 'artifacts', trial['trial_id'], artifact['name'])
                if artifact['raw_exported'] is not True:
                    if path.exists():
                        raise ValueError('unverified_artifact_bytes_exported')
                    continue
                raw = read_regular_file(path, max_bytes=MAX_ARTIFACT_BYTES)
                if _sha(raw)!=artifact['sha256'] or len(raw)!=artifact['size_bytes']:
                    raise ValueError('public_artifact_changed')
                if case['category']!='production' or not _public_business_correct(case, raw):
                    raise ValueError('public_business_truth_failed')
                exported_business = True
                result['business_artifacts_verified'] += 1
            if record.get('business_correct') is True and case['category']=='production' and not exported_business:
                raise ValueError('claimed_business_artifact_missing')
            if record.get('accepted') is True and case['category']=='production' and not record.get('approved'):
                raise ValueError('accepted_execution_lacks_approval')
        result['verified'] = True
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,RecursionError,csv.Error) as error:
        result['errors'].append(str(error) if isinstance(error,ValueError) else type(error).__name__)
    return result


def _public_path(root, *parts):
    path = _safe_path(root.joinpath(*parts))
    if not path.is_relative_to(root):
        raise ValueError('public_evidence_path_escaped')
    return path


def _verify_phase_records(plan, summary, phases):
    """Recompute both immutable ledger hashes and complete attempt coverage."""
    reservation_keys = {'schema_version','arm','phase','attempt_id','api_expected','scope','reserved_at'}
    trials = {t['trial_id']:t for t in plan['sessions']+plan['pilots']}
    draft_ids = set()
    for phase in phases:
        reservation = {key:phase[key] for key in reservation_keys}
        reservation['status'] = 'incomplete'
        reservation_hash = _sha(canonical_json(reservation).encode())
        if phase['status']=='incomplete':
            if phase['record_sha256']!=reservation_hash:
                raise ValueError('incomplete_cost_reservation_changed')
        else:
            outcome = {key:value for key,value in phase.items() if key not in reservation_keys | {'record_sha256'}}
            outcome['attempt_id'] = phase['attempt_id']
            if phase['reservation_sha256']!=reservation_hash or _sha(canonical_json(outcome).encode())!=phase['record_sha256']:
                raise ValueError('cost_phase_binding_changed')
        if phase['attempt_id']=='shared-preregistration':
            if phase['arm']!='shared' or phase['phase']!='preregistration' or phase['api_expected'] is not False:
                raise ValueError('shared_cost_identity_changed')
            continue
        suffix = '-'+phase['phase']
        if phase['phase'] not in _PHASES or not phase['attempt_id'].endswith(suffix):
            raise ValueError('unregistered_cost_phase')
        identity = phase['attempt_id'][:-len(suffix)]
        if identity not in trials or phase['arm']!=trials[identity]['arm']:
            raise ValueError('unregistered_cost_attempt')
        if phase['phase']=='draft':
            draft_ids.add(identity)
    if len(summary['records'])!=len(draft_ids) or {r['trial_id'] for r in summary['records']}!=draft_ids:
        raise ValueError('cost_attempt_coverage_changed')
    for record in summary['records']:
        trial = trials[record['trial_id']]
        if any(record[key]!=trial[key] for key in ('arm','phase','case_id','category')):
            raise ValueError('record_trial_identity_changed')
        if record.get('fingerprint', summary['fingerprint'] if record['status']=='incomplete' else None)!=summary['fingerprint']:
            raise ValueError('record_registration_link_changed')


def _verify_public_proposal(proof, case, record):
    saved = proof.get('public_proof_sha256')
    if _sha(canonical_json({k:v for k,v in proof.items() if k!='public_proof_sha256'}).encode())!=saved:
        raise ValueError('public_proposal_digest_changed')
    source = proof['source']
    if (source['raw']!=case['task'] or source['source_sha256']!=_sha(case['task'].encode())
            or source['session_id']!=record['session_id'] or source['source_sha256']!=record['source_sha256']):
        raise ValueError('public_proposal_source_changed')
    if not _candidate_matches(proof['candidate'], _expected_candidate(case)):
        raise ValueError('public_candidate_not_faithful')
    if proof['candidate_sha256']!=_sha(canonical_json(proof['candidate']).encode()):
        raise ValueError('public_candidate_digest_changed')
    if proof['proposal_id']!=proof['proposal_sha256']:
        raise ValueError('public_proposal_identity_changed')
    required = {'/capability','/constraints', *('/execution/'+key for key in proof['candidate']['execution'])}
    covered = set()
    for row in proof['requirements']:
        if set(row)!={'requirement_id','quote','fields','span','origin','text_sha256'}:
            raise ValueError('unapproved_requirement_fields')
        if not row['fields'] or set(row['fields'])-required or len(set(row['fields']))!=len(row['fields']):
            raise ValueError('public_requirement_mapping_changed')
        quote = row['quote']
        if quote is None:
            if row['origin']!='proposed_default' or row['span'] is not None:
                raise ValueError('public_default_label_changed')
        else:
            start = source['raw'].find(quote) if isinstance(quote, str) and quote else -1
            if start<0 or row['origin']!='source_quote' or row['span']!=[start,start+len(quote)]:
                raise ValueError('public_source_span_changed')
        covered.update(row['fields'])
    if covered!=required or proof['summary']!=_public_summary(proof):
        raise ValueError('public_review_coverage_changed')


def _verify_approval(approval, proof, record):
    if not isinstance(approval, dict) or proof is None:
        raise ValueError('approved_candidate_proof_missing')
    expected = {'schema_version':'rpe.confirmation/v1','status':'confirmed',
        'proposal_id':proof['proposal_id'],'proposal_sha256':proof['proposal_sha256'],
        'session_id':record['session_id'],'actor':_REVIEWER}
    if ({key:value for key,value in approval.items() if key!='confirmation_id'}!=expected
            or approval.get('confirmation_id')!=_sha(canonical_json(expected).encode())
            or proof['status']!='awaiting_confirmation' or proof['unresolved_sha256']):
        raise ValueError('approval_candidate_binding_changed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    register = sub.add_parser('preregister')
    for name in ('root','skill-root'):
        register.add_argument('--'+name, required=True)
    for name in ('hermes-root','helper-python'):
        register.add_argument('--'+name)
    register.add_argument('--timeout', type=int, default=120)
    register.add_argument('--max-turns', type=int, default=8)
    run = sub.add_parser('run')
    run.add_argument('--root', required=True)
    run.add_argument('--formal', action='store_true')
    run.add_argument('--max-sessions', type=int)
    summary = sub.add_parser('summary')
    summary.add_argument('--root', required=True)
    export = sub.add_parser('export')
    export.add_argument('--root', required=True)
    export.add_argument('--destination', required=True)
    verify = sub.add_parser('verify')
    verify.add_argument('--destination', required=True)
    arguments = vars(parser.parse_args(argv))
    command = arguments.pop('command')
    functions = dict(preregister=preregister_review_comparison, run=run_review_comparison,
        summary=summarize_review_comparison, export=export_review_evidence,
        verify=verify_review_evidence)
    result = functions[command](**arguments)
    print(_json(result))
    return 1 if command=='verify' and not result['verified'] else 0
