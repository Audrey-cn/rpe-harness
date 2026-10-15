"""Value profiles bind the actual request and parent-owned durable proposal."""
import json
import sqlite3
import sys

import pytest

from test_hermes_review import RAW, ReviewSDK, dispatch, draft, host_result


@pytest.fixture
def profile_review(tmp_path, monkeypatch, request):
    from rpe_harness.capability_catalog import build_catalog
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.integrations import hermes_review
    from rpe_harness.integrations.hermes_broker import execution_broker
    profile = getattr(request, 'param', 'values-v1')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'orders.csv').write_text('id,value\na,1\na,2\nb,3\n')
    database = tmp_path / 'runs.sqlite'
    store, catalog = HostEventStore(database), build_catalog()
    configuration = {'catalog_sha256': catalog.snapshot_id, 'entries': catalog.entries(),
                     'raw': RAW, 'source_ref': 'current-request:test'}
    options = {'review_profile': profile} if profile is not None else {}
    configuration.update(options)
    config = tmp_path / 'review.json'
    config.write_text(json.dumps(configuration))
    monkeypatch.setenv('RPE_HERMES_REVIEW_CONFIG', str(config))
    monkeypatch.setenv('RPE_HERMES_ENTRY_KEY', 'review-entry')
    sdk = ReviewSDK()
    hermes_review.register_review_workflow(sdk, store=store, workspace=workspace,
        python=sys.executable, error_path=tmp_path / 'errors')
    extension = hermes_review.review_extension(database, workspace, raw=RAW,
        source_ref='current-request:test', **options)
    with execution_broker(db_path=database, workspace=workspace, python=sys.executable,
                          extension=extension, review_auto=True) as broker:
        for key, value in broker.items():
            monkeypatch.setenv(key, value)
        yield {'api': hermes_review, 'sdk': sdk, 'workspace': workspace, 'db': database,
               'store': store, 'config': config, 'profile': profile}
    assert broker.cleanup['cleanup_complete'] is True


@pytest.mark.parametrize('profile_review', [None, 'values-v1'], indirect=True)
def test_profile_request_and_ingress_digest_bind_durable_parameter_evidence(profile_review):
    from rpe_harness.integrations.hermes_gate import digest_json
    from rpe_harness.proposal import ProposalStore
    from rpe_harness.review_values import VALUE_EVIDENCE_CONTRACT
    env = profile_review
    result = dispatch(env, draft())
    assert result['status'] == 'awaiting_confirmation', result
    request = env['request']
    assert request['messages'][1] == {'role': 'user', 'content': RAW}
    assert (VALUE_EVIDENCE_CONTRACT in request['messages'][0]['content']) is (
        env['profile'] == 'values-v1')
    events = env['store'].list('review-session')
    ingress = next(event for event in events if event['kind'] == 'rpe_skill_api_checked')
    assert ingress['request_messages_sha256'] == digest_json(request['messages'])
    assert ingress['request_payload_sha256'] == digest_json(request)
    assert any(event['kind'] == 'rpe_skill_rpc_received' for event in events)
    saved = ProposalStore(env['db']).show(result['proposal_id'], session_id='review-session')
    assert saved['source']['raw'] == RAW and saved['draft'] == draft()
    assert ('parameter_evidence' in saved) is (env['profile'] == 'values-v1')
    if env['profile'] == 'values-v1':
        evidence = saved['parameter_evidence']
        assert evidence['status'] == 'ready'
        assert {row['field']: row['canonical_value'] for row in evidence['witnesses']} == {
            '/execution/encoding': 'utf-8', '/execution/delimiter': ',', '/execution/keep': 'first'}
        assert all(row['status'] == 'verified_literal' for row in evidence['witnesses'])
    verdict = env['api'].evaluate_review_session(host_result(env), env['db'], env['workspace'])
    assert verdict['proposal_verified'] is True, verdict
    assert not (env['workspace'] / 'out.csv').exists()


def test_parent_profile_blocks_literal_conflict_after_real_request_and_rpc(profile_review):
    from rpe_harness.proposal import ProposalStore
    from rpe_harness.review_values import VALUE_EVIDENCE_CONTRACT
    env = profile_review
    candidate = draft()
    candidate['execution']['encoding'] = 'utf-8-sig'
    result = dispatch(env, candidate)
    assert VALUE_EVIDENCE_CONTRACT in env['request']['messages'][0]['content']
    assert result['status'] == 'needs_context', result
    store = ProposalStore(env['db'])
    saved = store.show(result['proposal_id'], session_id='review-session')
    assert saved['draft']['execution']['encoding'] == 'utf-8-sig'
    witness = next(row for row in saved['parameter_evidence']['witnesses']
                   if row['field'] == '/execution/encoding')
    assert witness['status'] == 'conflict'
    assert witness['literal_values'] == ['utf-8']
    with pytest.raises(ValueError):
        store.confirm(result['proposal_id'], session_id='review-session',
                      expected_proposal_sha256=result['proposal_sha256'], actor='test-reviewer')
    with sqlite3.connect(env['db']) as database:
        assert database.execute('SELECT COUNT(*) FROM rpe_confirmations').fetchone()[0] == 0
        assert database.execute('SELECT COUNT(*) FROM rpe_proposal_executions').fetchone()[0] == 0
    assert not (env['workspace'] / 'out.csv').exists()
