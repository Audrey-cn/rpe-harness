import csv
import hashlib
import json

import pytest

from rpe_harness.evolution import EvolutionStore, cycle, record_verified_feedback
from rpe_harness.runtime import HarnessRuntime


def verified_run(root, *, event_id='learn-1', output='result.csv'):
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'orders.csv'
    if not source.exists():
        source.write_text('订单号,金额\n001,10\n002,20\n001,99\n', encoding='utf-8-sig')
    db = root / 'runs.sqlite'
    request = {'schema_version': 1, 'event_id': event_id,
               'state': {'task_type': 'data.processing', 'goal': '保留首条',
                         'input_format': 'csv', 'required_capability': 'tabular.deduplicate',
                         'constraints': ['preserve_original', 'preserve_order']},
               'execution': {'input_path': str(source), 'output_path': str(root / output),
                             'key_columns': ['订单号']}}
    result = HarnessRuntime(db_path=db).run(request)
    assert result['status'] == 'succeeded'
    return db, result


def test_feedback_requires_real_durable_verified_artifact(tmp_path):
    db, run = verified_run(tmp_path)
    item = record_verified_feedback(run['run_id'], db)
    assert item['verified'] is True
    assert item['run_id'] == run['run_id']
    assert item['output_sha256'] == hashlib.sha256((tmp_path / 'result.csv').read_bytes()).hexdigest()
    assert record_verified_feedback(run['run_id'], db) == item
    store = EvolutionStore(db, tmp_path)
    assert len(store.list_feedback()) == 1
    with store.connect() as connection:
        connection.execute('INSERT INTO reported_usage(skill_id,success,created_at) VALUES(?,?,?)',
                           ('invented', 1, 1))
    assert len(store.list_feedback()) == 1


def test_claimed_success_and_summary_do_not_become_verified_feedback(tmp_path):
    db, run = verified_run(tmp_path)
    store = EvolutionStore(db, tmp_path)
    with store.connect() as connection:
        connection.execute("UPDATE runs SET status='failed',verification_ok=0 WHERE run_id=?",
                           (run['run_id'],))
    assert record_verified_feedback(run['run_id'], db) is None
    assert store.list_feedback() == []
    report = cycle(db, tmp_path, auto_apply=False)
    assert report['verified_feedback_count'] == 0
    assert store.get_active('recipe', 'recipe') is None
    assert store.list_proposals()


def test_changed_published_artifact_is_not_learning_evidence(tmp_path):
    db, run = verified_run(tmp_path)
    (tmp_path / 'result.csv').write_text('untrusted summary')
    assert record_verified_feedback(run['run_id'], db) is None


def test_offline_cycle_dedupes_and_generates_all_component_proposals(tmp_path):
    db, run = verified_run(tmp_path)
    first = cycle(db, tmp_path, auto_apply=False)
    store = EvolutionStore(db)
    initial = store.list_proposals()
    second = cycle(db, tmp_path, auto_apply=False)
    assert first['verified_feedback_count'] == second['verified_feedback_count'] == 1
    assert len(store.list_proposals()) == len(initial)
    assert {'contract', 'discovery', 'recipe', 'executor', 'host', 'core', 'verifier'} <= {
        proposal['component'] for proposal in initial}
    assert any(run['run_id'] in proposal['evidence_run_ids'] for proposal in initial)


def test_dormant_experience_stays_indexed_and_quarantine_is_visible(tmp_path):
    db, _ = verified_run(tmp_path)
    cycle(db, tmp_path, auto_apply=False)
    store = EvolutionStore(db)
    experience = store.list_experiences()[0]
    store.set_experience_state(experience['experience_id'], 'dormant')
    assert store.lookup_experiences(experience['lookup_key'])[0]['state'] == 'dormant'
    store.set_experience_state(experience['experience_id'], 'quarantine')
    assert store.lookup_experiences(experience['lookup_key'])[0]['state'] == 'quarantine'


def test_default_cycle_validates_and_activates_verified_recipe(tmp_path):
    db, _ = verified_run(tmp_path)
    report = cycle(db, tmp_path)
    store = EvolutionStore(db)
    active = store.get_active('recipe', 'recipe')
    assert report['applied'] and active['kind'] == 'recipe'
    assert active['validation']['passed'] is True
    assert active['validation']['heldout_count'] >= 3
    assert store.rollback(active['version_id'])['rolled_back'] is True
    assert store.get_active('recipe', 'recipe') is None


def test_empty_cycle_still_persists_diagnostic_without_fabricating_success(tmp_path):
    db = tmp_path / 'runs.sqlite'
    HarnessRuntime(db_path=db)
    report = cycle(db, tmp_path)
    assert report['verified_feedback_count'] == 0
    assert report['proposal_count'] > 0
    assert EvolutionStore(db).get_active('recipe', 'recipe') is None


def test_cycle_proposer_is_separate_from_acceptance(tmp_path):
    db, run = verified_run(tmp_path)

    def propose(context):
        assert context['verified_feedback'][0]['run_id'] == run['run_id']
        return [{'component': 'verifier', 'kind': 'source_patch',
                 'content': 'def verify(*args): return True',
                 'evidence_run_ids': [run['run_id']]}]

    cycle(db, tmp_path, proposer=propose)
    store = EvolutionStore(db)
    proposals = [p for p in store.list_proposals() if p['kind'] == 'source_patch']
    assert len(proposals) == 1
    assert proposals[0]['state'] == 'needs_external_validation'
    assert store.get_active('verifier', 'source_patch') is None


def test_failure_diagnostic_binds_structured_problem_and_expected_change(tmp_path):
    db, run = verified_run(tmp_path)
    store = EvolutionStore(db, tmp_path)
    with store.connect() as connection:
        connection.execute("UPDATE runs SET status='failed',verification_ok=0,stage='verify',error_json=? WHERE run_id=?",
                           (json.dumps({'code': 'execution_failed', 'type': 'RuntimeError',
                                        'message': 'independent artifact verification failed'}), run['run_id']))
    cycle(db, tmp_path, auto_apply=False)
    proposals = [p for p in store.list_proposals() if p['component'] == 'verifier']
    body = json.loads(__import__('pathlib').Path(proposals[0]['artifact_path']).read_text())
    assert body['problems'][0]['run_id'] == run['run_id']
    assert body['problems'][0]['stage'] == 'verify'
    assert body['problems'][0]['code'] == 'execution_failed'
    assert body['expected_change']
    assert body['evidence_hashes'][run['run_id']]


def test_repeated_applied_cycle_does_not_reactivate_identical_recipe(tmp_path):
    db, _ = verified_run(tmp_path)
    cycle(db, tmp_path)
    store = EvolutionStore(db)
    before = store.pointer('recipe', 'recipe')
    report = cycle(db, tmp_path)
    assert report['applied'] == []
    assert store.pointer('recipe', 'recipe') == before


def test_cycle_quarantines_experience_when_supporting_artifact_is_changed(tmp_path):
    db, _ = verified_run(tmp_path)
    cycle(db, tmp_path)
    store = EvolutionStore(db)
    (tmp_path / 'result.csv').write_text('unverified replacement')
    report = cycle(db, tmp_path)
    assert report['verified_feedback_count'] == 0
    assert store.list_experiences()[0]['state'] == 'quarantine'
    assert store.get_active('recipe', 'recipe') is None


def test_host_return_error_code_without_durable_run_becomes_fact_diagnostic(tmp_path):
    from pathlib import Path
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.evolution.store import digest

    db = tmp_path / 'runs.sqlite'
    returned = HostEventStore(db).record('rpe_task_returned', {
        'session_id': 'host-failure-session', 'task_id': 'host-failure-task',
        'turn_id': 'host-failure-turn', 'api_request_id': 'host-failure-api',
        'tool_call_id': 'host-failure-call', 'receipt_id': 'persisted-failure-receipt',
        'run_id': None, 'status': 'failed', 'error_code': 'harness_launch_error'})
    report = cycle(db, tmp_path, auto_apply=False)
    store = EvolutionStore(db)
    assert report['verified_feedback_count'] == 0
    with store.connect() as connection:
        assert connection.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
        metadata = json.loads(connection.execute(
            'SELECT metadata_json FROM hermes_host_events WHERE ingest_event_id=?',
            (returned['ingest_event_id'],)).fetchone()[0])
    for proposal in store.list_proposals():
        body = json.loads(Path(proposal['artifact_path']).read_text())
        if proposal['component'] in {'host', 'core'}:
            assert body['host_errors'] == [{
                'host_event_id': returned['ingest_event_id'], 'kind': 'rpe_task_returned',
                'error_type': None, 'code': 'harness_launch_error',
                'metadata_sha256': digest(metadata)}]
            assert body['evidence_hashes']['host:' + returned['ingest_event_id']]
            assert body['problem'] == 'persisted_failures_require_regression'
        else:
            assert body['host_errors'] == []
    assert store.get_active('recipe', 'recipe') is None


def test_real_successful_host_return_without_error_code_is_not_host_error(tmp_path):
    from pathlib import Path
    from rpe_harness.integrations.hermes import HostEventStore

    db, run = verified_run(tmp_path)
    HostEventStore(db).record('rpe_task_returned', {
        'session_id': 'host-success-session', 'tool_call_id': 'host-success-call',
        'run_id': run['run_id'], 'status': run['status'], 'error_code': None})
    cycle(db, tmp_path, auto_apply=False)
    for proposal in EvolutionStore(db).list_proposals():
        body = json.loads(Path(proposal['artifact_path']).read_text())
        if proposal['kind'] == 'diagnostic':
            assert body['host_errors'] == []
