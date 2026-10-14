"""Asset boundary tests: bytes remain inert and provenance never grants authority."""
import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest


def api():
    return importlib.import_module('rpe_harness.assets')


def package(tmp_path, name='local/csv', content=b'raise RuntimeError("must never execute")\n', **changes):
    root = tmp_path / name.replace('/', '-')
    root.mkdir()
    manifest = {'schema_version': 'rpe.capability-package/v1', 'name': name,
                'component': 'executor', 'kind': 'csv_executor',
                'contract': {'capability': 'tabular.deduplicate', 'version': 1, 'input_format': 'csv'},
                'entrypoint': 'prepare', 'content_file': 'executor.py',
                'content_sha256': hashlib.sha256(content).hexdigest(), 'parents': [],
                'dependencies': [], 'resources': {'network': False, 'subprocess': False},
                'provenance': {'creator': 'test'}}
    manifest.update(changes)
    (root / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'executor.py').write_bytes(content)
    return root


def store(tmp_path):
    return api().AssetStore(tmp_path / 'owned' / 'state.db', tmp_path / 'owned')


def test_import_is_inert_idempotent_and_content_addressed(tmp_path):
    first = package(tmp_path)
    second = package(tmp_path, name='another/alias')
    assets = store(tmp_path)
    one = assets.import_package(first)
    two = assets.import_package(second)
    assert assets.import_package(first)['asset_id'] == one['asset_id']
    assert one['asset_id'] != two['asset_id']
    assert one['content_path'] == two['content_path']
    assert one['state'] == 'unverified' and not one['executable']
    assert one['signature'] is None
    assert one['manifest']['entrypoint'] == 'prepare'
    assert assets.read_content(one['asset_id']) == (first / 'executor.py').read_bytes()
    assert len(assets.list_assets()) == 2
    with assets.evolution.connect() as db:
        assert db.execute('SELECT count(*) FROM evolution_active').fetchone()[0] == 0


@pytest.mark.parametrize('changes', [
    {'content_sha256': '0' * 64}, {'content_sha256': 'A' * 64},
    {'content_file': '../escape.py'}, {'content_file': '/escape.py'},
    {'content_file': 'sub/../executor.py'}, {'content_file': 'manifest.json'},
    {'dependencies': ['anything']}, {'resources': {'network': True, 'subprocess': False}},
    {'resources': {'network': 0, 'subprocess': False}}, {'parents': ['bad']},
    {'contract': {'capability': 'tabular.deduplicate', 'version': True, 'input_format': 'csv'}},
    {'entrypoint': 'main'}, {'component': 'host'}, {'name': 'no-namespace'},
    {'schema_version': 'other/v1'}, {'provenance': []}, {'unexpected': 'field'},
])
def test_invalid_manifest_rejected(tmp_path, changes):
    root = package(tmp_path, **changes)
    with pytest.raises(ValueError):
        api().inspect_package(root)


@pytest.mark.parametrize('target', ['content', 'manifest', 'ancestor', 'package'])
def test_symlinks_rejected_at_every_component(tmp_path, target):
    root = package(tmp_path)
    if target in {'content', 'manifest'}:
        path = root / ('executor.py' if target == 'content' else 'manifest.json')
        retained = root / 'real'
        path.rename(retained)
        path.symlink_to(retained)
    else:
        alias = tmp_path / 'alias'
        alias.symlink_to(root if target == 'package' else tmp_path, target_is_directory=True)
        root = alias if target == 'package' else alias / root.name
    with pytest.raises(ValueError):
        api().inspect_package(root)


@pytest.mark.parametrize('special', ['fifo', 'directory'])
def test_special_content_files_rejected_without_blocking(tmp_path, special):
    root = package(tmp_path)
    path = root / 'executor.py'
    path.unlink()
    os.mkfifo(path) if special == 'fifo' else path.mkdir()
    with pytest.raises(ValueError):
        api().inspect_package(root)


@pytest.mark.parametrize('large', ['content', 'manifest', 'signature'])
def test_reads_are_bounded(tmp_path, large):
    root = package(tmp_path, content=b'x' * (65537 if large == 'content' else 1))
    if large != 'content':
        (root / (large + '.json')).write_bytes(b' ' * (1048576 + 1))
    with pytest.raises(ValueError):
        api().inspect_package(root)


@pytest.mark.parametrize('changed', ['manifest', 'signature', 'content'])
def test_retained_package_tampering_fails_closed(tmp_path, changed):
    root = package(tmp_path)
    (root / 'signature.json').write_text('{"untrusted":true}')
    assets = store(tmp_path)
    item = assets.import_package(root)
    path = Path(item[changed + '_path'])
    path.chmod(0o600)
    path.write_bytes(b'{}' if changed != 'content' else b'changed')
    with pytest.raises(ValueError):
        assets.get_asset(item['asset_id'])
    with pytest.raises(ValueError):
        assets.read_content(item['asset_id'])


def test_original_manifest_and_signature_bytes_retained(tmp_path):
    root = package(tmp_path)
    raw_signature = b'{ "not_trusted" : true }\n'
    (root / 'signature.json').write_bytes(raw_signature)
    raw_manifest = (root / 'manifest.json').read_bytes()
    item = store(tmp_path).import_package(root)
    assert Path(item['manifest_path']).read_bytes() == raw_manifest
    assert Path(item['signature_path']).read_bytes() == raw_signature
    assert item['state'] == 'unverified'


def test_recipe_and_legacy_abi(tmp_path):
    recipe = package(tmp_path, name='local/recipe', component='recipe', kind='recipe', entrypoint=None,
                     content=b'{"dedup":true}')
    assert api().inspect_package(recipe)['compatible']
    legacy = package(tmp_path, name='registry/old', component='discovery', kind='legacy_gene',
                     contract=None, entrypoint='main')
    assert not api().inspect_package(legacy)['compatible']
    assert not api().inspect_package(legacy)['executable']


def test_registry_six_names_five_contents_stay_discovery_only(tmp_path):
    registry = Path('/Users/audrey/项目/progenitor-registry/.akashic_index.json')
    rows = api().read_registry(registry)
    assert len(rows) == 6
    assert len({row['content_sha256'] for row in rows}) == 5
    assets = store(tmp_path)
    items = [api().import_registry_asset(assets, registry, row['name']) for row in rows]
    assert len({row['asset_id'] for row in items}) == 6
    assert len({row['content_path'] for row in items}) == 5
    assert all(row['state'] == 'unverified' and not row['compatible'] for row in items)
    assert all(row['manifest']['provenance']['external']['trust_state'] == 'registry_verified' for row in items)
    assert all('verified_success' not in row and 'enabled' not in row for row in items)


@pytest.mark.parametrize('change', ['hash', 'path', 'content'])
def test_registry_rechecks_path_and_all_hashes(tmp_path, change):
    registry = tmp_path / 'registry'
    (registry / 'genes').mkdir(parents=True)
    content = b'raise RuntimeError("not executable")'
    sha = hashlib.sha256(content).hexdigest()
    (registry / 'genes' / sha).write_bytes(content if change != 'content' else b'tampered')
    entry = {'cid': sha, 'content_sha256': sha, 'expected_sha256': sha,
             'registry_path': 'genes/' + sha, 'trust_state': 'registry_verified', 'score': 100}
    if change == 'hash':
        entry['expected_sha256'] = '0' * 64
    elif change == 'path':
        entry['registry_path'] = '../' + sha
    path = registry / '.akashic_index.json'
    path.write_text(json.dumps({'example': entry}))
    with pytest.raises(ValueError):
        api().import_registry_asset(store(tmp_path), path, 'example')


@pytest.mark.parametrize('field,value', [('kind', []), ('kind', {}), ('component', []),
                                         ('entrypoint', {}), ('content_file', []),
                                         ('parents', {}), ('resources', []), ('contract', [])])
def test_malformed_field_types_fail_as_value_error(tmp_path, field, value):
    root = package(tmp_path, **{field: value})
    with pytest.raises(ValueError):
        api().inspect_package(root)


def test_legacy_entrypoint_unhashable_fails_as_value_error(tmp_path):
    root = package(tmp_path, component='discovery', kind='legacy_gene', contract=None, entrypoint=[])
    with pytest.raises(ValueError):
        api().inspect_package(root)


@pytest.mark.parametrize('bad_json', [b'{"schema_version":1,"schema_version":2}', b'{"x":NaN}', b'\xff', b'[]'])
def test_invalid_json_rejected(tmp_path, bad_json):
    root = package(tmp_path)
    (root / 'manifest.json').write_bytes(bad_json)
    with pytest.raises(ValueError):
        api().inspect_package(root)


@pytest.mark.parametrize('target', ['signature', 'content', 'package_directory', 'content_directory'])
def test_retained_symlink_substitution_rejected(tmp_path, target):
    root = package(tmp_path)
    (root / 'signature.json').write_text('{}')
    assets = store(tmp_path)
    item = assets.import_package(root)
    if target.endswith('_directory'):
        path = Path(item['package_path']) if target == 'package_directory' else Path(item['content_path']).parent
    else:
        path = Path(item[target + '_path'])
    real = path.with_name(path.name + '-original')
    path.rename(real)
    path.symlink_to(real, target_is_directory=real.is_dir())
    with pytest.raises(ValueError):
        assets.read_content(item['asset_id'])


def test_store_rejects_symlink_owned_ancestor_before_writing(tmp_path):
    external = tmp_path / 'external'
    external.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError):
        api().AssetStore(alias / 'owned' / 'state.db', alias / 'owned')
    assert not list(external.iterdir())


def test_nested_content_retained_without_executing(tmp_path):
    root = package(tmp_path, content_file='source/entry.py')
    (root / 'source').mkdir()
    (root / 'executor.py').rename(root / 'source' / 'entry.py')
    assets = store(tmp_path)
    item = assets.import_package(root)
    assert (Path(item['package_path']) / 'source' / 'entry.py').is_file()
    assert assets.read_content(item['asset_id']) == (root / 'source' / 'entry.py').read_bytes()


def test_read_only_asset_view_reuses_store_during_immediate_transaction(tmp_path):
    assets = store(tmp_path)
    item = assets.import_package(package(tmp_path))
    with assets.evolution.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        view = api().AssetStore.from_evolution(assets.evolution)
        assert view.evolution is assets.evolution
        assert view.get_asset(item['asset_id'])['manifest'] == item['manifest']
        assert view.read_content(item['asset_id']) == Path(item['content_path']).read_bytes()
