import json, sqlite3, subprocess, sys
from pathlib import Path
import pytest
sys.path.insert(0,'/Users/audrey/项目/rpe-harness/tests')
from test_candidates import candidate, fresh_plan, GOOD_CODE
from rpe_harness.evolution import EvolutionStore, validate_candidate, apply_candidate, prepare_candidate, propose_candidate, rollback
from rpe_harness.evolution.store import digest
from rpe_harness.storage import canonical_json


def test_incomplete_certificate_rejected_without_switch(tmp_path):
    db,p = candidate(tmp_path)
    validate_candidate(p['proposal_id'],db)
    store = EvolutionStore(db)
    p = store.get_proposal(p['proposal_id'])
    report = p['validation']
    report.update(checks=[], train_count=0, heldout_count=0)
    h = digest(report)
    v = digest({'proposal_id':p['proposal_id'],'certificate_hash':h})
    with store.connect() as connection:
        connection.execute('UPDATE evolution_proposals SET validation_json=?,validation_hash=?,version_id=? WHERE proposal_id=?',(canonical_json(report),h,v,p['proposal_id']))
        connection.execute('INSERT INTO evolution_versions VALUES(?,?,?,?,?)',(v,p['proposal_id'],'validated',None,0))
    with pytest.raises(ValueError,match='certificate|fixture'):
        apply_candidate(p['proposal_id'],db)
    assert store.pointer('executor','csv_executor')[0] is None


def test_corrupt_json_quarantines_and_actual_builtin_run_succeeds(tmp_path):
    from rpe_harness.runtime import HarnessRuntime
    db,p = candidate(tmp_path)
    validate_candidate(p['proposal_id'],db)
    a=apply_candidate(p['proposal_id'],db)
    store=EvolutionStore(db)
    with store.connect() as connection:
        connection.execute('UPDATE evolution_proposals SET validation_json=? WHERE proposal_id=?',('bad JSON',p['proposal_id']))
    assert prepare_candidate(fresh_plan(tmp_path),db) is None
    with store.connect() as connection:
        assert connection.execute('SELECT state FROM evolution_versions WHERE version_id=?',(a['version_id'],)).fetchone()[0]=='quarantine'
    plan=fresh_plan(tmp_path)
    r=HarnessRuntime(db_path=db).run({'schema_version':2,'event_id':'bad-certificate-fallback','capability':plan.capability,'goal':plan.goal,'constraints':list(plan.constraints),'execution':{'input_path':plan.input_path,'output_path':plan.output_path,'key_columns':list(plan.key_columns)}})
    assert r['status']=='succeeded'
    assert r['artifact']['actual_executor']['kind']=='builtin'


def test_mutation_at_intent_rejected_and_database_write_locked_during_check(tmp_path,monkeypatch):
    import rpe_harness.evolution.candidates as module
    db,p=candidate(tmp_path)
    validate_candidate(p['proposal_id'],db)
    store=EvolutionStore(db)
    def corrupt(stage):
        if stage=='intent_persisted':
            with store.connect() as connection:
                connection.execute('UPDATE evolution_proposals SET validation_json=? WHERE proposal_id=?',('corrupt',p['proposal_id']))
    with pytest.raises(ValueError):
        apply_candidate(p['proposal_id'],db,_checkpoint=corrupt)
    assert store.pointer('executor','csv_executor')[0] is None
    db,p=candidate(tmp_path/'separate-lock-probe')
    validate_candidate(p['proposal_id'],db)
    original=module._certificate
    seen=0
    def checked_under_write_lock(store,proposal,**kwargs):
        nonlocal seen
        result=original(store,proposal,**kwargs)
        seen+=1
        if seen==2:
            with sqlite3.connect(db,timeout=0) as other:
                with pytest.raises(sqlite3.OperationalError,match='locked'):
                    other.execute('UPDATE evolution_proposals SET validation_json=? WHERE proposal_id=?',('corrupt',p['proposal_id']))
        return result
    monkeypatch.setattr(module,'_certificate',checked_under_write_lock)
    assert apply_candidate(p['proposal_id'],db)['applied'] is True
    assert seen==2


def test_rollback_corrupt_predecessor_restores_builtin(tmp_path):
    db,p=candidate(tmp_path)
    validate_candidate(p['proposal_id'],db)
    a=apply_candidate(p['proposal_id'],db)
    q=propose_candidate(db,tmp_path,component='executor',kind='csv_executor',content=GOOD_CODE+'\n# next\n',evidence_run_ids=p['evidence_run_ids'])
    validate_candidate(q['proposal_id'],db)
    b=apply_candidate(q['proposal_id'],db)
    path=Path(a['artifact_path']); path.chmod(0o600); path.write_text('corrupt')
    result=rollback(b['version_id'],db)
    assert result['restored_version_id'] is None
    assert 'invalid' in result['reason']
    assert EvolutionStore(db).pointer('executor','csv_executor')[0] is None
    assert EvolutionStore(db).get_version(a['version_id'])['state']=='quarantine'


def test_real_control_sandbox_observer_hash_works_without_gate_import(tmp_path):
    from rpe_harness.integrations.hermes_session import sandbox_profile
    profile=sandbox_profile([tmp_path],Path.home()/'.hermes',native_import_root=Path('/Users/audrey/项目/rpe-harness/src'))
    program="import sys;sys.path.insert(0,'/Users/audrey/项目/rpe-harness/src');from rpe_harness.integrations.hermes import _metadata;print(_metadata({'request':{'body':{'tool_choice':'auto','tools':[]}}}))"
    r=subprocess.run(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-B','-c',program],capture_output=True,text=True,cwd=tmp_path)
    assert r.returncode==0,r.stderr
    assert 'request_tools_sha256' in r.stdout


def test_installed_package_root_denied_even_when_prefix_readable(tmp_path,monkeypatch):
    from rpe_harness.evolution import sandbox
    fake_prefix=tmp_path/'prefix'
    pkg=fake_prefix/'lib/python3.13/site-packages/rpe_harness'
    (pkg/'evolution').mkdir(parents=True)
    oracle=pkg/'verification.py';oracle.write_text('private oracle')
    monkeypatch.setattr(sandbox,'__file__',str(pkg/'evolution/sandbox.py'))
    monkeypatch.setattr(sandbox.sys,'prefix',str(fake_prefix))
    readable=fake_prefix/'readable.txt';readable.write_text('prefix-readable')
    profile=sandbox._profile(tmp_path,tmp_path/'staging',tmp_path/'log')
    program=f'from pathlib import Path;print(Path({str(readable)!r}).read_text(),flush=True);Path({str(oracle)!r}).read_text()'
    r=subprocess.run(['/usr/bin/sandbox-exec','-p',profile,str(Path(sys.executable).resolve()),'-I','-B','-c',program],capture_output=True,text=True,cwd=tmp_path)
    assert r.returncode!=0
    assert 'prefix-readable' in r.stdout
    assert 'PermissionError' in r.stderr


def test_valid_strict_empty_key_rejection_can_activate(tmp_path):
    code=GOOD_CODE.replace('        if key not in seen:',"        if any(not item.strip() for item in key):\n            raise ValueError('empty key values are not supported')\n        if key not in seen:")
    db,p=candidate(tmp_path,code)
    r=validate_candidate(p['proposal_id'],db)
    assert r['passed'] is True,r
    assert apply_candidate(p['proposal_id'],db)['applied'] is True
