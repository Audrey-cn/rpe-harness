import importlib

import pytest

from test_candidates import GOOD_CODE


def test_independent_process_sequence_records_actual_reuse_and_failure_fallback(tmp_path):
    try:
        module = importlib.import_module('rpe_harness.experiments.reuse_sequence')
    except ImportError:
        pytest.fail('cross-process reuse acceptance is unavailable')
    code = tmp_path / 'candidate.py'
    code.write_text(GOOD_CODE)
    report = module.run_sequence(tmp_path / 'owned', candidate_path=code)
    assert report['passed'] is True
    assert report['process_restarts'] >= 6
    assert len(report['cases']) >= 8 and all(case['truth_passed'] for case in report['cases'])
    assert report['candidate_reuse_count'] >= 2
    assert report['failed_candidate_quarantined'] is True
    assert report['fallback_used_builtin'] is True
    assert report['rollback_used_builtin'] is True
    assert report['dormant_lookup_retained'] is True
    assert report['learning_benefit_estimated'] is False
    assert report['bad_output_oracle_rejected'] is True
    assert report['bad_output_candidate_quarantined'] is True
    assert report['bad_output_fallback_same_plan'] is True


def test_failed_sequence_retains_completed_attempts(tmp_path):
    import json
    module = importlib.import_module('rpe_harness.experiments.reuse_sequence')
    root = tmp_path / 'failed'
    with pytest.raises(FileNotFoundError):
        module.run_sequence(root, candidate_path=tmp_path / 'missing.py')
    progress = json.loads((root / 'sequence-progress.json').read_text())
    assert progress['passed'] is False
    assert progress['cases'][0]['name'] == 'csv-seed'
    assert progress['cases'][0]['truth_passed'] is True


def test_cli_failure_itself_is_retained(tmp_path, monkeypatch):
    import json
    import subprocess
    module = importlib.import_module('rpe_harness.experiments.reuse_sequence')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 1, 'invalid', ''))
    root = tmp_path / 'failed-cli'
    with pytest.raises(json.JSONDecodeError):
        module.run_sequence(root, candidate_path=tmp_path / 'missing.py')
    progress = json.loads((root / 'sequence-progress.json').read_text())
    assert progress['passed'] is False and progress['process_restarts'] == 1
    assert progress['cases'][0]['returncode'] == 1
    assert progress['cases'][0]['error_type'] == 'JSONDecodeError'
