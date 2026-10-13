"""Durable local events and verified run records. No database is opened at import."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class EventConflict(ValueError):
    """An idempotency key was reused for a different request."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


class RunStore:
    def __init__(self, path: Path | str):
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
                    CHECK(status IN ('received','running','succeeded','rejected','failed','interrupted')),
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

    @contextmanager
    def connect(self):
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
            db.execute("UPDATE runs SET status='running',stage='validate',state_json=?,skill_id=? WHERE run_id=? AND status='received'",
                       (canonical_json(state) if state else None, skill_id, run_id))

    def step(self, run_id: str, stage: str, evidence: dict | None = None,
             *, skill_id: str | None = None, attempted: bool = False) -> None:
        with self.connect() as db:
            db.execute('UPDATE runs SET stage=?,skill_id=COALESCE(?,skill_id),attempted=MAX(attempted,?) WHERE run_id=?',
                       (stage, skill_id, int(attempted), run_id))
            db.execute('INSERT INTO steps(run_id,stage,evidence_json,created_at) VALUES(?,?,?,?)',
                       (run_id, stage, canonical_json(evidence or {}), time.time()))

    def finish(self, run_id: str, status: str, *, verification: dict | None = None,
               artifact: dict | None = None, error: dict | None = None) -> dict:
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

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
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
        with self.connect() as db:
            rows = db.execute("SELECT run_id,owner_pid FROM runs WHERE status IN ('received','running')").fetchall()
        count = 0
        for row in rows:
            lease_path = self.lease_dir / row['run_id']
            lease = lease_path.open('a+b') if lease_path.exists() else None
            try:
                if lease is not None:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.finish(row['run_id'], 'interrupted', error={'code': 'owner_process_exited',
                            'message': 'previous owner exited before recording a verified result'})
                count += 1
            except BlockingIOError:
                pass
            finally:
                if lease is not None:
                    lease.close()
                if self.get(row['run_id'])['status'] == 'interrupted':
                    lease_path.unlink(missing_ok=True)
        return count

    def report_usage(self, skill_id: str, success: bool):
        with self.connect() as db:
            db.execute('INSERT INTO reported_usage(skill_id,success,created_at) VALUES(?,?,?)',
                       (skill_id, int(success), time.time()))

    def usage(self) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT skill_id,COUNT(*) AS n,SUM(status='succeeded') AS successes,MAX(started_at) AS last_used FROM runs WHERE skill_id IS NOT NULL AND attempted=1 AND status IN ('succeeded','failed','interrupted') GROUP BY skill_id").fetchall()
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
