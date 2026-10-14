"""Bounded task understanding and immutable, source-bound minimal handoffs."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .capability_catalog import _canonical, validate_catalog_source
from .path_binding import source_digest_nofollow
from .skill_catalog import _absolute, validate_skill_binding
from .tasks import plan_from_request

MAX_INTENT_BYTES = 65536
MAX_DEPTH = 12
MAX_STATE_BYTES = 16384
_FIELDS = {'goal', 'capability', 'constraints', 'execution'}
_READY_FIELDS = {'status', 'request', 'catalog_sha256', 'input_sha256', 'workspace',
                 'source_ref', 'session_id', 'catalog_source', 'normalizations', 'state_sha256'}


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identity(value, name):
    if (not isinstance(value, str) or not value.strip() or '\x00' in value
            or len(value.encode('utf-8')) > 512):
        raise ValueError(name + ' must be a bounded nonempty string')


def _bounded(value):
    def visit(item, depth):
        if depth > MAX_DEPTH:
            raise ValueError('intent nesting exceeds 12 levels')
        if type(item) is dict:
            if any(not isinstance(key, str) for key in item):
                raise ValueError('intent object keys must be strings')
            for child in item.values():
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError('intent requires JSON values')
    visit(value, 0)
    if len(_canonical(value)) > MAX_INTENT_BYTES:
        raise ValueError('intent exceeds 64 KiB')


def _path(value, workspace):
    if not isinstance(value, str) or not value or '\x00' in value or '..' in Path(value).parts:
        raise ValueError('execution paths must be strings without traversal or NUL')
    path = Path(value)
    path = _absolute(path if path.is_absolute() else workspace / path)
    if not path.is_relative_to(workspace) or path == workspace:
        raise ValueError('execution paths must be inside workspace')
    return str(path)


def _normal_execution(execution, workspace):
    result = copy.deepcopy(execution)
    for name in ('input_path', 'output_path'):
        if name in result:
            result[name] = _path(result[name], workspace)
    normalizations = []
    if result.get('delimiter') in ('TAB', '\\t'):
        result['delimiter'] = '\t'
        normalizations.append({'field': 'execution.delimiter', 'rule': 'explicit_tab_alias'})
    return result, normalizations


def _failure(status, *, missing=(), errors=(), catalog_sha256=None):
    result = {'status': status, 'missing': list(missing), 'errors': list(errors)}
    if catalog_sha256 is not None:
        result['catalog_sha256'] = catalog_sha256
    return result


def interpret_intent(extracted, *, catalog, workspace, session_id, source_ref, source_facts=None):
    """Validate a model's four-field extraction through the existing real contracts.

    Source facts constrain the extraction; they never fill omitted business fields.
    Only the documented TAB/escaped-tab aliases and workspace-relative paths are
    normalized here. No history, hidden answer or discovery metadata is an oracle.
    """
    try:
        _identity(session_id, 'session_id')
        _identity(source_ref, 'source_ref')
        _bounded(extracted)
        if type(extracted) is not dict or set(extracted) - _FIELDS:
            raise ValueError('extraction must contain only goal, capability, constraints and execution')
        missing = sorted(_FIELDS - set(extracted))
        if 'goal' in extracted and (not isinstance(extracted['goal'], str) or not extracted['goal'].strip()):
            raise ValueError('goal must be a nonempty string')
        capability = extracted.get('capability')
        if capability is not None and (not isinstance(capability, str) or not capability.strip()):
            raise ValueError('capability must be a nonempty string')
        constraints = extracted.get('constraints', [])
        if type(constraints) is not list or any(not isinstance(item, str) for item in constraints):
            raise ValueError('constraints must be an explicit list of strings')
        execution = extracted.get('execution', {})
        if type(execution) is not dict:
            raise ValueError('execution must be an object')
        if source_facts is not None:
            _bounded(source_facts)
            if type(source_facts) is not dict or set(source_facts) - _FIELDS:
                raise ValueError('source facts must use the extraction schema')
            if 'execution' in source_facts and type(source_facts['execution']) is not dict:
                raise ValueError('source execution facts must be an object')
            for name in ('goal', 'capability'):
                if name in source_facts and (not isinstance(source_facts[name], str) or not source_facts[name].strip()):
                    raise ValueError('source ' + name + ' must be a nonempty string')
            if ('constraints' in source_facts and (type(source_facts['constraints']) is not list
                    or any(not isinstance(item, str) for item in source_facts['constraints']))):
                raise ValueError('source constraints must be an explicit list of strings')
        root = _absolute(workspace)
        if not root.is_dir():
            raise ValueError('workspace must be an existing directory')
        execution, normalizations = _normal_execution(execution, root)
        known_result = catalog.resolve(capability) if capability is not None else None
        known = known_result['candidates'][0] if known_result and known_result['candidates'] else None
        if known is not None and set(execution) - set(known['execution_fields']):
            raise ValueError('execution contains unknown parameters or a model-supplied binding')
        if source_facts is not None:
            facts = copy.deepcopy(source_facts)
            if 'execution' in facts:
                facts['execution'], _ = _normal_execution(facts['execution'], root)
                if known is not None and set(facts['execution']) - set(known['execution_fields']):
                    raise ValueError('source execution facts contain unknown parameters')
            conflicts = []
            for name, value in facts.items():
                if name == 'execution':
                    conflicts += ['execution.' + key for key, fact in value.items()
                                  if key in execution and execution[key] != fact]
                elif name in extracted:
                    observed = extracted[name]
                    if (set(observed) != set(value) if name == 'constraints'
                            and type(value) is list and type(observed) is list else observed != value):
                        conflicts.append(name)
            if conflicts:
                return _failure('conflict', errors=['source conflict: ' + ', '.join(sorted(conflicts))])
        if missing:
            return _failure('needs_context', missing=missing)
        if known is None:
            return _failure('unsupported', errors=['capability is not registered'],
                            catalog_sha256=catalog.snapshot_id)
        required = known['required_execution']
        missing = ['execution.' + name for name in required if name not in execution]
        if missing:
            return _failure('needs_context', missing=missing, catalog_sha256=catalog.snapshot_id)
        fmt = Path(execution['input_path']).suffix.lower().lstrip('.')
        resolved = catalog.resolve(capability, fmt, constraints)
        if resolved['status'] != 'ready':
            return _failure(resolved['status'], missing=resolved['missing'],
                errors=['no_adapter: reviewed XLSX binding is unavailable' if capability == 'xlsx.inspect'
                        and not known['executable'] else
                        'capability, input format, constraints or adapter is unavailable'],
                catalog_sha256=resolved['catalog_sha256'])
        entry = resolved['candidates'][0]
        if capability == 'xlsx.inspect':
            execution['skill_binding'] = copy.deepcopy(entry['skill_binding'])
        request = {'schema_version': 2, 'capability': capability, 'contract_version': 1,
                   'goal': extracted['goal'], 'constraints': copy.deepcopy(constraints),
                   'execution': execution, 'source_ref': source_ref, 'session_id': session_id}
        # An event is stable within one real session/source and exact extraction.
        request['event_id'] = 'address-' + _sha(request)
        plan = plan_from_request(request)
        if catalog.snapshot_id != resolved['catalog_sha256']:
            raise ValueError('catalog changed during interpretation')
        result = {'status': 'ready', 'request': request,
                  'catalog_sha256': resolved['catalog_sha256'], 'input_sha256': plan.input_sha256,
                  'workspace': str(root), 'session_id': session_id, 'source_ref': source_ref,
                  'catalog_source': copy.deepcopy(catalog.source), 'normalizations': normalizations}
        result['state_sha256'] = _sha(result)
        if len(_canonical(result)) > MAX_STATE_BYTES:
            raise ValueError('minimal ready state exceeds 16 KiB')
        return result
    except (ValueError, TypeError, KeyError, OSError, RecursionError) as error:
        return _failure('rejected', errors=[str(error)])


def _validate_document(document, *, session_id, catalog_sha256):
    if (type(document) is not dict or set(document) != _READY_FIELDS
            or document.get('status') != 'ready'
            or document.get('state_sha256') != _sha({k: v for k, v in document.items() if k != 'state_sha256'})):
        raise ValueError('minimal state integrity failed')
    if document['session_id'] != session_id:
        raise ValueError('minimal state belongs to another session')
    if document['catalog_sha256'] != catalog_sha256:
        raise ValueError('minimal state catalog changed')
    request = document['request']
    if request.get('session_id') != session_id or request.get('source_ref') != document['source_ref']:
        raise ValueError('minimal state source/session integrity failed')
    root = _absolute(document['workspace'])
    for name in ('input_path', 'output_path'):
        if _path(request['execution'][name], root) != request['execution'][name]:
            raise ValueError('minimal state paths changed')
    budget = 1048576 if request['capability'] == 'json.normalize' else 16 * 1024 * 1024 if request['capability'] == 'xlsx.inspect' else None
    if source_digest_nofollow(request['execution']['input_path'], budget) != document['input_sha256']:
        raise ValueError('minimal state source input changed')
    source = document['catalog_source']
    if source is not None:
        validate_catalog_source(source)
    if request['capability'] == 'xlsx.inspect':
        validate_skill_binding(request['execution']['skill_binding'])


class MinimalStateStore:
    """Content-addressed immutable requests; only the five-field handoff is public."""
    def __init__(self, db_path):
        self.db_path = _absolute(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS rpe_minimal_states '
                       '(state_id TEXT PRIMARY KEY, document BLOB NOT NULL)')

    def _connect(self):
        _absolute(self.db_path)
        return sqlite3.connect(self.db_path, timeout=10)

    @staticmethod
    def _handoff(state_id, document):
        return {'state_id': state_id, 'capability': document['request']['capability'],
                'constraints': copy.deepcopy(document['request']['constraints']),
                'catalog_sha256': document['catalog_sha256'], 'status': 'ready'}

    def capture(self, result, *, session_id, source_ref):
        _identity(session_id, 'session_id')
        _identity(source_ref, 'source_ref')
        document = copy.deepcopy(result)
        _bounded(document)
        if len(_canonical(document)) > MAX_STATE_BYTES:
            raise ValueError('minimal ready state exceeds 16 KiB')
        _validate_document(document, session_id=session_id,
                           catalog_sha256=document.get('catalog_sha256') if type(document) is dict else None)
        if document['source_ref'] != source_ref:
            raise ValueError('minimal state source conflict')
        # Capture cannot accept a caller's invented shape or bypass a real probe.
        if plan_from_request(document['request']).input_sha256 != document['input_sha256']:
            raise ValueError('minimal state source input changed')
        raw = _canonical(document)
        state_id = hashlib.sha256(raw).hexdigest()
        with self._connect() as db:
            db.execute('INSERT OR IGNORE INTO rpe_minimal_states VALUES (?,?)', (state_id, raw))
            saved = bytes(db.execute('SELECT document FROM rpe_minimal_states WHERE state_id=?', (state_id,)).fetchone()[0])
        if saved != raw:
            raise ValueError('minimal state integrity collision')
        return self._handoff(state_id, document)

    def _get(self, state_id):
        if not isinstance(state_id, str) or not re.fullmatch('[a-f0-9]{64}', state_id):
            raise ValueError('state_id must be SHA256')
        with self._connect() as db:
            row = db.execute('SELECT document FROM rpe_minimal_states WHERE state_id=?', (state_id,)).fetchone()
        if row is None:
            raise ValueError('minimal state not found')
        raw = row[0].encode('utf-8') if isinstance(row[0], str) else bytes(row[0])
        if hashlib.sha256(raw).hexdigest() != state_id:
            raise ValueError('minimal state integrity failed')
        return json.loads(raw)

    def show(self, state_id, *, session_id):
        """Inspect a handoff without probing or executing business files."""
        document = self._get(state_id)
        if document['session_id'] != session_id:
            raise ValueError('minimal state belongs to another session')
        return self._handoff(state_id, document)

    def resolve(self, state_id, *, session_id, catalog_sha256):
        document = self._get(state_id)
        _validate_document(document, session_id=session_id, catalog_sha256=catalog_sha256)
        return copy.deepcopy(document)
