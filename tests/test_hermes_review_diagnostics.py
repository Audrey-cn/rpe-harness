"""Content-free diagnostics retain failures without becoming execution evidence."""
import json
import os
from pathlib import Path

import pytest

from test_hermes_review import review

from rpe_harness.integrations.review_diagnostics import (
    MAX_JOURNAL, append_response, collect_diagnostics, persist_diagnostics, response_summary)


def row(**changes):
    native = {'session_id': 's', 'task_id': 't', 'turn_id': 'turn', 'api_request_id': 'api'}
    return {**response_summary(native, {'tool_calls': [], 'finish_reason': 'stop', 'provider_data': {}},
        phase='discover', expected_tool='rpe_discover', native_action='fallthrough',
        failure_code='review_protocol_recovery_not_allowed'), **changes}


def observed():
    return {'completed': True, 'process_cleanup': {'cleanup_complete': True},
        'events': [{'kind': 'post_api_request', 'session_id': 's', 'task_id': 't',
                    'turn_id': 'turn', 'api_request_id': 'api'}]}


def test_private_journal_permissions_and_missing_observations_are_not_complete(tmp_path, monkeypatch):
    journal = tmp_path / 'journal'
    journal.touch(mode=0o644)
    monkeypatch.setenv('RPE_HERMES_REVIEW_DIAGNOSTICS', str(journal))
    append_response(row())
    assert journal.stat().st_mode & 0o777 == 0o600
    result = collect_diagnostics(journal, observed())
    assert result['coverage']['complete'] is True
    no_events = {**observed(), 'events': []}
    assert collect_diagnostics(journal, no_events)['coverage']['complete'] is False


@pytest.mark.parametrize('fault', ['missing', 'empty', 'symlink', 'fifo', 'json', 'oversize', 'rows', 'secret', 'injection'])
def test_unsafe_or_incomplete_diagnostics_remain_bounded_and_content_free(tmp_path, fault):
    journal = tmp_path / 'journal'
    if fault == 'empty':
        journal.write_text('')
    elif fault == 'symlink':
        target = tmp_path / 'target'
        target.write_text('PRIVATE_SECRET')
        journal.symlink_to(target)
    elif fault == 'fifo':
        os.mkfifo(journal)
    elif fault == 'json':
        journal.write_text('{PRIVATE_SECRET')
    elif fault == 'oversize':
        journal.write_bytes(b'x' * (MAX_JOURNAL + 1))
    elif fault == 'rows':
        journal.write_text((json.dumps(row()) + '\n') * 33)
    elif fault == 'secret':
        journal.write_text(json.dumps(row(session_id='PRIVATE_SECRET')) + '\n')
    elif fault == 'injection':
        journal.write_text(json.dumps(row(content='PRIVATE_SECRET', arguments='PRIVATE_SECRET',
            native_action='PRIVATE_SECRET', tool_call_count=-1, phase='PRIVATE_SECRET')) + '\n')
    facts = {**observed(), 'events': []} if fault == 'empty' else observed()
    result = collect_diagnostics(journal, facts, secret='PRIVATE_SECRET')
    assert result['diagnostic_only'] is True
    assert result['coverage']['complete'] is False
    assert len(result['responses']) <= 32
    assert len(json.dumps(result).encode()) <= 65536
    assert 'PRIVATE_SECRET' not in json.dumps(result)
    artifact = persist_diagnostics(tmp_path/'runs.sqlite', result)
    path = Path(artifact['path'])
    import hashlib
    assert artifact['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == result


def test_private_journal_never_grows_past_hard_limit(tmp_path, monkeypatch):
    journal = tmp_path/'journal'
    monkeypatch.setenv('RPE_HERMES_REVIEW_DIAGNOSTICS', str(journal))
    for _ in range(1200):
        append_response(row())
    assert journal.stat().st_size <= MAX_JOURNAL
    result = collect_diagnostics(journal, observed())
    assert result['coverage']['complete'] is False
    assert 'diagnostic_limit_exceeded' in result['coverage']['reasons']


@pytest.mark.parametrize('refusal', ['', None, 'PRIVATE_SECRET'])
def test_refusal_presence_uses_native_field_presence_not_truthiness(refusal):
    result = response_summary({}, {'provider_data': {'refusal': refusal}, 'tool_calls': []},
        native_action='return', failure_code='review_protocol_native_response_not_dispatchable')
    assert result['refusal_field_present'] is True
    assert result['native_action'] == 'return'
    assert 'PRIVATE_SECRET' not in json.dumps(result)


def test_content_filter_before_native_intake_keeps_unknown_verdict(tmp_path):
    result = collect_diagnostics(tmp_path/'missing', {**observed(), 'completed': False,
        'events': [{'kind': 'api_request_error', 'error_type': 'ContentPolicyBlocked',
                    'session_id': 's', 'task_id': 't', 'turn_id': 'turn', 'api_request_id': 'api',
                    'error': 'PRIVATE_SECRET'}]})
    response = result['responses'][0]
    assert response['native_action'] is None and response['tool_call_count'] is None
    assert response['failure_code'] == 'api_content_filter'
    assert result['failures'][0]['domain'] == 'model'
    assert 'PRIVATE_SECRET' not in json.dumps(result)


def test_auto_admission_is_explicit_and_nonreview_policy_stays_strict(review):
    from test_hermes_review_recovery import invalid_response
    from rpe_harness.integrations.hermes_skills import _actual_policy, _raw_policy
    from rpe_harness.integrations.hermes_broker import _validated_skill_request
    from rpe_harness.integrations.hermes_gate import digest_json
    invalid_response(review, calls=[{'id': 'call-1', 'name': 'rpe_propose', 'arguments': '{}'}])
    native = {'session_id': 'review-session', 'task_id': 'review-task', 'turn_id': 'review-turn',
        'api_request_id': 'api-1', 'tool_call_id': 'call-1', 'tool_name': 'rpe_propose',
        'entry_key': 'review-entry', 'arguments_sha256': digest_json({})}
    with pytest.raises(ValueError):
        _actual_policy(review['store'], native, 'rpe_propose', require_pre=False)
    _actual_policy(review['store'], native, 'rpe_propose', require_pre=False, review_auto=True)
    state = review['api'].ReviewWorkflowStore(review['db']).get(native, create=False)
    with pytest.raises(ValueError):
        _raw_policy(review['store'], native, state)
    _raw_policy(review['store'], native, state, review_auto=True)
    receipt = review['store'].record('rpe_skill_rpc_received', native)
    body = {'operation': 'propose', 'native': native, 'arguments': {}, 'receipt_id': receipt['ingest_event_id']}
    with pytest.raises(PermissionError):
        _validated_skill_request(body, review['db'])
    assert _validated_skill_request(body, review['db'], review_auto=True) == native
    import sqlite3
    with sqlite3.connect(review['db']) as db:
        key, raw = db.execute("SELECT id, metadata_json FROM hermes_host_events WHERE kind='pre_api_request'").fetchone()
        event = json.loads(raw)
        event['request_parallel_tool_calls'] = False
        db.execute('UPDATE hermes_host_events SET metadata_json=? WHERE id=?', (json.dumps(event), key))
    with pytest.raises(ValueError):
        _actual_policy(review['store'], native, 'rpe_propose', require_pre=False, review_auto=True)
    with pytest.raises(PermissionError):
        _validated_skill_request(body, review['db'], review_auto=True)


@pytest.mark.parametrize('stage', ['normalization', 'serialization'])
def test_unexpected_intake_failure_is_fail_closed_without_error_text(tmp_path, monkeypatch, stage):
    import sys
    from types import ModuleType, SimpleNamespace
    from rpe_harness.integrations.hermes_review import ReviewIngressBlocked
    from rpe_harness.integrations.hermes_review_finish import install_review_response_adapter
    module = ModuleType('agent.conversation_loop')
    def normalize(agent, *, response, api_request_id, effective_task_id):
        if stage == 'normalization':
            raise ValueError('PRIVATE_SECRET')
        return SimpleNamespace(action='fallthrough', assistant_message=object())
    module.normalize_model_response = normalize
    monkeypatch.setitem(sys.modules, 'agent.conversation_loop', module)
    monkeypatch.setenv('RPE_HERMES_ENTRY_MODE', 'review')
    journal = tmp_path/'journal'
    monkeypatch.setenv('RPE_HERMES_REVIEW_DIAGNOSTICS', str(journal))
    install_review_response_adapter()
    with pytest.raises(ReviewIngressBlocked):
        module.normalize_model_response(SimpleNamespace(session_id='s', _current_turn_id='t'),
            response={}, api_request_id='api', effective_task_id='task')
    saved = json.loads(journal.read_text())
    assert saved['stage'] == stage and saved['rpe_action'] == 'blocked'
    assert 'PRIVATE_SECRET' not in journal.read_text()
