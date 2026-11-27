"""Tests for developer contracts: env.patch and sqlite.query_export."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from rpe_harness.runtime import HarnessRuntime


def test_env_patch_preserves_comments_blank_lines_and_order(tmp_path: Path):
    src = tmp_path / 'app.env'
    out = tmp_path / 'app.patched.env'
    original = (
        '# Database config\n'
        'DB_HOST=localhost\n'
        'DB_PORT=5432\n'
        '\n'
        '# Feature flags\n'
        'LEGACY_MODE=1\n'
        'APP_TITLE="Hello World"\n'
    )
    src.write_text(original, encoding='utf-8')

    rt = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    res = rt.run({
        'schema_version': 2,
        'event_id': 'evt-env-1',
        'capability': 'env.patch',
        'goal': 'Update DB_PORT, remove LEGACY_MODE, add TIMEOUT',
        'constraints': ['preserve_original', 'preserve_order'],
        'execution': {
            'input_path': str(src),
            'output_path': str(out),
            'operations': [
                {'op': 'set', 'key': 'DB_PORT', 'value': '6432'},
                {'op': 'delete', 'key': 'LEGACY_MODE'},
                {'op': 'set', 'key': 'API_TIMEOUT', 'value': '30s'},
            ],
        },
    })
    assert res['status'] == 'succeeded', res
    assert res['verification']['passed'] is True
    assert src.read_text(encoding='utf-8') == original
    expected = (
        '# Database config\n'
        'DB_HOST=localhost\n'
        'DB_PORT=6432\n'
        '\n'
        '# Feature flags\n'
        'APP_TITLE="Hello World"\n'
        'API_TIMEOUT=30s\n'
    )
    assert out.read_text(encoding='utf-8') == expected


def test_sqlite_query_export_readonly_and_blocks_writes(tmp_path: Path):
    db_in = tmp_path / 'orders.sqlite'
    out_csv = tmp_path / 'orders_active.csv'
    with sqlite3.connect(db_in) as conn:
        conn.execute('CREATE TABLE orders (code TEXT, status TEXT, amount INTEGER)')
        conn.executemany(
            'INSERT INTO orders VALUES (?, ?, ?)',
            [('0042', 'active', 100), ('0007', 'cancelled', 50), ('0099', 'active', 250)],
        )

    rt = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')

    # Write SQL must be rejected at admission
    bad = rt.run({
        'schema_version': 2,
        'event_id': 'evt-sql-write',
        'capability': 'sqlite.query_export',
        'goal': 'Attempt destructive drop',
        'execution': {
            'input_path': str(db_in),
            'output_path': str(tmp_path / 'bad.csv'),
            'query': 'DROP TABLE orders',
        },
    })
    assert bad['status'] == 'rejected'
    assert not (tmp_path / 'bad.csv').exists()

    # Valid read-only SELECT exports CSV with leading zeros intact
    ok = rt.run({
        'schema_version': 2,
        'event_id': 'evt-sql-ok',
        'capability': 'sqlite.query_export',
        'goal': 'Export active orders to CSV',
        'constraints': ['preserve_original'],
        'execution': {
            'input_path': str(db_in),
            'output_path': str(out_csv),
            'query': "SELECT code, amount FROM orders WHERE status = 'active' ORDER BY code",
        },
    })
    assert ok['status'] == 'succeeded', ok
    assert ok['verification']['passed'] is True
    assert out_csv.read_text(encoding='utf-8') == 'code,amount\n0042,100\n0099,250\n'
