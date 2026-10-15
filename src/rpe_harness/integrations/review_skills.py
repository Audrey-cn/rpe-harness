"""Real skill discovery and immutable selections for the bounded review host."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re

from ..skill_catalog import SkillCatalog, _absolute, _read


SELECTION_FIELDS = {'kind', 'snapshot_id', 'skill_id', 'skill_sha256', 'script_sha256',
                    'source_path', 'adapter_status', 'capabilities', 'executable'}
PHASE_TOOL = {'discover': 'rpe_discover', 'select': 'rpe_select', 'view': 'skill_view',
              'propose': 'rpe_propose', 'none': 'none'}


def selection_for(row):
    """Keep original source/snapshot identities, never ephemeral native HOME paths."""
    return {'kind': 'selected', **{key: copy.deepcopy(row.get(key)) for key in
        ('snapshot_id', 'skill_id', 'skill_sha256', 'script_sha256', 'source_path',
         'adapter_status', 'capabilities', 'executable')}}


def validate_selection(db_path, catalog_root, selection):
    if type(selection) is not dict or not catalog_root:
        raise ValueError('skill selection requires its original catalog')
    catalog = SkillCatalog(db_path, catalog_root)
    if selection.get('kind') == 'none':
        if set(selection) != {'kind', 'snapshot_id'}:
            raise ValueError('invalid none skill selection')
        catalog._document(selection['snapshot_id'])
    else:
        if set(selection) != SELECTION_FIELDS or selection.get('kind') != 'selected':
            raise ValueError('invalid skill selection fields')
        try:
            row = catalog.lookup(selection['skill_id'], snapshot_id=selection['snapshot_id'])
        except (KeyError, OSError) as error:
            raise ValueError('selected skill is unavailable') from error
        if row['status'] != 'available' or selection_for(row) != selection:
            raise ValueError('skill selection or original source changed')
    return copy.deepcopy(selection)


def prepare_review_skills(snapshot):
    """Archive cold copies only inside a newly materialized isolated Hermes HOME.

    These copies are disposable. Canonical IDs and adapter bindings continue to
    refer to the durable catalog; Hermes restore_skill later restores a flat name.
    """
    value = copy.deepcopy(snapshot)
    root = _absolute(value['materialized_path'])
    if (root.name != 'skills' or not root.is_dir()
            or root == Path.home() / '.hermes/skills'):
        raise ValueError('review skills require an isolated materialized HOME')
    names = [row['name'] for row in value['entries']]
    for row in value['entries']:
        original = _absolute(row['source_path'])
        local = _absolute(row['skill_path'])
        if original.is_relative_to(root) or not local.is_relative_to(root):
            raise ValueError('materialized skill must differ from original source')
        if hashlib.sha256(_read(local)).hexdigest() != row['skill_sha256']:
            raise ValueError('materialized skill content changed')
        row.update(native_skill_path=str(local), native_load_name=row['load_name'], archive_path=None)
        if row.get('temperature') != 'cold':
            continue
        name = row['name']
        if (not re.fullmatch(r'[\w.-]{1,160}', name) or names.count(name) != 1
                or (root / name).exists() and root / name != local.parent):
            raise ValueError('cold skill cannot be restored under an ambiguous flat name')
        archive = root / '.archive' / name
        _absolute(archive)
        if archive.exists() and archive != local.parent:
            raise ValueError('cold skill archive already exists')
        archive.parent.mkdir(exist_ok=True)
        if archive != local.parent:
            local.parent.rename(archive)
        row.update(native_skill_path=str(root / name / 'SKILL.md'), native_load_name=name,
                   archive_path=str(archive))
    return value


def restore_review_skill(row, root):
    """Use Hermes' real archive recovery, constrained to this isolated HOME."""
    root = _absolute(root)
    if row.get('archive_path'):
        from tools import skill_usage
        if _absolute(skill_usage._skills_dir()) != root:
            raise ValueError('Hermes archive recovery HOME differs')
        archive = _absolute(row['archive_path'])
        if (archive.parent != root / '.archive'
                or hashlib.sha256(_read(archive / 'SKILL.md')).hexdigest() != row['skill_sha256']):
            raise ValueError('cold archive source differs')
        ok, _message = skill_usage.restore_skill(row['name'])
        if not ok:
            raise ValueError('native Hermes cold skill restoration failed')
    path = _absolute(row['native_skill_path'])
    if (not path.is_relative_to(root)
            or hashlib.sha256(_read(path)).hexdigest() != row['skill_sha256']):
        raise ValueError('native restored skill content differs')
    if row.get('script_sha256'):
        helper = path.parent / 'scripts/xlsx_read.py'
        if hashlib.sha256(_read(helper)).hexdigest() != row['script_sha256']:
            raise ValueError('native restored helper content differs')
    return path


def model_skill(row):
    """Metadata supports selection, and never supplies execution authority."""
    fields = ('snapshot_id', 'skill_id', 'name', 'description', 'tags', 'load_name',
              'skill_sha256', 'script_sha256', 'adapter_status', 'executable',
              'temperature', 'capabilities')
    return {key: copy.deepcopy(row[key]) for key in fields if key in row}
