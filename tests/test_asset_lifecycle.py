import csv
import importlib
import json
from pathlib import Path

import pytest

from rpe_harness.contracts import CONTRACT_VERSION
from rpe_harness.evolution import EvolutionStore, apply_candidate, rollback, validate_candidate
from rpe_harness.evolution.store import digest
from rpe_harness.runtime import HarnessRuntime
from test_candidates import GOOD_CODE
from test_evolution import verified_run
from test_asset_trust import signing_key, trusted_identity


def lifecycle():
    try:
        return importlib.import_module('rpe_harness.assets.lifecycle')
    except ImportError:
        pytest.fail('asset adoption/export lifecycle is unavailable')


def package(root, identity=None):
    root.mkdir()
    raw = GOOD_CODE.encode()
    manifest = {'schema_version': 'rpe.capability-package/v1', 'name': 'test/csv-dedup',
        'component': 'executor', 'kind': 'csv_executor',
        'contract': {'capability': 'tabular.deduplicate', 'version': CONTRACT_VERSION,
                     'input_format': 'csv'},
        'entrypoint': 'prepare', 'content_file': 'candidate.py',
        'content_sha256': digest(raw), 'parents': [], 'dependencies': [],
        'resources': {'network': False, 'subprocess': False},
        'provenance': {'repository': 'https://example.invalid/capabilities', 'revision': 'test'}}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'candidate.py').write_bytes(raw)
    if identity:
        from rpe_harness.assets.identity import sign_envelope
        (root / 'signature.json').write_text(json.dumps(
            sign_envelope(manifest, identity, scope='capability-package')))
    return root


def policy(identity):
    return {'schema_version': 'rpe.trust-policy/v1', 'trusted_keys': [{
        'key_id': identity['public_key_id'], 'owner': 'test-creator', 'roles': ['creator'],
        'scopes': ['capability-package'], 'public_key': identity['public_key'], 'revoked': False}]}


def test_unsigned_package_cannot_propose_or_activate(tmp_path):
    module = lifecycle()
    from rpe_harness.assets.store import AssetStore
    db, run = verified_run(tmp_path)
    asset = AssetStore(db, tmp_path).import_package(package(tmp_path / 'unsigned'))
    with pytest.raises(ValueError, match='source|signed|trust|signature'):
        module.propose_asset(asset['asset_id'], db,
            keyring={'schema_version': 'rpe.trust-policy/v1', 'trusted_keys': []},
            evidence_run_ids=[run['run_id']])
    assert EvolutionStore(db).list_proposals() == []
    assert EvolutionStore(db).get_active('executor', 'csv_executor') is None


def signed_asset(root, identity):
    from rpe_harness.assets.store import AssetStore
    db, run = verified_run(root)
    asset = AssetStore(db, root).import_package(package(root / 'signed', identity))
    keyring = root / 'trust.json'
    keyring.write_text(json.dumps(policy(identity)))
    return db, run, asset, keyring


def next_task(root, db, output='next.csv'):
    return HarnessRuntime(db_path=db).run({'schema_version': 2, 'event_id': output,
        'capability': 'tabular.deduplicate', 'goal': '按订单号保留首条',
        'constraints': ['preserve_original', 'preserve_order'],
        'execution': {'input_path': str(root / 'orders.csv'),
                      'output_path': str(root / output), 'key_columns': ['订单号']}})


def test_signed_asset_uses_one_activation_authority_and_exports_public_package(tmp_path, trusted_identity):
    module = lifecycle()
    db, run, asset, keyring = signed_asset(tmp_path, trusted_identity)
    proposal = module.propose_asset(asset['asset_id'], db, keyring=keyring,
        evidence_run_ids=[run['run_id']])
    assert proposal['provenance']['asset_id'] == asset['asset_id']
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    active = apply_candidate(proposal['proposal_id'], db)
    result = next_task(tmp_path, db)
    assert result['status'] == 'succeeded'
    actual = [s['evidence']['actual_executor'] for s in result['steps']
              if 'actual_executor' in s['evidence']][0]
    assert actual['candidate_version_id'] == active['version_id']
    assert actual['code_sha256'] == asset['content_sha256']
    with (tmp_path / 'next.csv').open(encoding='utf-8-sig', newline='') as handle:
        assert list(csv.reader(handle)) == [['订单号', '金额'], ['001', '10'], ['002', '20']]
    from rpe_harness.assets.store import AssetStore
    store = AssetStore(db)
    shown = module.asset_status(asset['asset_id'], db)
    assert shown['local_validation'] == 'active'
    assert shown['active_version_id'] == active['version_id']
    exported = module.export_version(active['version_id'], db, tmp_path / 'export',
                                     identity=trusted_identity)
    exported_text = (tmp_path / 'export' / 'manifest.json').read_text()
    assert str(tmp_path) not in exported_text and run['run_id'] not in exported_text
    assert '订单号' not in exported_text and 'goal' not in exported_text
    reimported = store.import_package(tmp_path / 'export')
    assert reimported['content_sha256'] == asset['content_sha256']
    assert reimported['state'] == 'unverified'
    assert exported['content_sha256'] == asset['content_sha256']
    assert rollback(active['version_id'], db)['rolled_back'] is True
    assert module.asset_status(asset['asset_id'], db)['local_validation'] == 'rolled_back'
    assert EvolutionStore(db).get_active('executor', 'csv_executor') is None


def test_revoked_source_fails_before_activation_and_during_production(tmp_path, trusted_identity):
    module = lifecycle()
    db, run, asset, keyring = signed_asset(tmp_path, trusted_identity)
    proposal = module.propose_asset(asset['asset_id'], db, keyring=keyring,
        evidence_run_ids=[run['run_id']])
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    revoked = policy(trusted_identity)
    revoked['trusted_keys'][0]['revoked'] = True
    keyring.write_text(json.dumps(revoked))
    with pytest.raises(ValueError, match='source|trust|revoked'):
        apply_candidate(proposal['proposal_id'], db)
    assert EvolutionStore(db).get_active('executor', 'csv_executor') is None
    keyring.write_text(json.dumps(policy(trusted_identity)))
    active = apply_candidate(proposal['proposal_id'], db)
    keyring.write_text(json.dumps(revoked))
    result = next_task(tmp_path, db)
    assert result['status'] == 'succeeded'
    assert EvolutionStore(db).get_version(active['version_id'])['state'] == 'quarantine'
    assert EvolutionStore(db).get_active('executor', 'csv_executor') is None


def test_package_tampering_and_missing_run_evidence_never_create_trusted_candidate(tmp_path, trusted_identity):
    module = lifecycle()
    db, run, asset, keyring = signed_asset(tmp_path, trusted_identity)
    with pytest.raises(ValueError, match='evidence|verified'):
        module.propose_asset(asset['asset_id'], db, keyring=keyring, evidence_run_ids=[])
    path = Path(asset['manifest_path'])
    path.chmod(0o600)
    value = json.loads(path.read_text())
    value['provenance']['revision'] = 'changed'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        module.propose_asset(asset['asset_id'], db, keyring=keyring,
                             evidence_run_ids=[run['run_id']])
    assert EvolutionStore(db).list_proposals() == []


def test_export_requires_validated_version_and_no_overwrite(tmp_path, trusted_identity):
    module = lifecycle()
    db, run, asset, keyring = signed_asset(tmp_path, trusted_identity)
    proposal = module.propose_asset(asset['asset_id'], db, keyring=keyring,
        evidence_run_ids=[run['run_id']])
    with pytest.raises((KeyError, ValueError)):
        module.export_version(proposal['proposal_id'], db, tmp_path / 'export')
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    active = apply_candidate(proposal['proposal_id'], db)
    destination = tmp_path / 'occupied'
    destination.mkdir()
    (destination / 'sentinel').write_text('preserve')
    with pytest.raises((ValueError, FileExistsError)):
        module.export_version(active['version_id'], db, destination)
    assert (destination / 'sentinel').read_text() == 'preserve'


@pytest.mark.parametrize('phase', ['validate', 'apply', 'production'])
@pytest.mark.parametrize('change', ['delete', 'null'])
def test_source_provenance_cannot_be_downgraded_after_proposal(tmp_path, trusted_identity, phase, change):
    module = lifecycle()
    db, run, asset, keyring = signed_asset(tmp_path, trusted_identity)
    proposal = module.propose_asset(asset['asset_id'], db, keyring=keyring,
        evidence_run_ids=[run['run_id']])
    active = None
    if phase != 'validate':
        assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    if phase == 'production':
        active = apply_candidate(proposal['proposal_id'], db)
    store = EvolutionStore(db)
    with store.connect() as connection:
        row = connection.execute('SELECT payload_json FROM evolution_proposals WHERE proposal_id=?',
                                 (proposal['proposal_id'],)).fetchone()
        payload = json.loads(row[0])
        if change == 'delete':
            del payload['provenance']
        else:
            payload['provenance'] = None
        connection.execute('UPDATE evolution_proposals SET payload_json=? WHERE proposal_id=?',
                           (json.dumps(payload), proposal['proposal_id']))
    if phase == 'validate':
        assert validate_candidate(proposal['proposal_id'], db)['passed'] is False
    elif phase == 'apply':
        with pytest.raises(ValueError, match='source|provenance|fingerprint|identity'):
            apply_candidate(proposal['proposal_id'], db)
    else:
        result = next_task(tmp_path, db)
        assert result['status'] == 'succeeded'
        assert result['artifact']['actual_executor']['kind'] == 'builtin'
        assert store.get_version(active['version_id'])['state'] == 'quarantine'
    assert store.get_active('executor', 'csv_executor') is None
