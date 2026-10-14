"""Literal truths for cross-process reuse; this is not a learning-benefit estimate."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import sys


def run_sequence(root, *, candidate_path):
    from ..evolution import EvolutionStore, apply_candidate, cycle, propose_candidate, rollback, validate_candidate
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    database = root / 'runs.sqlite'
    cases, processes = [], 0
    def progress(report=None):
        (root / 'sequence-progress.json').write_text(json.dumps(
            report or {'passed': False, 'cases': cases, 'process_restarts': processes},
            ensure_ascii=False, indent=2) + '\n')
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(root),
                       TMP=str(root), TEMP=str(root))
    # Point subprocesses to precisely the currently imported package (source or installed).
    import rpe_harness
    environment['PYTHONPATH'] = str(Path(rpe_harness.__file__).parent.parent)
    def task(name, raw, capability, expected, *, style=None):
        nonlocal processes
        suffix = '.json' if capability == 'json.normalize' else '.csv'
        source, output = root / (name + '-input' + suffix), root / (name + '-output' + suffix)
        source.write_bytes(raw)
        execution = {'input_path': str(source), 'output_path': str(output)}
        execution.update(style=style) if style else execution.update(key_columns=['id'])
        request = {'schema_version': 2, 'event_id': name, 'goal': '固定真值能力复用验收',
                   'capability': capability, 'execution': execution}
        request_file = root / (name + '-request.json')
        request_file.write_text(json.dumps(request, ensure_ascii=False))
        processes += 1
        case = {'name': name, 'capability': capability, 'style': style, 'returncode': None,
                'status': 'unknown', 'truth_passed': False, 'run_id': None,
                'actual_executor': None, 'artifact_sha256': None}
        try:
            process = subprocess.run([sys.executable, '-B', '-m', 'rpe_harness', 'run',
                '--request', str(request_file), '--db', str(database)], env=environment,
                capture_output=True, text=True, timeout=30)
            case['returncode'] = process.returncode
            result = json.loads(process.stdout)
            if capability == 'json.normalize':
                truth = output.is_file() and output.read_bytes() == expected
            else:
                with output.open(encoding='utf-8-sig', newline='') as stream:
                    truth = list(csv.reader(stream)) == expected
            truth = truth and source.read_bytes() == raw
            case.update(status=result['status'], truth_passed=truth, run_id=result['run_id'],
                actual_executor=(result.get('artifact') or {}).get('actual_executor'),
                artifact_sha256=(result.get('artifact') or {}).get('sha256'))
        except Exception as error:
            case['error_type'] = type(error).__name__
            cases.append(case)
            progress()
            raise
        cases.append(case)
        progress()
        if process.returncode or result['status'] != 'succeeded' or not truth:
            raise RuntimeError('reuse sequence case failed: ' + name)
        return result
    seed = task('csv-seed', b'id,value\n01,first\n01,later\n02,last\n', 'tabular.deduplicate',
                [['id', 'value'], ['01', 'first'], ['02', 'last']])
    code = Path(candidate_path).read_text(encoding='utf-8')
    proposal = propose_candidate(database, root, component='executor', kind='csv_executor',
        content=code, evidence_run_ids=[seed['run_id']])
    if not validate_candidate(proposal['proposal_id'], database)['passed']:
        raise RuntimeError('candidate did not pass independent acceptance')
    active = apply_candidate(proposal['proposal_id'], database)
    for number in range(3):
        key = f'00{number}'
        raw = f'id,value\n{key},首条\n{key},重复\nX{number},末条\n'.encode('utf-8-sig')
        result = task('csv-reuse-' + str(number), raw, 'tabular.deduplicate',
            [['id', 'value'], [key, '首条'], ['X' + str(number), '末条']])
        if result['artifact']['actual_executor'].get('candidate_version_id') != active['version_id']:
            raise RuntimeError('new process did not reuse the active candidate')
    task('json-pretty', b' {"a": [1.2300, true], "b": "\\u4e2d"} ', 'json.normalize',
         b'{\n  "a": [\n    1.2300,\n    true\n  ],\n  "b": "\\u4e2d"\n}\n', style='pretty')
    task('json-minify', b' [ 9007199254740993, 1e999, -0, " a b " ] ', 'json.normalize',
         b'[9007199254740993,1e999,-0," a b "]\n', style='minify')
    task('json-new-input', b' { "fresh": { }, "x": false } ', 'json.normalize',
         b'{"fresh":{},"x":false}\n', style='minify')
    # This owned immutable candidate is deliberately corrupted; production must quarantine it.
    artifact = Path(active['artifact_path'])
    artifact.chmod(0o600)
    artifact.write_text('invalid owned test candidate')
    fallback = task('csv-corrupt-fallback', b'id,value\nA,keep\nA,drop\n', 'tabular.deduplicate',
                    [['id', 'value'], ['A', 'keep']])
    store = EvolutionStore(database)
    quarantined = store.get_version(active['version_id'])['state'] == 'quarantine'
    # A separately certified owned variant passes all fixed fixtures, but makes
    # a wrong new production result. This tests the production oracle itself.
    anchor = '    with open(staging_path'
    if anchor not in code:
        raise ValueError('reuse experiment candidate must use the documented example writer')
    bad_code = code.replace(anchor,
        "    if plan['output_path'].endswith('csv-bad-output-output.csv'):\n        kept=kept[:1]\n" + anchor, 1)
    bad = propose_candidate(database, root, component='executor', kind='csv_executor',
        content=bad_code, evidence_run_ids=[seed['run_id']])
    if not validate_candidate(bad['proposal_id'], database)['passed']:
        raise RuntimeError('production-only variant failed independent fixture acceptance')
    bad_active = apply_candidate(bad['proposal_id'], database)
    bad_result = task('csv-bad-output', b'id,value\nC,first\nD,second\nC,drop\n',
                      'tabular.deduplicate', [['id', 'value'], ['C', 'first'], ['D', 'second']])
    fallback_step = next((s['evidence'] for s in bad_result['steps']
                          if s['stage'] == 'candidate_fallback'), {})
    bad_rejected = fallback_step.get('verification', {}).get('passed') is False
    bad_quarantined = store.get_version(bad_active['version_id'])['state'] == 'quarantine'
    same_plan = fallback_step.get('plan_sha256') == bad_result['artifact']['plan_sha256']
    replacement = propose_candidate(database, root, component='executor', kind='csv_executor',
        content=code + '\n# separately verified recovery variant\n', evidence_run_ids=[seed['run_id']])
    if not validate_candidate(replacement['proposal_id'], database)['passed']:
        raise RuntimeError('recovery variant failed independent acceptance')
    replacement_active = apply_candidate(replacement['proposal_id'], database)
    rollback(replacement_active['version_id'], database)
    after_rollback = task('csv-rollback', b'id,value\nB,keep\nB,drop\n', 'tabular.deduplicate',
                          [['id', 'value'], ['B', 'keep']])
    cycle(database, root, auto_apply=False)
    experiences = store.list_experiences()
    if not experiences:
        raise RuntimeError('verified reuse did not preserve experience lookup')
    experience = experiences[0]
    store.set_experience_state(experience['experience_id'], 'dormant')
    dormant = any(x['experience_id'] == experience['experience_id'] and x['state'] == 'dormant'
                  for x in EvolutionStore(database).lookup_experiences(experience['lookup_key']))
    reuse_count = sum(x['actual_executor'].get('candidate_version_id') == active['version_id'] for x in cases)
    report = {'passed': all(x['truth_passed'] for x in cases) and quarantined and dormant,
        'scope': 'literal-truth tasks across separate CLI processes, quarantine, rollback and lookup',
        'cases': cases, 'process_restarts': processes, 'candidate_reuse_count': reuse_count,
        'failed_candidate_quarantined': quarantined,
        'fallback_used_builtin': fallback['artifact']['actual_executor']['kind'] == 'builtin',
        'rollback_used_builtin': after_rollback['artifact']['actual_executor']['kind'] == 'builtin',
        'dormant_lookup_retained': dormant, 'learning_benefit_estimated': False,
        'bad_output_oracle_rejected': bad_rejected,
        'bad_output_candidate_quarantined': bad_quarantined,
        'bad_output_fallback_same_plan': same_plan,
        'bad_output_fallback_evidence': fallback_step}
    report['passed'] = report['passed'] and report['fallback_used_builtin'] and report['rollback_used_builtin']
    report['passed'] = report['passed'] and bad_rejected and bad_quarantined and same_plan
    progress(report)
    return report
