"""Finite 1.11 experiment tests: real local boundaries, no provider calls."""
import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest


@pytest.fixture
def api():
    return importlib.import_module('rpe_harness.experiments.review_v111_comparison')


@pytest.fixture
def plan(api, tmp_path, monkeypatch):
    skills = tmp_path/'skills'
    shutil.copytree(Path(__file__).parent/'fixtures/skills/productivity/xlsx',
                    skills/'productivity/xlsx')
    monkeypatch.setattr(api, '_environment', lambda *a: {'source_sha256': {'fixture': 'frozen'}})
    return api.preregister_v111_comparison(tmp_path/'owned', skill_root=skills,
        hermes_root=tmp_path/'absent-host', helper_python=sys.executable)


def test_finite_pairs_share_business_input_but_not_answers(plan):
    assert len(plan['pilots']) == 2 and len(plan['sessions']) == 8
    assert plan['max_draft_sessions'] == 10
    assert plan['max_native_execution_sessions'] == 5
    for trial in plan['sessions']:
        raw = (Path(trial['workspace'])/'prompt.txt').read_text()
        case = next(c for c in plan['cases'] if c['case_id'] == trial['case_id'])
        assert raw == case['task'] and not raw.startswith('{')
        assert 'expected_rows' not in raw and 'oracle_plan' not in raw
    assert sum(c['use_skills'] for c in plan['cases']) == 1
    assert {c['case_id'] for c in plan['cases'] if c['sources']} == {'source-key', 'missing-key'}


def test_business_review_accepts_omitted_redundant_preservation_flags(api, plan):
    case = plan['cases'][0]
    candidate = {'capability': 'tabular.deduplicate', 'constraints': [],
        'execution': {'input_path': 'input.csv', 'output_path': 'output.csv',
                      'key_columns': ['订单', '店铺'], 'encoding': 'utf-8'},
        'requirements': [], 'unresolved': []}
    assert api.semantic_decision(case, candidate)['faithful'] is True
    candidate['execution']['key_columns'] = ['订单']
    assert api.semantic_decision(case, candidate)['faithful'] is False


def test_missing_business_key_is_never_approved(api, plan):
    case = plan['cases'][-1]
    draft = {'capability': 'tabular.deduplicate', 'constraints': [],
             'execution': {'input_path': 'input.csv', 'output_path': 'output.csv',
                           'encoding': 'utf-8'}, 'requirements': [],
             'unresolved': ['还需决定业务唯一标识']}
    decision = api.semantic_decision(case, draft)
    assert decision == {'faithful': True, 'probe_correct': True, 'approve': False}
    draft['execution']['key_columns'] = ['姓名']
    assert api.semantic_decision(case, draft)['probe_correct'] is False


def test_public_cli_runner_uses_module_entry_and_preserves_observations(api, plan):
    calls = []
    trial = next(t for t in plan['pilots'] if t['arm'] == 'review')
    def execute(command, **kwargs):
        calls.append(command)
        assert command[:5] == [sys.executable, '-B', '-m', 'rpe_harness', 'review']
        assert command[5] == 'draft' and '--json' in command
        assert '--owner-root' in command and '--read-isolation-root' in command
        return type('Completed', (), {'returncode': 0, 'stdout': json.dumps({
            'status': 'needs_context', 'host_observation': {'usage': None,
                'session_id': 'native-session'}, 'proposal_verification': {'verified': False}}),
            'stderr': ''})()
    result = api.public_review_draft(plan, trial, subprocess_runner=execute)
    assert len(calls) == 1 and result['host_observation']['session_id'] == 'native-session'


def test_failure_attempts_are_reserved_once_and_usage_stays_unknown(api, plan):
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('PRIVATE_PROVIDER_ERROR')
    api.run_v111_comparison(plan['root'], native_runner=fail, public_runner=fail)
    api.freeze_formal(plan['root'])
    result = api.run_v111_comparison(plan['root'], formal=True,
        native_runner=fail, public_runner=fail)
    api.run_v111_comparison(plan['root'], formal=True,
        native_runner=fail, public_runner=fail)
    assert len(calls) == 10
    assert result['formal_attempts'] == 8 and result['pilot_attempts'] == 2
    assert result['costs']['total_tokens'] is None
    assert result['native_execution_sessions'] == 0
    assert 'PRIVATE_PROVIDER_ERROR' not in json.dumps(result)


def test_export_verifies_after_owned_root_removal_and_detects_source_tamper(api, plan, tmp_path):
    destination = tmp_path/'public'
    api.export_v111_evidence(plan['root'], destination)
    shutil.rmtree(plan['root'])
    result = api.verify_v111_evidence(destination)
    assert result['verified'] is True and result['source_documents_verified'] == 5
    (destination/'prompts'/'source-key.txt').write_text('changed')
    assert api.verify_v111_evidence(destination)['verified'] is False


def test_actual_public_show_confirm_run_produces_business_artifact_and_portable_proof(api, plan, tmp_path):
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    from rpe_harness.proposal import create_proposal
    trial = next(t for t in plan['sessions'] if t['case_id'] == 'csv-complete' and t['arm'] == 'review')
    case = api._case(plan, trial)
    candidate = {'capability': case['capability'], 'constraints': [],
        'execution': {'input_path': 'input.csv', 'output_path': 'output.csv',
                      'key_columns': ['店铺', '订单'], 'encoding': 'utf-8'},
        'requirements': [{'text': 'review exactly the submitted scope', 'quote': case['task'],
            'fields': ['/capability', '/constraints', '/execution/input_path',
                '/execution/output_path', '/execution/key_columns', '/execution/encoding']}],
        'unresolved': []}
    proposal = create_proposal(case['task'], candidate, db_path=trial['db'],
        workspace=trial['workspace'], session_id='fixture-session', source_ref='explicit-public-source')
    assert proposal['status'] == 'awaiting_confirmation'
    observation = {'session_id': 'fixture-session', 'completed': True, 'exit_code': 0,
        'process_cleanup': {'cleanup_complete': True},
        'original_prompt_sha256': trial['prompt_sha256'], 'prompt_sha256': trial['prompt_sha256'],
        'catalog_snapshot_id': None, 'public_contract_sha256': plan['public_contract_sha256'],
        'model_identity': {'provider': 'fixture', 'requested_model': 'fixture', 'response_models': ['fixture']},
        'usage': {'api_requests': 1, 'coverage_complete': True, 'total_tokens': 7,
                  'known_total_tokens': 7, 'missing_observations': 0}}
    result = {**proposal, 'host_observation': observation,
        'proposal_verification': {'verified': True, 'proposal_verified': True}}
    record = api._observe(plan, trial, PhaseLedger(Path(plan['root'])/'ledger'),
        lambda *a, **k: pytest.fail('deterministic execution invoked native model'), lambda *a: result)
    assert record['accepted'] is True and record['execution_verified'] is True
    assert record['execution_kind'] == 'approved_public_controller'
    assert record['approval']['actor'] == 'simulated_semantic_reviewer'
    api._save(Path(plan['root'])/'records'/(trial['trial_id']+'.json'), record)
    destination = tmp_path/'public'
    api.export_v111_evidence(plan['root'], destination)
    verified = api.verify_v111_evidence(destination)
    assert verified['verified'] is True and verified['business_artifacts_verified'] == 1
    shutil.rmtree(plan['root'])
    assert api.verify_v111_evidence(destination)['verified'] is True
    (destination/'artifacts'/trial['trial_id']/'output.csv').write_text('tampered\n')
    assert api.verify_v111_evidence(destination)['verified'] is False


def test_pilot_requires_cold_skill_and_context_but_never_supplies_business_gold(api, plan):
    pilot = plan['pilot_case']
    assert pilot['use_skills'] and pilot['sources'][0]['facts'] == {'mode': 'rows', 'sheet': '采购'}
    assert 'rows' not in pilot['task'] and '采购' not in pilot['task']
    trial = next(t for t in plan['pilots'] if t['arm'] == 'native')
    prompt = api._native_prompt(plan, trial)
    assert 'source_ref' in prompt and 'quotes' in prompt
    assert 'expected' not in prompt and 'oracle_plan' not in prompt
    assert api._native_prompt(plan, trial) == prompt


def test_native_trace_cannot_relabel_attachment_as_current_user_quote(api, plan):
    case = next(c for c in plan['cases'] if c['case_id'] == 'source-key')
    draft = {'execution': {'key_columns': ['客户', '单据']},
        'requirements': [{'text': 'key', 'quote': '客户与单据',
            'fields': ['/capability', '/constraints', '/execution/key_columns']}]}
    assert api.native_draft_trace(case, draft) is False


def test_export_rechecks_inner_cost_receipts_when_outer_hash_is_rewritten(api, plan, tmp_path):
    destination = tmp_path/'public'
    api.export_v111_evidence(plan['root'], destination)
    path = destination/'cost-phases.json'
    phases = api._read(path)
    phases['records'][0]['elapsed_seconds'] += 1
    path.unlink()
    api._save(path, {k: v for k, v in phases.items() if k != 'sha256'})
    assert api.verify_v111_evidence(destination)['verified'] is False


def test_native_trace_rejects_forged_or_cross_attachment_reference(api, plan):
    trial = next(t for t in plan['sessions'] if t['case_id'] == 'source-key' and t['arm'] == 'native')
    case = api._case(plan, trial)
    bindings = api._native_context(plan, trial)['model_sources']
    quote = bindings[0]['quotes']['key_columns']
    candidate = {'execution': {'key_columns': ['客户', '单据']},
        'constraints': [], 'requirements': [
            {'text': 'scope', 'quote': case['task'], 'fields': ['/capability', '/constraints']},
            {'text': 'recovered key', 'quote': quote, 'fields': ['/execution/key_columns'],
             'source_ref': bindings[0]['source_ref']}]}
    assert api.native_draft_trace(case, candidate, bindings) is True
    candidate['requirements'][1]['source_ref'] = {'FAKE': 'not a SourceRef'}
    assert api.native_draft_trace(case, candidate, bindings) is False
    candidate['requirements'][1]['source_ref'] = {**bindings[0]['source_ref'], 'sha256': '0'*64}
    assert api.native_draft_trace(case, candidate, bindings) is False
    candidate['requirements'][1]['source_ref'] = bindings[0]['source_ref']
    assert api.native_draft_trace(case, candidate, []) is False


def test_negative_unresolved_text_is_preserved_for_identical_offline_scoring(api, plan):
    case = plan['cases'][-1]
    draft = {'capability': 'tabular.deduplicate', 'constraints': [],
        'execution': {'input_path': 'input.csv', 'output_path': 'output.csv',
            'encoding': 'utf-8', 'delimiter': ',', 'keep': 'first'},
        'requirements': [], 'unresolved': ['需要补充信息']}
    live = api.semantic_decision(case, draft)
    proof = api._proof(draft)
    assert proof['unresolved'] == ['需要补充信息']
    hashes = proof.pop('unresolved_sha256')
    assert hashes == [api._sha('需要补充信息'.encode())]
    assert api.semantic_decision(case, proof) == live
    assert live['probe_correct'] is False


def test_native_recovered_key_must_not_be_relabelled_default(api, plan):
    trial = next(t for t in plan['sessions'] if t['case_id'] == 'source-key' and t['arm'] == 'native')
    case = api._case(plan, trial)
    bindings = api._native_context(plan, trial)['model_sources']
    draft = {'execution': {'key_columns': ['客户', '单据']}, 'constraints': [],
        'requirements': [{'text': 'key recovered but source omitted', 'quote': None,
             'fields': ['/capability', '/constraints', '/execution/key_columns']}]}
    assert api.native_draft_trace(case, draft, bindings) is False
