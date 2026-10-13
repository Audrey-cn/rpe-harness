import json
import os
import subprocess
import sys

import pytest

from rpe_harness.runtime import HarnessRuntime


@pytest.mark.parametrize('state', [{'task_type': 42}, {'constraints': 'bad-list'}])
def test_route_schema_error_is_json_without_traceback(tmp_path, state):
    payload = tmp_path / 'bad.json'
    payload.write_text(json.dumps(state))
    result = subprocess.run([sys.executable, '-m', 'rpe_harness', 'route', '--request', str(payload),
                             '--db', str(tmp_path / 'runs.sqlite')], capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)['error_code'] == 'invalid_request'
    assert 'Traceback' not in result.stderr


def test_pending_replay_is_not_cli_success(tmp_path):
    payload = {'event_id': 'pending', 'schema_version': 1}
    request = tmp_path / 'pending.json'
    request.write_text(json.dumps(payload))
    db = tmp_path / 'runs.sqlite'
    runtime = HarnessRuntime(db_path=db)
    runtime.store.claim_event('local', 'pending', payload)
    result = subprocess.run([sys.executable, '-m', 'rpe_harness', 'run', '--request', str(request),
                             '--db', str(db)], capture_output=True, text=True)
    assert result.returncode == 3
    assert json.loads(result.stdout)['status'] == 'received'


def test_invalid_state_type_is_persisted_as_rejected(tmp_path):
    source = tmp_path / 'input.csv'
    source.write_text('id\na\n')
    payload = {'schema_version': 1, 'event_id': 'invalid',
               'state': {'task_type': 'data.processing', 'constraints': 'wrong'},
               'execution': {'input_path': str(source), 'output_path': str(tmp_path/'out.csv'),
                             'key_columns': ['id']}}
    result = HarnessRuntime(db_path=tmp_path/'runs.sqlite').run(payload)
    assert result['status'] == 'rejected'
    assert result['error']['code'] == 'invalid_request'


def test_querying_a_rejected_record_is_a_successful_query(tmp_path):
    db = tmp_path / 'runs.sqlite'
    record = HarnessRuntime(db_path=db).run({'event_id': 'bad-query', 'schema_version': 0})
    result = subprocess.run([sys.executable, '-m', 'rpe_harness', 'runs', 'show', record['run_id'],
                             '--db', str(db)], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)['status'] == 'rejected'


@pytest.mark.parametrize('events,expected_exit,expected_status', [
    ([], 1, 'unverified'),
    ([{'kind': 'rpe_task_returned', 'status': 'rejected'}], 1, 'unverified'),
])
def test_hermes_completion_does_not_claim_verified_task_success(tmp_path, monkeypatch, capsys,
                                                               events, expected_exit, expected_status):
    from rpe_harness.cli import main
    from rpe_harness.integrations import hermes_session
    prompt = tmp_path/'prompt.txt'
    prompt.write_text('task')
    monkeypatch.setattr(hermes_session, 'run_hermes_session', lambda *args, **kwargs: {
        'completed': True, 'events': events, 'session_root': 'owned-temp', 'stream_tool_calls': []})
    code = main(['hermes', 'run', '--workspace', str(tmp_path), '--prompt-file', str(prompt),
                 '--db', str(tmp_path/'runs.sqlite')])
    result = json.loads(capsys.readouterr().out)
    assert code == expected_exit
    assert result['status'] == expected_status
    assert result['harness_verified'] is False
    assert result['temporary_history_cleaned'] is True


def test_hermes_cli_defaults_to_required_entry_and_passes_fixed_request(tmp_path, monkeypatch, capsys):
    from rpe_harness.cli import main
    from rpe_harness.integrations import hermes_session

    source = tmp_path / 'input.csv'
    source.write_text('id,value\na,first\na,duplicate\n')
    request = {'schema_version': 2, 'event_id': 'user-contract',
               'capability': 'tabular.deduplicate', 'goal': 'Keep first records',
               'constraints': ['preserve_original', 'preserve_order'],
               'execution': {'input_path': str(source),
                             'output_path': str(tmp_path / 'output.csv'), 'key_columns': ['id']}}
    request_file = tmp_path / 'request.json'
    request_file.write_text(json.dumps(request))
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('Complete the supplied task contract.')
    seen = {}

    def no_model(*args, **kwargs):
        seen.update(kwargs)
        return {'completed': True, 'events': [], 'session_root': 'owned',
                'stream_tool_calls': [], 'exit_code': 0,
                'process_cleanup': {'cleanup_complete': True, 'remaining_descendants': []}}

    monkeypatch.setattr(hermes_session, 'run_hermes_session', no_model)
    code = main(['hermes', 'run', '--workspace', str(tmp_path), '--prompt-file', str(prompt),
                 '--request', str(request_file), '--db', str(tmp_path / 'runs.sqlite')])
    assert code == 1
    assert seen['entry_mode'] == 'strict'
    assert seen['required_request'] == request
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'unverified'
    assert result['harness_verified'] is False
    assert result['temporary_history_cleaned'] is True


def test_evolution_cli_empty_cycle_is_owned_and_queryable(tmp_path):
    db = tmp_path / 'runs.sqlite'
    result = subprocess.run([sys.executable, '-B', '-m', 'rpe_harness', 'evolution', 'cycle',
                             '--owned-root', str(tmp_path), '--db', str(db)],
                            capture_output=True, text=True)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert isinstance(report, dict)
    assert db.is_file()
    query = subprocess.run([sys.executable, '-B', '-m', 'rpe_harness', 'evolution', 'list',
                            '--db', str(db)], capture_output=True, text=True)
    assert query.returncode == 0
    proposals = json.loads(query.stdout)['proposals']
    assert len(proposals) == report['proposal_count']
    assert not report['applied']
    assert all(proposal['kind'] == 'diagnostic' for proposal in proposals)
