"""JSON CLI adapters for the shared capability and minimal-state interfaces."""
from __future__ import annotations

from pathlib import Path

from .capability_catalog import build_catalog
from .intent import MinimalStateStore, interpret_intent
from .skill_cli import _read_json


def _catalog_options(parser):
    parser.add_argument('--db', type=Path)
    parser.add_argument('--catalog-root', type=Path)
    parser.add_argument('--skill-snapshot-id')


def register_address(commands):
    capabilities = commands.add_parser('capabilities', help='unified executable capability directory')
    operations = capabilities.add_subparsers(dest='capabilities_command', required=True)
    _catalog_options(operations.add_parser('list'))
    resolve = operations.add_parser('resolve')
    _catalog_options(resolve)
    resolve.add_argument('--capability')
    resolve.add_argument('--input-format')
    resolve.add_argument('--constraint', action='append', default=[])

    intent = commands.add_parser('intent', help='prepare and inspect immutable task handoffs')
    operations = intent.add_subparsers(dest='intent_command', required=True)
    prepare = operations.add_parser('prepare')
    prepare.add_argument('--db', type=Path, required=True)
    prepare.add_argument('--catalog-root', type=Path)
    prepare.add_argument('--skill-snapshot-id')
    prepare.add_argument('--workspace', type=Path, required=True)
    prepare.add_argument('--session-id', required=True)
    prepare.add_argument('--source-ref', required=True)
    prepare.add_argument('--extracted', required=True, help='four-field extraction JSON file or -')
    prepare.add_argument('--source-facts', help='explicit trusted source facts JSON file')
    show = operations.add_parser('show')
    show.add_argument('state_id')
    show.add_argument('--db', type=Path, required=True)
    show.add_argument('--session-id', required=True)


def prepare_handoff(extracted, *, db_path, workspace, session_id, source_ref,
                    catalog_root=None, skill_snapshot_id=None, source_facts=None):
    """Shared public adapter: never expose the internal anchored request as a handoff."""
    catalog = build_catalog(db_path=db_path if catalog_root is not None else None,
                            catalog_root=catalog_root, skill_snapshot_id=skill_snapshot_id)
    result = interpret_intent(extracted, catalog=catalog, workspace=workspace,
                              session_id=session_id, source_ref=source_ref, source_facts=source_facts)
    if result['status'] != 'ready':
        return result
    return MinimalStateStore(db_path).capture(result, session_id=session_id, source_ref=source_ref)


def dispatch_address(args):
    if args.command == 'capabilities':
        catalog = build_catalog(db_path=args.db if args.catalog_root is not None else None,
                                catalog_root=args.catalog_root, skill_snapshot_id=args.skill_snapshot_id)
        if args.capabilities_command == 'list':
            return {'catalog_sha256': catalog.snapshot_id, 'entries': catalog.entries()}
        return catalog.resolve(args.capability, args.input_format, args.constraint)
    if args.intent_command == 'show':
        return MinimalStateStore(args.db).show(args.state_id, session_id=args.session_id)
    if args.extracted == args.source_facts == '-':
        raise ValueError('extraction and source facts cannot both read stdin')
    return prepare_handoff(_read_json(args.extracted), db_path=args.db, workspace=args.workspace,
        session_id=args.session_id, source_ref=args.source_ref, catalog_root=args.catalog_root,
        skill_snapshot_id=args.skill_snapshot_id,
        source_facts=_read_json(args.source_facts) if args.source_facts else None)
