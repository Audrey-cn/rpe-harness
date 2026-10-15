"""Explicit isolated credentials never depend on or modify the daily host profile."""
import json
import os
from pathlib import Path
import sys

import pytest

from test_hermes_request_budget import budget_profile


SECRET = 'FAKE_PRIVATE_HOST_KEY'


def settings(**changes):
    return {'model': 'mimo-v2.6-pro', 'provider': 'xiaomi',
            'base_url': 'https://example.invalid/v1', 'api_key': SECRET, **changes}


def test_host_settings_file_loads_only_four_fields(tmp_path):
    from rpe_harness.integrations import hermes_session
    path = tmp_path / 'host-settings.json'
    path.write_text(json.dumps(settings()))
    assert hermes_session._load_host_settings(path) == settings()


@pytest.mark.parametrize('fault', ['duplicate', 'extra', 'missing', 'provider', 'empty',
    'http', 'userinfo', 'query', 'fragment', 'newline', 'json', 'encoding', 'large',
    'directory', 'symlink', 'fifo'])
def test_host_settings_rejects_unsafe_or_invalid_file_without_secret(tmp_path, fault):
    from rpe_harness.integrations import hermes_session
    path = tmp_path / 'host-settings.json'
    value = settings()
    if fault == 'extra':
        value['other'] = SECRET
    elif fault == 'missing':
        value.pop('model')
    elif fault == 'provider':
        value['provider'] = 'nous'
    elif fault == 'empty':
        value['api_key'] = ''
    elif fault in ('http', 'userinfo', 'query', 'fragment'):
        value['base_url'] = {'http': 'http://example.invalid/v1',
            'userinfo': 'https://user:password@example.invalid/v1',
            'query': 'https://example.invalid/v1?key=' + SECRET,
            'fragment': 'https://example.invalid/v1#' + SECRET}[fault]
    elif fault == 'newline':
        value['api_key'] += '\n'
    if fault == 'duplicate':
        path.write_text(json.dumps(value)[:-1] + ',"api_key":"other"}')
    elif fault == 'json':
        path.write_text('{"api_key":"' + SECRET)
    elif fault == 'encoding':
        path.write_bytes(b'\xff' + SECRET.encode())
    elif fault == 'large':
        path.write_text(json.dumps(settings(api_key=SECRET * 2000)))
    elif fault == 'directory':
        path.mkdir()
    elif fault == 'symlink':
        target = tmp_path / 'target.json'
        target.write_text(json.dumps(value))
        path.symlink_to(target)
    elif fault == 'fifo':
        os.mkfifo(path)
    else:
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError) as caught:
        hermes_session._load_host_settings(path)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize('explicit', [False, True])
def test_session_uses_explicit_settings_or_unchanged_default_reader(tmp_path, monkeypatch, explicit):
    from rpe_harness.integrations import hermes_session
    class Prepared(Exception):
        pass
    default_reads, prepared = [], []
    def read(*_):
        default_reads.append(True)
        return settings(model='daily-test-model')
    def prepare(*args, **kwargs):
        prepared.append(kwargs['settings'])
        raise Prepared()
    monkeypatch.setattr(hermes_session, '_installed_python', lambda *_: Path(sys.executable))
    monkeypatch.setattr(hermes_session, '_read_host_settings', read)
    monkeypatch.setattr(hermes_session, 'prepare_home', prepare)
    source = settings()
    with pytest.raises(Prepared):
        hermes_session.run_hermes_session('Exact business request.', tmp_path, tmp_path/'s.db',
            tool_enabled=True, owner_root=tmp_path/'owned',
            **({'host_settings': source} if explicit else {}))
    assert len(default_reads) == (0 if explicit else 1)
    assert prepared == [source if explicit else settings(model='daily-test-model')]
    if explicit:
        assert prepared[0] is not source
    assert source == settings()


def test_explicit_host_settings_reuses_endpoint_and_budget_validation_before_home(tmp_path):
    from rpe_harness.integrations.hermes_session import prepare_home
    with pytest.raises(ValueError, match='model differs'):
        prepare_home(tmp_path/'session', tmp_path, tmp_path/'s.db', tool_enabled=True,
                     settings=settings(model='different-model'), request_budget=budget_profile())
    assert not (tmp_path/'session').exists()


def test_review_wrapper_forwards_explicit_settings_without_returning_credentials(tmp_path, monkeypatch):
    from rpe_harness.integrations import hermes_review, hermes_session
    received = []
    def session(*args, **kwargs):
        received.append(kwargs)
        return {}
    monkeypatch.setattr(hermes_session, 'run_hermes_session', session)
    result = hermes_review.run_hermes_review('Original request.', tmp_path, tmp_path/'s.db',
        owner_root=tmp_path/'owned', host_settings=settings())
    assert received[0]['host_settings'] == settings()
    assert SECRET not in json.dumps(result)


def test_cli_reads_private_settings_and_denies_original_file_to_host(tmp_path, monkeypatch):
    from rpe_harness import proposal_cli
    from rpe_harness.integrations import hermes_review
    from test_proposal_cli import parser
    path = tmp_path/'private.json'
    path.write_text(json.dumps(settings()))
    denied = tmp_path/'other-private'
    received = []
    def review(raw, *args, **kwargs):
        received.append((raw, kwargs))
        return {}
    monkeypatch.setattr(hermes_review, 'run_hermes_review', review)
    result = proposal_cli.dispatch_review(parser().parse_args(['review', 'draft',
        '--workspace', str(tmp_path), '--db', str(tmp_path/'s.db'), '--text', '完整原文',
        '--host-settings', str(path), '--deny-read-root', str(denied)]))
    assert received[0][0] == '完整原文'
    assert received[0][1]['host_settings'] == settings()
    assert set(received[0][1]['deny_read_roots']) == {denied, path}
    assert SECRET not in json.dumps(result)
