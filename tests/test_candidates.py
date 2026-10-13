import csv
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from rpe_harness.evolution import (
    EvolutionStore, apply_candidate, prepare_candidate, propose_candidate,
    quarantine_candidate, rollback, validate_candidate,
)
from rpe_harness.execution import cleanup_staging, publish_output
from rpe_harness.verification import verify_deduplication

from test_evolution import verified_run


GOOD_CODE = '''import csv
def prepare(plan, staging_path):
    with open(plan['input_path'], encoding=plan['encoding'], newline='') as source:
        rows = list(csv.reader(source, delimiter=plan['delimiter'], strict=True))
    columns = [rows[0].index(key) for key in plan['key_columns']]
    seen, kept = set(), []
    for row in rows[1:]:
        key = tuple(row[index] for index in columns)
        if key not in seen:
            seen.add(key)
            kept.append(row)
    with open(staging_path, 'w', encoding=plan['encoding'], newline='') as result:
        csv.writer(result, delimiter=plan['delimiter']).writerows([rows[0], *kept])
'''


def candidate(root, content=GOOD_CODE):
    db, run = verified_run(root)
    from rpe_harness.evolution import record_verified_feedback
    record_verified_feedback(run['run_id'], db)
    proposal = propose_candidate(db, root, component='executor', kind='csv_executor',
                                 content=content, evidence_run_ids=[run['run_id']])
    return db, proposal


def fresh_plan(root, output='next.csv'):
    from rpe_harness.contracts import ExecutionPlan
    return ExecutionPlan.from_request({'schema_version': 2, 'event_id': output,
        'capability': 'tabular.deduplicate', 'goal': '按订单号保留首条',
        'constraints': ['preserve_original', 'preserve_order'],
        'execution': {'input_path': str(root / 'orders.csv'),
                      'output_path': str(root / output), 'key_columns': ['订单号']}})


def test_real_code_validation_application_use_and_rollback(tmp_path):
    db, proposal = candidate(tmp_path)
    validation = validate_candidate(proposal['proposal_id'], db)
    assert validation['passed'] is True
    assert validation['heldout_count'] >= 3
    active = apply_candidate(proposal['proposal_id'], db)
    plan = fresh_plan(tmp_path)
    prepared = prepare_candidate(plan, db)
    assert prepared['candidate_version_id'] == active['version_id']
    assert verify_deduplication(prepared, plan)['passed'] is True
    publish_output(prepared, plan)
    cleanup_staging(prepared)
    with (tmp_path / 'next.csv').open(encoding='utf-8-sig', newline='') as handle:
        assert list(csv.reader(handle)) == [['订单号', '金额'], ['001', '10'], ['002', '20']]
    assert rollback(active['version_id'], db)['rolled_back'] is True
    assert prepare_candidate(fresh_plan(tmp_path, 'third.csv'), db) is None


@pytest.mark.parametrize('content', [
    "def prepare(plan, staging_path):\n    open(staging_path,'w').write('wrong')\n",
    GOOD_CODE.replace('if key not in seen:', 'if key in seen:'),
    GOOD_CODE.replace('key = tuple(row[index] for index in columns)',
                      'key = tuple(str(int(row[index])) for index in columns)'),
])
def test_bad_code_is_quarantined_and_cannot_activate(tmp_path, content):
    db, proposal = candidate(tmp_path, content)
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is False
    with pytest.raises(ValueError):
        apply_candidate(proposal['proposal_id'], db)
    assert EvolutionStore(db).get_active('executor', 'csv_executor') is None
    assert EvolutionStore(db).get_proposal(proposal['proposal_id'])['state'] == 'quarantine'


def test_candidate_and_certificate_tampering_leave_pointer_unchanged(tmp_path):
    db, proposal = candidate(tmp_path)
    validate_candidate(proposal['proposal_id'], db)
    path = Path(proposal['artifact_path'])
    path.chmod(0o600)
    path.write_text(GOOD_CODE + '\n# tampered\n')
    with pytest.raises(ValueError):
        apply_candidate(proposal['proposal_id'], db)
    assert EvolutionStore(db).get_active('executor', 'csv_executor') is None


def test_changed_certificate_is_not_a_validation_gate(tmp_path):
    db, proposal = candidate(tmp_path)
    validate_candidate(proposal['proposal_id'], db)
    store = EvolutionStore(db)
    with store.connect() as connection:
        connection.execute('UPDATE evolution_proposals SET validation_json=? WHERE proposal_id=?',
                           (json.dumps({'passed': True}), proposal['proposal_id']))
    with pytest.raises(ValueError):
        apply_candidate(proposal['proposal_id'], db)
    assert store.get_active('executor', 'csv_executor') is None


def test_stale_base_cas_cannot_replace_new_activation(tmp_path):
    db, first = candidate(tmp_path)
    run_id = first['evidence_run_ids'][0]
    second = propose_candidate(db, tmp_path, component='executor', kind='csv_executor',
        content=GOOD_CODE + '\n# variant two\n', evidence_run_ids=[run_id])
    validate_candidate(first['proposal_id'], db)
    validate_candidate(second['proposal_id'], db)
    active = apply_candidate(first['proposal_id'], db)
    with pytest.raises(ValueError, match='base|compare|stale'):
        apply_candidate(second['proposal_id'], db)
    assert EvolutionStore(db).get_active('executor', 'csv_executor')['version_id'] == active['version_id']


def test_activation_crash_is_recovered_without_switch(tmp_path):
    db, proposal = candidate(tmp_path)
    validate_candidate(proposal['proposal_id'], db)
    program = '''import os,sys
from rpe_harness.evolution import apply_candidate
apply_candidate(sys.argv[1],sys.argv[2],_checkpoint=lambda stage:os._exit(73))
'''
    child = subprocess.run([sys.executable, '-B', '-c', program, proposal['proposal_id'], str(db)],
                           capture_output=True, text=True)
    assert child.returncode == 73
    store = EvolutionStore(db)
    assert store.get_active('executor', 'csv_executor') is None
    with store.connect() as connection:
        assert connection.execute('SELECT state FROM evolution_activations').fetchone()[0] == 'aborted'


def test_sandbox_denies_source_writes_network_and_other_files(tmp_path):
    sentinel = tmp_path / 'private.txt'
    sentinel.write_text('preserve')
    attack = f"def prepare(plan, staging_path):\n    open({str(sentinel)!r},'w').write('changed')\n"
    db, proposal = candidate(tmp_path, attack)
    report = validate_candidate(proposal['proposal_id'], db)
    assert report['passed'] is False
    assert sentinel.read_text() == 'preserve'
    network = "import socket\ndef prepare(plan, staging_path):\n    socket.create_connection(('127.0.0.1',1))\n"
    proposal = propose_candidate(db, tmp_path, component='executor', kind='csv_executor',
        content=network, evidence_run_ids=proposal['evidence_run_ids'])
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is False


def test_timeout_cleans_owned_process_group(tmp_path):
    db, proposal = candidate(tmp_path, 'import time\ndef prepare(plan, staging_path):\n    time.sleep(60)\n')
    report = validate_candidate(proposal['proposal_id'], db, timeout=0.3)
    assert report['passed'] is False
    assert any('timeout' in str(item).lower() for item in report['checks'])


def test_production_corruption_quarantines_and_returns_core_fallback(tmp_path):
    db, proposal = candidate(tmp_path)
    validate_candidate(proposal['proposal_id'], db)
    active = apply_candidate(proposal['proposal_id'], db)
    path = Path(active['artifact_path'])
    path.chmod(0o600)
    path.write_text('untrusted')
    assert prepare_candidate(fresh_plan(tmp_path), db) is None
    assert EvolutionStore(db).get_active('executor', 'csv_executor') is None
    assert EvolutionStore(db).get_version(active['version_id'])['state'] == 'quarantine'


def test_no_output_overwrite_and_same_plan_source_binding(tmp_path):
    db, proposal = candidate(tmp_path)
    validate_candidate(proposal['proposal_id'], db)
    apply_candidate(proposal['proposal_id'], db)
    plan = fresh_plan(tmp_path)
    (tmp_path / 'next.csv').write_text('existing')
    with pytest.raises(ValueError):
        prepare_candidate(plan, db)
    assert (tmp_path / 'next.csv').read_text() == 'existing'


def test_summary_evidence_cannot_activate_code(tmp_path):
    db, proposal = candidate(tmp_path)
    proposal = propose_candidate(db, tmp_path, component='executor', kind='csv_executor',
        content=GOOD_CODE + '\n# unsupported evidence\n', evidence_run_ids=['model-summary'])
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is False
    with pytest.raises(ValueError):
        apply_candidate(proposal['proposal_id'], db)


def test_corrupt_active_certificate_json_quarantines_and_falls_back(tmp_path):
    db, proposal = candidate(tmp_path)
    validate_candidate(proposal['proposal_id'], db)
    active = apply_candidate(proposal['proposal_id'], db)
    store = EvolutionStore(db)
    with store.connect() as connection:
        connection.execute('UPDATE evolution_proposals SET validation_json=? WHERE proposal_id=?',
                           ('broken{', proposal['proposal_id']))
    assert prepare_candidate(fresh_plan(tmp_path), db) is None
    assert store.pointer('executor', 'csv_executor')[0] is None
    with store.connect() as connection:
        assert connection.execute('SELECT state FROM evolution_versions WHERE version_id=?',
                                  (active['version_id'],)).fetchone()[0] == 'quarantine'


def test_self_consistent_empty_certificate_cannot_activate(tmp_path):
    from rpe_harness.evolution.store import digest
    from rpe_harness.storage import canonical_json
    db, proposal = candidate(tmp_path)
    validate_candidate(proposal['proposal_id'], db)
    store = EvolutionStore(db)
    report = store.get_proposal(proposal['proposal_id'])['validation']
    report.update(checks=[], train_count=0, heldout_count=0)
    certificate_hash = digest(report)
    version_id = digest({'proposal_id': proposal['proposal_id'], 'certificate_hash': certificate_hash})
    with store.connect() as connection:
        connection.execute('UPDATE evolution_proposals SET validation_json=?,validation_hash=?,version_id=? WHERE proposal_id=?',
                           (canonical_json(report), certificate_hash, version_id, proposal['proposal_id']))
        connection.execute('INSERT INTO evolution_versions VALUES(?,?,?,?,?)',
                           (version_id, proposal['proposal_id'], 'validated', None, 1))
    with pytest.raises(ValueError, match='certificate|fixture'):
        apply_candidate(proposal['proposal_id'], db)
    assert store.get_active('executor', 'csv_executor') is None


def test_installed_prefix_cannot_expose_oracle_to_candidate(tmp_path, monkeypatch):
    from rpe_harness.evolution import sandbox
    import tempfile
    with tempfile.TemporaryDirectory(prefix='rpe-installed-probe-', dir=sys.prefix) as directory:
        package = Path(directory) / 'rpe_harness'
        (package / 'evolution').mkdir(parents=True)
        oracle = package / 'verification.py'
        oracle.write_text('private independently authored oracle')
        monkeypatch.setattr(sandbox, '__file__', str(package / 'evolution' / 'sandbox.py'))
        attack = GOOD_CODE.replace('    with open(plan', f'    open({str(oracle)!r}).read()\n    with open(plan', 1)
        db, proposal = candidate(tmp_path, attack)
        report = validate_candidate(proposal['proposal_id'], db)
        assert report['passed'] is False
        assert any('PermissionError' in str(check) for check in report['checks'])


def test_rss_soft_monitor_kills_safe_two_mebibyte_overage(tmp_path):
    from rpe_harness.evolution.sandbox import run_candidate
    db, _ = verified_run(tmp_path)
    plan = fresh_plan(tmp_path)
    baseline_code = b'''import resource,time
def prepare(plan, staging_path):
    open(staging_path,'w').write(str(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
    time.sleep(.1)
'''
    baseline = int(run_candidate(baseline_code, plan.to_dict(), tmp_path))
    overage = b'''import time
def prepare(plan, staging_path):
    extra=bytearray(2*1024*1024)
    time.sleep(.3)
    open(staging_path,'w').write('completed')
'''
    with pytest.raises(RuntimeError, match='RSS soft memory'):
        run_candidate(overage, plan.to_dict(), tmp_path,
                      memory_limit_bytes=baseline + 1024*1024)


def test_validation_certificate_discloses_soft_memory_boundary(tmp_path):
    db, proposal = candidate(tmp_path)
    report = validate_candidate(proposal['proposal_id'], db)
    assert report['resource_limits']['memory_enforcement'] == 'rss_monitor_soft'
    assert 'instantaneous' in report['resource_limits']['memory_limitation']


def test_rollback_invalid_previous_version_restores_builtin(tmp_path):
    db, first = candidate(tmp_path)
    validate_candidate(first['proposal_id'], db)
    a = apply_candidate(first['proposal_id'], db)
    second = propose_candidate(db, tmp_path, component='executor', kind='csv_executor',
                               content=GOOD_CODE + '\n# second\n', evidence_run_ids=first['evidence_run_ids'])
    validate_candidate(second['proposal_id'], db)
    b = apply_candidate(second['proposal_id'], db)
    path = Path(a['artifact_path'])
    path.chmod(0o600)
    path.write_text('corrupt previous code')
    report = rollback(b['version_id'], db)
    assert report['restored_version_id'] is None
    assert 'invalid' in report['reason']
    store = EvolutionStore(db)
    assert store.get_active() is None
    assert store.get_version(a['version_id'])['state'] == 'quarantine'


def test_certificate_mutation_at_intent_cannot_switch_pointer(tmp_path):
    db, proposal = candidate(tmp_path)
    validate_candidate(proposal['proposal_id'], db)
    store = EvolutionStore(db)

    def mutation(stage):
        if stage == 'intent_persisted':
            with store.connect() as connection:
                connection.execute('UPDATE evolution_proposals SET validation_json=? WHERE proposal_id=?',
                                   ('broken after durable intent', proposal['proposal_id']))

    with pytest.raises(ValueError):
        apply_candidate(proposal['proposal_id'], db, _checkpoint=mutation)
    assert store.pointer('executor', 'csv_executor')[0] is None
    with store.connect() as connection:
        assert connection.execute('SELECT state FROM evolution_activations').fetchone()[0] == 'aborted'


def test_activation_crash_after_commit_keeps_atomic_active_version(tmp_path):
    db, proposal = candidate(tmp_path)
    validate_candidate(proposal['proposal_id'], db)
    program = '''import os,sys
from rpe_harness.evolution import apply_candidate
def crash(stage):
    if stage=='activated': os._exit(73)
apply_candidate(sys.argv[1],sys.argv[2],_checkpoint=crash)
'''
    child = subprocess.run([sys.executable, '-B', '-c', program, proposal['proposal_id'], str(db)],
                           capture_output=True, text=True)
    assert child.returncode == 73
    store = EvolutionStore(db)
    active = store.get_active()
    assert active['state'] == 'active'
    with store.connect() as connection:
        assert connection.execute('SELECT state FROM evolution_activations').fetchone()[0] == 'applied'
    assert rollback(active['version_id'], db)['rolled_back'] is True


def test_production_only_bad_candidate_is_quarantined_and_core_replays_same_plan(tmp_path):
    from rpe_harness.runtime import HarnessRuntime
    code = GOOD_CODE.replace("    with open(staging_path", "    if plan['output_path'].endswith('_production_bad.csv'):\n        kept=kept[:1]\n    with open(staging_path", 1)
    db, proposal = candidate(tmp_path, code)
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    activation = apply_candidate(proposal['proposal_id'], db)
    request = {'schema_version': 2, 'event_id': 'real-fallback', 'goal': '保留首条',
               'capability': 'tabular.deduplicate', 'constraints': ['preserve_original', 'preserve_order'],
               'execution': {'input_path': str(tmp_path / 'orders.csv'),
                             'output_path': str(tmp_path / '_production_bad.csv'), 'key_columns': ['订单号']}}
    result = HarnessRuntime(db_path=db).run(request)
    assert result['status'] == 'succeeded'
    fallback = next(step['evidence'] for step in result['steps'] if step['stage'] == 'candidate_fallback')
    assert fallback['plan_sha256'] == result['artifact']['plan_sha256']
    assert result['artifact']['actual_executor']['kind'] == 'builtin'
    assert EvolutionStore(db).get_version(activation['version_id'])['state'] == 'quarantine'


def test_explicit_empty_key_rejection_activates_despite_random_diagnostic_paths(tmp_path):
    code = GOOD_CODE.replace('        if key not in seen:',
                            "        if any(not item.strip() for item in key):\n"
                            "            raise ValueError('empty key values are not supported')\n"
                            '        if key not in seen:')
    db, proposal = candidate(tmp_path, code)
    validation = validate_candidate(proposal['proposal_id'], db)
    assert validation['passed'] is True
    rejected = next(check for check in validation['checks'] if check['fixture'] == 'empty_key')
    assert rejected['passed'] is True
    assert 'evolution-sandboxes' in rejected['error']
    assert apply_candidate(proposal['proposal_id'], db)['applied'] is True
