#!/usr/bin/env python3
"""Exactly one explicitly directed final native execution of an actually reviewed model candidate.

This proof is outside the paired population, inside the preregistered total cost.
No gold, review controller, oracle or expected rows are passed to the model.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.dont_write_bytecode=True
OWNED=Path('/Users/audrey/项目/缓存/test/rpe-v114-8581d6e12eab449aaa71bf8af01ce97b')
COMPARISON=OWNED/'comparison'
WORKTREE=Path('/Users/audrey/.codex/worktrees/rpe-v114/rpe-harness')
sys.path.insert(0,str(WORKTREE/'src'))


def main():
    from rpe_harness.experiments import review_v111_comparison as shared
    from rpe_harness.experiments.review_v114_comparison import native_execution_options, request_budget_observation
    from rpe_harness.experiments.review_native_evidence import evaluate_native_review_execution_v114, native_chain_diagnostics
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    from rpe_harness.experiments.address_comparison import score_address_artifact
    from rpe_harness.integrations.hermes_session import run_hermes_session
    from rpe_harness.proposal import ProposalStore
    from rpe_harness.path_binding import read_regular_file
    shared._check_freeze(shared._load(COMPARISON),True)
    plan=shared._load(COMPARISON)
    marker=OWNED/'directed-native-entry-claim.json'
    with marker.open('x') as stream:
        json.dump({'attempts':1,'scope':'external_final_entry','counts_toward_native_execution_total':True},stream)
    eligible=[t for t in plan['sessions']+plan['pilots'] if t['arm']=='review'
        and next(c for c in shared._all_cases(plan) if c['case_id']==t['case_id'])['capability']=='xlsx.inspect']
    # Prefer the formal caller; the already observed engineering caller is a
    # declared fallback for this external entry proof only, never paired scoring.
    selected=[]
    for trial_option in eligible:
        record_option=json.loads((COMPARISON/'records'/(trial_option['trial_id']+'.json')).read_text())
        if record_option.get('draft_invocation_verified') is True and record_option.get('approved') is True:
            selected.append((trial_option,record_option))
    source_trial,source_record=selected[0] if selected else (eligible[0],{})
    proposal_id=source_record.get('draft_gate',{}).get('proposal_id')
    # Any missing actual reviewed candidate is a failed proof, never a gold fallback.
    if not proposal_id or source_record.get('draft_invocation_verified') is not True or source_record.get('approved') is not True:
        result={'verified':False,'error_code':'actual_reviewed_model_candidate_missing','model_called':False}
        (OWNED/'directed-native-entry.json').write_text(json.dumps(result))
        return result
    proposal=ProposalStore(source_trial['db']).show(proposal_id,session_id=source_record['session_id'])
    draft=shared._public_candidate(proposal)
    case=next(c for c in shared._all_cases(plan) if c['case_id']==source_trial['case_id'])
    workspace=COMPARISON/'workspaces'/'external-directed-native-entry'
    workspace.mkdir(mode=0o700)
    raw=read_regular_file(Path(source_trial['workspace'])/case['input_name'],max_bytes=1024*1024)
    (workspace/case['input_name']).write_bytes(raw)
    database=COMPARISON/'databases'/'external-directed-native-entry.sqlite'
    with sqlite3.connect(source_trial['db']) as source, sqlite3.connect(database) as target:
        source.backup(target)
    trial={**source_trial,'trial_id':'external-directed-native-entry','arm':'native','phase':'external',
        'workspace':str(workspace),'db':str(database),'input_sha256':hashlib.sha256(raw).hexdigest()}
    trial['oracle_plan']=copy.deepcopy(source_trial['oracle_plan'])
    trial['oracle_plan']['input_path']=str(workspace/case['input_name'])
    trial['oracle_plan']['output_path']=str(workspace/case['output_name'])
    candidate={key:copy.deepcopy(draft[key]) for key in ('capability','constraints','execution')}
    # The public candidate already uses relative paths. Refuse an ambiguous rebind.
    if candidate['execution'].get('input_path')!=case['input_name'] or candidate['execution'].get('output_path')!=case['output_name']:
        raise ValueError('reviewed candidate paths cannot bind to fresh caller workspace')
    import shlex
    command=('env -u PYTHONPATH -u PYTHONHOME -u PYTHONSTARTUP -u PYTHONUSERBASE '
        +shlex.quote(plan['helper_python'])+' -I -B \"$HERMES_HOME/skills/xlsx/scripts/xlsx_read.py\" '
        +shlex.quote(case['input_name'])+' --json --sheet '+shlex.quote(candidate['execution']['sheet'])
        +' > '+shlex.quote(case['output_name']))
    prompt=(shared._json(candidate)+'\n\nRuntime evidence protocol for this directed entry proof only: '
        'native rpe_* tools do not exist. If xlsx is archived, restore it using the installed '
        'Hermes curator CLI, then call native skill_view(name=\"xlsx\") before execution. '
        'The approved candidate above authorizes the business execution. To obtain the existing '
        'strong process receipt, run this exact original-helper command in the supplied workspace, '
        'with stdout directly to the final file; do not use scratch files, cp, tee, or rewrite helpers. '
        'Do not change original files or install dependencies. No expected rows are supplied.\n' + command)
    ledger=PhaseLedger(OWNED/'directed-entry-ledger')
    options=native_execution_options(plan,trial)
    _actual,observation,error=shared._model_phase(plan,trial,ledger,'execute',lambda:run_hermes_session(
        prompt,workspace,database,tool_enabled=False,
        owner_root=COMPARISON/'sessions'/'external-directed-native-entry-execute',timeout=180,
        hermes_root=plan['hermes_root'],max_turns=8,max_task_attempts=1,
        preserve_native_tool_search=True,public_contract='clean',read_isolation_root=plan['root'],
        deny_read_roots=shared._denied(plan,trial),
        request_budget=plan['request_budget'],native_finish_enabled=True,**options,**shared._skill_kwargs(plan,trial)))
    diagnostics=native_chain_diagnostics(observation,database)
    budget=request_budget_observation(observation,diagnostics)
    gate=evaluate_native_review_execution_v114(observation,shared._gate_plan(plan),trial)
    business=score_address_artifact(shared._gate_plan(plan),trial)
    output=workspace/case['output_name']
    try:
        output_bytes=read_regular_file(output,max_bytes=1024*1024)
    except (OSError,ValueError):
        output_bytes=None
    try:
        input_preserved=read_regular_file(workspace/case['input_name'],max_bytes=1024*1024)==raw
    except (OSError,ValueError):
        input_preserved=False
    result={'schema_version':'rpe.v114-final-native-entry/v1','model_called':True,
        'scope':'external_final_entry_not_paired_denominator','source_trial_id':source_trial['trial_id'],
        'source_proposal_id':proposal_id,'source_proposal_sha256':proposal['proposal_sha256'],
        'reviewer_kind':plan['reviewer_kind'],'human_confirmation_verified':False,
        'input_sha256':trial['input_sha256'],'candidate':candidate,'raw_request':case['task'],
        'candidate_sha256':hashlib.sha256(shared._json(candidate).encode()).hexdigest(),
        'actual_prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'directed_runtime_command':command,'proof_kind':'directed_engineering_not_freeform_baseline',
        'registration_source_module_sha256':plan['environment']['source_sha256'],
        'actual_model_source_module_sha256':shared._read(COMPARISON/'formal-freeze.json')['environment']['source_sha256'],
        'usage':observation.get('usage'),'model_identity':observation.get('model_identity'),
        'completed':observation.get('completed'),'timed_out':observation.get('timed_out'),
        'exit_code':observation.get('exit_code'),'error_code':error,
        'original_prompt_sha256':observation.get('original_prompt_sha256'),
        'public_contract_sha256':observation.get('public_contract_sha256'),
        'request_budget_observation':budget,'chain_diagnostics':diagnostics,'execution_gate':gate,
        'business':business,'process_cleanup':observation.get('process_cleanup'),
        'input_preserved':input_preserved,
        'output_sha256':hashlib.sha256(output_bytes).hexdigest() if output_bytes is not None else None,
        'verified':bool(not error and shared._host_ok(observation) and gate.get('verified') is True
            and budget.get('conformant') is True and diagnostics.get('verified') is True
            and observation.get('original_prompt_sha256')==hashlib.sha256(prompt.encode()).hexdigest())}
    with (OWNED/'directed-native-entry.json').open('x') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    if output_bytes is not None:(OWNED/'directed-native-output.json').write_bytes(output_bytes)
    # All rows are the plugin's allowlisted metadata, not private transcripts.
    with (OWNED/'directed-native-entry-events.json').open('x') as stream:
        json.dump(observation.get('events',[]),stream,ensure_ascii=False,indent=2)
    print(json.dumps({key:result.get(key) for key in ('verified','error_code','execution_gate','usage','timed_out')},ensure_ascii=False))
    return result


if __name__=='__main__':main()
