"""Bounded, source-backed context recovery. Stored text is evidence, never instructions.

This module supplies required facts, not execution authority. Callers must bind the
session from their trusted host and revalidate recovered values through the exact
execution contract, including any existing frozen-plan anchor.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

_SOURCE_LIMIT = 256 * 1024
_FACT_LIMIT = 16 * 1024
_MAX_DEPTH = 16
_KINDS = frozenset({'string', 'string_list', 'integer', 'number', 'boolean', 'object'})


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'{name} must be a nonempty string without NUL')
    if len(value.encode('utf-8')) > 1024:
        raise ValueError(f'{name} exceeds 1024 bytes')
    return value


def _json(value, limit):
    """Reject non-JSON values and deep trees before recursive serialization."""
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _SOURCE_LIMIT:
            raise ValueError('JSON nesting or node budget exceeded')
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError('JSON object keys must be strings')
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise ValueError('value must contain only JSON values')
        elif type(item) is float and not math.isfinite(item):
            raise ValueError('JSON numbers must be finite')
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                         allow_nan=False)
        if len(raw.encode('utf-8')) > limit:
            raise ValueError('JSON byte budget exceeded')
    except (TypeError, OverflowError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid JSON content') from exc
    return raw


def _digest(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class SourceRef:
    source: str
    event_id: str
    sha256: str

    def __post_init__(self):
        _text(self.source, 'source')
        _text(self.event_id, 'event_id')
        if not isinstance(self.sha256, str) or not re.fullmatch('[0-9a-f]{64}', self.sha256):
            raise ValueError('sha256 must be a full lowercase SHA256 digest')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {'source', 'event_id', 'sha256'}:
            raise ValueError('source ref must contain source, event_id, sha256 only')
        return cls(**value)


def _ref(value):
    if isinstance(value, SourceRef):
        return value
    return SourceRef.from_dict(value)


@dataclass(frozen=True)
class FactRequirement:
    name: str
    kind: str
    max_age_seconds: float | None = None

    def __post_init__(self):
        _text(self.name, 'fact name')
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            raise ValueError('unsupported fact kind')
        age = self.max_age_seconds
        if age is not None:
            try:
                valid_age = type(age) in (int, float) and math.isfinite(age) and age > 0
            except OverflowError:
                valid_age = False
            if not valid_age:
                raise ValueError('max_age_seconds must be a finite positive number')


@dataclass(frozen=True)
class CapabilityContext:
    capability: str
    version: int
    requirements: tuple[FactRequirement, ...]

    def __post_init__(self):
        _text(self.capability, 'capability')
        if type(self.version) is not int or self.version < 1:
            raise ValueError('requirement version must be a positive integer')
        if not isinstance(self.requirements, (tuple, list)) or not 1 <= len(self.requirements) <= 32:
            raise ValueError('one to 32 requirements are supported')
        if any(not isinstance(item, FactRequirement) for item in self.requirements):
            raise ValueError('requirements must be FactRequirement objects')
        if len({item.name for item in self.requirements}) != len(self.requirements):
            raise ValueError('required fact names must be unique')
        object.__setattr__(self, 'requirements', tuple(self.requirements))


def requirements_for(capability: str) -> CapabilityContext:
    """Trusted explicit definitions; discovery metadata cannot register requirements."""
    fields = {
        'tabular.deduplicate': ('key_columns', 'string_list'),
        'json.normalize': ('style', 'string'),
        'xlsx.inspect': ('mode', 'string'),
    }
    if not isinstance(capability, str) or capability not in fields:
        raise ValueError('unsupported context capability')
    name, kind = fields[capability]
    return CapabilityContext(capability, 1, (FactRequirement('input_path', 'string'),
                                            FactRequirement('output_path', 'string'),
                                            FactRequirement(name, kind)))


def _valid_fact(value, requirement, capability):
    kind = requirement.kind
    valid = {
        'string': lambda: type(value) is str and bool(value.strip()) and '\x00' not in value,
        'string_list': lambda: type(value) is list and bool(value) and all(
            type(item) is str and bool(item.strip()) and '\x00' not in item for item in value)
            and len(value) == len(set(value)),
        'integer': lambda: type(value) is int,
        'number': lambda: type(value) is int or type(value) is float and math.isfinite(value),
        'boolean': lambda: type(value) is bool,
        'object': lambda: type(value) is dict,
    }[kind]()
    if capability == 'json.normalize' and requirement.name == 'style':
        valid = valid and value in ('pretty', 'minify')
    if capability == 'xlsx.inspect' and requirement.name == 'mode':
        valid = valid and value in ('rows', 'sheets', 'formulas')
    return valid


class _SourceProblem(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ContextStore:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS context_sources (
                    source TEXT NOT NULL, event_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
                    facts_json TEXT NOT NULL, received_at REAL NOT NULL,
                    PRIMARY KEY(source,event_id));
                CREATE TABLE IF NOT EXISTS context_recoveries (
                    recovery_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    capability TEXT NOT NULL, requirement_version INTEGER NOT NULL,
                    request_hash TEXT NOT NULL, request_json TEXT NOT NULL,
                    result_hash TEXT NOT NULL, result_json TEXT NOT NULL, created_at REAL NOT NULL);
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _source_content(row):
        try:
            payload = json.loads(row['payload_json'])
            facts = json.loads(row['facts_json'])
            if type(payload) is not dict or type(facts) is not dict:
                raise ValueError('source content must be objects')
            _json(facts, _FACT_LIMIT)
            received_at = row['received_at']
            if type(received_at) not in (int, float) or not math.isfinite(received_at) or received_at <= 0:
                raise ValueError('invalid receive time')
            content = {'source': row['source'], 'event_id': row['event_id'],
                       'session_id': row['session_id'], 'payload': payload,
                       'facts': facts, 'received_at': received_at}
            raw = _json(content, _SOURCE_LIMIT)
            if _digest(raw) != row['payload_hash']:
                raise ValueError('source content changed')
            return content
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise _SourceProblem('hash_mismatch') from exc

    def capture(self, *, source, event_id, session_id, payload, facts) -> SourceRef:
        """Own and persist explicit facts; hash binds body, facts, session and receive time."""
        _text(source, 'source')
        _text(event_id, 'event_id')
        _text(session_id, 'session_id')
        if type(payload) is not dict or type(facts) is not dict:
            raise ValueError('payload and facts must be JSON objects')
        # Own both dictionaries before admission and persist exactly those bytes.
        payload_json = _json(payload, _SOURCE_LIMIT)
        facts_json = _json(facts, _FACT_LIMIT)
        content = {'source': source, 'event_id': event_id, 'session_id': session_id,
                   'payload': json.loads(payload_json), 'facts': json.loads(facts_json),
                   'received_at': time.time()}
        raw = _json(content, _SOURCE_LIMIT)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM context_sources WHERE source=? AND event_id=?',
                             (source, event_id)).fetchone()
            if old is not None:
                previous = self._source_content(old)
                if (previous['session_id'] != session_id or
                        _json(previous['payload'], _SOURCE_LIMIT) != payload_json or
                        _json(previous['facts'], _FACT_LIMIT) != facts_json):
                    raise ValueError('source identity already exists with different content or session')
                return SourceRef(source, event_id, old['payload_hash'])
            digest = _digest(raw)
            db.execute('INSERT INTO context_sources VALUES(?,?,?,?,?,?,?)',
                       (source, event_id, session_id, digest, payload_json, facts_json,
                        content['received_at']))
        return SourceRef(source, event_id, digest)

    def resolve(self, ref: SourceRef | dict, *, session_id) -> dict:
        """Resolve an explicitly named source in this session after verifying its full hash."""
        ref = _ref(ref)
        _text(session_id, 'session_id')
        with self._connect() as db:
            row = db.execute('SELECT * FROM context_sources WHERE source=? AND event_id=?',
                             (ref.source, ref.event_id)).fetchone()
        if row is None:
            raise _SourceProblem('source_missing')
        if row['session_id'] != session_id:
            raise _SourceProblem('session_mismatch')
        if row['payload_hash'] != ref.sha256:
            raise _SourceProblem('hash_mismatch')
        content = self._source_content(row)
        content['sha256'] = ref.sha256
        return content

    def recover(self, *, requirement: CapabilityContext, session_id, facts, source_refs) -> dict:
        """Recover only declared required fields, preserving conflict and source evidence."""
        if not isinstance(requirement, CapabilityContext):
            raise ValueError('requirement must be CapabilityContext')
        _text(session_id, 'session_id')
        # Nothing from malformed or oversized requests is echoed into a durable response.
        result = {'status': 'ready', 'facts': {}, 'missing': [], 'conflicts': [], 'issues': [],
                  'provenance': {}, 'recovered_fields': [], 'source_refs': [],
                  'session_id': session_id, 'capability': requirement.capability,
                  'requirement_version': requirement.version}
        request = {'session_id': session_id, 'requirement': {
            'capability': requirement.capability, 'version': requirement.version,
            'requirements': [asdict(item) for item in requirement.requirements]}}
        try:
            if type(facts) is not dict or type(source_refs) is not list:
                raise ValueError('facts must be an object and source_refs an array')
            facts = json.loads(_json(facts, _FACT_LIMIT))
            if len(source_refs) > 8:
                raise ValueError('at most eight source references are allowed')
            refs = [_ref(item) for item in source_refs]
            request.update(facts=facts, source_refs=[ref.to_dict() for ref in refs])
            result['source_refs'] = request['source_refs']
        except (ValueError, TypeError, UnicodeError) as exc:
            result['status'] = 'rejected'
            result['issues'] = [{'code': 'limit_exceeded' if 'budget' in str(exc) or
                                 'eight' in str(exc) else 'invalid_request'}]
            return self._save_recovery(request, result)

        sources = []
        fatal = False
        incomplete = False
        now = time.time()
        for ref in refs:
            try:
                source = self.resolve(ref, session_id=session_id)
                if source['received_at'] > now:
                    raise _SourceProblem('source_future')
                sources.append((ref, source))
            except _SourceProblem as exc:
                result['issues'].append({'code': exc.code, 'source_ref': ref.to_dict()})
                fatal |= exc.code != 'source_missing'
                incomplete |= exc.code == 'source_missing'

        # Invalid source sets fail closed before restoring any field.
        if fatal:
            result['status'] = 'rejected'
            result['missing'] = [item.name for item in requirement.requirements]
            return self._save_recovery(request, result)

        for item in requirement.requirements:
            candidates = []
            if item.name in facts:
                candidates.append((facts[item.name], None))
            for ref, source in sources:
                if item.name not in source['facts']:
                    continue
                if item.max_age_seconds is not None and now - source['received_at'] > item.max_age_seconds:
                    result['issues'].append({'code': 'source_expired', 'field': item.name,
                                             'source_ref': ref.to_dict()})
                    incomplete = True
                    continue
                candidates.append((source['facts'][item.name], ref))
            invalid = any(not _valid_fact(value, item, requirement.capability) for value, _ in candidates)
            if invalid:
                fatal = True
                result['issues'].append({'code': 'invalid_fact', 'field': item.name})
                result['missing'].append(item.name)
                continue
            if not candidates:
                result['missing'].append(item.name)
                continue
            identities = {_json(value, _FACT_LIMIT) for value, _ in candidates}
            if len(identities) != 1:
                result['conflicts'].append(item.name)
                continue
            result['facts'][item.name] = candidates[0][0]
            origins = [ref.to_dict() for _, ref in candidates if ref is not None]
            if origins:
                result['provenance'][item.name] = origins
            if item.name not in facts:
                result['recovered_fields'].append(item.name)
        result['status'] = ('rejected' if fatal else 'conflict' if result['conflicts'] else
                            'needs_context' if result['missing'] or incomplete else 'ready')
        return self._save_recovery(request, result)

    def _save_recovery(self, request, result):
        # request_json is independently bounded and stores only an owned request snapshot.
        raw_request = _json(request, _SOURCE_LIMIT)
        result['recovery_id'] = uuid.uuid4().hex
        result['request_sha256'] = _digest(raw_request)
        try:
            raw_result = _json(result, _FACT_LIMIT)
        except ValueError:
            result.update(status='rejected', facts={}, provenance={}, recovered_fields=[],
                          missing=[], conflicts=[], source_refs=[],
                          issues=[{'code': 'limit_exceeded'}])
            raw_result = _json(result, _FACT_LIMIT)
        with self._connect() as db:
            db.execute('INSERT INTO context_recoveries VALUES(?,?,?,?,?,?,?,?,?)',
                       (result['recovery_id'], result['session_id'], result['capability'],
                        result['requirement_version'], result['request_sha256'], raw_request,
                        _digest(raw_result), raw_result, time.time()))
        return json.loads(raw_result)

    def get_recovery(self, recovery_id) -> dict:
        """Read verified durable evidence; host gates must compare its session and capability."""
        _text(recovery_id, 'recovery_id')
        with self._connect() as db:
            row = db.execute('SELECT * FROM context_recoveries WHERE recovery_id=?',
                             (recovery_id,)).fetchone()
        if row is None:
            raise KeyError(recovery_id)
        if _digest(row['request_json']) != row['request_hash'] or _digest(row['result_json']) != row['result_hash']:
            raise ValueError('recovery record hash mismatch')
        result = json.loads(row['result_json'])
        _json(result, _FACT_LIMIT)
        if (result.get('recovery_id') != recovery_id or result.get('request_sha256') != row['request_hash']
                or result.get('session_id') != row['session_id'] or result.get('capability') != row['capability']
                or result.get('requirement_version') != row['requirement_version']):
            raise ValueError('recovery record identity mismatch')
        return result
