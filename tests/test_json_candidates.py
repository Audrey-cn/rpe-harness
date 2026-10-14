"""JSON candidates retain dedicated acceptance under the single lifecycle."""
import subprocess
import sys
import json
import os
import threading
from pathlib import Path

import pytest

from rpe_harness.evolution import (EvolutionStore, apply_candidate, prepare_candidate,
    propose_candidate, quarantine_candidate, rollback, validate_candidate)
from rpe_harness.evolution.candidates import current_revisions
from rpe_harness.runtime import HarnessRuntime

RECIPE = {'capability': 'json.normalize', 'input_format': 'json', 'style': 'pretty',
          'constraints': ['preserve_order', 'preserve_original'],
          'procedure': ['plan', 'prepare', 'independent_verify', 'exclusive_publish']}


def request(root, event, *, style='pretty', content=b'{ "n":[1,1.0,1e0,-0],"s":"\\u4e2d" }'):
    source, output = root / (event + '-source.json'), root / (event + '-output.json')
    source.write_bytes(content)
    return {'schema_version': 2, 'event_id': event, 'goal': 'Preserve JSON lexical tokens',
            'capability': 'json.normalize', 'constraints': ['preserve_original', 'preserve_order'],
            'execution': {'input_path': str(source), 'output_path': str(output), 'style': style}}


def seed(root):
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'state.sqlite'
    result = HarnessRuntime(db_path=db).run(request(root, 'seed'))
    assert result['status'] == 'succeeded'
    return db, result['run_id']


def code():
    return Path(__file__).resolve().parents[1].joinpath('examples/json-candidate/candidate.py').read_text()


def proposed(root, *, kind='json_recipe', content=None, db=None, run_id=None):
    if db is None:
        db, run_id = seed(root)
    content = content if content is not None else RECIPE if kind == 'json_recipe' else code()
    proposal = propose_candidate(db, root, component='recipe' if kind == 'json_recipe' else 'executor',
                                 kind=kind, content=content, evidence_run_ids=[run_id])
    return db, proposal


def activate(root, *, content=None, kind='json_executor', db=None, run_id=None):
    db, proposal = proposed(root, content=content, kind=kind, db=db, run_id=run_id)
    report = validate_candidate(proposal['proposal_id'], db)
    assert report['passed'] is True, report
    result = apply_candidate(proposal['proposal_id'], db)
    return db, proposal, result


def test_json_revisions_are_explicit_and_csv_default_remains():
    csv = current_revisions()
    assert current_revisions('csv_executor') == csv == current_revisions('recipe')
    revisions = current_revisions('json_executor')
    assert revisions == current_revisions('json_recipe')
    assert revisions['capability'] == 'json.normalize'
    assert revisions['fixture_suite_sha256'] != csv['fixture_suite_sha256']


def test_json_recipe_validation_activation_and_style_scope(tmp_path):
    db, proposal, active = activate(tmp_path, kind='json_recipe')
    report = EvolutionStore(db).get_version(active['version_id'])['validation']
    assert report['kind'] == 'json_recipe' and report['capability'] == 'json.normalize'
    pretty = HarnessRuntime(db_path=db).run(request(tmp_path, 'pretty'))
    assert pretty['status'] == 'succeeded'
    assert pretty['artifact']['actual_executor']['recipe_version_id'] == active['version_id']
    compact = HarnessRuntime(db_path=db).run(request(tmp_path, 'compact', style='minify'))
    assert compact['status'] == 'succeeded'
    assert 'recipe_version_id' not in compact['artifact']['actual_executor']
    assert EvolutionStore(db).pointer('recipe', 'json_recipe')[0] == active['version_id']
    assert (tmp_path / 'compact-output.json').read_bytes() == b'{"n":[1,1.0,1e0,-0],"s":"\\u4e2d"}\n'


@pytest.mark.parametrize('kind', ['json_executor', 'json_recipe', 'csv_executor', 'recipe'])
def test_evidence_must_match_registered_contract(tmp_path, kind):
    from test_evolution import verified_run
    from test_candidates import GOOD_CODE
    if kind.startswith('json_'):
        db, run = verified_run(tmp_path)
        run_id = run['run_id']
        content = RECIPE if kind == 'json_recipe' else 'def prepare(plan, path): pass\n'
    else:
        db, run_id = seed(tmp_path)
        content = GOOD_CODE if kind == 'csv_executor' else {
            'capability': 'tabular.deduplicate', 'input_format': 'csv', 'keep': 'first',
            'procedure': RECIPE['procedure']}
    proposal = propose_candidate(db, tmp_path, component='recipe' if kind in {'recipe', 'json_recipe'} else 'executor',
                                 kind=kind, content=content, evidence_run_ids=[run_id])
    report = validate_candidate(proposal['proposal_id'], db)
    assert report['passed'] is False
    assert 'evidence' in report.get('error', '').lower() and 'contract' in report.get('error', '').lower()
    assert EvolutionStore(db).pointer(proposal['component'], kind) == (None, 0)


def test_json_code_reuses_version_for_both_styles_in_separate_slot(tmp_path):
    db, proposal, active = activate(tmp_path)
    assert Path(proposal['artifact_path']).suffix == '.py'
    for style in ['pretty', 'minify']:
        result = HarnessRuntime(db_path=db).run(request(tmp_path, style, style=style))
        assert result['status'] == 'succeeded'
        actual = result['artifact']['actual_executor']
        assert actual['kind'] == 'candidate' and actual['candidate_version_id'] == active['version_id']
        assert actual['code_sha256'] == proposal['content_hash']
    assert EvolutionStore(db).pointer('executor', 'csv_executor') == (None, 0)
    assert rollback(active['version_id'], db)['restored_version_id'] is None


def test_csv_certificate_cannot_be_reused_for_json(tmp_path):
    from test_candidates import candidate
    from rpe_harness.evolution.candidates import _complete_certificate
    db, proposal = candidate(tmp_path)
    report = validate_candidate(proposal['proposal_id'], db)
    assert report['passed'] is True
    assert _complete_certificate(report) is True
    assert _complete_certificate(report, 'json_executor') is False


def test_fresh_evidence_is_rechecked_before_json_activation(tmp_path):
    db, proposal = proposed(tmp_path)
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    (tmp_path / 'seed-output.json').write_bytes(b'null\n')
    with pytest.raises(ValueError, match='evidence'):
        apply_candidate(proposal['proposal_id'], db)
    assert EvolutionStore(db).pointer('recipe', 'json_recipe') == (None, 0)


def test_json_stale_base_cas_cannot_replace_another_version(tmp_path):
    db, first = proposed(tmp_path)
    _, second = proposed(tmp_path, content={**RECIPE, 'style': 'minify'}, db=db, run_id=first['evidence_run_ids'][0])
    assert validate_candidate(first['proposal_id'], db)['passed'] is True
    assert validate_candidate(second['proposal_id'], db)['passed'] is True
    active = apply_candidate(first['proposal_id'], db)
    with pytest.raises(ValueError, match='base|compare|stale'):
        apply_candidate(second['proposal_id'], db)
    assert EvolutionStore(db).pointer('recipe', 'json_recipe')[0] == active['version_id']


def test_quarantine_restores_verified_previous_for_later_tasks(tmp_path):
    db, first, one = activate(tmp_path)
    _, _, two = activate(tmp_path, db=db, run_id=first['evidence_run_ids'][0], content=code() + '\n# second\n')
    path = Path(two['artifact_path'])
    path.chmod(0o600)
    path.write_text('corrupt')
    current = HarnessRuntime(db_path=db).run(request(tmp_path, 'corrupt-candidate'))
    assert current['status'] == 'succeeded'
    assert current['artifact']['actual_executor']['kind'] == 'builtin'
    assert EvolutionStore(db).pointer('executor', 'json_executor')[0] == one['version_id']
    following = HarnessRuntime(db_path=db).run(request(tmp_path, 'next-task'))
    assert following['artifact']['actual_executor']['candidate_version_id'] == one['version_id']
    assert EvolutionStore(db).get_version(two['version_id'])['state'] == 'quarantine'


@pytest.mark.parametrize('previous_fault', ['code', 'evidence', 'slot'])
def test_invalid_previous_restores_builtin_without_cross_slot(tmp_path, previous_fault):
    db, first, one = activate(tmp_path)
    _, _, two = activate(tmp_path, db=db, run_id=first['evidence_run_ids'][0], content=code() + '\n# second\n')
    store = EvolutionStore(db)
    if previous_fault == 'code':
        path = Path(one['artifact_path'])
        path.chmod(0o600)
        path.write_text('corrupt')
    elif previous_fault == 'evidence':
        (tmp_path / 'seed-output.json').write_bytes(b'false\n')
    else:
        with store.connect() as connection:
            connection.execute("UPDATE evolution_proposals SET kind='csv_executor' WHERE proposal_id=?", (first['proposal_id'],))
    result = quarantine_candidate(two['version_id'], db, 'production failure')
    assert result['restored_version_id'] is None
    assert store.pointer('executor', 'json_executor')[0] is None
    assert store.pointer('executor', 'csv_executor') == (None, 0)


def test_production_oracle_failure_uses_same_plan_builtin_and_restores_previous(tmp_path):
    db, first, one = activate(tmp_path)
    broken = code() + "\n_original_prepare = prepare\ndef prepare(plan, staging_path):\n    if b'production-only' in open(plan['input_path'], 'rb').read():\n        open(staging_path, 'wb').write(b'true\\n')\n    else:\n        _original_prepare(plan, staging_path)\n"
    _, _, two = activate(tmp_path, db=db, run_id=first['evidence_run_ids'][0], content=broken)
    result = HarnessRuntime(db_path=db).run(request(tmp_path, 'bad-output', content=b'{"production-only":1.0}'))
    assert result['status'] == 'succeeded'
    assert (tmp_path / 'bad-output-output.json').read_bytes() == b'{\n  "production-only": 1.0\n}\n'
    fallback = [step for step in result['steps'] if step['stage'] == 'candidate_fallback']
    assert len(fallback) == 1
    assert fallback[0]['evidence']['actual_executor']['candidate_version_id'] == two['version_id']
    assert fallback[0]['evidence']['plan_sha256'] == result['artifact']['plan_sha256']
    assert result['artifact']['actual_executor']['kind'] == 'builtin'
    assert EvolutionStore(db).pointer('executor', 'json_executor')[0] == one['version_id']


def test_quarantining_dormant_version_does_not_replace_current(tmp_path):
    db, first, one = activate(tmp_path)
    _, _, two = activate(tmp_path, db=db, run_id=first['evidence_run_ids'][0], content=code() + '\n# second\n')
    before = EvolutionStore(db).pointer('executor', 'json_executor')
    result = quarantine_candidate(one['version_id'], db, 'dormant failure')
    assert result['restored_version_id'] is None
    assert EvolutionStore(db).pointer('executor', 'json_executor') == before
    assert before[0] == two['version_id']


def test_manual_json_rollback_rechecks_previous_evidence(tmp_path):
    db, first, one = activate(tmp_path, kind='json_recipe')
    _, _, two = activate(tmp_path, kind='json_recipe', content={**RECIPE, 'style': 'minify'},
                          db=db, run_id=first['evidence_run_ids'][0])
    (tmp_path / 'seed-output.json').write_bytes(b'false\n')
    result = rollback(two['version_id'], db)
    assert result['restored_version_id'] is None
    assert 'invalid' in result['reason']
    assert EvolutionStore(db).pointer('recipe', 'json_recipe')[0] is None


@pytest.mark.parametrize('checkpoint, expected', [('intent_persisted', 'aborted'), ('activated', 'applied')])
def test_json_activation_process_crash_is_durably_recovered(tmp_path, checkpoint, expected):
    db, proposal = proposed(tmp_path)
    assert validate_candidate(proposal['proposal_id'], db)['passed'] is True
    program = "import os,sys\nfrom rpe_harness.evolution import apply_candidate\napply_candidate(sys.argv[1],sys.argv[2],_checkpoint=lambda stage:os._exit(73) if stage==sys.argv[3] else None)\n"
    child = subprocess.run([sys.executable, '-B', '-c', program, proposal['proposal_id'], str(db), checkpoint],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    store = EvolutionStore(db)
    with store.connect() as connection:
        row = connection.execute('SELECT state FROM evolution_activations').fetchone()
    assert row['state'] == expected
    assert (store.pointer('recipe', 'json_recipe')[0] is not None) is (expected == 'applied')


def test_unknown_json_kind_requires_external_acceptance(tmp_path):
    db, run_id = seed(tmp_path)
    proposal = propose_candidate(db, tmp_path, component='core', kind='new_json_core',
        content='opaque proposal', evidence_run_ids=[run_id])
    report = validate_candidate(proposal['proposal_id'], db)
    assert report['reason'] == 'requires_external_trusted_acceptance_suite'
    assert EvolutionStore(db).get_proposal(proposal['proposal_id'])['state'] == 'needs_external_validation'


def test_csv_candidate_preflight_applies_read_budget_before_allocation(tmp_path, monkeypatch):
    from test_candidates import candidate, fresh_plan, GOOD_CODE
    from rpe_harness.evolution.candidates import _prepare_code
    from rpe_harness import path_binding
    db, _ = candidate(tmp_path)
    plan = fresh_plan(tmp_path)
    original = path_binding.read_source
    def bounded(path, max_bytes=None):
        assert max_bytes == 16 * 1024 * 1024, 'CSV candidate source read did not enforce its budget'
        return original(path, max_bytes=max_bytes)
    monkeypatch.setattr(path_binding, 'read_source', bounded)
    prepared = _prepare_code(plan, GOOD_CODE.encode(), tmp_path)
    from rpe_harness.execution import cleanup_staging
    cleanup_staging(prepared)


def test_fifo_candidate_artifact_is_bounded_and_falls_back(tmp_path):
    db, proposal, active = activate(tmp_path, kind='json_recipe')
    artifact = Path(active['artifact_path'])
    artifact.unlink()
    os.mkfifo(artifact)
    payload = request(tmp_path, 'fifo')
    packet = tmp_path / 'request.json'
    packet.write_text(json.dumps(payload))
    program = "import json,sys\nfrom rpe_harness.runtime import HarnessRuntime\nprint(json.dumps(HarnessRuntime(db_path=sys.argv[1]).run(json.load(open(sys.argv[2])))))\n"
    try:
        child = subprocess.run([sys.executable, '-B', '-c', program, str(db), str(packet)],
                               capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        pytest.fail('FIFO candidate blocked the unattended runtime before sandbox admission')
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert result['status'] == 'succeeded' and result['artifact']['actual_executor']['kind'] == 'builtin'
    assert EvolutionStore(db).get_version(active['version_id'])['state'] == 'quarantine'


def test_grown_candidate_artifact_rejected_before_unbounded_allocation(tmp_path, monkeypatch):
    db, proposal, active = activate(tmp_path, kind='json_recipe')
    artifact = Path(active['artifact_path'])
    artifact.chmod(0o600)
    with artifact.open('wb') as stream:
        stream.truncate(65537)
    old_read = Path.read_bytes
    def no_unbounded(path):
        if path == artifact:
            raise AssertionError('candidate artifact was read before its bounded regular-file check')
        return old_read(path)
    monkeypatch.setattr(Path, 'read_bytes', no_unbounded)
    result = HarnessRuntime(db_path=db).run(request(tmp_path, 'grown'))
    assert result['status'] == 'succeeded'
    assert result['artifact']['actual_executor']['kind'] == 'builtin'
    assert EvolutionStore(db).get_version(active['version_id'])['state'] == 'quarantine'


def test_immutable_artifact_reuse_does_not_read_unbounded_file(tmp_path, monkeypatch):
    store = EvolutionStore(tmp_path / 'state.sqlite', tmp_path)
    raw = b'owned trust snapshot'
    filename, _ = store.write_artifact(raw, '.trust.json')
    artifact = Path(filename)
    old_read = Path.read_bytes
    def no_unbounded(path):
        if path == artifact:
            raise AssertionError('existing immutable artifact uses an unbounded read')
        return old_read(path)
    monkeypatch.setattr(Path, 'read_bytes', no_unbounded)
    assert store.write_artifact(raw, '.trust.json')[0] == filename


def test_manual_rollback_does_not_resurrect_concurrently_quarantined_previous(tmp_path, monkeypatch):
    from rpe_harness.evolution import candidates as module
    db, first, one = activate(tmp_path, kind='json_recipe')
    _, _, two = activate(tmp_path, kind='json_recipe', content={**RECIPE, 'style': 'minify'},
                         db=db, run_id=first['evidence_run_ids'][0])
    original, workers, errors = module._certificate, [], []
    def quarantine_previous():
        try:
            quarantine_candidate(one['version_id'], db, 'concurrent maintenance rejection')
        except BaseException as error:
            errors.append(error)
    def certificate_then_race(store, proposal, **options):
        content = original(store, proposal, **options)
        if proposal['version_id'] == one['version_id']:
            worker = threading.Thread(target=quarantine_previous)
            workers.append(worker)
            worker.start()
            worker.join(timeout=0.2)
        return content
    monkeypatch.setattr(module, '_certificate', certificate_then_race)
    try:
        rollback(two['version_id'], db)
    finally:
        for worker in workers:
            worker.join(timeout=12)
    assert workers and not any(worker.is_alive() for worker in workers) and not errors
    store = EvolutionStore(db)
    assert store.get_version(one['version_id'])['state'] == 'quarantine'
    assert store.pointer('recipe', 'json_recipe')[0] is None
