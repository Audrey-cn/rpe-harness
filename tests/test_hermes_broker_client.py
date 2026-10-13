import json
import subprocess

import pytest

from test_hermes_integration import configured_plugin, integration, request


@pytest.mark.parametrize('url,token', [
    ('http://example.com/task', 'secret'),
    ('http://127.0.0.1:1234/other', 'secret'),
    ('https://127.0.0.1:1234/task', 'secret'),
    ('http://127.0.0.1:1234/task?secret=yes', 'secret'),
    ('http://127.0.0.1:1234/task', ''),
    ('', 'secret'),
])
def test_invalid_broker_configuration_never_falls_back_to_local_executor(
        tmp_path, monkeypatch, url, token):
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    monkeypatch.setenv('RPE_HERMES_BROKER_URL', url)
    monkeypatch.setenv('RPE_HERMES_BROKER_TOKEN', token)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('local fallback'))
    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    assert result['status'] == 'failed'
    assert result['error']['code'] == 'harness_protocol_error'
    assert not (workspace / 'out.csv').exists()
    assert 'secret' not in json.dumps(integration().HostEventStore(db).list())


def test_broker_transport_receives_durable_receipt_and_rechecks_success(
        tmp_path, monkeypatch):
    module = integration()
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    monkeypatch.setenv('RPE_HERMES_BROKER_URL', 'http://127.0.0.1:1234/task')
    monkeypatch.setenv('RPE_HERMES_BROKER_TOKEN', 'transient-private-token')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('local fallback'))

    def exchange(payload, receipt, native):
        rows = module.HostEventStore(db).list('s')
        assert rows[-1]['kind'] == 'rpe_task_received'
        assert rows[-1]['ingest_event_id'] == receipt
        assert rows[-1]['contract_sha256'] == native['contract_sha256']
        assert payload['execution']['input_path'] == str(workspace / 'orders.csv')
        return {'returncode': 0, 'result': {'status': 'succeeded', 'run_id': 'unverified'}}

    monkeypatch.setattr(module, '_broker_exchange', exchange)
    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    assert result['error']['code'] == 'harness_protocol_error'
    assert module.HostEventStore(db).list('s')[-1]['kind'] == 'rpe_task_returned'


def test_broker_timeout_is_durable_without_local_fallback(tmp_path, monkeypatch):
    module = integration()
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    monkeypatch.setenv('RPE_HERMES_BROKER_URL', 'http://127.0.0.1:1234/task')
    monkeypatch.setenv('RPE_HERMES_BROKER_TOKEN', 'transient-private-token')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('local fallback'))

    def timeout(*args):
        raise TimeoutError('bounded broker request')

    monkeypatch.setattr(module, '_broker_exchange', timeout)
    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    assert result['error']['code'] == 'harness_timeout'
    assert module.HostEventStore(db).list('s')[-1]['error_code'] == 'harness_timeout'


@pytest.mark.parametrize('status,expected', [(504, 'harness_timeout'), (403, 'harness_protocol_error'),
                                            (302, 'harness_protocol_error')])
def test_broker_http_failures_are_bounded_durable_failures(tmp_path, monkeypatch, status, expected):
    from urllib.error import HTTPError
    import urllib.request
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    monkeypatch.setenv('RPE_HERMES_BROKER_URL', 'http://127.0.0.1:1234/task')
    monkeypatch.setenv('RPE_HERMES_BROKER_TOKEN', 'transient-private-token')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('local fallback'))

    class FakeOpener:
        def open(self, req, timeout):
            assert req.get_header('Authorization') == 'Bearer transient-private-token'
            assert timeout == 125
            raise HTTPError(req.full_url, status, 'synthetic response', {}, None)

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: FakeOpener())
    result = json.loads(context.tools['rpe_task']['handler']({'request': payload}, session_id='s'))
    assert result['error']['code'] == expected
    assert integration().HostEventStore(db).list('s')[-1]['error_code'] == expected
