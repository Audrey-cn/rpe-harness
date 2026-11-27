"""Fixed CLI consumers preserve durable cancellation and its exit-code contract."""
import io
import json
import subprocess
import sys
from email.message import Message

import pytest
from test_hermes_integration import (
    configured_plugin,
    native_dispatch,
    native_identity,
    request,
    strict_entry,
    strict_request,
)
from test_proposal import confirmed

from rpe_harness.integrations import hermes, hermes_broker, hermes_session
from rpe_harness.integrations.hermes_gate import contract_for, digest_json, request_input_sha
from rpe_harness.storage import RunStore


def cancelled_event(database, payload):
    store = RunStore(database)
    run, _ = store.claim_event('hermes', payload['event_id'], payload)
    return store.cancel(run['run_id'], reason='cancel before worker admission')['run']


def inject_exited_worker(monkeypatch, result, returncode):
    """Inject only the worker wire response for deliberately inconsistent exits."""
    class ExitedWorker:
        pid = 424242

        def __init__(self, *args, **kwargs):
            self.returncode = returncode

        def communicate(self, **kwargs):
            return json.dumps(result), ''

        def poll(self):
            return self.returncode

        def wait(self, **kwargs):
            return self.returncode

    monkeypatch.setattr(subprocess, 'Popen', ExitedWorker)


@pytest.fixture
def process_inventory_unavailable(monkeypatch):
    # These are protocol tests, not evidence of descendant cleanup. The real
    # fixed CLI and cleanup helpers run, without consulting sandbox-denied ps.
    monkeypatch.setattr(hermes_session, '_process_table', lambda: {})


@pytest.fixture
def socketless_broker_server(monkeypatch):
    servers = []

    class SocketlessHTTPServer:
        """Replace only TCP transport; admission, handler and CLI remain real."""
        def __init__(self, address, handler):
            self.server_port = 12345
            self.handler = handler
            servers.append(self)

        def serve_forever(self, **kwargs):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(hermes_broker, 'ThreadingHTTPServer', SocketlessHTTPServer)
    return servers


def broker_post(server, environment, body):
    handler = server.handler.__new__(server.handler)
    encoded = json.dumps(body).encode()
    handler.path = '/task'
    handler.request_version = 'HTTP/1.1'
    handler.requestline = 'POST /task HTTP/1.1'
    handler.command = 'POST'
    handler.headers = Message()
    handler.headers['Authorization'] = 'Bearer ' + environment['RPE_HERMES_BROKER_TOKEN']
    handler.headers['Content-Type'] = 'application/json'
    handler.headers['Content-Length'] = str(len(encoded))
    handler.rfile, handler.wfile = io.BytesIO(encoded), io.BytesIO()
    handler.do_POST()
    headers, _, response = handler.wfile.getvalue().partition(b'\r\n\r\n')
    return int(headers.splitlines()[0].split()[1]), json.loads(response)


@pytest.mark.parametrize('returncode', [1, 0, 2])
def test_hermes_direct_decoder_preserves_cancelled_cli_result(tmp_path, monkeypatch, returncode):
    context, workspace, database = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    canonical = hermes._prepare_request({'request': payload}, workspace, 's')
    cancelled = cancelled_event(database, canonical)
    for name in ('RPE_HERMES_BROKER_URL', 'RPE_HERMES_BROKER_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    if returncode != 1:
        monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], returncode, json.dumps(cancelled), ''))

    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    returned = hermes.HostEventStore(database).list('s')[-1]
    if returncode == 1:
        assert result['status'] == returned['status'] == 'cancelled'
        assert result['run_id'] == returned['run_id'] == cancelled['run_id']
        assert result['stage'] == 'cancel'
        assert result['error'] is None
    else:
        assert result['status'] == returned['status'] == 'failed'
        assert result['error']['code'] == 'harness_protocol_error'
    assert not (workspace / 'out.csv').exists()
    assert RunStore(database).get(cancelled['run_id']) == cancelled


@pytest.mark.parametrize('returncode', [1, 0, 2])
def test_hermes_broker_transport_decoder_preserves_cancelled_result(tmp_path, monkeypatch, returncode):
    context, workspace, database = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    canonical = hermes._prepare_request({'request': payload}, workspace, 's')
    cancelled = cancelled_event(database, canonical)
    monkeypatch.setenv('RPE_HERMES_BROKER_URL', 'http://127.0.0.1:12345/task')
    monkeypatch.setenv('RPE_HERMES_BROKER_TOKEN', 'private-test-token')
    monkeypatch.setattr(hermes, '_broker_exchange', lambda *args: {
        'returncode': returncode, 'result': cancelled})

    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    if returncode == 1:
        assert result['status'] == 'cancelled'
        assert result['run_id'] == cancelled['run_id']
        assert hermes.HostEventStore(database).list('s')[-1]['status'] == 'cancelled'
    else:
        assert result['status'] == 'failed'
        assert result['error']['code'] == 'harness_protocol_error'
    assert not (workspace / 'out.csv').exists()


@pytest.mark.parametrize('returncode', [1, 0, 2])
def test_proposal_worker_decoder_preserves_cancelled_cli_result(
        tmp_path, monkeypatch, process_inventory_unavailable, returncode):
    store, proposal, confirmation = confirmed(tmp_path)
    document = store.resolve(confirmation['confirmation_id'], session_id='s',
                             catalog_sha256=proposal['catalog_sha256'])
    payload = document['request']
    payload['event_id'] = 'review-' + confirmation['confirmation_id']
    cancelled = cancelled_event(store.db_path, payload)
    if returncode != 1:
        inject_exited_worker(monkeypatch, cancelled, returncode)

    result = store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path)
    if returncode == 1:
        assert result['status'] == 'cancelled'
        assert result['run_id'] == cancelled['run_id']
        assert result['stage'] == 'cancel'
        assert result['proposal_id'] == proposal['proposal_id']
        assert store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path) == result
    else:
        assert result['status'] == 'failed'
        assert result['error']['code'] == 'harness_worker_error'
    assert not (tmp_path / 'out.json').exists()
    assert RunStore(store.db_path).get(cancelled['run_id']) == cancelled


@pytest.mark.parametrize('returncode', [1, 0, 2])
def test_broker_handler_decoder_preserves_cancelled_cli_result(
        tmp_path, monkeypatch, process_inventory_unavailable, socketless_broker_server, returncode):
    _, workspace, database = configured_plugin(tmp_path, monkeypatch)
    native = {name: name + '-native' for name in
              ('session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id')}
    payload = hermes._prepare_request({'request': request(workspace)}, workspace, native['session_id'])
    native.update(tool_name='rpe_task', contract_sha256=digest_json(contract_for(payload, workspace)),
                  input_sha256=request_input_sha(payload, workspace))
    receipt = hermes.HostEventStore(database).record('rpe_task_received', native)
    body = {'payload': payload, 'receipt_id': receipt['ingest_event_id'], 'native': native}
    cancelled = cancelled_event(database, payload)
    if returncode != 1:
        inject_exited_worker(monkeypatch, cancelled, returncode)
    with hermes_broker.execution_broker(
            db_path=database, workspace=workspace, python=sys.executable) as environment:
        status, result = broker_post(socketless_broker_server[-1], environment, body)

    if returncode == 1:
        assert status == 200
        assert result['returncode'] == 1
        assert result['result']['status'] == 'cancelled'
        assert result['result']['run_id'] == cancelled['run_id']
        assert result['result']['stage'] == 'cancel'
    else:
        assert status == 502
        assert result['error']['code'] == 'harness_protocol_error'
    assert not (workspace / 'out.csv').exists()
    assert RunStore(database).get(cancelled['run_id']) == cancelled


@pytest.mark.parametrize('queued', [False, True])
def test_cancelled_strict_outcome_blocks_fresh_policy_and_queued_task(tmp_path, monkeypatch, queued):
    context, workspace, database = strict_entry(tmp_path, monkeypatch)
    payload = request(workspace)
    first, following = native_identity(), native_identity(call='queued-call', api='queued-api')
    strict_request(context, first)
    if queued:
        strict_request(context, following)
    canonical = hermes._prepare_request({'request': payload}, workspace, first['session_id'])
    canonical['event_id'] = 'entry-' + digest_json([
        'trusted-guard-entry', digest_json(contract_for(canonical, workspace)),
        request_input_sha(canonical, workspace)])
    cancelled = cancelled_event(database, canonical)
    assert native_dispatch(context, {'request': payload}, first)['status'] == 'cancelled'
    if not queued:
        assert strict_request(context, following)['tool_choice'] == 'none'
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: pytest.fail('closed turn launched CLI'))
    next_payload = {**payload, 'execution': {**payload['execution'], 'output_path': 'next.csv'}}
    blocked = native_dispatch(context, {'request': next_payload}, following)
    assert blocked['status'] == 'rejected'
    assert blocked.get('run_id') is None
    assert blocked['error']['code'] == ('entry_closed' if queued else 'entry_policy_none')
    assert RunStore(database).list() == [cancelled]
    assert not (workspace / 'next.csv').exists()


def test_failed_strict_outcome_keeps_correction_admissible(tmp_path, monkeypatch):
    context, workspace, database = strict_entry(tmp_path, monkeypatch)
    payload = request(workspace)
    first = native_identity()
    strict_request(context, first)
    canonical = hermes._prepare_request({'request': payload}, workspace, first['session_id'])
    canonical['event_id'] = 'entry-' + digest_json([
        'trusted-guard-entry', digest_json(contract_for(canonical, workspace)),
        request_input_sha(canonical, workspace)])
    store = RunStore(database)
    run, _ = store.claim_event('hermes', canonical['event_id'], canonical)
    store.finish(run['run_id'], 'failed', error={'code': 'execution_failed'})
    assert native_dispatch(context, {'request': payload}, first)['status'] == 'failed'
    following = native_identity(call='corrected-call', api='corrected-api')
    assert strict_request(context, following)['tool_choice']['function']['name'] == 'rpe_task'
    corrected = {**payload, 'execution': {**payload['execution'], 'output_path': 'corrected.csv'}}
    assert native_dispatch(context, {'request': corrected}, following)['status'] == 'succeeded'
    assert (workspace / 'corrected.csv').exists()


def broker_body(database, workspace, payload, native):
    native = dict(native)
    payload = hermes._prepare_request({'request': payload}, workspace, native['session_id'])
    native.update(tool_name='rpe_task', contract_sha256=digest_json(contract_for(payload, workspace)),
                  input_sha256=request_input_sha(payload, workspace))
    if native.get('entry_key'):
        payload['event_id'] = 'entry-' + digest_json([
            native['entry_key'], native['contract_sha256'], native['input_sha256']])
    host = hermes.HostEventStore(database)
    receipt = host.record('rpe_task_received', native)
    if native.get('entry_key'):
        host.record('pre_api_request', {**native, 'request': {'body': {
            'tool_choice': {'type': 'function', 'function': {'name': 'rpe_task'}},
            'parallel_tool_calls': False, 'tools': [{'function': {'name': 'rpe_task'}}]}}})
    return {'payload': payload, 'native': native, 'receipt_id': receipt['ingest_event_id']}


@pytest.mark.parametrize('strict', [False, True])
@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('outcome', ['cancelled', 'failed'])
def test_broker_turn_closes_on_cancellation_but_keeps_optional_and_failure_retries(
        tmp_path, monkeypatch, process_inventory_unavailable, socketless_broker_server,
        strict, restart, outcome):
    _, workspace, database = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    native = native_identity()
    if strict:
        native['entry_key'] = 'broker-strict-entry'
    first = broker_body(database, workspace, payload, native)
    second = broker_body(database, workspace,
        {**payload, 'event_id': 'next-event', 'execution': {
            **payload['execution'], 'output_path': 'next.csv'}},
        {**native, 'api_request_id': 'next-api', 'tool_call_id': 'next-call'})
    store = RunStore(database)
    run, _ = store.claim_event('hermes', first['payload']['event_id'], first['payload'])
    if outcome == 'cancelled':
        store.cancel(run['run_id'])
    else:
        store.finish(run['run_id'], 'failed', error={'code': 'execution_failed'})
    with hermes_broker.execution_broker(
            db_path=database, workspace=workspace, python=sys.executable) as environment:
        status, result = broker_post(socketless_broker_server[-1], environment, first)
        assert status == 200 and result['result']['status'] == outcome
        if not restart:
            status, result = broker_post(socketless_broker_server[-1], environment, second)
    if restart:
        host = hermes.HostEventStore(database)
        linked = {**first['native'], 'receipt_id': first['receipt_id'],
                  'run_id': run['run_id'], 'status': outcome}
        host.record('rpe_task_returned', linked)
        host.record('post_tool_call', linked)
        with hermes_broker.execution_broker(
                db_path=database, workspace=workspace, python=sys.executable) as environment:
            status, result = broker_post(socketless_broker_server[-1], environment, second)
    if strict and outcome == 'cancelled':
        assert status == 403
        assert result['error']['code'] == ('broker_receipt_invalid' if restart else 'broker_entry_closed')
        assert len(store.list()) == 1
        assert not (workspace / 'next.csv').exists()
    else:
        assert status == 200 and result['result']['status'] == 'succeeded'
        assert len(store.list()) == 2
        assert (workspace / 'next.csv').exists()
