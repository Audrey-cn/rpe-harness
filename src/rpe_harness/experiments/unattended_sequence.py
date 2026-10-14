"""Bounded, literal-truth unattended lifecycle acceptance; no autonomous code claim."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


_CONTROL = '''import json,os,sys
from pathlib import Path
from rpe_harness.evolution import EvolutionStore,cycle,propose_candidate,validate_candidate,apply_candidate,rollback
packet=json.load(sys.stdin)
database,root=Path(packet['database']),Path(packet['root'])
operation=packet['operation']
if operation=='cycle':
    path=packet.get('candidate_path')
    def proposer(context):
        evidence=[item['run_id'] for item in context['verified_feedback']
                  if item['plan']['capability']=='json.normalize']
        return [{'component':'executor','kind':'json_executor',
                 'content':Path(path).read_text(encoding='utf-8'),'evidence_run_ids':evidence}]
    result=cycle(database,root,auto_apply=packet.get('auto_apply',True),
                 proposer=proposer if path else None)
elif operation=='validated-proposal':
    result=propose_candidate(database,root,component='executor',kind='json_executor',
        content=Path(packet['candidate_path']).read_text(encoding='utf-8'),
        evidence_run_ids=packet['evidence_run_ids'])
    validation=validate_candidate(result['proposal_id'],database)
    if validation.get('passed') is not True:
        raise RuntimeError('owned proposal failed independent JSON acceptance')
    result=EvolutionStore(database).get_proposal(result['proposal_id'])
elif operation=='apply':
    checkpoint=packet.get('checkpoint')
    def crash(stage):
        if stage==checkpoint: os._exit(packet['exit_code'])
    result=apply_candidate(packet['proposal_id'],database,_checkpoint=crash if checkpoint else None)
elif operation=='inspect':
    store=EvolutionStore(database)
    with store.connect() as db:
        activations=[dict(row) for row in db.execute(
            'SELECT activation_id,version_id,state FROM evolution_activations ORDER BY created_at')]
    result={'executor_pointer':list(store.pointer('executor','json_executor')),
            'recipe_pointer':list(store.pointer('recipe','json_recipe')),
            'versions':[{'version_id':item['version_id'],'state':item['state']} for item in store.list_versions()],
            'activations':activations}
elif operation=='corrupt':
    store=EvolutionStore(database)
    version=store.get_version(packet['version_id'])
    artifact=store.owned_path(version['artifact_path'])
    artifact.chmod(0o600)
    artifact.write_text('invalid owned test artifact')
    result={'owned_artifact_corrupted':True,'version_id':version['version_id']}
elif operation=='rollback':
    result=rollback(packet['version_id'],database)
elif operation=='dormant':
    from rpe_harness.experiments.unattended_sequence import _dormant_result
    result=_dormant_result(database,packet['expected_fallbacks'])
else:
    raise ValueError('unknown fixed experiment operation')
print(json.dumps(result,ensure_ascii=False,allow_nan=False))
'''


def _stop_owned_process_tree(process):
    """Stop this still-owned child and its observed descendants on timeout."""
    # Candidate sandboxes have their own process group. Capture descendants while
    # the parent is alive, then stop descendants before the owned parent itself.
    try:
        snapshot = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid='], capture_output=True,
                                  text=True, timeout=5, check=True)
        edges = []
        for line in snapshot.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                edges.append(tuple(map(int, parts)))
        descendants = {process.pid}
        while True:
            expanded = descendants | {pid for pid, parent in edges if parent in descendants}
            if expanded == descendants:
                break
            descendants = expanded
        for pid in sorted(descendants - {process.pid}, reverse=True):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('owned process inventory or descendant cleanup failed') from error
    finally:
        # Inventory failure cannot strand the exact child owned by this driver.
        try:
            process.kill()
        finally:
            process.wait()


def _dormant_result(database, expected_fallbacks):
    """The existing final child checks lookup and bound durable failure facts."""
    from ..evolution import EvolutionStore
    store = EvolutionStore(database)
    experience = next(item for item in store.list_experiences()
                      if item['lookup_key'].startswith('json.normalize:json:'))
    store.set_experience_state(experience['experience_id'], 'dormant')
    retained = any(item['experience_id'] == experience['experience_id'] and item['state'] == 'dormant'
                   for item in EvolutionStore(database).lookup_experiences(experience['lookup_key']))
    facts = []
    for proposal in store.list_proposals(limit=1000):
        if proposal['component'] != 'executor' or proposal['kind'] != 'diagnostic':
            continue
        content = json.loads(store.read_artifact(proposal))
        facts.extend(item for item in content.get('problems', [])
                     if item.get('kind') == 'candidate_fallback'
                     and item.get('verification_passed') is False)
    fields = ('run_id', 'candidate_version_id', 'plan_sha256', 'evidence_sha256')
    matched = [expected for expected in expected_fallbacks
               if any(all(expected.get(key) is not None and fact.get(key) == expected[key]
                          for key in fields) for fact in facts)]
    return {'retained': retained,
            'diagnostics_matched': bool(expected_fallbacks) and len(matched) == len(expected_fallbacks),
            'matched_candidate_failures': matched}


def run_sequence(root, *, candidate_path, max_steps=32, step_timeout=60):
    """Run a fixed cross-process JSON upgrade/recovery sequence without a service.

    candidate_path is explicitly externally supplied code. It receives the same
    trusted JSON suite and production oracle as any other proposal.
    """
    if type(max_steps) is not int or not 1 <= max_steps <= 32:
        raise ValueError('step budget must be a positive integer <=32')
    if type(step_timeout) not in {int, float} or not 0 < step_timeout <= 60:
        raise ValueError('timeout budget must be in (0,60] seconds')
    from ..path_binding import _directory, read_source
    submitted = Path(root).expanduser().absolute()
    descriptor = _directory(submitted.parent)
    os.close(descriptor)
    submitted.mkdir(exist_ok=False)
    root = submitted.resolve()
    database = root / 'runs.sqlite'
    attempts, cases, checks = [], [], {}
    cycle_count = 0
    import rpe_harness
    home = root / 'child-home'
    home.mkdir()
    environment = dict(os.environ, HOME=str(home), TMPDIR=str(root), TMP=str(root), TEMP=str(root),
                       PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPATH=str(Path(rpe_harness.__file__).resolve().parent.parent))

    def progress(**extra):
        report = {'passed': False, 'scope': 'bounded cross-process JSON lifecycle acceptance',
                  'attempts': attempts, 'cases': cases, 'checks': checks,
                  'process_restarts': len(attempts), 'cycle_count': cycle_count,
                  'budget': {'max_steps': max_steps, 'step_timeout': step_timeout,
                             'max_proposals_per_cycle': 32, 'max_history_runs': 100},
                  'proposal_origin': 'external_injected_code', 'autonomous_code_generation': False,
                  'learning_benefit_estimated': False, **extra}
        (root / 'unattended-progress.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        return report

    def child(name, command, *, packet=None, expected_exit=0):
        if len(attempts) >= max_steps:
            raise RuntimeError('unattended step budget exhausted before ' + name)
        attempt = {'name': name, 'returncode': None, 'expected_exit': expected_exit,
                   'passed': False, 'timeout': False}
        attempts.append(attempt)
        progress()
        started = time.monotonic()
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=environment, cwd=root, start_new_session=True)
        try:
            stdout, stderr = process.communicate(
                json.dumps(packet) if packet is not None else None, timeout=step_timeout)
        except subprocess.TimeoutExpired:
            attempt['timeout'] = True
            cleanup_error = None
            try:
                _stop_owned_process_tree(process)
            except Exception as error:
                cleanup_error = error
                attempt['cleanup_error_type'] = type(error).__name__
            attempt.update(returncode=process.returncode, elapsed_s=round(time.monotonic() - started, 3))
            progress(error_type='TimeoutExpired', failed_step=name)
            if cleanup_error is not None:
                raise RuntimeError('unattended timeout cleanup failed') from cleanup_error
            raise
        attempt.update(returncode=process.returncode, elapsed_s=round(time.monotonic() - started, 3))
        if process.returncode != expected_exit:
            attempt['stderr_excerpt'] = stderr[-1000:]
            progress(error_type='ChildProcessError', failed_step=name)
            raise ChildProcessError('unattended child failed: ' + name)
        result = json.loads(stdout) if expected_exit == 0 else {'expected_crash': expected_exit}
        attempt['passed'] = True
        progress()
        return result

    def control(name, operation, **options):
        expected_exit = options.pop('expected_exit', 0)
        return child(name, [sys.executable, '-B', '-c', _CONTROL],
            packet={'operation': operation, 'database': str(database), 'root': str(root), **options},
            expected_exit=expected_exit)

    def cli_cycle(name):
        nonlocal cycle_count
        cycle_count += 1
        return child(name, [sys.executable, '-B', '-m', 'rpe_harness', 'evolution', 'cycle',
                     '--db', str(database), '--owned-root', str(root)])

    def injected_cycle(name, path, *, auto_apply=True):
        nonlocal cycle_count
        cycle_count += 1
        return control(name, 'cycle', candidate_path=str(path) if path else None, auto_apply=auto_apply)

    def task(name, raw, style, expected):
        source, output = root / (name + '-input.json'), root / (name + '-output.json')
        source.write_bytes(raw)
        request = root / (name + '-request.json')
        request.write_text(json.dumps({'schema_version': 2, 'event_id': name,
            'goal': 'fixed independent JSON byte truth', 'capability': 'json.normalize',
            'execution': {'input_path': str(source), 'output_path': str(output), 'style': style}}))
        result = child(name, [sys.executable, '-B', '-m', 'rpe_harness', 'run',
                       '--db', str(database), '--request', str(request)])
        truth = output.is_file() and output.read_bytes() == expected and source.read_bytes() == raw
        cases.append({'name': name, 'style': style, 'truth_passed': truth,
                      'status': result['status'], 'run_id': result['run_id'],
                      'actual_executor': (result.get('artifact') or {}).get('actual_executor'),
                      'plan_sha256': (result.get('artifact') or {}).get('plan_sha256')})
        progress()
        if result['status'] != 'succeeded' or not truth:
            raise RuntimeError('literal JSON truth failed: ' + name)
        return result

    def active_from_cycle(result):
        activation = next((item for item in result['applied'] if item['kind'] == 'json_executor'), None)
        if activation is None:
            raise RuntimeError('external JSON code was not automatically activated')
        return activation

    def variant(name, code):
        path = root / (name + '.py')
        path.write_text(code, encoding='utf-8')
        return path

    try:
        seed = task('json-seed', b' { "a": 1.2300, "b": "\\u4e2d" } ', 'minify',
                    b'{"a":1.2300,"b":"\\u4e2d"}\n')
        first = cli_cycle('default-auto-recipe')
        recipe = next((item for item in first['applied'] if item['kind'] == 'json_recipe'), None)
        checks['default_recipe_auto_activated'] = recipe is not None
        repeat = cli_cycle('same-evidence-cycle')
        checks['repeated_cycle_did_not_reactivate'] = not repeat['applied']
        code = read_source(candidate_path, max_bytes=65536).decode('utf-8')
        good_path = variant('external-good', code)
        proposed = child('independent-cli-proposal', [sys.executable, '-B', '-m', 'rpe_harness',
            'evolution', 'propose', '--db', str(database), '--owned-root', str(root),
            '--component', 'executor', '--kind', 'json_executor', '--candidate', str(good_path),
            '--evidence-run-id', seed['run_id']])
        good_cycle = cli_cycle('consume-persisted-code-proposal')
        active = active_from_cycle(good_cycle)
        checks['persisted_cli_proposal_auto_applied'] = (
            proposed['proposal_id'] in good_cycle['validated'] and
            any(item['proposal_id'] == proposed['proposal_id'] and
                item['origin'] == 'external_persisted_proposal' for item in good_cycle['proposal_origins']))
        pretty = task('candidate-pretty', b' [1e999, -0, " a b ", {}] ', 'pretty',
                      b'[\n  1e999,\n  -0,\n  " a b ",\n  {}\n]\n')
        minify = task('candidate-minify', b' {"n":9007199254740993,"a":[ true, null ]} ', 'minify',
                      b'{"n":9007199254740993,"a":[true,null]}\n')
        checks['both_styles_used_candidate'] = all(item['artifact']['actual_executor'].get(
            'candidate_version_id') == active['version_id'] for item in (pretty, minify))
        grow = injected_cycle('new-evidence-same-code-cycle', good_path)
        checks['new_evidence_did_not_reactivate'] = not grow['applied']
        intent_path = variant('intent-recovery', code + '\n# independent intent recovery variant\n')
        intent = control('validate-intent-variant', 'validated-proposal', candidate_path=str(intent_path),
                         evidence_run_ids=[seed['run_id']])
        control('crash-after-intent', 'apply', proposal_id=intent['proposal_id'],
                checkpoint='intent_persisted', exit_code=73, expected_exit=73)
        recovered = control('restart-aborts-orphan-intent', 'inspect')
        checks['intent_crash_aborted_without_switch'] = (
            recovered['executor_pointer'][0] == active['version_id'] and
            any(item['version_id'] == intent['version_id'] and item['state'] == 'aborted'
                for item in recovered['activations']))
        intent_active = control('apply-recovered-intent', 'apply', proposal_id=intent['proposal_id'])
        commit_path = variant('commit-recovery', code + '\n# independent commit recovery variant\n')
        commit = control('validate-commit-variant', 'validated-proposal', candidate_path=str(commit_path),
                         evidence_run_ids=[seed['run_id']])
        control('crash-after-commit', 'apply', proposal_id=commit['proposal_id'],
                checkpoint='activated', exit_code=74, expected_exit=74)
        committed = control('restart-keeps-committed-pointer', 'inspect')
        checks['commit_crash_retained_applied'] = (committed['executor_pointer'][0] == commit['version_id']
            and any(item['version_id'] == commit['version_id'] and item['state'] == 'applied'
                    for item in committed['activations']))
        commit_task = task('commit-reuse', b' {"v":-0.00} ', 'minify', b'{"v":-0.00}\n')
        checks['restart_reused_committed_candidate'] = commit_task['artifact']['actual_executor'].get(
            'candidate_version_id') == commit['version_id']
        bad_code = code + '''\n_original_prepare = prepare
def prepare(plan, staging_path):
    if plan['output_path'].endswith(('bad-output-output.json', 'bad-output-2-output.json')):
        with open(staging_path, 'wb') as handle:
            handle.write(b'null\\n')
    else:
        _original_prepare(plan, staging_path)
'''
        bad_path = variant('production-only-wrong', bad_code)
        bad_active = active_from_cycle(injected_cycle('auto-upgrade-production-variant', bad_path))
        bad_result = task('bad-output', b'{"must":"preserve"}', 'minify', b'{"must":"preserve"}\n')
        failure = next(step['evidence'] for step in bad_result['steps'] if step['stage'] == 'candidate_fallback')
        after_bad = control('inspect-previous-restoration', 'inspect')
        checks['bad_output_rejected_and_quarantined'] = (failure['verification']['passed'] is False
            and any(item['version_id'] == bad_active['version_id'] and item['state'] == 'quarantine'
                    for item in after_bad['versions']))
        checks['bad_output_same_plan_builtin'] = (failure['plan_sha256'] == bad_result['artifact']['plan_sha256']
            and bad_result['artifact']['actual_executor']['kind'] == 'builtin')
        checks['previous_pointer_restored'] = after_bad['executor_pointer'][0] == commit['version_id']
        previous_task = task('previous-reuse', b' [false, 1.0] ', 'minify', b'[false,1.0]\n')
        checks['next_task_reused_previous'] = previous_task['artifact']['actual_executor'].get(
            'candidate_version_id') == commit['version_id']
        manual = control('manual-rollback', 'rollback', version_id=commit['version_id'])
        checks['manual_rollback_restored_previous'] = manual['restored_version_id'] == intent_active['version_id']
        rollback_task = task('rollback-reuse', b' {"rollback": true} ', 'minify', b'{"rollback":true}\n')
        checks['rollback_reused_previous'] = rollback_task['artifact']['actual_executor'].get(
            'candidate_version_id') == intent_active['version_id']
        bad_second = variant('production-only-second', bad_code + '\n# independent invalid-previous variant\n')
        bad_second_active = active_from_cycle(injected_cycle('upgrade-before-invalid-previous', bad_second))
        control('corrupt-owned-previous', 'corrupt', version_id=intent_active['version_id'])
        invalid = task('bad-output-2', b'{"fresh":2}', 'minify', b'{"fresh":2}\n')
        invalid_failure = next(step['evidence'] for step in invalid['steps'] if step['stage'] == 'candidate_fallback')
        checks['invalid_previous_restored_builtin'] = (
            invalid_failure['verification']['passed'] is False and
            invalid_failure['quarantine'].get('restored_version_id') is None and
            invalid_failure['quarantine']['version_id'] == bad_second_active['version_id'])
        builtin_task = task('builtin-after-invalid', b' {"builtin": []} ', 'pretty',
                            b'{\n  "builtin": []\n}\n')
        checks['next_task_used_builtin'] = builtin_task['artifact']['actual_executor']['kind'] == 'builtin'
        injected_cycle('final-diagnostics-only-cycle', None, auto_apply=False)
        from ..evolution.store import digest
        expected_fallbacks = [{'run_id': result['run_id'],
            'candidate_version_id': version['version_id'],
            'plan_sha256': evidence['plan_sha256'], 'evidence_sha256': digest(evidence)}
            for result, version, evidence in [(bad_result, bad_active, failure),
                                              (invalid, bad_second_active, invalid_failure)]]
        final = control('dormant-lookup', 'dormant', expected_fallbacks=expected_fallbacks)
        checks['diagnostics_recorded_after_fallback'] = final['diagnostics_matched']
        checks['dormant_json_lookup_retained'] = final['retained']
        reuse_count = sum(case['actual_executor'].get('kind') == 'candidate' for case in cases)
        return progress(passed=all(checks.values()) and all(case['truth_passed'] for case in cases),
                        candidate_reuse_count=reuse_count,
                        candidate_failure_diagnoses=final['matched_candidate_failures'])
    except Exception as error:
        progress(error_type=type(error).__name__)
        raise
