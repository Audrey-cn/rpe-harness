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
