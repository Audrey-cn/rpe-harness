"""Bounded local release bundles; trusted signatures never authorize activation."""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import stat
import sys
import uuid

from ..evolution.candidates import current_revisions
from ..evolution.store import EvolutionStore, digest
from .identity import canonical_json, sign_envelope
from .lifecycle import export_version
from .manifest import (MAX_CONTENT, MAX_JSON, _directory, _json, _read_at, _read_file,
                       validate_manifest, valid_hash)
from .store import _write_at
from .trust import verify_envelope


MAX_ENTRIES = 100
_CATALOG_FIELDS = {'schema_version', 'producer', 'audit_file', 'audit_sha256', 'entries'}
_ENTRY_FIELDS = {'name', 'asset_id', 'package_path', 'manifest_sha256', 'signature_sha256',
                 'content_sha256', 'creator_key_id', 'source_version_id', 'certificate_sha256'}
_AUDIT_FIELDS = {'schema_version', 'producer_version', 'revisions', 'keyring_sha256', 'entries'}
_AUDIT_ENTRY_FIELDS = {'asset_id', 'source_version_id', 'certificate_sha256', 'creator_key_id',
                       'source_certificate', 'evidence_binding', 'package_integrity',
                       'creator_signature'}


def _object(value, fields, label):
    if type(value) is not dict or set(value) != fields:
        raise ValueError(label + ' fields are invalid')


def _document(raw):
    value = _json(raw)
    canonical_json(value)  # Complexity, finite values and string budgets, in addition to bytes.
    return value


def _local_document(source):
    if type(source) is dict:
        raw = canonical_json(source)
    elif isinstance(source, (str, os.PathLike)):
        raw = _read_file(Path(os.path.abspath(Path(source).expanduser())), MAX_JSON)
    else:
        raise ValueError('explicit local JSON source is required')
    return _document(raw), digest(raw)


def _origin(envelope, policy, scope, role):
    verdict = verify_envelope(envelope, policy, scope=scope, role=role)
    if verdict['passed'] is not True:
        raise ValueError(role + ' signature failed: ' + verdict['reason'])
    return verdict


def _package_at(fd, prefix):
    manifest_raw = _read_at(fd, prefix + '/manifest.json', MAX_JSON)
    signature_raw = _read_at(fd, prefix + '/signature.json', MAX_JSON)
    manifest, signature = _document(manifest_raw), _document(signature_raw)
    if validate_manifest(manifest) is not True:
        raise ValueError('catalog accepts only registered CSV executor/recipe packages')
    content = _read_at(fd, prefix + '/' + manifest['content_file'], MAX_CONTENT)
    if not content or digest(content) != manifest['content_sha256']:
        raise ValueError('package content hash mismatch')
    if canonical_json(signature.get('document')) != canonical_json(manifest):
        raise ValueError('creator signature does not bind exact manifest')
    asset_id = digest({'manifest': manifest, 'signature': signature})
    return manifest, signature, content, manifest_raw, signature_raw, asset_id


def _exact_tree(fd, expected_files):
    """Inspect only bounded expected trees through directory descriptors, never symlinks."""
    expected = {}
    for relative in expected_files:
        parts = relative.split('/')
        head = parts[0]
        if len(parts) == 1:
            if head in expected:
                raise ValueError('bundle file/directory collision')
            expected[head] = None
        else:
            if head in expected and expected[head] is None:
                raise ValueError('bundle file/directory collision')
            expected.setdefault(head, set()).add('/'.join(parts[1:]))
    seen = set()
    with os.scandir(fd) as children:
        for child in children:
            name = child.name
            if name not in expected or name in seen:
                raise ValueError('bundle contains extra files or directories')
            seen.add(name)
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            descendants = expected[name]
            if descendants is None:
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError('bundle file is special or a symlink')
            else:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError('bundle directory is special or a symlink')
                nested = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    _exact_tree(nested, descendants)
                finally:
                    os.close(nested)
    if seen != set(expected):
        raise ValueError('bundle files or directories are missing')


def _catalog_schema(document):
    from .. import __version__
    _object(document, _CATALOG_FIELDS, 'catalog')
    if document['schema_version'] != 'rpe.registry-catalog/v1':
        raise ValueError('unsupported catalog schema')
    producer = document['producer']
    _object(producer, {'name', 'version', 'revisions'}, 'producer')
    if (producer['name'] != 'rpe-harness' or producer['version'] != __version__
            or canonical_json(producer['revisions']) != canonical_json(current_revisions())):
        raise ValueError('catalog producer or validation revisions are stale')
    if document['audit_file'] != 'audit.json' or not valid_hash(document['audit_sha256']):
        raise ValueError('catalog audit reference is invalid')
    entries = document['entries']
    if type(entries) is not list or not 0 < len(entries) <= MAX_ENTRIES:
        raise ValueError('catalog requires 1..100 entries')
    names, identifiers = [], set()
    for entry in entries:
        _object(entry, _ENTRY_FIELDS, 'catalog entry')
        if type(entry['name']) is not str or not 0 < len(entry['name']) <= 160:
            raise ValueError('catalog name is invalid')
        for field in _ENTRY_FIELDS - {'name', 'package_path'}:
            if not valid_hash(entry[field]):
                raise ValueError('catalog ' + field + ' must be a SHA256')
        if entry['package_path'] != 'packages/' + entry['asset_id']:
            raise ValueError('catalog package path is invalid')
        if entry['asset_id'] in identifiers:
            raise ValueError('catalog asset ids must be unique')
        identifiers.add(entry['asset_id'])
        names.append(entry['name'])
    if names != sorted(set(names)):
        raise ValueError('catalog names must be unique and sorted')


def _audit_schema(audit, document):
    _object(audit, _AUDIT_FIELDS, 'audit')
    if (audit['schema_version'] != 'rpe.registry-export-audit/v1'
            or audit['producer_version'] != document['producer']['version']
            or canonical_json(audit['revisions']) != canonical_json(document['producer']['revisions'])
            or not valid_hash(audit['keyring_sha256'])
            or type(audit['entries']) is not list
            or len(audit['entries']) != len(document['entries'])):
        raise ValueError('export audit is invalid or inconsistent')
    for row, entry in zip(audit['entries'], document['entries']):
        _object(row, _AUDIT_ENTRY_FIELDS, 'audit entry')
        bindings = {field: entry[field] for field in (
            'asset_id', 'source_version_id', 'certificate_sha256', 'creator_key_id')}
        bindings.update(source_certificate='verified_current', evidence_binding='verified',
                        package_integrity='passed', creator_signature='passed')
        if row != bindings:
            raise ValueError('export audit does not bind catalog entries')


class _AcceptanceScope:
    """Only the owned-path interface needed by the existing manual fixture replay."""
    def __init__(self, root):
        self.owned_root = root

    def owned_path(self, path):
        # Keep the production owned-path checks; lexical containment alone follows symlinks.
        return EvolutionStore.owned_path(self, path)


def _acceptance(packages, owned_root):
    from ..evolution.candidates import _FIXTURES, _recipe_valid, _replay_acceptance
    if owned_root is None:
        raise ValueError('acceptance requires an explicit owned_root')
    parent = Path(os.path.abspath(Path(owned_root).expanduser()))
    with _directory(parent) as fd:
        stage = 'registry-acceptance-' + uuid.uuid4().hex
        os.mkdir(stage, 0o700, dir_fd=fd)
        root = parent / stage
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        stage_inode = os.fstat(stage_fd).st_ino
        os.close(stage_fd)
        try:
            result = []
            for manifest, content in packages:
                if manifest['kind'] == 'recipe':
                    _recipe_valid(_document(content))
                checks = _replay_acceptance(_AcceptanceScope(root), content, manifest['kind'])
                passed = (len(checks) == len(_FIXTURES) and all(
                    check.get('passed') is True and check.get('source_preserved') is True
                    and check.get('output_preserved') is True for check in checks))
                result.append({'passed': passed, 'checks': checks})
                if not passed:
                    raise ValueError('independent acceptance failed: ' + str(checks[-1].get('error')))
            return result
        finally:
            _remove_owned_stage(fd, stage, stage_inode)


def validate_release_bundle(bundle, *, keyring, mode='integrity', owned_root=None):
    """Offline checks; producer evidence is an attestation until independent replay passes."""
    report = {'passed': False, 'mode': mode, 'published': False, 'activated': False,
              'producer_validation': 'attested', 'independent_acceptance': 'not_run',
              'entries': []}
    try:
        if mode not in {'integrity', 'acceptance'}:
            raise ValueError('mode must be integrity or acceptance')
        policy, policy_sha = _local_document(keyring)
        path = Path(os.path.abspath(Path(bundle).expanduser()))
        with _directory(path) as fd:
            raw_catalog = _read_at(fd, 'catalog.json', MAX_JSON)
            envelope = _document(raw_catalog)
            origin = _origin(envelope, policy, 'registry-index', 'registry')
            document = envelope['document']
            _catalog_schema(document)
            raw_audit = _read_at(fd, 'audit.json', MAX_JSON)
            if digest(raw_audit) != document['audit_sha256']:
                raise ValueError('export audit bytes changed')
            _audit_schema(_document(raw_audit), document)
            expected_files = {'catalog.json', 'audit.json'}
            packages = []
            for entry in document['entries']:
                manifest, signature, content, manifest_raw, signature_raw, asset_id = _package_at(
                    fd, entry['package_path'])
                creator = _origin(signature, policy, 'capability-package', 'creator')
                if (asset_id != entry['asset_id'] or manifest['name'] != entry['name']
                        or digest(manifest_raw) != entry['manifest_sha256']
                        or digest(signature_raw) != entry['signature_sha256']
                        or manifest['content_sha256'] != entry['content_sha256']
                        or creator['key_id'] != entry['creator_key_id']
                        or manifest['provenance'].get('producer') != 'rpe-harness'
                        or manifest['provenance'].get('certificate_sha256') != entry['certificate_sha256']
                        or manifest['provenance'].get('acceptance_suite_sha256') !=
                           document['producer']['revisions']['fixture_suite_sha256']):
                    raise ValueError('catalog does not bind exact package and certificate claims')
                expected_files.update(entry['package_path'] + '/' + relative for relative in (
                    'manifest.json', 'signature.json', manifest['content_file']))
                packages.append((manifest, content))
                report['entries'].append({'asset_id': asset_id, 'name': manifest['name'],
                                          'creator': creator, 'integrity': 'passed'})
            _exact_tree(fd, expected_files)
            report.update(catalog_sha256=digest(raw_catalog), keyring_sha256=policy_sha,
                          registry=origin, revisions=current_revisions())
        if mode == 'acceptance':
            report['independent_acceptance'] = 'failed'
            acceptance = _acceptance(packages, owned_root)
            for row, result in zip(report['entries'], acceptance):
                row['acceptance'] = result
            report['independent_acceptance'] = 'passed'
        report['passed'] = True
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, RecursionError) as error:
        report['error'] = str(error)
    return report


def _remove_owned_stage(parent_fd, name, expected_inode):
    """Remove our unshared random stage, never a replacement at its former name."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        if os.fstat(fd).st_ino != expected_inode:
            raise ValueError('owned stage was replaced; refusing cleanup')
        with os.scandir(fd) as children:
            for child in children:
                metadata = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    _remove_owned_stage(fd, child.name, metadata.st_ino)
                else:
                    os.unlink(child.name, dir_fd=fd)
    finally:
        os.close(fd)
    # Check ownership again before removing the directory entry.
    if os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_ino != expected_inode:
        raise ValueError('owned stage was replaced; refusing cleanup')
    os.rmdir(name, dir_fd=parent_fd)


def _bound_directory(fd, name, source_fd):
    expected = os.fstat(source_fd)
    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if (not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)):
        raise ValueError('stage directory identity changed; refusing unvalidated replacement')


def _publish_exclusive(fd, source, destination, *, source_fd=None):
    # Parent directories are trusted. These checks bind observed identities around rename;
    # they cannot make pathname rename immune to an arbitrary same-UID concurrent attacker.
    if source_fd is not None:
        _bound_directory(fd, source, source_fd)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename = getattr(library, 'renameatx_np', None)
        flag = 4  # Darwin RENAME_EXCL.
    elif sys.platform.startswith('linux'):
        rename = getattr(library, 'renameat2', None)
        flag = 1  # Linux RENAME_NOREPLACE.
    else:
        raise ValueError('exclusive directory publication is unavailable')
    if rename is None:
        raise ValueError('exclusive directory publication is unavailable')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(fd, os.fsencode(source), fd, os.fsencode(destination), flag) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError('release destination already exists')
        raise OSError(code, 'exclusive release directory publication failed')
    if source_fd is not None:
        _bound_directory(fd, destination, source_fd)
    os.fsync(fd)


def build_release_bundle(version_ids, db_path, destination, *, creator_identity,
                         registry_identity, keyring):
    """Export freshly certified versions to a new local, separately signed bundle."""
    from .. import __version__
    if (type(version_ids) is not list or not 0 < len(version_ids) <= MAX_ENTRIES
            or any(not valid_hash(item) for item in version_ids)
            or len(set(version_ids)) != len(version_ids)):
        raise ValueError('build requires 1..100 unique verified version ids')
    policy, policy_sha = _local_document(keyring)
    creator_identity = _local_document(creator_identity)[0]
    registry_identity = _local_document(registry_identity)[0]
    revisions = current_revisions()
    # Verify supplied identities' authorized uses before constructing any output.
    _origin(sign_envelope({}, creator_identity, scope='capability-package'), policy,
            'capability-package', 'creator')
    _origin(sign_envelope({}, registry_identity, scope='registry-index'), policy,
            'registry-index', 'registry')
    db = Path(os.path.abspath(Path(db_path).expanduser()))
    # Validate without reading an entire database or accidentally creating a missing one.
    with _directory(db.parent) as db_parent:
        descriptor = os.open(db.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=db_parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError('explicit database must be a regular file')
        finally:
            os.close(descriptor)
    store = EvolutionStore.read_only(db)
    versions = [store.get_version(item) for item in version_ids]
    target = Path(os.path.abspath(Path(destination).expanduser()))
    with _directory(target.parent) as parent_fd:
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError('release destination already exists')
        stage = '.rpe-registry-stage-' + uuid.uuid4().hex
        os.mkdir(stage, 0o700, dir_fd=parent_fd)
        stage_path = target.parent / stage
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        stage_inode = os.fstat(stage_fd).st_ino
        published = False
        try:
            os.mkdir('packages', 0o700, dir_fd=stage_fd)
            packages_fd = os.open('packages', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=stage_fd)
            try:
                entries = []
                for number, version in enumerate(versions):
                    temporary = 'export-' + str(number)
                    export_version(version['version_id'], db, stage_path / 'packages' / temporary,
                                   identity=creator_identity)
                    manifest, signature, _, manifest_raw, signature_raw, asset_id = _package_at(
                        stage_fd, 'packages/' + temporary)
                    verdict = _origin(signature, policy, 'capability-package', 'creator')
                    _publish_exclusive(packages_fd, temporary, asset_id)
                    entries.append({'name': manifest['name'], 'asset_id': asset_id,
                        'package_path': 'packages/' + asset_id,
                        'manifest_sha256': digest(manifest_raw),
                        'signature_sha256': digest(signature_raw),
                        'content_sha256': manifest['content_sha256'],
                        'creator_key_id': verdict['key_id'], 'source_version_id': version['version_id'],
                        'certificate_sha256': version['validation_hash']})
            finally:
                os.close(packages_fd)
            entries.sort(key=lambda item: item['name'])
            audit = {'schema_version': 'rpe.registry-export-audit/v1',
                     'producer_version': __version__, 'revisions': revisions,
                     'keyring_sha256': policy_sha, 'entries': [
                         {**{field: entry[field] for field in (
                             'asset_id', 'source_version_id', 'certificate_sha256', 'creator_key_id')},
                          'source_certificate': 'verified_current', 'evidence_binding': 'verified',
                          'package_integrity': 'passed', 'creator_signature': 'passed'}
                         for entry in entries]}
            audit_raw = canonical_json(audit)
            _write_at(stage_fd, 'audit.json', audit_raw)
            document = {'schema_version': 'rpe.registry-catalog/v1',
                        'producer': {'name': 'rpe-harness', 'version': __version__,
                                     'revisions': revisions},
                        'audit_file': 'audit.json', 'audit_sha256': digest(audit_raw), 'entries': entries}
            _catalog_schema(document)
            envelope = sign_envelope(document, registry_identity, scope='registry-index')
            _write_at(stage_fd, 'catalog.json', canonical_json(envelope))
            result = validate_release_bundle(stage_path, keyring=keyring)
            if result['passed'] is not True:
                raise ValueError('built release failed integrity: ' + result.get('error', 'unknown'))
            os.fsync(stage_fd)
            _publish_exclusive(parent_fd, stage, target.name, source_fd=stage_fd)
            published = True
            return {'built': True, 'bundle_path': str(target), 'entries': entries,
                    'published': False, 'activated': False, 'integrity': result}
        finally:
            os.close(stage_fd)
            if not published:
                _remove_owned_stage(parent_fd, stage, stage_inode)
