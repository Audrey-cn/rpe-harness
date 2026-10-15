#!/usr/bin/env python3
"""One registered population, no pilot, replay, gold injection or extra model proof."""
import json
import hashlib
import subprocess
from pathlib import Path
import sys

sys.dont_write_bytecode = True
WORKTREE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKTREE/'src'))
REPORT = WORKTREE/'reports/v1.15.0'
OWNED = Path(json.loads((REPORT/'validation/owned.json').read_bytes())['root'])


def save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def main():
    from rpe_harness.experiments import review_v111_comparison as shared
    from rpe_harness.integrations.hermes_session import run_hermes_session
    root = OWNED/'comparison-runtime'
    shared._check_freeze(shared._load(root), True)
    marker = 'formal-resume-unattempted-claim.json' if '--resume-unattempted' in sys.argv else 'formal-runtime-claim.json'
    save(OWNED/marker, {'draft_sessions': 6, 'native_execute_sessions': 2,
         'pilot_sessions': 0, 'external_native_execute_sessions': 0, 'replays': 0})
    observations = OWNED/'observations-final'
    observations.mkdir(mode=0o700, exist_ok='--resume-unattempted' in sys.argv)
    keys = ('session_id', 'session_root', 'usage', 'elapsed_seconds', 'model_identity',
        'process_cleanup', 'events', 'completed', 'timed_out', 'exit_code', 'observer_errors',
        'original_prompt_sha256', 'prompt_sha256', 'catalog_snapshot_id', 'public_contract_sha256',
        'review_diagnostics')

    def captured(label, function):
        try:
            result = function()
        except Exception as error:
            observation = getattr(error, 'diagnostics', {})
            save(observations/(label+'.json'), {**{key: observation[key] for key in keys if key in observation},
                'launcher_error_type': type(error).__name__})
            raise
        observation = result.get('host_observation', result)
        save(observations/(label+'.json'), {key: observation[key] for key in keys if key in observation})
        if '-draft' in label:
            save(observations/(label+'-proposal.json'), result)
        return result

    settings_path = OWNED/'runtime-profile/host-settings.json'
    amendment = json.loads((REPORT/'runtime-profile-amendment.json').read_bytes())

    def settings():
        raw = settings_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != amendment['private_settings_sha256']:
            raise ValueError('registered private runtime settings changed')
        return json.loads(raw)

    def public(plan, trial):
        settings()
        def execute(command, **kwargs):
            return subprocess.run([*command, '--host-settings', str(settings_path),
                '--deny-read-root', str(OWNED/'comparison'),
                '--deny-read-root', str(settings_path.parent)], **kwargs)
        return captured(trial['trial_id']+'-draft', lambda: shared.public_review_draft(
            plan, trial, subprocess_runner=execute))

    def native(*args, **kwargs):
        kwargs['host_settings'] = settings()
        kwargs['deny_read_roots'] = [*kwargs.get('deny_read_roots', ()), OWNED/'comparison', settings_path.parent]
        label = Path(kwargs['owner_root']).name
        return captured(label, lambda: run_hermes_session(*args, **kwargs))

    summary = shared.run_v111_comparison(root, formal=True, native_runner=native, public_runner=public)
    save(OWNED/'formal-runtime-summary.json', summary)
    print(json.dumps({key: summary[key] for key in ('profile', 'formal_attempts', 'by_arm', 'comparison_ready', 'costs')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
