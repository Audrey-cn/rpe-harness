import importlib
import json

import pytest


def module():
    try:
        return importlib.import_module('rpe_harness.experiments.unattended_sequence')
    except ImportError:
        pytest.fail('bounded unattended JSON acceptance driver is unavailable')


def test_fixed_unattended_sequence_observes_upgrade_rollback_and_crash_recovery(tmp_path):
    from pathlib import Path
    candidate = Path(__file__).resolve().parents[1] / 'examples/json-candidate/candidate.py'
    report = module().run_sequence(tmp_path / 'owned', candidate_path=candidate)
    assert report['passed'] is True
    assert all(report['checks'].values())
    assert report['process_restarts'] == len(report['attempts']) <= 32
    assert report['cycle_count'] >= 4
    assert report['candidate_reuse_count'] >= 3
    assert report['proposal_origin'] == 'external_injected_code'
    assert report['learning_benefit_estimated'] is False
    assert report['autonomous_code_generation'] is False
    assert json.loads((tmp_path / 'owned/unattended-progress.json').read_text()) == report


def test_failed_source_load_keeps_real_completed_attempt_and_failure(tmp_path):
    root = tmp_path / 'failed'
    with pytest.raises(FileNotFoundError):
        module().run_sequence(root, candidate_path=tmp_path / 'absent.py')
    progress = json.loads((root / 'unattended-progress.json').read_text())
    assert progress['passed'] is False
    assert progress['attempts'][0]['name'] == 'json-seed'
    assert progress['attempts'][0]['returncode'] == 0
    assert progress['error_type'] == 'FileNotFoundError'


@pytest.mark.parametrize('field,value', [('max_steps', 0), ('max_steps', 33), ('max_steps', True),
    ('step_timeout', 0), ('step_timeout', 61), ('step_timeout', True)])
def test_driver_budget_is_fixed_and_invalid_values_do_not_create_scope(tmp_path, field, value):
    root = tmp_path / 'invalid'
    with pytest.raises(ValueError, match='budget'):
        module().run_sequence(root, candidate_path=tmp_path / 'unused.py', **{field: value})
    assert not root.exists()


def test_step_budget_retains_completed_attempt(tmp_path):
    root = tmp_path / 'bounded'
    with pytest.raises(RuntimeError, match='step.*budget'):
        module().run_sequence(root, candidate_path=tmp_path / 'unused.py', max_steps=1)
    progress = json.loads((root / 'unattended-progress.json').read_text())
    assert progress['passed'] is False and len(progress['attempts']) == 1


def test_real_child_timeout_is_stopped_and_retained(tmp_path):
    import subprocess
    root = tmp_path / 'timeout'
    with pytest.raises(subprocess.TimeoutExpired):
        module().run_sequence(root, candidate_path=tmp_path / 'unused.py', step_timeout=0.000001)
    progress = json.loads((root / 'unattended-progress.json').read_text())
    assert progress['passed'] is False
    assert progress['attempts'][0]['timeout'] is True
    assert progress['attempts'][0]['returncode'] is not None


def test_inventory_error_still_stops_and_waits_owned_child_and_reports_failure(monkeypatch):
    driver = module()
    calls = []
    class OwnedChild:
        pid = 99999999
        def kill(self):
            calls.append('kill')
        def wait(self):
            calls.append('wait')
    def unavailable(*args, **kwargs):
        raise OSError('owned inventory test failure')
    monkeypatch.setattr(driver.subprocess, 'run', unavailable)
    with pytest.raises(RuntimeError, match='inventory'):
        driver._stop_owned_process_tree(OwnedChild())
    assert calls == ['kill', 'wait']


@pytest.mark.parametrize('fallback_present', [False, True])
def test_dormant_step_checks_bound_persisted_candidate_failure_facts(tmp_path, fallback_present):
    from rpe_harness.evolution import cycle
    from rpe_harness.evolution.store import digest
    from rpe_harness.runtime import HarnessRuntime
    from test_json_cycle import json_run
    driver = module()
    checker = getattr(driver, '_dormant_result', None)
    assert callable(checker), 'dormant step must inspect real failure diagnoses'
    database, seed = json_run(tmp_path)
    evidence = {'plan_sha256': seed['artifact']['plan_sha256'],
                'actual_executor': {'kind': 'candidate', 'candidate_version_id': 'owned-bad-version'},
                'verification': {'passed': False, 'checks': {'tokens_preserved': False}},
                'quarantine': {'quarantined': True, 'version_id': 'owned-bad-version'}}
    if fallback_present:
        HarnessRuntime(db_path=database).store.step(seed['run_id'], 'candidate_fallback', evidence)
    cycle(database, tmp_path, auto_apply=False)
    expected = {'run_id': seed['run_id'], 'candidate_version_id': 'owned-bad-version',
                'plan_sha256': seed['artifact']['plan_sha256'], 'evidence_sha256': digest(evidence)}
    result = checker(database, [expected])
    assert result['retained'] is True
    assert result['diagnostics_matched'] is fallback_present
    wrong = {**expected, 'evidence_sha256': '0' * 64}
    assert checker(database, [wrong])['diagnostics_matched'] is False
