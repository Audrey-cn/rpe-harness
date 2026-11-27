"""Raw HTTP cancellation requests through the real handler without a listener."""
import http.client
import io
import json
from types import SimpleNamespace

import pytest

from rpe_harness.runtime import HarnessRuntime
from rpe_harness.server import HarnessHandler


class MemoryTransport:
    """Replace only the request socket's byte transport."""

    def __init__(self, incoming):
        self.incoming = incoming
        self.outgoing = bytearray()

    def makefile(self, mode, buffering=-1):
        assert mode == 'rb'
        return io.BytesIO(self.incoming)

    def sendall(self, data):
        self.outgoing.extend(data)

    def settimeout(self, timeout):
        pass

    def shutdown(self, direction):
        pass


def post(runtime, path, body=b'{}', *, headers=None):
    fields = {'Host': '127.0.0.1:18400', 'Content-Type': 'application/json',
              'Content-Length': str(len(body)), **(headers or {})}
    request = ('POST ' + path + ' HTTP/1.0\r\n' + ''.join(
        name + ': ' + value + '\r\n' for name, value in fields.items()) + '\r\n')
    transport = MemoryTransport(request.encode('ascii') + body)
    server = SimpleNamespace(runtime=runtime, request_timeout=2, server_port=18400)
    HarnessHandler(transport, ('127.0.0.1', 12345), server)
    response = http.client.HTTPResponse(MemoryTransport(bytes(transport.outgoing)))
    try:
        response.begin()
        assert response.getheader('Content-Type') == 'application/json; charset=utf-8'
        return response.status, json.loads(response.read())
    finally:
        response.close()


@pytest.fixture
def pending(tmp_path):
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    record, _ = runtime.store.claim_event('http', 'pending', {'event_id': 'pending'})
    return runtime, record


@pytest.mark.parametrize('previous', ['received', 'running', 'interrupted'])
def test_raw_http_cancel_is_durable_and_idempotent(pending, previous):
    runtime, record = pending
    run_id = record['run_id']
    if previous == 'running':
        runtime.store.mark_running(run_id)
    elif previous == 'interrupted':
        runtime.store.finish(run_id, 'interrupted')
    path = '/runs/' + run_id + '/cancel'

    status, result = post(runtime, path, json.dumps({'reason': '停止任务'}).encode())
    assert status == 200
    assert result['status'] == 'cancelled'
    assert result['run_id'] == run_id
    assert result['already_cancelled'] is False
    saved = runtime.store.get(run_id)
    assert result['run'] == saved
    assert saved['status'] == 'cancelled'
    assert saved['steps'][-1]['evidence']['reason'] == '停止任务'

    status, repeated = post(runtime, path, b'{"reason":"later"}')
    assert status == 200
    assert repeated['already_cancelled'] is True
    assert repeated['run'] == saved


@pytest.mark.parametrize('body', [b'', b'{}', b'{"reason":null}'])
def test_raw_http_cancel_reason_is_optional(pending, body):
    runtime, record = pending
    status, result = post(runtime, '/runs/' + record['run_id'] + '/cancel', body)
    assert status == 200
    assert result['run']['status'] == 'cancelled'
    assert result['run']['steps'][-1]['evidence']['reason'] is None


def test_raw_http_cancel_missing_run_is_404(pending):
    runtime, record = pending
    status, result = post(runtime, '/runs/missing/cancel')
    assert status == 404
    assert 'error' in result
    assert runtime.store.get(record['run_id']) == record


@pytest.mark.parametrize('previous', ['succeeded', 'rejected', 'failed', 'publish'])
def test_raw_http_cancel_conflict_returns_true_record(pending, tmp_path, previous):
    runtime, record = pending
    run_id = record['run_id']
    artifact = tmp_path / 'published.csv'
    artifact.write_bytes(b'preserve this artifact\n')
    if previous == 'publish':
        runtime.store.mark_running(run_id)
        runtime.store.step(run_id, 'publish', {'output_path': str(artifact)})
    elif previous == 'succeeded':
        runtime.store.finish(run_id, previous, verification={'passed': True},
                             artifact={'path': str(artifact), 'published': True})
    else:
        runtime.store.finish(run_id, previous)
    saved = runtime.store.get(run_id)

    status, result = post(runtime, '/runs/' + run_id + '/cancel')
    assert status == 409
    assert result['status'] == 'conflict'
    assert result['error_code'] == (
        'publication_started' if previous == 'publish' else 'already_terminal')
    assert result['run'] == saved
    assert runtime.store.get(run_id) == saved
    assert artifact.read_bytes() == b'preserve this artifact\n'


@pytest.mark.parametrize('headers,body,expected', [
    ({'Origin': 'https://untrusted.example'}, b'{}', 403),
    ({'Content-Type': 'text/plain'}, b'{}', 415),
    ({'Transfer-Encoding': 'chunked'}, b'{}', 400),
    ({'Content-Length': 'nope'}, b'{}', 400),
    ({'Content-Length': '-1'}, b'{}', 400),
    ({'Content-Length': '1048577'}, b'{}', 413),
    ({'Content-Length': '100'}, b'{}', 400),
    ({}, b'{bad', 400),
    ({}, b'null', 400),
    ({}, b'[]', 400),
    ({}, b'"text"', 400),
    ({}, b'\xff', 400),
    ({}, b'{"unexpected":true}', 400),
    ({}, b'{"reason":false}', 400),
    ({}, b'{"reason":""}', 400),
    ({}, b'{"reason":"  "}', 400),
    ({}, json.dumps({'reason': '界' * 1366}).encode(), 400),
])
def test_raw_http_cancel_guards_reject_without_mutation(pending, headers, body, expected):
    runtime, record = pending
    status, result = post(runtime, '/runs/' + record['run_id'] + '/cancel', body,
                          headers=headers)
    assert status == expected
    assert 'error' in result
    assert runtime.store.get(record['run_id']) == record


@pytest.mark.parametrize('path,source', [('/run', 'http'), ('/events', 'hermes')])
def test_raw_http_cancelled_replay_returns_409(tmp_path, path, source):
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    payload = {'event_id': 'cancelled-replay'}
    record, _ = runtime.store.claim_event(source, payload['event_id'], payload)
    status, _ = post(runtime, '/runs/' + record['run_id'] + '/cancel')
    assert status == 200

    status, replay = post(runtime, path, json.dumps(payload).encode())
    assert status == 409
    assert replay['status'] == 'cancelled'
    assert replay == runtime.store.get(record['run_id'])
