#!/usr/bin/env python3
"""Offline record, package and artifact checks; no provider calls or deleted keys."""
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.dont_write_bytecode = True
REPORT = Path(__file__).resolve().parent
WORKTREE = REPORT.parents[1]
sys.path.insert(0, str(WORKTREE/'src'))


def read(name):
    return json.loads((REPORT/name).read_bytes())


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    from rpe_harness.experiments.review_v115_comparison import verify_v115_evidence
    errors = []
    proof, scope, trial = read('validation/wheel-proof.json'), read('new-task-scope.json'), read('new-task-result.json')
    package = WORKTREE/'src/rpe_harness'
    modules = {str(p.relative_to(package)): sha(p.read_bytes()) for p in package.rglob('*.py')}
    if modules != proof['source_modules'] or modules != scope['source_environment']['source_sha256']:
        errors.append('source_wheel_model_freeze_differs')
    wheel = REPORT/'release/rpe_harness-1.15.1-py3-none-any.whl'
    if sha(wheel.read_bytes()) != proof['wheel_sha256']:
        errors.append('wheel_hash_changed')
    with zipfile.ZipFile(wheel) as archive:
        if any(sha(archive.read('rpe_harness/'+name)) != digest for name, digest in modules.items()):
            errors.append('wheel_modules_changed')
    portable = verify_v115_evidence(REPORT/'evidence')
    if portable['verified'] is not True:
        errors.append('portable_evidence_changed')
    summary = read('evidence/summary.json')
    if any(trial[key] != summary[key] for key in ('by_arm', 'pairs', 'costs', 'comparison_ready')):
        errors.append('smoke_summary_changed')
    if trial['counters'] != {'review_drafts': 1, 'native_drafts': 1, 'native_executions': 0}:
        errors.append('registered_smoke_counts_changed')
    # This freezes the observed outcome, never declares all future probes must pass.
    if trial['comparison_ready'] is not False or len(trial['outcomes']) != 2:
        errors.append('partial_smoke_relabelled_complete_benchmark')
    result_rows = []
    for row in trial['outcomes']:
        observed = next(item for item in summary['records'] if item['arm'] == row['arm'])
        if any(row[key] != observed[key] for key in ('accepted', 'approved', 'source_preserved', 'comparable')):
            errors.append('smoke_outcome_changed')
        path = REPORT/'evidence/artifacts'/('formal-warehouse-picklist-new-'+row['arm'])/'output.csv'
        correct = None
        if path.exists():
            raw = path.read_bytes()
            correct = (not raw.startswith(b'\xef\xbb\xbf') and list(csv.reader(io.StringIO(raw.decode('utf-8'))))
                       == scope['task']['expected_rows'])
        if correct != row['business_output_correct_independent']:
            errors.append('independent_business_oracle_changed')
        result_rows.append({'arm': row['arm'], 'accepted': row['accepted'], 'artifact_correct': correct})
    tests = {}
    for name in ('full-suite', 'package'):
        tree = ET.parse(REPORT/'validation'/(name+'.xml')).getroot()
        suites = [tree] if tree.tag == 'testsuite' else tree.findall('testsuite')
        counts = {key: sum(int(suite.get(key, 0)) for suite in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
        tests[name] = counts
        if counts['failures'] or counts['errors']:
            errors.append(name+'_not_green')
    manifest = read('manifest.json')
    for name, digest in manifest['files'].items():
        if sha((REPORT/name).read_bytes()) != digest:
            errors.append('retained_file_changed:'+name)
    preservation = read('cleanup.json')['preservation']
    if preservation['owned_cleanup_and_session_isolation_verified'] is not True:
        errors.append('owned_cleanup_incomplete')
    result = {'verified': not errors, 'errors': errors, 'tests': tests, 'source_modules': len(modules),
        'delivery_record_consistency_verified': not any(error != 'full-suite_not_green' for error in errors),
        'full_suite_green': not tests['full-suite']['failures'] and not tests['full-suite']['errors'],
        'full_model_and_final_test_environment_equivalence_verified': read('validation/final-source-proof.json')['full_recorded_environment_equality'],
        'ready_for_unattended_release': False,
        'new_task_outcomes': result_rows, 'portable_records': portable,
        'strict_daily_content_preservation': preservation['strict_daily_content_preservation'],
        'runtime_replayed': False, 'deleted_keys_reauthenticated': False,
        'verified_billing': None, 'human_confirmation_verified': False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
