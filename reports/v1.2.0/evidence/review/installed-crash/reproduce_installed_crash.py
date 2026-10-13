"""Run with owned/release-venv/bin/python -I -B from this directory.
All generated fixtures, SQLite files and reports stay beneath this script.
Fault injection uses the installed package; verification uses its public CLI.
"""
from __future__ import annotations
import csv
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
import rpe_harness
from rpe_harness import contracts, execution, recovery

ROOT = Path(__file__).resolve().parent
REPOSITORY = Path('/Users/audrey/项目/rpe-harness').resolve()
RELEASE_PREFIX = ROOT.parents[1] / 'release-venv'
DRIVER = r'''import importlib.metadata,json,os,sys
import rpe_harness
from rpe_harness import execution
from rpe_harness.cli import main
from rpe_harness.storage import RunStore
phase,request_file,database=sys.argv[1:]
print(json.dumps({'package_path':rpe_harness.__file__,'version':importlib.metadata.version('rpe-harness'),'prefix':sys.prefix}),flush=True)
if phase=='after_execution':
    original=execution.prepare_deduplication
    def crash(**kwargs):
        original(**kwargs)
        os._exit(73)
    execution.prepare_deduplication=crash
elif phase=='before_publication_intent':
    original=RunStore.step
    def crash(self,run_id,stage,*args,**kwargs):
        if stage=='publish': os._exit(73)
        return original(self,run_id,stage,*args,**kwargs)
    RunStore.step=crash
elif phase=='after_publication':
    original=execution.publish_output
    def crash(prepared,plan=None):
        original(prepared,plan=plan)
        os._exit(73)
    execution.publish_output=crash
else:
    raise ValueError('unknown phase')
raise SystemExit(main(['run','--request',request_file,'--db',database]))
'''

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def owned_command(arguments, cwd):
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    temporary = cwd / 'tmp'
    temporary.mkdir(exist_ok=True)
    env.update(PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
    process = subprocess.Popen([sys.executable, '-I', '-B', *arguments], cwd=cwd,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        raise AssertionError('owned child exceeded 20-second budget')
    assert not stderr, 'unexpected child stderr: ' + stderr[-1000:]
    return process.returncode, stdout, process.pid

def cli(arguments, cwd):
    code, stdout, pid = owned_command(['-m', 'rpe_harness', *arguments], cwd)
    result = json.loads(stdout)
    assert isinstance(result, dict)
    return code, result, pid

def plan_sha(run):
    anchors = [s['evidence'] for s in run['steps'] if s['stage'] == 'plan']
    assert len(anchors) == 1
    return anchors[0]['plan_sha256']

def probe(phase, root):
    directory = root / phase
    directory.mkdir()
    source, output = directory / 'source.csv', directory / 'result.csv'
    source.write_text('订单号,备注\n001,"首条\n备注"\n001,后条\n002,另条\n', encoding='utf-8-sig', newline='')
    input_sha = sha(source)
    request = {'schema_version': 2, 'event_id': 'installed-crash-' + phase,
        'capability': 'tabular.deduplicate', 'goal': '按订单号保留首条',
        'constraints': ['preserve_original', 'preserve_order'],
        'execution': {'input_path': str(source), 'output_path': str(output), 'key_columns': ['订单号']}}
    request_path, database = directory / 'request.json', directory / 'runs.sqlite'
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    processes = []
    crash_code, stdout, pid = owned_command(['-c', DRIVER, phase, str(request_path), str(database)], directory)
    processes.append(pid)
    assert crash_code == 73
    installed = json.loads(stdout)
    assert Path(installed['package_path']).resolve().is_relative_to(RELEASE_PREFIX.resolve())
    assert not Path(installed['package_path']).resolve().is_relative_to(REPOSITORY)
    initial_output_sha = sha(output) if output.exists() else None
    code, listing, pid = cli(['runs', 'list', '--db', str(database)], directory)
    processes.append(pid)
    assert code == 0 and len(listing['runs']) == 1
    original = listing['runs'][0]
    assert original['status'] == 'interrupted'
    assert original['error']['code'] == 'owner_process_exited'
    stages = [s['stage'] for s in original['steps']]
    safe = phase != 'after_publication'
    assert ('publish' in stages) is (not safe)
    assert output.exists() is (not safe)
    assert sha(source) == input_sha
    if safe:
        code, recovered, pid = cli(['recover', original['run_id'], '--db', str(database)], directory)
        processes.append(pid)
        assert code == 0 and recovered['status'] == 'succeeded'
        assert recovered['retry_of'] == original['run_id']
        assert recovered['run_id'] != original['run_id']
        assert plan_sha(recovered) == plan_sha(original)
        assert recovered['verification']['passed'] is True
        assert recovered['artifact']['published'] is True
        artifact_sha = sha(output)
        assert artifact_sha == recovered['artifact']['sha256'] == recovered['verification']['output_sha256']
        expected = [['订单号', '备注'], ['001', '首条\n备注'], ['002', '另条']]
        assert list(csv.reader(io.StringIO(output.read_text(encoding='utf-8-sig'), newline=''))) == expected
        code, replay, pid = cli(['recover', original['run_id'], '--db', str(database)], directory)
        processes.append(pid)
        assert code == 0 and replay['status'] == 'succeeded'
        assert replay['run_id'] == recovered['run_id']
        assert sha(output) == artifact_sha
        terminal = {'recovery_status': recovered['status'], 'retry_of': recovered['retry_of'],
            'successor_run_id': recovered['run_id'], 'verification_passed': True,
            'manual_truth_passed': True, 'same_original_plan': True,
            'repeated_recovery_same_run': True, 'published_sha256': artifact_sha}
    else:
        codes = []
        for _ in range(2):
            code, refused, pid = cli(['recover', original['run_id'], '--db', str(database)], directory)
            processes.append(pid)
            codes.append(code)
            assert code == 2 and refused['status'] == 'error'
            assert refused['error_code'] == 'invalid_request'
            assert 'publication started or its outcome is unknown' in refused['error']
            assert sha(output) == initial_output_sha
        terminal = {'recovery_refused_twice': True, 'recovery_exit_codes': codes,
            'refusal_reason': 'publication_started_or_outcome_unknown',
            'published_sha256': initial_output_sha, 'artifact_unchanged': True}
    code, replay_original, pid = cli(['run', '--request', str(request_path), '--db', str(database)], directory)
    processes.append(pid)
    assert code == 1 and replay_original['status'] == 'interrupted'
    assert replay_original['run_id'] == original['run_id']
    code, listing, pid = cli(['runs', 'list', '--db', str(database)], directory)
    processes.append(pid)
    expected_count = 2 if safe else 1
    assert code == 0 and len(listing['runs']) == expected_count
    assert sha(source) == input_sha
    final_sha = sha(output)
    assert final_sha == terminal['published_sha256']
    removed = 0
    for staging in directory.glob('.rpe-*.csv'):
        assert staging.is_file() and not staging.is_symlink()
        staging.unlink()
        removed += 1
    assert sha(output) == final_sha
    alive = []
    for pid in processes:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        alive.append(pid)
    assert not alive
    return {'phase': phase, 'passed': True, 'actual_owner_exit_code': crash_code,
        'original_run_id': original['run_id'], 'original_status_after_restart': original['status'],
        'original_stage': original['stage'], 'original_plan_sha256': plan_sha(original),
        'publication_intent_persisted': 'publish' in stages,
        'output_present_after_crash': initial_output_sha is not None,
        'source_sha256': input_sha, 'source_preserved': True,
        'original_event_replay_same_run': True, 'original_event_replay_exit_code': 1,
        'durable_run_count': expected_count, 'known_child_processes': len(processes),
        'all_known_children_stopped': True, 'orphan_staging_removed': removed,
        'staging_cleanup_preserved_output': True, **terminal}

def main():
    assert Path.cwd().resolve().is_relative_to(ROOT)
    assert not Path.cwd().resolve().is_relative_to(REPOSITORY)
    assert sys.flags.isolated == 1 and sys.dont_write_bytecode is True
    assert Path(sys.prefix).resolve() == RELEASE_PREFIX.resolve()
    root = ROOT / ('run-' + time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    root.mkdir()
    report = {'experiment': 'installed-wheel-public-cli-owner-crash',
        'version': importlib.metadata.version('rpe-harness'), 'python': sys.executable,
        'isolated_python': True, 'outside_checkout': True, 'cwd': str(Path.cwd()),
        'root': str(root), 'model_or_api_calls': 0,
        'installed_modules': {module.__name__: {'path': str(Path(module.__file__).resolve()),
            'sha256': sha(module.__file__)} for module in (rpe_harness, contracts, execution, recovery)},
        'phases': [], 'passed': False}
    for phase in ('after_execution', 'before_publication_intent', 'after_publication'):
        try:
            report['phases'].append(probe(phase, root))
        except Exception as exc:
            report['phases'].append({'phase': phase, 'passed': False,
                'error_type': type(exc).__name__, 'error': str(exc)[:1500]})
    report['passed'] = len(report['phases']) == 3 and all(p['passed'] for p in report['phases'])
    evidence = ROOT / 'installed-crash-evidence.json'
    evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'phase_count': len(report['phases']), 'evidence': str(evidence), 'script': str(Path(__file__).resolve())}, ensure_ascii=False))
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
