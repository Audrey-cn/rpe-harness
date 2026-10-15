"""Bounded no-model acceptance checks for the NL draft/review comparison."""
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def api():
    return importlib.import_module('rpe_harness.experiments.review_comparison')


@pytest.fixture
def review_plan(tmp_path, monkeypatch):
    module = api()
    skills = tmp_path/'skills'
    shutil.copytree(Path(__file__).parent/'fixtures/skills/productivity/xlsx',
                    skills/'productivity/xlsx')
    # Concurrent core/host development must not invalidate a unit-test fixture.
    # The production registration still recomputes the real external fingerprint.
    environment = module._environment(sys.executable, tmp_path/'absent-host')
    monkeypatch.setattr(module, '_environment', lambda *args: environment)
    return module.preregister_review_comparison(tmp_path/'comparison', skill_root=skills,
        hermes_root=tmp_path/'absent-host', timeout=120, max_turns=8)


def test_registration_freezes_same_nl_and_public_schema_without_oracle_to_either_arm(review_plan):
    assert review_plan['profile'] == 'review'
    assert len(review_plan['sessions']) == 12
    assert len(review_plan['pilots']) == 2
    assert review_plan['max_draft_sessions'] == 14
    assert review_plan['max_native_execution_sessions'] == 7
    for left, right in zip(review_plan['sessions'][::2], review_plan['sessions'][1::2]):
        assert {left['arm'], right['arm']} == {'native', 'review'}
        assert left['prompt_sha256'] == right['prompt_sha256']
        assert left['input_sha256'] == right['input_sha256']
        text = (Path(left['workspace'])/'prompt.txt').read_text()
        case = next(c for c in review_plan['cases'] if c['case_id']==left['case_id'])
        assert text == case['task']
        assert not text.lstrip().startswith('{')
        assert 'expected_rows' not in text and 'oracle_plan' not in text
    assert review_plan['reviewer_kind'] == 'simulated_fixture_reviewer'
    assert review_plan['human_confirmation_verified'] is False


def test_failed_drafting_is_reserved_once_and_never_executes_or_repairs(review_plan):
    module = api()
    calls = []
    def unavailable(prompt, workspace, db, **kwargs):
        assert 'required_request' not in kwargs
        calls.append((prompt, kwargs))
        raise RuntimeError('PRIVATE_PROVIDER_ERROR')
    module.run_review_comparison(review_plan['root'], max_sessions=2,
                                 runner=unavailable, review_runner=unavailable)
    summary = module.run_review_comparison(review_plan['root'], formal=True,
                                           runner=unavailable, review_runner=unavailable)
    assert len(calls)==14
    assert summary['formal_attempts']==12 and summary['pilot_attempts']==2
    assert summary['costs']['total_tokens'] is None
    assert summary['costs']['failed_phases'] >= 14
    assert summary['native_execution_sessions']==0
    assert summary['human_review_seconds'] is None
    assert all(not r['approved'] for r in summary['records'])
    module.run_review_comparison(review_plan['root'], formal=True,
                                 runner=unavailable, review_runner=unavailable)
    assert len(calls)==14
    raw = ''.join(p.read_text() for p in (Path(review_plan['root'])/'records').glob('*.json'))
    assert 'PRIVATE_PROVIDER_ERROR' not in raw


def test_public_export_is_portable_and_recomputes_costs_and_public_source(review_plan, tmp_path):
    module = api()
    destination = tmp_path/'public'
    module.export_review_evidence(review_plan['root'], destination)
    shutil.rmtree(review_plan['root'])
    result = module.verify_review_evidence(destination)
    assert result['verified'] is True
    assert result['source_documents_verified']==6
    assert result['reviewer_kind']=='simulated_fixture_reviewer'
    assert result['human_confirmation_verified'] is False
    assert result['runtime_identity_attestation']=='recorded_runtime_checks_only'
    prompt = destination/'prompts'/'csv-zh-composite.txt'
    prompt.write_text('changed public source')
    assert module.verify_review_evidence(destination)['verified'] is False


def test_interrupted_phase_is_retained_as_unknown_and_cannot_be_replayed(review_plan, tmp_path):
    module = api()
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    trial = review_plan['pilots'][0]
    PhaseLedger(Path(review_plan['root'])/'ledger').reserve(
        trial['arm'], 'draft', trial['trial_id']+'-draft')
    summary = module.run_review_comparison(review_plan['root'], max_sessions=0)
    assert summary['pilot_attempts']==1
    assert summary['records'][0]['status']=='incomplete'
    assert summary['costs']['total_tokens'] is None
    destination = tmp_path/'public'
    module.export_review_evidence(review_plan['root'], destination)
    assert module.verify_review_evidence(destination)['verified'] is True


def test_cli_registration_summary_export_verify_do_not_call_models(review_plan, tmp_path):
    module = api()
    destination = tmp_path/'public'
    module.export_review_evidence(review_plan['root'], destination)
    result = subprocess.run([sys.executable, '-B',
        str(Path(__file__).parents[1]/'scripts/v110_review_comparison.py'),
        'verify', '--destination', str(destination)],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)['verified'] is True


def csv_draft(*, wrong_key=False):
    return {'capability':'tabular.deduplicate',
        'constraints':['preserve_original','preserve_order'],
        'execution':{'input_path':'input.csv','output_path':'output.csv',
            'key_columns':['地区'] if wrong_key else ['地区','编号'],
            'delimiter':',','encoding':'utf-8','keep':'first'},
        'requirements':[{'text':'PRIVATE_ROW_TEXT', 'quote':'地区和编号合起来才算同一笔',
            'fields':['/capability','/constraints','/execution/input_path',
                '/execution/output_path','/execution/key_columns','/execution/delimiter',
                '/execution/encoding','/execution/keep']}], 'unresolved':[]}


def stored_csv_proposal(plan, *, wrong_key=False):
    from rpe_harness.proposal import create_proposal
    trial = next(t for t in plan['sessions'] if t['case_id']=='csv-zh-composite' and t['arm']=='review')
    proposal = create_proposal((Path(trial['workspace'])/'prompt.txt').read_text(),
        csv_draft(wrong_key=wrong_key), db_path=trial['db'], workspace=trial['workspace'],
        session_id='fixture-session', source_ref='public-fixture',
        catalog_root=plan['catalog_root'], skill_snapshot_id=plan['skill_snapshot_id'])
    return trial, proposal


def test_simulated_reviewer_approves_exact_faithful_candidate_and_keeps_unknown_text_private(review_plan):
    module = api()
    trial, proposal = stored_csv_proposal(review_plan)
    assert proposal['status']=='awaiting_confirmation'
    review = module.review_fixture_proposal(review_plan, trial, proposal)
    assert review['approved'] is True and review['draft_faithful'] is True
    assert review['approval']['proposal_id']==proposal['proposal_id']
    assert review['approval']['proposal_sha256']==proposal['proposal_sha256']
    assert review['approval']['actor']=='simulated_fixture_reviewer'
    assert review['public_proposal']['requirements'][0]['span']==[22,35]
    assert 'PRIVATE_ROW_TEXT' not in json.dumps(review, ensure_ascii=False)
    assert not (Path(trial['workspace'])/'output.csv').exists()


def test_simulated_reviewer_rejects_wrong_key_without_repair_or_confirmation(review_plan):
    module = api()
    trial, proposal = stored_csv_proposal(review_plan, wrong_key=True)
    assert proposal['status']=='awaiting_confirmation'
    review = module.review_fixture_proposal(review_plan, trial, proposal)
    assert review['approved'] is False and review['draft_faithful'] is False
    assert review['approval'] is None
    from rpe_harness.proposal import ProposalStore
    saved = ProposalStore(trial['db']).show(proposal['proposal_id'], session_id='fixture-session')
    assert saved['draft']['execution']['key_columns']==['地区']
    import sqlite3
    with sqlite3.connect(trial['db']) as database:
        assert database.execute('SELECT count(*) FROM rpe_confirmations').fetchone()[0]==0


def test_reviewer_accepts_reordered_constraint_flags_and_composite_keys_without_changing_candidate(review_plan):
    module = api()
    from rpe_harness.proposal import create_proposal, ProposalStore
    trial = next(t for t in review_plan['sessions'] if t['case_id']=='csv-zh-composite' and t['arm']=='review')
    candidate = csv_draft()
    candidate['constraints'] = ['preserve_order','preserve_original']
    candidate['execution']['key_columns'] = ['编号','地区']
    proposal = create_proposal((Path(trial['workspace'])/'prompt.txt').read_text(), candidate,
        db_path=trial['db'],workspace=trial['workspace'],session_id='fixture-session',source_ref='public-fixture',
        catalog_root=review_plan['catalog_root'],skill_snapshot_id=review_plan['skill_snapshot_id'])
    review = module.review_fixture_proposal(review_plan, trial, proposal)
    assert review['approved'] is True
    assert review['public_proposal']['candidate']['constraints']==['preserve_order','preserve_original']
    assert review['public_proposal']['candidate']['execution']['key_columns']==['编号','地区']
    assert ProposalStore(trial['db']).show(proposal['proposal_id'], session_id='fixture-session')['draft']==candidate


def complete_host_result(plan, prompt, *, proposal=None):
    import hashlib
    return {'completed':True,'exit_code':0,'events':[], 'session_id':'fixture-session',
        'elapsed_seconds':0.01,'process_cleanup':{'cleanup_complete':True},
        'model_identity':{'provider':'fixture','requested_model':'fixture-model',
                          'response_models':['fixture-model']},
        'usage':{'api_requests':1,'known_total_tokens':7,'total_tokens':7,
                 'coverage_complete':True,'missing_observations':0},
        'original_prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'public_contract_sha256':plan['public_contract_sha256'],
        'catalog_snapshot_id':plan['skill_snapshot_id'],'proposal':proposal}


def test_review_trial_executes_real_approved_worker_and_exports_recomputable_proof(review_plan, monkeypatch, tmp_path):
    module = api()
    from rpe_harness.integrations import hermes_review
    from rpe_harness.proposal import create_proposal
    trial = next(t for t in review_plan['pilots'] if t['arm']=='review')
    def draft_host(prompt, workspace, database, **kwargs):
        proposal = create_proposal(prompt, csv_draft(), db_path=database, workspace=workspace,
            session_id='fixture-session',source_ref='public-fixture',
            catalog_root=review_plan['catalog_root'],skill_snapshot_id=review_plan['skill_snapshot_id'])
        return complete_host_result(review_plan, prompt, proposal=proposal)
    # Native SDK identity/transport proof is tested by the host integration;
    # isolate that external boundary while keeping proposal, approval, worker,
    # oracle, ledger, export and offline verification real here.
    monkeypatch.setattr(hermes_review, 'evaluate_review_session', lambda result,*args,**kwargs:
        {'verified':True,'proposal_verified':True,'proposal_id':result['proposal']['proposal_id']})
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    record = module._observe(review_plan, trial, PhaseLedger(Path(review_plan['root'])/'ledger'),
                             lambda *a,**k:pytest.fail('controller execution called a model'), draft_host)
    module._save(Path(review_plan['root'])/'records'/(trial['trial_id']+'.json'), record)
    assert record['accepted'] is True
    assert record['execution_kind']=='approved_controller'
    assert (Path(trial['workspace'])/'output.csv').read_text() == (
        '地区,编号,金额\n华东,001,10\n华西,001,20\n华东,1,30\n')
    summary = module.summarize_review_comparison(review_plan['root'])
    assert summary['costs']['api_requests']==1 and summary['costs']['total_tokens']==7
    assert summary['costs']['phase_count']==5
    destination = tmp_path/'public'
    module.export_review_evidence(review_plan['root'], destination)
    shutil.rmtree(review_plan['root'])
    result = module.verify_review_evidence(destination)
    assert result['verified'] is True and result['business_artifacts_verified']==1
    assert result['approved_proposals_verified']==1
    raw = ''.join(p.read_text() for p in destination.rglob('*.json'))
    assert 'PRIVATE_ROW_TEXT' not in raw
    artifact = destination/'artifacts'/trial['trial_id']/'output.csv'
    artifact.write_text('tampered\n')
    assert module.verify_review_evidence(destination)['verified'] is False


def test_driver_timer_includes_freeze_and_orchestration_without_doubling_phase_costs(review_plan, monkeypatch):
    module = api()
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda:clock[0])
    def freeze(plan):
        clock[0] += 2.0
    monkeypatch.setattr(module, '_check_freeze', freeze)
    summary = module.run_review_comparison(review_plan['root'], max_sessions=0)
    assert summary['driver_invocations']==1
    assert summary['driver_elapsed_seconds_sum']==2.0
    assert summary['costs']['phase_count']==1
    assert summary['by_arm']['native']['formal_costs']['phase_count']==0
    assert summary['by_arm']['review']['formal_costs']['phase_count']==0
    # A second read-only invocation receives its own immutable receipt.
    summary = module.run_review_comparison(review_plan['root'], max_sessions=0)
    assert summary['driver_invocations']==2 and summary['driver_elapsed_seconds_sum']==4.0


def test_native_execution_uses_only_exact_approved_candidate_and_common_clean_contract(review_plan, monkeypatch):
    module = api()
    trial = next(t for t in review_plan['pilots'] if t['arm']=='native')
    calls = []
    def native_host(prompt, workspace, database, **kwargs):
        calls.append((prompt, kwargs))
        if len(calls)==1:
            (workspace/'draft.json').write_text(json.dumps(csv_draft(), ensure_ascii=False))
        else:
            assert set(json.loads(prompt))=={'capability','constraints','execution'}
            assert json.loads(prompt)['execution']['key_columns']==['地区','编号']
            assert 'expected_rows' not in prompt and 'oracle_plan' not in prompt
            assert kwargs['public_contract']=='clean' and 'required_request' not in kwargs
            (workspace/'output.csv').write_text(
                '地区,编号,金额\n华东,001,10\n华西,001,20\n华东,1,30\n')
        result = complete_host_result(review_plan, prompt)
        if len(calls)>1:
            result['public_contract_sha256'] = review_plan['execution_public_contract_sha256']
        return result
    monkeypatch.setattr(module, 'evaluate_native_address_session', lambda *a,**k:{'verified':True})
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    record = module._observe(review_plan, trial, PhaseLedger(Path(review_plan['root'])/'ledger'),
                             native_host, lambda *a,**k:pytest.fail('wrong draft arm'))
    assert record['accepted'] is True and record['execution_kind']=='approved_native'
    assert record['execution_public_contract_sha256']==review_plan['execution_public_contract_sha256']
    assert len(calls)==2
    phases = PhaseLedger(Path(review_plan['root'])/'ledger').records()
    assert sum(p['api_expected'] for p in phases)==2


def test_full_trial_timer_includes_mandatory_gates_outside_narrow_phase_timers(review_plan, monkeypatch):
    module = api()
    trial = next(t for t in review_plan['pilots'] if t['arm']=='native')
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda:clock[0])
    def native_host(prompt, workspace, database, **kwargs):
        clock[0] += 2.0
        (workspace/'draft.json').write_text(json.dumps(csv_draft(wrong_key=True), ensure_ascii=False))
        return complete_host_result(review_plan, prompt)
    def checked_gate(*args, **kwargs):
        clock[0] += 3.0
        return {'verified':True}
    monkeypatch.setattr(module, 'evaluate_native_address_session', checked_gate)
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    record = module._observe(review_plan, trial, PhaseLedger(Path(review_plan['root'])/'ledger'),
                             native_host, lambda *a,**k:pytest.fail('wrong draft arm'))
    module._save(Path(review_plan['root'])/'records'/(trial['trial_id']+'.json'), record)
    assert record['full_trial_elapsed_seconds']==5.0
    summary = module.summarize_review_comparison(review_plan['root'])
    assert summary['by_arm']['native']['costs']['elapsed_seconds_sum']==2.0
    assert summary['by_arm']['native']['full_trial_elapsed_seconds_sum']==5.0
    assert summary['by_arm']['native']['formal_full_trial_elapsed_seconds_sum']==0.0


@pytest.mark.parametrize('alter,error', [
    ('approval','approval_candidate_binding_changed'),
    ('span','public_source_span_changed'),
    ('cost','cost_phase_binding_changed'),
])
def test_offline_verifier_recomputes_inner_bindings_even_when_outer_hash_is_rewritten(review_plan, tmp_path, alter, error):
    module = api()
    trial, proposal = stored_csv_proposal(review_plan)
    review = module.review_fixture_proposal(review_plan, trial, proposal)
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    ledger = PhaseLedger(Path(review_plan['root'])/'ledger')
    ledger.reserve(trial['arm'], 'draft', trial['trial_id']+'-draft')
    ledger.finish(trial['trial_id']+'-draft', {'status':'failed','elapsed_seconds':1.0})
    record = {key:trial[key] for key in ('trial_id','phase','arm','case_id','category')}
    record.update(review, fingerprint=review_plan['sha256'],status='failed',
        session_id='fixture-session',source_sha256=trial['prompt_sha256'])
    module._save(Path(review_plan['root'])/'records'/(trial['trial_id']+'.json'), record)
    destination = tmp_path/'public'
    module.export_review_evidence(review_plan['root'], destination)
    if alter=='cost':
        path = destination/'cost-phases.json'
        document = module._read(path)
        document['records'][-1]['elapsed_seconds'] = 99.0
        # Rewrite the aggregate too: integrity must come from the inner phase.
        from rpe_harness.experiments.cost_ledger import summarize_phases
        summary = module._read(destination/'summary.json')
        summary['costs'] = summarize_phases(document['records'])
        summary.pop('sha256')
        (destination/'summary.json').unlink()
        module._save(destination/'summary.json', summary)
    else:
        path = destination/'summary.json'
        document = module._read(path)
        if alter=='approval':
            document['records'][0]['approval']['proposal_sha256'] = 'a'*64
        else:
            proof = document['records'][0]['public_proposal']
            proof['requirements'][0]['span'] = [0,1]
            from rpe_harness.storage import canonical_json
            proof['public_proof_sha256'] = module._sha(canonical_json({k:v for k,v in proof.items()
                if k!='public_proof_sha256'}).encode())
    document.pop('sha256')
    path.unlink()
    module._save(path, document)
    result = module.verify_review_evidence(destination)
    assert result['verified'] is False and result['errors']==[error]
