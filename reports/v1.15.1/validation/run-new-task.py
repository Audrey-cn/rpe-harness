#!/usr/bin/env python3
"""One new paired smoke, never a complete six-session benchmark or a replay."""
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
WORKTREE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKTREE/'src'))
REPORT = WORKTREE/'reports/v1.15.1'
OWNED = Path(json.loads((REPORT/'validation/baseline.json').read_bytes())['owned_root'])
TASK = {
    'case_id': 'warehouse-picklist-new', 'capability': 'tabular.deduplicate',
    'task': '把 input.csv 按仓区和领料单号的组合去重，0007与7是不同领料单号。保留首条整行和原行顺序，使用UTF-8编码、逗号分隔另存 output.csv，原文件保留。',
    'source_text': '仓区,领料单号,件数\nA区,0007,2\nB区,0007,5\nA区,0007,99\nA区,7,3\n',
    'parameters': {'key_columns': ['仓区', '领料单号'], 'encoding': 'utf-8', 'delimiter': ',', 'keep': 'first'},
    'expected_rows': [['仓区', '领料单号', '件数'], ['A区', '0007', '2'], ['B区', '0007', '5'], ['A区', '7', '3']]}


def save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def main():
    from rpe_harness.experiments import review_v111_comparison as shared
    from rpe_harness.experiments import review_v115_comparison as profile
    from rpe_harness.experiments.review_v114_comparison import _materialize
    from rpe_harness.integrations.hermes_session import run_hermes_session, _load_host_settings
    settings_path = OWNED/'private/host-settings.json'
    settings_sha = hashlib.sha256(settings_path.read_bytes()).hexdigest()
    _load_host_settings(settings_path)
    save(OWNED/'new-task-attempt.json', {'draft_sessions': 2, 'native_execution_sessions': 1,
        'task_attempts': 1, 'replays': 0, 'private_settings_sha256': settings_sha})
    original = profile.fixtures
    def fixtures(directory):
        cases = original(directory)
        cases[0] = _materialize(directory, json.loads(json.dumps(TASK)))
        return cases
    profile.fixtures = fixtures
    root = OWNED/'new-task'
    plan = shared.preregister_v111_comparison(root, skill_root=Path.home()/'.hermes/skills',
        hermes_root=Path.home()/'.hermes/hermes-agent', helper_python=sys.executable, profile='review-v115')
    profile.fixtures = original
    frozen = shared.freeze_formal(root)
    save(REPORT/'new-task-scope.json', {'task': TASK, 'source_environment': frozen['environment'],
        'registered_sessions': len(plan['sessions']), 'selected_trial_ids': [t['trial_id'] for t in plan['sessions'][:2]],
        'draft_limit': 2, 'native_execution_limit': 1, 'replays': 0,
        'other_registered_pairs': 'unattempted; this is a partial smoke, comparison_ready remains false',
        'private_settings_sha256': settings_sha, 'confirmation': 'simulated_semantic_reviewer_not_human'})
    observations = OWNED/'new-task-observations'
    observations.mkdir(mode=0o700)
    public_keys = ('session_id', 'session_root', 'usage', 'elapsed_seconds', 'model_identity',
        'process_cleanup', 'events', 'completed', 'timed_out', 'exit_code', 'observer_errors',
        'original_prompt_sha256', 'prompt_sha256', 'catalog_snapshot_id', 'public_contract_sha256',
        'review_diagnostics')
    counters = {'review_drafts': 0, 'native_drafts': 0, 'native_executions': 0}
    def settings():
        if hashlib.sha256(settings_path.read_bytes()).hexdigest() != settings_sha:
            raise ValueError('registered runtime settings changed')
        return _load_host_settings(settings_path)
    def captured(label, function):
        try:
            value = function()
        except Exception as error:
            observed = getattr(error, 'diagnostics', {})
            save(observations/(label+'.json'), {**{k: observed[k] for k in public_keys if k in observed},
                'launcher_error_type': type(error).__name__})
            raise
        observed = value.get('host_observation', value)
        save(observations/(label+'.json'), {k: observed[k] for k in public_keys if k in observed})
        return value
    def public(plan, trial):
        counters['review_drafts'] += 1
        if counters['review_drafts'] != 1:
            raise ValueError('public draft budget exhausted')
        settings()
        def execute(command, **kwargs):
            return subprocess.run([*command, '--host-settings', str(settings_path)], **kwargs)
        return captured(trial['trial_id']+'-draft', lambda: shared.public_review_draft(plan, trial, subprocess_runner=execute))
    def native(*args, **kwargs):
        label = Path(kwargs['owner_root']).name
        counter = 'native_executions' if label.endswith('-execute') else 'native_drafts'
        counters[counter] += 1
        if counters[counter] != 1:
            raise ValueError('native session budget exhausted')
        kwargs['host_settings'] = settings()
        kwargs['deny_read_roots'] = [*kwargs.get('deny_read_roots', ()), settings_path.parent]
        return captured(label, lambda: run_hermes_session(*args, **kwargs))
    summary = shared.run_v111_comparison(root, formal=True, max_sessions=2,
                                         native_runner=native, public_runner=public)
    shared.export_v111_evidence(root, REPORT/'evidence')
    portable = profile.verify_v115_evidence(REPORT/'evidence')
    outcomes = []
    for record in summary['records']:
        trial = next(t for t in plan['sessions'] if t['trial_id'] == record['trial_id'])
        path = Path(trial['workspace'])/'output.csv'
        correct = None
        if path.exists():
            raw = path.read_bytes()
            correct = not raw.startswith(b'\xef\xbb\xbf') and list(csv.reader(io.StringIO(raw.decode('utf-8')))) == TASK['expected_rows']
        outcomes.append({'arm': trial['arm'], 'accepted': record['accepted'],
            'approved': record['approved'], 'business_output_correct_independent': correct,
            'source_preserved': record['source_preserved'], 'comparable': record['comparable'],
            'error_code': record.get('error_code')})
    result = {'counters': counters, 'outcomes': outcomes, 'costs': summary['costs'],
        'by_arm': summary['by_arm'], 'pairs': summary['pairs'], 'comparison_ready': summary['comparison_ready'],
        'portable_verification': portable, 'model_replayed': False, 'verified_billing': None}
    save(REPORT/'new-task-result.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
