#!/usr/bin/env python3
"""Installed JSON upgrade/unattended acceptance with explicit owned state only."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def validate(root, candidate, report_path):
    import rpe_harness
    from rpe_harness.evolution import cycle, EvolutionStore
    from rpe_harness.runtime import HarnessRuntime
    from rpe_harness.experiments.unattended_sequence import run_sequence
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'version': rpe_harness.__version__, 'module_path': rpe_harness.__file__,
              'proposal_origin': 'explicit_external_candidate_file', 'exceptions': [], 'checks': [],
              'global_changes_by_runner': False, 'autonomous_code_generation_claimed': False}
    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    save()
    try:
        assert rpe_harness.__version__ == '1.5.0' and '/site-packages/' in rpe_harness.__file__
        report['unattended'] = run_sequence(root / 'unattended', candidate_path=candidate)
        assert report['unattended']['passed'] is True
        save()
        state = root / 'state'
        state.mkdir()
        db = state / 'runs.sqlite'
        source, output = state / 'seed.json', state / 'seed-result.json'
        source.write_bytes(b' { "values": [9007199254740993, 1.2300, 1e999, -0], "text": "\\u4e2d  x" } ')
        request = {'schema_version': 2, 'event_id': 'installed-json-seed', 'goal': 'preserve all raw JSON tokens',
            'capability': 'json.normalize', 'constraints': ['preserve_original', 'preserve_order'],
            'execution': {'input_path': str(source), 'output_path': str(output), 'style': 'minify'}}
        seed = HarnessRuntime(db_path=db).run(request)
        assert seed['status'] == 'succeeded'
        assert output.read_bytes() == b'{"values":[9007199254740993,1.2300,1e999,-0],"text":"\\u4e2d  x"}\n'
        declaration = cycle(db, state)
        assert any(item['kind'] == 'json_recipe' for item in declaration['applied'])
        code = candidate.read_text()
        def proposer(context):
            assert all(f['plan']['capability'] == 'json.normalize' for f in context['verified_feedback'])
            return [{'component': 'executor', 'kind': 'json_executor', 'content': code,
                     'evidence_run_ids': [seed['run_id']]}]
        upgraded = cycle(db, state, proposer=proposer)
        applied = [item for item in upgraded['applied'] if item['kind'] == 'json_executor']
        assert len(applied) == 1
        active = EvolutionStore(db).get_version(applied[0]['version_id'])
        report['host_candidate'] = {'db_path': str(db), 'version_id': active['version_id'],
            'seed_run_id': seed['run_id'], 'content_sha256': active['content_hash'],
            'independent_fixture_checks': len(active['validation']['checks']),
            'validation': active['validation'], 'declaration_cycle': declaration, 'code_cycle': upgraded}
        # CSV still executes through its original registered authority in a fresh CLI process.
        csv_source, csv_output = state / 'orders.csv', state / 'orders-result.csv'
        csv_source.write_bytes(b'id,value\n001,first\n001,later\n002,last\n')
        payload = {'schema_version': 2, 'event_id': 'installed-csv-regression', 'goal': 'keep first by id',
            'capability': 'tabular.deduplicate', 'execution': {'input_path': str(csv_source),
                'output_path': str(csv_output), 'key_columns': ['id']}}
        request_file = state / 'csv-request.json'
        request_file.write_text(json.dumps(payload))
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(root))
        environment.pop('PYTHONPATH', None)
        process = subprocess.run([sys.executable, '-B', '-m', 'rpe_harness', 'run', '--request',
            str(request_file), '--db', str(db)], env=environment, capture_output=True, text=True, timeout=30)
        assert process.returncode == 0
        result = json.loads(process.stdout)
        assert result['status'] == 'succeeded'
        assert csv_output.read_bytes() == 'id,value\r\n001,first\r\n002,last\r\n'.encode('utf-8-sig')
        report['checks'].append({'check': 'installed_csv_regression', 'passed': True,
                                'actual_executor': result['artifact']['actual_executor']})
        report['passed'] = True
    except Exception as error:
        report['exceptions'].append({'type': type(error).__name__})
    finally:
        save()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owned-root', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = validate(args.owned_root, args.candidate, args.report)
    print(json.dumps({key: result[key] for key in ('passed', 'version', 'module_path', 'exceptions')}))
    raise SystemExit(0 if result['passed'] else 1)
