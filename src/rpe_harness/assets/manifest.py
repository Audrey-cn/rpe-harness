"""Strict inert capability packages; all source reads bind directory descriptors.

Pass canonical absolute paths on systems with aliases such as macOS /var. Package
sources reject symlinks in every path component and are never imported as code.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from ..contracts import CAPABILITY, CONTRACT_VERSION

SCHEMA_VERSION = 'rpe.capability-package/v1'
MAX_CONTENT = 65536
MAX_JSON = 1048576
_FIELDS = {'schema_version', 'name', 'component', 'kind', 'contract', 'entrypoint',
           'content_file', 'content_sha256', 'parents', 'dependencies', 'resources', 'provenance'}
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*[/:][A-Za-z0-9][A-Za-z0-9_./-]*\Z')


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_hash(value) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _relative(value: str) -> tuple[str, ...]:
    if (not isinstance(value, str) or not value or len(value) > 256 or '\\' in value
            or '\x00' in value or value.startswith('/')):
        raise ValueError('content path must be a bounded relative path')
    parts = tuple(value.split('/'))
    if any(part in {'', '.', '..'} for part in parts):
        raise ValueError('content path must not traverse directories')
    return parts


@contextmanager
def _directory(path, *, create=False):
    """Open each absolute directory component without following symlinks."""
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError as error:
        raise ValueError('asset directory is missing, special, or contains a symlink') from error
    finally:
        os.close(fd)


@contextmanager
def _parent(directory_fd, relative, *, create=False):
    parts = _relative(relative)
    fd = os.dup(directory_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _read_at(directory_fd, relative, limit, *, optional=False):
    try:
        with _parent(directory_fd, relative) as (parent_fd, name):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent_fd)
            except FileNotFoundError:
                if optional:
                    return None
                raise
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                    raise ValueError('asset file must be regular and within its size limit')
                chunks = []
                remaining = limit + 1
                while remaining:
                    chunk = os.read(fd, min(remaining, 65536))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b''.join(chunks)
                if len(raw) > limit:
                    raise ValueError('asset file exceeds its size limit')
                return raw
            finally:
                os.close(fd)
    except OSError as error:
        raise ValueError('asset file is missing, special, or contains a symlink') from error


def _read_file(path, limit):
    path = Path(path)
    with _directory(path.parent) as fd:
        return _read_at(fd, path.name, limit)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON keys are not allowed')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('asset JSON must be valid UTF-8 JSON') from error
    if not isinstance(value, dict):
        raise ValueError('asset JSON must be an object')
    return value


def validate_manifest(manifest) -> bool:
    if not isinstance(manifest, dict) or set(manifest) != _FIELDS:
        raise ValueError('manifest fields do not match capability-package/v1')
    if manifest['schema_version'] != SCHEMA_VERSION:
        raise ValueError('unsupported capability package schema')
    name = manifest['name']
    if not isinstance(name, str) or len(name) > 160 or not _NAME.fullmatch(name):
        raise ValueError('asset name must have a bounded namespace')
    parts = _relative(manifest['content_file'])
    if parts[0] in {'manifest.json', 'signature.json'}:
        raise ValueError('content path collides with package metadata')
    if not valid_hash(manifest['content_sha256']):
        raise ValueError('content_sha256 must be a lowercase SHA256')
    parents = manifest['parents']
    if (not isinstance(parents, list) or len(parents) > 128
            or any(not valid_hash(parent) for parent in parents)):
        raise ValueError('parents must be bounded content SHA256 values')
    if manifest['dependencies'] != [] or not isinstance(manifest['dependencies'], list):
        raise ValueError('dependencies are not supported')
    resources = manifest['resources']
    if (not isinstance(resources, dict) or set(resources) != {'network', 'subprocess'}
            or any(value is not False for value in resources.values())):
        raise ValueError('network and subprocess declarations must be false')
    if not isinstance(manifest['provenance'], dict):
        raise ValueError('provenance must be an object')
    kind = manifest['kind']
    if not isinstance(kind, str):
        raise ValueError('asset kind must be a string')
    if kind == 'legacy_gene':
        if (manifest['component'] != 'discovery' or manifest['contract'] is not None
                or (manifest['entrypoint'] is not None and manifest['entrypoint'] != 'main')):
            raise ValueError('legacy genes are discovery only with main or no entrypoint')
        return False
    if kind not in {'csv_executor', 'recipe'}:
        raise ValueError('unsupported asset kind')
    component, entrypoint = ('executor', 'prepare') if kind == 'csv_executor' else ('recipe', None)
    if manifest['component'] != component or manifest['entrypoint'] != entrypoint:
        raise ValueError('asset ABI is incompatible')
    contract = manifest['contract']
    if (not isinstance(contract, dict) or set(contract) != {'capability', 'version', 'input_format'}
            or contract['capability'] != CAPABILITY or type(contract['version']) is not int
            or contract['version'] != CONTRACT_VERSION or contract['input_format'] != 'csv'):
        raise ValueError('asset must use the current tabular.deduplicate CSV contract')
    return True


def _read_package(path):
    with _directory(path) as fd:
        manifest_raw = _read_at(fd, 'manifest.json', MAX_JSON)
        manifest = _json(manifest_raw)
        compatible = validate_manifest(manifest)
        raw = _read_at(fd, manifest['content_file'], MAX_CONTENT)
        if not raw or content_digest(raw) != manifest['content_sha256']:
            raise ValueError('content bytes do not match manifest SHA256')
        signature_raw = _read_at(fd, 'signature.json', MAX_JSON, optional=True)
        signature = _json(signature_raw) if signature_raw is not None else None
    return ({'manifest': manifest, 'signature': signature,
             'content_sha256': content_digest(raw), 'compatible': compatible, 'executable': False},
            manifest_raw, signature_raw, raw)


def inspect_package(path) -> dict:
    """Check package syntax and exact bytes; this does not verify source trust."""
    return _read_package(path)[0]
