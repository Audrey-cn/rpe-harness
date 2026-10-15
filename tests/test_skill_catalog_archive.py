"""Native Hermes lifecycle paths retain bounded metadata and pinned identity."""
import shutil
from pathlib import Path
import pytest
from rpe_harness.skill_catalog import SkillCatalog, XLSX_SKILL_ID
from test_skill_catalog import XLSX_FIXTURE, skill


def test_physically_archived_real_skill_discovered_as_cold_and_explicitly_trusted(tmp_path):
    root = tmp_path / 'skills'
    shutil.copytree(XLSX_FIXTURE, root / '.archive/xlsx')
    catalog = SkillCatalog(tmp_path / 'state.sqlite', tmp_path / 'catalog')
    catalog.sync(root)
    row = catalog.query('spreadsheet')['candidates'][0]
    assert row['skill_id'] == XLSX_SKILL_ID
    assert row['temperature'] == 'cold'
    assert row['adapter_status'] == 'untrusted'
    assert catalog.query('spreadsheet', include_cold=False)['candidates'] == []
    catalog.trust_adapter(XLSX_SKILL_ID)
    assert catalog.lookup(XLSX_SKILL_ID)['executable'] is True
    assert (root / '.archive/xlsx/SKILL.md').exists()
    assert not (root / 'xlsx').exists()


def test_flat_native_restoration_rebinds_only_exact_registered_bytes(tmp_path):
    root = tmp_path / 'skills'
    shutil.copytree(XLSX_FIXTURE, root / '.archive/xlsx')
    catalog = SkillCatalog(tmp_path / 'state.sqlite', tmp_path / 'catalog')
    before = catalog.sync(root)['snapshot_id']
    catalog.trust_adapter(XLSX_SKILL_ID)
    shutil.move(root / '.archive/xlsx', root / 'xlsx')
    catalog.sync(root)
    row = catalog.lookup(XLSX_SKILL_ID)
    assert row['temperature'] == 'warm'
    assert row['adapter_status'] == 'untrusted'  # New source-bound snapshot needs explicit trust.
    assert catalog.lookup(XLSX_SKILL_ID, before)['status'] == 'stale'
    (root / 'xlsx/scripts/xlsx_read.py').write_text('print("wrong")')
    catalog.sync(root)
    with pytest.raises(KeyError):
        catalog.lookup(XLSX_SKILL_ID)
    assert catalog.lookup('xlsx')['executable'] is False


def test_archived_unregistered_skill_never_gains_adapter(tmp_path):
    root = tmp_path / 'skills'
    skill(root, name='novel', relative='.archive/novel', description='Novel spreadsheet function')
    catalog = SkillCatalog(tmp_path / 'state.sqlite', tmp_path / 'catalog')
    catalog.sync(root)
    row = catalog.query('novel')['candidates'][0]
    assert row['temperature'] == 'cold'
    assert row['executable'] is False


def test_duplicate_native_archive_copies_do_not_collapse_to_one_trusted_identity(tmp_path):
    root = tmp_path / 'skills'
    for name in ['xlsx', 'xlsx-20261015010101']:
        shutil.copytree(XLSX_FIXTURE, root / '.archive' / name)
    catalog = SkillCatalog(tmp_path / 'state.sqlite', tmp_path / 'catalog')
    catalog.sync(root)
    with pytest.raises(KeyError):
        catalog.lookup(XLSX_SKILL_ID)
    assert all(not row['executable'] for row in catalog.query('spreadsheet')['candidates'])
