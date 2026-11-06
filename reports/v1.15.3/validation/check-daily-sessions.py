#!/usr/bin/env python3
"""Look only for the isolated run's session IDs in a stable copy of the daily DB."""
import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import sys
import time


def fingerprint(path):
    try:
        with path.open('rb') as f:
            return hashlib.file_digest(f, 'sha256').hexdigest()
    except FileNotFoundError:
        return None


root, receipt = map(Path, sys.argv[1:])
if (root.parent != Path('/private/tmp') or not root.name.startswith('rpe-v1153.')
        or root != root.resolve(strict=True) or root.stat().st_mode & 0o077
        or json.loads((root/'owner.json').read_bytes()) != {
            'owned_root': str(root), 'purpose': 'rpe-v1153-hermes-compatibility'}):
    raise SystemExit('unrecognized owned root')
sessions = set()
for arm in ('native', 'review'):
    [db] = (root/'model-pair/trial').rglob(f'formal-stock-acceptance-xlsx-{arm}.sqlite')
    with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as conn:
        sessions.update(row[0] for row in conn.execute(
            "SELECT DISTINCT session_id FROM hermes_host_events WHERE kind='on_session_start'"))
if len(sessions) != 3 or any(not isinstance(x, str) or not x for x in sessions):
    raise SystemExit('unexpected isolated session identifiers')
os.umask(0o077)
temp = root/'daily-check-readonly-copy'
temp.mkdir(mode=0o700)
daily = Path.home()/'.hermes'
originals = [daily/name for name in ('state.db', 'state.db-wal')]
result = {'isolated_session_ids_checked': len(sessions), 'stable_db_and_wal_copy': False,
          'session_rows_matching': None, 'message_rows_matching': None}
try:
    for attempt in range(1, 5):
        before = [fingerprint(path) for path in originals]
        for path, digest in zip(originals, before):
            target = temp/path.name
            if digest is None:
                target.unlink(missing_ok=True)
            else:
                try:
                    shutil.copyfile(path, target)
                except FileNotFoundError:
                    break
        after = [fingerprint(path) for path in originals]
        if before[0] and before == after == [fingerprint(temp/p.name) for p in originals]:
            result['stable_db_and_wal_copy'] = True
            break
        time.sleep(.15)
    result['copy_attempts'] = attempt
    if result['stable_db_and_wal_copy']:
        with closing(sqlite3.connect((temp/'state.db').as_uri()+'?mode=ro', uri=True)) as conn:
            ids = sorted(sessions)
            result['session_rows_matching'] = conn.execute(
                'SELECT count(*) FROM sessions WHERE id IN (?,?,?)', ids).fetchone()[0]
            result['message_rows_matching'] = conn.execute(
                'SELECT count(*) FROM messages WHERE session_id IN (?,?,?)', ids).fetchone()[0]
finally:
    shutil.rmtree(temp)
with receipt.open('x', encoding='utf-8') as f:
    json.dump(result, f, indent=2)
    f.write('\n')
