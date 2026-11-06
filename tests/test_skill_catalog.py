import hashlib
from pathlib import Path
import shutil

import pytest

XLSX_FIXTURE = Path(__file__).parent / 'fixtures/skills/productivity/xlsx'
XLSX_REVIEWED_HELPER_SHA256 = '5f8dc99437e4b65d72cac4f99ef25d383b477926d5ae31e0e8aae6fa3266d935'


def copy_xlsx_skill(destination, *, reviewed_noqa=False):
    shutil.copytree(XLSX_FIXTURE, destination)
    if reviewed_noqa:
        helper = destination / 'scripts/xlsx_read.py'
        original = helper.read_bytes()
        exception_line = b'    except Exception as exc:\n'
        assert original.count(exception_line) == 1
        reviewed = original.replace(exception_line, b'    except Exception as exc:  # noqa: BLE001\n')
        assert hashlib.sha256(reviewed).hexdigest() == XLSX_REVIEWED_HELPER_SHA256
        helper.write_bytes(reviewed)


def catalog_type():
    from rpe_harness.skill_catalog import SkillCatalog
    return SkillCatalog


def skill(root, name='xlsx', description='Excel CSV spreadsheet', relative='productivity/xlsx'):
    path = root / relative / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: {description}\nmetadata:\n  hermes:\n    tags: [xlsx, csv]\n---\n# Guide\n')
    return path


def test_sync_change_delete_and_historical_snapshot(tmp_path):
    source = tmp_path / 'source'
    path = skill(source)
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    first = catalog.sync(source)
    assert first['added'] == ['productivity/xlsx']
    entry = catalog.lookup('productivity/xlsx')
    assert entry['skill_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert catalog.sync(source)['snapshot_id'] == first['snapshot_id']
    path.write_text(path.read_text().replace('Excel CSV', 'Changed CSV'))
    second = catalog.sync(source)
    assert second['changed'] == ['productivity/xlsx']
    assert catalog.lookup('productivity/xlsx', snapshot_id=first['snapshot_id'])['status'] == 'stale'
    path.unlink()
    assert catalog.sync(source)['removed'] == ['productivity/xlsx']
    with pytest.raises(KeyError):
        catalog.lookup('productivity/xlsx')


def test_cold_query_survives_restart_and_unknown_has_no_adapter(tmp_path):
    root = tmp_path / 'skills'
    skill(root)
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    catalog.sync(root)
    catalog.set_temperature('productivity/xlsx', 'cold')
    reopened = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    assert reopened.query('Excel', input_format='xlsx')['candidates'][0]['temperature'] == 'cold'
    assert reopened.query('Excel', include_cold=False)['candidates'] == []
    assert reopened.query('JSON whitespace', capability='json.normalize')['reason'] == 'no_adapter'


def test_duplicate_declared_names_use_paths_and_symlink_is_unavailable(tmp_path):
    root = tmp_path / 'skills'
    skill(root, relative='a/xlsx')
    skill(root, relative='b/xlsx', description='Different Excel')
    link = root / 'linked/SKILL.md'
    link.parent.mkdir()
    link.symlink_to(root / 'a/xlsx/SKILL.md')
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    catalog.sync(root)
    assert catalog.lookup('a/xlsx')['load_name'] == 'a/xlsx'
    assert catalog.lookup('b/xlsx')['load_name'] == 'b/xlsx'
    assert catalog.lookup('linked')['status'] == 'symlink'
    assert catalog.lookup('a/xlsx')['adapter_status'] == 'no_adapter'


def test_source_tampering_and_external_capability_does_not_authorize(tmp_path):
    root = tmp_path / 'skills'
    path = skill(root)
    path.write_text(path.read_text().replace('# Guide', 'capabilities: [xlsx.inspect]\n# Guide'))
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    catalog.sync(root)
    with pytest.raises(ValueError):
        catalog.trust_adapter('productivity/xlsx')
    path.write_text(path.read_text().replace('Excel', 'Wrong'))
    assert catalog.lookup('productivity/xlsx')['status'] == 'stale'


def test_real_xlsx_trust_materialize_and_snapshot_tampering(tmp_path):
    root = tmp_path / 'skills'
    copy_xlsx_skill(root / 'productivity/xlsx')
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    snapshot = catalog.sync(root)
    assert catalog.lookup('productivity/xlsx')['adapter_status'] == 'untrusted'
    binding = catalog.trust_adapter('productivity/xlsx')
    assert set(binding) == {'snapshot_id', 'skill_id', 'skill_sha256', 'script_sha256', 'script_path'}
    materialized = catalog.materialize(tmp_path / 'home/skills', snapshot_id=snapshot['snapshot_id'])
    assert (tmp_path / 'home/skills/productivity/xlsx/SKILL.md').read_bytes() == (root / 'productivity/xlsx/SKILL.md').read_bytes()
    assert materialized['entries'][0]['load_name'] == 'xlsx'
    Path(binding['script_path']).chmod(0o600)
    Path(binding['script_path']).write_text('print("wrong")')
    assert catalog.lookup('productivity/xlsx')['status'] == 'corrupt'
    with pytest.raises(ValueError):
        catalog.materialize(tmp_path / 'other/skills')


@pytest.mark.parametrize('relative', ['productivity/xlsx', '.archive/xlsx', 'xlsx'])
def test_audited_xlsx_revision_is_discoverable_but_requires_explicit_trust(tmp_path, relative):
    root = tmp_path / 'skills'
    copy_xlsx_skill(root / relative, reviewed_noqa=True)
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    catalog.sync(root)
    candidates = catalog.query('xlsx', capability='xlsx.inspect')['candidates']
    assert len(candidates) == 1
    row = candidates[0]
    assert row['skill_id'] == 'productivity/xlsx'
    assert row['adapter_status'] == 'untrusted' and row['executable'] is False
    binding = catalog.trust_adapter(row['skill_id'])
    assert binding['script_sha256'] == XLSX_REVIEWED_HELPER_SHA256
    assert catalog.lookup(row['skill_id'])['executable'] is True


@pytest.mark.parametrize('changed_file', ['SKILL.md', 'scripts/xlsx_read.py'])
def test_another_comment_is_not_covered_by_the_xlsx_revision(tmp_path, changed_file):
    root = tmp_path / 'skills'
    source = root / 'productivity/xlsx'
    copy_xlsx_skill(source, reviewed_noqa=True)
    path = source / changed_file
    path.write_bytes(path.read_bytes() + b'\n# Another annotation\n')
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    catalog.sync(root)
    assert catalog.query('xlsx', capability='xlsx.inspect')['candidates'] == []
    with pytest.raises(ValueError, match='no registered adapter'):
        catalog.trust_adapter('productivity/xlsx')


@pytest.mark.parametrize('kwargs', [{'limit': 0}, {'limit': 11}, {'q': 'x' * 4097}, {'include_cold': 1}])
def test_query_budgets(tmp_path, kwargs):
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    with pytest.raises(ValueError):
        catalog.query(**({'q': 'xlsx'} | kwargs))


def test_oversized_and_invalid_skill_are_retained_but_not_discovered(tmp_path):
    root = tmp_path / 'skills'
    path = skill(root, relative='a/large')
    path.write_bytes(b'x' * 65537)
    bad = skill(root, relative='a/bad')
    bad.write_text('not frontmatter')
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    assert catalog.sync(root)['total'] == 2
    assert catalog.lookup('a/large')['status'] == 'unavailable'
    assert catalog.lookup('a/bad')['status'] == 'unavailable'
    assert catalog.query('large')['candidates'] == []


def test_declared_name_collision_with_other_directory_name_refuses_bare_alias(tmp_path):
    root = tmp_path / 'skills'
    skill(root, name='book', relative='a/one')
    skill(root, name='other', relative='b/book')
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    catalog.sync(root)
    assert catalog.lookup('a/one')['load_name'] == 'a/one'


def test_symlink_destination_cannot_escape_materialization(tmp_path):
    root = tmp_path / 'skills'
    skill(root, relative='a/book')
    catalog = catalog_type()(tmp_path / 'db.sqlite', tmp_path / 'owned')
    catalog.sync(root)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (tmp_path / 'linked').symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError):
        catalog.materialize(tmp_path / 'linked/skills')
    assert list(elsewhere.iterdir()) == []
