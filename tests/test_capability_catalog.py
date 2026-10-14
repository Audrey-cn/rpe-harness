import copy
import shutil
import sqlite3
from pathlib import Path

import pytest


def trusted_catalog(tmp_path):
    from rpe_harness.capability_catalog import build_catalog
    from rpe_harness.skill_catalog import SkillCatalog
    root = tmp_path / 'skills'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx',
                    root / 'productivity/xlsx')
    skills = SkillCatalog(tmp_path / 'catalog.sqlite', tmp_path / 'owned')
    skills.sync(root)
    skills.trust_adapter('productivity/xlsx')
    return build_catalog(db_path=skills.db_path, catalog_root=skills.owned_root), skills


def test_explicit_capability_cannot_be_changed_by_same_format():
    from rpe_harness.capability_catalog import CapabilityCatalog
    catalog = CapabilityCatalog()
    assert catalog.resolve('tabular.deduplicate', 'xlsx')['status'] == 'ready'
    inspect = catalog.resolve('xlsx.inspect', 'xlsx')
    assert inspect['status'] == 'unsupported'
    assert inspect['candidates'][0]['adapter_status'] == 'unbound'
    assert catalog.resolve(input_format='xlsx')['status'] == 'ambiguous'
    assert catalog.resolve('json.normalize', 'xlsx')['status'] == 'unsupported'


def test_unknown_capability_constraints_and_missing_selector_are_explicit():
    from rpe_harness.capability_catalog import CapabilityCatalog
    catalog = CapabilityCatalog()
    assert catalog.resolve('pdf.summarize')['status'] == 'unsupported'
    assert catalog.resolve('json.normalize', constraints=['preserve_formulas'])['status'] == 'unsupported'
    assert catalog.resolve()['status'] == 'needs_context'
    assert catalog.resolve('tabular.deduplicate', 'csv', ['preserve_formulas'])['status'] == 'unsupported'
    assert catalog.resolve('json.normalize')['candidates'][0]['required_execution'] == [
        'input_path', 'output_path', 'style']


def test_discovery_rows_do_not_grant_execution_and_snapshots_are_immutable():
    from rpe_harness.capability_catalog import CapabilityCatalog
    row = {'skill_id': 'cold/example', 'name': 'Cold skill', 'description': 'inspect xlsx',
           'capabilities': ['xlsx.inspect'], 'input_formats': ['xlsx'], 'temperature': 'cold',
           'status': 'available', 'adapter_status': 'trusted', 'executable': True}
    catalog = CapabilityCatalog([row])
    assert any(e.get('skill_id') == 'cold/example' and e['adapter_status'] == 'discovery_only'
               and e['temperature'] == 'cold' for e in catalog.entries())
    before = catalog.snapshot_id
    row['description'] = 'mutated'
    leaked = catalog.entries()
    leaked[0]['capability'] = 'changed'
    assert catalog.snapshot_id == before
    assert catalog.resolve('xlsx.inspect')['status'] == 'unsupported'


def test_trusted_binding_and_revocation_change_current_catalog(tmp_path):
    catalog, skills = trusted_catalog(tmp_path)
    resolved = catalog.resolve('xlsx.inspect', 'xlsx')
    assert resolved['status'] == 'ready'
    assert resolved['candidates'][0]['skill_binding']['skill_id'] == 'productivity/xlsx'
    with sqlite3.connect(skills.db_path) as db:
        db.execute('DELETE FROM rpe_skill_trust')
    assert catalog.resolve('xlsx.inspect')['status'] == 'unsupported'
    assert catalog.snapshot_id != resolved['catalog_sha256']


def test_real_binding_bytes_are_checked_even_when_metadata_says_trusted(tmp_path):
    catalog, skills = trusted_catalog(tmp_path)
    row = skills.lookup('productivity/xlsx')
    Path(row['skill_binding']['script_path']).chmod(0o600)
    Path(row['skill_binding']['script_path']).write_text('print("tampered")')
    assert catalog.resolve('xlsx.inspect')['status'] == 'unsupported'


def test_explicit_resolve_never_rebuilds_or_scans_unrelated_discovery_rows(monkeypatch):
    from rpe_harness.capability_catalog import CapabilityCatalog
    catalog = CapabilityCatalog({'skill_id': f'cold/{i}', 'description': 'unrelated',
        'status': 'available', 'temperature': 'cold'} for i in range(2000))
    class Unscannable:
        def __iter__(self):
            raise AssertionError('resolve scanned unrelated rows')
    catalog._rows = Unscannable()
    for _ in range(3):
        assert catalog.resolve('json.normalize', 'json')['status'] == 'ready'
        assert catalog.resolve('tabular.deduplicate', 'csv')['status'] == 'ready'


def test_current_pointer_or_trust_does_not_reload_unrelated_skill_bodies(tmp_path, monkeypatch):
    from rpe_harness.skill_catalog import SkillCatalog
    catalog, skills = trusted_catalog(tmp_path)
    def no_lookup(*args, **kwargs):
        raise AssertionError('ordinary resolve reloaded skill bodies')
    monkeypatch.setattr(SkillCatalog, 'lookup', no_lookup)
    assert catalog.resolve('json.normalize')['status'] == 'ready'
    assert catalog.snapshot_id
