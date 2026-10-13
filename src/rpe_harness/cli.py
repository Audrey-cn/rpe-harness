"""JSON-only command line interface for local tasks and saved runs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .runtime import HarnessRuntime
from .storage import EventConflict


def _configuration(parser):
    parser.add_argument('--db', help='SQLite path; default ~/.local/share/rpe-harness/runs.sqlite')
    parser.add_argument('--data-dir', help='optional external metadata directory')


def read_json(path):
    if path == '-':
        raw = sys.stdin.read(1_048_577)
    else:
        with Path(path).open(encoding='utf-8') as stream:
            raw = stream.read(1_048_577)
    if len(raw.encode('utf-8')) > 1_048_576:
        raise ValueError('request exceeds 1 MiB')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('request must be a JSON object')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(prog='rpe-harness', description=__doc__)
    from . import __version__
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--request', required=True, help='JSON file or - for stdin')
    _configuration(run)
    events = commands.add_parser('events')
    event_commands = events.add_subparsers(dest='event_command', required=True)
    submit = event_commands.add_parser('submit')
    submit.add_argument('--event', required=True)
    _configuration(submit)
    runs = commands.add_parser('runs')
    run_commands = runs.add_subparsers(dest='runs_command', required=True)
    show = run_commands.add_parser('show')
    show.add_argument('run_id')
    _configuration(show)
    listing = run_commands.add_parser('list')
    listing.add_argument('--limit', type=int, default=20)
    _configuration(listing)
    serve = commands.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=18400)
    _configuration(serve)
    route = commands.add_parser('route')
    route.add_argument('--request', required=True)
    _configuration(route)
    benchmark = commands.add_parser('benchmark')
    benchmark.add_argument('--report', type=Path)
    hermes = commands.add_parser('hermes', help='isolated installed Hermes host integration')
    hermes_commands = hermes.add_subparsers(dest='hermes_command', required=True)
    hermes_run = hermes_commands.add_parser('run')
    hermes_run.add_argument('--workspace', type=Path, required=True)
    hermes_run.add_argument('--prompt-file', type=Path, required=True)
    hermes_run.add_argument('--timeout', type=float, default=180)
    hermes_run.add_argument('--hermes-root', type=Path)
    _configuration(hermes_run)
    args = parser.parse_args(argv)
    try:
        if args.command == 'hermes':
            import tempfile
            from .runtime import default_db_path
            from .integrations.hermes_session import run_hermes_session
            with tempfile.TemporaryDirectory(prefix='rpe-hermes-') as owned:
                result = run_hermes_session(args.prompt_file.read_text(encoding='utf-8'),
                    args.workspace, Path(args.db) if args.db else default_db_path(),
                    tool_enabled=True, owner_root=Path(owned), timeout=args.timeout,
                    hermes_root=args.hermes_root)
            result.pop('session_root', None)
            result.pop('stream_tool_calls', None)
            result['temporary_history_cleaned'] = True
            result['harness_verified'] = False
            result['status'] = 'failed' if not result['completed'] else 'unverified'
            returned = [e for e in result['events'] if e.get('kind') == 'rpe_task_returned']
            if result['completed'] and returned:
                last = returned[-1]
                result['status'] = last.get('status', 'unverified')
                if last.get('run_id'):
                    from .storage import RunStore
                    saved = RunStore(Path(args.db) if args.db else default_db_path()).get(last['run_id'])
                    verified = ((saved.get('verification') or {}).get('passed') is True
                                and (saved.get('artifact') or {}).get('published') is True)
                    result['harness_verified'] = saved.get('status') == 'succeeded' and verified
                    if result['status'] == 'succeeded' and not result['harness_verified']:
                        result['status'] = 'unverified'
        elif args.command == 'benchmark':
            from .experiments.basic_validation import run_basic_validation
            result = run_basic_validation(args.report)
        elif args.command == 'serve':
            from .server import main as serve_main
            serve_argv = ['--host', args.host, '--port', str(args.port)]
            if args.db:
                serve_argv += ['--db', args.db]
            if args.data_dir:
                serve_argv += ['--data-dir', args.data_dir]
            serve_main(serve_argv)
            return 0
        else:
            runtime = HarnessRuntime(data_dir=args.data_dir, db_path=args.db)
            if args.command == 'run':
                result = runtime.run(read_json(args.request))
            elif args.command == 'events':
                result = runtime.run(read_json(args.event), source='hermes')
            elif args.command == 'runs':
                result = runtime.store.get(args.run_id) if args.runs_command == 'show' else {'runs': runtime.store.list(args.limit)}
            else:
                state, decision = runtime.route(read_json(args.request))
                result = {'state': state.to_dict(), 'level': decision.level.name,
                          'candidates': [s.skill_id for s in decision.candidates],
                          'fallback_to_llm': decision.fallback_to_llm, 'reason': decision.reason}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if args.command == 'runs':
            return 0
        status = result.get('status')
        if status in ['received', 'running']:
            return 3
        return 2 if status == 'rejected' else 1 if status in ['failed', 'interrupted', 'unverified'] or result.get('passed') is False else 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({'status': 'error', 'error_code': 'event_conflict' if isinstance(exc, EventConflict) else 'invalid_request', 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
