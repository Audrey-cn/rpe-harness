"""Register the explicitly budgeted JSON diagnostic pair; never run a model."""
from pathlib import Path
import json
import sqlite3
import sys
import time

from rpe_harness.experiments.address_comparison import _save
from rpe_harness.experiments.review_comparison import (
    _check_freeze, _new_trial, preregister_review_comparison,
)

OWNED = Path('/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2')
TARGET = OWNED/'comparison-contract-check'


def main():
    # Deliberately register-only and fresh-root-only. This script must not be
    # used to rewrite a frozen registration or rerun either previous population.
    if len(sys.argv)!=3:
        raise SystemExit('usage: register-contract-check.py SKILL_ROOT HERMES_ROOT')
    if TARGET.exists():
        raise SystemExit('diagnostic_registration_already_exists')
    started = time.monotonic()
    plan = preregister_review_comparison(TARGET, skill_root=sys.argv[1],
        hermes_root=sys.argv[2], helper_python=sys.executable, timeout=120, max_turns=8)
    case = next(row for row in plan['cases'] if row['case_id']=='json-lossless')
    pilots = [_new_trial(TARGET, case, arm, 'pilot', plan['helper_python'], None)
              for arm in ('native','review')]
    for trial in pilots:
        trial['db'] = str(TARGET/'databases'/(trial['trial_id']+'.sqlite'))
        with sqlite3.connect(plan['db']) as source, sqlite3.connect(trial['db']) as destination:
            source.backup(destination)
    plan.update(pilots=pilots, sessions=[],
        experiment_role='post_diagnostic_smoke', selected_case_ids=['json-lossless'],
        additional_budget_reason=('After preserving the completed original pilot and formal negatives, '
            'test one publicly diagnosed JSON contract/schema repair on a separately selected pair. '
            'This pair is not a formal rerun, replacement, or population-quality estimate.'),
        population_policy='Selected JSON diagnostic pair only; report independently from the original formal comparison.',
        formal_session_limit=0,pilot_session_limit=2,max_draft_sessions=2,
        max_native_execution_sessions=1,max_host_sessions=3,
        one_shot_policy='One pair only; no further repair, retry, or replacement after these observations.',
        diagnostic_registration_timer_definition=(
            'Whole adaptation wall time including ordinary preregistration; excludes the final registration write. '
            'Overlaps the shared preregistration phase and must not be added to it.'))
    plan['coverage_limits'] += [
        'This registration selects only json-lossless after observing prior failures; it cannot revise formal outcomes.',
        'All six public cases remain available for portable-verifier compatibility; only the selected pilot pair may run.',
        'No formal sessions exist in this registration. Use run without --formal, at most two draft sessions.',
        'No source, fixture, scorer, public-contract or model change is permitted after this registration is frozen.']
    _check_freeze(plan)
    plan['diagnostic_registration_elapsed_seconds'] = time.monotonic()-started
    plan.pop('sha256')
    (TARGET/'preregistration.json').unlink()
    frozen = _save(TARGET/'preregistration.json', plan)
    print(json.dumps({'root':str(TARGET),'fingerprint':frozen['sha256'],
        'experiment_role':frozen['experiment_role'],'selected_case_ids':frozen['selected_case_ids'],
        'draft_session_limit':2,'native_execution_session_limit':1,
        'formal_sessions':len(frozen['sessions'])},ensure_ascii=False))


if __name__=='__main__':
    main()
