"""Durable local events and verified run records. No database is opened at import."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
import fcntl
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class EventConflict(ValueError):
    """An idempotency key was reused for a different request."""


class RunCancelled(RuntimeError):
    """A durable cancellation fenced further work by this run's owner."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


def _output_may_exist(path, parent_identity=None):
    """Only proven absence permits cancellation after publication intent."""
    if not isinstance(path, str) or not path or parent_identity is None:
        return True
    try:
        parent = os.stat(Path(path).parent, follow_symlinks=False)
    except OSError:
        return True
    if [parent.st_dev, parent.st_ino] != parent_identity:
        return True
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _read_only_database_path(path):
    """Require an existing regular database without resolving submitted symlink aliases."""
    from .path_binding import _directory
    path = Path(os.path.abspath(Path(path).expanduser()))
    parent = _directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError('read-only database must be an existing regular file')
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    return path


class RunStore:
    def __init__(self, path: Path | str):
        self._read_only = False
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_dir = self.path.parent / ('.' + self.path.name + '.leases')
        self._leases = {}
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, event_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL, received_at REAL NOT NULL,
                    UNIQUE(source,event_id));
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, event_fk INTEGER NOT NULL UNIQUE REFERENCES events(id),
                    status TEXT NOT NULL DEFAULT 'received', stage TEXT NOT NULL DEFAULT 'receive',
                    owner_pid INTEGER NOT NULL, started_at REAL NOT NULL, finished_at REAL,
                    state_json TEXT, skill_id TEXT, attempted INTEGER NOT NULL DEFAULT 0,
                    verification_json TEXT, verification_ok INTEGER NOT NULL DEFAULT 0,
                    artifact_json TEXT, error_json TEXT,
                    CHECK(status IN ('received','running','succeeded','rejected','failed','interrupted','cancelled')),
                    CHECK(status != 'succeeded' OR verification_ok=1));
                CREATE TABLE IF NOT EXISTS steps (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    stage TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS reported_usage (
                    id INTEGER PRIMARY KEY, skill_id TEXT NOT NULL, success INTEGER NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_states (
                    skill_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, dormant INTEGER NOT NULL);
            ''')
            self._migrate_cancellation(db)

    @staticmethod
    def _migrate_cancellation(db):
        """Extend the original CHECK without losing rows or inbound foreign keys."""
        db.commit()
        db.execute('PRAGMA foreign_keys=OFF')
        try:
            db.execute('BEGIN IMMEDIATE')
            schema = db.execute("SELECT sql FROM sqlite_master WHERE name='runs'").fetchone()[0]
            if "'cancelled'" not in schema:
                objects = db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='runs' "
                                     "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
                replacement = re.sub(r'^CREATE TABLE\s+(?:"runs"|runs)\s*\(',
                                     'CREATE TABLE runs_c4 (', schema, count=1, flags=re.I)
                replacement = replacement.replace("'interrupted'", "'interrupted','cancelled'", 1)
                db.execute(replacement)
                db.execute('INSERT INTO runs_c4 SELECT * FROM runs')
                db.execute('DROP TABLE runs')
                db.execute('ALTER TABLE runs_c4 RENAME TO runs')
                for item in objects:
                    db.execute(item[0])
                if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise ValueError('run status migration would violate foreign keys')
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.execute('PRAGMA foreign_keys=ON')

    @classmethod
    def read_only(cls, path):
        """Inspect existing run records without schema, journal-mode or lease initialization."""
        view = cls.__new__(cls)
        view.path = _read_only_database_path(path)
        view._read_only = True
        return view

    @contextmanager
    def connect(self):
        if self._read_only:
            _read_only_database_path(self.path)
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10)
            db.execute('PRAGMA query_only=ON')
        else:
            db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim_event(self, source: str, event_id: str, payload: dict) -> tuple[dict, bool]:
        raw = canonical_json(payload)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM events WHERE source=? AND event_id=?',
                                  (source, event_id)).fetchone()
            if existing:
                if existing['payload_hash'] != digest:
                    raise EventConflict('event_id already exists with different content')
                row = db.execute('SELECT run_id FROM runs WHERE event_fk=?', (existing['id'],)).fetchone()
                run_id, fresh = row['run_id'], False
            else:
                event = db.execute('INSERT INTO events(source,event_id,payload_hash,payload_json,received_at) VALUES(?,?,?,?,?)',
                                   (source, event_id, digest, raw, time.time()))
                run_id, fresh = uuid.uuid4().hex, True
                db.execute('INSERT INTO runs(run_id,event_fk,owner_pid,started_at) VALUES(?,?,?,?)',
                           (run_id, event.lastrowid, os.getpid(), time.time()))
                self.lease_dir.mkdir(exist_ok=True)
                lease = (self.lease_dir / run_id).open('a+b')
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._leases[run_id] = lease
        return self.get(run_id), fresh

    def mark_running(self, run_id: str, state: dict | None = None,
                     skill_id: str | None = None) -> None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._check_cancelled(db, run_id)
            db.execute("UPDATE runs SET status='running',stage='validate',state_json=?,skill_id=? WHERE run_id=? AND status='received'",
                       (canonical_json(state) if state else None, skill_id, run_id))

    def step(self, run_id: str, stage: str, evidence: dict | None = None,
             *, skill_id: str | None = None, attempted: bool = False) -> None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._check_cancelled(db, run_id)
            db.execute('UPDATE runs SET stage=?,skill_id=COALESCE(?,skill_id),attempted=MAX(attempted,?) WHERE run_id=?',
                       (stage, skill_id, int(attempted), run_id))
            db.execute('INSERT INTO steps(run_id,stage,evidence_json,created_at) VALUES(?,?,?,?)',
                       (run_id, stage, canonical_json(evidence or {}), time.time()))

    @staticmethod
    def _check_cancelled(db, run_id):
        row = db.execute('SELECT status FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        if row['status'] == 'cancelled':
            raise RunCancelled(run_id)
        return row['status']

    def cancel(self, run_id: str, reason: str | None = None) -> dict:
        """Cancel once, or explicitly preserve an already terminal/published result."""
        if reason is not None:
            if (not isinstance(reason, str) or not reason.strip()
                    or len(reason.encode('utf-8', errors='replace')) > 4096):
                raise ValueError('reason must be nonempty UTF-8 text of at most 4096 bytes')
            try:
                reason.encode('utf-8')
            except UnicodeError as error:
                raise ValueError('reason must be valid UTF-8 text') from error
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,artifact_json FROM runs WHERE run_id=?',
                             (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            previous = row['status']
            conflict = None
            if previous in ('succeeded', 'rejected', 'failed'):
                conflict = 'already_terminal'
            elif previous != 'cancelled':
                # A process may have died after os.link but before success committed.
                # Never relabel this ambiguous published artifact as cancelled.
                intents = db.execute("SELECT stage,evidence_json FROM steps WHERE run_id=? "
                                     "AND stage IN ('publish','complete')", (run_id,)).fetchall()
                if row['artifact_json'] or any(
                    item['stage'] == 'complete' or evidence.get('publication_confirmed') is True
                    or _output_may_exist(evidence.get('output_path') or evidence.get('path'),
                                        evidence.get('output_parent_identity'))
                    for item in intents for evidence in [json.loads(item['evidence_json'])]
                ):
                    conflict = 'publication_started'
                # Recovery reservation and cancellation use the same write lock.
                # Once handed off, this parent cannot promise to stop its successor.
                recovery = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                      "AND name='recovery_attempts'").fetchone()
                if not conflict and recovery and db.execute(
                    'SELECT 1 FROM recovery_attempts WHERE retry_of=?', (run_id,)
                ).fetchone():
                    conflict = 'recovery_started'
                if not conflict:
                    timestamp = time.time()
                    db.execute("UPDATE runs SET status='cancelled',stage='cancel',finished_at=? "
                               'WHERE run_id=?', (timestamp, run_id))
                    db.execute('INSERT INTO steps(run_id,stage,evidence_json,created_at) '
                               'VALUES(?,?,?,?)', (run_id, 'cancel', canonical_json({
                                   'reason': reason, 'previous_status': previous}), timestamp))
        if not conflict:
            self._release_lease(run_id)
            self._reap_cancelled_lease(run_id)
        record = self.get(run_id)
        if conflict:
            return {'status': 'conflict', 'error_code': conflict, 'run_id': run_id, 'run': record}
        return {'status': 'cancelled', 'run_id': run_id, 'run': record,
                'already_cancelled': previous == 'cancelled'}

    def complete_publication(self, run_id: str, publisher, *, verification: dict,
                             artifact: dict) -> dict:
        """Serialize the no-overwrite link and success receipt against cancellation.

        The caller saves publication intent first, so even an owner crash between
        the file link and SQLite commit cannot make cancellation falsify history.
        """
        if verification.get('passed') is not True or not artifact:
            raise ValueError('success requires verified published artifact')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._check_cancelled(db, run_id) not in ('received', 'running'):
                raise ValueError('run is already terminal')
            publisher()
            timestamp = time.time()
            db.execute('INSERT INTO steps(run_id,stage,evidence_json,created_at) VALUES(?,?,?,?)',
                       (run_id, 'complete', canonical_json(artifact), timestamp))
            db.execute("UPDATE runs SET status='succeeded',stage='complete',finished_at=?,"
                       'verification_json=?,verification_ok=1,artifact_json=?,error_json=NULL '
                       'WHERE run_id=?',
                       (timestamp, canonical_json(verification), canonical_json(artifact), run_id))
        self._release_lease(run_id)
        return self.get(run_id)

    def finish(self, run_id: str, status: str, *, verification: dict | None = None,
               artifact: dict | None = None, error: dict | None = None) -> dict:
        if status == 'cancelled':
            raise ValueError('use cancel to record an auditable cancellation')
        verified = verification is not None and verification.get('passed') is True
        if status == 'succeeded' and (not verified or not artifact):
            raise ValueError('success requires verified published artifact')
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?,finished_at=?,verification_json=?,verification_ok=?,artifact_json=?,error_json=? WHERE run_id=? AND status IN ('received','running')",
                       (status, time.time(), canonical_json(verification) if verification else None,
                        int(verified), canonical_json(artifact) if artifact else None,
                        canonical_json(error) if error else None, run_id))
        self._release_lease(run_id)
        return self.get(run_id)

    def _release_lease(self, run_id):
        lease = self._leases.pop(run_id, None)
        if lease is not None:
            lease.close()
            (self.lease_dir / run_id).unlink(missing_ok=True)

    def _reap_cancelled_lease(self, run_id):
        """Remove an abandoned lease, without unlinking a live owner's lock."""
        path = self.lease_dir / run_id
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            owned = os.fstat(descriptor)
            try:
                current = path.lstat()
            except FileNotFoundError:
                return
            if ((current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino)
                    and stat.S_ISREG(current.st_mode)):
                path.unlink(missing_ok=True)
        finally:
            os.close(descriptor)

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            # Status and audit steps must describe the same committed snapshot.
            db.execute('BEGIN')
            row = db.execute('SELECT r.*,e.source,e.event_id,e.payload_hash,e.payload_json FROM runs r JOIN events e ON r.event_fk=e.id WHERE run_id=?',
                             (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            steps = db.execute('SELECT stage,evidence_json,created_at FROM steps WHERE run_id=? ORDER BY id', (run_id,)).fetchall()
        result = dict(row)
        for src, dest in [('state_json', 'state'), ('verification_json', 'verification'),
                          ('artifact_json', 'artifact'), ('error_json', 'error'), ('payload_json', 'event')]:
            value = result.pop(src)
            result[dest] = json.loads(value) if value else None
        result['steps'] = [{'stage': r['stage'], 'evidence': json.loads(r['evidence_json']),
                            'created_at': r['created_at']} for r in steps]
        result.pop('event_fk')
        return result

    def list(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be an integer from 1 to 100')
        with self.connect() as db:
            rows = db.execute('SELECT run_id FROM runs ORDER BY started_at DESC LIMIT ?', (limit,)).fetchall()
        return [self.get(r['run_id']) for r in rows]

    def recover_interrupted(self) -> int:
        if self._read_only:
            raise ValueError('read-only run inspection cannot recover owners')
        with self.connect() as db:
            rows = db.execute("SELECT run_id,owner_pid FROM runs WHERE status IN ('received','running')").fetchall()
        count = 0
        for row in rows:
            lease_path = self.lease_dir / row['run_id']
            lease = lease_path.open('a+b') if lease_path.exists() else None
            try:
                if lease is not None:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.finish(row['run_id'], 'interrupted', error={'code': 'owner_process_exited',
                            'message': 'previous owner exited before recording a verified result'})
                count += result['status'] == 'interrupted'
            except BlockingIOError:
                pass
            finally:
                if lease is not None:
                    lease.close()
                if self.get(row['run_id'])['status'] == 'interrupted':
                    lease_path.unlink(missing_ok=True)
        with self.connect() as db:
            cancelled = db.execute("SELECT run_id FROM runs WHERE status='cancelled'").fetchall()
        for row in cancelled:
            self._reap_cancelled_lease(row['run_id'])
        return count

    def report_usage(self, skill_id: str, success: bool):
        with self.connect() as db:
            db.execute('INSERT INTO reported_usage(skill_id,success,created_at) VALUES(?,?,?)',
                       (skill_id, int(success), time.time()))

    def usage(self) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT skill_id,COUNT(*) AS n,SUM(status='succeeded') AS successes,MAX(started_at) AS last_used FROM runs WHERE skill_id IS NOT NULL AND attempted=1 AND status IN ('succeeded','failed','interrupted','cancelled') GROUP BY skill_id").fetchall()
            reports = db.execute('SELECT skill_id,COUNT(*) AS n,SUM(success) AS successes,MAX(created_at) AS last_used FROM reported_usage GROUP BY skill_id').fetchall()
        result = {}
        for row in rows:
            result[row['skill_id']] = {'use_count': row['n'], 'verified_success_count': row['successes'],
                                      'reported_count': 0, 'reported_success_count': 0,
                                      'last_used': row['last_used']}
        for row in reports:
            item = result.setdefault(row['skill_id'], {'use_count': 0, 'verified_success_count': 0,
                                        'reported_count': 0, 'reported_success_count': 0, 'last_used': 0})
            item['use_count'] += row['n']
            item['reported_count'] = row['n']
            item['reported_success_count'] = row['successes']
            item['last_used'] = max(item['last_used'], row['last_used'])
        for item in result.values():
            verified_attempts = item['use_count'] - item['reported_count']
            item['verified_success_rate'] = (item['verified_success_count'] / verified_attempts
                                            if verified_attempts else None)
        return result

    def set_skill_state(self, skill_id: str, enabled: bool, dormant: bool):
        with self.connect() as db:
            db.execute('INSERT INTO skill_states VALUES(?,?,?) ON CONFLICT(skill_id) DO UPDATE SET enabled=excluded.enabled,dormant=excluded.dormant',
                       (skill_id, int(enabled), int(dormant)))

    def skill_states(self) -> dict:
        with self.connect() as db:
            return {r['skill_id']: {'enabled': bool(r['enabled']), 'is_dormant': bool(r['dormant'])}
                    for r in db.execute('SELECT * FROM skill_states')}
