"""Cancellation is durable and cannot race a verified artifact into a false state."""
import json
import fcntl
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rpe_harness.runtime import HarnessRuntime
from rpe_harness.storage import RunStore


def request(tmp_path):
    source = tmp_path / 'source.csv'
    source.write_text('id,value\n1,first\n1,duplicate\n2,last\n')
    return {'schema_version': 2, 'event_id': 'cancel-task',
            'capability': 'tabular.deduplicate', 'goal': 'keep first',
            'execution': {'input_path': str(source), 'output_path': str(tmp_path / 'out.csv'),
                          'key_columns': ['id']}}


@pytest.mark.parametrize('status', ['received', 'running', 'interrupted'])
def test_cancel_nonterminal_run_records_reason_and_releases_lease(tmp_path, status):
    store = RunStore(tmp_path / 'runs.sqlite')
    run, _ = store.claim_event('local', 'one', {})
    run_id = run['run_id']
    if status == 'running':
        store.mark_running(run_id)
    elif status == 'interrupted':
        store.finish(run_id, status, error={'code': 'owner_process_exited'})
    result = store.cancel(run_id, reason='user changed scope')
    saved = result['run']
    assert result['status'] == saved['status'] == 'cancelled'
    assert result['already_cancelled'] is False
    assert saved['finished_at'] >= run['started_at']
    assert saved['stage'] == 'cancel'
    assert saved['steps'][-1]['evidence'] == {
        'reason': 'user changed scope', 'previous_status': status}
    assert saved['steps'][-1]['created_at'] == saved['finished_at']
    assert not (store.lease_dir / run_id).exists()
    assert RunStore(tmp_path / 'runs.sqlite').get(run_id) == saved


def test_repeated_and_concurrent_cancellation_has_one_audit_event(tmp_path):
    store = RunStore(tmp_path / 'runs.sqlite')
    run, _ = store.claim_event('local', 'one', {})
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda n: store.cancel(run['run_id'], reason=f'cancel {n}'),
                                range(12)))
    assert sum(not r['already_cancelled'] for r in results) == 1
    saved = store.get(run['run_id'])
    assert len(saved['steps']) == 1
    assert store.cancel(run['run_id'], reason='again')['run'] == saved
    assert store.claim_event('local', 'one', {}) == (saved, False)
    assert store.finish(run['run_id'], 'failed')['status'] == 'cancelled'


@pytest.mark.parametrize('status', ['succeeded', 'rejected', 'failed'])
def test_terminal_cancellation_is_explicit_non_destructive_conflict(tmp_path, status):
    store = RunStore(tmp_path / 'runs.sqlite')
    run, _ = store.claim_event('local', 'one', {})
    output = tmp_path / 'out.csv'
    output.write_bytes(b'published or preexisting data')
    kwargs = ({'verification': {'passed': True}, 'artifact': {'published': True,
               'path': str(output)}} if status == 'succeeded' else {'error': {'code': 'original'}})
    saved = store.finish(run['run_id'], status, **kwargs)
    result = store.cancel(run['run_id'], reason='too late')
    assert result['status'] == 'conflict'
    assert result['error_code'] == 'already_terminal'
    assert result['run'] == saved == store.get(run['run_id'])
    assert output.read_bytes() == b'published or preexisting data'


def test_completed_publication_receipt_is_preserved_even_if_output_disappeared(tmp_path):
    store = RunStore(tmp_path / 'runs.sqlite')
    run, _ = store.claim_event('local', 'legacy-publication', {})
    store.step(run['run_id'], 'complete', {'path': str(tmp_path / 'removed.csv'),
                                         'published': True})
    saved = store.finish(run['run_id'], 'interrupted')
    result = store.cancel(run['run_id'])
    assert result['status'] == 'conflict'
    assert result['error_code'] == 'publication_started'
    assert result['run'] == saved


def test_uninspectable_publication_never_becomes_cancelled(tmp_path, monkeypatch):
    store = RunStore(tmp_path / 'runs.sqlite')
    run, _ = store.claim_event('local', 'uncertain-publication', {})
    output = tmp_path / 'published.csv'
    output.write_bytes(b'published bytes')
    parent = tmp_path.stat()
    store.step(run['run_id'], 'publish', {'output_path': str(output),
        'output_parent_identity': [parent.st_dev, parent.st_ino]})
    saved = store.finish(run['run_id'], 'interrupted')
    original = os.lstat

    def denied(path, *args, **kwargs):
        if Path(path) == output:
            raise PermissionError('publication path cannot be inspected')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, 'lstat', denied)
    result = store.cancel(run['run_id'])
    assert result['status'] == 'conflict'
    assert result['error_code'] == 'publication_started'
    assert result['run'] == saved
    assert output.read_bytes() == b'published bytes'


@pytest.mark.parametrize('replace_parent', [False, True])
def test_replaced_publication_directory_does_not_hide_published_output(tmp_path, replace_parent):
    store = RunStore(tmp_path / 'runs.sqlite')
    run, _ = store.claim_event('local', 'moved-publication', {})
    directory = tmp_path / 'output'
    directory.mkdir()
    output = directory / 'published.csv'
    identity = directory.stat()
    store.step(run['run_id'], 'publish', {'output_path': str(output),
        'output_parent_identity': [identity.st_dev, identity.st_ino]})
    output.write_bytes(b'published bytes')
    directory.rename(tmp_path / 'moved-output')
    if replace_parent:
        directory.mkdir()
    saved = store.finish(run['run_id'], 'interrupted')
    result = store.cancel(run['run_id'])
    assert result['status'] == 'conflict'
    assert result['run'] == saved
    assert (tmp_path / 'moved-output/published.csv').read_bytes() == b'published bytes'


@pytest.mark.parametrize('reason', ['', '  ', 1, False, [], {}, '界' * 1366, '\ud800'])
def test_invalid_reason_leaves_run_untouched(tmp_path, reason):
    store = RunStore(tmp_path / 'runs.sqlite')
    run, _ = store.claim_event('local', 'one', {})
    try:
        with pytest.raises(ValueError):
            store.cancel(run['run_id'], reason=reason)
        assert store.get(run['run_id']) == run
    finally:
        store._release_lease(run['run_id'])


def test_unknown_run_does_not_create_cancellation_record(tmp_path):
    store = RunStore(tmp_path / 'runs.sqlite')
    with pytest.raises(KeyError):
        store.cancel('missing')
    assert store.list() == []


def test_remote_cancellation_reaps_dead_owner_lease(tmp_path):
    database = tmp_path / 'runs.sqlite'
    program = '''
import os, sys
from rpe_harness.storage import RunStore
store = RunStore(sys.argv[1])
run, _ = store.claim_event('local', 'crash', {})
print(run['run_id'], flush=True)
os._exit(77)
'''
    child = subprocess.run([sys.executable, '-B', '-c', program, str(database)],
                           capture_output=True, text=True, timeout=10)
    assert child.returncode == 77, child.stderr
    store = RunStore(database)
    run_id = child.stdout.strip()
    assert (store.lease_dir / run_id).exists()
    assert store.cancel(run_id)['status'] == 'cancelled'
    assert not (store.lease_dir / run_id).exists()


def test_remote_cancel_preserves_live_lease_until_owner_exit_then_reaps_on_restart(tmp_path):
    database = tmp_path / 'runs.sqlite'
    program = '''
import os, sys
from rpe_harness.storage import RunStore
store = RunStore(sys.argv[1])
run, _ = store.claim_event('local', 'live-owner', {})
print(run['run_id'], flush=True)
sys.stdin.read(1)
os._exit(77)
'''
    with subprocess.Popen([sys.executable, '-B', '-c', program, str(database)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) as child:
        try:
            run_id = child.stdout.readline().strip()
            assert run_id
            store = RunStore(database)
            cancelled = store.cancel(run_id)
            lease_path = store.lease_dir / run_id
            assert cancelled['status'] == 'cancelled'
            # Reopening/recovery must not unlink the active owner's lock inode.
            HarnessRuntime(db_path=database)
            with lease_path.open('r+b') as lease:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child.communicate('x', timeout=10)
            assert child.returncode == 77
            runtime = HarnessRuntime(db_path=database)
            assert not lease_path.exists()
            assert runtime.store.get(run_id) == cancelled['run']
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


@pytest.mark.parametrize('attempted', [False, True])
def test_cancel_preserves_actual_attempt_usage_only(tmp_path, attempted):
    store = RunStore(tmp_path / 'runs.sqlite')
    run, _ = store.claim_event('local', 'one', {})
    store.mark_running(run['run_id'], skill_id='builtin/csv-deduplicate')
    if attempted:
        store.step(run['run_id'], 'execute', attempted=True)
    store.finish(run['run_id'], 'interrupted')
    before = store.usage()
    store.cancel(run['run_id'])
    assert store.usage() == before
    assert bool(before) == attempted


def interrupted_run(runtime, payload):
    from rpe_harness.tasks import plan_from_request
    run, _ = runtime.store.claim_event('local', payload['event_id'], payload)
    plan = plan_from_request(payload)
    runtime.store.mark_running(run['run_id'])
    runtime.store.step(run['run_id'], 'plan', {'plan': plan.to_dict(), 'plan_sha256': plan.sha256})
    return runtime.store.finish(run['run_id'], 'interrupted')


def test_reserved_recovery_conflicts_with_parent_cancellation(tmp_path, monkeypatch):
    from rpe_harness.recovery import recover_run
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    payload = request(tmp_path)
    parent = interrupted_run(runtime, payload)
    entered, release = threading.Event(), threading.Event()
    original = runtime.run

    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, 'run', pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(recover_run, runtime, parent['run_id'])
        try:
            assert entered.wait(10)
            result = runtime.cancel(parent['run_id'])
        finally:
            release.set()
        successor = worker.result(timeout=10)
    assert result['status'] == 'conflict'
    assert result['error_code'] == 'recovery_started'
    assert result['run'] == parent
    assert successor['status'] == 'succeeded'


def test_cancel_before_recovery_reservation_prevents_successor(tmp_path, monkeypatch):
    from rpe_harness import recovery
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    payload = request(tmp_path)
    parent = interrupted_run(runtime, payload)
    entered, release = threading.Event(), threading.Event()
    original = recovery._guards

    def pause(*args, **kwargs):
        original(*args, **kwargs)
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(recovery, '_guards', pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(recovery.recover_run, runtime, parent['run_id'])
        try:
            assert entered.wait(10)
            assert runtime.cancel(parent['run_id'])['status'] == 'cancelled'
        finally:
            release.set()
        with pytest.raises(ValueError, match='cancelled|stopped'):
            worker.result(timeout=10)
    assert len(runtime.store.list()) == 1
    assert not Path(payload['execution']['output_path']).exists()


def test_staging_cleanup_failure_still_releases_cancelled_worker_lease(tmp_path, monkeypatch):
    import rpe_harness.runtime as module
    from rpe_harness import execution
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    original = module.prepare_plan
    cleanup = execution.cleanup_staging
    owned = []

    def cancel_after_prepare(plan):
        prepared = original(plan)
        owned.append(prepared)
        RunStore(runtime.store.path).cancel(runtime.store.list()[0]['run_id'])
        return prepared

    def fail_cleanup(prepared):
        raise OSError('staging cleanup denied')

    monkeypatch.setattr(module, 'prepare_plan', cancel_after_prepare)
    monkeypatch.setattr(execution, 'cleanup_staging', fail_cleanup)
    try:
        with pytest.raises(OSError, match='staging cleanup denied'):
            runtime.run(request(tmp_path))
        saved = runtime.store.list()[0]
        assert saved['status'] == 'cancelled'
        assert not (runtime.store.lease_dir / saved['run_id']).exists()
        assert not (tmp_path / 'out.csv').exists()
    finally:
        for prepared in owned:
            cleanup(prepared)


@pytest.mark.parametrize('boundary', ['prepare', 'verified'])
def test_cancel_wins_before_publication_and_worker_cleans_staging(tmp_path, monkeypatch, boundary):
    import rpe_harness.runtime as module
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    payload = request(tmp_path)
    original_source = Path(payload['execution']['input_path']).read_bytes()
    entered, release = threading.Event(), threading.Event()
    target = 'prepare_plan' if boundary == 'prepare' else 'verify_plan'
    original = getattr(module, target)

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(module, target, pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(runtime.run, payload)
        try:
            assert entered.wait(10)
            run_id = runtime.store.list()[0]['run_id']
            # Another connection/store represents a CLI or HTTP cancellation.
            cancelled = RunStore(runtime.store.path).cancel(run_id, reason='stop now')
            assert cancelled['status'] == 'cancelled'
        finally:
            release.set()
        result = worker.result(timeout=10)
    assert result['status'] == 'cancelled'
    assert result['stage'] == 'cancel'
    assert result['steps'][-1]['stage'] == 'cancel'
    assert not Path(payload['execution']['output_path']).exists()
    assert not list(tmp_path.glob('.rpe-*'))
    assert not (runtime.store.lease_dir / run_id).exists()
    assert Path(payload['execution']['input_path']).read_bytes() == original_source
    assert runtime.run(payload)['status'] == 'cancelled'


def test_publish_wins_cancel_waits_and_preserves_verified_success(tmp_path, monkeypatch):
    from rpe_harness import execution
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    payload = request(tmp_path)
    published, release, cancelling = threading.Event(), threading.Event(), threading.Event()
    original = execution.publish_output

    def pause(*args, **kwargs):
        original(*args, **kwargs)
        published.set()
        assert release.wait(10)

    monkeypatch.setattr(execution, 'publish_output', pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(runtime.run, payload)
        try:
            assert published.wait(10)
            run_id = runtime.store.list()[0]['run_id']

            def cancel():
                cancelling.set()
                return RunStore(runtime.store.path).cancel(run_id, reason='late cancellation')

            cancellation = pool.submit(cancel)
            assert cancelling.wait(10)
        finally:
            release.set()
        result = worker.result(timeout=10)
        conflict = cancellation.result(timeout=10)
    assert result['status'] == 'succeeded'
    assert conflict['status'] == 'conflict'
    assert conflict['run'] == result
    assert Path(payload['execution']['output_path']).read_text(encoding='utf-8-sig') == (
        'id,value\n1,first\n2,last\n')


@pytest.mark.parametrize('phase', ['before_publish', 'after_publish'])
def test_crashed_publication_preserves_uncertainty_or_allows_safe_cancel(tmp_path, phase):
    payload = request(tmp_path)
    database = tmp_path / 'runs.sqlite'
    program = '''
import json, os, sys
from rpe_harness import execution
from rpe_harness.runtime import HarnessRuntime
original = execution.publish_output
def crash(*args, **kwargs):
    if sys.argv[2] == 'after_publish': original(*args, **kwargs)
    os._exit(77)
execution.publish_output = crash
HarnessRuntime(db_path=sys.argv[1]).run(json.load(sys.stdin))
'''
    child = subprocess.run([sys.executable, '-B', '-c', program, str(database), phase],
                           input=json.dumps(payload), text=True, capture_output=True, timeout=10)
    assert child.returncode == 77, child.stderr
    runtime = HarnessRuntime(db_path=database)
    saved = runtime.store.list()[0]
    result = runtime.cancel(saved['run_id'])
    if phase == 'after_publish':
        assert result['status'] == 'conflict'
        assert result['error_code'] == 'publication_started'
        assert result['run'] == saved
        assert Path(payload['execution']['output_path']).exists()
    else:
        assert result['status'] == 'cancelled'
        assert not Path(payload['execution']['output_path']).exists()


def test_legacy_schema_migration_preserves_records_and_foreign_keys(tmp_path):
    database = tmp_path / 'runs.sqlite'
    store = RunStore(database)
    run, _ = store.claim_event('local', 'one', {'legacy': True})
    store.step(run['run_id'], 'execute', {'original': True})
    store._release_lease(run['run_id'])
    # Recreate the pre-C4 CHECK constraint with a referencing table still present.
    with sqlite3.connect(database) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='runs'").fetchone()[0]
        legacy = schema.replace(", 'cancelled'", '').replace(",'cancelled'", '')
        db.execute(legacy.replace('CREATE TABLE runs', 'CREATE TABLE runs_legacy', 1))
        db.execute('INSERT INTO runs_legacy SELECT * FROM runs')
        db.execute('CREATE TABLE task_links (run_id TEXT REFERENCES runs(run_id))')
        db.execute('INSERT INTO task_links VALUES(?)', (run['run_id'],))
        db.execute('DROP TABLE runs')
        db.execute('ALTER TABLE runs_legacy RENAME TO runs')
    store = RunStore(database)
    assert store.cancel(run['run_id'])['run']['event'] == {'legacy': True}
    assert store.get(run['run_id'])['steps'][0]['evidence'] == {'original': True}
    with store.connect() as db:
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert db.execute('SELECT run_id FROM task_links').fetchone()[0] == run['run_id']
