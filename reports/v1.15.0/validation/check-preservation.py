#!/usr/bin/env python3
"""Read-only comparison with this round's fresh baseline; never restores daily state."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def check():
    baseline = json.loads((HERE/'preservation-baseline.json').read_bytes())
    hashes = set(json.loads((HERE/'test-session-ids.json').read_bytes())['session_id_sha256'])
    result = {'read_only': True, 'state_restored': False, 'baseline_files': len(baseline['files']),
              'unchanged_files': 0, 'changed_files': [], 'mutable_daily_drift': [], 'errors': []}
    for name, wanted in baseline['files'].items():
        try:
            if sha(Path(name).read_bytes()) == wanted:
                result['unchanged_files'] += 1
            else:
                result['mutable_daily_drift' if name in baseline['mutable_daily_files'] else 'changed_files'].append(name)
        except OSError as error:
            result['errors'].append({'stage': 'file', 'path': name, 'error': type(error).__name__})
    result['skill_changes'] = []
    root = Path(baseline['skill_root'])
    current_names = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    result['skill_inventory_same'] = current_names == set(baseline['skills'])
    result['skill_added_files'] = sorted(current_names-set(baseline['skills']))
    result['skill_removed_files'] = sorted(set(baseline['skills'])-current_names)
    for name, wanted in baseline['skills'].items():
        try:
            path = root/name
            link = os.readlink(path) if path.is_symlink() else None
            if link != wanted['link'] or sha(path.read_bytes()) != wanted['sha256']:
                result['skill_changes'].append(name)
        except OSError:
            result['skill_changes'].append(name)
    result['repositories'] = {}
    integration_path = HERE.parent/'integration.json'
    integration = json.loads(integration_path.read_bytes()) if integration_path.exists() else None
    for name, wanted in baseline['repositories'].items():
        def git(args):
            return subprocess.check_output(['git', '-C', name, *args], text=True).strip()
        head_ok = git(['rev-parse', 'HEAD']) == wanted['head']
        if integration and name == integration['main_repository']:
            source = integration['release_source_commit']
            ancestor = subprocess.run(['git','-C',name,'merge-base','--is-ancestor',source,'HEAD']).returncode == 0
            changed = [path for path in git(['diff','--name-only','-z',source,'HEAD']).split('\0') if path]
            allowed = all(path=='README.md' or path.startswith(('docs/','reports/')) for path in changed)
            package = Path(name)/'src/rpe_harness'
            modules = {str(p.relative_to(package)):sha(p.read_bytes()) for p in package.rglob('*.py')}
            head_ok = bool(integration['baseline_head']==wanted['head'] and ancestor and allowed
                           and modules == integration['source_modules'])
        result['repositories'][name] = {'head_preserved_or_authorized_release': head_ok,
                                      'original_untracked_status_unchanged': git(['status', '--porcelain']) == wanted['status']}
    leaks = set()
    path = Path(baseline['state_db_path'])
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as database:
        database.execute('PRAGMA query_only=ON')
        for (table,) in database.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            quoted = '"'+table.replace('"', '""')+'"'
            columns = [row[1] for row in database.execute('PRAGMA table_info('+quoted+')')]
            for column in columns:
                if column not in {'session_id', 'parent_session_id', 'child_session_id'}:
                    continue
                for (value,) in database.execute('SELECT "'+column+'" FROM '+quoted+' WHERE "'+column+'" IS NOT NULL'):
                    if type(value) is str and sha(value.encode()) in hashes:
                        leaks.add(sha(value.encode()))
    for name in baseline['daily_directories']:
        directory = Path(baseline['daily_root'])/name
        if directory.is_dir():
            for parent, dirs, files in os.walk(directory, followlinks=False):
                for item in dirs+files:
                    for candidate in (item, Path(item).stem):
                        if sha(candidate.encode()) in hashes:
                            leaks.add(sha(candidate.encode()))
    result['daily_test_session_leaks'] = sorted(leaks)
    result['test_session_hash_count'] = len(hashes)
    owned = baseline['owned_root']
    result['owned_absent'] = not os.path.lexists(owned)
    processes = subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True)
    result['owned_process_pids'] = [int(line.strip().split(None, 1)[0]) for line in processes.splitlines() if owned in line]
    result['preserved'] = bool(not result['errors'] and not result['changed_files']
        and not result['skill_changes'] and result['skill_inventory_same']
        and all(all(row.values()) for row in result['repositories'].values())
        and not leaks and result['owned_absent'] and not result['owned_process_pids'])
    result['owned_cleanup_and_test_isolation_verified'] = bool(not result['errors']
        and not result['changed_files'] and all(all(row.values()) for row in result['repositories'].values())
        and not leaks and result['owned_absent'] and not result['owned_process_pids'])
    result['daily_skill_drift_observed'] = bool(result['skill_changes'] or not result['skill_inventory_same'])
    return result


if __name__ == '__main__':
    result = check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['preserved'] else 1)
