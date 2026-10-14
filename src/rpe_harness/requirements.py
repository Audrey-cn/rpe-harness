"""Complete coverage of a bounded, explicit current-request JSON vocabulary.

The original source is authoritative. Model extraction and model-authored coverage
never establish completeness. Free prose remains an unverified proposal; only the
entire strict source object can produce a source-bound executable handoff.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3

from .capability_catalog import _canonical, _definitions
from .intent import (MAX_INTENT_BYTES, MinimalStateStore, _bounded, _failure,
                     _identity, _normal_execution, interpret_intent)
from .skill_catalog import _absolute

MAX_SOURCE_BYTES = MAX_INTENT_BYTES
MAX_SOURCE_DOCUMENT_BYTES = 192 * 1024
_SOURCE_SCHEMA = 'rpe.current-source/v1'
_STATE_SCHEMA = 'rpe.source-state/v1'
_SOURCE_FIELDS = {'capability', 'constraints', 'execution'}


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate source JSON keys are not allowed')
        value[key] = item
    return value


def _nonfinite(value):
    raise ValueError('source JSON numbers must be finite')


def _ledger(source, requirements):
    return _sha({'source': source, 'requirements': requirements})


def _source_failure(status, source=None, *, missing=(), errors=()):
    result = _failure(status, missing=missing, errors=errors)
    if source is not None:
        result.update(source=source, requirements=[], requirements_sha256=_ledger(source, []))
    return result


def _execution_types(execution):
    for name, value in execution.items():
        if name == 'key_columns':
            if (type(value) is not list or not value
                    or any(not isinstance(item, str) or not item.strip() for item in value)
                    or len(set(value)) != len(value)):
                raise ValueError('source execution.key_columns must be unique nonempty strings')
        elif not isinstance(value, str) or not value or '\x00' in value:
            raise ValueError('source execution.' + name + ' must be a nonempty string')


def source_document(raw, *, session_id, source_ref):
    """Parse the *whole* current source and derive a traceable requirement ledger.

    JSON pointers identify values in the exact SHA-bound original. Duplicate keys
    are forbidden, so each pointer is unambiguous. Array-valued execution options
    are atomic requirements; constraints also receive individual value anchors.
    This proves coverage of this grammar, not arbitrary natural-language meaning.
    """
    source = None
    try:
        _identity(session_id, 'session_id')
        _identity(source_ref, 'source_ref')
        if not isinstance(raw, str):
            raise ValueError('current source must be UTF-8 text')
        encoded = raw.encode('utf-8')
        if len(encoded) > MAX_SOURCE_BYTES:
            raise ValueError('current source exceeds 64 KiB')
        source = {'schema_version': _SOURCE_SCHEMA, 'raw': raw,
                  'source_sha256': hashlib.sha256(encoded).hexdigest(),
                  'session_id': session_id, 'source_ref': source_ref}
        try:
            value = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_nonfinite)
        except json.JSONDecodeError:
            if not raw.lstrip().startswith(('{', '[')):
                return _source_failure('needs_context', source,
                    missing=['structured_current_request'],
                    errors=['semantic completeness unverified for free text'])
            raise ValueError('current source must be one strict JSON object without trailing text')
        _bounded(value)
        if type(value) is not dict:
            raise ValueError('current source must be a JSON object')
        unknown = set(value) - _SOURCE_FIELDS
        if unknown:
            return _source_failure('unsupported', source,
                errors=['unregistered source fields: ' + ', '.join(sorted(unknown))])
        missing = sorted(_SOURCE_FIELDS - set(value))
        if missing:
            return _source_failure('needs_context', source, missing=missing)
        capability, constraints, execution = (value[name] for name in
                                              ('capability', 'constraints', 'execution'))
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError('source capability must be a nonempty string')
        if type(constraints) is not list or any(not isinstance(item, str) for item in constraints):
            raise ValueError('source constraints must be an explicit list of strings')
        if type(execution) is not dict:
            raise ValueError('source execution must be an object')
        definition = next((entry for entry in _definitions() if entry['capability'] == capability), None)
        if definition is None:
            return _source_failure('unsupported', source, errors=['source capability is not registered'])
        if set(constraints) - set(definition['constraints']):
            return _source_failure('unsupported', source, errors=['source constraints are not registered'])
        if set(execution) - set(definition['execution_fields']):
            return _source_failure('unsupported', source,
                errors=['source execution contains unregistered requirements'])
        _execution_types(execution)
        missing = ['execution.' + field for field in definition['required_execution']
                   if field not in execution]
        if missing:
            return _source_failure('needs_context', source, missing=missing)
        if (execution.get('keep', 'first') != 'first'
                or 'style' in execution and execution['style'] not in {'minify', 'pretty'}
                or 'mode' in execution and execution['mode'] not in {'sheets', 'rows', 'formulas'}):
            return _source_failure('unsupported', source, errors=['source option value is not registered'])
        rows = []

        def require(pointer, item, field):
            rows.append({'requirement_id': 'r' + str(len(rows) + 1).zfill(4),
                         'pointer': pointer, 'value': copy.deepcopy(item),
                         'value_sha256': _sha(item), 'source_sha256': source['source_sha256'],
                         'acceptance_field': field})

        require('/capability', capability, 'capability')
        require('/constraints', constraints, 'constraints')
        for index, constraint in enumerate(constraints):
            require('/constraints/' + str(index), constraint, 'constraints')
        for name in sorted(execution):
            require('/execution/' + name, execution[name], 'execution.' + name)
        result = {'status': 'ready', 'source': source, 'requirements': rows,
                  'requirements_sha256': _ledger(source, rows),
                  'source_facts': {'goal': 'Execute ' + capability, **copy.deepcopy(value)}}
        if len(_canonical(result)) > MAX_SOURCE_DOCUMENT_BYTES:
            raise ValueError('source document and ledger exceed 192 KiB')
        return result
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        return _source_failure('rejected', source, errors=[str(error)])


def _validate_source_document(document, *, session_id, source_ref):
    if type(document) is not dict or type(document.get('source')) is not dict:
        raise ValueError('source document integrity failed')
    saved = document['source']
    if saved.get('session_id') != session_id or saved.get('source_ref') != source_ref:
        raise ValueError('source document belongs to another session or source')
    regenerated = source_document(saved.get('raw'), session_id=session_id, source_ref=source_ref)
    if regenerated != document:
        raise ValueError('source document or requirement ledger integrity failed')
    return regenerated


def _match_fields(extracted, source, workspace):
    """No hidden goal, extra option, guessed value or dropped explicit default."""
    _bounded(extracted)
    if type(extracted) is not dict or set(extracted) - {'goal', *_SOURCE_FIELDS}:
        return _failure('rejected', errors=['extraction contains unregistered fields'])
    facts = source['source_facts']
    missing = sorted(set(facts) - set(extracted))
    if missing:
        return _failure('needs_context', missing=missing)
    if type(extracted['execution']) is not dict:
        return _failure('rejected', errors=['execution must be an object'])
    expected, _ = _normal_execution(facts['execution'], _absolute(workspace))
    observed, _ = _normal_execution(extracted['execution'], _absolute(workspace))
    if set(observed) - set(expected):
        return _failure('rejected', errors=['extraction adds execution fields absent from current source'])
    missing = ['execution.' + key for key in expected if key not in observed]
    conflicts = [name for name in ('goal', 'capability', 'constraints')
                 if extracted[name] != facts[name]]
    conflicts += ['execution.' + key for key in expected
                  if key in observed and observed[key] != expected[key]]
    if conflicts:
        return _failure('conflict', errors=['source conflict: ' + ', '.join(sorted(conflicts))])
    if missing:
        return _failure('needs_context', missing=missing)
    return None


def interpret_source_intent(extracted, *, source_document, catalog, workspace,
                            session_id, source_ref):
    """Validate extraction against every requirement in the immutable source."""
    try:
        source = _validate_source_document(source_document, session_id=session_id, source_ref=source_ref)
        if source['status'] != 'ready':
            return copy.deepcopy(source)
        failure = _match_fields(extracted, source, workspace)
        if failure is not None:
            return failure
        return interpret_intent(extracted, catalog=catalog, workspace=workspace,
            session_id=session_id, source_ref=source_ref, source_facts=source['source_facts'])
    except (ValueError, TypeError, KeyError, OSError, RecursionError) as error:
        return _failure('rejected', errors=[str(error)])


def _match_request(document, source):
    try:
        request = document['request']
        extracted = {field: copy.deepcopy(request[field]) for field in ('goal', *_SOURCE_FIELDS)}
        if request['capability'] == 'xlsx.inspect':
            extracted['execution'].pop('skill_binding', None)
        failure = _match_fields(extracted, source, document['workspace'])
        if (failure is not None or request['session_id'] != source['source']['session_id']
                or request['source_ref'] != source['source']['source_ref']):
            raise ValueError('minimal request does not match the complete source requirements')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('source-bound minimal request integrity failed') from error


class SourceStateStore:
    """Immutable source ledgers linked to unchanged v1.8 minimal states.

    Public clean state IDs hash both the source ledger and underlying minimal ID.
    Equal normalized requests with different original bytes cannot share a clean
    state identity. Hashes detect altered records, not independent authorship.
    """
    def __init__(self, db_path):
        self.db_path = _absolute(db_path)
        self.minimal = MinimalStateStore(self.db_path)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS rpe_source_states '
                       '(state_id TEXT PRIMARY KEY, document BLOB NOT NULL)')

    def _connect(self):
        _absolute(self.db_path)
        return sqlite3.connect(self.db_path, timeout=10)

    @staticmethod
    def _handoff(state_id, base, source):
        return {**base, 'state_id': state_id,
                'requirements_sha256': source['requirements_sha256']}

    def capture(self, source, result, *, session_id, source_ref):
        source = _validate_source_document(copy.deepcopy(source),
                                           session_id=session_id, source_ref=source_ref)
        if source['status'] != 'ready':
            raise ValueError('only complete ready source requirements can be captured')
        result = copy.deepcopy(result)
        _match_request(result, source)
        base = self.minimal.capture(result, session_id=session_id, source_ref=source_ref)
        binding = {'schema_version': _STATE_SCHEMA, 'minimal_state_id': base['state_id'],
                   'source_document': source}
        raw = _canonical(binding)
        state_id = hashlib.sha256(raw).hexdigest()
        with self._connect() as db:
            db.execute('INSERT OR IGNORE INTO rpe_source_states VALUES (?,?)', (state_id, raw))
            saved = bytes(db.execute('SELECT document FROM rpe_source_states WHERE state_id=?',
                                     (state_id,)).fetchone()[0])
        if saved != raw:
            raise ValueError('source state integrity collision')
        return self._handoff(state_id, base, source)

    def _get(self, state_id, *, session_id):
        if not isinstance(state_id, str) or not re.fullmatch('[a-f0-9]{64}', state_id):
            raise ValueError('state_id must be SHA256')
        with self._connect() as db:
            row = db.execute('SELECT document FROM rpe_source_states WHERE state_id=?', (state_id,)).fetchone()
        if row is None:
            raise ValueError('source state not found')
        raw = row[0].encode('utf-8') if isinstance(row[0], str) else bytes(row[0])
        if len(raw) > MAX_SOURCE_DOCUMENT_BYTES + 512 or hashlib.sha256(raw).hexdigest() != state_id:
            raise ValueError('source state integrity failed')
        binding = json.loads(raw)
        if (type(binding) is not dict
                or set(binding) != {'schema_version', 'minimal_state_id', 'source_document'}
                or binding['schema_version'] != _STATE_SCHEMA):
            raise ValueError('source state integrity failed')
        source = binding['source_document']
        if type(source) is not dict or type(source.get('source')) is not dict:
            raise ValueError('source document integrity failed')
        _validate_source_document(source, session_id=session_id,
                                  source_ref=source['source'].get('source_ref'))
        if source['status'] != 'ready':
            raise ValueError('source state requires complete ready requirements')
        return binding

    def show(self, state_id, *, session_id):
        """Verify the source ledger and inspect without probing business files."""
        binding = self._get(state_id, session_id=session_id)
        document = self.minimal._get(binding['minimal_state_id'])
        source = binding['source_document']
        _match_request(document, source)
        base = self.minimal.show(binding['minimal_state_id'], session_id=session_id)
        return self._handoff(state_id, base, source)

    def resolve(self, state_id, *, session_id, catalog_sha256):
        binding = self._get(state_id, session_id=session_id)
        try:
            document = self.minimal.resolve(binding['minimal_state_id'], session_id=session_id,
                                            catalog_sha256=catalog_sha256)
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError('source-bound minimal state integrity failed') from error
        source = binding['source_document']
        _match_request(document, source)
        return {**document, 'source': copy.deepcopy(source['source']),
                'requirements': copy.deepcopy(source['requirements']),
                'requirements_sha256': source['requirements_sha256']}
