"""Strict current-source preparation and immutable handoff inspection."""
from __future__ import annotations

from pathlib import Path
import sys

from .capability_catalog import build_catalog
from .path_binding import read_regular_file
from .requirements import (MAX_SOURCE_BYTES, SourceStateStore, interpret_source_intent,
                           source_document)


def register_source(commands):
    source = commands.add_parser('source', help='complete strict source requirements and handoffs')
    operations = source.add_subparsers(dest='source_command', required=True)
    prepare = operations.add_parser('prepare')
    prepare.add_argument('--db', type=Path, required=True)
    prepare.add_argument('--catalog-root', type=Path)
    prepare.add_argument('--skill-snapshot-id')
    prepare.add_argument('--workspace', type=Path, required=True)
    prepare.add_argument('--session-id', required=True)
    prepare.add_argument('--source-ref', required=True)
    prepare.add_argument('--source', required=True, help='entire strict source JSON file or -')
    show = operations.add_parser('show')
    show.add_argument('state_id')
    show.add_argument('--db', type=Path, required=True)
    show.add_argument('--session-id', required=True)


def _read_source(path):
    if path == '-':
        stream = getattr(sys.stdin, 'buffer', sys.stdin)
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    else:
        raw = read_regular_file(Path(path).absolute(), max_bytes=MAX_SOURCE_BYTES)
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('current source exceeds 64 KiB')
    return raw.decode('utf-8')


def dispatch_source(args):
    if args.source_command == 'show':
        return SourceStateStore(args.db).show(args.state_id, session_id=args.session_id)
    source = source_document(_read_source(args.source), session_id=args.session_id,
                             source_ref=args.source_ref)
    if source['status'] != 'ready':
        return source
    catalog = build_catalog(db_path=args.db if args.catalog_root is not None else None,
                            catalog_root=args.catalog_root, skill_snapshot_id=args.skill_snapshot_id)
    result = interpret_source_intent(source['source_facts'], source_document=source,
        catalog=catalog, workspace=args.workspace, session_id=args.session_id, source_ref=args.source_ref)
    if result['status'] != 'ready':
        return result
    return SourceStateStore(args.db).capture(source, result, session_id=args.session_id,
                                             source_ref=args.source_ref)
