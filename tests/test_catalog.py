"""Local release governance rejects tampering without granting execution authority."""
from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from rpe_harness.assets.identity import canonical_json, sign_envelope
from rpe_harness.evolution import EvolutionStore, propose_candidate, validate_candidate
from rpe_harness.evolution.candidates import current_revisions
from rpe_harness.evolution.store import digest
from test_asset_trust import generate_test_identity
from test_candidates import GOOD_CODE
from test_evolution import verified_run


RECIPE = {'capability': 'tabular.deduplicate', 'input_format': 'csv', 'keep': 'first',
          'procedure': ['plan', 'prepare', 'independent_verify', 'exclusive_publish']}


def api():
    try:
        return importlib.import_module('rpe_harness.assets.catalog')
    except ImportError:
        pytest.fail('release catalog API is missing')


@pytest.fixture(scope='session')
def keys(tmp_path_factory):
    root = tmp_path_factory.mktemp('catalog-keys')
    creator = generate_test_identity(root / 'creator')[0]
    registry = generate_test_identity(root / 'registry')[0]
    return creator, registry


def policy(keys):
    return {'schema_version': 'rpe.trust-policy/v1', 'trusted_keys': [
        {'key_id': key['public_key_id'], 'owner': role, 'roles': [role], 'scopes': [scope],
         'public_key': key['public_key'], 'revoked': False}
        for key, role, scope in ((keys[0], 'creator', 'capability-package'),
                                 (keys[1], 'registry', 'registry-index'))]}


@pytest.fixture(scope='session')
def source(tmp_path_factory):
    root = tmp_path_factory.mktemp('catalog-source')
    db, run = verified_run(root)
    proposal = propose_candidate(db, root, component='recipe', kind='recipe', content=RECIPE,
                                 evidence_run_ids=[run['run_id']])
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    return db, EvolutionStore(db).get_proposal(proposal['proposal_id'])['version_id']


@pytest.fixture
def bundle(tmp_path, keys, source):
    root = tmp_path / 'bundle'
    api().build_release_bundle([source[1]], source[0], root,
        creator_identity=keys[0], registry_identity=keys[1], keyring=policy(keys))
    return root


def rewrite(path, value):
    path.chmod(0o600)
    path.write_bytes(canonical_json(value))


def catalog(root):
    return json.loads((root / 'catalog.json').read_bytes())


def resign(root, document, keys):
    rewrite(root / 'catalog.json', sign_envelope(document, keys[1], scope='registry-index'))


def test_build_and_integrity_are_inert_and_sanitized(bundle, keys, source):
    before = EvolutionStore(source[0]).pointer('recipe', 'recipe')
    report = api().validate_release_bundle(bundle, keyring=policy(keys))
    assert report['passed'] is True
    assert report['published'] is report['activated'] is False
    assert report['independent_acceptance'] == 'not_run'
    assert report['producer_validation'] == 'attested'
    assert EvolutionStore(source[0]).pointer('recipe', 'recipe') == before == (None, 0)
    document = catalog(bundle)['document']
    entry = document['entries'][0]
    assert entry['source_version_id'] == source[1]
    assert entry['creator_key_id'] == keys[0]['public_key_id']
    assert document['producer']['revisions'] == current_revisions()
    for name in ('catalog.json', 'audit.json'):
        text = (bundle / name).read_text()
        assert str(source[0].parent) not in text and 'private_key' not in text
    assert api().validate_release_bundle(bundle, keyring={'schema_version': 'rpe.trust-policy/v1',
                                                        'trusted_keys': []})['passed'] is False


@pytest.mark.parametrize('key_index', [0, 1])
@pytest.mark.parametrize('change', ['revoked', 'role', 'scope'])
def test_fresh_local_policy_controls_both_signers(bundle, keys, tmp_path, key_index, change):
    local = policy(keys)
    target = tmp_path / 'local-policy.json'
    target.write_bytes(canonical_json(local))
    assert api().validate_release_bundle(bundle, keyring=target)['passed'] is True
    if change == 'revoked':
        local['trusted_keys'][key_index]['revoked'] = True
    elif change == 'role':
        local['trusted_keys'][key_index]['roles'] = ['registry' if key_index == 0 else 'creator']
    else:
        local['trusted_keys'][key_index]['scopes'] = [
            'registry-index' if key_index == 0 else 'capability-package']
    target.write_bytes(canonical_json(local))
    assert api().validate_release_bundle(bundle, keyring=target)['passed'] is False


@pytest.mark.parametrize('file', ['catalog.json', 'audit.json', 'manifest.json',
                                  'signature.json', 'recipe.json'])
def test_each_exact_file_is_bound(bundle, keys, file):
    entry = catalog(bundle)['document']['entries'][0]
    path = bundle / file if file in {'catalog.json', 'audit.json'} else bundle / entry['package_path'] / file
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b' ')
    # Catalog uses canonical signed objects; harmless whitespace on the envelope itself
    # does not change its signed payload, unlike the catalog's exact bound child files.
    if file == 'catalog.json':
        value = json.loads(path.read_bytes())
        value['document']['producer']['version'] = 'untrusted'
        rewrite(path, value)
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is False


@pytest.mark.parametrize('where', ['', 'packages', 'package'])
def test_extra_files_never_succeed(bundle, keys, where):
    entry = catalog(bundle)['document']['entries'][0]
    directory = bundle / entry['package_path'] if where == 'package' else bundle / where
    (directory / 'extra.pyc').write_bytes(b'extra')
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is False


@pytest.mark.parametrize('change', ['missing', 'path', 'duplicate', 'stale', 'empty', 'audit_claim'])
def test_signed_bad_catalog_cannot_bypass_structural_checks(bundle, keys, change):
    document = catalog(bundle)['document']
    entry = document['entries'][0]
    if change == 'missing':
        (bundle / entry['package_path'] / 'signature.json').unlink()
    elif change == 'path':
        entry['package_path'] = '../outside'
    elif change == 'duplicate':
        document['entries'].append(copy.deepcopy(entry))
    elif change == 'stale':
        document['producer']['revisions']['acceptance_revision'] = '0' * 64
    elif change == 'empty':
        document['entries'] = []
    else:
        audit = json.loads((bundle / 'audit.json').read_bytes())
        audit['entries'][0]['source_version_id'] = '0' * 64
        rewrite(bundle / 'audit.json', audit)
        document['audit_sha256'] = digest((bundle / 'audit.json').read_bytes())
    resign(bundle, document, keys)
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is False


@pytest.mark.parametrize('target', ['root', 'ancestor', 'content', 'directory', 'policy'])
def test_symlinks_are_rejected(bundle, keys, tmp_path, target):
    entry = catalog(bundle)['document']['entries'][0]
    submitted = bundle
    local = policy(keys)
    if target == 'root':
        submitted = tmp_path / 'alias'
        submitted.symlink_to(bundle, target_is_directory=True)
    elif target == 'ancestor':
        (tmp_path / 'alias').symlink_to(tmp_path, target_is_directory=True)
        submitted = tmp_path / 'alias' / 'bundle'
    elif target == 'policy':
        real = tmp_path / 'real.json'
        real.write_bytes(canonical_json(local))
        local = tmp_path / 'alias.json'
        local.symlink_to(real)
    else:
        path = bundle / entry['package_path']
        if target == 'content':
            path = path / 'recipe.json'
            real = tmp_path / 'real.json'
            real.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(real)
        else:
            real = tmp_path / 'moved'
            path.rename(real)
            path.symlink_to(real, target_is_directory=True)
    assert api().validate_release_bundle(submitted, keyring=local)['passed'] is False


def test_recipe_acceptance_has_real_checks_and_requires_owned_root(bundle, keys, tmp_path):
    assert api().validate_release_bundle(bundle, keyring=policy(keys), mode='acceptance')['passed'] is False
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    report = api().validate_release_bundle(bundle, keyring=policy(keys), mode='acceptance',
                                           owned_root=scratch)
    assert report['passed'] is True and report['independent_acceptance'] == 'passed'
    assert len(report['entries'][0]['acceptance']['checks']) == 13
    assert all(row['passed'] for row in report['entries'][0]['acceptance']['checks'])
    assert list(scratch.rglob('*.sqlite')) == []


def test_build_errors_never_publish_partial_or_overwrite_unknown_directory(tmp_path, keys, source):
    target = tmp_path / 'bundle'
    with pytest.raises((ValueError, KeyError)):
        api().build_release_bundle([source[1], '0' * 64], source[0], target,
            creator_identity=keys[0], registry_identity=keys[1], keyring=policy(keys))
    assert not target.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == []
    target.mkdir()
    (target / 'sentinel').write_text('preserve')
    with pytest.raises((ValueError, FileExistsError)):
        api().build_release_bundle([source[1]], source[0], target,
            creator_identity=keys[0], registry_identity=keys[1], keyring=policy(keys))
    assert (target / 'sentinel').read_text() == 'preserve'


def test_standalone_ci_cli_reports_success_and_failure_without_a_db(bundle, keys, tmp_path):
    local = tmp_path / 'policy.json'
    local.write_bytes(canonical_json(policy(keys)))
    report = tmp_path / 'report.json'
    command = [sys.executable, '-B', '-m', 'rpe_harness.assets.prepublish',
               '--bundle', str(bundle), '--keyring', str(local), '--report', str(report)]
    result = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['passed'] is True
    assert json.loads(report.read_bytes())['independent_acceptance'] == 'not_run'
    local.write_bytes(canonical_json({'schema_version': 'rpe.trust-policy/v1', 'trusted_keys': []}))
    result = subprocess.run(command[:-2], capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode == 1 and json.loads(result.stdout)['passed'] is False
    assert list(tmp_path.rglob('*.sqlite')) == []


def test_cli_report_must_be_external_and_must_not_overwrite(bundle, keys, tmp_path):
    local = tmp_path / 'policy.json'
    local.write_bytes(canonical_json(policy(keys)))
    inside = bundle / 'extra-report.json'
    command = [sys.executable, '-B', '-m', 'rpe_harness.assets.prepublish',
               '--bundle', str(bundle), '--keyring', str(local), '--report', str(inside)]
    result = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode == 1
    assert not inside.exists()
    sentinel = tmp_path / 'old-report.json'
    sentinel.write_text('preserve')
    result = subprocess.run(command[:-1] + [str(sentinel)], capture_output=True, text=True,
                            env=os.environ.copy())
    assert result.returncode == 1 and sentinel.read_text() == 'preserve'


def test_build_does_not_follow_identity_or_destination_ancestor_alias(tmp_path, keys, source):
    private = tmp_path / 'identity.json'
    private.write_bytes(canonical_json(keys[0]))
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        api().build_release_bundle([source[1]], source[0], tmp_path / 'bundle',
            creator_identity=alias / 'identity.json', registry_identity=keys[1], keyring=policy(keys))
    assert not (tmp_path / 'bundle').exists()
    with pytest.raises(ValueError):
        api().build_release_bundle([source[1]], source[0], alias / 'bundle',
            creator_identity=keys[0], registry_identity=keys[1], keyring=policy(keys))
    assert not (tmp_path / 'bundle').exists()


def repackage(bundle, keys, *, content_file, content, kind='recipe', signature_document=None):
    """Publisher-controlled attestation fixture; no local validation claim is independently trusted."""
    document = catalog(bundle)['document']
    entry = document['entries'][0]
    old = bundle / entry['package_path']
    manifest = json.loads((old / 'manifest.json').read_bytes())
    (old / manifest['content_file']).unlink()
    manifest.update(content_file=content_file, content_sha256=digest(content), kind=kind,
                    component='executor' if kind == 'csv_executor' else 'recipe',
                    entrypoint='prepare' if kind == 'csv_executor' else None)
    target = old / content_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    if signature_document:
        manifest['provenance']['release_reviewed'] = False
        signed_document = copy.deepcopy(manifest)
        signature_document(signed_document)
    else:
        signed_document = manifest
    signature = sign_envelope(signed_document, keys[0], scope='capability-package')
    rewrite(old / 'manifest.json', manifest)
    rewrite(old / 'signature.json', signature)
    identifier = digest({'manifest': manifest, 'signature': signature})
    new = bundle / 'packages' / identifier
    old.rename(new)
    entry.update(asset_id=identifier, package_path='packages/' + identifier,
                 content_sha256=digest(content), manifest_sha256=digest((new / 'manifest.json').read_bytes()),
                 signature_sha256=digest((new / 'signature.json').read_bytes()))
    audit = json.loads((bundle / 'audit.json').read_bytes())
    audit['entries'][0]['asset_id'] = identifier
    rewrite(bundle / 'audit.json', audit)
    document['audit_sha256'] = digest((bundle / 'audit.json').read_bytes())
    resign(bundle, document, keys)


def test_nested_content_is_supported_but_no_extra_nested_directories(bundle, keys):
    repackage(bundle, keys, content_file='source/config/recipe.json', content=canonical_json(RECIPE))
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is True
    entry = catalog(bundle)['document']['entries'][0]
    (bundle / entry['package_path'] / 'source' / 'unexpected').mkdir()
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is False


def test_integrity_never_executes_content_and_missing_sandbox_is_acceptance_failure(
        bundle, keys, tmp_path, monkeypatch):
    marker = tmp_path / 'executed'
    code = ('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("bad")\n').encode()
    repackage(bundle, keys, content_file='candidate.py', content=code, kind='csv_executor')
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is True
    assert not marker.exists()
    from rpe_harness.evolution import sandbox
    monkeypatch.setattr(sandbox.sys, 'platform', 'linux')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    report = api().validate_release_bundle(bundle, keyring=policy(keys), mode='acceptance',
                                           owned_root=scratch)
    assert report['passed'] is False and report['independent_acceptance'] == 'failed'
    assert 'sandbox' in report['error']
    assert not marker.exists() and list(scratch.iterdir()) == []


def test_oversized_metadata_and_special_content_fail_closed(bundle, keys):
    entry = catalog(bundle)['document']['entries'][0]
    content = bundle / entry['package_path'] / 'recipe.json'
    content.unlink()
    os.mkfifo(content)
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is False
    content.unlink()
    content.write_bytes(b'x' * 65537)
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is False
    path = bundle / 'catalog.json'
    path.chmod(0o600)
    path.write_bytes(b' ' * 1048577)
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is False


def test_creator_signature_binds_exact_json_types_in_the_manifest(bundle, keys):
    repackage(bundle, keys, content_file='recipe.json', content=canonical_json(RECIPE),
              signature_document=lambda value: value['provenance'].update(release_reviewed=0))
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is False


def test_revision_numbers_cannot_be_signed_as_equal_floats(bundle, keys):
    document = catalog(bundle)['document']
    document['producer']['revisions']['contract_version'] = float(
        document['producer']['revisions']['contract_version'])
    resign(bundle, document, keys)
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is False


def test_csv_bundle_independent_acceptance_replays_real_sandbox(tmp_path, keys):
    root = tmp_path / 'producer'
    db, run = verified_run(root)
    proposal = propose_candidate(db, root, component='executor', kind='csv_executor',
                                 content=GOOD_CODE, evidence_run_ids=[run['run_id']])
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    identifier = EvolutionStore(db).get_proposal(proposal['proposal_id'])['version_id']
    bundle = tmp_path / 'release'
    api().build_release_bundle([identifier], db, bundle, creator_identity=keys[0],
                               registry_identity=keys[1], keyring=policy(keys))
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    report = api().validate_release_bundle(bundle, keyring=policy(keys), mode='acceptance',
                                           owned_root=scratch)
    assert report['passed'] is True and report['independent_acceptance'] == 'passed'
    checks = report['entries'][0]['acceptance']['checks']
    assert len(checks) == 13 and all(check['passed'] for check in checks)
    assert EvolutionStore(db).pointer('executor', 'csv_executor') == (None, 0)
    assert list(scratch.iterdir()) == []


def test_failed_export_cleans_its_new_stage_without_publishing(tmp_path, keys):
    root = tmp_path / 'producer'
    db, run = verified_run(root)
    proposal = propose_candidate(db, root, component='recipe', kind='recipe', content=RECIPE,
                                 evidence_run_ids=[run['run_id']])
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    identifier = EvolutionStore(db).get_proposal(proposal['proposal_id'])['version_id']
    # A previously verified result can no longer back a current export after its bytes change.
    (root / 'result.csv').write_text('changed')
    release_parent = tmp_path / 'releases'
    release_parent.mkdir()
    with pytest.raises(ValueError, match='evidence|certificate'):
        api().build_release_bundle([identifier], db, release_parent / 'new',
            creator_identity=keys[0], registry_identity=keys[1], keyring=policy(keys))
    assert list(release_parent.iterdir()) == []


def test_explicit_bundle_budget_rejects_over_one_hundred_entries(bundle, keys):
    document = catalog(bundle)['document']
    document['entries'] *= 101
    resign(bundle, document, keys)
    assert api().validate_release_bundle(bundle, keyring=policy(keys))['passed'] is False


@pytest.mark.parametrize('action', ['build', 'export', 'occupied_build'])
def test_governance_rechecks_evidence_without_mutating_source_db_or_activation_leases(
        tmp_path, keys, action):
    from rpe_harness.assets.lifecycle import export_version
    root = tmp_path / 'producer'
    db, run = verified_run(root)
    proposal = propose_candidate(db, root, component='recipe', kind='recipe', content=RECIPE,
                                 evidence_run_ids=[run['run_id']])
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    store = EvolutionStore(db)
    identifier = store.get_proposal(proposal['proposal_id'])['version_id']
    lease = root / 'unlocked-activation.lease'
    lease.write_bytes(b'preserve activation lease')
    with sqlite3.connect(db) as connection:
        # Inspection still must rederive feedback from durable runs if no cached row exists.
        connection.execute('DELETE FROM evolution_feedback')
        connection.execute('INSERT INTO evolution_activations VALUES(?,?,?,?,?,?,?)',
            ('pending-governance-test', 'recipe:recipe', identifier, '{}', 'prepared', str(lease), 1))
    before = db.read_bytes()
    destination = tmp_path / 'release'
    if action == 'occupied_build':
        destination.mkdir()
        (destination / 'sentinel').write_text('preserve')
        with pytest.raises((FileExistsError, ValueError)):
            api().build_release_bundle([identifier], db, destination, creator_identity=keys[0],
                                       registry_identity=keys[1], keyring=policy(keys))
    elif action == 'export':
        assert export_version(identifier, db, destination, identity=keys[0])['exported'] is True
    else:
        assert api().build_release_bundle([identifier], db, destination, creator_identity=keys[0],
                                          registry_identity=keys[1], keyring=policy(keys))['built'] is True
    assert db.read_bytes() == before
    assert lease.read_bytes() == b'preserve activation lease'
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT state FROM evolution_activations WHERE activation_id=?',
                                  ('pending-governance-test',)).fetchone()[0] == 'prepared'
        assert connection.execute('SELECT COUNT(*) FROM evolution_feedback').fetchone()[0] == 0


def test_validated_stage_name_replacement_is_not_published_or_deleted(tmp_path, keys, source, monkeypatch):
    module = api()
    original = module.validate_release_bundle
    moved = tmp_path / 'retained-original-stage'
    replacement = []
    def swap(path, **kwargs):
        report = original(path, **kwargs)
        assert report['passed'] is True
        Path(path).rename(moved)
        Path(path).mkdir()
        (Path(path) / 'sentinel').write_text('unvalidated replacement')
        replacement.append(Path(path))
        return report
    monkeypatch.setattr(module, 'validate_release_bundle', swap)
    with pytest.raises(ValueError, match='stage|replaced|identity'):
        module.build_release_bundle([source[1]], source[0], tmp_path / 'release',
            creator_identity=keys[0], registry_identity=keys[1], keyring=policy(keys))
    assert not (tmp_path / 'release').exists()
    assert moved.is_dir()
    assert (replacement[0] / 'sentinel').read_text() == 'unvalidated replacement'


def test_acceptance_rejects_existing_symlink_descendant_without_external_fixture_writes(
        bundle, keys, tmp_path, monkeypatch):
    from rpe_harness.evolution import candidates
    original = candidates._replay_acceptance
    outside = tmp_path / 'outside'
    outside.mkdir()
    def inject(scope, *args, **kwargs):
        (scope.owned_root / 'evolution-validation').symlink_to(outside, target_is_directory=True)
        return original(scope, *args, **kwargs)
    monkeypatch.setattr(candidates, '_replay_acceptance', inject)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    report = api().validate_release_bundle(bundle, keyring=policy(keys), mode='acceptance',
                                           owned_root=scratch)
    assert report['passed'] is False
    assert report['independent_acceptance'] == 'failed'
    assert list(outside.iterdir()) == [] and list(scratch.iterdir()) == []


@pytest.mark.parametrize('kind', ['runs', 'evolution'])
def test_read_only_views_reject_missing_or_aliased_sources_and_cannot_write(tmp_path, kind):
    from rpe_harness.storage import RunStore
    cls = RunStore if kind == 'runs' else EvolutionStore
    missing = tmp_path / 'missing.sqlite'
    with pytest.raises((OSError, ValueError)):
        cls.read_only(missing)
    assert not missing.exists()
    root = tmp_path / 'producer'
    db, run = verified_run(root)
    EvolutionStore(db)
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        cls.read_only(alias / db.name)
    view = cls.read_only(db)
    with pytest.raises(sqlite3.OperationalError):
        with view.connect() as connection:
            connection.execute("UPDATE runs SET status='failed' WHERE run_id=?", (run['run_id'],))
    assert RunStore.read_only(db).get(run['run_id'])['status'] == 'succeeded'


def test_read_only_export_still_rejects_changed_cached_feedback(tmp_path, keys):
    from rpe_harness.assets.lifecycle import export_version
    root = tmp_path / 'producer'
    db, run = verified_run(root)
    proposal = propose_candidate(db, root, component='recipe', kind='recipe', content=RECIPE,
                                 evidence_run_ids=[run['run_id']])
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    identifier = EvolutionStore(db).get_proposal(proposal['proposal_id'])['version_id']
    with sqlite3.connect(db) as connection:
        connection.execute('UPDATE evolution_feedback SET payload_hash=? WHERE run_id=?',
                           ('0' * 64, run['run_id']))
    with pytest.raises(ValueError, match='feedback|evidence'):
        export_version(identifier, db, tmp_path / 'release', identity=keys[0])
    assert not (tmp_path / 'release').exists()
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT payload_hash FROM evolution_feedback WHERE run_id=?',
                                  (run['run_id'],)).fetchone()[0] == '0' * 64
