"""The fresh value-evidence comparison stays finite and independently exportable."""
import copy
import importlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from rpe_harness.experiments import review_v111_comparison as shared


@pytest.fixture
def plan(tmp_path, monkeypatch):
    skills = tmp_path/'skills'
    shutil.copytree(Path(__file__).parent/'fixtures/skills/productivity/xlsx', skills/'productivity/xlsx')
    monkeypatch.setattr(shared, '_environment', lambda *args: {
        'source_sha256': {'experiments/review_v111_comparison.py': 'b'*64},
        'helper_python_identity': {'executable_sha256': 'a'*64}})
    from rpe_harness.integrations import hermes_session
    monkeypatch.setattr(hermes_session, '_installed_python', lambda root: root/'.venv/bin/python')
    return shared.preregister_v111_comparison(tmp_path/'owned', skill_root=skills,
        hermes_root=tmp_path/'absent-host', helper_python=sys.executable, profile='review-v115')


def api():
    return importlib.import_module('rpe_harness.experiments.review_v115_comparison')


def test_registration_uses_three_new_pairs_without_pilot_or_external_execution(plan):
    assert len(plan['cases']) == 3 and len(plan['sessions']) == 6
    assert plan['pilots'] == [] and plan['pilot_case'] is None
    assert plan['max_draft_sessions'] == 6 and plan['max_native_execution_sessions'] == 2
    assert plan['max_external_native_execution_sessions'] == 0
    assert plan['max_total_native_execution_sessions'] == 2
    assert plan['independent_audit_budget']['cli_processes'] == 1
    assert plan['review_profile'] == 'values-v1'
    assert plan['native_stdout_name'] == '.rpe-xlsx-stage.json'
    assert [case['case_id'] for case in plan['cases']] == [
        'dispatch-tabs', 'cold-inventory', 'pending-account-key']
    assert plan['cases'][0]['parameters']['delimiter'] == '\t'
    assert plan['cases'][1]['parameters'] == {'mode': 'rows', 'sheet': '盘存'}
    assert plan['cases'][2]['category'] == 'contract_probe'


def test_public_profile_and_native_instruction_are_same_value_contract(plan):
    from rpe_harness.review_values import VALUE_EVIDENCE_CONTRACT
    trial = next(row for row in plan['sessions'] if row['arm'] == 'native')
    calls = []
    shared._native_draft(plan, trial, lambda prompt, *args, **kwargs: calls.append((prompt, kwargs)) or {})
    prompt, kwargs = calls[0]
    assert VALUE_EVIDENCE_CONTRACT in prompt
    assert 'expected_rows' not in prompt and 'oracle_plan' not in prompt
    assert kwargs['request_budget'] == plan['request_budget']
    assert kwargs['native_finish_enabled'] is True
    assert kwargs['public_contract'] == 'review'
    assert 'review_profile' not in kwargs
    commands = []
    def execute(command, **kwargs):
        commands.append(command)
        return type('Completed', (), {'returncode': 0, 'stdout': '{}', 'stderr': ''})()
    shared.public_review_draft(plan, {**trial, 'arm': 'review'}, subprocess_runner=execute)
    command = commands[0]
    assert command[command.index('--review-profile')+1] == 'values-v1'
    assert command[command.index('--request-budget')+1] == plan['request_budget_path']
    assert api().public_contract_sha(plan, 'review') != api().public_contract_sha(plan, 'native')


def test_new_profile_never_replays_failed_formal_sessions(plan):
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('private diagnostic')
    shared.freeze_formal(plan['root'])
    first = shared.run_v111_comparison(plan['root'], formal=True, native_runner=fail, public_runner=fail)
    second = shared.run_v111_comparison(plan['root'], formal=True, native_runner=fail, public_runner=fail)
    assert len(calls) == 6 and first['formal_attempts'] == second['formal_attempts'] == 6
    assert first['pilot_attempts'] == 0 and first['costs']['total_tokens'] is None
    assert all(row['source_preserved'] and not row['accepted'] for row in first['records'])
    with pytest.raises(ValueError):
        shared.run_v111_comparison(plan['root'], formal=True, max_sessions=7,
                                    native_runner=fail, public_runner=fail)


def test_native_value_gate_uses_real_prepared_context_and_portable_content_hash(plan):
    trial = next(row for row in plan['sessions'] if row['arm'] == 'native' and row['case_id'] == 'cold-inventory')
    draft = {'capability': 'xlsx.inspect', 'constraints': [], 'execution': {'mode': 'rows'},
        'requirements': [], 'unresolved': []}
    context = shared._native_context(plan, trial)
    draft['requirements'] = [{'text': '按附件读取行。', 'fields': ['/execution/mode'],
        'quote_ref': context['model_sources'][0]['quote_refs']['mode']}]
    evidence, sources = api().parameter_value_check(plan, trial, draft)
    assert evidence['status'] == 'ready'
    assert evidence['witnesses'][0]['origin'] == 'context_source'
    assert sources[0]['payload']['text'] == '本次盘存检查决定：mode=rows；sheet=盘存。'
    assert sources[0]['session_id'] == 'native-context-'+trial['trial_id']
    assert sources[0]['sha256'] == shared._sha(json.dumps(
        {key: value for key, value in sources[0].items() if key != 'sha256'},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    changed = copy.deepcopy(draft)
    changed['execution']['mode'] = 'sheets'
    assert api().parameter_value_check(plan, trial, changed)[0]['status'] == 'needs_context'


def test_native_execution_stage_configuration_preserves_exact_candidate_json(plan):
    trial = next(row for row in plan['sessions'] if row['arm'] == 'native' and row['case_id'] == 'cold-inventory')
    options = api().native_execution_options(plan, trial)
    assert options['native_execution_evidence']['stdout_name'] == '.rpe-xlsx-stage.json'
    assert options['native_execution_evidence']['parameters'] == {'mode': 'rows', 'sheet': '盘存'}
    draft = {'capability': 'xlsx.inspect', 'constraints': ['preserve_original'],
             'execution': {'input_path': 'input.xlsx', 'output_path': 'output.json', 'mode': 'rows', 'sheet': '盘存'}}
    assert json.loads(api().native_execution_prompt(plan, trial, draft)) == draft


def test_portable_evidence_keeps_new_finite_budget_after_owned_cleanup(plan, tmp_path):
    destination = tmp_path/'public'
    shared.export_v111_evidence(plan['root'], destination)
    shutil.rmtree(plan['root'])
    assert api().verify_v115_evidence(destination)['verified'] is True
    assert shared.verify_v111_evidence(destination)['verified'] is False
    assert json.loads((destination/'request-budget.json').read_text()) == plan['request_budget']
    path = destination/'preregistration.json'
    value = shared._read(path)
    value.pop('sha256')
    value['max_draft_sessions'] = 7
    path.unlink()
    shared._save(path, value)
    assert api().verify_v115_evidence(destination)['verified'] is False


def test_six_comparable_pairs_mark_ready_without_accepting_failed_business(plan):
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    ledger = PhaseLedger(Path(plan['root'])/'ledger')
    identity = {'provider': 'xiaomi', 'requested_model': 'mimo-v2.6-pro', 'response_models': ['mimo-v2.6-pro']}
    for trial in plan['sessions']:
        attempt = trial['trial_id']+'-draft'
        ledger.reserve(trial['arm'], 'draft', attempt)
        ledger.finish(attempt, {'status': 'failed', 'elapsed_seconds': 0})
        shared._save(Path(plan['root'])/'records'/(trial['trial_id']+'.json'), {
            **{key: trial[key] for key in ('trial_id', 'phase', 'arm', 'case_id', 'category')},
            'fingerprint': plan['sha256'], 'comparable': True, 'status': 'failed',
            'approved': False, 'accepted': False, 'model_identity': identity})
    summary = shared.summarize_v111_comparison(plan['root'])
    assert summary['comparison_ready'] is True
    assert all(row['complete_chain'] == 0 for row in summary['by_arm'].values())


def test_parameter_proof_cannot_be_relabelled_after_source_or_value_change(plan):
    trial = next(row for row in plan['sessions'] if row['arm'] == 'native' and row['case_id'] == 'dispatch-tabs')
    case = shared._case(plan, trial)
    draft = {'capability': case['capability'], 'constraints': [],
        'execution': {'encoding': 'utf-8-sig'}, 'requirements': [{
            'text': '输出UTF-8。', 'fields': ['/execution/encoding'], 'quote': 'UTF-8'}], 'unresolved': []}
    evidence, sources = api().parameter_value_check(plan, trial, draft)
    record = {'trial_id': trial['trial_id'], 'arm': 'native', 'candidate_proof': {
        **draft, 'unresolved_sha256': []}, 'parameter_value_evidence': evidence,
        'parameter_sources': sources, 'draft_faithful': False, 'probe_correct': False, 'accepted': False}
    api().verify_record_values(plan, trial, record, draft)
    record['parameter_value_evidence']['status'] = 'ready'
    with pytest.raises(ValueError, match='parameter_value_evidence_changed'):
        api().verify_record_values(plan, trial, record, draft)


def test_malformed_portable_source_is_a_readable_rejection(plan):
    trial = next(row for row in plan['sessions'] if row['arm'] == 'native')
    draft = {'capability': 'tabular.deduplicate', 'constraints': [], 'execution': {},
             'requirements': [], 'unresolved': []}
    evidence, sources = api().parameter_value_check(plan, trial, draft)
    sources[0]['payload'] = []
    record = {'parameter_sources': sources, 'parameter_value_evidence': evidence}
    with pytest.raises(ValueError, match='parameter_source'):
        api().verify_record_values(plan, trial, record, draft)


@pytest.mark.parametrize('arm', ['native', 'review'])
@pytest.mark.parametrize('misbound', [False, True])
def test_observe_applies_literal_gate_before_approval_for_both_arms(plan, monkeypatch, arm, misbound):
    from rpe_harness.review_context import prepare_context
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    from rpe_harness.experiments import review_native_evidence
    trial = next(row for row in plan['sessions'] if row['arm'] == arm and row['case_id'] == 'dispatch-tabs')
    case = shared._case(plan, trial)
    session = 'synthetic-'+arm
    context = (shared._native_context(plan, trial) if arm == 'native' else
               prepare_context(trial['db'], case['sources'], session))
    draft = {'capability': case['capability'], 'constraints': ['preserve_original', 'preserve_order'],
        'execution': {'input_path': 'input.csv', 'output_path': 'output.csv', **case['parameters']},
        'requirements': [
            {'text': '当前去重任务。', 'quote': case['task'], 'fields': [
                '/capability', '/constraints', '/execution/input_path', '/execution/output_path', '/execution/keep']},
            {'text': '附件业务键。', 'quote_ref': context['model_sources'][0]['quote_refs']['key_columns'],
             'fields': ['/execution/key_columns']},
            {'text': '保留编码。', 'quote': 'UTF-8', 'fields': ['/execution/encoding']},
            {'text': '保留分隔符。', 'quote': 'UTF-8' if misbound else 'TAB', 'fields': ['/execution/delimiter']}],
        'unresolved': []}
    identity = {'provider': 'xiaomi', 'requested_model': 'mimo-v2.6-pro', 'response_models': ['mimo-v2.6-pro']}
    prompt = shared._native_prompt(plan, trial) if arm == 'native' else case['task']
    observation = {'session_id': session, 'completed': True, 'exit_code': 0,
        'process_cleanup': {'cleanup_complete': True}, 'model_identity': identity,
        'original_prompt_sha256': shared._sha(prompt.encode()), 'prompt_sha256': shared._sha(prompt.encode()),
        'catalog_snapshot_id': None, 'public_contract_sha256': api().public_contract_sha(plan, arm),
        'events': [{'kind': 'pre_api_request', 'request_max_completion_tokens': 2400,
                    'request_thinking_type': 'disabled'}]}
    monkeypatch.setattr(review_native_evidence, 'native_chain_diagnostics',
                        lambda *args: {'durable_observations_match': True})
    monkeypatch.setattr(review_native_evidence, 'evaluate_native_review_draft',
                        lambda *args: {'verified': True})
    approvals = []
    monkeypatch.setattr(shared, '_approve_and_execute', lambda *args: approvals.append(args[1]['trial_id']))
    def native(*args, **kwargs):
        (Path(trial['workspace'])/'draft.json').write_text(json.dumps(draft, ensure_ascii=False))
        return observation
    proposal = {'draft': draft, 'review_context': context,
        'proposal_verification': {'verified': True, 'proposal_verified': True}, 'host_observation': observation}
    record = shared._observe(plan, trial, PhaseLedger(Path(plan['root'])/'ledger'), native,
                             lambda *args: proposal)
    assert record['comparable'] is True
    assert record['draft_invocation_verified'] is True
    assert record['draft_faithful'] is (not misbound)
    assert record['parameter_value_evidence']['status'] == ('needs_context' if misbound else 'ready')
    assert approvals == ([] if misbound else [trial['trial_id']])
    api().verify_record_values(plan, trial, record, draft)


def test_native_execution_dispatch_uses_staged_gate_and_matching_system_contract(plan, monkeypatch):
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    from rpe_harness.experiments import review_native_evidence
    trial = next(row for row in plan['sessions'] if row['arm'] == 'native' and row['case_id'] == 'cold-inventory')
    case = shared._case(plan, trial)
    draft = {'capability': 'xlsx.inspect', 'constraints': ['preserve_original'],
             'execution': {'input_path': 'input.xlsx', 'output_path': 'output.json', **case['parameters']}}
    prompt = shared._json(draft)
    identity = {'provider': 'xiaomi', 'requested_model': 'mimo-v2.6-pro', 'response_models': ['mimo-v2.6-pro']}
    observation = {'completed': True, 'exit_code': 0, 'model_identity': identity,
        'process_cleanup': {'cleanup_complete': True}, 'original_prompt_sha256': shared._sha(prompt.encode()),
        'public_contract_sha256': api().execution_contract_sha(plan, trial),
        'events': [{'kind': 'pre_api_request', 'request_max_completion_tokens': 2400,
                    'request_thinking_type': 'disabled'}]}
    monkeypatch.setattr(review_native_evidence, 'native_chain_diagnostics',
                        lambda *args: {'durable_observations_match': True})
    monkeypatch.setattr(review_native_evidence, 'evaluate_native_review_execution_v114',
                        lambda *args: pytest.fail('v115 must not dispatch through the direct-stdout v114 gate'))
    gates, calls = [], []
    def gate(result, observed_plan, observed_trial):
        gates.append((observed_plan['native_stdout_name'], observed_trial['trial_id']))
        return {'verified': True, 'status': 'accepted', 'receipt_authenticated': True,
                'proof_kind': 'authenticated_darwin_kernel_process_exit', 'kernel_exit_observed': True,
                'process_returncode': 0,
                'artifact_binding': 'same_call_final_bytes_equal_observed_stdout_not_exclusive_writer'}
    monkeypatch.setattr(review_native_evidence, 'evaluate_native_review_execution_v115', gate)
    def native(actual_prompt, *args, **kwargs):
        calls.append((actual_prompt, kwargs))
        return observation
    record = {'model_identity': identity, 'native_chain_diagnostics': {}, 'request_budget_observations': {}}
    shared._approve_and_execute(plan, trial, PhaseLedger(Path(plan['root'])/'ledger'), {}, draft, record, native)
    assert gates == [('.rpe-xlsx-stage.json', trial['trial_id'])]
    assert json.loads(calls[0][0]) == draft
    assert calls[0][1]['native_execution_evidence']['stdout_name'] == '.rpe-xlsx-stage.json'
    assert calls[0][1]['native_finish_enabled'] is True
    assert calls[0][1]['request_budget'] == plan['request_budget']
    assert record['execution_verified'] is True
    assert record['execution_gate']['artifact_binding'].startswith('same_call_final_bytes_equal_observed_stdout')


def test_native_execution_budget_rejects_third_launch_without_consuming_extra_slot(plan):
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    ledger = PhaseLedger(Path(plan['root'])/'ledger')
    for index in range(2):
        attempt = 'consumed-'+str(index)
        ledger.reserve('native', 'execute', attempt)
        ledger.finish(attempt, {'status': 'failed', 'elapsed_seconds': 0})
    trial = next(row for row in plan['sessions'] if row['arm'] == 'native')
    record = {}
    shared._approve_and_execute(plan, trial, ledger, {}, {}, record,
        lambda *args, **kwargs: pytest.fail('a third native execution must never launch'))
    assert record == {'error_code': 'native_execution_budget_exhausted'}
    assert sum(row['phase'] == 'execute' for row in ledger.records()) == 2
