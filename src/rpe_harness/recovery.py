"""Bounded linked retries of stopped attempts with no publication ambiguity."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .contracts import ExecutionPlan, EXECUTOR_REVISION, VERIFIER_REVISION


def _initialize(store) -> None:
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS recovery_attempts (
            retry_of TEXT PRIMARY KEY REFERENCES runs(run_id),
            root_run_id TEXT NOT NULL REFERENCES runs(run_id),
            retry_event_id TEXT NOT NULL UNIQUE,
            retry_run_id TEXT UNIQUE REFERENCES runs(run_id),
            plan_sha256 TEXT NOT NULL,
            created_at REAL NOT NULL)''')


@contextmanager
def _chain_lock(store, root_run_id: str):
    directory = store.path.parent / ('.' + store.path.name + '.recovery')
    directory.mkdir(exist_ok=True)
    # root_run_id comes from a persisted UUID, never directly from a path input.
    lock_path = directory / (hashlib.sha256(root_run_id.encode()).hexdigest() + '.lock')
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def _stopped_owner(store, original: dict):
    path = store.lease_dir / original['run_id']
    lease = None
    try:
        if path.exists():
            lease = path.open('r+b')
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('original attempt owner still holds its lease') from error
        elif original['finished_at'] is None:
            raise ValueError('original owner termination is not confirmed')
        yield
    finally:
        if lease is not None:
            lease.close()


def _saved_plan(original: dict) -> ExecutionPlan:
    evidence = [step['evidence'] for step in original['steps'] if step['stage'] == 'plan']
    if len(evidence) != 1:
        raise ValueError('original immutable plan is unavailable or ambiguous')
    plan = ExecutionPlan.from_dict(evidence[0].get('plan'))
    if evidence[0].get('plan_sha256') != plan.sha256:
        raise ValueError('original plan hash does not match persisted evidence')
    if plan.executor_revision != EXECUTOR_REVISION or plan.verifier_revision != VERIFIER_REVISION:
        raise ValueError('original executor or verifier revision is no longer loaded')
    return plan


def _guards(original: dict, plan: ExecutionPlan) -> None:
    if original['status'] not in ('failed', 'interrupted'):
        raise ValueError('only a stopped failed or interrupted attempt can be retried')
    if original.get('artifact') or any(step['stage'] in ('publish', 'complete') for step in original['steps']):
        raise ValueError('publication started or its outcome is unknown; reconcile explicitly')
    source, output = Path(plan.input_path), Path(plan.output_path)
    if not source.is_file() or source.is_symlink():
        raise ValueError('original input is no longer a regular file')
    if hashlib.sha256(source.read_bytes()).hexdigest() != plan.input_sha256:
        raise ValueError('original input changed; recovery cannot reinterpret it')
    if os.path.lexists(output):
        raise ValueError('output already exists; automatic recovery cannot overwrite it')


def _linked(store, original_id: str, child_id: str) -> dict:
    result = store.get(child_id)
    result['retry_of'] = original_id
    return result


def recover_run(runtime, run_id: str, *, max_attempts: int = 3) -> dict:
    """Create at most one successor per attempt, under the original frozen plan.

    The total budget includes the original attempt. Calling again with the same
    parent returns its saved successor, even after a lost response or restart.
    A failed successor requires an explicit request using that successor's ID.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise ValueError('max_attempts must be an integer from 1 to 10')
    store = runtime.store
    original = store.get(run_id)
    _initialize(store)
    with store.connect() as db:
        parent = db.execute('SELECT root_run_id FROM recovery_attempts WHERE retry_run_id=?',
                            (run_id,)).fetchone()
    root_id = parent['root_run_id'] if parent else run_id
    with _chain_lock(store, root_id):
        original = store.get(run_id)
        with store.connect() as db:
            reserved = db.execute('SELECT * FROM recovery_attempts WHERE retry_of=?', (run_id,)).fetchone()
            if reserved:
                if reserved['retry_run_id']:
                    return _linked(store, run_id, reserved['retry_run_id'])
                claimed = db.execute('SELECT r.run_id FROM runs r JOIN events e ON r.event_fk=e.id '
                                     'WHERE e.source=? AND e.event_id=?',
                                     ('recovery', reserved['retry_event_id'])).fetchone()
                if claimed:
                    db.execute('UPDATE recovery_attempts SET retry_run_id=? WHERE retry_of=?',
                               (claimed['run_id'], run_id))
                    return _linked(store, run_id, claimed['run_id'])
        if original['status'] not in ('failed', 'interrupted'):
            raise ValueError('only failed or interrupted attempts can be retried')
        with _stopped_owner(store, original):
            plan = _saved_plan(original)
            _guards(original, plan)
            if not reserved:
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    count = db.execute('SELECT COUNT(*) AS n FROM recovery_attempts WHERE root_run_id=?',
                                       (root_id,)).fetchone()['n']
                    if 1 + count >= max_attempts:
                        raise ValueError('recovery attempt budget is exhausted')
                    event_id = 'retry:' + run_id
                    db.execute('INSERT INTO recovery_attempts '
                               '(retry_of,root_run_id,retry_event_id,plan_sha256,created_at) VALUES(?,?,?,?,?)',
                               (run_id, root_id, event_id, plan.sha256, time.time()))
            else:
                event_id = reserved['retry_event_id']
                if reserved['plan_sha256'] != plan.sha256:
                    raise ValueError('reserved retry does not match original plan')
            request = copy.deepcopy(original['event'])
            request['event_id'] = event_id
            result = runtime.run(request, source='recovery', expected_plan=plan, retry_of=run_id)
            with store.connect() as db:
                db.execute('UPDATE recovery_attempts SET retry_run_id=? WHERE retry_of=?',
                           (result['run_id'], run_id))
            return _linked(store, run_id, result['run_id'])
