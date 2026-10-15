#!/usr/bin/env python3
"""Exact v1.10 audit; defaults to dry run. Run with a Python outside OWNED."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid

TOKEN = '37f23ae55b694cb3912f32f88684cfe2'
OWNED = Path('/Users/audrey/项目/缓存/test/rpe-v110-' + TOKEN)
REPO = Path(__file__).absolute().parents[3]
REPORT = REPO / 'reports/v1.10.0/cleanup.json'
EXPORTS = [REPO / 'reports/v1.10.0' / name for name in ('pilot-original', 'public-evidence')]
DAILY = Path('/Users/audrey/.hermes/state.db')


def guard(path):
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink_or_noncanonical_audit_path')
    return path


def digest(path):
    guard(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def git(path, *args):
    return subprocess.run(['git', '-C', path, *args], check=True, capture_output=True,
                          text=True, timeout=10).stdout


def preservation(baseline):
    differences, repositories = [], {}
    for name, expected in baseline['files'].items():
        actual = digest(Path(name))
        if actual != expected:
            differences.append({'path': name, 'baseline_sha256': expected, 'current_sha256': actual,
                'record_only': name in {'/Users/audrey/.hermes/auth.json', str(DAILY)}})
    for path, expected in baseline['repositories'].items():
        guard(Path(path))
        actual = {'head': git(path, 'rev-parse', 'HEAD').strip(), 'status': git(path, 'status', '--porcelain')}
        repositories[path] = {**actual, 'unchanged': actual == expected}
    return {'file_hash_differences': differences, 'old_repositories': repositories,
        'strict_preservation': all(item['record_only'] for item in differences)
            and all(item['unchanged'] for item in repositories.values())}


def session_ids():
    identities, databases = set(), 0
    for comparison in OWNED.glob('comparison*'):
        guard(comparison)
        for path in (comparison / 'databases').glob('*.sqlite'):
            guard(path)
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hermes_host_events'").fetchone():
                    identities.update(row[0] for row in db.execute('SELECT DISTINCT session_id FROM hermes_host_events') if row[0])
                    databases += 1
    return identities, databases


def daily_rows(identities):
    guard(DAILY)
    with sqlite3.connect(DAILY.as_uri() + '?mode=ro', uri=True) as db:
        return sum(db.execute('SELECT count(*) FROM sessions WHERE id=?', (identity,)).fetchone()[0]
                   for identity in identities)


def processes():
    from rpe_harness.integrations.hermes_session import _process_table
    table, registered = _process_table(), {}
    ancestors, current = set(), os.getpid()
    while current in table and current not in ancestors:
        ancestors.add(current)
        current = table[current][0]
    for path in OWNED.glob('comparison*/sessions/**/child-processes.jsonl'):
        guard(path)
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if type(row.get('pid')) is not int or row['pid'] <= 1 or not isinstance(row.get('lstart'), str) or not row['lstart']:
                raise ValueError('invalid_owned_process_registration')
            registered[row['pid']] = row['lstart']
    live = {pid: start for pid, start in registered.items() if pid not in ancestors and table.get(pid, (None, None))[1] == start}
    # Command/environment text never leaves memory or enters the JSON report.
    scan = subprocess.run(['/bin/ps', '-Eww', '-U', str(os.getuid()), '-o', 'pid=,command='],
        check=True, capture_output=True, text=True, errors='replace', timeout=10).stdout
    marker = re.compile(re.escape(str(OWNED)) + r'(?=$|[\s/;:\"\x27])')
    for line in scan.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and marker.search(parts[1]):
            pid = int(parts[0])
            if pid not in ancestors and pid in table:
                live[pid] = table[pid][1]
    return live, ancestors, {'registry_identities': len(registered), 'live_owned': len(live)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delete', action='store_true')
    parser.add_argument('--stop-owned', action='store_true')
    args, report = parser.parse_args(), {'owned_root': str(OWNED), 'removed': False, 'errors': []}
    try:
        guard(REPO); guard(REPORT); guard(OWNED); guard(OWNED / 'ownership.json')
        if uuid.UUID(TOKEN).hex != TOKEN or OWNED.name != 'rpe-v110-' + TOKEN or not OWNED.is_dir():
            raise ValueError('exact_owned_root_identity_failed')
        if Path(sys.executable).resolve().is_relative_to(OWNED):
            raise ValueError('use_python_outside_deleted_owned_root')
        baseline = json.loads((OWNED / 'ownership.json').read_text())
        if baseline.get('owned_root') != str(OWNED):
            raise ValueError('ownership_baseline_root_mismatch')
        root_identity = (OWNED.stat().st_dev, OWNED.stat().st_ino)
        sys.path.insert(0, str(guard(REPO / 'src')))
        from rpe_harness.experiments.review_comparison import verify_review_evidence
        def verify():
            return {path.name: verify_review_evidence(guard(path)) for path in EXPORTS}
        report.update(mode='delete' if args.delete else 'dry_run', public_before=verify(), preservation_before=preservation(baseline))
        identities, count = session_ids()
        report.update(paid_databases=count, paid_session_ids=len(identities), owned_session_rows_in_daily_history=daily_rows(identities))
        live, ancestors, report['processes_before'] = processes()
        if live and args.stop_owned:
            from rpe_harness.integrations.hermes_session import _capture_descendants, _stop_descendants, _remaining_descendants
            for pid, start in list(live.items()):
                _capture_descendants(pid, start, live)
            report['stopped_owned'] = len(_stop_descendants({pid: start for pid, start in live.items() if pid not in ancestors}))
            report['stop_survivors'] = len(_remaining_descendants({pid: start for pid, start in live.items() if pid not in ancestors}))
        live, _, report['processes_after'] = processes()
        if live or report.get('stop_survivors') or report['owned_session_rows_in_daily_history'] or not report['preservation_before']['strict_preservation']:
            raise ValueError('live_owned_or_history_or_preservation_guard_failed')
        if not all(item['verified'] for item in report['public_before'].values()):
            raise ValueError('both_portable_public_exports_must_verify_before_cleanup')
        entries = [item for path in OWNED.rglob('*') if not stat.S_ISDIR((item := path.lstat()).st_mode)]
        report['owned_size'] = {'files': len(entries), 'bytes': sum(item.st_size for item in entries)}
        if args.delete:
            guard(OWNED)
            if (OWNED.stat().st_dev, OWNED.stat().st_ino) != root_identity or json.loads((OWNED / 'ownership.json').read_text()) != baseline:
                raise ValueError('owned_root_identity_changed_during_audit')
            shutil.rmtree(OWNED)
            report['removed'] = not os.path.lexists(OWNED)
            if not report['removed']:
                raise ValueError('exact_owned_root_still_exists')
        report.update(public_after=verify(), preservation_after=preservation(baseline), daily_rows_after=daily_rows(identities))
        report['verified'] = all(item['verified'] for item in report['public_after'].values()) and report['preservation_after']['strict_preservation'] and report['daily_rows_after'] == 0
    except Exception as error:
        report['errors'].append(type(error).__name__ + ':' + str(error) if isinstance(error, ValueError) else type(error).__name__)
        report['verified'] = False
    guard(REPORT)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report': str(REPORT), 'removed': report['removed'], 'verified': report['verified']}))
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
