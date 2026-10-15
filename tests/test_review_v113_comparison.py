"""A single frozen 1.13 comparison has no model pilot or replacement attempts."""
import json
from pathlib import Path
import shutil
import sys

import pytest

from rpe_harness.experiments import review_v111_comparison as shared


@pytest.fixture
def plan(tmp_path, monkeypatch):
    skills = tmp_path / 'skills'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx',
                    skills / 'productivity/xlsx')
    monkeypatch.setattr(shared, '_environment', lambda *a: {
        'source_sha256': {'fixture': 'frozen'},
        'helper_python_identity': {'executable_sha256': 'a' * 64}})
    from rpe_harness.integrations import hermes_session
    monkeypatch.setattr(hermes_session, '_installed_python', lambda root: root / '.venv/bin/python')
    return shared.preregister_v111_comparison(tmp_path / 'owned', skill_root=skills,
        hermes_root=tmp_path / 'absent-host', helper_python=sys.executable, profile='review-v113')


def test_registration_limits_actual_hosts_without_unused_model_pilots(plan):
    assert plan['profile'] == 'review-v113'
    assert len(plan['sessions']) == 8 and plan['pilots'] == []
    assert plan['pilot_case'] is None
    assert plan['max_draft_sessions'] == 8
    assert plan['max_native_execution_sessions'] == 3
    assert [case['case_id'] for case in plan['cases']] == [
        'receipt-comma', 'cold-shipment', 'returns-tabs', 'undecided-staff-key']
    assert plan['cases'][2]['parameters']['delimiter'] == '\t'
    assert plan['cases'][2]['sources'][0]['facts']['key_columns'] == ['渠道', '退货单']
    assert sum(case['category'] == 'production' for case in plan['cases']) == 3
    assert sum(case['category'] == 'contract_probe' for case in plan['cases']) == 1


def test_formal_failures_are_consumed_once_and_unknown_cost_stays_unknown(plan):
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('private failure must not enter public evidence')
    with pytest.raises(FileNotFoundError):
        shared.run_v111_comparison(plan['root'], formal=True, native_runner=fail, public_runner=fail)
    shared.freeze_formal(plan['root'])
    summary = shared.run_v111_comparison(plan['root'], formal=True,
        native_runner=fail, public_runner=fail)
    repeated = shared.run_v111_comparison(plan['root'], formal=True,
        native_runner=fail, public_runner=fail)
    assert len(calls) == 8 and summary['formal_attempts'] == 8
    assert repeated['formal_attempts'] == 8 and repeated['pilot_attempts'] == 0
    assert summary['costs']['total_tokens'] is None
    assert summary['comparison_ready'] is False
    assert all(row['source_preserved'] and not row['accepted'] for row in summary['records'])
    assert all(row.get('error_code') != 'private failure must not enter public evidence'
               for row in summary['records'])


def test_native_drafts_retain_real_file_receipts_and_cold_restore(plan):
    calls = []
    def native(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('consumed')
    shared.freeze_formal(plan['root'])
    shared.run_v111_comparison(plan['root'], formal=True, native_runner=native,
        public_runner=lambda *a: {})
    assert len(calls) == 4
    assert all(call['native_evidence']['draft_name'] == 'draft.json' for call in calls)
    assert sum('cold_skill_id' in call['native_evidence'] for call in calls) == 1
    assert all('native_execution_evidence' not in call for call in calls)


def test_portable_verifier_pins_new_budget_and_preserves_old_profiles(plan, tmp_path):
    from rpe_harness.experiments.review_v113_comparison import verify_v113_evidence
    from rpe_harness.experiments.review_v112_comparison import verify_v112_evidence
    destination = tmp_path / 'public'
    shared.export_v111_evidence(plan['root'], destination)
    shutil.rmtree(plan['root'])
    assert verify_v113_evidence(destination)['verified'] is True
    assert verify_v112_evidence(destination)['verified'] is False
    assert shared.verify_v111_evidence(destination)['verified'] is False
    path = destination / 'preregistration.json'
    document = json.loads(path.read_text())
    document.pop('sha256')
    document['max_draft_sessions'] = 10
    document['sha256'] = shared._sha(shared._json(document).encode())
    path.write_text(shared._json(document))
    assert verify_v113_evidence(destination)['verified'] is False


def test_native_execution_observation_configuration_is_not_a_business_tool(plan):
    from rpe_harness.experiments.review_v113_comparison import native_execution_options
    trial = next(row for row in plan['sessions']
                 if row['case_id'] == 'cold-shipment' and row['arm'] == 'native')
    options = native_execution_options(plan, trial)
    assert set(options) == {'native_execution_evidence'}
    evidence = options['native_execution_evidence']
    assert evidence['helper_python'] == sys.executable
    assert evidence['helper_python_sha256'] == 'a' * 64
    assert evidence['parameters'] == {'mode': 'rows', 'sheet': '发运'}
    assert evidence['input_sha256'] == trial['input_sha256']
    other = next(row for row in plan['sessions'] if row['case_id'] == 'receipt-comma')
    assert native_execution_options(plan, other) == {}


def test_native_process_summary_retains_actual_exit_without_private_environment():
    from rpe_harness.experiments.review_v113_comparison import native_execution_summary
    gate = {'verified': True, 'proof_kind': 'authenticated_darwin_kernel_process_exit',
        'receipt_authenticated': True, 'process_returncode': 0,
        'artifact_binding': 'final_bytes_on_observed_stdout_inode_not_exclusive_writer',
        'output_sha256': 'a' * 64, 'process_argv_sha256': 'b' * 64,
        'environment': {'API_KEY': 'private'}, 'signing_key': 'must-not-export'}
    result = native_execution_summary(gate)
    assert result['process_returncode'] == 0
    assert result['receipt_authenticated'] is True
    assert result['artifact_binding'].endswith('not_exclusive_writer')
    assert result['output_sha256'] == 'a' * 64
    assert 'environment' not in result and 'signing_key' not in result
