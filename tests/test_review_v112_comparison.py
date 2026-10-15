"""1.12 finite new inputs must not overwrite 1.11 or infer missing costs."""
import json
from pathlib import Path
import shutil
import sys

import pytest

from rpe_harness.experiments import review_v111_comparison as shared


@pytest.fixture
def plan(tmp_path, monkeypatch):
    skills = tmp_path / 'skills'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx', skills / 'productivity/xlsx')
    monkeypatch.setattr(shared, '_environment', lambda *a: {'source_sha256': {'fixture': 'frozen'}})
    from rpe_harness.integrations import hermes_session
    monkeypatch.setattr(hermes_session, '_installed_python', lambda root: root/'.venv/bin/python')
    return shared.preregister_v111_comparison(tmp_path / 'owned', skill_root=skills,
        hermes_root=tmp_path / 'absent-host', helper_python=sys.executable, profile='review-v112')


def test_versioned_registration_has_fresh_comma_tab_source_cold_and_missing_cases(plan):
    assert plan['profile'] == 'review-v112'
    assert len(plan['sessions']) == 8 and len(plan['pilots']) == 2
    assert plan['max_draft_sessions'] == 10 and plan['max_native_execution_sessions'] == 5
    cases = plan['cases']
    assert cases[0]['case_id'] == 'comma-orders'
    assert cases[1]['case_id'] == 'cold-stock'
    assert cases[2]['parameters']['delimiter'] == '\t'
    assert '\t' in Path(cases[2]['source_path']).read_text()
    assert cases[2]['sources'][0]['facts']['key_columns'] == ['站点', '票号']
    assert cases[3]['category'] == 'contract_probe'
    assert plan['independent_audit_budget'] == {'sessions': 1, 'max_turns': 1, 'timeout': 180, 'cli_estimated_usd': .25}


def test_versioned_failure_attempts_are_finite_and_unknown_usage_is_not_zero(plan):
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('private')
    shared.run_v111_comparison(plan['root'], native_runner=fail, public_runner=fail)
    shared.freeze_formal(plan['root'])
    result = shared.run_v111_comparison(plan['root'], formal=True, native_runner=fail, public_runner=fail)
    shared.run_v111_comparison(plan['root'], formal=True, native_runner=fail, public_runner=fail)
    assert len(calls) == 10 and result['profile'] == 'review-v112'
    assert result['costs']['total_tokens'] is None
    assert result['formal_attempts'] == 8 and result['comparison_ready'] is False
    assert all('source_preserved' in row and row['source_preserved'] for row in result['records'])
    assert all(row['error_code'] != 'private' for row in result['records'])


def test_versioned_export_is_offline_and_keeps_old_verifier_boundary(plan, tmp_path):
    from rpe_harness.experiments.review_v112_comparison import verify_v112_evidence
    destination = tmp_path / 'public'
    shared.export_v111_evidence(plan['root'], destination)
    shutil.rmtree(plan['root'])
    assert verify_v112_evidence(destination)['verified'] is True
    assert shared.verify_v111_evidence(destination)['verified'] is False
    public = json.loads((destination / 'preregistration.json').read_text())
    assert public['profile'] == 'review-v112'
    (destination / 'prompts/source-tabs.txt').write_text('changed')
    assert verify_v112_evidence(destination)['verified'] is False


def test_failed_native_input_deletion_still_records_trial_and_continues_pairs(plan):
    native_calls = []
    def fail_native(prompt, workspace, *args, **kwargs):
        native_calls.append(str(workspace))
        for path in Path(workspace).glob('input.*'):
            path.unlink()
        raise RuntimeError('private')
    def fail_public(*args):
        raise RuntimeError('private')
    summary = shared.run_v111_comparison(plan['root'], native_runner=fail_native, public_runner=fail_public)
    assert len(summary['records']) == 2
    native = next(row for row in summary['records'] if row['arm'] == 'native')
    assert native['source_preserved'] is False and native['accepted'] is False
    assert native['verification_error_code'] == 'source_unavailable'
    assert len(native_calls) == 1
