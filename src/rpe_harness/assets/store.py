"""Immutable inert assets in the existing evolution database and owned root."""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

from ..evolution.store import EvolutionStore, digest
from ..storage import canonical_json
from .manifest import (MAX_CONTENT, _directory, _parent, _read_at, _read_file,
                       _read_package, content_digest, valid_hash)


def _write_at(directory_fd, relative, raw):
    with _parent(directory_fd, relative, create=True) as (fd, name):
        try:
            target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o400, dir_fd=fd)
        except FileExistsError:
            if _read_at(fd, name, len(raw)) != raw:
                raise ValueError('immutable asset file is corrupt')
            return
        try:
            with os.fdopen(target, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            os.unlink(name, dir_fd=fd)
            raise
        os.fsync(fd)


def _trusted_system_path(path):
    # Only these operating-system aliases are canonicalized; descendants stay literal.
    path = Path(os.path.abspath(Path(path).expanduser()))
    for alias, canonical in (('/var', '/private/var'), ('/tmp', '/private/tmp')):
        if path == Path(alias) or path.is_relative_to(Path(alias)):
            if Path(alias).is_symlink() and Path(alias).resolve() == Path(canonical):
                return Path(canonical) / path.relative_to(alias)
    return path


class AssetStore:
    def __init__(self, db_path, owned_root=None):
        db_path = _trusted_system_path(db_path)
        owned_root = _trusted_system_path(owned_root) if owned_root is not None else None
        if owned_root is not None and not db_path.is_relative_to(owned_root):
            raise ValueError('asset database must be inside owned_root')
        with _directory(db_path.parent, create=True):
            pass
        if os.path.lexists(db_path) and not stat.S_ISREG(db_path.lstat().st_mode):
            raise ValueError('asset database must be a regular file without symlinks')
        self.evolution = EvolutionStore(db_path, owned_root)
        self.path = self.evolution.path
        self.owned_root = self.evolution.owned_root
        self.root = self.owned_root / 'assets'
        with _directory(self.root, create=True):
            pass
        with self.evolution.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS assets ('
                       'asset_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,'
                       'payload_hash TEXT NOT NULL,created_at REAL NOT NULL)')

    @classmethod
    def from_evolution(cls, evolution_store):
        """Reuse an initialized store without writes, including during activation locks.

        The assets table must already exist. Source verification may safely read
        this view while the caller holds the evolution database write transaction.
        """
        view = cls.__new__(cls)
        view.evolution = evolution_store
        view.path = evolution_store.path
        view.owned_root = evolution_store.owned_root
        view.root = view.owned_root / 'assets'
        return view

    def import_package(self, path) -> dict:
        item, manifest_raw, signature_raw, raw = _read_package(path)
        return self._retain(item, manifest_raw, signature_raw, raw, str(Path(path).absolute()))

    def _retain(self, item, manifest_raw, signature_raw, raw, origin):
        asset_id = digest({'manifest': item['manifest'], 'signature': item['signature']})
        with self.evolution.connect() as db:
            existing = db.execute('SELECT asset_id FROM assets WHERE asset_id=?', (asset_id,)).fetchone()
        if existing:
            return self.get_asset(asset_id)
        content_path = self.root / 'content' / item['content_sha256']
        package_path = self.root / 'packages' / asset_id
        with _directory(content_path.parent, create=True) as fd:
            _write_at(fd, content_path.name, raw)
        with _directory(package_path, create=True) as fd:
            _write_at(fd, 'manifest.json', manifest_raw)
            if signature_raw is not None:
                _write_at(fd, 'signature.json', signature_raw)
            with _parent(fd, item['manifest']['content_file'], create=True) as (dest_fd, name):
                with _directory(content_path.parent) as source_fd:
                    try:
                        os.link(content_path.name, name, src_dir_fd=source_fd,
                                dst_dir_fd=dest_fd, follow_symlinks=False)
                        os.fsync(dest_fd)
                    except FileExistsError:
                        if _read_at(dest_fd, name, MAX_CONTENT) != raw:
                            raise ValueError('immutable package content is corrupt')
        payload = dict(item, asset_id=asset_id, state='unverified', origin=origin,
                       package_path=str(package_path), content_path=str(content_path),
                       manifest_path=str(package_path / 'manifest.json'),
                       signature_path=str(package_path / 'signature.json') if signature_raw is not None else None,
                       manifest_raw_sha256=content_digest(manifest_raw),
                       signature_raw_sha256=content_digest(signature_raw) if signature_raw is not None else None)
        with self.evolution.connect() as db:
            db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?)',
                       (asset_id, canonical_json(payload), digest(payload), time.time()))
        return self.get_asset(asset_id)

    def get_asset(self, asset_id) -> dict:
        """Recheck retained bytes and DB hashes. Signature trust is still unverified."""
        if not valid_hash(asset_id):
            raise ValueError('asset_id must be a SHA256')
        with self.evolution.connect() as db:
            row = db.execute('SELECT * FROM assets WHERE asset_id=?', (asset_id,)).fetchone()
        if row is None:
            raise KeyError(asset_id)
        item = json.loads(row['payload_json'])
        if digest(item) != row['payload_hash'] or item['asset_id'] != asset_id:
            raise ValueError('asset database record is corrupt')
        expected_package = self.root / 'packages' / asset_id
        expected_content = self.root / 'content' / item['content_sha256']
        if (item['package_path'] != str(expected_package) or item['content_path'] != str(expected_content)
                or item['manifest_path'] != str(expected_package / 'manifest.json')
                or item['signature_path'] != (str(expected_package / 'signature.json') if item['signature'] is not None else None)):
            raise ValueError('retained asset paths are corrupt')
        current, manifest_raw, signature_raw, raw = _read_package(expected_package)
        if (digest({'manifest': current['manifest'], 'signature': current['signature']}) != asset_id
                or current['manifest'] != item['manifest'] or current['signature'] != item['signature']
                or content_digest(manifest_raw) != item['manifest_raw_sha256']
                or (content_digest(signature_raw) if signature_raw is not None else None) != item['signature_raw_sha256']
                or current['content_sha256'] != item['content_sha256']
                or _read_file(expected_content, MAX_CONTENT) != raw):
            raise ValueError('retained asset bytes changed')
        return item

    def list_assets(self) -> list[dict]:
        with self.evolution.connect() as db:
            identifiers = [row[0] for row in db.execute('SELECT asset_id FROM assets ORDER BY created_at,asset_id')]
        return [self.get_asset(identifier) for identifier in identifiers]

    def read_content(self, asset_id) -> bytes:
        item = self.get_asset(asset_id)
        raw = _read_file(item['content_path'], MAX_CONTENT)
        if content_digest(raw) != item['content_sha256']:
            raise ValueError('retained asset content changed')
        return raw
