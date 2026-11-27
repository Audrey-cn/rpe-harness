"""Cancellation is available through the CLI and guarded loopback API."""
from http.client import HTTPConnection
import json
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from rpe_harness.runtime import HarnessRuntime
from rpe_harness.server import create_server


def claim(runtime, event_id='pending', source='local'):
    payload = {'event_id': event_id, 'schema_version': 1}
    record, fresh = runtime.store.claim_event(source, event_id, payload)
    assert fresh
    return record, payload


def cli(database, *arguments):
    completed = subprocess.run(
        [sys.executable, '-B', '-m', 'rpe_harness', 'runs', *arguments,
         '--db', str(database)], capture_output=True, text=True, timeout=10)
    assert 'Traceback' not in completed.stderr
    assert completed.stdout.strip(), completed.stderr
    return completed.returncode, json.loads(completed.stdout)


@pytest.mark.parametrize('reason', [None, '用户停止本次任务'])
def test_cli_cancel_is_durable_idempotent_and_queryable(tmp_path, reason):
    database = tmp_path / 'runs.sqlite'
    runtime = HarnessRuntime(db_path=database)
    record, _ = claim(runtime)
    arguments = ['cancel', record['run_id']]
    if reason is not None:
        arguments += ['--reason', reason]

    code, result = cli(database, *arguments)
    assert code == 0
    assert result['status'] == 'cancelled'
    assert result['run_id'] == record['run_id']
    assert result['already_cancelled'] is False
    saved = runtime.store.get(record['run_id'])
    assert result['run'] == saved
    assert saved['status'] == 'cancelled'

    code, repeated = cli(database, 'cancel', record['run_id'], '--reason', 'a later reason')
    assert code == 0
    assert repeated['already_cancelled'] is True
    assert repeated['run'] == saved

    assert cli(database, 'show', record['run_id']) == (0, saved)
    code, listing = cli(database, 'list')
    assert code == 0
    assert listing['runs'] == [saved]


@pytest.mark.parametrize('terminal', ['failed', 'rejected'])
def test_cli_cancel_terminal_conflict_has_nonzero_exit_and_true_record(tmp_path, terminal):
    database = tmp_path / 'runs.sqlite'
    runtime = HarnessRuntime(db_path=database)
    record, _ = claim(runtime)
    finished = runtime.store.finish(record['run_id'], terminal)

    code, result = cli(database, 'cancel', record['run_id'])
    assert code == 2
    assert result['status'] == 'conflict'
    assert result['error_code'] == 'already_terminal'
    assert result['run'] == finished
    assert cli(database, 'show', record['run_id']) == (0, finished)


def test_cli_cancel_interrupted_run_succeeds(tmp_path):
    database = tmp_path / 'runs.sqlite'
    runtime = HarnessRuntime(db_path=database)
    record, _ = claim(runtime)
    runtime.store.finish(record['run_id'], 'interrupted')

    code, result = cli(database, 'cancel', record['run_id'])
    assert code == 0
    assert result['status'] == 'cancelled'
    assert result['already_cancelled'] is False
    assert result['run']['status'] == 'cancelled'
    assert result['run'] == runtime.store.get(record['run_id'])


@pytest.mark.parametrize('command,source', [(['run', '--request'], 'local'),
                                         (['events', 'submit', '--event'], 'hermes')])
def test_cli_cancelled_replay_is_not_success(tmp_path, command, source):
    database = tmp_path / 'runs.sqlite'
    runtime = HarnessRuntime(db_path=database)
    record, payload = claim(runtime, source=source)
    saved = runtime.cancel(record['run_id'])['run']
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(payload))

    result = subprocess.run(
        [sys.executable, '-B', '-m', 'rpe_harness', *command, str(request),
         '--db', str(database)], capture_output=True, text=True, timeout=10)
    assert 'Traceback' not in result.stderr
    assert result.returncode == 1
    assert json.loads(result.stdout) == saved
    assert runtime.store.list() == [saved]


def test_cli_cancel_publication_conflict_preserves_running_record(tmp_path):
    database = tmp_path / 'runs.sqlite'
    runtime = HarnessRuntime(db_path=database)
    record, _ = claim(runtime)
    runtime.store.mark_running(record['run_id'])
    runtime.store.step(record['run_id'], 'publish', {'publication_confirmed': False})
    prior = runtime.store.get(record['run_id'])

    code, result = cli(database, 'cancel', record['run_id'])
    assert code == 2
    assert result['status'] == 'conflict'
    assert result['error_code'] == 'publication_started'
    assert result['run'] == prior


def test_cli_cancel_unknown_run_is_json_error(tmp_path):
    code, result = cli(tmp_path / 'runs.sqlite', 'cancel', 'missing')
    assert code == 2
    assert result['status'] == 'error'
    assert 'missing' in result['error']


@pytest.mark.parametrize('reason', ['', '  ', '界' * 1366])
def test_cli_cancel_invalid_reason_does_not_cancel(tmp_path, reason):
    database = tmp_path / 'runs.sqlite'
    runtime = HarnessRuntime(db_path=database)
    record, _ = claim(runtime)
    code, result = cli(database, 'cancel', record['run_id'], '--reason', reason)
    assert code == 2
    assert result['status'] == 'error'
    assert result['error_code'] == 'invalid_request'
    assert runtime.store.get(record['run_id']) == record


@pytest.fixture
def api(tmp_path):
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    server = create_server(runtime, port=0, request_timeout=0.3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address, runtime
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def http(address, path, payload=None, *, raw=None, headers=None, method='POST'):
    connection = HTTPConnection(*address, timeout=3)
    body = raw if raw is not None else json.dumps(payload) if payload is not None else None
    try:
        connection.request(method, path, body, headers or {'Content-Type': 'application/json'})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize('body', [{}, {'reason': None}, {'reason': '停止任务'}])
def test_http_cancel_is_durable_and_idempotent(api, body):
    address, runtime = api
    record, _ = claim(runtime)
    path = '/runs/' + record['run_id'] + '/cancel'
    status, result = http(address, path, body)
    assert status == 200
    assert result['status'] == 'cancelled'
    assert result['run_id'] == record['run_id']
    assert result['already_cancelled'] is False
    saved = runtime.store.get(record['run_id'])
    assert result['run'] == saved
    assert saved['status'] == 'cancelled'
    status, repeated = http(address, path, {'reason': 'different'})
    assert status == 200
    assert repeated['already_cancelled'] is True
    assert repeated['run'] == saved
    assert http(address, '/runs/' + record['run_id'], method='GET') == (200, saved)


def test_http_cancel_accepts_empty_body(api):
    address, runtime = api
    record, _ = claim(runtime)
    status, result = http(address, '/runs/' + record['run_id'] + '/cancel')
    assert status == 200
    assert result['run']['status'] == 'cancelled'


def test_http_cancel_unknown_run_is_404(api):
    address, _ = api
    status, result = http(address, '/runs/missing/cancel', {})
    assert status == 404
    assert 'error' in result


@pytest.mark.parametrize('terminal', ['failed', 'rejected'])
def test_http_cancel_terminal_conflict_returns_true_record(api, terminal):
    address, runtime = api
    record, _ = claim(runtime)
    saved = runtime.store.finish(record['run_id'], terminal)
    status, result = http(address, '/runs/' + record['run_id'] + '/cancel', {})
    assert status == 409
    assert result['status'] == 'conflict'
    assert result['error_code'] == 'already_terminal'
    assert result['run'] == saved


def test_http_cancel_interrupted_run_succeeds(api):
    address, runtime = api
    record, _ = claim(runtime)
    runtime.store.finish(record['run_id'], 'interrupted')

    status, result = http(address, '/runs/' + record['run_id'] + '/cancel', {})
    assert status == 200
    assert result['status'] == 'cancelled'
    assert result['already_cancelled'] is False
    assert result['run']['status'] == 'cancelled'
    assert result['run'] == runtime.store.get(record['run_id'])


def test_http_cancel_after_publish_intent_conflicts(api):
    address, runtime = api
    record, _ = claim(runtime)
    runtime.store.mark_running(record['run_id'])
    runtime.store.step(record['run_id'], 'publish', {'publication_confirmed': False})
    saved = runtime.store.get(record['run_id'])
    status, result = http(address, '/runs/' + record['run_id'] + '/cancel', {})
    assert status == 409
    assert result['error_code'] == 'publication_started'
    assert result['run'] == saved


@pytest.mark.parametrize('path,source', [('/run', 'http'), ('/events', 'hermes')])
def test_http_cancelled_event_replay_is_409(api, path, source):
    address, runtime = api
    record, payload = claim(runtime, source=source)
    cancel_status, _ = http(address, '/runs/' + record['run_id'] + '/cancel', {})
    assert cancel_status == 200
    status, result = http(address, path, payload)
    assert status == 409
    assert result['status'] == 'cancelled'
    assert result['run_id'] == record['run_id']
    assert runtime.store.list() == [result]


@pytest.mark.parametrize('body', [
    {'reason': ''}, {'reason': '  '}, {'reason': False}, {'reason': []},
    {'reason': '界' * 1366}, {'reason': 'valid', 'unexpected': True}, {'run_id': 'other'},
])
def test_http_cancel_rejects_invalid_or_unknown_fields_without_mutation(api, body):
    address, runtime = api
    record, _ = claim(runtime)
    status, result = http(address, '/runs/' + record['run_id'] + '/cancel', body)
    assert status == 400
    assert 'error' in result
    assert runtime.store.get(record['run_id']) == record


@pytest.mark.parametrize('raw', ['{bad', 'null', '[1]', '"text"'])
def test_http_cancel_reuses_json_object_guards(api, raw):
    address, runtime = api
    record, _ = claim(runtime)
    status, result = http(address, '/runs/' + record['run_id'] + '/cancel', raw=raw)
    assert status == 400
    assert 'error' in result
    assert runtime.store.get(record['run_id']) == record


@pytest.mark.parametrize('headers,expected', [
    ({'Origin': 'https://untrusted.example', 'Content-Type': 'application/json'}, 403),
    ({'Content-Type': 'text/plain'}, 415),
    ({'Transfer-Encoding': 'chunked', 'Content-Type': 'application/json'}, 400),
])
def test_http_cancel_reuses_origin_content_type_and_transfer_guards(api, headers, expected):
    address, runtime = api
    record, _ = claim(runtime)
    status, result = http(address, '/runs/' + record['run_id'] + '/cancel', {}, headers=headers)
    assert status == expected
    assert 'error' in result
    assert runtime.store.get(record['run_id']) == record


@pytest.mark.parametrize('length,expected', [('nope', 400), ('-1', 400), ('1048577', 413)])
def test_http_cancel_reuses_length_and_size_guards(api, length, expected):
    address, runtime = api
    record, _ = claim(runtime)
    path = '/runs/' + record['run_id'] + '/cancel'
    with socket.create_connection(address, timeout=3) as connection:
        connection.sendall(
            f'POST {path} HTTP/1.0\r\nContent-Length: {length}\r\n\r\n'.encode())
        assert f' {expected} '.encode() in connection.recv(4096)
    assert runtime.store.get(record['run_id']) == record


def test_http_cancel_incomplete_body_times_out_without_mutation(api):
    address, runtime = api
    record, _ = claim(runtime)
    path = '/runs/' + record['run_id'] + '/cancel'
    with socket.create_connection(address, timeout=3) as connection:
        connection.sendall(
            f'POST {path} HTTP/1.0\r\nContent-Length: 100\r\n'
            'Content-Type: application/json\r\n\r\n{'.encode())
        assert b' 408 ' in connection.recv(4096)
    assert runtime.store.get(record['run_id']) == record


def test_http_can_cancel_a_request_waiting_in_execution(api, tmp_path, monkeypatch):
    from rpe_harness import runtime as runtime_module

    address, runtime = api
    source = tmp_path / 'input.csv'
    output = tmp_path / 'output.csv'
    source.write_text('id,value\na,first\na,duplicate\n')
    payload = {'schema_version': 2, 'event_id': 'active-http-run',
               'capability': 'tabular.deduplicate', 'goal': 'Keep first records',
               'constraints': ['preserve_original', 'preserve_order'],
               'execution': {'input_path': str(source), 'output_path': str(output),
                             'key_columns': ['id']}}
    reached = threading.Event()
    release = threading.Event()
    original = runtime_module.prepare_plan

    def paused_prepare(plan):
        prepared = original(plan)
        reached.set()
        assert release.wait(5), 'test failed to release execution'
        return prepared

    monkeypatch.setattr(runtime_module, 'prepare_plan', paused_prepare)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(http, address, '/run', payload)
        try:
            assert reached.wait(3), 'HTTP execution did not reach preparation'
            record = runtime.store.list()[0]
            status, result = http(address, '/runs/' + record['run_id'] + '/cancel',
                                  {'reason': 'Cancel active execution'})
            assert status == 200
            assert result['status'] == 'cancelled'
        finally:
            release.set()
        status, returned = request.result(timeout=5)
    assert status == 409
    assert returned['status'] == 'cancelled'
    assert returned == runtime.store.get(record['run_id'])
    assert not output.exists()
    assert source.read_text() == 'id,value\na,first\na,duplicate\n'
    assert not list(tmp_path.glob('.rpe-*'))
