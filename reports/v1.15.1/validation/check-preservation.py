#!/usr/bin/env python3
"""Read-only daily-state audit; optional removal is limited to this owned root."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
REPORT = Path(__file__).resolve().parents[1]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def git(root, *arguments):
    return subprocess.check_output(['git', '-C', root, *arguments], text=True).strip()


def owned_processes(owned):
    return [int(parts[0]) for line in subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True).splitlines()
            if len(parts := line.strip().split(None, 1)) == 2 and str(owned) in parts[1]]


def check():
    baseline = read(REPORT/'validation/baseline.json')
    owned = Path(baseline['owned_root'])
    changed, errors = [], []
    for name, digest in baseline['daily_files'].items():
        try:
            if sha(Path(name).read_bytes()) != digest:
                changed.append(name)
        except OSError as error:
            errors.append({'path': name, 'error': type(error).__name__})
    skills = Path(baseline['skill_root'])
    current = {str(p.relative_to(skills)): sha(p.read_bytes()) for p in skills.rglob('*') if p.is_file()}
    skill_changes = sorted(name for name, digest in baseline['skills'].items() if current.get(name) != digest)
    added = sorted(set(current)-set(baseline['skills']))
    integration = REPORT/'integration.json'
    integrated = read(integration) if integration.exists() else None
    repositories = {}
    for name, original in baseline['repositories'].items():
        head_ok = git(name, 'rev-parse', 'HEAD') == original['head']
        if integrated and name == integrated['main_repository']:
            release = integrated['release_source_commit']
            changes = git(name, 'diff', '--name-only', '-z', release, 'HEAD').split('\0')
            modules = {str(p.relative_to(Path(name)/'src/rpe_harness')): sha(p.read_bytes())
                       for p in (Path(name)/'src/rpe_harness').rglob('*.py')}
            head_ok = bool(subprocess.run(['git', '-C', name, 'merge-base', '--is-ancestor', release, 'HEAD']).returncode == 0
                and all(not p or p == 'README.md' or p.startswith(('docs/', 'reports/')) for p in changes)
                and modules == read(REPORT/'validation/wheel-proof.json')['source_modules'])
        repositories[name] = {'head_preserved_or_authorized_integration': head_ok,
                             'original_untracked_status_unchanged': git(name, 'status', '--porcelain') == original['status']}
    hashes = set(read(REPORT/'validation/test-session-ids.json')['sha256'])
    leaks = set()
    db = Path.home()/'.hermes/state.db'
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            quoted = '"'+table.replace('"', '""')+'"'
            for row in connection.execute('PRAGMA table_info('+quoted+')'):
                if row[1] not in {'session_id', 'parent_session_id', 'child_session_id'}:
                    continue
                column = '"'+row[1]+'"'
                for (value,) in connection.execute('SELECT '+column+' FROM '+quoted+' WHERE '+column+' IS NOT NULL'):
                    if type(value) is str and sha(value.encode()) in hashes:
                        leaks.add(sha(value.encode()))
    for name in ('cache', 'history', 'logs', 'sessions'):
        for parent, directories, files in os.walk(Path.home()/'.hermes'/name, followlinks=False):
            for item in directories+files:
                for candidate in (item, Path(item).stem):
                    if sha(candidate.encode()) in hashes:
                        leaks.add(sha(candidate.encode()))
    absent, pids = not os.path.lexists(owned), owned_processes(owned)
    result = {'daily_files_checked': len(baseline['daily_files']), 'daily_files_changed': changed,
        'skill_files_checked': len(baseline['skills']), 'skill_files_changed_or_removed': skill_changes,
        'skill_files_added': added, 'errors': errors, 'repositories': repositories,
        'test_session_hashes_checked': len(hashes), 'daily_test_session_leaks': sorted(leaks),
        'session_check_scope': 'Hermes database session-ID columns and daily directory entry names; not a full content scan',
        'owned_absent': absent, 'owned_process_pids': pids, 'daily_state_restored': False}
    result['owned_cleanup_and_session_isolation_verified'] = bool(absent and not pids and not leaks and not errors)
    result['strict_daily_content_preservation'] = bool(not changed and not skill_changes and not added and not errors
        and all(all(row.values()) for row in repositories.values()))
    return result


def main():
    if '--remove-owned' in sys.argv:
        baseline = read(REPORT/'validation/baseline.json')
        owned = Path(baseline['owned_root'])
        if (owned.parent != Path('/Users/audrey/项目/缓存/test') or owned.is_symlink()
                or read(owned/'owner.json') != {'schema': 'rpe.maintenance-owned/v1', 'root': str(owned)}
                or owned_processes(owned)):
            raise ValueError('owned cleanup identity/process guard failed')
        files = size = links = 0
        for path in owned.rglob('*'):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                links += 1
            elif stat.S_ISREG(info.st_mode):
                files += 1
                size += info.st_size
        shutil.rmtree(owned)
        result = check()
        value = {'owned_root': str(owned), 'removed_regular_files': files, 'removed_regular_bytes': size,
                 'removed_symlinks': links, 'removed_at': time.time(), 'preservation': result}
        with (REPORT/'cleanup.json').open('x') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
    else:
        result = check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['owned_cleanup_and_session_isolation_verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
