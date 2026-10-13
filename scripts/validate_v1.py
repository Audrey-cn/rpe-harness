#!/usr/bin/env python3
"""Fresh-process acceptance check; fixtures and SQLite are removed on exit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', required=True, help='installed Python executable to validate')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    checks = []
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # Deliberately remove source overrides to exercise the installed distribution.
    env.pop('PYTHONPATH', None)
    with tempfile.TemporaryDirectory(prefix='rpe-v1-acceptance-') as folder:
        root = Path(folder)
        db = root / 'runs.sqlite'

        def command(*values, expected=0):
            proc = subprocess.run([args.python, '-m', 'rpe_harness', *map(str, values)],
                                  cwd=root, env=env, capture_output=True, text=True, timeout=20)
            if proc.returncode != expected:
                raise AssertionError({'argv': values, 'exit': proc.returncode,
                                      'stdout': proc.stdout, 'stderr': proc.stderr})
            return json.loads(proc.stdout)

        source = root / 'orders.csv'
        source.write_text('订单号,金额\n001,10\n002,20\n001,99\n', encoding='utf-8-sig')
        original = sha(source)
        payload = {'schema_version': 1, 'event_id': 'csv-acceptance', 'session_id': 'fresh-process',
                   'state': {'task_type': 'data.processing', 'goal': '按订单号去重保留首条',
                             'input_format': 'csv', 'required_capability': 'tabular.deduplicate',
                             'constraints': ['preserve_original', 'preserve_order']},
                   'execution': {'input_path': str(source), 'output_path': str(root / 'out.csv'),
                                 'key_columns': ['订单号']}}
        request = root / 'request.json'
        request.write_text(json.dumps(payload, ensure_ascii=False))
        first = command('run', '--request', request, '--db', db)
        assert first['status'] == 'succeeded' and first['verification']['passed']
        assert sha(source) == original
        assert (root / 'out.csv').read_text(encoding='utf-8-sig').splitlines() == ['订单号,金额', '001,10', '002,20']
        checks.append({'check': 'installed_csv_first_record_and_source_hash', 'passed': True})
        replay = command('run', '--request', request, '--db', db)
        assert replay['run_id'] == first['run_id']
        loaded = command('runs', 'show', first['run_id'], '--db', db)
        assert loaded['status'] == 'succeeded' and loaded['state']['session_id'] == 'fresh-process'
        checks.append({'check': 'fresh_process_query_and_idempotent_replay', 'passed': True})
        payload['execution']['key_columns'] = ['金额']
        request.write_text(json.dumps(payload))
        conflict = command('run', '--request', request, '--db', db, expected=2)
        assert conflict['error_code'] == 'event_conflict'
        checks.append({'check': 'conflicting_event_cannot_reexecute', 'passed': True})

        make_xlsx = "from openpyxl import Workbook; import sys; w=Workbook();s=w.active;s.title='订单';s.append(['订单号','金额','公式']);s.append(['A',10,'=B2*2']);s.append(['A',99,'=B3*2']);s.append(['B',20,'=B4*2']);w.save(sys.argv[1])"
        xlsx = root / 'orders.xlsx'
        subprocess.run([args.python, '-c', make_xlsx, str(xlsx)], cwd=root, env=env, check=True, timeout=10)
        xlsx_hash = sha(xlsx)
        payload['event_id'] = 'xlsx-acceptance'
        payload['state']['input_format'] = 'xlsx'
        payload['state']['constraints'].append('preserve_formulas')
        payload['execution'] = {'input_path': str(xlsx), 'output_path': str(root / 'out.xlsx'),
                                'key_columns': ['订单号'], 'sheet': '订单'}
        request.write_text(json.dumps(payload))
        second = command('run', '--request', request, '--db', db)
        assert second['status'] == 'succeeded' and sha(xlsx) == xlsx_hash
        inspect = "from openpyxl import load_workbook; import json,sys;w=load_workbook(sys.argv[1]);s=w['订单'];print(json.dumps([[c.value for c in r] for r in s.iter_rows()],ensure_ascii=False));w.close()"
        proc = subprocess.run([args.python, '-c', inspect, str(root / 'out.xlsx')], cwd=root, env=env,
                              check=True, capture_output=True, text=True, timeout=10)
        assert json.loads(proc.stdout) == [['订单号', '金额', '公式'], ['A', 10, '=B2*2'], [None, None, None], ['B', 20, '=B4*2']]
        checks.append({'check': 'installed_xlsx_rows_and_formulas_preserved', 'passed': True})
        payload['event_id'] = 'bad-acceptance'
        payload['execution']['key_columns'] = ['不存在列']
        payload['execution']['output_path'] = str(root / 'bad.xlsx')
        request.write_text(json.dumps(payload))
        bad = command('run', '--request', request, '--db', db, expected=2)
        assert bad['status'] == 'rejected' and not (root / 'bad.xlsx').exists()
        assert command('runs', 'show', bad['run_id'], '--db', db)['status'] == 'rejected'
        checks.append({'check': 'rejection_is_durable_and_creates_no_output', 'passed': True})
        payload['event_id'] = 'hermes-protocol'
        payload['state']['input_format'] = 'csv'
        payload['state']['constraints'] = ['preserve_original']
        payload['execution'] = {'input_path': str(source), 'output_path': str(root / 'hermes-out.csv'), 'key_columns': ['订单号']}
        request.write_text(json.dumps(payload))
        event = command('events', 'submit', '--event', request, '--db', db)
        assert event['status'] == 'succeeded' and event['source'] == 'hermes'
        listing = command('runs', 'list', '--db', db)
        assert len(listing['runs']) == 4
        checks.append({'check': 'hermes_json_protocol_not_live_host', 'passed': True})
    result = {'passed': all(c['passed'] for c in checks), 'checks': checks,
              'installed_python': args.python, 'subprocess_validation': True,
              'temporary_fixtures_and_database_cleaned': True,
              'formula_calculation_verified': False, 'live_hermes_host_verified': False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
