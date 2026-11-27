"""Explicit, bounded review attachments. Stored prose is evidence, never authority."""
from __future__ import annotations

import copy
import hashlib
import json
import time

from .capability_catalog import CapabilityCatalog, _definitions
from .context import (CapabilityContext, ContextStore, FactRequirement, SourceRef,
                      _json, _text, requirements_for)
from .contract_sdk import DEFAULT_REGISTRY, _strict_equal
from .review_quotes import source_quote_anchors

_SCHEMA = 'rpe.review-context/v1'
_FACT_FIELDS = frozenset({'constraints', *(field for definition in _definitions()
                                         for field in definition['execution_fields'])})


def validate_sources(value):
    """Validate an explicitly chosen import file, without touching a database."""
    if type(value) is not list or len(value) > 8:
        raise ValueError('review context requires an array of at most eight sources')
    owned = json.loads(_json(value, 256 * 1024))
    # Only trusted Python registrations can extend the import schema. Discover
    # declarations at validation time so registration and removal take effect
    # without a module reload; skill-catalog prose cannot grant fact names.
    plugin_fields = {key for plugin in DEFAULT_REGISTRY.list_plugins()
                     for key in plugin.option_keys}
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
        if type(facts) is not dict or set(facts) - (_FACT_FIELDS | plugin_fields):
            raise ValueError('context facts contain unknown or unauthorized fields')
        _json(facts, 16 * 1024)
        for name, fact in facts.items():
            if name in plugin_fields and name not in _FACT_FIELDS:
                # Plugin options may be structured JSON, numbers, booleans or
                # empty values. Their registered validator checks semantics at
                # admission; recovery still requires exact explicit agreement.
                continue
            elif name in {'key_columns', 'constraints'}:
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


def _projection(ref, content):
    facts = copy.deepcopy(content['facts'])
    anchors = source_quote_anchors(ref, content['payload']['text'], facts)
    return {'source_ref': copy.deepcopy(ref), 'facts': facts,
            'quotes': {field: row['quote'] for field, row in anchors.items()},
            'quote_refs': {field: row['quote_ref'] for field, row in anchors.items()},
            'quote_spans': {field: row['span'] for field, row in anchors.items()}}


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
    legacy = [{key: value for key, value in row.items() if key not in {'quote_refs', 'quote_spans'}}
              for row in projected]
    if (not _strict_equal(projected, owned['model_sources'])
            and not _strict_equal(legacy, owned['model_sources'])):
        raise ValueError('review context model projection differs from verified sources')
    return owned


def recover_context(db_path, bundle, session_id, capability, execution, constraints):
    """Check explicit draft values against required facts; never fill the draft."""
    bundle = validate_context(db_path, bundle, session_id)
    candidates = CapabilityCatalog().resolve(capability)['candidates']
    if not candidates:
        raise ValueError('unsupported context capability')
    definition = candidates[0]
    if definition.get('plugin_options'):
        requirement = CapabilityContext(capability, definition['contract_version'], tuple(
            FactRequirement(name, 'string') for name in definition['required_execution']))
    else:
        requirement = requirements_for(capability)
    names = {item.name for item in requirement.requirements}
    store = ContextStore(db_path)
    for ref in bundle['source_refs']:
        content = store.resolve(ref, session_id=session_id)
        for field in content['facts']:
            if field == 'constraints':
                if set(content['facts'][field]) != set(constraints):
                    raise ValueError('source conflict: constraints')
            elif (field not in names and
                  (definition.get('plugin_options') or field in definition['execution_fields'])):
                if field not in execution or not _strict_equal(execution[field], content['facts'][field]):
                    raise ValueError('source conflict or missing explicit field: execution.' + field)
    return store.recover(requirement=requirement, session_id=session_id,
                         facts={name: value for name, value in execution.items() if name in names},
                         source_refs=bundle['source_refs'])
