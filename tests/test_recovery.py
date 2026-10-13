"""Guarded, durable retry links for stopped pre-publication attempts."""
import importlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rpe_harness.runtime import HarnessRuntime


def recover(runtime, run_id, **kwargs):
    assert importlib.util.find_spec('rpe_harness.recovery'), 'guarded recovery is missing'
    return importlib.import_module('rpe_harness.recovery').recover_run(runtime, run_id, **kwargs)


def payload(tmp_path):
    source = tmp_path / 'source.csv'
    source.write_text('id,value\n1,a\n1,b\n2,c\n', encoding='utf-8-sig')
    return {'schema_version': 2, 'event_id': 'recover-original',
            'capability': 'tabular.deduplicate', 'goal': '保留首条',
            'execution': {'input_path': str(source), 'output_path': str(tmp_path / 'result.csv'),
                          'key_columns': ['id']}}


def failed_run(tmp_path, monkeypatch):
    from rpe_harness import execution
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    original = execution.prepare_deduplication
    monkeypatch.setattr(execution, 'prepare_deduplication', lambda **kw: (_ for _ in ()).throw(RuntimeError('owned child stopped')))
    result = runtime.run(payload(tmp_path))
    assert result['status'] == 'failed'
    monkeypatch.setattr(execution, 'prepare_deduplication', original)
    return runtime, result


def test_stopped_failure_retries_same_plan_once_and_links_history(tmp_path, monkeypatch):
    runtime, original = failed_run(tmp_path, monkeypatch)
    retried = recover(runtime, original['run_id'])
    assert retried['status'] == 'succeeded'
    assert retried['run_id'] != original['run_id']
    assert retried['retry_of'] == original['run_id']
    assert recover(runtime, original['run_id'])['run_id'] == retried['run_id']
    assert runtime.run(original['event'])['run_id'] == original['run_id']
    new_runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    assert recover(new_runtime, original['run_id'])['run_id'] == retried['run_id']
    assert len(runtime.store.list()) == 2


def test_concurrent_recovery_does_not_duplicate_attempts(tmp_path, monkeypatch):
    runtime, original = failed_run(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        attempts = list(pool.map(lambda _: recover(runtime, original['run_id']), range(8)))
    assert len({attempt['run_id'] for attempt in attempts}) == 1
    assert len(runtime.store.list()) == 2


@pytest.mark.parametrize('mutation', ['source_changed', 'output_exists', 'publication_intent', 'artifact', 'missing_plan'])
def test_uncertain_or_changed_original_refuses_retry(tmp_path, monkeypatch, mutation):
    runtime, original = failed_run(tmp_path, monkeypatch)
    if mutation == 'source_changed':
        Path(original['event']['execution']['input_path']).write_text('id,value\n1,changed\n')
    elif mutation == 'output_exists':
        Path(original['event']['execution']['output_path']).write_text('existing output')
    elif mutation == 'publication_intent':
        runtime.store.step(original['run_id'], 'publish', {'publication_confirmed': False})
    elif mutation == 'artifact':
        from rpe_harness.storage import canonical_json
        with runtime.store.connect() as db:
            db.execute('UPDATE runs SET artifact_json=? WHERE run_id=?',
                       (canonical_json({'path': str(tmp_path / 'result.csv'), 'published': True}), original['run_id']))
    else:
        with runtime.store.connect() as db:
            db.execute("DELETE FROM steps WHERE run_id=? AND stage='plan'", (original['run_id'],))
    with pytest.raises(ValueError):
        recover(runtime, original['run_id'])
    assert len(runtime.store.list()) == 1


def test_successful_or_rejected_work_is_never_retried(tmp_path):
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    success = runtime.run(payload(tmp_path))
    assert success['status'] == 'succeeded'
    with pytest.raises(ValueError):
        recover(runtime, success['run_id'])
    invalid = payload(tmp_path)
    invalid['event_id'] = 'invalid'
    invalid['capability'] = 'unknown'
    result = runtime.run(invalid)
    assert result['status'] == 'rejected'
    with pytest.raises(ValueError):
        recover(runtime, result['run_id'])


def test_active_owner_lease_prevents_retry_even_if_status_is_tampered(tmp_path):
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    original, _ = runtime.store.claim_event('local', 'active', payload(tmp_path))
    with runtime.store.connect() as db:
        db.execute("UPDATE runs SET status='interrupted' WHERE run_id=?", (original['run_id'],))
    try:
        with pytest.raises(ValueError):
            recover(runtime, original['run_id'])
        assert len(runtime.store.list()) == 1
    finally:
        runtime.store._release_lease(original['run_id'])


def test_recovery_attempt_budget_applies_to_entire_chain(tmp_path, monkeypatch):
    runtime, original = failed_run(tmp_path, monkeypatch)
    from rpe_harness import execution
    monkeypatch.setattr(execution, 'prepare_deduplication', lambda **kw: (_ for _ in ()).throw(RuntimeError('again')))
    second = recover(runtime, original['run_id'], max_attempts=2)
    assert second['status'] == 'failed'
    with pytest.raises(ValueError):
        recover(runtime, second['run_id'], max_attempts=2)
    assert len(runtime.store.list()) == 2


@pytest.mark.parametrize('phase', ['execute', 'before_publish', 'after_publish'])
def test_real_owner_exit_retries_only_before_publication_intent(tmp_path, phase):
    request = payload(tmp_path)
    db = tmp_path / 'runs.sqlite'
    program = '''
import json, os, sys
from rpe_harness.runtime import HarnessRuntime
from rpe_harness import execution
runtime = HarnessRuntime(db_path=sys.argv[1])
phase = sys.argv[2]
if phase == 'execute':
    def crash(**kwargs): os._exit(23)
    execution.prepare_deduplication = crash
else:
    publish = execution.publish_output
    def crash(prepared, plan=None):
        if phase == 'after_publish': publish(prepared, plan=plan)
        os._exit(23)
    execution.publish_output = crash
runtime.run(json.load(sys.stdin))
'''
    child = subprocess.run([sys.executable, '-B', '-c', program, str(db), phase],
                           input=json.dumps(request), capture_output=True, text=True, timeout=10)
    assert child.returncode == 23, child.stderr
    runtime = HarnessRuntime(db_path=db)
    original = runtime.store.list()[0]
    assert original['status'] == 'interrupted'
    if phase == 'execute':
        assert recover(runtime, original['run_id'])['status'] == 'succeeded'
    else:
        with pytest.raises(ValueError, match='publication'):
            recover(runtime, original['run_id'])
        assert len(runtime.store.list()) == 1
        assert (tmp_path / 'result.csv').exists() == (phase == 'after_publish')
    assert runtime.run(request)['run_id'] == original['run_id']


def test_input_race_after_recovery_guard_is_rejected_by_anchored_plan(tmp_path, monkeypatch):
    runtime, original = failed_run(tmp_path, monkeypatch)
    run = runtime.run

    def change_before_admission(request, **kwargs):
        Path(request['execution']['input_path']).write_text('id,value\n1,new\n')
        return run(request, **kwargs)

    monkeypatch.setattr(runtime, 'run', change_before_admission)
    result = recover(runtime, original['run_id'])
    assert result['status'] == 'rejected'
    assert not (tmp_path / 'result.csv').exists()
