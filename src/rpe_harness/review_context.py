"""Explicit, bounded review attachments. Stored prose is evidence, never authority."""
from __future__ import annotations

import copy
import hashlib
import json
import time

from .capability_catalog import _definitions
from .context import ContextStore, SourceRef, _json, _text, requirements_for

_SCHEMA = 'rpe.review-context/v1'
_FACT_FIELDS = frozenset({'constraints', *(field for definition in _definitions()
                                         for field in definition['execution_fields'])})


def validate_sources(value):
    """Validate an explicitly chosen import file, without touching a database."""
    if type(value) is not list or len(value) > 8:
        raise ValueError('review context requires an array of at most eight sources')
    owned = json.loads(_json(value, 256 * 1024))
    identities = set()
    for item in owned:
        if type(item) is not dict or set(item) != {'source', 'event_id', 'payload', 'facts'}:
            raise ValueError('context source must contain source, event_id, payload and facts only')
        _text(item['source'], 'source')
        _text(item['event_id'], 'event_id')
        identity = (item['source'], item['event_id'])
        if identity in identities:
            raise ValueError('duplicate context source identity')
        identities.add(identity)
        payload, facts = item['payload'], item['facts']
        if (type(payload) is not dict or type(payload.get('text')) is not str
                or not payload['text'].strip()):
            raise ValueError('context source payload.text must be nonempty text')
        _json(payload, 64 * 1024)
        if type(facts) is not dict or set(facts) - _FACT_FIELDS:
            raise ValueError('context facts contain unknown or unauthorized fields')
        _json(facts, 16 * 1024)
        for name, fact in facts.items():
            if name in {'key_columns', 'constraints'}:
                valid = (type(fact) is list and (bool(fact) or name == 'constraints')
                    and all(type(part) is str and part.strip() and '\x00' not in part for part in fact)
                    and len(set(fact)) == len(fact))
            else:
                valid = type(fact) is str and bool(fact.strip()) and '\x00' not in fact
                if name == 'delimiter':
                    valid = type(fact) is str and len(fact) == 1 and fact not in '\r\n\x00'
            if not valid:
                raise ValueError('invalid explicit context fact: ' + name)
    return owned


def _excerpt(text, value):
    """Return only a short literal span containing the explicitly supplied value."""
    parts = value if type(value) is list else [value]
    positions = [(text.find(part), len(part)) for part in parts if type(part) is str]
    if not positions or any(start < 0 for start, _ in positions):
        return None
    first, last = min(start for start, _ in positions), max(start + size for start, size in positions)
    return text[first:last] if last - first <= 256 else None


def _projection(ref, content):
    facts = copy.deepcopy(content['facts'])
    return {'source_ref': copy.deepcopy(ref), 'facts': facts,
            'quotes': {field: quote for field, value in facts.items()
                       if (quote := _excerpt(content['payload']['text'], value)) is not None}}


def prepare_context(db_path, sources, session_id):
    """Import caller-selected sources under the actual host session and own the result."""
    _text(session_id, 'session_id')
    sources = validate_sources(sources)
    store = ContextStore(db_path)
    refs, model_sources = [], []
    for item in sources:
        identity = hashlib.sha256(_json([session_id, item['source'], item['event_id']], 8192).encode()).hexdigest()
        ref = store.capture(source=item['source'], event_id='review:' + identity, session_id=session_id,
            payload={'text': item['payload']['text'], 'original_event_id': item['event_id'],
                     'event': item['payload']}, facts=item['facts']).to_dict()
        refs.append(ref)
        model_sources.append(_projection(ref, store.resolve(ref, session_id=session_id)))
    bundle = {'schema_version': _SCHEMA, 'session_id': session_id,
              'source_refs': refs, 'model_sources': model_sources}
    _json(bundle, 64 * 1024)
    return bundle


def validate_context(db_path, bundle, session_id):
    """Reopen exact session/hash sources and reject a changed model projection."""
    _text(session_id, 'session_id')
    if (type(bundle) is not dict or set(bundle) != {
            'schema_version', 'session_id', 'source_refs', 'model_sources'}
            or bundle['schema_version'] != _SCHEMA or bundle['session_id'] != session_id
            or type(bundle['source_refs']) is not list or len(bundle['source_refs']) > 8
            or type(bundle['model_sources']) is not list):
        raise ValueError('review context bundle or session differs')
    owned = json.loads(_json(bundle, 64 * 1024))
    store, projected, seen = ContextStore(db_path), [], set()
    for value in owned['source_refs']:
        ref = SourceRef.from_dict(value)
        identity = (ref.source, ref.event_id)
        if identity in seen:
            raise ValueError('duplicate review source reference')
        seen.add(identity)
        content = store.resolve(ref, session_id=session_id)
        if content['received_at'] > time.time():
            raise ValueError('review context source is in the future')
        # Recheck the trusted import schema; a different source-capture API does
        # not get to introduce arbitrary fact names through a forged bundle.
        validate_sources([{'source': ref.source, 'event_id': content['payload']['original_event_id'],
            'payload': content['payload']['event'], 'facts': content['facts']}])
        projected.append(_projection(value, content))
    if projected != owned['model_sources']:
        raise ValueError('review context model projection differs from verified sources')
    return owned


def recover_context(db_path, bundle, session_id, capability, execution, constraints):
    """Check explicit draft values against required facts; never fill the draft."""
    bundle = validate_context(db_path, bundle, session_id)
    definition = next(item for item in _definitions() if item['capability'] == capability)
    requirement = requirements_for(capability)
    names = {item.name for item in requirement.requirements}
    store = ContextStore(db_path)
    for ref in bundle['source_refs']:
        content = store.resolve(ref, session_id=session_id)
        for field in content['facts']:
            if field == 'constraints':
                if set(content['facts'][field]) != set(constraints):
                    raise ValueError('source conflict: constraints')
            elif field in definition['execution_fields'] and field not in names:
                if field not in execution or execution[field] != content['facts'][field]:
                    raise ValueError('source conflict or missing explicit field: execution.' + field)
    return store.recover(requirement=requirement, session_id=session_id,
                         facts={name: value for name, value in execution.items() if name in names},
                         source_refs=bundle['source_refs'])
