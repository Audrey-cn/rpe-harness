"""Bounded durable scheduling. Queue records never authorize activation."""
from __future__ import annotations

import os
import stat

from ..file_lock import lock_exclusive
import time
from contextlib import contextmanager
from pathlib import Path

from ..storage import canonical_json
from .store import digest


@contextmanager
def maintenance_lease(db_path, owned_root):
    root = Path(owned_root).expanduser()
    submitted = Path(db_path).expanduser()
    if root.is_symlink() or submitted.is_symlink():
        raise ValueError('maintenance ownership paths must not be symlinks')
    root = root.resolve()
    if not submitted.resolve().is_relative_to(root):
        raise ValueError('maintenance database must be inside owned_root')
    root.mkdir(parents=True, exist_ok=True)
    from ..path_binding import _directory
    directory = _directory(root)
    try:
        fd = os.open('.evolution-maintenance.lease', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError('maintenance lease must be a regular file')
    finally:
        os.close(directory)
    with os.fdopen(fd, 'a+b') as lease:
        try:
            lock_exclusive(lease, blocking=False)
        except BlockingIOError:
            yield False
        else:
            # Keep the inode: unlinking a lease permits another owner to lock a
            # new inode while a previously opened waiter owns the original.
            yield True


def _cursor(db, tables, name):
    if 'evolution_maintenance_state' not in tables:
        return 0
    row = db.execute('SELECT cursor FROM evolution_maintenance_state WHERE name=?',
                     (name,)).fetchone()
    return row[0] if row else 0


def preview(runs_store, limit):
    """Read one discovery batch and one independent background batch, without writes."""
    empty = {'new': [], 'work': [], 'recheck': [], 'host': [], 'cursor': 0,
             'host_cursor': 0, 'recheck_cursor': 0}
    if runs_store is None:
        return empty
    with runs_store.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cursor = _cursor(db, tables, 'runs')
        host_cursor = _cursor(db, tables, 'host')
        recheck_cursor = _cursor(db, tables, 'recheck')
        new = [dict(row) for row in db.execute(
            'SELECT e.id,r.run_id,r.status FROM events e JOIN runs r ON r.event_fk=e.id '
            'WHERE e.id>? ORDER BY e.id LIMIT ?', (cursor, limit))] if 'runs' in tables else []
        pending = []
        recheck = []
        if 'evolution_maintenance_queue' in tables:
            pending = [row[0] for row in db.execute(
                "SELECT subject_id FROM evolution_maintenance_queue WHERE kind='run' "
                "AND state IN ('waiting','pending') ORDER BY updated_at,sequence LIMIT ?", (limit,))]
            recheck = [dict(row) for row in db.execute(
                "SELECT sequence,subject_id FROM evolution_maintenance_queue WHERE kind='run' "
                "AND state='done' AND sequence>? ORDER BY sequence LIMIT ?", (recheck_cursor, limit))]
            if not recheck:
                recheck = [dict(row) for row in db.execute(
                    "SELECT sequence,subject_id FROM evolution_maintenance_queue WHERE kind='run' "
                    "AND state='done' ORDER BY sequence LIMIT ?", (limit,))]
        work = list(dict.fromkeys([*pending, *[row['run_id'] for row in new]]))[:limit]
        # Newly discovered rows not inspected this turn remain in the queue.
        host = [dict(row) for row in db.execute(
            'SELECT id,ingest_event_id,kind,metadata_json FROM hermes_host_events '
            'WHERE id>? ORDER BY id LIMIT ?', (host_cursor, limit))] if 'hermes_host_events' in tables else []
    return {'new': new, 'work': work, 'recheck': recheck, 'host': host,
            'cursor': new[-1]['id'] if new else cursor,
            'host_cursor': host[-1]['id'] if host else host_cursor,
            'recheck_cursor': recheck[-1]['sequence'] if recheck else recheck_cursor}


def discover(store, batch):
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in batch['new']:
            state = 'waiting' if row['status'] in {'received', 'running'} else 'pending'
            db.execute('INSERT OR IGNORE INTO evolution_maintenance_queue '
                       '(work_key,kind,subject_id,payload_json,state,created_at,updated_at) '
                       'VALUES(?,?,?,?,?,?,?)',
                       ('run:' + row['run_id'], 'run', row['run_id'], '{}', state, now, now))
        for row in batch['host']:
            db.execute('INSERT OR IGNORE INTO evolution_maintenance_queue '
                       '(work_key,kind,subject_id,payload_json,state,created_at,updated_at) '
                       'VALUES(?,?,?,?,?,?,?)',
                       ('host:' + row['ingest_event_id'], 'host', row['ingest_event_id'],
                        canonical_json(row), 'pending', now, now))
        for name, value in [('runs', batch['cursor']), ('host', batch['host_cursor'])]:
            db.execute('INSERT INTO evolution_maintenance_state VALUES(?,?) '
                       'ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor', (name, value))


def semantic_key(candidate, origin):
    from .candidates import current_revisions
    content = candidate['content']
    if origin == 'default_diagnostic':
        omitted = {'run_id', 'plan_sha256', 'retry_of', 'evidence_sha256',
                   'host_event_id', 'metadata_sha256'}
        def facts(items):
            return sorted({canonical_json({key: value for key, value in item.items()
                                           if key not in omitted}) for item in items})
        content = {**{key: value for key, value in content.items()
                      if key not in {'problems', 'host_errors', 'evidence_hashes'}},
                   'problems': facts(content['problems']),
                   'host_errors': facts(content['host_errors'])}
    identity = {'component': candidate['component'], 'kind': candidate['kind'],
                'content': content, 'revisions': current_revisions(candidate['kind'])}
    if origin == 'external_injected_code':
        identity['candidate'] = candidate
    return digest(identity)


def enqueue(store, candidate, origin, references):
    key = semantic_key(candidate, origin)
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO evolution_maintenance_queue '
                   '(work_key,kind,subject_id,payload_json,state,created_at,updated_at) '
                   'VALUES(?,?,?,?,?,?,?)',
                   ('candidate:' + key, 'candidate', key,
                    canonical_json({'candidate': candidate, 'origin': origin}), 'pending', now, now))
        for source, item in references.items():
            kind, identifier = ('host', source[5:]) if source.startswith('host:') else ('run', source)
            db.execute('INSERT OR IGNORE INTO evolution_maintenance_evidence VALUES(?,?,?,?,?,?)',
                       (key, kind, identifier, item['sha256'], canonical_json(item), now))
    return key


def update_work(store, key, state, payload=None):
    with store.connect() as db:
        if payload is None:
            db.execute('UPDATE evolution_maintenance_queue SET state=?,updated_at=? WHERE work_key=?',
                       (state, time.time(), key))
        else:
            db.execute('UPDATE evolution_maintenance_queue SET state=?,payload_json=?,updated_at=? '
                       'WHERE work_key=?', (state, canonical_json(payload), time.time(), key))


def summary(store, inspected, *, busy=False, auto_apply=False):
    with store.connect() as db:
        cursor = db.execute('SELECT name,cursor FROM evolution_maintenance_state').fetchall()
        positions = {row[0]: row[1] for row in cursor}
        remaining = db.execute('SELECT count(*) FROM events e JOIN runs r ON r.event_fk=e.id '
                               'WHERE e.id>?', (positions.get('runs', 0),)).fetchone()[0]
        remaining += db.execute("SELECT count(*) FROM evolution_maintenance_queue "
                                "WHERE kind='run' AND state!='done'").fetchone()[0]
        waiting = db.execute("SELECT count(*) FROM evolution_maintenance_queue "
                             "WHERE kind='run' AND state='waiting'").fetchone()[0]
        proposals = db.execute("SELECT count(*) FROM evolution_maintenance_queue "
                               "WHERE kind='candidate' AND state='pending'").fetchone()[0]
        if auto_apply:
            proposals += db.execute("SELECT count(*) FROM evolution_maintenance_queue "
                "WHERE kind='candidate' AND state='done' "
                "AND json_extract(payload_json,'$.deferred_apply')=1").fetchone()[0]
        host = db.execute("SELECT count(*) FROM evolution_maintenance_queue "
                          "WHERE kind='host' AND state='pending'").fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='hermes_host_events'").fetchone():
            host += db.execute('SELECT count(*) FROM hermes_host_events WHERE id>?',
                               (positions.get('host', 0),)).fetchone()[0]
    return {'state': 'busy' if busy else ('pending' if remaining or proposals or host else 'idle'),
            'cursor': positions, 'inspected_run_ids': inspected,
            'remaining_run_count': remaining, 'waiting_run_count': waiting,
            'pending_proposal_count': proposals, 'remaining_host_count': host,
            'has_more': bool(remaining or proposals or host), 'busy': busy}
