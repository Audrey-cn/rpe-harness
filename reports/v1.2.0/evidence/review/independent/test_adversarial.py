import json, sys
from pathlib import Path
import pytest
sys.path.insert(0,'/Users/audrey/项目/rpe-harness/tests')
from test_candidates import candidate, fresh_plan
from rpe_harness.evolution import EvolutionStore, validate_candidate, apply_candidate, prepare_candidate
from rpe_harness.evolution.store import digest
from rpe_harness.storage import canonical_json


def test_forged_incomplete_certificate_is_accepted(tmp_path):
    db,p = candidate(tmp_path)
    validate_candidate(p['proposal_id'],db)
    store = EvolutionStore(db)
    p = store.get_proposal(p['proposal_id'])
    report = p['validation']
    report['checks'] = []
    report['train_count'] = report['heldout_count'] = 0
    cert_hash = digest(report)
    version_id = digest({'proposal_id':p['proposal_id'],'certificate_hash':cert_hash})
    with store.connect() as connection:
        connection.execute('UPDATE evolution_proposals SET validation_json=?,validation_hash=?,version_id=? WHERE proposal_id=?',
            (canonical_json(report),cert_hash,version_id,p['proposal_id']))
        connection.execute('INSERT INTO evolution_versions VALUES(?,?,?,?,?)',(version_id,p['proposal_id'],'validated',None,0))
    applied = apply_candidate(p['proposal_id'],db)
    assert applied['applied'] is True
    print('accepted incomplete fixture certificate',version_id)


def test_corrupt_certificate_json_escapes_builtin_fallback(tmp_path):
    db,p = candidate(tmp_path)
    validate_candidate(p['proposal_id'],db)
    apply_candidate(p['proposal_id'],db)
    store = EvolutionStore(db)
    with store.connect() as connection:
        connection.execute('UPDATE evolution_proposals SET validation_json=? WHERE proposal_id=?',('bad JSON',p['proposal_id']))
    with pytest.raises(json.JSONDecodeError):
        prepare_candidate(fresh_plan(tmp_path),db)
    print('malformed certificate caused JSONDecodeError instead of builtin fallback')


def test_certificate_changed_after_recheck_still_switches(tmp_path, monkeypatch):
    import rpe_harness.evolution.candidates as module
    db,p = candidate(tmp_path)
    validate_candidate(p['proposal_id'],db)
    original = module._certificate
    seen = 0
    def mutate_after_validation(store, proposal, **kwargs):
        nonlocal seen
        result = original(store, proposal, **kwargs)
        seen += 1
        if seen == 2:
            with store.connect() as connection:
                connection.execute('UPDATE evolution_proposals SET validation_json=? WHERE proposal_id=?',('broken after recheck',p['proposal_id']))
        return result
    monkeypatch.setattr(module, '_certificate', mutate_after_validation)
    assert apply_candidate(p['proposal_id'],db)['applied'] is True
    with EvolutionStore(db).connect() as connection:
        assert connection.execute('SELECT version_id FROM evolution_active').fetchone()[0] is not None
    print('certificate corrupted after second recheck; activation still switches pointer')


def test_rollback_corrupt_previous_certificate_does_not_restore_builtin(tmp_path):
    from test_candidates import GOOD_CODE
    from rpe_harness.evolution import propose_candidate, rollback
    db,first = candidate(tmp_path)
    validate_candidate(first['proposal_id'],db)
    a = apply_candidate(first['proposal_id'],db)
    second = propose_candidate(db,tmp_path,component='executor',kind='csv_executor',content=GOOD_CODE+'\n# second\n',evidence_run_ids=first['evidence_run_ids'])
    validate_candidate(second['proposal_id'],db)
    b = apply_candidate(second['proposal_id'],db)
    artifact = Path(a['artifact_path']); artifact.chmod(0o600); artifact.write_text('corrupt prior code')
    with pytest.raises(ValueError,match='hash changed'):
        rollback(b['version_id'],db)
    assert EvolutionStore(db).pointer('executor','csv_executor')[0] == b['version_id']
    print('rollback refuses corrupt prior version and leaves current active rather than builtin')
