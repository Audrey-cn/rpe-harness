"""Thin JSON CLI facade over HostAPI for host-neutral task lifecycle operations."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from .host_api import HostAPI, TrustedHostAction
from .runtime import default_db_path


def _read_json_object(source):
    if source == '-':
        raw = sys.stdin.read(1_048_577)
    else:
        with Path(source).open(encoding='utf-8') as stream:
            raw = stream.read(1_048_577)
    if len(raw.encode('utf-8')) > 1_048_576:
        raise ValueError('JSON input exceeds 1 MiB')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('JSON input must be an object')
    return value


def register_host(commands):
    host = commands.add_parser('host', help='host-neutral task facade (capabilities/start/show/confirm/cancel/reconcile)')
    sub = host.add_subparsers(dest='host_command', required=True)

    sub.add_parser('capabilities', help='declare supported host facade methods and version')

    start = sub.add_parser('start', help='start a new host-owned task draft without confirming execution')
    req_group = start.add_mutually_exclusive_group(required=True)
    req_group.add_argument('--request', help='JSON file or - for stdin')
    req_group.add_argument('--text', help='raw natural-language task text')
    start.add_argument('--host-context', help='JSON file with owner/session_id/workspace')
    start.add_argument('--owner', help='trusted host owner identifier')
    start.add_argument('--session-id', help='trusted host session identifier')
    start.add_argument('--workspace', help='existing workspace directory path')
    start.add_argument('--db', help='SQLite path')

    show = sub.add_parser('show', help='show current task state for its owner')
    show.add_argument('operation_id')
    show.add_argument('--owner', required=True)
    show.add_argument('--db', help='SQLite path')

    confirm = sub.add_parser('confirm', help='record digest-bound trusted confirmation')
    confirm.add_argument('operation_id')
    confirm.add_argument('--digest', required=True)
    confirm.add_argument('--generation', type=int, required=True)
    confirm.add_argument('--owner', required=True)
    confirm.add_argument('--actor', default='trusted-host')
    confirm.add_argument('--trusted-action', action='store_true',
                         help='explicitly mark invocation as a trusted host user approval action')
    confirm.add_argument('--db', help='SQLite path')

    cancel = sub.add_parser('cancel', help='cancel a task for an expected generation')
    cancel.add_argument('operation_id')
    cancel.add_argument('--generation', type=int, required=True)
    cancel.add_argument('--owner', required=True)
    cancel.add_argument('--reason', help='optional cancellation reason')
    cancel.add_argument('--db', help='SQLite path')

    reconcile = sub.add_parser('reconcile', help='reconcile persisted run state and cleanup status')
    reconcile.add_argument('operation_id')
    reconcile.add_argument('--owner', required=True)
    reconcile.add_argument('--db', help='SQLite path')


def dispatch_host(args):
    if args.host_command == 'capabilities':
        return HostAPI.capabilities()
    db_path = Path(args.db) if getattr(args, 'db', None) else default_db_path()
    api = HostAPI(db_path)
    if args.host_command == 'start':
        request = _read_json_object(args.request) if args.request is not None else args.text
        if args.host_context:
            context = _read_json_object(args.host_context)
        else:
            context = {'owner': args.owner, 'session_id': args.session_id, 'workspace': args.workspace}
        return api.start_task(request, context)
    if args.host_command == 'show':
        return api.show_task(args.operation_id, args.owner)
    if args.host_command == 'confirm':
        trusted = (TrustedHostAction(owner=args.owner, operation_id=args.operation_id,
                                     generation=args.generation, digest=args.digest,
                                     actor=args.actor)
                   if args.trusted_action else None)
        return api.confirm_task(args.operation_id, args.digest, trusted)
    if args.host_command == 'cancel':
        return api.cancel_task(args.operation_id, args.generation, owner=args.owner, reason=args.reason)
    if args.host_command == 'reconcile':
        return api.reconcile_task(args.operation_id, args.owner)
    raise ValueError('unknown host command')
