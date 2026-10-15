"""No-model checks for the clean comparison and portable public evidence."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from rpe_harness.experiments import address_comparison as api


@pytest.fixture
def clean_plan(tmp_path, monkeypatch):
    skills = tmp_path / 'skills'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx',
                    skills / 'productivity/xlsx')
    # Other development tracks may edit unrelated package modules mid-test.
    # Freeze the real initial fingerprint at this external metadata boundary.
    environment = api._environment(sys.executable,tmp_path/'absent-host')
    monkeypatch.setattr(api,'_environment',lambda *args: environment)
    return api.preregister_address_comparison(tmp_path / 'comparison', skill_root=skills,
        hermes_root=tmp_path / 'absent-host', timeout=60, max_turns=6, profile='clean')


def test_clean_registration_keeps_exact_public_sources_and_original_truth(clean_plan):
    assert clean_plan['profile'] == 'clean'
    assert len(clean_plan['sessions']) == 12
    assert len(clean_plan['pilots']) == 2
    for left, right in zip(clean_plan['sessions'][::2], clean_plan['sessions'][1::2]):
        assert {left['arm'], right['arm']} == {'native', 'clean'}
        assert left['prompt_sha256'] == right['prompt_sha256']
        assert left['input_sha256'] == right['input_sha256']
        text = (Path(left['workspace'])/'prompt.txt').read_text()
        case = next(c for c in clean_plan['cases'] if c['case_id'] == left['case_id'])
        assert text == case['source_raw']
        if case['case_id'] != 'free-text-condition':
            source = json.loads(text)
            assert set(source) == {'capability', 'constraints', 'execution'}
            assert 'skill_binding' not in source['execution']
        assert 'expected_rows' not in text
    assert next(c for c in clean_plan['cases'] if c['case_id']=='json-lossless')['expected_text'].endswith('\n')
    assert next(c for c in clean_plan['cases'] if c['case_id']=='free-text-condition')['expected_missing'] == ['structured_current_request']


def test_clean_budget_keeps_every_failure_without_retry_or_oracle_injection(clean_plan):
    calls = []
    def unavailable(prompt, workspace, db, **kwargs):
        assert 'required_request' not in kwargs
        assert kwargs['max_task_attempts'] == 1
        calls.append((prompt, kwargs))
        raise RuntimeError('transport unavailable')
    api.run_address_comparison(clean_plan['root'], max_sessions=2, runner=unavailable)
    summary = api.run_address_comparison(clean_plan['root'], formal=True, runner=unavailable)
    assert len(calls) == 14
    assert summary['formal_attempts'] == 12
    assert summary['pilot_attempts'] == 2
    assert summary['costs']['failed_phases'] == 14
    assert summary['costs']['total_tokens'] is None
    assert calls[0][0] == calls[1][0]
    assert calls[0][1]['preserve_native_tool_search'] is True
    assert calls[1][1]['entry_mode'] == 'clean'
    api.run_address_comparison(clean_plan['root'], formal=True, runner=unavailable)
    api.run_address_comparison(clean_plan['root'], runner=unavailable)
    assert len(calls) == 14


def test_clean_export_offline_verifies_source_ledger_and_truth_after_raw_state_removed(clean_plan, tmp_path):
    trial = clean_plan['sessions'][0]
    workspace = Path(trial['workspace'])
    (workspace/'output.csv').write_text('地区,编号,金额\n华东,001,10\n华西,001,20\n华东,1,30\n')
    def correct_without_runtime_proof(*args, **kwargs):
        return {'elapsed_seconds':0.1, 'session_id':'public-session', 'events':[],
                'completed':True, 'exit_code':0, 'process_cleanup':{'cleanup_complete':True}}
    api.run_address_comparison(clean_plan['root'], formal=True, max_sessions=1,
                               runner=correct_without_runtime_proof)
    target = tmp_path/'public'
    api.export_address_evidence(clean_plan['root'], target)
    shutil.rmtree(clean_plan['root'])
    verdict = api.verify_clean_evidence(target)
    assert verdict['verified'] is True
    assert verdict['business_artifacts_verified'] == 1
    assert verdict['source_documents_verified'] == 6
    assert verdict['runtime_identity_attestation'] == 'recorded_runtime_checks_only'
    artifact = target/'artifacts'/trial['trial_id']/'output.csv'
    artifact.write_text('wrong business output\n')
    assert api.verify_clean_evidence(target)['verified'] is False


def test_clean_script_preregistration_and_offline_verification_never_launch_models(clean_plan, tmp_path):
    target = tmp_path/'public'
    api.export_address_evidence(clean_plan['root'], target)
    result = subprocess.run([clean_plan['helper_python'],'-B',
        str(Path(__file__).parents[1]/'scripts/v19_clean_comparison.py'),
        'verify','--destination',str(target)], capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)['verified'] is True
    script = str(Path(__file__).parents[1]/'scripts/v19_clean_comparison.py')
    registered = subprocess.run([clean_plan['helper_python'],'-B',script,'preregister',
        '--root',str(tmp_path/'cli-registration'),'--skill-root',clean_plan['skill_root'],
        '--hermes-root',str(tmp_path/'absent-host')],capture_output=True,text=True,check=True)
    assert json.loads(registered.stdout)['profile']=='clean'
    assert len(json.loads(registered.stdout)['sessions'])==12


def test_clean_source_proof_reads_real_state_and_never_exports_a_changed_private_source(clean_plan):
    from rpe_harness.capability_catalog import build_catalog
    from rpe_harness.requirements import source_document, interpret_source_intent, SourceStateStore
    trial = next(t for t in clean_plan['sessions'] if t['arm']=='clean')
    raw = (Path(trial['workspace'])/'prompt.txt').read_text()
    source = source_document(raw,session_id='public-session',source_ref='public-source')
    catalog = build_catalog(db_path=trial['db'],catalog_root=clean_plan['catalog_root'],
                            skill_snapshot_id=clean_plan['skill_snapshot_id'])
    interpreted = interpret_source_intent(source['source_facts'],source_document=source,
        catalog=catalog,workspace=trial['workspace'],session_id='public-session',source_ref='public-source')
    handoff = SourceStateStore(trial['db']).capture(source,interpreted,
        session_id='public-session',source_ref='public-source')
    proof = api._clean_source_proof(clean_plan,trial,{'session_id':'public-session'},
                                   {**handoff,'verified':True})
    assert proof['coverage_verified'] is True
    assert any(r['pointer']=='/execution/key_columns' and r['value']==['地区','编号']
               for r in proof['requirements'])
    # A correct state from another public source cannot authorize this prompt.
    (Path(trial['workspace'])/'prompt.txt').write_text('different public request')
    changed = api._clean_source_proof(clean_plan,trial,{'session_id':'public-session'},
                                     {**handoff,'verified':True})
    assert changed['coverage_verified'] is False
    assert changed['source']['raw']=='different public request'


def test_offline_verifier_recomputes_ledger_even_when_outer_hash_was_rewritten(clean_plan, tmp_path):
    target = tmp_path/'public'
    api.export_address_evidence(clean_plan['root'],target)
    path = target/'source-requirements.json'
    document = api._read(path)
    document['documents'][0]['requirements'][0]['value'] = 'json.normalize'
    document.pop('sha256')
    path.unlink()
    api._save(path,document)
    verdict = api.verify_clean_evidence(target)
    assert verdict['verified'] is False
    assert verdict['errors']==['public_requirement_ledger_changed']


def test_clean_cli_cannot_run_an_old_address_registration(clean_plan, tmp_path):
    old = api.preregister_address_comparison(tmp_path/'old-address',
        skill_root=clean_plan['skill_root'],hermes_root=clean_plan['hermes_root'])
    with pytest.raises(ValueError,match='clean registration'):
        api.main(['run','--root',old['root'],'--max-sessions','0'],profile='clean')


def test_clean_offline_evidence_preserves_interrupted_attempt_as_unknown(clean_plan, tmp_path):
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    trial = clean_plan['pilots'][0]
    PhaseLedger(Path(clean_plan['root'])/'ledger').reserve(trial['arm'],'pilot',trial['trial_id'])
    target = tmp_path/'public'
    exported = api.export_address_evidence(clean_plan['root'],target)
    assert exported['costs']['incomplete_phases']==1
    assert exported['costs']['total_tokens'] is None
    assert api.verify_clean_evidence(target)['verified'] is True


def test_clean_cost_ledger_counts_complete_trial_beyond_host_session(clean_plan, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(api.time,'monotonic',lambda: clock[0])
    scorer = api.score_address_artifact
    def checked_artifact(plan, trial):
        verdict = scorer(plan,trial)
        clock[0] += 3.0  # Independent controller/oracle work after the host returns.
        return verdict
    monkeypatch.setattr(api,'score_address_artifact',checked_artifact)
    def host(*args, **kwargs):
        clock[0] += 2.0  # Launcher setup plus the one-second host session.
        return {'elapsed_seconds':1.0,'session_id':'no-model-session','events':[]}
    summary = api.run_address_comparison(clean_plan['root'],max_sessions=1,runner=host)
    record = summary['records'][0]
    assert record['elapsed_seconds']==5.0
    assert record['session_elapsed_seconds']==1.0
    assert summary['costs']['elapsed_seconds_sum']==5.0

