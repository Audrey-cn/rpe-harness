"""Asset provenance feeds the existing acceptance and activation authority."""
from __future__ import annotations

import os
from pathlib import Path

from ..evolution.store import EvolutionStore, digest
from ..storage import canonical_json
from .identity import sign_envelope
from .manifest import MAX_JSON, _directory, _json, _read_file, validate_manifest
from .store import AssetStore, _write_at
from .trust import verify_envelope


def _policy_path(store, keyring):
    if isinstance(keyring, dict):
        raw = canonical_json(keyring).encode()
        if len(raw) > MAX_JSON:
            raise ValueError('source trust policy exceeds 1 MiB')
        path, _ = store.evolution.write_artifact(raw, '.trust.json')
        return Path(path)
    return Path(os.path.abspath(Path(keyring).expanduser()))


def _verify_source(asset, policy):
    signature = asset['signature']
    if not signature or signature.get('document') != asset['manifest']:
        raise ValueError('source signature does not bind the exact manifest')
    verdict = verify_envelope(signature, policy, scope='capability-package', role='creator')
    if verdict.get('passed') is not True:
        raise ValueError('source trust failed: ' + verdict.get('reason', 'untrusted'))
    return verdict


def propose_asset(asset_id, db_path, *, keyring, evidence_run_ids, owned_root=None):
    """Create a proposal, never an activation. Fresh local run evidence is required."""
    from ..evolution.candidates import propose_candidate
    from ..evolution.cycle import record_verified_feedback
    store = AssetStore(db_path, owned_root)
    asset = store.get_asset(asset_id)
    if asset['compatible'] is not True:
        raise ValueError('asset ABI is incompatible; legacy genes remain discovery only')
    if (not isinstance(evidence_run_ids, list) or not evidence_run_ids
            or any(not isinstance(item, str) or not item for item in evidence_run_ids)):
        raise ValueError('asset adoption requires nonempty verified run evidence')
    for run_id in evidence_run_ids:
        feedback = record_verified_feedback(run_id, store.path)
        if feedback is None or feedback.get('plan_sha256') is None:
            raise ValueError('asset adoption requires durable plan-bound verified evidence')
    path = _policy_path(store, keyring)
    raw = _read_file(path, MAX_JSON)
    verdict = _verify_source(asset, _json(raw))
    content = store.read_content(asset_id)
    provenance = {'schema_version': 'rpe.asset-source/v1', 'asset_id': asset_id,
        'manifest_sha256': digest(asset['manifest']), 'signature_sha256': digest(asset['signature']),
        'content_sha256': asset['content_sha256'], 'trust_policy_path': str(path),
        'trust_policy_sha256': digest(raw), 'owner': verdict['owner'], 'key_id': verdict['key_id']}
    proposal = propose_candidate(store.path, store.owned_root,
        component=asset['manifest']['component'], kind=asset['manifest']['kind'],
        content=content.decode('utf-8'), evidence_run_ids=evidence_run_ids, provenance=provenance)
    return proposal


def verify_proposal_source(proposal, db_path):
    """Recheck source on validation, activation and every production candidate use."""
    provenance = proposal.get('provenance')
    if provenance is None:
        return
    fields = {'schema_version', 'asset_id', 'manifest_sha256', 'signature_sha256',
              'content_sha256', 'trust_policy_path', 'trust_policy_sha256', 'owner', 'key_id'}
    if (not isinstance(provenance, dict) or set(provenance) != fields
            or provenance['schema_version'] != 'rpe.asset-source/v1'):
        raise ValueError('candidate source provenance is invalid')
    store = (AssetStore.from_evolution(db_path) if isinstance(db_path, EvolutionStore)
             else AssetStore(db_path))
    asset = store.get_asset(provenance['asset_id'])
    raw = _read_file(provenance['trust_policy_path'], MAX_JSON)
    verdict = _verify_source(asset, _json(raw))
    if (digest(raw) != provenance['trust_policy_sha256']
            or digest(asset['manifest']) != provenance['manifest_sha256']
            or digest(asset['signature']) != provenance['signature_sha256']
            or asset['content_sha256'] != provenance['content_sha256']
            or verdict['owner'] != provenance['owner'] or verdict['key_id'] != provenance['key_id']
            or not asset['compatible']
            or proposal['kind'] != asset['manifest']['kind']
            or proposal['component'] != asset['manifest']['component']):
        raise ValueError('candidate source or trust policy changed; submit a fresh proposal')
    # Declarative recipes are normalized by the existing proposer; code retains exact bytes.
    content = store.read_content(asset['asset_id'])
    expected = digest(_json(content)) if proposal['kind'] == 'recipe' else digest(content)
    if expected != proposal['content_hash']:
        raise ValueError('candidate code is not bound to the source asset')


def asset_status(asset_id, db_path, *, keyring=None):
    """Lifecycle is derived from EvolutionStore; assets have no second active pointer."""
    store = AssetStore(db_path)
    asset = store.get_asset(asset_id)
    proposals, versions = [], []
    with store.evolution.connect() as db:
        rows = db.execute('SELECT * FROM evolution_proposals ORDER BY created_at').fetchall()
    for row in rows:
        item = store.evolution._proposal(row)
        if (item.get('provenance') or {}).get('asset_id') == asset_id:
            proposals.append(item)
            if item.get('version_id'):
                versions.append(store.evolution.get_version(item['version_id']))
    manifest = asset['manifest']
    pointer = store.evolution.pointer(manifest['component'], manifest['kind'])[0]
    active = next((v for v in versions if v['version_id'] == pointer), None)
    latest = versions[-1] if versions else (proposals[-1] if proposals else None)
    asset.update(local_validation='active' if active else latest['state'] if latest else 'unverified',
                 active_version_id=active['version_id'] if active else None,
                 proposal_ids=[p['proposal_id'] for p in proposals])
    if keyring is not None:
        try:
            raw = _read_file(_policy_path(store, keyring), MAX_JSON)
            asset['source_verification'] = _verify_source(asset, _json(raw))
        except (ValueError, OSError, TypeError) as error:
            asset['source_verification'] = {'passed': False, 'reason': str(error)}
    return asset


def export_version(version_id, db_path, destination, *, identity=None):
    """Write a new package with code and public digests, excluding private run context."""
    from ..contracts import CAPABILITY, CONTRACT_VERSION
    from ..evolution.candidates import _certificate
    store = EvolutionStore.read_only(db_path)
    version = store.get_version(version_id)
    if version['state'] not in {'validated', 'active', 'dormant', 'rolled_back'}:
        raise ValueError('export requires a locally validated non-quarantined version')
    content = _certificate(store, version, fresh_evidence=True)
    if version['kind'] not in {'csv_executor', 'recipe'}:
        raise ValueError('export ABI is not registered')
    suffix = 'candidate.py' if version['kind'] == 'csv_executor' else 'recipe.json'
    source = version.get('provenance') or {}
    manifest = {'schema_version': 'rpe.capability-package/v1',
        'name': 'rpe/' + version['kind'] + '-' + version['content_hash'][:16],
        'component': version['component'], 'kind': version['kind'],
        'contract': {'capability': CAPABILITY, 'version': CONTRACT_VERSION, 'input_format': 'csv'},
        'entrypoint': 'prepare' if version['kind'] == 'csv_executor' else None,
        'content_file': suffix, 'content_sha256': digest(content),
        'parents': [source['content_sha256']] if source.get('content_sha256') else [],
        'dependencies': [], 'resources': {'network': False, 'subprocess': False},
        'provenance': {'producer': 'rpe-harness', 'certificate_sha256': version['validation_hash'],
                       'acceptance_suite_sha256': version['validation']['revisions']['fixture_suite_sha256']}}
    validate_manifest(manifest)
    signature = sign_envelope(manifest, identity, scope='capability-package') if identity else None
    target = Path(os.path.abspath(Path(destination).expanduser()))
    # Create and write relative to the same descriptor; an existing directory is never reused.
    with _directory(target.parent) as parent_fd:
        os.mkdir(target.name, 0o700, dir_fd=parent_fd)
        directory_fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=parent_fd)
        try:
            _write_at(directory_fd, suffix, content)
            _write_at(directory_fd, 'manifest.json', canonical_json(manifest).encode())
            if signature is not None:
                _write_at(directory_fd, 'signature.json', canonical_json(signature).encode())
            os.fsync(directory_fd)
            os.fsync(parent_fd)
        finally:
            os.close(directory_fd)
    return {'exported': True, 'package_path': str(target), 'content_sha256': digest(content),
            'signed': signature is not None, 'locally_validated': True}
