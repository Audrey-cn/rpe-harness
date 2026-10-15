#!/usr/bin/env python3
"""Read-only post-cleanup preservation check, independent of deleted test roots.

Reads only the saved report baseline and surviving daily/repository paths. The
removed paths are inspected for absence only. No deletion, repair, restore,
network, model call, signal, or report-file write is performed; JSON goes to
stdout. SQLite uses a WAL-aware mode=ro/query_only read transaction.
"""
import sys
sys.dont_write_bytecode = True

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time

BASELINE = Path('/Users/audrey/项目/rpe-harness/reports/v1.13.0/validation/preservation-baseline.json')
SCHEMA = 'rpe.post-cleanup-preservation-baseline/v1'
ERRORS = []
PREEXISTING_SKILL_LINKS = frozenset(
    name+'/SKILL.md' for name in ('uaek-agent-enhancement', 'uaek-effort-control',
        'uaek-memory-management', 'uaek-verification-framework', 'uaek-workflow-guidance'))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def error(stage, exc, path=None):
    row = {'stage': stage, 'error_type': type(exc).__name__}
    if path is not None:
        row['path'] = str(path)
    ERRORS.append(row)  # No exception text, SQL cell, environment, or argv.


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute path required')
    return path


def fingerprint(path):
    path = absolute(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('fingerprint cannot follow links')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        def identity(value):
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('fingerprint requires regular file')
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        return {'sha256': digest.hexdigest(), 'size': after.st_size,
                'stable_during_read': identity(before) == identity(after) == identity(current)}
    finally:
        os.close(fd)


def fingerprint_preexisting_link(path, recorded, original, goal_started_at):
    """Verify an explicit old skill link against supplemental topology evidence.

    Link topology was measured after cleanup, not in the original snapshot.
    Pre-goal link birth/ctime corroborates age; original target hash/size remains
    the content baseline. This exception never applies to production modules.
    """
    path = absolute(path)
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError('preexisting skill link has a linked parent')
    required = {'target', 'target_resolved', 'mtime_ns', 'ctime_ns', 'birthtime',
                'target_sha256', 'target_size'}
    if (type(recorded) is not dict or required-set(recorded)
            or type(original) is not dict
            or recorded['target_sha256'] != original['sha256']
            or recorded['target_size'] != original['size']
            or type(goal_started_at) not in (int, float) or goal_started_at <= 0):
        raise ValueError('preexisting link evidence differs from original content baseline')
    before = path.lstat()
    if (not stat.S_ISLNK(before.st_mode)
            or before.st_mtime_ns != recorded['mtime_ns']
            or before.st_ctime_ns != recorded['ctime_ns']
            or before.st_birthtime != recorded['birthtime']
            or not 0 <= before.st_birthtime < goal_started_at
            or not 0 <= before.st_ctime_ns < goal_started_at * 1_000_000_000
            or not 0 <= before.st_mtime_ns < goal_started_at * 1_000_000_000
            or os.readlink(path) != recorded['target']):
        raise ValueError('preexisting skill link topology or pre-goal times changed')
    target = path.resolve(strict=True)
    if target != absolute(recorded['target_resolved']):
        raise ValueError('preexisting skill link resolved target changed')
    current = fingerprint(target)
    after = path.lstat()
    identity = lambda row: (row.st_dev, row.st_ino, row.st_mode, row.st_size,
                           row.st_mtime_ns, row.st_ctime_ns, row.st_birthtime)
    if (identity(before) != identity(after) or os.readlink(path) != recorded['target']
            or path.resolve(strict=True) != target
            or current['sha256'] != original['sha256'] or current['size'] != original['size']
            or current['stable_during_read'] is not True):
        raise ValueError('preexisting skill link or original target bytes changed')
    return {**current, 'preexisting_link_verified': True}


def inventory(root, suffix=None):
    root = absolute(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('inventory root must be a real directory')
    names, links = set(), []
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            path = Path(directory)/name
            if path.is_symlink():
                links.append(path.relative_to(root).as_posix())
                directories.remove(name)
        for name in files:
            path = Path(directory)/name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                links.append(relative)
            elif suffix is None or path.suffix == suffix:
                names.add(relative)
    return names, sorted(links)


def checked_files(expected, mutable=()):
    result = {'baseline_count': len(expected), 'unchanged_count': 0,
              'changed': [], 'missing': [], 'mutable_daily_drift': []}
    for name, wanted in expected.items():
        try:
            current = fingerprint(name)
            if current['sha256'] == wanted and current['stable_during_read']:
                result['unchanged_count'] += 1
            elif name in mutable:
                result['mutable_daily_drift'].append({'path': name,
                    'stable_during_read': current['stable_during_read']})
            else:
                result['changed'].append(name)
        except FileNotFoundError:
            result['missing'].append(name)
        except Exception as exc:
            error('baseline_file', exc, name)
    return result


def checked_tree(root, expected, *, suffix=None, preexisting_links=None, goal_started_at=None):
    preexisting_links = {} if preexisting_links is None else preexisting_links
    if type(preexisting_links) is not dict or set(preexisting_links)-set(expected):
        raise ValueError('skill-link allowlist escaped the original content baseline')
    result = {'baseline_count': len(expected), 'checked_count': 0,
              'changed': [], 'missing': [], 'added': [], 'links': [],
              'verified_preexisting_links': [], 'unverified_links': []}
    for relative, wanted in expected.items():
        try:
            path = Path(relative)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('tree entry escaped root')
            current = (fingerprint_preexisting_link(absolute(root)/path,
                preexisting_links[relative], wanted, goal_started_at)
                if relative in preexisting_links else fingerprint(absolute(root)/path))
            result['checked_count'] += 1
            wanted_sha = wanted['sha256'] if isinstance(wanted, dict) else wanted
            wanted_size = wanted.get('size') if isinstance(wanted, dict) else None
            if (current['sha256'] != wanted_sha or not current['stable_during_read']
                    or wanted_size is not None and current['size'] != wanted_size):
                result['changed'].append(relative)
            elif current.get('preexisting_link_verified') is True:
                result['verified_preexisting_links'].append(relative)
        except FileNotFoundError:
            result['missing'].append(relative)
        except Exception as exc:
            result['changed'].append(relative)
            error('tree_file', exc, str(Path(root)/relative))
    try:
        names, result['links'] = inventory(root, suffix)
        # Keep every observed link visible. Only a verified explicit old link
        # contributes to the original file inventory and can pass tree_ok().
        names.update(set(result['verified_preexisting_links']) & set(result['links']))
        result['unverified_links'] = sorted(set(result['links'])-set(result['verified_preexisting_links']))
        result['added'] = sorted(names-set(expected))
        result['missing'] = sorted(set(result['missing']) | (set(expected)-names))
    except Exception as exc:
        error('tree_inventory', exc, root)
    return result


@contextmanager
def readonly_db(path):
    path = absolute(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('daily state must be an existing regular database')
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=3)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def quoted(name):
    return '"'+name.replace('"', '""')+'"'


def daily_state(path, baseline, target_hashes):
    result = {'counts': {}, 'count_deltas': {}, 'session_reference_columns_checked': [],
              'leaked_test_session_ID_count': 0, 'leaked_test_session_id_sha256': [],
              'leaks_by_table_column': {}, 'counts_are_observations_not_restore_targets': True}
    leaked = set()
    with readonly_db(path) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            result['counts'][table] = db.execute('SELECT COUNT(*) FROM '+quoted(table)).fetchone()[0]
            if table in baseline:
                result['count_deltas'][table] = result['counts'][table]-baseline[table]
            columns = [row[1] for row in db.execute('PRAGMA table_info('+quoted(table)+')')]
            wanted = [column for column in columns if column.endswith('session_id')
                      or (table == 'sessions' and column == 'id')]
            for column in wanted:
                identity = table+'.'+column
                result['session_reference_columns_checked'].append(identity)
                matched = set()
                for value, in db.execute('SELECT DISTINCT '+quoted(column)+' FROM '+quoted(table)):
                    raw = value.encode('utf-8') if isinstance(value, str) else value if isinstance(value, bytes) else None
                    if raw is not None:
                        digest = sha(raw)
                        if digest in target_hashes:
                            matched.add(digest)
                if matched:
                    result['leaks_by_table_column'][identity] = len(matched)
                    leaked.update(matched)
        result['missing_baseline_tables'] = sorted(set(baseline)-set(tables))
        result['added_tables'] = sorted(set(tables)-set(baseline))
    if not result['session_reference_columns_checked']:
        raise ValueError('daily state has no session-reference columns')
    result['leaked_test_session_ID_count'] = len(leaked)
    result['leaked_test_session_id_sha256'] = sorted(leaked)
    return result


def git(path, *arguments, check=True):
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    return subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
        '-c', 'core.untrackedCache=false', '-c', 'maintenance.auto=0', '-c', 'gc.auto=0',
        '-C', str(absolute(path)), *arguments], env=env, capture_output=True,
        text=True, timeout=20, check=check)


def repository(path, expected):
    head = git(path, 'rev-parse', 'HEAD').stdout.strip()
    status_text = git(path, 'status', '--porcelain=v1', '--untracked-files=normal').stdout.strip()
    return {'head_matches': head == expected['head'].strip(),
            'status_matches': sha(status_text.encode()) == expected['status_sha256'],
            'current_head': head, 'current_status_sha256': sha(status_text.encode())}


def main_repository(expected):
    path = absolute(expected['path'])
    source_commit = expected['expected_source_commit']
    if not re.fullmatch('[a-f0-9]{40}', source_commit):
        raise ValueError('main source commit must be a full commit identity')
    ancestor = git(path, 'merge-base', '--is-ancestor', source_commit, 'HEAD', check=False)
    if ancestor.returncode not in (0, 1):
        raise ValueError('main source ancestry cannot be checked')
    status_text = git(path, 'status', '--porcelain=v1', '--untracked-files=normal').stdout.strip()
    names = expected['original_untracked']
    if len(names) != 4 or len(set(names)) != 4:
        raise ValueError('four distinct original untracked paths are required')
    retained = []
    for name in names:
        relative = Path(name.rstrip('/'))
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('original untracked path escaped main')
        retained.append({'path': name, 'exists': os.path.lexists(path/relative)})
    status_rows = status_text.splitlines()
    untracked_rows = [row for row in status_rows if row.startswith('?? ')]
    tracked_rows = [row for row in status_rows if not row.startswith('?? ')]
    untracked_text = '\n'.join(untracked_rows)
    production = checked_tree(expected['production_root'], expected['production_files'], suffix='.py')
    return {'source_commit_is_ancestor': ancestor.returncode == 0,
            'current_head': git(path, 'rev-parse', 'HEAD').stdout.strip(),
            'current_untracked_status_sha256': sha(untracked_text.encode()),
            'tracked_status_row_count': len(tracked_rows),
            'tracked_status_sha256': sha('\n'.join(tracked_rows).encode()),
            'status_matches': sha(untracked_text.encode()) == expected['expected_status_sha256'],
            'only_four_original_untracked': sorted(untracked_rows) == sorted('?? '+name for name in names),
            'original_untracked': retained, 'production_modules': production}


def process_snapshot(markers):
    # This mirrors the pre-cleanup check's explicit scope. Raw command text
    # remains in memory; only matched PID links and executable basenames leave.
    output = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,args='], check=True,
                            capture_output=True, text=True, timeout=15).stdout
    rows = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            pid, parent = map(int, parts[:2])
            rows[pid] = {'parent': parent, 'args': parts[2]}
    excluded, current = {os.getpid()}, os.getpid()
    while current in rows and rows[current]['parent'] not in excluded:
        current = rows[current]['parent']; excluded.add(current)
    inspection = {os.getpid()}
    while True:
        added = {pid for pid, row in rows.items() if row['parent'] in inspection}-inspection
        if not added: break
        inspection.update(added)
    excluded.update(inspection)
    direct = {pid for pid, row in rows.items() if pid not in excluded
              and any(marker in row['args'] for marker in markers)}
    selected = set(direct)
    while True:
        added = {pid for pid, row in rows.items() if row['parent'] in selected and pid not in excluded}-selected
        if not added: break
        selected.update(added)
    return {'scope': 'command-argument path matches plus descendants',
            'checker_and_ancestry_excluded': True, 'count': len(selected),
            'processes': [{'pid': pid, 'ppid': rows[pid]['parent'],
                'executable': Path(rows[pid]['args'].split(None, 1)[0]).name,
                'direct_path_match': pid in direct} for pid in sorted(selected)]}


def tree_ok(value):
    return (not any(value.get(key) for key in ('changed', 'missing', 'added'))
            and not value.get('unverified_links', value.get('links')))


def main():
    snapshot = {'schema_version': 'rpe.post-cleanup-preservation/v1', 'checked_at': time.time(),
                'read_only': True, 'state_restored': False, 'deleted_owned_contents_read': False,
                'preservation_ok': False, 'owned_processes': {'count': None},
                'leaked_test_session_ID_count': None}
    try:
        if any(item.is_symlink() for item in (BASELINE, *BASELINE.parents)):
            raise ValueError('saved baseline cannot follow links')
        baseline_raw = BASELINE.read_bytes()
        baseline = json.loads(baseline_raw)
        if baseline['schema_version'] != SCHEMA:
            raise ValueError('unknown saved preservation baseline')
        required = {'files', 'mutable_daily_files', 'skill_root', 'skill_files', 'repositories',
            'main_repository', 'state_db_path', 'state_tables', 'daily_directories', 'daily_root',
            'actual_test_session_id_sha256', 'deleted_paths', 'owned_process_path_markers'}
        if required-set(baseline):
            raise ValueError('saved preservation baseline is incomplete')
        if any(type(baseline[key]) is not dict or not baseline[key]
               for key in ('files', 'skill_files', 'repositories', 'main_repository', 'state_tables')):
            raise ValueError('saved preservation mappings must be nonempty')
        required_main = {'path', 'expected_source_commit', 'expected_status_sha256', 'original_untracked',
                         'production_root', 'production_files'}
        if required_main-set(baseline['main_repository']) or not baseline['main_repository']['production_files']:
            raise ValueError('main source and original-untracked baseline is incomplete')
        skill_links = baseline.get('preexisting_skill_links', {})
        if skill_links and (type(skill_links) is not dict or set(skill_links) != PREEXISTING_SKILL_LINKS
                            or not set(skill_links) <= set(baseline['skill_files'])):
            raise ValueError('only the five explicitly recorded preexisting skill links are allowed')
        target_hashes = baseline['actual_test_session_id_sha256']
        if (not target_hashes or len(target_hashes) != len(set(target_hashes))
                or any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value) for value in target_hashes)):
            raise ValueError('actual test-session hashes must be nonempty and unique')
        if not baseline['deleted_paths'] or not baseline['owned_process_path_markers']:
            raise ValueError('actual cleanup paths and process markers are required')
        for name in baseline['deleted_paths']+baseline['owned_process_path_markers']:
            absolute(name)
        removed = [absolute(name) for name in baseline['deleted_paths']]
        surviving_reads = [*baseline['files'], *baseline['repositories'], baseline['skill_root'],
            baseline['state_db_path'], baseline['daily_root'], baseline['main_repository']['path'],
            baseline['main_repository']['production_root'], str(BASELINE)]
        if any(absolute(name).is_relative_to(path) for name in surviving_reads for path in removed):
            raise ValueError('baseline cannot read any deleted test-root contents')
        mutable = set(baseline['mutable_daily_files'])
        allowed_mutable = {str(Path(baseline['daily_root'])/'auth.json'),
                           str(Path(baseline['daily_root'])/'state.db'), '/Users/audrey/.claude.json'}
        if mutable-allowed_mutable or mutable-set(baseline['files']):
            raise ValueError('mutable daily drift scope expanded')
        snapshot['baseline_sha256'] = sha(baseline_raw)
        snapshot['actual_test_session_id_hash_count'] = len(target_hashes)
    except Exception as exc:
        error('saved_preservation_baseline', exc, BASELINE)
        snapshot['errors'] = ERRORS
        print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
        return 1
    deleted = {name: not os.path.lexists(absolute(name)) for name in baseline['deleted_paths']}
    snapshot['deleted_paths'] = {'checked_count': len(deleted),
        'all_absent': all(deleted.values()), 'still_present': [name for name, absent in deleted.items() if not absent]}
    files = checked_files(baseline['files'], mutable)
    snapshot['files'] = files
    report_names = {name for name in baseline['files'] if '/reports/' in name}
    snapshot['prior_reports'] = {'baseline_count': len(report_names),
        'changed': sorted(report_names & set(files['changed'])),
        'missing': sorted(report_names & set(files['missing']))}
    skills = checked_tree(baseline['skill_root'], baseline['skill_files'], preexisting_links=skill_links,
                          goal_started_at=baseline.get('baseline_observation_started_at'))
    if skill_links:
        skills['link_topology_observation'] = 'post_cleanup_supplement_not_initial_topology_snapshot'
        skills['link_age_corroboration'] = 'birthtime_and_ctime_predate_goal; target_hash_and_size_match_original_baseline'
    snapshot['skills'] = skills
    snapshot['repositories'] = {}
    for name, expected in baseline['repositories'].items():
        try:
            snapshot['repositories'][name] = repository(name, expected)
        except Exception as exc:
            error('old_repository', exc, name)
    try:
        snapshot['main_repository'] = main_repository(baseline['main_repository'])
    except Exception as exc:
        error('main_repository', exc, baseline['main_repository'].get('path'))
    snapshot['daily_directory_top_levels'] = {}
    for name, expected in baseline['daily_directories'].items():
        try:
            path = absolute(baseline['daily_root'])/name
            if Path(name).name != name or name in ('', '.', '..'):
                raise ValueError('daily directory name escaped root')
            current = {item.name for item in path.iterdir()} if path.exists() else set()
            snapshot['daily_directory_top_levels'][name] = {
                'exists': path.exists(), 'baseline_exists': expected['exists'],
                'baseline_count': len(expected['names']), 'current_count': len(current),
                'added': sorted(current-set(expected['names'])), 'missing': sorted(set(expected['names'])-current)}
        except Exception as exc:
            error('daily_directory', exc, name)
    try:
        snapshot['daily_state'] = daily_state(baseline['state_db_path'], baseline['state_tables'], set(target_hashes))
        snapshot['leaked_test_session_ID_count'] = snapshot['daily_state']['leaked_test_session_ID_count']
    except Exception as exc:
        error('daily_state', exc, baseline['state_db_path'])
    try:
        snapshot['owned_processes'] = process_snapshot(baseline['owned_process_path_markers'])
    except Exception as exc:
        error('owned_processes', exc)
    main_check = snapshot.get('main_repository', {})
    snapshot['errors'] = ERRORS
    snapshot['preservation_ok'] = bool(not ERRORS and snapshot['deleted_paths']['all_absent']
        and not files['changed'] and not files['missing'] and tree_ok(skills)
        and all(row['head_matches'] and row['status_matches'] for row in snapshot['repositories'].values())
        and len(snapshot['repositories']) == len(baseline['repositories'])
        and main_check.get('source_commit_is_ancestor') is True and main_check.get('status_matches') is True
        and main_check.get('only_four_original_untracked') is True
        and all(row['exists'] for row in main_check.get('original_untracked', []))
        and tree_ok(main_check.get('production_modules', {}))
        and snapshot['leaked_test_session_ID_count'] == 0
        and not snapshot.get('daily_state', {}).get('missing_baseline_tables')
        and snapshot['owned_processes'].get('count') == 0)
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    return 0 if snapshot['preservation_ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
