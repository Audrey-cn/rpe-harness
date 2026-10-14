#!/usr/bin/env python3
"""Validate installed maintenance with 105 real CLI tasks in a unique owned workspace.

Only standard-library code runs in the driver. Every Harness invocation uses
the supplied installed interpreter with -I; source PYTHONPATH is discarded.
SQLite is queried read-only for independent persistence checks, never to create runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid


SOURCE = b' { "n": 1.2300, "text": "\\u4e2d", "nested": [true, null, {"k": "v"}] } '
TRUTHS = {
    'minify': b'{"n":1.2300,"text":"\\u4e2d","nested":[true,null,{"k":"v"}]}\n',
    'pretty': b'{\n  "n": 1.2300,\n  "text": "\\u4e2d",\n  "nested": [\n'
              b'    true,\n    null,\n    {\n      "k": "v"\n    }\n  ]\n}\n',
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, required=True, help='installed wheel venv interpreter')
    parser.add_argument('--owned-root', type=Path, required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.python.is_absolute() or not args.python.is_file():
        parser.error('--python must be an absolute existing interpreter path')
    owned = args.owned_root.absolute()
    if owned.is_symlink():
        parser.error('--owned-root must not be a symlink')
    owned.mkdir(parents=True, exist_ok=True)
    run_root = owned / ('installed-maintenance-' + uuid.uuid4().hex)
    run_root.mkdir(mode=0o700)
    workspace = run_root / 'workspace'
    workspace.mkdir(mode=0o700)
    for name in ('home', 'tmp', 'cache', 'config', 'data'):
        (run_root / name).mkdir(mode=0o700)
    database = run_root / 'runs.sqlite'
    report_dir = args.report_dir.absolute()
    if report_dir.is_symlink():
        parser.error('--report-dir must not be a symlink')
    report_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONUSERBASE'):
        env.pop(key, None)
    env.update(HOME=str(run_root / 'home'), TMPDIR=str(run_root / 'tmp'),
               XDG_CACHE_HOME=str(run_root / 'cache'), XDG_CONFIG_HOME=str(run_root / 'config'),
               XDG_DATA_HOME=str(run_root / 'data'), PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    report = {'driver': 'installed-maintenance-105-real-cli', 'passed': False, 'task_count': 105,
              'run_root': str(run_root), 'interpreter': str(args.python), 'package': None,
              'runs': [], 'cycles': [], 'checks': {}, 'errors': []}
    started = time.monotonic()

    def check(name, passed):
        report['checks'][name] = bool(passed)

    def invoke(arguments, payload=None, *, tolerate_failure=False, timeout=60):
        result = subprocess.run([str(args.python), '-I', '-B', *arguments],
            input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            cwd=workspace, env=env, capture_output=True, text=True, timeout=timeout)
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError('CLI response is not an object')
        except ValueError:
            value = {'status': 'driver_cli_failure', 'returncode': result.returncode,
                     'stdout_sha256': sha256(result.stdout.encode()),
                     'stderr_sha256': sha256(result.stderr.encode())}
        if result.returncode and not tolerate_failure:
            report['errors'].append({'code': 'cli_failed', 'response': value,
                                     'stderr_sha256': sha256(result.stderr.encode())})
            raise RuntimeError('installed CLI failed with exit ' + str(result.returncode))
        return value

    def run_task(index):
        name = 'task-' + str(index).zfill(3)
        style = 'minify' if index % 2 == 0 else 'pretty'
        source, output = workspace / (name + '.json'), workspace / (name + '-output.json')
        source.write_bytes(SOURCE)
        request = {'schema_version': 2, 'event_id': name, 'goal': 'Preserve exact literal JSON tokens.',
                   'capability': 'json.normalize', 'execution': {'input_path': str(source),
                   'output_path': str(output), 'style': style}}
        value = invoke(['-m', 'rpe_harness', 'run', '--request', '-', '--db', str(database)],
                       request, tolerate_failure=True)
        actual = output.read_bytes() if output.is_file() and not output.is_symlink() else None
        item = {'index': index, 'event_id': name, 'style': style, 'run_id': value.get('run_id'),
                'status': value.get('status'), 'input_sha256': sha256(SOURCE),
                'output_sha256': sha256(actual) if actual is not None else None,
                'expected_output_sha256': sha256(TRUTHS[style]),
                'source_unchanged': source.read_bytes() == SOURCE,
                'literal_output_correct': actual == TRUTHS[style],
                'verification_passed': (value.get('verification') or {}).get('passed') is True}
        report['runs'].append(item)
        return item

    def cycle(phase):
        value = invoke(['-m', 'rpe_harness', 'evolution', 'cycle', '--db', str(database),
                        '--owned-root', str(run_root)])
        report['cycles'].append({'phase': phase, **value})
        state = value.get('maintenance') or {}
        check('cycle_' + str(len(report['cycles'])) + '_bounded',
              len(state.get('inspected_run_ids', [])) <= 100
              and len(state.get('rechecked_run_ids', [])) <= 100
              and type(state.get('discovered_run_count')) is int
              and state['discovered_run_count'] <= 100
              and type(state.get('scanned_host_count')) is int and state['scanned_host_count'] <= 100
              and type(value.get('proposal_count')) is int and value['proposal_count'] <= 32
              and value.get('budget') == {'max_proposals': 32, 'max_history_runs': 100}
              and state.get('busy') is False)
        return value

    def connect():
        db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return db

    def pointer():
        with connect() as db:
            row = db.execute("SELECT version_id,generation FROM evolution_active WHERE slot='recipe:json_recipe'").fetchone()
        return list(row) if row else None

    try:
        package = invoke(['-c', 'import hashlib,importlib.metadata,json,pathlib,sys,rpe_harness; '
            'p=pathlib.Path(rpe_harness.__file__).resolve(); '
            'print(json.dumps({"path":str(p),"prefix":sys.prefix,"version":rpe_harness.__version__, '
            '"distribution_version":importlib.metadata.version("rpe-harness"), '
            '"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}))'])
        report['package'] = package
        installed = (Path(package['path']).is_relative_to(Path(package['prefix']).resolve())
                     and 'site-packages' in Path(package['path']).parts)
        check('imports_installed_wheel_without_source_pythonpath', installed)
        if not installed:
            raise RuntimeError('target interpreter imported source outside its installed prefix')
        first = run_task(0)
        cycle('first-anchor')
        before = pointer()
        check('first_verified_recipe_activated', bool(before and before[0] and before[1] == 1))
        for index in range(1, 105):
            run_task(index)
        for index in range(10):
            current = cycle('history-over-100-batch-' + str(index + 1))
            if current['maintenance']['has_more'] is False:
                break
        else:
            raise RuntimeError('bounded maintenance failed to drain in ten cycles')
        check('all_105_real_cli_tasks_succeeded', len(report['runs']) == 105
              and all(item['status'] == 'succeeded' and item['verification_passed'] for item in report['runs']))
        check('independent_literal_outputs_and_sources', all(item['literal_output_correct']
              and item['source_unchanged'] for item in report['runs']))
        identifiers = {item['run_id'] for item in report['runs']}
        check('all_run_ids_unique', len(identifiers) == 105 and None not in identifiers)
        with connect() as db:
            durable = db.execute('SELECT run_id,status,verification_ok FROM runs ORDER BY event_fk').fetchall()
            feedback = db.execute('SELECT run_id,payload_json,payload_hash FROM evolution_feedback').fetchall()
            queue = db.execute("SELECT subject_id,state FROM evolution_maintenance_queue WHERE kind='run'").fetchall()
            cursors = dict(db.execute('SELECT name,cursor FROM evolution_maintenance_state'))
            max_event = db.execute('SELECT max(id) FROM events').fetchone()[0]
            pending = db.execute("SELECT count(*) FROM evolution_maintenance_queue WHERE state IN ('pending','waiting')").fetchone()[0]
            deferred = db.execute("SELECT count(*) FROM evolution_maintenance_queue WHERE json_extract(payload_json,'$.deferred_apply')=1").fetchone()[0]
            ledger = db.execute("SELECT source_id,evidence_hash,payload_json FROM evolution_maintenance_evidence WHERE source_kind='run'").fetchall()
            proposals = db.execute('SELECT proposal_id,kind,state,payload_json FROM evolution_proposals').fetchall()
            active = db.execute("SELECT p.payload_json FROM evolution_active a JOIN evolution_versions v ON a.version_id=v.version_id JOIN evolution_proposals p ON v.proposal_id=p.proposal_id WHERE a.slot='recipe:json_recipe'").fetchone()
            activations = db.execute("SELECT count(*) FROM evolution_activations WHERE state='applied'").fetchone()[0]
        check('sqlite_has_exact_real_verified_run_ids', len(durable) == 105
              and {row['run_id'] for row in durable} == identifiers
              and all(row['status'] == 'succeeded' and row['verification_ok'] == 1 for row in durable))
        feedback_hashes = {row['run_id']: row['payload_hash'] for row in feedback}
        check('all_feedback_preserved_with_independent_hashes', len(feedback) == 105
              and set(feedback_hashes) == identifiers
              and all(sha256(canonical(json.loads(row['payload_json'])).encode()) == row['payload_hash'] for row in feedback))
        check('run_queue_complete_without_pending_or_waiting', len(queue) == 105
              and {row['subject_id'] for row in queue} == identifiers
              and all(row['state'] == 'done' for row in queue) and pending == 0 and deferred == 0)
        check('discovery_cursor_reaches_last_real_event', cursors.get('runs') == max_event == 105)
        check('ledger_retains_every_original_verified_reference', {row['source_id'] for row in ledger} == identifiers
              and all(row['evidence_hash'] == feedback_hashes[row['source_id']]
                      and json.loads(row['payload_json'])['sha256'] == row['evidence_hash'] for row in ledger))
        check('diagnostic_semantics_stable_across_105_runs', sum(row['kind'] == 'diagnostic' for row in proposals) == 7)
        check('recipes_keep_single_first_evidence_anchor', all(len(json.loads(row['payload_json'])['evidence_run_ids']) == 1
              for row in proposals if row['kind'] == 'json_recipe'))
        check('old_active_anchor_and_generation_unchanged', pointer() == before and active is not None
              and json.loads(active['payload_json'])['evidence_run_ids'] == [first['run_id']]
              and activations == 1)
        fresh = invoke(['-c', 'import json,sys; from rpe_harness.evolution.cycle import inspect_verified_feedback; '
            'ids=json.load(sys.stdin)["ids"]; items=[inspect_verified_feedback(i,sys.argv[1]) for i in ids]; '
            'print(json.dumps({"fresh":[{"run_id":i["run_id"],"output_sha256":i["output_sha256"]} '
            'for i in items if i is not None]}))', str(database)], {'ids': sorted(identifiers)})
        check('all_105_old_and_new_artifacts_freshly_reverified',
              {item['run_id'] for item in fresh['fresh']} == identifiers
              and {item['run_id']: item['output_sha256'] for item in fresh['fresh']}
              == {item['run_id']: item['expected_output_sha256'] for item in report['runs']})
        repeated = cycle('repeat-idle')
        with connect() as db:
            after_count = db.execute('SELECT count(*) FROM evolution_proposals').fetchone()[0]
            after_activations = db.execute("SELECT count(*) FROM evolution_activations WHERE state='applied'").fetchone()[0]
        check('repeat_idle_without_new_proposal_or_activation', repeated['maintenance']['has_more'] is False
              and repeated['proposal_count'] == 0 and repeated['applied'] == []
              and after_count == len(proposals) and after_activations == activations and pointer() == before)
        report['durable_counts'] = {'runs': len(durable), 'feedback': len(feedback), 'run_queue': len(queue),
            'distinct_ledger_run_refs': len({row['source_id'] for row in ledger}),
            'proposals': len(proposals), 'activations': activations, 'pending': pending, 'deferred': deferred}
        report['cursor'] = cursors
        report['passed'] = all(report['checks'].values()) and not report['errors']
    except (ValueError, KeyError, OSError, sqlite3.Error, RuntimeError, subprocess.SubprocessError) as error:
        report['errors'].append({'code': 'validation_failed', 'type': type(error).__name__,
                                 'message': str(error)[:512]})
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    destination = report_dir / ('installed-maintenance-' + run_root.name.rsplit('-', 1)[-1] + '.json')
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'passed': report['passed'], 'report_path': str(destination),
                      'run_root': str(run_root), 'checks_passed': sum(report['checks'].values()),
                      'checks_total': len(report['checks']), 'elapsed_seconds': report['elapsed_seconds']}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
