"""Local tasks and saved runs, with readable natural-language task review."""
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
    from .assets.cli import register_assets
    register_assets(commands)
    from .skill_cli import register_skills_and_context
    register_skills_and_context(commands)
    from .address_cli import register_address
    register_address(commands)
    from .source_cli import register_source
    register_source(commands)
    from .proposal_cli import register_review
    register_review(commands)
    from .host_cli import register_host
    register_host(commands)
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
    cancel = run_commands.add_parser('cancel', help='cancel a run before publication starts')
    cancel.add_argument('run_id')
    cancel.add_argument('--reason', help='optional cancellation reason, at most 4096 UTF-8 bytes')
    _configuration(cancel)
    serve = commands.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=18400)
    serve.add_argument('--catalog-root', type=Path)
    serve.add_argument('--skill-snapshot-id')
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
    hermes_run.add_argument('--entry-mode', choices=('strict', 'optional', 'skills', 'address', 'clean'), default='strict')
    hermes_run.add_argument('--catalog-root', type=Path)
    hermes_run.add_argument('--skill-snapshot-id')
    hermes_run.add_argument('--context-sources', help='trusted JSON file with a sources array')
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
    worker = evolution_commands.add_parser('worker', help='generate one candidate, without approving it')
    worker.add_argument('--owned-root', type=Path, required=True)
    worker.add_argument('--kind', choices=('csv_executor', 'json_executor'), default='json_executor')
    worker.add_argument('--timeout', type=int, default=120)
    worker.add_argument('--hermes-root', type=Path)
    worker.add_argument('--training-packet', type=Path, help='authorized bounded CSV training packet JSON')
    worker.add_argument('--previous-candidate', type=Path, help='original previous candidate Python file')
    worker.add_argument('--max-turns', type=int, default=6)
    worker.add_argument('--producer-only', action='store_true', help='expose only one verified candidate write with the public ABI inline')
    _configuration(worker)
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
        if args.command == 'review':
            from .proposal_cli import dispatch_review
            result = dispatch_review(args)
        elif args.command == 'host':
            from .host_cli import dispatch_host
            result = dispatch_host(args)
        elif args.command == 'source':
            from .source_cli import dispatch_source
            result = dispatch_source(args)
        elif args.command in ('capabilities', 'intent'):
            from .address_cli import dispatch_address
            result = dispatch_address(args)
        elif args.command in ('skills','context'):
            from .skill_cli import dispatch_skills_and_context
            result = dispatch_skills_and_context(args)
        elif args.command == 'assets':
            from .assets.cli import handle_assets
            result = handle_assets(args)
        elif args.command == 'hermes':
            import tempfile
            from .runtime import default_db_path
            from .integrations.hermes_session import run_hermes_session
            from .integrations.hermes_gate import evaluate_session
            db_path = Path(args.db) if args.db else default_db_path()
            required_request = read_json(args.request) if args.request else None
            context_sources = read_json(args.context_sources) if args.context_sources else []
            if isinstance(context_sources,dict):
                context_sources = context_sources.get('sources')
            if not isinstance(context_sources,list) or len(context_sources)>8:
                raise ValueError('context sources must be a list of at most eight source records')
            with tempfile.TemporaryDirectory(prefix='rpe-hermes-') as owned:
                result = run_hermes_session(args.prompt_file.read_text(encoding='utf-8'),
                    args.workspace, db_path,
                    tool_enabled=True, owner_root=Path(owned), timeout=args.timeout,
                    hermes_root=args.hermes_root, entry_mode=args.entry_mode,
                    required_request=required_request, max_task_attempts=args.max_task_attempts,
                    catalog_root=args.catalog_root,skill_snapshot_id=args.skill_snapshot_id,
                    context_sources=context_sources)
                if args.entry_mode == 'clean':
                    from .integrations.hermes_clean import evaluate_clean_session
                    verdict = evaluate_clean_session(result, db_path, args.workspace,
                        catalog_root=args.catalog_root)
                elif args.entry_mode == 'address':
                    from .integrations.hermes_address import evaluate_address_session
                    verdict = evaluate_address_session(result, db_path, args.workspace,
                        catalog_root=args.catalog_root, required_request=required_request)
                elif args.entry_mode == 'skills':
                    from .integrations.skill_gate import evaluate_skill_session
                    verdict = evaluate_skill_session(result,db_path,args.workspace,
                        catalog_root=args.catalog_root,required_request=required_request)
                else:
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
            elif args.evolution_command == 'worker':
                from .evolution.worker import run_proposal_worker
                from .path_binding import read_regular_file
                training_packet = (json.loads(read_regular_file(args.training_packet.expanduser().absolute(), max_bytes=65536).decode('utf-8'))
                                   if args.training_packet is not None else None)
                previous_candidate = (read_regular_file(args.previous_candidate.expanduser().absolute(), max_bytes=65536).decode('utf-8')
                                      if args.previous_candidate is not None else None)
                result = run_proposal_worker(db_path,args.owned_root,kind=args.kind,
                    timeout=args.timeout,hermes_root=args.hermes_root,training_packet=training_packet,
                    previous_candidate=previous_candidate,max_turns=args.max_turns,producer_only=args.producer_only)
                result['status'] = 'proposed' if result.get('proposal_id') else 'failed'
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
            if args.catalog_root:
                serve_argv += ['--catalog-root', str(args.catalog_root)]
            if args.skill_snapshot_id:
                serve_argv += ['--skill-snapshot-id', args.skill_snapshot_id]
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
                if args.runs_command == 'cancel':
                    result = runtime.cancel(args.run_id, reason=args.reason)
                elif args.runs_command == 'show':
                    result = runtime.store.get(args.run_id)
                else:
                    result = {'runs': runtime.store.list(args.limit)}
            else:
                state, decision = runtime.route(read_json(args.request))
                result = {'state': state.to_dict(), 'level': decision.level.name,
                          'candidates': [s.skill_id for s in decision.candidates],
                          'fallback_to_llm': decision.fallback_to_llm, 'reason': decision.reason}
        if (args.command == 'review' and not getattr(args, 'json', False)
                and isinstance(result.get('summary'), str)):
            print(result['summary'])
        else:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if args.command == 'runs':
            return 2 if args.runs_command == 'cancel' and result.get('status') == 'conflict' else 0
        if (args.command == 'evolution' and
                args.evolution_command in ('list', 'show', 'experiences', 'propose')):
            return 0
        status = result.get('status')
        if args.command in ('context', 'intent', 'capabilities', 'source') or (
                args.command == 'review' and args.review_command != 'run'):
            if status == 'unverified':
                return 1
            if status in ('unsupported', 'rejected', 'conflict'):
                return 2
            if status in ('needs_context', 'ambiguous'):
                return 3
            return 0
        if status in ['received', 'running']:
            return 3
        return 2 if status == 'rejected' else 1 if status in ['failed', 'interrupted', 'cancelled', 'unverified'] or result.get('passed') is False else 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({'status': 'error', 'error_code': 'event_conflict' if isinstance(exc, EventConflict) else 'invalid_request', 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
