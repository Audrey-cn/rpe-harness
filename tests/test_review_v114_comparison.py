"""The bounded 1.14 profile reuses the public runner without changing old scores."""
import copy
import importlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from rpe_harness.experiments import review_v111_comparison as shared


def make_plan(tmp_path, monkeypatch):
    skills = tmp_path/'skills'
    shutil.copytree(Path(__file__).parent/'fixtures/skills/productivity/xlsx',
                    skills/'productivity/xlsx')
    monkeypatch.setattr(shared, '_environment', lambda *a: {
        'source_sha256': {'experiments/review_v111_comparison.py': 'b'*64},
        'helper_python_identity': {'executable_sha256': 'a'*64}})
    from rpe_harness.integrations import hermes_session
    monkeypatch.setattr(hermes_session, '_installed_python', lambda root: root/'.venv/bin/python')
    return shared.preregister_v111_comparison(tmp_path/'owned', skill_root=skills,
        hermes_root=tmp_path/'absent-host', helper_python=sys.executable, profile='review-v114')


def test_v114_registration_freezes_fresh_population_and_owned_request_budget(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, monkeypatch)
    assert len(plan['sessions']) == 8 and len(plan['pilots']) == 2
    assert plan['max_draft_sessions'] == 10
    assert plan['max_native_execution_sessions'] == 4
    assert plan['max_external_native_execution_sessions'] == 1
    assert plan['max_total_native_execution_sessions'] == 5
    assert [case['case_id'] for case in plan['cases']] == [
        'invoice-ledger', 'cold-quality', 'depot-tabs', 'pending-member-key']
    assert plan['pilot_case']['case_id'] == 'pilot-cold-recheck'
    assert plan['pilot_case']['input_sha256'] != plan['cases'][1]['input_sha256']
    assert plan['cases'][2]['parameters']['delimiter'] == '\t'
    expected = {'schema_version': 'rpe.hermes-request-budget/v1', 'provider': 'xiaomi',
        'model': 'mimo-v2.6-pro', 'max_completion_tokens': 2400, 'thinking_type': 'disabled',
        'request_timeout_seconds': 90, 'stale_timeout_seconds': 90}
    assert plan['request_budget'] == expected
    path = Path(plan['request_budget_path'])
    assert path == Path(plan['root'])/'request-budget.json'
    assert json.loads(path.read_text()) == expected
    assert shared._sha(path.read_bytes()) == plan['request_budget_sha256']
    assert plan['independent_audit_budget']['cli_processes'] == 2
    assert plan['independent_audit_budget']['profile'] == 'structured-v2'
    assert plan['independent_audit_budget']['requested_max_output_tokens'] == 2400


def test_v114_native_and_public_draft_transport_the_same_budget_without_gold(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, monkeypatch)
    calls = []
    def native(prompt, workspace, db, **kwargs):
        calls.append((prompt, kwargs))
        return {}
    for trial in (plan['sessions'][0], plan['pilots'][0]):
        shared._native_draft(plan, trial, native)
    assert all(kwargs['request_budget'] == plan['request_budget'] and
               kwargs['native_finish_enabled'] is True for _, kwargs in calls)
    assert all(kwargs['native_evidence']['draft_name'] == 'draft.json' for _, kwargs in calls)
    assert all(Path(shared.__file__).with_name('review_v114_comparison.py') in kwargs['deny_read_roots']
               for _, kwargs in calls)
    assert all('expected_rows' not in prompt and 'oracle_plan' not in prompt for prompt, _ in calls)
    commands = []
    def execute(command, **kwargs):
        commands.append(command)
        return type('Completed', (), {'returncode': 0, 'stdout': '{}', 'stderr': ''})()
    shared.public_review_draft(plan, plan['pilots'][1], subprocess_runner=execute)
    assert commands[0][commands[0].index('--request-budget')+1] == plan['request_budget_path']


@pytest.mark.parametrize('case_id,cold', [('invoice-ledger', False), ('pilot-cold-recheck', True)])
def test_v114_native_prompt_appends_runtime_availability_without_rewriting_sources(tmp_path, monkeypatch, case_id, cold):
    plan = make_plan(tmp_path, monkeypatch)
    trial = next(t for t in plan['sessions']+plan['pilots']
                 if t['arm'] == 'native' and t['case_id'] == case_id)
    case = shared._case(plan, trial)
    before = copy.deepcopy(case)
    previous_prompt = shared._native_prompt({**plan, 'profile': 'review-v113'}, trial)
    current = shared._native_prompt(plan, trial)
    marker = '\n\nNative runtime availability for this arm: '
    assert marker in current
    assert current.split(marker)[0] == previous_prompt
    note = current.split(marker)[1]
    assert 'rpe_propose and all rpe_* tools are unavailable' in note
    assert 'write draft.json directly with native tools' in note
    assert ('restore_command' in note) is cold
    if cold:
        assert 'explicitly requested by original_request' in note
        assert 'permitted runtime preparation' in note
        assert 'not business-task execution or dependency installation' in note
    assert current.startswith(case['task']+'\n\n')
    assert case == before


def test_v114_execution_dispatch_keeps_xlsx_process_gate_and_observes_csv_io(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, monkeypatch)
    api = importlib.import_module('rpe_harness.experiments.review_v114_comparison')
    csv_trial = next(t for t in plan['sessions'] if t['case_id'] == 'invoice-ledger')
    assert api.native_execution_options(plan, csv_trial) == {
        'native_evidence': {'artifact_name': 'output.csv'}}
    xlsx_trial = next(t for t in plan['sessions'] if t['case_id'] == 'cold-quality')
    xlsx = api.native_execution_options(plan, xlsx_trial)
    assert set(xlsx) == {'native_execution_evidence'}
    assert xlsx['native_execution_evidence']['parameters'] == {'mode': 'rows', 'sheet': '质检'}
    assert xlsx['native_execution_evidence']['helper_python_sha256'] == 'a'*64


@pytest.mark.parametrize('encoding,faithful', [('UTF-8', True), ('utf8', True),
    ('utf_8', True), ('utf-8-sig', False), ('UTF-16', False), ('unknown-codec', False)])
def test_utf8_alias_equivalence_is_exclusive_to_v114(tmp_path, monkeypatch, encoding, faithful):
    plan = make_plan(tmp_path, monkeypatch)
    case = plan['cases'][0]
    candidate = {'capability': case['capability'], 'constraints': [],
        'execution': {'input_path': case['input_name'], 'output_path': case['output_name'],
            **case['parameters'], 'encoding': encoding}, 'requirements': [], 'unresolved': []}
    original = copy.deepcopy(candidate)
    assert shared.semantic_decision(case, candidate, profile='review-v114')['faithful'] is faithful
    assert shared.semantic_decision(case, candidate)['faithful'] is False
    assert candidate == original


def test_v114_failed_attempts_retain_actual_diagnostics_without_replay(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, monkeypatch)
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('private failure')
    shared.run_v111_comparison(plan['root'], native_runner=fail, public_runner=fail)
    shared.freeze_formal(plan['root'])
    summary = shared.run_v111_comparison(plan['root'], formal=True,
        native_runner=fail, public_runner=fail)
    shared.run_v111_comparison(plan['root'], formal=True, native_runner=fail, public_runner=fail)
    assert len(calls) == 10
    assert summary['pilot_attempts'] == 2 and summary['formal_attempts'] == 8
    assert summary['costs']['total_tokens'] is None
    assert all('draft' in row['native_chain_diagnostics'] for row in summary['records'])
    assert all(row['request_budget_observations']['draft']['conformant'] is False
               and row['comparable'] is False for row in summary['records'])


def test_v114_freeze_rejects_budget_file_tampering_before_any_host(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, monkeypatch)
    Path(plan['request_budget_path']).write_text('{}')
    with pytest.raises(ValueError, match='request_budget'):
        shared.run_v111_comparison(plan['root'],
            native_runner=lambda *a, **k: pytest.fail('host invoked after budget changed'),
            public_runner=lambda *a, **k: pytest.fail('host invoked after budget changed'))


def test_v114_budget_observation_requires_actual_durable_request_fields():
    api = importlib.import_module('rpe_harness.experiments.review_v114_comparison')
    observation = {'events': [{'kind': 'pre_api_request',
        'request_max_completion_tokens': 2400, 'request_thinking_type': 'disabled'}]}
    diagnostic = {'durable_observations_match': True}
    proof = api.request_budget_observation(observation, diagnostic)
    assert proof['conformant'] is True and proof['api_requests'] == 1
    assert proof['requests'] == [{'max_completion_tokens': 2400, 'thinking_type': 'disabled'}]
    assert api.request_budget_observation(observation, {})['conformant'] is False
    observation['events'].append({'kind': 'pre_api_request'})
    assert api.request_budget_observation(observation, diagnostic)['conformant'] is False


@pytest.mark.parametrize('actual_cap,verified', [(2400, True), (32000, False)])
def test_v114_native_execute_dispatch_and_actual_budget_gate(tmp_path, monkeypatch, actual_cap, verified):
    plan = make_plan(tmp_path, monkeypatch)
    from rpe_harness.experiments import review_native_evidence as evidence
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    trial = next(t for t in plan['sessions'] if t['case_id'] == 'invoice-ledger' and t['arm'] == 'native')
    case = shared._case(plan, trial)
    identity = {'provider': 'xiaomi', 'requested_model': 'mimo-v2.6-pro', 'response_models': ['mimo-v2.6-pro']}
    draft = {'capability': case['capability'], 'constraints': [], 'execution': {
        'input_path': case['input_name'], 'output_path': case['output_name'], **case['parameters']}}
    prompt = shared._json(draft)
    observation = {'completed': True, 'exit_code': 0, 'model_identity': identity,
        'process_cleanup': {'cleanup_complete': True},
        'original_prompt_sha256': shared._sha(prompt.encode()),
        'public_contract_sha256': plan['execution_public_contract_sha256'],
        'events': [{'kind': 'pre_api_request', 'request_max_completion_tokens': actual_cap,
                    'request_thinking_type': 'disabled'}]}
    gates, calls = [], []
    def gate(result, gate_plan, gate_trial):
        gates.append(gate_trial['trial_id'])
        return {'verified': True, 'status': 'accepted', 'proof_kind': 'authenticated_native_tool_io',
                'receipt_authenticated': True}
    monkeypatch.setattr(evidence, 'evaluate_native_review_execution_v114', gate)
    monkeypatch.setattr(evidence, 'native_chain_diagnostics', lambda *a: {'durable_observations_match': True})
    def native(*args, **kwargs):
        calls.append(kwargs)
        return observation
    record = {'model_identity': identity, 'native_chain_diagnostics': {}, 'request_budget_observations': {}}
    shared._approve_and_execute(plan, trial, PhaseLedger(Path(plan['root'])/'ledger'), {}, draft, record, native)
    assert gates == [trial['trial_id']]
    assert calls[0]['native_evidence'] == {'artifact_name': 'output.csv'}
    assert calls[0]['request_budget'] == plan['request_budget']
    assert calls[0]['native_finish_enabled'] is True and calls[0]['timeout'] == 180
    assert record['execution_verified'] is verified
    assert record['request_budget_observations']['execute']['conformant'] is verified


def test_v114_comparison_native_execution_limit_cannot_consume_external_entry_budget(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, monkeypatch)
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    ledger = PhaseLedger(Path(plan['root'])/'ledger')
    for index in range(4):
        attempt = 'already-consumed-'+str(index)
        ledger.reserve('native', 'execute', attempt)
        ledger.finish(attempt, {'status': 'failed', 'elapsed_seconds': 0})
    trial = next(t for t in plan['sessions'] if t['arm'] == 'native')
    record = {}
    shared._approve_and_execute(plan, trial, ledger, {}, {}, record,
        lambda *a, **k: pytest.fail('comparison consumed reserved external execution'))
    assert record == {'error_code': 'native_execution_budget_exhausted'}


def test_v114_portable_budget_observations_cannot_be_relabelled_conformant(tmp_path, monkeypatch):
    plan = make_plan(tmp_path, monkeypatch)
    api = importlib.import_module('rpe_harness.experiments.review_v114_comparison')
    def fail(*args, **kwargs):
        raise RuntimeError('private')
    shared.run_v111_comparison(plan['root'], max_sessions=1, native_runner=fail, public_runner=fail)
    destination = tmp_path/'public'
    shared.export_v111_evidence(plan['root'], destination)
    assert api.verify_v114_evidence(destination)['verified'] is True
    path = destination/'summary.json'
    summary = shared._read(path)
    summary.pop('sha256')
    summary['records'][0]['request_budget_observations']['draft']['conformant'] = True
    path.unlink()
    shared._save(path, summary)
    assert api.verify_v114_evidence(destination)['verified'] is False


@pytest.mark.parametrize('fault', ['draft_limit', 'execute_limit', 'output_limit', 'request_file'])
def test_v114_portable_verifier_rechecks_profile_budgets_after_cleanup(tmp_path, monkeypatch, fault):
    plan = make_plan(tmp_path, monkeypatch)
    api = importlib.import_module('rpe_harness.experiments.review_v114_comparison')
    destination = tmp_path/'public'
    shared.export_v111_evidence(plan['root'], destination)
    shutil.rmtree(plan['root'])
    assert api.verify_v114_evidence(destination)['verified'] is True
    assert shared.verify_v111_evidence(destination)['verified'] is False
    assert 'request_budget_path' not in shared._read(destination/'preregistration.json')
    if fault == 'request_file':
        (destination/'request-budget.json').write_text('{}')
    else:
        path = destination/'preregistration.json'
        document = shared._read(path)
        document.pop('sha256')
        if fault == 'draft_limit':
            document['max_draft_sessions'] = 11
        elif fault == 'execute_limit':
            document['max_native_execution_sessions'] = 5
        else:
            document['request_budget']['max_completion_tokens'] = 32000
        path.unlink()
        shared._save(path, document)
    assert api.verify_v114_evidence(destination)['verified'] is False
