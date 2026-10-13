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
    hermes_run.add_argument('--entry-mode', choices=('strict', 'optional'), default='strict')
    hermes_run.add_argument('--request', help='optional fixed JSON task contract')
    hermes_run.add_argument('--max-task-attempts', type=int, default=3)
    _configuration(hermes_run)
    recover = commands.add_parser('recover', help='resume an interrupted task before publication')
    recover.add_argument('run_id')
    recover.add_argument('--max-attempts', type=int, default=3)
    _configuration(recover)
    evolution = commands.add_parser('evolution', help='durable feedback and validated upgrade proposals')
    evolution_commands = evolution.add_subparsers(dest='evolution_command', required=True)
    cycle = evolution_commands.add_parser('cycle')
    cycle.add_argument('--owned-root', type=Path, required=True)
    cycle.add_argument('--no-auto-apply', action='store_true')
    _configuration(cycle)
    for name in ('list', 'experiences'):
        _configuration(evolution_commands.add_parser(name))
    proposal = evolution_commands.add_parser('propose')
    proposal.add_argument('--candidate', type=Path, required=True)
    proposal.add_argument('--component', required=True)
    proposal.add_argument('--kind', required=True)
    proposal.add_argument('--evidence-run-id', action='append', default=[])
    proposal.add_argument('--base-version-id')
    proposal.add_argument('--owned-root', type=Path, required=True)
    _configuration(proposal)
    for name in ('show', 'validate', 'apply'):
        operation = evolution_commands.add_parser(name)
        operation.add_argument('proposal_id')
        if name != 'show':
            operation.add_argument('--owned-root', type=Path)
        _configuration(operation)
    rollback = evolution_commands.add_parser('rollback')
    rollback.add_argument('version_id')
    _configuration(rollback)
    args = parser.parse_args(argv)
    try:
        if args.command == 'hermes':
            import tempfile
            from .runtime import default_db_path
            from .integrations.hermes_session import run_hermes_session
            from .integrations.hermes_gate import evaluate_session
            db_path = Path(args.db) if args.db else default_db_path()
            required_request = read_json(args.request) if args.request else None
            with tempfile.TemporaryDirectory(prefix='rpe-hermes-') as owned:
                result = run_hermes_session(args.prompt_file.read_text(encoding='utf-8'),
                    args.workspace, db_path,
                    tool_enabled=True, owner_root=Path(owned), timeout=args.timeout,
                    hermes_root=args.hermes_root, entry_mode=args.entry_mode,
                    required_request=required_request, max_task_attempts=args.max_task_attempts)
                verdict = evaluate_session(result, db_path, args.workspace,
                    required_request=required_request, require_policy=args.entry_mode == 'strict')
            result.pop('session_root', None)
            result.pop('stream_tool_calls', None)
            result['temporary_history_cleaned'] = True
            result['entry_validation'] = verdict
            result.update({key: value for key, value in verdict.items() if key != 'errors'})
        elif args.command == 'evolution':
            from .runtime import default_db_path
            from .evolution.store import EvolutionStore
            db_path = Path(args.db) if args.db else default_db_path()
            if args.evolution_command == 'cycle':
                from .evolution.cycle import cycle
                result = cycle(db_path, args.owned_root, auto_apply=not args.no_auto_apply)
            elif args.evolution_command == 'propose':
                from .evolution.candidates import propose_candidate
                result = propose_candidate(db_path, args.owned_root,
                    component=args.component, kind=args.kind,
                    content=args.candidate.read_text(encoding='utf-8'),
                    evidence_run_ids=args.evidence_run_id, base_version_id=args.base_version_id)
            elif args.evolution_command == 'validate':
                from .evolution.candidates import validate_candidate
                result = validate_candidate(args.proposal_id, db_path, args.owned_root)
            elif args.evolution_command == 'apply':
                from .evolution.candidates import apply_candidate
                result = apply_candidate(args.proposal_id, db_path, args.owned_root)
            elif args.evolution_command == 'rollback':
                from .evolution.candidates import rollback
                result = rollback(args.version_id, db_path)
            else:
                store = EvolutionStore(db_path)
                if args.evolution_command == 'list':
                    result = {'proposals': store.list_proposals()}
                elif args.evolution_command == 'experiences':
                    result = {'experiences': store.list_experiences()}
                else:
                    result = store.get_proposal(args.proposal_id)
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
            elif args.command == 'recover':
                from .recovery import recover_run
                result = recover_run(runtime, args.run_id, max_attempts=args.max_attempts)
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
        if args.command == 'runs' or (args.command == 'evolution' and
                                     args.evolution_command in ('list', 'show', 'experiences', 'propose')):
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
