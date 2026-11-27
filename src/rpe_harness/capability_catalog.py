"""One indexed capability directory; discovery rows never register executors."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3

from . import contracts, json_contract, skill_inspection
from .contract_sdk import DEFAULT_REGISTRY, _callable_revision
from .skill_catalog import SkillCatalog, validate_skill_binding, XLSX_SKILL_ID, _absolute


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _definitions():
    return [
        {'entry_id': 'tabular.deduplicate/v1', 'capability': 'tabular.deduplicate',
         'description': 'Remove duplicate rows by explicit key columns; keep first and retain order.',
         'input_formats': ['csv', 'xlsx'], 'constraints': sorted(contracts.CONSTRAINTS),
         'required_execution': ['input_path', 'output_path', 'key_columns'],
         'execution_fields': sorted(contracts._OPTIONS), 'contract_version': 1,
         'executor_ids': dict(contracts.EXECUTORS), 'adapter_status': 'builtin', 'executable': True},
        {'entry_id': 'json.normalize/v1', 'capability': 'json.normalize',
         'description': 'Normalize JSON whitespace with minify or pretty style; preserve values and order.',
         'input_formats': ['json'], 'constraints': sorted(json_contract.CONSTRAINTS),
         'required_execution': ['input_path', 'output_path', 'style'],
         'execution_fields': ['input_path', 'output_path', 'style'], 'contract_version': 1,
         'executor_ids': {'json': json_contract.EXECUTOR_ID}, 'adapter_status': 'builtin', 'executable': True},
        {'entry_id': 'xlsx.inspect/v1', 'capability': 'xlsx.inspect',
         'description': 'Inspect workbook sheets, rows or formulas using the reviewed XLSX skill adapter.',
         'input_formats': ['xlsx'], 'constraints': ['preserve_original'],
         'required_execution': ['input_path', 'output_path', 'mode'],
         'execution_fields': ['input_path', 'output_path', 'mode', 'sheet'], 'contract_version': 1,
         'executor_ids': {'xlsx': skill_inspection.EXECUTOR_ID}, 'adapter_status': 'unbound',
         'executable': False},
    ]


def _plugin_definitions():
    entries = []
    reserved = {entry['capability'] for entry in _definitions()}
    # Only registered Python plugins grant execution. Discovery skill rows
    # cannot introduce validators, options, or executable capabilities.
    for plugin in DEFAULT_REGISTRY.list_plugins():
        if plugin.capability in reserved:
            continue
        entries.append({**plugin.metadata(),
            'entry_id': f'{plugin.capability}/v{plugin.contract_version}',
            'required_execution': ['input_path', 'output_path'],
            'execution_fields': ['input_path', 'output_path'],
            'plugin_options': True,
            'executor_ids': dict.fromkeys(plugin.input_formats, plugin.executor_id),
            'executor_revision': plugin.executor_revision,
            'verifier_revision': plugin.verifier_revision,
            'validator_revision': _callable_revision(plugin.validate_options, plugin.capability),
            'adapter_status': 'plugin', 'executable': True})
    return entries


class CapabilityCatalog:
    """Trusted builtins plus explicit local skill rows, with exact capability/format indexes.

    Rows supplied by the parent are discovery evidence, never executable code.
    Only the pinned XLSX binding can activate its already registered adapter.
    ``build_catalog`` additionally refreshes the current snapshot and trust DB.
    """
    def __init__(self, skill_rows=()):
        rows = list(skill_rows)
        if len(rows) > 2048 or any(type(row) is not dict for row in rows):
            raise ValueError('catalog requires at most 2048 skill row objects')
        if len(_canonical(rows)) > 8 * 1024 * 1024:
            raise ValueError('skill rows exceed catalog budget')
        self._rows = copy.deepcopy(rows)
        self._row_loader = None
        self.source = None
        self._stale = False
        self._build(rows)

    def _build(self, rows):
        entries = _definitions() + _plugin_definitions()
        inspection = entries[2]
        for raw in sorted(rows, key=lambda row: str(row.get('skill_id', ''))):
            enabled = raw.get('enabled', True) is True and raw.get('status') == 'available'
            trusted = False
            if (enabled and raw.get('skill_id') == XLSX_SKILL_ID
                    and raw.get('adapter_status') == 'trusted' and raw.get('executable') is True
                    and not raw.get('dynamic')):
                try:
                    validate_skill_binding(raw.get('skill_binding'))
                    trusted = True
                except (ValueError, OSError, KeyError, TypeError):
                    pass
            if trusted:
                inspection.update(adapter_status='trusted', executable=True,
                                  skill_binding=copy.deepcopy(raw['skill_binding']))
            # The external item remains discoverable, including cold/no-adapter items.
            entries.append({'entry_id': 'skill/' + str(raw.get('skill_id', 'unknown')),
                'skill_id': raw.get('skill_id'), 'capability': None,
                'description': raw.get('description', ''), 'name': raw.get('name', ''),
                'capabilities': list(raw.get('capabilities', [])),
                'input_formats': list(raw.get('input_formats', [])),
                'temperature': raw.get('temperature', 'warm'), 'status': raw.get('status', 'unavailable'),
                'adapter_status': 'trusted' if trusted else 'discovery_only', 'executable': False,
                'skill_sha256': raw.get('skill_sha256'), 'snapshot_id': raw.get('snapshot_id')})
        self._entries = entries
        self._by_capability = {entry['capability']: entry for entry in entries if entry['capability']}
        self._by_format = {}
        for entry in self._by_capability.values():
            for fmt in entry['input_formats']:
                self._by_format.setdefault(fmt, []).append(entry)
        revisions = {'tabular': [contracts.EXECUTOR_REVISION, contracts.VERIFIER_REVISION],
                     'json': json_contract.current_revisions(), 'xlsx': skill_inspection.current_revisions()}
        self._base_snapshot_id = hashlib.sha256(_canonical({'entries': entries, 'revisions': revisions,
                                                           'source': self.source})).hexdigest()
        self._snapshot_id = self._base_snapshot_id

    def _check_current(self):
        # A constant-size pointer/trust check, never a sweep of descriptions or
        # skill bodies. Changed directories require an explicit reload.
        if self.source is not None:
            try:
                live = _authority_state(self.source)
            except (ValueError, OSError, sqlite3.Error):
                live = {'unavailable': True}
            expected = {key: self.source[key] for key in ('current_snapshot_id', 'trusted')}
            if live != expected:
                self._stale = True
            if self._stale:
                self._snapshot_id = hashlib.sha256(_canonical(
                    {'previous': self._base_snapshot_id, 'current': live, 'stale': True})).hexdigest()

    def reload(self):
        """Explicitly rebuild immutable discovery entries and exact indexes."""
        if self.source is not None:
            self.source.update(_authority_state(self.source))
        rows = self._row_loader() if self._row_loader else self._rows
        self._stale = False
        self._build(rows)
        return self

    @property
    def snapshot_id(self):
        self._check_current()
        return self._snapshot_id

    def entries(self):
        self._check_current()
        result = copy.deepcopy(self._entries)
        if self._stale:
            for entry in result:
                entry.update(executable=False, adapter_status='stale_catalog')
        return result

    def resolve(self, capability=None, input_format=None, constraints=()):
        if (capability is not None and (not isinstance(capability, str) or not capability.strip())
                or input_format is not None and not isinstance(input_format, str)
                or not isinstance(constraints, (tuple, list))
                or any(not isinstance(item, str) for item in constraints)):
            raise ValueError('capability, input format and constraints have invalid types')
        self._check_current()
        missing = []
        if capability is not None:
            entry = self._by_capability.get(capability)
            selected = [entry] if entry is not None else []
        elif input_format is not None:
            selected = self._by_format.get(input_format, [])
        else:
            selected, missing = [], ['capability']
        # Only an address to the registered XLSX adapter probes its current bytes.
        if any(entry['capability'] == 'xlsx.inspect' and entry['executable'] for entry in selected):
            entry = self._by_capability['xlsx.inspect']
            try:
                validate_skill_binding(entry['skill_binding'])
            except (ValueError, OSError, KeyError, TypeError):
                entry.update(executable=False, adapter_status='no_adapter')
                entry.pop('skill_binding', None)
                self._snapshot_id = hashlib.sha256(_canonical(
                    {'previous': self._base_snapshot_id, 'adapter': 'unavailable'})).hexdigest()
        selected = [entry for entry in selected if
            (input_format is None or input_format in entry['input_formats'])
            and not set(constraints) - set(entry['constraints'])
            and not (input_format == 'csv' and 'preserve_formulas' in constraints)]
        status = ('needs_context' if missing else 'unsupported' if self._stale or not selected else
                  'ambiguous' if len(selected) > 1 else 'ready' if selected[0]['executable'] else 'unsupported')
        return {'status': status, 'candidates': copy.deepcopy(selected),
                'catalog_sha256': self._snapshot_id, 'missing': missing}


def _authority_state(source):
    """Read only the current directory pointer and its one registered trust bit."""
    path = _absolute(source['db_path'])
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10) as db:
        row = db.execute('SELECT snapshot_id FROM rpe_skill_current WHERE singleton=1').fetchone()
        current = row[0] if row else None
        selected = source.get('skill_snapshot_id') or current
        trusted = bool(db.execute('SELECT 1 FROM rpe_skill_trust WHERE snapshot_id=? AND skill_id=?',
                                  (selected, XLSX_SKILL_ID)).fetchone())
    return {'current_snapshot_id': current, 'trusted': trusted}


def validate_catalog_source(source):
    """Revalidate a durable catalog anchor without rebuilding unrelated skills."""
    if (type(source) is not dict or set(source) != {'db_path', 'catalog_root', 'skill_snapshot_id',
                                                 'current_snapshot_id', 'trusted'}
            or _authority_state(source) != {key: source[key] for key in ('current_snapshot_id', 'trusted')}):
        raise ValueError('catalog snapshot or adapter trust changed')


def build_catalog(db_path=None, catalog_root=None, skill_snapshot_id=None):
    """Shared CLI/HTTP/Hermes source; old snapshots cannot retain current authorization."""
    if (db_path is None) != (catalog_root is None):
        raise ValueError('skill catalog database and catalog root must be supplied together')
    catalog = CapabilityCatalog()
    if db_path is None:
        if skill_snapshot_id is not None:
            raise ValueError('skill snapshot requires an explicit catalog source')
        return catalog
    skills = SkillCatalog(db_path, catalog_root)
    catalog.source = {'db_path': str(skills.db_path), 'catalog_root': str(skills.owned_root),
                      'skill_snapshot_id': skill_snapshot_id}
    catalog.source.update(_authority_state(catalog.source))

    def current_rows():
        current_id, document = skills._document()
        requested = skill_snapshot_id or current_id
        if requested is None:
            return []
        if requested != current_id:
            _, document = skills._document(requested)
        result = []
        for raw in document['entries']:
            row = skills.lookup(raw['skill_id'], snapshot_id=requested)
            if requested != current_id:
                row.update(status='stale', adapter_status='untrusted', executable=False)
                row.pop('skill_binding', None)
            result.append(row)
        return result

    catalog._row_loader = current_rows
    catalog.reload()
    return catalog
