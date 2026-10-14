"""Explicit local skill catalog and source-backed context command adapters."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from .context import ContextStore, requirements_for
from .skill_catalog import SkillCatalog

_JSON_LIMIT = 1024 * 1024
_CAPABILITIES = ('tabular.deduplicate', 'json.normalize', 'xlsx.inspect')


def register_skills_and_context(commands):
    """Register both top-level groups; every operation requires explicit storage."""
    skills = commands.add_parser('skills', help='local snapshots and explicitly trusted adapters')
    skill_commands = skills.add_subparsers(dest='skills_command', required=True)
    for name in ('sync', 'query', 'trust', 'lookup', 'materialize', 'temperature'):
        operation = skill_commands.add_parser(name)
        operation.add_argument('--db', type=Path, required=True)
        operation.add_argument('--owned-root', type=Path, required=True)
        if name == 'sync':
            operation.add_argument('--skill-root', type=Path, required=True)
        if name in ('trust', 'lookup', 'temperature'):
            operation.add_argument('skill_id')
        if name in ('query', 'trust', 'lookup', 'materialize'):
            operation.add_argument('--snapshot-id')
        if name == 'query':
            operation.add_argument('--query', required=True)
            operation.add_argument('--input-format', choices=('csv', 'xlsx', 'json', 'docx', 'pdf'))
            operation.add_argument('--capability')
            operation.add_argument('--limit', type=int, default=5)
            operation.add_argument('--exclude-cold', action='store_true')
        if name == 'materialize':
            operation.add_argument('--destination', type=Path, required=True)
        if name == 'temperature':
            operation.add_argument('--temperature', choices=('warm', 'cold'), required=True)

    context = commands.add_parser('context', help='durable source facts and bounded recovery')
    context_commands = context.add_subparsers(dest='context_command', required=True)
    for name in ('capture', 'recover', 'show', 'revalidate'):
        operation = context_commands.add_parser(name)
        operation.add_argument('--db', type=Path, required=True)
        if name in ('capture', 'recover', 'revalidate'):
            operation.add_argument('--session-id', required=True)
        if name == 'capture':
            operation.add_argument('--source', required=True)
            operation.add_argument('--event-id', required=True)
            operation.add_argument('--payload', required=True, help='JSON object file or - for stdin')
            operation.add_argument('--facts', required=True, help='JSON object file or - for stdin')
        if name == 'recover':
            operation.add_argument('--capability', choices=_CAPABILITIES, required=True)
            operation.add_argument('--request', required=True, help='JSON {facts,source_refs} file or -')
        if name in ('show', 'revalidate'):
            operation.add_argument('recovery_id')


def _read_json(path):
    """Read at most one bounded JSON object, measured in UTF-8 bytes."""
    if path == '-':
        stream = getattr(sys.stdin, 'buffer', sys.stdin)
        raw = stream.read(_JSON_LIMIT + 1)
    else:
        with Path(path).open('rb') as stream:
            raw = stream.read(_JSON_LIMIT + 1)
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    if len(raw) > _JSON_LIMIT:
        raise ValueError('JSON input exceeds 1 MiB')

    def reject_constant(value):
        raise ValueError(f'JSON numbers must be finite: {value}')

    try:
        value = json.loads(raw.decode('utf-8'), parse_constant=reject_constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError('invalid JSON input') from error
    if type(value) is not dict:
        raise ValueError('JSON input must be an object')
    return value


def dispatch_skills_and_context(args) -> dict:
    """Return JSON-ready results without printing; the main CLI owns error handling."""
    if args.command == 'skills':
        catalog = SkillCatalog(args.db, args.owned_root)
        action = args.skills_command
        if action == 'sync':
            return catalog.sync(args.skill_root)
        if action == 'query':
            return catalog.query(args.query, input_format=args.input_format,
                capability=args.capability, include_cold=not args.exclude_cold,
                limit=args.limit, snapshot_id=args.snapshot_id)
        if action == 'trust':
            return catalog.trust_adapter(args.skill_id, snapshot_id=args.snapshot_id)
        if action == 'lookup':
            return catalog.lookup(args.skill_id, snapshot_id=args.snapshot_id)
        if action == 'materialize':
            return catalog.materialize(args.destination, snapshot_id=args.snapshot_id)
        if action == 'temperature':
            return catalog.set_temperature(args.skill_id, args.temperature)
        raise ValueError('unsupported skills operation')
    if args.command == 'context':
        action = args.context_command
        if action == 'capture':
            if args.payload == args.facts == '-':
                raise ValueError('payload and facts cannot both read stdin')
            payload, facts = _read_json(args.payload), _read_json(args.facts)
            return ContextStore(args.db).capture(source=args.source, event_id=args.event_id,
                session_id=args.session_id, payload=payload, facts=facts).to_dict()
        if action == 'recover':
            request = _read_json(args.request)
            if set(request) != {'facts', 'source_refs'}:
                raise ValueError('context request must contain facts and source_refs only')
            requirement = requirements_for(args.capability)
            return ContextStore(args.db).recover(requirement=requirement,
                session_id=args.session_id, facts=request['facts'], source_refs=request['source_refs'])
        if action == 'show':
            return dict(ContextStore(args.db).get_recovery(args.recovery_id), historical=True)
        if action == 'revalidate':
            return ContextStore(args.db).revalidate_recovery(args.recovery_id,
                session_id=args.session_id)
        raise ValueError('unsupported context operation')
    raise ValueError('unsupported skills/context command')
