"""Maintenance batches retain evidence and resume durable work."""
import fcntl
import importlib
import os
import subprocess
import sys

import pytest

from rpe_harness.evolution import EvolutionStore, cycle
from rpe_harness.evolution.cycle import inspect_verified_feedback
from rpe_harness.runtime import HarnessRuntime
from test_json_cycle import json_run, snapshot


def drain(database, root, **budgets):
    reports = []
    for _ in range(250):
        report = cycle(database, root, **budgets)
        reports.append(report)
        if not report['maintenance']['has_more']:
            return reports
    pytest.fail('maintenance never drained')


def test_more_than_100_real_runs_are_processed_without_losing_first_anchor(tmp_path):
    database, first = json_run(tmp_path)
    cycle(database, tmp_path)
    pointer = EvolutionStore(database).pointer('recipe', 'json_recipe')
    for index in range(1, 105):
        json_run(tmp_path, str(index))
    reports = drain(database, tmp_path)
    assert all(len(report['maintenance']['inspected_run_ids']) <= 100 for report in reports)
    assert len(EvolutionStore(database).list_feedback()) == 105
    assert EvolutionStore(database).pointer('recipe', 'json_recipe') == pointer
    assert inspect_verified_feedback(first['run_id'], database)['verified'] is True
    assert not reports[-1]['maintenance']['has_more']


def test_new_same_semantic_evidence_does_not_create_more_diagnostic_proposals(tmp_path):
    database, first = json_run(tmp_path)
    cycle(database, tmp_path)
    before = [item['proposal_id'] for item in EvolutionStore(database).list_proposals()
              if item['kind'] == 'diagnostic']
    _, second = json_run(tmp_path, 'later')
    cycle(database, tmp_path)
    store = EvolutionStore(database)
    assert [item['proposal_id'] for item in store.list_proposals()
            if item['kind'] == 'diagnostic'] == before
    with store.connect() as db:
        references = {row[0] for row in db.execute(
            'SELECT source_id FROM evolution_maintenance_evidence WHERE source_kind=\'run\'')}
    assert {first['run_id'], second['run_id']} <= references


def test_cursor_does_not_lose_an_old_running_run_when_it_later_finishes(tmp_path):
    database = tmp_path / 'runs.sqlite'
    runtime = HarnessRuntime(db_path=database)
    old, _ = runtime.store.claim_event('waiting', 'old', {'id': 'old'})
    runtime.store.mark_running(old['run_id'])
    json_run(tmp_path, 'later')
    first = cycle(database, tmp_path, max_history_runs=1)
    assert first['maintenance']['waiting_run_count'] == 1
    runtime.store.finish(old['run_id'], 'failed', error={'code': 'old_completed'})
    reports = drain(database, tmp_path, max_history_runs=1)
    with EvolutionStore(database).connect() as db:
        references = {row[0] for row in db.execute(
            'SELECT source_id FROM evolution_maintenance_evidence')}
    assert old['run_id'] in references
    assert reports[-1]['maintenance']['waiting_run_count'] == 0


def test_default_proposals_over_one_cycle_budget_continue_from_queue(tmp_path):
    database, _ = json_run(tmp_path)
    reports = drain(database, tmp_path, max_proposals=1)
    assert all(report['proposal_count'] <= 1 for report in reports)
    assert EvolutionStore(database).get_active('recipe', 'json_recipe') is not None


def test_duplicate_external_proposals_are_deduped_before_budget(tmp_path):
    database, seed = json_run(tmp_path)
    candidate = {'component': 'core', 'kind': 'source_patch', 'content': 'untrusted patch',
                 'evidence_run_ids': [seed['run_id']]}
    report = cycle(database, tmp_path, proposer=lambda context: [candidate] * 100)
    assert len([item for item in report['proposal_origins']
                if item['origin'] == 'external_injected_code']) == 1


def test_crash_after_discovery_resumes_without_skipping_runs(tmp_path, monkeypatch):
    database, seed = json_run(tmp_path)
    module = importlib.import_module('rpe_harness.evolution.cycle')
    real_add = EvolutionStore.add_feedback
    def crash(self, feedback):
        raise RuntimeError('simulated death after discovery commit')
    monkeypatch.setattr(EvolutionStore, 'add_feedback', crash)
    with pytest.raises(RuntimeError, match='simulated death'):
        module.cycle(database, tmp_path)
    monkeypatch.setattr(EvolutionStore, 'add_feedback', real_add)
    drain(database, tmp_path)
    assert [item['run_id'] for item in EvolutionStore(database).list_feedback()] == [seed['run_id']]


def test_concurrent_maintenance_owner_returns_busy_without_mutating_database(tmp_path):
    database, _ = json_run(tmp_path)
    cycle(database, tmp_path)
    before = snapshot(database)
    with (tmp_path / '.evolution-maintenance.lease').open('a+b') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = cycle(database, tmp_path)
    assert report['maintenance']['busy'] is True
    assert snapshot(database) == before


def test_old_artifact_is_freshly_rechecked_after_new_batch(tmp_path):
    database, _ = json_run(tmp_path)
    cycle(database, tmp_path)
    json_run(tmp_path, 'new')
    (tmp_path / 'seed-output.json').write_bytes(b'bad')
    report = cycle(database, tmp_path, max_history_runs=1)
    assert report['quarantined']
    assert EvolutionStore(database).get_active('recipe', 'json_recipe') is None
    assert len(EvolutionStore(database).list_feedback()) == 2


def test_mixed_failure_semantics_do_not_depend_on_batch_composition(tmp_path):
    database = tmp_path / 'runs.sqlite'
    runtime = HarnessRuntime(db_path=database)
    for index, code in enumerate(['bad_contract', 'bad_producer']):
        run, _ = runtime.store.claim_event('failures', str(index), {'index': index})
        runtime.store.step(run['run_id'], 'verify', {'failed': True})
        runtime.store.finish(run['run_id'], 'failed', error={'code': code, 'type': 'ValueError'})
    cycle(database, tmp_path)
    before = len(EvolutionStore(database).list_proposals())
    run, _ = runtime.store.claim_event('failures', '2', {'index': 2})
    runtime.store.step(run['run_id'], 'verify', {'failed': True})
    runtime.store.finish(run['run_id'], 'failed', error={'code': 'bad_contract', 'type': 'ValueError'})
    cycle(database, tmp_path, max_history_runs=1)
    assert len(EvolutionStore(database).list_proposals()) == before


def test_validated_default_recipe_can_activate_on_later_explicit_cycle(tmp_path):
    database, _ = json_run(tmp_path)
    cycle(database, tmp_path, auto_apply=False)
    assert EvolutionStore(database).get_active('recipe', 'json_recipe') is None
    cycle(database, tmp_path)
    assert EvolutionStore(database).get_active('recipe', 'json_recipe') is not None
    assert len(EvolutionStore(database).list_versions()) == 1


def test_deferred_apply_backlog_is_reported_until_all_bounded_work_is_consumed(tmp_path):
    database, _ = json_run(tmp_path)
    json_run(tmp_path, 'second-style', 'pretty')
    drain(database, tmp_path, auto_apply=False, max_proposals=1)
    first = cycle(database, tmp_path, max_proposals=1)
    assert first['maintenance']['has_more'] is True
    remaining = drain(database, tmp_path, max_proposals=1)
    assert remaining[-1]['maintenance']['has_more'] is False
    with EvolutionStore(database).connect() as db:
        assert db.execute("SELECT count(*) FROM evolution_maintenance_queue "
            "WHERE json_extract(payload_json,'$.deferred_apply')=1").fetchone()[0] == 0


def test_host_discovery_budget_and_restart_keep_every_receipt(tmp_path):
    from rpe_harness.integrations.hermes import HostEventStore
    database = tmp_path / 'runs.sqlite'
    HarnessRuntime(db_path=database)
    host = HostEventStore(database)
    ids = []
    for index in range(105):
        receipt = host.record('tool_error', {'call': str(index), 'error_code': 'same_failure'})
        ids.append(receipt['ingest_event_id'])
    reports = drain(database, tmp_path)
    assert all(report['maintenance']['scanned_host_count'] <= 100 for report in reports)
    with EvolutionStore(database).connect() as db:
        refs = {row[0] for row in db.execute(
            "SELECT source_id FROM evolution_maintenance_evidence WHERE source_kind='host'")}
    assert refs == set(ids)
    assert len(EvolutionStore(database).list_proposals()) == 7


def test_sigkill_after_discovery_commit_releases_lease_and_replays_queue(tmp_path):
    database, seed = json_run(tmp_path)
    program = '''import os,signal,sys
from rpe_harness.evolution import cycle, EvolutionStore
def killed(self, feedback):
    os.kill(os.getpid(), signal.SIGKILL)
EvolutionStore.add_feedback=killed
cycle(sys.argv[1], sys.argv[2])
'''
    result = subprocess.run([sys.executable, '-B', '-c', program, str(database), str(tmp_path)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(tmp_path)),
        capture_output=True, text=True, timeout=20)
    assert result.returncode == -9, result.stderr
    reports = drain(database, tmp_path)
    assert not reports[-1]['maintenance']['busy']
    assert [item['run_id'] for item in EvolutionStore(database).list_feedback()] == [seed['run_id']]


@pytest.mark.parametrize('checkpoint', ['proposal', 'activation'])
def test_sigkill_after_candidate_mutation_does_not_duplicate_or_reactivate(tmp_path, checkpoint):
    database, _ = json_run(tmp_path)
    program = '''import os,signal,sys
from rpe_harness.evolution import cycle
from rpe_harness.evolution import candidates
name = 'propose_candidate' if sys.argv[3]=='proposal' else 'apply_candidate'
original = getattr(candidates,name)
def interrupted(*args, **kwargs):
    result = original(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
setattr(candidates,name,interrupted)
cycle(sys.argv[1],sys.argv[2])
'''
    result = subprocess.run([sys.executable, '-B', '-c', program, str(database), str(tmp_path), checkpoint],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(tmp_path)),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == -9, result.stderr
    before = EvolutionStore(database).pointer('recipe', 'json_recipe')
    if checkpoint == 'activation':
        resumed = cycle(database, tmp_path, max_proposals=1)
        assert resumed['proposal_count'] == 0
    drain(database, tmp_path)
    after = EvolutionStore(database).pointer('recipe', 'json_recipe')
    assert after[1] == 1
    if checkpoint == 'activation':
        assert after == before
    assert len(EvolutionStore(database).list_proposals()) == 8


def test_stale_active_revision_is_quarantined_with_its_old_evidence_retained(tmp_path, monkeypatch):
    from rpe_harness.evolution import candidates
    database, seed = json_run(tmp_path)
    cycle(database, tmp_path)
    store = EvolutionStore(database)
    old = store.get_active('recipe', 'json_recipe')
    revisions = candidates.current_revisions
    monkeypatch.setattr(candidates, 'current_revisions',
                        lambda kind='csv_executor': {**revisions(kind), 'new_revision': 'v2'})
    result = cycle(database, tmp_path)
    assert any(item.get('version_id') == old['version_id'] for item in result['quarantined'])
    assert store.get_version(old['version_id'])['state'] == 'quarantine'
    assert store.get_proposal(old['proposal_id'])['evidence_run_ids'] == [seed['run_id']]
    assert inspect_verified_feedback(seed['run_id'], database)['verified'] is True


def test_nonregular_maintenance_lease_is_rejected_without_db_mutation(tmp_path):
    database, _ = json_run(tmp_path)
    before = snapshot(database)
    os.mkfifo(tmp_path / '.evolution-maintenance.lease')
    with pytest.raises(ValueError, match='regular'):
        cycle(database, tmp_path)
    assert snapshot(database) == before
