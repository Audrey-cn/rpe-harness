"""Narrow, deterministic reads of explicitly authorized context snapshots.

The host constructs the resolver with grants from its trusted job/session binding;
``source_refs`` on a resolve call select among those grants and cannot grant access.
Grants are immutable in-memory policy, not a second source or execution ledger.
ContextStore remains the authority for the exact canonical SHA-256 snapshot.
"""
from __future__ import annotations

import copy
import json
import time
from types import MappingProxyType

from .capability_catalog import _definitions
from .context import ContextStore, _FACT_LIMIT, _SourceProblem, _json, _ref
from .intent import _identity

MAX_FETCHES = 2
MAX_RESULT_BYTES = 24 * 1024
_EXECUTION_FIELDS = frozenset(field for row in _definitions() for field in row['execution_fields'])
_FIELDS = frozenset({'goal', 'capability', 'constraints', *_EXECUTION_FIELDS,
                     *('execution.' + field for field in _EXECUTION_FIELDS)})


def _field_name(field):
    if type(field) is not str or field not in _FIELDS:
        raise ValueError('only registered individual fact fields may be resolved')
    return field.removeprefix('execution.')


def _valid_value(field, value):
    """Never return arbitrary objects, even under a registered field name."""
    if field in {'constraints', 'key_columns'}:
        return (type(value) is list and (bool(value) or field == 'constraints')
                and all(type(part) is str and part.strip() and '\x00' not in part
                        for part in value) and len(set(value)) == len(value))
    if field == 'delimiter':
        return type(value) is str and len(value) == 1 and value not in '\r\n\x00'
    return type(value) is str and bool(value.strip()) and '\x00' not in value


def _values(facts, name):
    # Current intent facts nest execution; review/context snapshots use flat
    # execution names. Both shapes remain explicit evidence. Never infer from prose.
    result = [facts[name]] if name in facts else []
    if name in _EXECUTION_FIELDS and type(facts.get('execution')) is dict:
        if name in facts['execution']:
            result.append(facts['execution'][name])
    return result


class SourceResolver:
    """Resolve one field through at most two exact ContextStore reads.

    ``authorizations`` is a host-owned list/tuple of dictionaries containing only
    ``job_id``, ``session_id`` and ``source_ref`` (a SourceRef or its dictionary).
    It must come from trusted host policy, never model-produced request content.
    The constructor snapshots it, so subsequent caller mutations confer no access.

    Result dictionaries contain status, field, provenance, fetch_count, fetch_limit
    and reason; only ``found`` includes ``value``. Provenance contains references
    alone, never raw text or other facts. More than two distinct authorized refs
    require the user to narrow the selection; a partial result is never accepted.
    """

    def __init__(self, db_path_or_store, *, authorizations=()):
        if type(authorizations) not in (list, tuple) or len(authorizations) > 4096:
            raise ValueError('authorizations must be a bounded host-owned list or tuple')
        grants = {}
        for grant in authorizations:
            if type(grant) is not dict or set(grant) != {'job_id', 'session_id', 'source_ref'}:
                raise ValueError('authorization requires job_id, session_id and source_ref only')
            _identity(grant['job_id'], 'job_id')
            _identity(grant['session_id'], 'session_id')
            ref = _ref(grant['source_ref'])
            key = (grant['job_id'], grant['session_id'], ref.source, ref.event_id)
            if key in grants and grants[key] != ref.sha256:
                raise ValueError('authorization identity has conflicting hashes')
            grants[key] = ref.sha256
        self._grants = MappingProxyType(grants)
        self.store = (db_path_or_store if isinstance(db_path_or_store, ContextStore)
                      else ContextStore(db_path_or_store))

    def resolve(self, field, source_refs, job_id, session_id):
        """Read only authorized, exact snapshots; failures never include fact values."""
        result = {'status': 'needs_user', 'field': None, 'provenance': [],
                  'fetch_count': 0, 'fetch_limit': MAX_FETCHES, 'reason': 'missing_source'}

        def finish(status, reason, *, provenance=(), value=None):
            result.update(status=status, reason=reason,
                          provenance=[ref.to_dict() for ref in provenance])
            if status == 'found':
                result['value'] = copy.deepcopy(value)
            # Own the complete result and bound provenance plus the returned value.
            return json.loads(_json(result, MAX_RESULT_BYTES))

        try:
            name = _field_name(field)
            result['field'] = field
            _identity(job_id, 'job_id')
            _identity(session_id, 'session_id')
            if type(source_refs) not in (list, tuple) or len(source_refs) > 32:
                return finish('forbidden', 'invalid_source_refs')
            refs = [_ref(value) for value in source_refs]
        except (ValueError, TypeError, UnicodeError):
            return finish('forbidden', 'invalid_request')

        # Authorize the entire set before any fetch. A later unauthorized source
        # cannot cause an earlier authorized source to be read or partially exposed.
        for ref in refs:
            key = (job_id, session_id, ref.source, ref.event_id)
            if key not in self._grants:
                return finish('forbidden', 'source_not_authorized')
        if any(self._grants[(job_id, session_id, ref.source, ref.event_id)] != ref.sha256
               for ref in refs):
            return finish('stale', 'hash_mismatch')
        refs = list(dict.fromkeys(refs))
        if len(refs) > MAX_FETCHES:
            return finish('needs_user', 'fetch_budget_exceeded')
        if not refs:
            return finish('needs_user', 'missing_source')

        candidates, origins, issues = [], [], []
        now = time.time()
        for ref in refs:
            result['fetch_count'] += 1
            try:
                content = self.store.resolve(ref, session_id=session_id)
                if content['received_at'] > now:
                    issues.append(('stale', 'source_future'))
                    continue
                values = _values(content['facts'], name)
                if any(not _valid_value(name, value) for value in values):
                    issues.append(('needs_user', 'invalid_fact'))
                    continue
                candidates.extend(values)
                if values:
                    origins.append(ref)
            except _SourceProblem as error:
                status = ('forbidden' if error.code == 'session_mismatch' else
                          'stale' if error.code == 'hash_mismatch' else 'needs_user')
                issues.append((status, error.code))

        # Complete-source checks win over apparent agreement or conflicting values.
        # No success can be grounded in a subset when another selected ref failed.
        for status in ('forbidden', 'stale', 'needs_user'):
            issue = next((reason for kind, reason in issues if kind == status), None)
            if issue is not None:
                return finish(status, issue)
        if not candidates:
            return finish('needs_user', 'missing_field')
        if len({_json(value, _FACT_LIMIT) for value in candidates}) != 1:
            return finish('conflict', 'conflicting_values', provenance=origins)
        return finish('found', 'authorized_fact', provenance=origins, value=candidates[0])
