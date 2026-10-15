#!/usr/bin/env python3
"""Retain a rejected original candidate after controller bookkeeping crashed; no model replay."""
import copy
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
WORKTREE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKTREE/'src'))
REPORT = WORKTREE/'reports/v1.15.0'
OWNED = Path(json.loads((REPORT/'validation/owned.json').read_bytes())['root'])


def main():
    from rpe_harness.experiments import review_v111_comparison as shared
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    from rpe_harness.experiments.review_native_evidence import evaluate_native_review_draft, native_chain_diagnostics
    from rpe_harness.experiments.review_v114_comparison import request_budget_observation
    root = OWNED/'comparison-runtime'
    plan = shared._load(root)
    trial = next(t for t in plan['sessions'] if t['case_id']=='pending-account-key' and t['arm']=='native')
    ledger = PhaseLedger(root/'ledger')
    attempts = ledger.records()
    assert sum(p['phase']=='draft' for p in attempts)==5
    assert not (root/'records'/(trial['trial_id']+'.json')).exists()
    observed = json.loads((OWNED/'observations-final'/(trial['trial_id']+'-draft.json')).read_bytes())
    artifact = Path(trial['workspace'])/'draft.json'
    raw = artifact.read_bytes()
    candidate = json.loads(raw)
    assert type(candidate.get('unresolved')) is list
    assert any(type(value) is not str for value in candidate['unresolved'])
    gate = evaluate_native_review_draft(observed, shared._gate_plan(plan), trial)
    diagnostics = native_chain_diagnostics(observed, trial['db'])
    budget = request_budget_observation(observed, diagnostics)
    prompt = shared._native_prompt(plan, trial)
    identity = shared._public_identity(observed.get('model_identity'))
    from rpe_harness.experiments.review_v115_comparison import public_contract_sha
    record = {key: trial[key] for key in ('trial_id','phase','arm','case_id','category')}
    record.update(fingerprint=plan['sha256'], status='failed', approved=False, accepted=False,
        draft_invocation_verified=gate.get('verified') is True, execution_verified=False,
        comparable=bool(shared._host_ok(observed) and budget['conformant']
            and observed.get('original_prompt_sha256')==hashlib.sha256(prompt.encode()).hexdigest()
            and observed.get('prompt_sha256')==hashlib.sha256(prompt.encode()).hexdigest()
            and observed.get('catalog_snapshot_id') is None
            and observed.get('public_contract_sha256')==public_contract_sha(plan,'native')
            and shared._observed_identity(identity)),
        reviewer_kind=plan['reviewer_kind'], human_confirmation_verified=False,
        session_id=observed.get('session_id'), model_identity=identity,
        raw_source_sha256=trial['prompt_sha256'], actual_host_prompt_sha256=observed.get('original_prompt_sha256'),
        host_cleanup_complete=observed.get('process_cleanup',{}).get('cleanup_complete') is True,
        draft_gate=gate, draft_faithful=False, trace_verified=False, probe_correct=False,
        no_business_output_before_approval=not (Path(trial['workspace'])/'output.csv').exists(),
        native_chain_diagnostics={'draft':diagnostics}, request_budget_observations={'draft':budget},
        error_code='invalid_native_draft_unresolved_item_type',
        original_invalid_candidate=copy.deepcopy(candidate), original_candidate_sha256=hashlib.sha256(raw).hexdigest(),
        controller_bookkeeping_error='AttributeError_in_proof_unresolved_sha256',
        bookkeeping_recovery_only=True, model_replayed=False, full_trial_elapsed_seconds=None)
    record.update(shared._local_phase(ledger, trial, 'verification', lambda:shared._verify_trial(plan,trial,record)))
    shared._save(root/'records'/(trial['trial_id']+'.json'), record)
    (REPORT/'validation/invalid-native-candidate.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'record_retained':True,'accepted':record['accepted'],'model_replayed':False}))


if __name__=='__main__':main()
