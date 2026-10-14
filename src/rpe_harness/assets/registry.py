"""Read-only adapter for the historical Akashic index and local genes directory."""
from __future__ import annotations

import re
from pathlib import Path

from ..storage import canonical_json
from .manifest import (MAX_CONTENT, MAX_JSON, SCHEMA_VERSION, _json, _read_file,
                       content_digest, valid_hash, validate_manifest)


def _index_path(path):
    path = Path(path)
    return path / '.akashic_index.json' if path.is_dir() else path


def _entry(name, metadata):
    if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name)
            or not isinstance(metadata, dict)):
        raise ValueError('legacy registry names and entries must be bounded objects')
    sha = metadata.get('content_sha256', metadata.get('expected_sha256', metadata.get('cid')))
    if not valid_hash(sha):
        raise ValueError('registry entry must advertise a lowercase SHA256')
    for key in ('cid', 'expected_sha256', 'content_sha256', 'content_id'):
        if key in metadata and metadata[key] != sha:
            raise ValueError('registry advertised hashes do not agree')
    if metadata.get('registry_path') != 'genes/' + sha:
        raise ValueError('registry content path must be genes/<advertised SHA256>')
    return {'name': name, 'content_sha256': sha, 'state': 'discovered',
            'compatible': False, 'executable': False, 'provenance': {'external': metadata}}


def read_registry(path) -> list[dict]:
    """List inert names. Original trust/score/signature are external provenance only."""
    index = _json(_read_file(_index_path(path), MAX_JSON))
    if len(index) > 10000:
        raise ValueError('registry index has too many entries')
    return [_entry(name, metadata) for name, metadata in sorted(index.items())]


def import_registry_asset(store, registry_path, name) -> dict:
    path = _index_path(registry_path)
    rows = read_registry(path)
    row = next((item for item in rows if item['name'] == name), None)
    if row is None:
        raise KeyError(name)
    raw = _read_file(path.parent / 'genes' / row['content_sha256'], MAX_CONTENT)
    if not raw or content_digest(raw) != row['content_sha256']:
        raise ValueError('legacy registry content SHA256 changed')
    manifest = {'schema_version': SCHEMA_VERSION, 'name': 'akashic/' + name,
                'component': 'discovery', 'kind': 'legacy_gene', 'contract': None,
                'entrypoint': 'main', 'content_file': 'gene.py',
                'content_sha256': row['content_sha256'], 'parents': [], 'dependencies': [],
                'resources': {'network': False, 'subprocess': False},
                'provenance': row['provenance']}
    validate_manifest(manifest)
    manifest_raw = canonical_json(manifest).encode('utf-8')
    if len(manifest_raw) > MAX_JSON:
        raise ValueError('converted registry manifest exceeds JSON limit')
    item = {'manifest': manifest, 'signature': None, 'content_sha256': row['content_sha256'],
            'compatible': False, 'executable': False}
    return store._retain(item, manifest_raw, None, raw, str(path.absolute()) + '#' + name)
