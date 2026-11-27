"""Directed probe method checks run no model and use actual CSV contracts."""
import argparse
import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def api():
    spec = importlib.util.spec_from_file_location('directed_probes', Path(__file__).parents[1] / 'scripts/v16_directed_probes.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_valid_fixture_and_empty_key_refusal_use_real_cli(api, tmp_path):
    rows = api.recovery_rows()
    assert all(row[0] for row in rows[1:])
    for empty, expected in ((False, 'succeeded'), (True, 'rejected')):
        source, output = tmp_path / f'{empty}.csv', tmp_path / f'{empty}-out.csv'
        with source.open('w', encoding='utf-8-sig', newline='') as stream:
            csv.writer(stream).writerows(rows + ([['', 'empty-key']] if empty else []))
        request = {'schema_version': 2, 'event_id': 'case-' + str(empty), 'goal': 'retain first literal ids',
            'capability': 'tabular.deduplicate', 'execution': {'input_path': str(source), 'output_path': str(output), 'key_columns': ['id']}}
        child = subprocess.run([sys.executable, '-B', '-m', 'rpe_harness', 'run', '--request', '-', '--db', str(tmp_path / 'runs.sqlite')],
            input=json.dumps(request), capture_output=True, text=True, timeout=20)
        result = json.loads(child.stdout)
        assert result['status'] == expected, result
        if empty:
            assert not output.exists()
        else:
            with output.open(encoding='utf-8-sig', newline='') as stream:
                assert list(csv.reader(stream)) == api.csv_truth(rows)


def test_idless_streams_use_real_receipts_and_counted_arguments(api):
    args = {'name': 'xlsx'}
    identity = {'session_id': 's', 'task_id': 't', 'turn_id': 'turn', 'api_request_id': 'api'}
    call = {**identity, 'tool_call_id': 'call', 'tool_name': 'skill_view', 'arguments_sha256': api.sha(api.canonical(args).encode())}
    events = [{**identity, 'kind': 'pre_api_request'}, {**identity, 'kind': 'post_api_request',
        'model_tool_calls': [{'tool_name': 'skill_view', 'tool_call_id': 'call'}]},
        {**call, 'kind': 'pre_tool_call'}, {**call, 'kind': 'post_tool_call'}]
    streams = [{'name': 'skill_view', 'input': args}]
    assert api.receipt_links(events, streams)['passed'] is True
    assert api.receipt_links(events, streams + streams)['passed'] is False
    events[-1]['tool_call_id'] = 'wrong'
    assert api.receipt_links(events, streams)['passed'] is False


def test_gate_failure_retains_measured_metrics_before_validation(api, tmp_path, monkeypatch):
    from rpe_harness.integrations import hermes_session, skill_gate
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (tmp_path / 'records').mkdir()
    (workspace / 'prompt.txt').write_text('frozen prompt')
    (workspace / 'input.csv').write_text('id,v\na,1\n')
    config = tmp_path / '.hermes/config.yaml'
    config.parent.mkdir()
    config.write_text('frozen-config')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(api, 'package_identity', lambda: {'frozen': True})
    monkeypatch.setattr(hermes_session, 'run_hermes_session', lambda *a, **k: {'completed': True, 'exit_code': 0,
        'session_id': 'measured', 'events': [], 'usage': {'total_tokens': 321}, 'elapsed_seconds': 12.5,
        'model_identity': 'same-model', 'billed_cost': None})
    monkeypatch.setattr(skill_gate, 'evaluate_skill_session', lambda *a, **k: (_ for _ in ()).throw(ValueError('scorer failure')))
    trial = {'trial_id': 'recovery-rpe', 'arm': 'rpe', 'probe_id': 'required-source-context',
        'workspace': str(workspace), 'input_name': 'input.csv', 'output_name': 'out.csv',
        'prompt_sha256': api.sha(b'frozen prompt'), 'input_sha256': api.sha((workspace / 'input.csv').read_bytes()), 'context_sha256': None}
    plan = {'owned_root': str(tmp_path), 'report_dir': str(tmp_path), 'package': {'frozen': True},
        'host_config_sha256': api.sha(config.read_bytes()), 'trials': [trial], 'scope': 'directed', 'fingerprint': 'f',
        'source_payload': {'key_columns': ['id']}, 'db': str(tmp_path / 'state.sqlite'),
        'hermes_root': str(tmp_path), 'timeout': 180, 'catalog_root': str(tmp_path), 'snapshot_id': 'snapshot', 'csv_expected': [['id', 'v'], ['a', '1']]}
    result = api.run(plan, 1)['records'][0]
    assert result['status'] == 'failed'
    assert result['usage'] == {'total_tokens': 321} and result['elapsed_seconds'] == 12.5
    assert result['session_id'] == 'measured' and result['model_identity'] == 'same-model'


def test_preregister_rejects_old_version_marker_before_reusing(api, tmp_path, monkeypatch):
    import shutil
    from rpe_harness.skill_catalog import SkillCatalog
    source = tmp_path / 'sources'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx', source / 'productivity/xlsx')
    catalog = SkillCatalog(tmp_path / 'state.sqlite', tmp_path / 'catalog')
    catalog.sync(source)
    catalog.trust_adapter('productivity/xlsx')
    config = tmp_path / '.hermes/config.yaml'
    config.parent.mkdir()
    config.write_text('frozen-config')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(api, 'package_identity', lambda: {'frozen': True})
    reports = tmp_path / 'reports'
    reports.mkdir()
    old = {'directed_version': 'old-invalid-fixture'}
    old['fingerprint'] = api.sha(api.canonical(old).encode())
    (reports / 'preregistration.json').write_text(json.dumps(old))
    args = argparse.Namespace(db=tmp_path / 'state.sqlite', owned_root=tmp_path / 'owned', report_dir=reports,
        catalog_root=tmp_path / 'catalog', hermes_root=tmp_path, helper_python=Path(sys.executable),
        probe_only='required-source-context')
    with pytest.raises(ValueError, match='frozen preregistration differs'):
        api.preregister(args)
    assert json.loads((reports / 'preregistration.json').read_text()) == old


def test_probe_only_cli_registers_without_model(api, tmp_path, monkeypatch):
    captured = {}
    def registration(args):
        captured['probe_only'] = args.probe_only
        return {'registered': True}
    monkeypatch.setattr(api, 'preregister', registration)
    monkeypatch.setattr(api, 'offline', lambda plan: {'passed': True})
    def no_model(plan, limit):
        captured['limit'] = limit
        return {'passed': False, 'records': []}
    monkeypatch.setattr(api, 'run', no_model)
    arguments = []
    for flag in ('db', 'owned-root', 'catalog-root', 'hermes-root', 'report-dir', 'helper-python'):
        arguments.extend(['--' + flag, str(tmp_path)])
    arguments.extend(['--probe-only', 'required-source-context'])
    assert api.main(arguments) == 0
    assert captured == {'probe_only': 'required-source-context', 'limit': 0}
