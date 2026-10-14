"""Durable evolution records. Candidate text never grants execution authority."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from ..storage import canonical_json, _read_only_database_path
from ..path_binding import read_regular_file


def digest(value) -> str:
    data = value if isinstance(value, bytes) else canonical_json(value).encode()
    return hashlib.sha256(data).hexdigest()


class EvolutionStore:
    def __init__(self, db_path, owned_root=None):
        self._read_only = False
        submitted = Path(db_path).expanduser()
        if submitted.is_symlink():
            raise ValueError('evolution database must not be a symlink')
        self.path = submitted.resolve()
        if owned_root is not None:
            supplied_root = Path(owned_root).expanduser()
            if supplied_root.is_symlink() or not self.path.is_relative_to(supplied_root.resolve()):
                raise ValueError('evolution database must be inside an owned_root without symlinks')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS evolution_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_feedback(
                    run_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,payload_hash TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_experiences(
                    experience_id TEXT PRIMARY KEY,lookup_key TEXT NOT NULL,state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS evolution_experiences_lookup
                    ON evolution_experiences(lookup_key,state);
                CREATE TABLE IF NOT EXISTS evolution_proposals(
                    proposal_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL UNIQUE,
                    component TEXT NOT NULL,kind TEXT NOT NULL,content_hash TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,base_version_id TEXT,base_generation INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,state TEXT NOT NULL,validation_json TEXT,
                    validation_hash TEXT,version_id TEXT,created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_versions(
                    version_id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL,state TEXT NOT NULL,
                    reason TEXT,created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_active(
                    slot TEXT PRIMARY KEY,version_id TEXT,generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_activations(
                    activation_id TEXT PRIMARY KEY,slot TEXT NOT NULL,version_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,state TEXT NOT NULL,lease_path TEXT,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_maintenance_state(
                    name TEXT PRIMARY KEY,cursor INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS evolution_maintenance_queue(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,work_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,subject_id TEXT NOT NULL,payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('waiting','pending','done')),
                    created_at REAL NOT NULL,updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS evolution_maintenance_pending
                    ON evolution_maintenance_queue(kind,state,updated_at,sequence);
                CREATE TABLE IF NOT EXISTS evolution_maintenance_evidence(
                    diagnosis_key TEXT NOT NULL,source_kind TEXT NOT NULL,source_id TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(diagnosis_key,source_kind,source_id,evidence_hash));
            ''')
            saved = db.execute("SELECT value FROM evolution_meta WHERE key='owned_root'").fetchone()
            root = Path(owned_root).expanduser() if owned_root else (
                Path(saved['value']) if saved else self.path.parent)
            if root.is_symlink():
                raise ValueError('owned_root must not be a symlink')
            self.owned_root = root.resolve()
            if not self.path.is_relative_to(self.owned_root):
                raise ValueError('evolution database must be inside owned_root')
            if saved and Path(saved['value']) != self.owned_root:
                raise ValueError('database already belongs to another owned_root')
            db.execute('INSERT OR IGNORE INTO evolution_meta VALUES(?,?)',
                       ('owned_root', str(self.owned_root)))
        self.owned_root.mkdir(parents=True, exist_ok=True)
        self.recover_activations()

    @classmethod
    def read_only(cls, db_path):
        """Read an initialized store without migrations, recovery, leases or feedback writes."""
        from ..path_binding import _directory
        view = cls.__new__(cls)
        view.path = _read_only_database_path(db_path)
        view._read_only = True
        with view.connect() as db:
            saved = db.execute("SELECT value FROM evolution_meta WHERE key='owned_root'").fetchone()
        if saved is None:
            raise ValueError('read-only evolution inspection requires initialized ownership metadata')
        view.owned_root = Path(saved['value'])
        if not view.owned_root.is_absolute() or not view.path.is_relative_to(view.owned_root):
            raise ValueError('evolution database must be inside its saved owned_root')
        root_descriptor = _directory(view.owned_root)
        os.close(root_descriptor)
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
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def owned_path(self, path) -> Path:
        path = Path(path)
        if not path.is_absolute() or not path.resolve().is_relative_to(self.owned_root):
            raise ValueError('candidate path leaves owned_root')
        cursor = self.owned_root
        for part in path.relative_to(self.owned_root).parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError('candidate paths must not contain symlinks')
        return path

    def write_artifact(self, content: bytes, suffix: str) -> tuple[str, str]:
        if self._read_only:
            raise ValueError('read-only evolution inspection cannot write artifacts')
        content_hash = digest(content)
        directory = self.owned_path(self.owned_root / 'evolution-artifacts')
        directory.mkdir(exist_ok=True)
        path = self.owned_path(directory / (content_hash + suffix))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(path, flags, 0o400)
        except FileExistsError:
            if digest(read_regular_file(path, max_bytes=max(1, len(content)))) != content_hash:
                raise ValueError('immutable candidate artifact is corrupt')
        else:
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return str(path), content_hash

    def read_artifact(self, record) -> bytes:
        path = self.owned_path(record['artifact_path'])
        data = read_regular_file(path, max_bytes=65536)
        if digest(data) != record['content_hash']:
            raise ValueError('candidate artifact hash changed')
        return data

    def list_feedback(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute('SELECT payload_json,payload_hash FROM evolution_feedback ORDER BY created_at').fetchall()
        result = []
        for row in rows:
            item = json.loads(row['payload_json'])
            if digest(item) != row['payload_hash']:
                raise ValueError('verified feedback is corrupt')
            result.append(item)
        return result

    def add_feedback(self, item) -> dict:
        with self.connect() as db:
            existing = db.execute('SELECT payload_hash FROM evolution_feedback WHERE run_id=?',
                                  (item['run_id'],)).fetchone()
            if existing and existing['payload_hash'] != digest(item):
                raise ValueError('durable verified feedback changed')
            db.execute('INSERT OR IGNORE INTO evolution_feedback VALUES(?,?,?,?)',
                       (item['run_id'], canonical_json(item), digest(item), time.time()))
        return item

    def add_experience(self, lookup_key, payload) -> str:
        identifier = digest({'lookup_key': lookup_key, 'recipe': payload['recipe']})
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO evolution_experiences VALUES(?,?,?,?,?)',
                       (identifier, lookup_key, 'active', canonical_json(payload), time.time()))
        return identifier

    def list_experiences(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute('SELECT * FROM evolution_experiences ORDER BY created_at').fetchall()
        return [dict(row, **json.loads(row['payload_json'])) for row in rows]

    def lookup_experiences(self, lookup_key) -> list[dict]:
        with self.connect() as db:
            rows = db.execute('SELECT * FROM evolution_experiences WHERE lookup_key=? ORDER BY created_at',
                              (lookup_key,)).fetchall()
        return [dict(row, **json.loads(row['payload_json'])) for row in rows]

    def set_experience_state(self, experience_id, state):
        if state not in {'active', 'dormant', 'quarantine'}:
            raise ValueError('invalid experience lifecycle')
        with self.connect() as db:
            if not db.execute('UPDATE evolution_experiences SET state=? WHERE experience_id=?',
                              (state, experience_id)).rowcount:
                raise KeyError(experience_id)

    @staticmethod
    def _proposal(row):
        if row is None:
            raise KeyError('proposal not found')
        item = dict(row)
        item.update(json.loads(item.pop('payload_json')))
        validation = item.pop('validation_json')
        item['validation'] = json.loads(validation) if validation else None
        return item

    def get_proposal(self, proposal_id) -> dict:
        with self.connect() as db:
            return self._proposal(db.execute('SELECT * FROM evolution_proposals WHERE proposal_id=?',
                                             (proposal_id,)).fetchone())

    def list_proposals(self, *, limit=100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('proposal limit must be 1..1000')
        with self.connect() as db:
            rows = db.execute('SELECT * FROM evolution_proposals ORDER BY created_at LIMIT ?',
                              (limit,)).fetchall()
        return [self._proposal(row) for row in rows]

    def pointer(self, component, kind) -> tuple[str | None, int]:
        with self.connect() as db:
            row = db.execute('SELECT * FROM evolution_active WHERE slot=?',
                             (component + ':' + kind,)).fetchone()
        return (row['version_id'], row['generation']) if row else (None, 0)

    def get_version(self, version_id) -> dict:
        with self.connect() as db:
            version = db.execute('SELECT * FROM evolution_versions WHERE version_id=?',
                                 (version_id,)).fetchone()
        if version is None:
            raise KeyError(version_id)
        item = self.get_proposal(version['proposal_id'])
        item.update(version_id=version['version_id'], state=version['state'], reason=version['reason'])
        return item

    def list_versions(self) -> list[dict]:
        with self.connect() as db:
            ids = [row[0] for row in db.execute('SELECT version_id FROM evolution_versions ORDER BY created_at')]
        return [self.get_version(identifier) for identifier in ids]

    def get_active(self, component='executor', kind='csv_executor') -> dict | None:
        identifier, _ = self.pointer(component, kind)
        return self.get_version(identifier) if identifier else None

    def recover_activations(self):
        if self._read_only:
            raise ValueError('read-only evolution inspection cannot recover activations')
        with self.connect() as db:
            rows = db.execute("SELECT activation_id,lease_path FROM evolution_activations WHERE state='prepared'").fetchall()
        for row in rows:
            lease_path = self.owned_path(row['lease_path'])
            lease = lease_path.open('a+b')
            try:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                with self.connect() as db:
                    db.execute("UPDATE evolution_activations SET state='aborted' WHERE activation_id=? AND state='prepared'",
                               (row['activation_id'],))
                lease_path.unlink(missing_ok=True)
            finally:
                lease.close()

    def rollback(self, version_id):
        if self._read_only:
            raise ValueError('read-only evolution inspection cannot roll back versions')
        from .candidates import rollback
        return rollback(version_id, self.path)
