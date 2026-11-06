#!/usr/bin/env python3
"""Run one XLSX pair under a caller-owned root; default to offline registration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import site
import stat
import subprocess
import sys

import rpe_harness
from rpe_harness.experiments import review_v111_comparison as shared
from rpe_harness.experiments import review_v115_comparison as profile
from rpe_harness.experiments.address_comparison import _environment, score_address_artifact
from rpe_harness.experiments.review_v114_comparison import _materialize, request_budget_profile
from rpe_harness.integrations.hermes_session import _load_host_settings, run_hermes_session
from rpe_harness.path_binding import read_regular_file

WORKTREE = Path(__file__).resolve().parents[3]
TASK = {
    'case_id': 'stock-acceptance-xlsx', 'capability': 'xlsx.inspect',
    'task': '恢复并读取真实电子表格技能。只读 input.xlsx，按本次附件决定的模式和工作表，用原始 xlsx_read.py 把完整 JSON 另存 output.json。保留输入文件、原行序和 JSON 类型，不重写 helper；要求清单标明附件来源。',
    'parameters': {'mode': 'rows', 'sheet': '验货'},
    'sources': [{'source': 'user-stock-acceptance', 'event_id': 'approved-inspection',
                 'payload': {'text': '本次验货：mode=rows；sheet=验货。'},
                 'facts': {'mode': 'rows', 'sheet': '验货'}}],
    'expected': {'sheet': '验货', 'rows': [['箱号', '净重', '验收'],
                                      ['0014', 9, True], ['14', 2.5, False]]},
}


def save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def read(path):
    return json.loads(path.read_bytes())


def snapshot(hermes):
    cfg = (Path(sys.prefix)/'pyvenv.cfg').read_bytes()
    if (site.ENABLE_USER_SITE is not False
            or b'include-system-site-packages = false' not in cfg.splitlines()
            or os.environ.get('PYTHONPATH') != str(WORKTREE/'src')
            or Path(rpe_harness.__file__).resolve() != WORKTREE/'src/rpe_harness/__init__.py'):
        raise ValueError('isolated validation interpreter required')
    return {'fingerprint': _environment(sys.executable, hermes),
            'source': str(WORKTREE/'src'), 'user_site_enabled': False,
            'venv_config_sha256': hashlib.sha256(cfg).hexdigest()}


def register(root, skills, hermes):
    original = profile.fixtures

    def fixtures(directory):
        cases = original(directory)
        cases[0] = _materialize(directory, json.loads(json.dumps(TASK)))
        return cases

    profile.fixtures = fixtures
    try:
        plan = shared.preregister_v111_comparison(root, skill_root=skills,
            hermes_root=hermes, helper_python=sys.executable, profile='review-v115')
    finally:
        profile.fixtures = original
    return plan, shared.freeze_formal(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('owned_root', type=Path)
    parser.add_argument('--run-model', action='store_true')
    parser.add_argument('--snapshot', action='store_true')
    args = parser.parse_args()
    o = args.owned_root.resolve()
    if (o.parent != Path('/private/tmp') or not o.name.startswith('rpe-v1153.')
            or read(o/'owner.json') != {'owned_root': str(o), 'purpose': 'rpe-v1153-hermes-compatibility'}
            or Path(sys.prefix).resolve() != o/'venv' or o.stat().st_mode & 0o077):
        raise ValueError('unrecognized private validation root')
    os.umask(0o077)
    hermes = Path.home()/'.hermes/hermes-agent'
    skills = Path.home()/'.hermes/skills'
    if args.snapshot:
        save(o/'before-validation.json', snapshot(hermes))
        return
    run = o/('model-pair' if args.run_model else 'offline-registration')
    run.mkdir(mode=0o700)  # Exclusive claim: an existing attempt cannot be replayed.
    before = snapshot(hermes)
    save(run/'before.json', before)
    state = {'phase': 'registration', 'attempts': {'review': 0, 'native': 0, 'native_execute': 0},
             'model_replayed': False, 'human_confirmation_verified': False, 'verified_billing': None}
    try:
        root = run/'trial'
        plan, frozen = register(root, skills, hermes)
        selected = {row['arm']: row for row in plan['sessions'][:2]}
        if (frozen['environment'] != before['fingerprint'] or set(selected) != {'review', 'native'}
                or any(row['case_id'] != TASK['case_id'] for row in selected.values())):
            raise ValueError('unexpected frozen environment or pair')
        state.update(phase='registered', preregistered_trials=len(plan['sessions']),
                     draft_limit=2, native_execution_limit=1,
                     skill_sha256=plan['skill_sha256'], script_sha256=plan['script_sha256'])
        if not args.run_model:
            return
        if before != read(o/'before-validation.json'):
            raise ValueError('model run differs from tested environment')
        state['phase'] = 'settings'
        settings_path = o/'private/host-settings.json'
        mode = settings_path.lstat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077 or settings_path.parent.is_symlink():
            raise ValueError('private regular host settings required')
        frozen_settings = read_regular_file(settings_path, max_bytes=16384)
        requested = request_budget_profile()

        def settings():
            if read_regular_file(settings_path, max_bytes=16384) != frozen_settings:
                raise ValueError('host settings changed')
            config = _load_host_settings(settings_path)
            if any(config[key] != requested[key] for key in ('model', 'provider')):
                raise ValueError('host settings differ from budget')
            return config

        secret = settings()['api_key']
        home = Path.home()
        denied = [settings_path.parent, Path(__file__).resolve(), run/'evidence',
                  *(home/name for name in ('.ssh', '.aws', '.claude', '.claude.json', '.config/gh',
                                            '.codex/auth.json', '.codex/memories', '.codex/sessions',
                                            'Desktop', 'Documents', '项目'))]

        def reserve(key):
            if state['attempts'][key]:
                raise ValueError('session budget exhausted')
            state['attempts'][key] = 1

        def public(plan_arg, trial):
            if trial != selected['review']:
                raise ValueError('unregistered review trial')
            reserve('review')
            settings()
            invoked = False

            def launch(command, **kwargs):
                nonlocal invoked
                if invoked or command[:6] != [sys.executable, '-B', '-m', 'rpe_harness', 'review', 'draft']:
                    raise ValueError('unexpected public entry')
                invoked = True
                flags = [part for path in denied for part in ('--deny-read-root', str(path))]
                return subprocess.run([*command, '--host-settings', str(settings_path), *flags], **kwargs)

            return shared.public_review_draft(plan_arg, trial, subprocess_runner=launch)

        def native(prompt, workspace, database, **kwargs):
            trial = selected['native']
            owner = Path(kwargs['owner_root'])
            executing = owner.name == trial['trial_id']+'-execute'
            if (owner.parent != root/'sessions' or owner.name not in {
                    trial['trial_id']+'-draft', trial['trial_id']+'-execute'}
                    or Path(workspace) != Path(trial['workspace']) or database != trial['db']
                    or kwargs.get('max_task_attempts') != 1
                    or executing and (not state['attempts']['native'] or not kwargs.get('native_execution_evidence'))):
                raise ValueError('unregistered native trial')
            reserve('native_execute' if executing else 'native')
            kwargs['host_settings'] = settings()
            kwargs['deny_read_roots'] = (*kwargs.get('deny_read_roots', ()), *denied)
            return run_hermes_session(prompt, workspace, database, **kwargs)

        state['phase'] = 'model'
        try:
            shared.run_v111_comparison(root, formal=True, max_sessions=2,
                                        native_runner=native, public_runner=public)
        except Exception as error:
            state['driver_error_type'] = type(error).__name__
        state['phase'] = 'export'
        evidence = run/'evidence'
        result = shared.export_v111_evidence(root, evidence)
        variants = [secret.encode(), json.dumps(secret, ensure_ascii=False)[1:-1].encode()]
        if any(any(value in path.read_bytes() for value in variants)
               for path in evidence.rglob('*') if path.is_file()):
            raise ValueError('private value in evidence; keep output owned')
        state['portable'] = profile.verify_v115_evidence(evidence)
        state['comparison_ready'] = result['comparison_ready']
        state['observed_costs'] = result['costs']
        state['outcomes'] = [{**{key: row.get(key) for key in
                             ('arm', 'approved', 'accepted', 'execution_verified', 'comparable', 'error_code')},
                             **score_address_artifact(shared._gate_plan(plan), selected[row['arm']])}
                             for row in result['records']]
        state['phase'] = 'recorded'
    except Exception as error:
        state['error_type'] = type(error).__name__
        raise SystemExit(1) from None
    finally:
        try:
            after = snapshot(hermes)
            save(run/'after.json', after)
            state['environment_unchanged'] = before == after
        except Exception as error:
            state.update(environment_unchanged=False, snapshot_error_type=type(error).__name__)
        save(run/'result.json', state)
        print(json.dumps(state, ensure_ascii=False))


if __name__ == '__main__':
    main()
