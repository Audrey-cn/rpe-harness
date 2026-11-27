import http.client
import json
import socket
import threading
import time

import pytest

from rpe_harness.runtime import HarnessRuntime
from rpe_harness.server import create_server


@pytest.fixture
def api(tmp_path):
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    server = create_server(runtime, port=0, request_timeout=0.4)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address, runtime
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def call(address, path, payload=None, *, raw=None):
    connection = http.client.HTTPConnection(*address, timeout=2)
    body = raw if raw is not None else json.dumps(payload) if payload is not None else None
    connection.request('POST' if body is not None else 'GET', path, body,
                       {'Content-Type': 'application/json'})
    response = connection.getresponse()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


@pytest.mark.parametrize('raw', ['[1,2]', '{bad', 'null', '"text"'])
def test_malformed_json_does_not_disconnect_or_kill_server(api, raw):
    address, _ = api
    assert call(address, '/route', raw=raw)[0] == 400
    assert call(address, '/health')[0] == 200


def test_unknown_and_invalid_topk_reject_wrong_skill(api):
    address, _ = api
    status, result = call(address, '/route', {'goal': '未知任务'})
    assert status == 200
    assert result['fallback_to_llm'] and result['candidates'] == []
    assert call(address, '/route', {'task_type': 'web.research', 'goal': '查文献'})[0] == 200
    assert call(address, '/route', {'top_k': True})[0] == 400


def test_partial_body_cannot_block_health_and_times_out(api):
    address, _ = api
    with socket.create_connection(address, timeout=2) as slow:
        slow.sendall((f'POST /route HTTP/1.0\r\nHost: {address[0]}:{address[1]}\r\n'
                      'Content-Length: 100\r\nContent-Type: application/json\r\n\r\n{').encode())
        start = time.monotonic()
        assert call(address, '/health')[0] == 200
        assert time.monotonic() - start < 0.3
        assert b'408' in slow.recv(4096)


def test_bad_length_large_body_and_unknown_route_have_json_errors(api):
    address, _ = api
    for length, expected in [('nope', b'400'), ('-1', b'400'), ('99999999', b'413')]:
        with socket.create_connection(address, timeout=2) as sock:
            sock.sendall((f'POST /route HTTP/1.0\r\nHost: {address[0]}:{address[1]}\r\n'
                          f'Content-Length: {length}\r\n\r\n').encode())
            assert expected in sock.recv(4096)
    assert call(address, '/missing', {})[0] == 404


def test_usage_requires_real_skill_boolean_and_survives_reload(api):
    address, runtime = api
    assert call(address, '/usage', {'skill_id': 'missing', 'success': True})[0] == 400
    assert call(address, '/usage', {'skill_id': 'builtin/csv-deduplicate', 'success': 'false'})[0] == 400
    assert call(address, '/usage', {'skill_id': 'builtin/csv-deduplicate', 'success': False})[0] == 200
    assert call(address, '/reload', {})[0] == 200
    usage = call(address, '/stats')[1]['usage']['builtin/csv-deduplicate']
    assert usage['reported_count'] == 1 and usage['verified_success_count'] == 0
    assert runtime.router.skills['builtin/csv-deduplicate'].use_count == 1


def test_trickled_body_has_absolute_deadline(api):
    address, _ = api
    with socket.create_connection(address, timeout=2) as slow:
        slow.sendall((f'POST /route HTTP/1.0\r\nHost: {address[0]}:{address[1]}\r\n'
                      'Content-Length: 100\r\nContent-Type: application/json\r\n\r\n{').encode())
        started = time.monotonic()
        for _ in range(4):
            time.sleep(0.12)
            try:
                slow.sendall(b' ')
            except OSError:
                break
        response = slow.recv(4096)
        assert time.monotonic() - started < 0.7
        assert not response or b'408' in response


def test_cross_origin_plain_post_cannot_change_skill_state(api):
    address, runtime = api
    connection = http.client.HTTPConnection(*address, timeout=2)
    connection.request('POST', '/skills/state', json.dumps({'skill_id': 'builtin/csv-deduplicate', 'enabled': False}),
                       {'Origin': 'https://untrusted.example', 'Content-Type': 'text/plain'})
    response = connection.getresponse()
    assert response.status == 403
    response.read()
    connection.close()
    assert runtime.router.skills['builtin/csv-deduplicate'].enabled


def test_plain_json_without_origin_is_rejected(api):
    address, _ = api
    connection = http.client.HTTPConnection(*address, timeout=2)
    connection.request('POST', '/reload', '{}', {'Content-Type': 'text/plain'})
    response = connection.getresponse()
    assert response.status == 415
    response.read()
    connection.close()
