#!/usr/bin/env python3
"""One registered population, no pilot, replay, gold injection or extra model proof."""
import json
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
    root = OWNED/'comparison'
    shared._check_freeze(shared._load(root), True)
    save(OWNED/'formal-claim.json', {'draft_sessions': 6, 'native_execute_sessions': 2,
         'pilot_sessions': 0, 'external_native_execute_sessions': 0, 'replays': 0})
    observations = OWNED/'observations'
    observations.mkdir(mode=0o700)
    keys = ('session_id', 'session_root', 'usage', 'elapsed_seconds', 'model_identity',
        'process_cleanup', 'events', 'completed', 'timed_out', 'exit_code', 'observer_errors',
        'original_prompt_sha256', 'prompt_sha256', 'catalog_snapshot_id', 'public_contract_sha256',
        'review_diagnostics')

    def captured(label, function):
        try:
            result = function()
        except Exception as error:
            observation = getattr(error, 'diagnostics', {})
            save(observations/(label+'.json'), {key: observation[key] for key in keys if key in observation})
            raise
        observation = result.get('host_observation', result)
        save(observations/(label+'.json'), {key: observation[key] for key in keys if key in observation})
        if 'proposal_verification' in result:
            save(observations/(label+'-proposal.json'), result)
        return result

    def public(plan, trial):
        return captured(trial['trial_id']+'-draft', lambda: shared.public_review_draft(plan, trial))

    def native(*args, **kwargs):
        label = Path(kwargs['owner_root']).name
        return captured(label, lambda: run_hermes_session(*args, **kwargs))

    summary = shared.run_v111_comparison(root, formal=True, native_runner=native, public_runner=public)
    save(OWNED/'formal-summary.json', summary)
    print(json.dumps({key: summary[key] for key in ('profile', 'formal_attempts', 'by_arm', 'comparison_ready', 'costs')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
