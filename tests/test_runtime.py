import csv
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from rpe_harness.runtime import HarnessRuntime
from rpe_harness.storage import EventConflict, RunStore


def request(tmp_path, event_id='orders-1'):
    source = tmp_path / 'orders.csv'
    source.write_text('订单号,金额\n001,10\n002,20\n001,99\n', encoding='utf-8-sig')
    return {
        'schema_version': 1, 'event_id': event_id, 'session_id': 'session-1',
        'state': {'task_type': 'data.processing', 'goal': '按订单号保留首条',
                  'input_format': 'csv', 'required_capability': 'tabular.deduplicate',
                  'constraints': ['preserve_original', 'preserve_order']},
        'execution': {'input_path': str(source), 'output_path': str(tmp_path / 'result.csv'),
                      'key_columns': ['订单号']},
    }


def test_verified_success_replay_and_new_process_query(tmp_path):
    payload = request(tmp_path)
    original = (tmp_path / 'orders.csv').read_bytes()
    db = tmp_path / 'runs.sqlite'
    runtime = HarnessRuntime(db_path=db)
    result = runtime.run(payload)
    assert result['status'] == 'succeeded'
    assert result['verification']['passed'] is True
    assert result['state']['source_ref'] == 'orders-1'
    assert result['state']['session_id'] == 'session-1'
    assert (tmp_path / 'orders.csv').read_bytes() == original
    with (tmp_path / 'result.csv').open(encoding='utf-8-sig', newline='') as f:
        assert list(csv.reader(f)) == [['订单号', '金额'], ['001', '10'], ['002', '20']]
    assert runtime.run(payload)['run_id'] == result['run_id']
    assert runtime.store.usage()['builtin/csv-deduplicate']['use_count'] == 1
    program = 'import json,sys; from rpe_harness.runtime import HarnessRuntime; print(json.dumps(HarnessRuntime(db_path=sys.argv[1]).store.get(sys.argv[2])))'
    child = subprocess.run([sys.executable, '-c', program, str(db), result['run_id']],
                           capture_output=True, text=True, check=True)
    assert json.loads(child.stdout)['status'] == 'succeeded'


def test_changed_payload_same_event_is_conflict(tmp_path):
    payload = request(tmp_path)
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    runtime.run(payload)
    payload['execution']['key_columns'] = ['金额']
    with pytest.raises(EventConflict):
        runtime.run(payload)
    assert len(runtime.store.list()) == 1


@pytest.mark.parametrize('mutation', ['unknown', 'capability', 'format', 'keys', 'constraint'])
def test_unsupported_request_is_persisted_rejection(tmp_path, mutation):
    payload = request(tmp_path)
    if mutation == 'unknown':
        payload['state']['task_type'] = 'unknown'
    elif mutation == 'capability':
        payload['state']['required_capability'] = 'document'
    elif mutation == 'format':
        payload['state']['input_format'] = 'pdf'
    elif mutation == 'keys':
        payload['execution']['key_columns'] = []
    else:
        payload['state']['constraints'].append('send_email')
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    result = runtime.run(payload)
    assert result['status'] == 'rejected'
    assert runtime.store.get(result['run_id'])['error']['code']
    assert not (tmp_path / 'result.csv').exists()
    assert runtime.store.usage() == {}


def test_existing_output_is_never_overwritten_or_counted_success(tmp_path):
    payload = request(tmp_path)
    output = tmp_path / 'result.csv'
    output.write_text('keep this')
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    result = runtime.run(payload)
    assert result['status'] == 'rejected'
    assert output.read_text() == 'keep this'
    assert runtime.store.usage() == {}


def test_concurrent_duplicate_claims_execute_once(tmp_path):
    payload = request(tmp_path)
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: runtime.run(payload), range(12)))
    assert len({r['run_id'] for r in results}) == 1
    assert runtime.store.get(results[0]['run_id'])['status'] == 'succeeded'
    assert runtime.store.usage()['builtin/csv-deduplicate']['use_count'] == 1


def test_dead_process_is_interrupted_not_success(tmp_path):
    db = tmp_path / 'runs.sqlite'
    code = 'import sys; from rpe_harness.storage import RunStore; s=RunStore(sys.argv[1]); r,f=s.claim_event("local","crash",{"event_id":"crash"}); s.mark_running(r["run_id"]); print(r["run_id"],flush=True)'
    child = subprocess.run([sys.executable, '-c', code, str(db)], capture_output=True,
                           text=True, check=True)
    runtime = HarnessRuntime(db_path=db)
    result = runtime.store.get(child.stdout.strip())
    assert result['status'] == 'interrupted'
    assert result['verification'] is None


def test_skill_state_and_external_usage_survive_reload(tmp_path):
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    runtime.set_skill_state('builtin/csv-deduplicate', enabled=False)
    runtime.record_usage('builtin/xlsx-deduplicate', False)
    runtime.reload()
    assert runtime.router.skills['builtin/csv-deduplicate'].enabled is False
    assert runtime.router.skills['builtin/xlsx-deduplicate'].use_count == 1
    with pytest.raises(ValueError):
        runtime.record_usage('missing', True)


def test_reused_pid_cannot_keep_an_abandoned_run_active(tmp_path):
    db = tmp_path / 'runs.sqlite'
    code = 'import sys; from rpe_harness.storage import RunStore; s=RunStore(sys.argv[1]); r,f=s.claim_event("local","reuse",{}); s.mark_running(r["run_id"]); print(r["run_id"],flush=True)'
    child = subprocess.run([sys.executable, '-c', code, str(db)], capture_output=True,
                           text=True, check=True)
    store = RunStore(db)
    with store.connect() as connection:
        connection.execute('UPDATE runs SET owner_pid=?', (os.getpid(),))
    runtime = HarnessRuntime(db_path=db)
    assert runtime.store.get(child.stdout.strip())['status'] == 'interrupted'


def test_polluted_artifact_is_failed_and_never_published(tmp_path, monkeypatch):
    from pathlib import Path
    from rpe_harness import execution
    original_prepare = execution.prepare_deduplication

    def pollute(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        Path(prepared['staging_path']).write_text('订单号,金额\n001,999\n002,20\n', encoding='utf-8-sig')
        return prepared

    monkeypatch.setattr(execution, 'prepare_deduplication', pollute)
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    result = runtime.run(request(tmp_path))
    assert result['status'] == 'failed' and result['stage'] == 'verify'
    assert result['verification']['passed'] is False
    assert not (tmp_path / 'result.csv').exists()
    assert runtime.store.get(result['run_id'])['status'] == 'failed'
    usage = runtime.store.usage()['builtin/csv-deduplicate']
    assert usage['use_count'] == 1 and usage['verified_success_count'] == 0
    assert not list(tmp_path.glob('.rpe-*'))
