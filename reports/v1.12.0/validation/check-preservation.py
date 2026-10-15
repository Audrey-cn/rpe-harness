#!/usr/bin/env python3
"""Read-only final snapshot. This file never repairs, deletes, or signals."""
import sys
sys.dont_write_bytecode = True

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

OWNED = Path('/Users/audrey/项目/缓存/test/rpe-v112-779de8be2a164088b1b20e40264fc378')
WORKTREE = Path('/Users/audrey/.codex/worktrees/rpe-v112/rpe-harness')
HERMES = Path('/Users/audrey/.hermes')
ERRORS = []


def error(stage, exc, path=None):
    # Exception messages can contain credentials, SQL values, or command text.
    item = {'stage': stage, 'error_type': type(exc).__name__}
    if path is not None:
        item['path'] = str(path)
    ERRORS.append(item)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_fingerprint(path):
    path = Path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('not a regular file')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    after = path.stat()
    return {'sha256': digest.hexdigest(), 'size': after.st_size,
            'stable_during_read': (before.st_ino, before.st_size, before.st_mtime_ns)
                == (after.st_ino, after.st_size, after.st_mtime_ns)}


def readonly_db(path):
    path = Path(path)
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    return db


def quoted(name):
    return '"'+name.replace('"', '""')+'"'


def collect_ids(value, ids):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'session_id' and isinstance(item, str) and item:
                ids.add(item)
            elif isinstance(item, (dict, list)):
                collect_ids(item, ids)
    elif isinstance(value, list):
        for item in value:
            collect_ids(item, ids)


def comparison_session_ids():
    root, ids = OWNED/'comparison', set()
    sources = {'record_files': 0, 'stdout_files': 0, 'isolated_state_databases': 0}
    if not (root/'preregistration.json').is_file():
        raise ValueError('comparison registration is missing')
    plan = json.loads((root/'preregistration.json').read_bytes())
    if plan.get('root') != str(root) or len(plan.get('sessions', [])) != 8:
        raise ValueError('unexpected actual comparison registration')
    for trial in plan['sessions']+plan.get('pilots', []):
        path = root/'records'/(trial['trial_id']+'.json')
        if not path.exists() and trial.get('phase') == 'pilot':
            continue
        try:
            record = json.loads(path.read_bytes())
            if record.get('trial_id') != trial['trial_id'] or record.get('status') not in {'completed', 'failed'}:
                raise ValueError('record is not terminal')
            collect_ids(record, ids)
            sources['record_files'] += 1
        except Exception as exc:
            error('comparison_record_session_ids', exc, path)
    sessions = root/'sessions'
    for path in sessions.rglob('stdout.jsonl'):
        sources['stdout_files'] += 1
        try:
            if path.stat().st_size > 32*1024*1024:
                raise ValueError('session stdout exceeds bounded inspection size')
            with path.open('rb') as stream:
                for line in stream:
                    if line.strip():
                        collect_ids(json.loads(line), ids)
        except Exception as exc:
            error('comparison_stdout_session_ids', exc, path)
    for path in sessions.rglob('state.db'):
        sources['isolated_state_databases'] += 1
        try:
            with readonly_db(path) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()
                if exists:
                    ids.update(row[0] for row in db.execute('SELECT id FROM sessions')
                               if isinstance(row[0], str) and row[0])
        except Exception as exc:
            error('comparison_isolated_state_session_ids', exc, path)
    if not ids:
        raise ValueError('no actual comparison session IDs found')
    return ids, sources


def daily_state(baseline, ids):
    result = {'counts': {}, 'count_deltas': {}, 'session_reference_columns_checked': [],
              'leaked_session_id_count': 0, 'leaked_session_id_sha256': [],
              'leaks_by_table_column': {}}
    leaked = set()
    with readonly_db(HERMES/'state.db') as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            result['counts'][table] = db.execute('SELECT COUNT(*) FROM '+quoted(table)).fetchone()[0]
            if table in baseline:
                result['count_deltas'][table] = result['counts'][table]-baseline[table]
            columns = [row[1] for row in db.execute('PRAGMA table_info('+quoted(table)+')')]
            wanted = [column for column in columns if column in {'session_id', 'parent_session_id'}
                      or (table == 'sessions' and column == 'id')]
            for column in wanted:
                identity = table+'.'+column
                result['session_reference_columns_checked'].append(identity)
                matches = set()
                values = sorted(ids)
                for start in range(0, len(values), 500):
                    batch = values[start:start+500]
                    sql = ('SELECT DISTINCT '+quoted(column)+' FROM '+quoted(table)
                           +' WHERE '+quoted(column)+' IN ('+','.join('?' for _ in batch)+')')
                    matches.update(row[0] for row in db.execute(sql, batch))
                if matches:
                    result['leaks_by_table_column'][identity] = len(matches)
                    leaked.update(matches)
        result['missing_baseline_tables'] = sorted(set(baseline)-set(tables))
        result['added_tables'] = sorted(set(tables)-set(baseline))
    result['leaked_session_id_count'] = len(leaked)
    result['leaked_session_id_sha256'] = sorted(sha(value.encode()) for value in leaked)
    return result


def process_snapshot():
    # Keep all command arguments private. Report only PID links and executable
    # names for path matches and their descendants, excluding this check's tree.
    text = subprocess.run(['ps', '-axo', 'pid=,ppid=,args='], check=True,
                          capture_output=True, text=True, timeout=10).stdout
    rows = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            pid, ppid = map(int, parts[:2])
            rows[pid] = {'ppid': ppid, 'args': parts[2]}
    excluded, current = {os.getpid()}, os.getpid()
    while current in rows and rows[current]['ppid'] not in excluded:
        current = rows[current]['ppid']
        excluded.add(current)
    # Exclude ps and any inspection child as well as our ancestry.
    inspection_tree = {os.getpid()}
    while True:
        added = {pid for pid, row in rows.items() if row['ppid'] in inspection_tree}-inspection_tree
        if not added:
            break
        inspection_tree.update(added)
    excluded.update(inspection_tree)
    direct = {pid for pid, row in rows.items() if pid not in excluded
              and any(str(path) in row['args'] for path in (OWNED, WORKTREE))}
    selected = set(direct)
    while True:
        added = {pid for pid, row in rows.items() if row['ppid'] in selected
                 and pid not in excluded}-selected
        if not added:
            break
        selected.update(added)
    comm_text = subprocess.run(['ps', '-axo', 'pid=,comm='], check=True,
                               capture_output=True, text=True, timeout=10).stdout
    executable = {}
    for line in comm_text.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2:
            executable[int(fields[0])] = Path(fields[1]).name
    return {'scope': 'command-argument path matches plus descendants',
            'checker_and_ancestry_excluded': True,
            'count': len(selected), 'processes': [{'pid': pid, 'ppid': rows[pid]['ppid'],
                'executable': executable.get(pid), 'direct_path_match': pid in direct}
                for pid in sorted(selected)]}


def main():
    snapshot = {'schema_version': 'rpe.daily-preservation/v1', 'checked_at': time.time(),
                'read_only': True, 'state_restored': False}
    try:
        baseline = json.loads((OWNED/'ownership.json').read_bytes())
        if baseline.get('owned_root') != str(OWNED):
            raise ValueError('ownership root mismatch')
    except Exception as exc:
        error('ownership_baseline', exc, OWNED/'ownership.json')
        snapshot.update(preservation_ok=False, errors=ERRORS)
        print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
        return 1
    mutable = {str(HERMES/'auth.json'), str(HERMES/'state.db'),
               '/Users/audrey/.claude.json'}
    files = {'baseline_count': len(baseline['files']), 'unchanged_count': 0,
             'changed': [], 'missing': [], 'mutable_daily_drift': []}
    report_paths = {path for path in baseline['files'] if '/reports/' in path}
    for path, expected in baseline['files'].items():
        try:
            current = file_fingerprint(path)
            if current['sha256'] == expected and current['stable_during_read']:
                files['unchanged_count'] += 1
            elif path in mutable:
                files['mutable_daily_drift'].append({'path': path,
                    'stable_during_read': current['stable_during_read']})
            else:
                files['changed'].append(path)
        except FileNotFoundError:
            files['missing'].append(path)
        except Exception as exc:
            error('baseline_file', exc, path)
    snapshot['files'] = files
    snapshot['prior_reports'] = {'baseline_count': len(report_paths),
        'changed': sorted(report_paths & set(files['changed'])),
        'missing': sorted(report_paths & set(files['missing']))}
    skills = {'baseline_count': len(baseline['skill_files']), 'checked_count': 0,
              'changed': [], 'missing': [], 'added_count': None}
    if skills['baseline_count'] != 478:
        error('baseline_skill_count', ValueError())
    for relative, expected in baseline['skill_files'].items():
        path = HERMES/'skills'/relative
        try:
            current = file_fingerprint(path)
            skills['checked_count'] += 1
            if (current['sha256'] != expected['sha256'] or current['size'] != expected['size']
                    or not current['stable_during_read']):
                skills['changed'].append(relative)
        except FileNotFoundError:
            skills['missing'].append(relative)
        except Exception as exc:
            error('baseline_skill_file', exc, path)
    try:
        current_names = {str(path.relative_to(HERMES/'skills'))
                         for path in (HERMES/'skills').rglob('*') if path.is_file()}
        skills['added_count'] = len(current_names-set(baseline['skill_files']))
    except Exception as exc:
        error('skill_inventory', exc, HERMES/'skills')
    snapshot['skills'] = skills
    snapshot['daily_directory_top_levels'] = {}
    for name, expected in baseline['directories'].items():
        try:
            directory = HERMES/name
            if not directory.exists() and expected == []:
                snapshot['daily_directory_top_levels'][name] = {
                    'baseline_count': 0, 'current_count': 0, 'added': [], 'missing': [],
                    'exists': False, 'baseline_existence': 'not_recorded'}
                continue
            current = {path.name for path in directory.iterdir()}
            snapshot['daily_directory_top_levels'][name] = {
                'baseline_count': len(expected), 'current_count': len(current),
                'exists': True, 'added': sorted(current-set(expected)), 'missing': sorted(set(expected)-current)}
        except Exception as exc:
            error('daily_directory_inventory', exc, HERMES/name)
    snapshot['repositories'] = {}
    for path, expected in baseline['repositories'].items():
        try:
            head = subprocess.run(['git', '--no-optional-locks', '-C', path, 'rev-parse', 'HEAD'], check=True,
                capture_output=True, text=True, timeout=15).stdout.strip()
            status = subprocess.run(['git', '--no-optional-locks', '-C', path, 'status', '--porcelain=v1',
                '--untracked-files=normal'], check=True, capture_output=True, text=True, timeout=15).stdout.strip()
            snapshot['repositories'][path] = {'head_matches': head == expected['head'].strip(),
                'status_matches': status == expected['status'].strip(),
                'current_head': head, 'current_status_sha256': sha(status.encode())}
        except Exception as exc:
            error('repository_snapshot', exc, path)
    try:
        ids, sources = comparison_session_ids()
        snapshot['comparison_session_sources'] = {**sources, 'unique_actual_session_ids': len(ids)}
        snapshot['daily_state'] = daily_state(baseline['state_tables'], ids)
    except Exception as exc:
        error('daily_session_leakage_check', exc, HERMES/'state.db')
    try:
        snapshot['owned_processes'] = process_snapshot()
    except Exception as exc:
        error('owned_process_snapshot', exc)
    snapshot['errors'] = ERRORS
    snapshot['preservation_ok'] = bool(not ERRORS and not files['changed'] and not files['missing']
        and not skills['changed'] and not skills['missing']
        and all(row['head_matches'] and row['status_matches'] for row in snapshot['repositories'].values())
        and snapshot.get('daily_state', {}).get('leaked_session_id_count') == 0
        and not snapshot.get('daily_state', {}).get('missing_baseline_tables'))
    snapshot['cleanup_ready'] = snapshot['preservation_ok'] and snapshot.get('owned_processes', {}).get('count') == 0
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    return 0 if snapshot['preservation_ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
