"""Learning comparison safeguards; external model boundary is never called here."""
import importlib
import json
from pathlib import Path

import pytest


def module():
    name = 'rpe_harness.experiments.learning_comparison'
    assert importlib.util.find_spec(name), 'learning comparison driver is not implemented'
    return importlib.import_module(name)


def test_registration_aligns_48_sessions_and_keeps_holdout_unopened(tmp_path):
    m = module()
    plan = m.preregister_learning(tmp_path)
    assert len(plan['training']) == 6 and len(plan['holdout']) == 8
    assert plan['generation_tool_profile'] == 'producer-only/native-write-file'
    assert len(plan['sessions']) == 48
    assert len({c['input_sha256'] for c in plan['training'] + plan['holdout']}) == 14
    assert len({s['attempt_id'] for s in plan['sessions']}) == 48
    for case in plan['holdout']:
        sessions = [s for s in plan['sessions'] if s['case_id'] == case['case_id']]
        assert len(sessions) == 6
        assert len({s['prompt_sha256'] for s in sessions if s['repeat'] == 1}) == 1
        assert len({s['input_sha256'] for s in sessions}) == 1
    assert not list((tmp_path / 'arms').rglob('*.csv'))
    assert m.preregister_learning(tmp_path) == plan
    document = tmp_path / 'preregistration.json'
    tampered = json.loads(document.read_text())
    tampered['holdout'][0]['source_text'] += 'secret\n'
    document.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match='registration'):
        m.preregister_learning(tmp_path)


def test_literal_truth_disagrees_with_plausible_wrong_candidate(tmp_path):
    m = module()
    plan = m.preregister_learning(tmp_path)
    case = next(c for c in plan['holdout'] if c['case_id'] == 'eval-exact')
    output = tmp_path / 'bad.csv'
    output.write_text('value,ref\nfirst,1\n', encoding='utf-8')
    assert m.artifact_matches(output, case) is False
    assert case['expected_rows'] == [['value', 'ref'], ['first', '001'], ['numeric', '1'], ['space', ' 001']]


def test_summary_does_not_turn_fallback_or_unknown_cost_into_learning_benefit():
    m = module()
    records = [dict(attempt_id='a', arm='rpe_learned', case_id='one', repeat=1,
                    status='succeeded', artifact_correct=True, workflow_verified=True,
                    candidate_direct_success=False, fallback=True, model_identity=None),
               dict(attempt_id='b', arm='native', case_id='one', repeat=1,
                    status='failed', artifact_correct=False, workflow_verified=False,
                    candidate_direct_success=False, fallback=False, model_identity=None)]
    phases = [dict(attempt_id='a', arm='rpe_learned', phase='production',
                   status='failed', api_expected=True, elapsed_seconds=3,
                   usage={'known_total_tokens': 12, 'total_tokens': None})]
    report = m.summarize_learning(records, phases)
    assert report['arms']['rpe_learned']['candidate_direct_successes'] == 0
    assert report['cost']['total_tokens'] is None
    assert report['cost']['known_total_tokens'] == 12
    assert report['cost']['failed_phases'] == 1
    assert report['learning_benefit_established'] is False
    assert report['analysis_units'] == 8


def test_interrupted_attempt_is_not_replayed_or_overwritten(tmp_path):
    m = module()
    m.preregister_learning(tmp_path)
    calls = []
    def crash():
        calls.append(1)
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        m._phase(tmp_path, 'native', 'training', 'native-crash', crash, api_expected=True)
    result = m._phase(tmp_path, 'native', 'training', 'native-crash', lambda: calls.append(2), api_expected=True)
    assert calls == [1]
    assert result['status'] == 'incomplete'


def test_actual_cli_training_builds_six_verified_semantic_examples(tmp_path):
    m = module()
    plan = m.preregister_learning(tmp_path)
    packet = m.seed_training(tmp_path, plan, 'rpe_learned')
    assert packet['schema_version'] == 'rpe.training-feedback/v1'
    assert len(packet['evidence_run_ids']) == 6
    assert len(set(packet['evidence_run_ids'])) == 6
    assert packet['examples'][0]['expected_rows'] == [['order', 'amount'], ['001', '10'], ['002', '20']]
    from rpe_harness.evolution.training_feedback import validate_training_packet
    assert validate_training_packet(tmp_path/'arms/rpe_learned/runs.sqlite', packet) == packet
    assert m.seed_training(tmp_path, plan, 'rpe_learned') == packet
    phases = m.PhaseLedger(tmp_path/'ledger').records()
    assert len(phases) == 6
    assert all(p['status'] == 'completed' and p['api_expected'] is False for p in phases)


def test_candidate_version_hash_and_fallback_are_separate():
    m = module()
    frozen = {'version_id':'frozen-v1','candidate_sha256':'a'*64}
    assert m.executor_score({'kind':'candidate','candidate_version_id':'frozen-v1',
        'code_sha256':'a'*64}, False, frozen) is True
    assert m.executor_score({'kind':'builtin'}, True, frozen) is False
    assert m.executor_score({'kind':'candidate','candidate_version_id':'frozen-v2',
        'code_sha256':'a'*64}, False, frozen) is False
    assert m.executor_score({'kind':'candidate','candidate_version_id':'frozen-v1',
        'code_sha256':'b'*64}, False, frozen) is False


def test_formal_run_requires_existing_freeze_before_any_work(tmp_path):
    m = module()
    with pytest.raises(ValueError, match='freeze'):
        m.run_learning_comparison(tmp_path, formal=True)
    assert not list((tmp_path/'ledger').glob('*.reservation.json'))


def test_real_pilot_freezes_qualified_original_and_refuses_source_change(tmp_path, monkeypatch):
    m = module()
    from test_candidates import GOOD_CODE
    from rpe_harness.evolution import propose_candidate
    from rpe_harness.evolution.training_feedback import build_training_packet
    from rpe_harness.evolution import worker
    from rpe_harness.integrations import hermes_session
    generations = []
    identity = {'provider':'test','requested_model':'test-model','response_models':['test-model']}
    def generate(db, owned, **options):
        ids = [r['result']['run_id'] for p in sorted((tmp_path/'records').glob('train-*-rpe_learned.json'))
               if (r:=json.loads(p.read_text()))['result']['status']=='succeeded']
        index = len(generations)
        generations.append(options)
        code = GOOD_CODE + ('\n# revised\n' if index else '')
        artifact = Path(owned)/f'original-{index}.py'
        artifact.write_text(code)
        proposal = propose_candidate(db, owned, component='executor',kind='csv_executor',content=code,evidence_run_ids=ids)
        packet = options.get('training_packet')
        return {'completed':True,'proposal_id':proposal['proposal_id'], 'candidate_sha256':m._sha(code.encode()),
            'code_artifact_path':str(artifact),'model_identity':identity,'usage':None,
            'training_packet_sha256':packet['packet_sha256'] if packet else None,
            'generation_link':{'proven':True}, 'session_id':f'generation-{index}'}
    def native(prompt, workspace, database, **options):
        # Real files from a public training input; model boundary alone is replaced.
        import csv
        # Each prompt identifies the opened current directory, not sorted state.
        label = prompt.split('输入 ',1)[1].split('/input.csv',1)[0]
        directory = Path(workspace)/label
        plan = json.loads((tmp_path/'preregistration.json').read_text())
        case = next(c for c in plan['training']+plan['holdout'] if label==c['case_id'] or label.startswith(c['case_id']+'-r'))
        assert Path(options['read_isolation_root'])==tmp_path.parent
        assert tmp_path/'evaluator' in options['deny_read_roots']
        with (directory/'output.csv').open('w',encoding=case['parameters']['encoding'],newline='') as out:
            csv.writer(out,delimiter=case['parameters']['delimiter']).writerows(case['expected_rows'])
        return {'completed':True,'exit_code':0,'timed_out':False,'events':[],
            'session_id':'native-'+label,'model_identity':identity,'usage':None,
            'original_prompt_sha256':m._sha(prompt.encode()), 'observer_errors':0,
            'process_cleanup':{'cleanup_complete':True},'stream_tool_calls':[]}
    monkeypatch.setattr(worker,'run_proposal_worker',generate)
    monkeypatch.setattr(hermes_session,'run_hermes_session',native)
    # Host evidence is intentionally absent: correctness must not become invocation proof.
    result = m.run_learning_comparison(tmp_path)
    assert result['stage'] == 'frozen'
    assert len(generations) == 2
    assert all(options['producer_only'] is True for options in generations)
    assert generations[0]['training_packet'] is None
    assert generations[1]['previous_candidate'] == GOOD_CODE
    assert generations[1]['training_packet']['schema_version'] == 'rpe.training-feedback/v1'
    assert str(tmp_path/'evaluator') in map(str,generations[1]['deny_read_roots'])
    frozen = m._read_sealed(tmp_path/'freeze.json')
    assert frozen['candidate_sha256'] == m._sha((GOOD_CODE+'\n# revised\n').encode())
    assert frozen['training_behavior_improved'] is False
    assert frozen['ast_changed'] is False
    assert not list((tmp_path/'arms').rglob('eval-*/input.csv'))
    assert m.run_learning_comparison(tmp_path)['freeze']['sha256'] == frozen['sha256']
    formal = m.run_learning_comparison(tmp_path,formal=True)
    assert formal['completed_sessions']==48
    assert len(formal['records'])==48
    assert formal['arms']['rpe_learned']['artifact_successes']==16
    assert formal['arms']['rpe_learned']['candidate_direct_successes']==0
    assert formal['arms']['native']['workflow_successes']==0
    assert sum(r['fresh_session'] is False for r in formal['records'])==32
    saved = list((tmp_path/'records').glob('eval-*.json'))
    assert len(saved)==48
    assert m.run_learning_comparison(tmp_path,formal=True)['completed_sessions']==48
    assert len(list((tmp_path/'records').glob('eval-*.json')))==48
    original_path = Path(frozen['code_artifact_path'])
    original_code = original_path.read_bytes()
    original_path.write_bytes(original_code+b'\n# changed after freeze\n')
    with pytest.raises(ValueError,match='frozen'):
        m.run_learning_comparison(tmp_path,formal=True)
    original_path.write_bytes(original_code)
    environment = m._environment()
    environment['python_version'] = 'changed'
    monkeypatch.setattr(m,'_environment',lambda: environment)
    with pytest.raises(ValueError,match='frozen'):
        m.run_learning_comparison(tmp_path,formal=True)


def test_failed_qualification_is_a_failed_cost_phase(tmp_path):
    m = module()
    m.preregister_learning(tmp_path)
    result = m._phase(tmp_path,'rpe_learned','qualification','bad-qualification',
        lambda:{'passed':False,'checks':[]},api_expected=False)
    assert result['status']=='failed'
    assert m.PhaseLedger(tmp_path/'ledger').records()[0]['status']=='failed'


def test_pending_records_are_not_completed_sessions():
    m = module()
    report = m.summarize_learning([{'attempt_id':'reserved','arm':'native','case_id':'input1',
                                   'repeat':1,'status':'incomplete'}],[])
    assert report['completed_sessions']==0
    assert report['reserved_sessions']==1


def test_comparison_deltas_preserve_negative_effects_and_unknown_cost():
    m = module()
    records = [{'attempt_id':arm,'arm':arm,'case_id':'one','repeat':1,'status':'succeeded',
                'artifact_correct':arm!='rpe_learned'} for arm in m.ARMS]
    result = m.summarize_learning(records,[])
    assert result['comparisons']['learned_minus_fixed']['artifact_success_delta']==-1
    assert result['comparisons']['learned_minus_fixed']['total_tokens_delta'] is None


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(),reason='macOS sandbox boundary')
def test_real_scope_keeps_own_previous_workspace_but_denies_other_arms_and_future_truth(tmp_path):
    import subprocess
    import sys
    from rpe_harness.integrations.hermes_session import sandbox_profile
    m = module()
    m.preregister_learning(tmp_path)
    own = tmp_path/'arms/native/workspace'
    previous = own/'previous.txt'
    previous.write_text('prior own state')
    current = own/'current.txt'
    current.write_text('current input')
    other = tmp_path/'arms/rpe_learned/workspace/private.txt'
    other.write_text('other arm state')
    future = tmp_path/'evaluator/future.csv'
    future.write_text('unopened truth')
    paths = [current,previous,other,future,tmp_path/'preregistration.json']
    profile = sandbox_profile([own],tmp_path/'fake-home',owner_read_scope=(tmp_path,[own]),
                              deny_read_roots=m._denied(tmp_path,'native'))
    program = 'import json\nfrom pathlib import Path\nresult=[]\nfor p in '+repr(list(map(str,paths)))+':\n try:\n  Path(p).read_bytes();result.append(True)\n except PermissionError:\n  result.append(False)\nprint(json.dumps(result))'
    result = subprocess.run(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-B','-c',program],
                            capture_output=True,text=True,timeout=10,cwd=own)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==[True,True,False,False,False]


def test_post_generation_evidence_change_preserves_observed_cost_and_original(tmp_path, monkeypatch):
    m = module()
    from rpe_harness.evolution import worker, training_feedback
    packet = {'packet_sha256':'p'*64}
    monkeypatch.setattr(m,'seed_training',lambda *args:packet)
    monkeypatch.setattr(m,'_host_session',lambda *args:{'status':'failed'})
    validations=[]
    def validate(*args):
        validations.append(1)
        if len(validations)==2:
            raise ValueError('private changed evidence detail')
        return packet
    monkeypatch.setattr(training_feedback,'validate_training_packet',validate)
    identity = {'provider':'test','requested_model':'test','response_models':['test']}
    code = 'def prepare(plan, staging_path): raise ValueError("test")\n'
    def generate(database, owned, **options):
        artifact = Path(owned)/'original.py'
        artifact.write_text(code)
        return {'completed':True,'candidate_sha256':m._sha(code.encode()),'code_artifact_path':str(artifact),
                'session_id':'observed-generation','model_identity':identity,
                'usage':{'api_requests':2,'coverage_complete':False,'known_total_tokens':123,'total_tokens':None},
                'generation_link':{'proven':True,'content_sha256':m._sha(code.encode())}}
    monkeypatch.setattr(worker,'run_proposal_worker',generate)
    result = m.run_learning_comparison(tmp_path)
    assert result['stage']=='failed' and result['reason']=='training_evidence_changed'
    evidence = m._read_sealed(tmp_path/'records/generation-G0.json')['result']
    assert evidence['completed'] is False and evidence['training_evidence_verified'] is False
    assert evidence['error_code']=='training_evidence_changed' and evidence['error_type']=='ValueError'
    assert evidence['session_id']=='observed-generation' and evidence['model_identity']==identity
    assert Path(evidence['code_artifact_path']).read_text()==code
    assert evidence['generation_link']['proven'] is True
    assert result['cost']['known_total_tokens']==123
    assert result['cost']['total_tokens'] is None
    phase = next(p for p in m.PhaseLedger(tmp_path/'ledger').records() if p['attempt_id']=='generation-G0')
    assert phase['usage']['known_total_tokens']==123 and phase['elapsed_seconds']>=0
    assert not (tmp_path/'freeze.json').exists()
    assert not (tmp_path/'records/generation-G1.json').exists()
    assert 'private changed evidence detail' not in (tmp_path/'records/generation-G0.json').read_text()


@pytest.mark.parametrize('kind',['symlink_parent','traversal','missing_parent','relative'])
def test_registration_rejects_unsafe_ancestry_before_any_write(tmp_path, monkeypatch, kind):
    m = module()
    outside = tmp_path/'outside'
    outside.mkdir()
    link = tmp_path/'link'
    link.symlink_to(outside,target_is_directory=True)
    if kind=='symlink_parent':
        root = link/'experiment'
    elif kind=='traversal':
        root = tmp_path/'outside/../experiment'
    elif kind=='missing_parent':
        root = tmp_path/'missing/experiment'
    else:
        monkeypatch.chdir(tmp_path)
        root = Path('relative-experiment')
    before = set(tmp_path.rglob('*'))
    with pytest.raises((ValueError,OSError)):
        m.preregister_learning(root)
    assert set(tmp_path.rglob('*'))==before
    assert not (outside/'experiment').exists()


def _linked_learning_evidence(m, plan):
    """Literal acceptance fixture; no generated-model or runtime-success claim."""
    identity = {'provider':'test','requested_model':'test','response_models':['test']}
    before,after,packet_hash='0'*64,'1'*64,'2'*64
    ids=[f'training-{i}' for i in range(6)]
    outcomes=[{'run_id':identifier,'candidate_invoked':True} for identifier in ids]
    packet={'schema_version':'rpe.training-feedback/v1','packet_sha256':packet_hash,'evidence_run_ids':ids}
    frozen={'registration_sha256':plan['sha256'],'generation':1,'g0_sha256':before,'candidate_sha256':after,'packet_sha256':packet_hash,
            'version_id':'frozen-version','proposal_id':'proposal-one','model_identity':identity}
    pilot={
        'generation-G0':{'status':'succeeded','completed':True,'candidate_sha256':before,'model_identity':identity,
            'generation_link':{'proven':True,'content_sha256':before},'training_evidence_verified':True},
        'probe-G0':{'status':'succeeded','candidate_sha256':before,'packet_sha256':packet_hash,'outcomes':outcomes},
        'generation-G1':{'status':'succeeded','completed':True,'candidate_sha256':after,'proposal_id':'proposal-one',
            'model_identity':identity,'generation_link':{'proven':True,'content_sha256':after},
            'training_evidence_verified':True,'training_packet_sha256':packet_hash,'training_packet':packet,
            'previous_candidate_sha256':before,'training_feedback':{'candidate_sha256':before,
                'packet_sha256':packet_hash,'outcomes':outcomes}},
        'probe-G1':{'status':'succeeded','candidate_sha256':after,'packet_sha256':packet_hash,'outcomes':outcomes},
        'qualify-G1':{'status':'succeeded','passed':True,'proposal_id':'proposal-one','content_hash':after},
        'activate-G1':{'status':'succeeded','applied':True,'version_id':'frozen-version','content_hash':after}}
    records=[]
    for trial in plan['sessions']:
        learned=trial['arm']=='rpe_learned'
        records.append({**trial,'status':'succeeded' if learned else 'failed',
            'model_identity':identity if learned else None,'session_id':trial['attempt_id'],
            'run_id':'run-'+trial['attempt_id'] if learned else None,'fresh_session':True,'fresh_run':learned,
            'candidate_direct_success':learned,'artifact_correct':learned,'product_oracle_passed':learned,
            'workflow_verified':learned,'comparable':learned,'fallback':False,
            'actual_executor':{'kind':'candidate','candidate_version_id':'frozen-version','code_sha256':after}})
    return frozen,pilot,records


def test_one_proven_new_input_reuse_verifies_loop_without_asserting_full_quality(tmp_path):
    m = module()
    plan=m.preregister_learning(tmp_path)
    frozen,pilot,records=_linked_learning_evidence(m,plan)
    learned=[r for r in records if r['arm']=='rpe_learned']
    learned[-1].update(status='failed',candidate_direct_success=False,artifact_correct=False)
    verdict=m._learning_acceptance(plan,frozen,records,pilot)
    assert verdict['learning_loop_verified'] is True
    assert verdict['all_evaluation_reuses_verified'] is False
    assert verdict['comparison_complete'] is True
    assert verdict['comparison_model_alignment_verified'] is False
    # Native identity gaps do not invalidate a linked learned-arm loop or its own quality result.
    learned[-1].update(status='succeeded',candidate_direct_success=True,artifact_correct=True)
    verdict=m._learning_acceptance(plan,frozen,records,pilot)
    assert verdict['learning_loop_verified'] is True and verdict['all_evaluation_reuses_verified'] is True
    # An existence result is allowed after one real new-input observation, without ending the schedule.
    assert m._learning_acceptance(plan,frozen,[learned[0]],pilot)['learning_loop_verified'] is True
    assert m._learning_acceptance(plan,frozen,[learned[0]],pilot)['comparison_complete'] is False
    # G0 may have a proposal failure after proven code generation; G1 owns qualification.
    pilot['generation-G0'].update(status='failed',completed=False,error_code='proposal_failed')
    assert m._learning_acceptance(plan,frozen,[learned[0]],pilot)['learning_loop_verified'] is True


@pytest.mark.parametrize('broken',['feedback','qualification','activation','generation_link','candidate_hash','model_identity','fresh_run','unseen_input'])
def test_learning_loop_refuses_missing_chain_or_unfresh_reuse(tmp_path, broken):
    m=module()
    plan=m.preregister_learning(tmp_path)
    frozen,pilot,records=_linked_learning_evidence(m,plan)
    reuse=next(r for r in records if r['arm']=='rpe_learned')
    if broken=='feedback':
        pilot['generation-G1']['training_feedback']['candidate_sha256']='wrong'
    elif broken=='qualification':
        pilot['qualify-G1']['passed']=False
    elif broken=='activation':
        pilot['activate-G1']['version_id']='wrong'
    elif broken=='generation_link':
        pilot['generation-G1']['generation_link']['proven']=False
    elif broken=='candidate_hash':
        reuse['actual_executor']['code_sha256']='wrong'
    elif broken=='model_identity':
        reuse['model_identity']={'provider':'other','response_models':['other']}
    elif broken=='fresh_run':
        reuse['fresh_run']=False
    else:
        reuse['case_id']=plan['training'][0]['case_id']
        reuse['input_sha256']=plan['training'][0]['input_sha256']
    assert m._learning_acceptance(plan,frozen,[reuse],pilot)['learning_loop_verified'] is False


@pytest.mark.parametrize('arm',['native','rpe_learned'])
@pytest.mark.parametrize('with_usage',[True,False])
def test_direct_host_observation_error_keeps_safe_paid_evidence(tmp_path,monkeypatch,arm,with_usage):
    m=module()
    from rpe_harness.integrations import hermes_session
    plan=m.preregister_learning(tmp_path)
    case=plan['training'][0]
    identity={'provider':'test','requested_model':'test','response_models':['test']}
    def paid_but_unreadable(prompt,workspace,database,**options):
        target=Path(workspace)/case['case_id']/'output.csv'
        target.write_text('order,amount\n001,10\n002,20\n')
        diagnostics={'completed':True,'exit_code':0,'session_id':'paid-session','model_identity':identity,
            'usage':{'api_requests':1,'known_total_tokens':11,'total_tokens':11,'coverage_complete':True} if with_usage else None,
            'original_prompt_sha256':m._sha(prompt.encode()),
            'process_cleanup':{'cleanup_complete':True,'remaining_descendants':[]},
            'session_root':str(tmp_path/'owned-session'),
            'events':[{'kind':'post_api_request','api_request_id':'paid-api','args':{'private':'PRIVATE_SOURCE'},
                       'usage':{'raw_provider_usage':{'private':'PRIVATE_USAGE'}},'response_model':'test'}],
            'final_response':'PRIVATE_RESPONSE','stream_tool_calls':[{'input':{'private':'PRIVATE_ARGUMENTS'}}]}
        raise hermes_session.SessionObservationError(diagnostics,secret='PRIVATE_SECRET',error_type='JSONDecodeError')
    monkeypatch.setattr(hermes_session,'run_hermes_session',paid_but_unreadable)
    result=m._phase(tmp_path,arm,'training','paid-direct-host',
        lambda:m._host_session(tmp_path,plan,arm,case,case['case_id']),api_expected=True)
    assert result['status']=='failed' and result['observation_failed'] is True
    assert result['artifact_correct'] is True
    assert result['workflow_verified'] is False and result['candidate_direct_success'] is False
    assert result['session_id']=='paid-session' and result['model_identity']==identity
    assert result['process_cleanup']['cleanup_complete'] is True
    assert result['session_root']==str(tmp_path/'owned-session')
    cost=m.summarize_phases(m.PhaseLedger(tmp_path/'ledger').records())
    assert cost['known_total_tokens']==(11 if with_usage else 0)
    assert cost['total_tokens'] is None
    saved=(tmp_path/'records/paid-direct-host.json').read_text()
    assert 'PRIVATE_' not in saved and 'raw_provider_usage' not in saved
    assert 'stream_tool_calls' not in saved and 'final_response' not in saved


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(),reason='macOS sandbox boundary')
def test_installed_native_cannot_read_other_checkout_sources_but_can_load_observer(tmp_path,monkeypatch):
    import subprocess
    import sys
    from rpe_harness.evolution import worker
    from rpe_harness.integrations.hermes_session import sandbox_profile
    m=module()
    experiment=tmp_path/'experiment'
    m.preregister_learning(experiment)
    workspace=experiment/'arms/native/workspace'
    own=workspace/'current.txt'
    own.write_text('own current input')
    development=tmp_path/'development-checkout'
    development.mkdir()
    heldout=development/'learning_comparison.py'
    heldout.write_text('future independent truth')
    primary=tmp_path/'primary-checkout'
    primary.mkdir()
    oracle=primary/'qualification.py'
    oracle.write_text('independent qualification truth')
    installed=tmp_path/'installed-site-packages'
    observer=installed/'rpe_harness/integrations/hermes.py'
    observer.parent.mkdir(parents=True)
    observer.write_text('minimal observer bootstrap')
    monkeypatch.setattr(worker,'_deny_read_roots',lambda:[development,primary,installed/'rpe_harness'])
    denied=m._denied(experiment,'native')
    profile=sandbox_profile([workspace],tmp_path/'fake-home',native_import_root=installed,
        owner_read_scope=(experiment,[workspace]),deny_read_roots=denied)
    paths=[own,heldout,oracle,observer]
    program='import json\nfrom pathlib import Path\nresult=[]\nfor p in '+repr(list(map(str,paths)))+':\n try:\n  Path(p).read_bytes();result.append(True)\n except PermissionError:\n  result.append(False)\nprint(json.dumps(result))'
    completed=subprocess.run(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-B','-c',program],
                             capture_output=True,text=True,timeout=10,cwd=workspace)
    assert completed.returncode==0,completed.stderr
    assert json.loads(completed.stdout)==[True,False,False,True]
    # Strict RPE hosts still need their trusted package imports; they have no native read/terminal tools.
    assert development not in m._denied(experiment,'rpe_learned')
    assert installed/'rpe_harness' not in m._denied(experiment,'rpe_no_experience')


def test_posthost_fifo_is_rejected_without_hanging_and_keeps_paid_usage(tmp_path):
    import subprocess
    import sys
    program = r'''
import json,os,sys
from pathlib import Path
from rpe_harness.experiments import learning_comparison as m
from rpe_harness.integrations import hermes_session
root=Path(sys.argv[1])
plan=m.preregister_learning(root,timeout=1)
case=plan['training'][0]
def host(prompt,workspace,database,**options):
    source=Path(workspace)/case['case_id']/'input.csv'
    output=source.with_name('output.csv')
    output.write_text('order,amount\n001,10\n002,20\n')
    source.unlink()
    os.mkfifo(source)
    return {'completed':True,'exit_code':0,'session_id':'fifo-session','events':[],
        'usage':{'api_requests':1,'known_total_tokens':11,'total_tokens':None,'coverage_complete':False},
        'model_identity':{'provider':'test','requested_model':'test','response_models':['test']},
        'original_prompt_sha256':m._sha(prompt.encode()),'process_cleanup':{'cleanup_complete':True}}
hermes_session.run_hermes_session=host
result=m._phase(root,'native','training','fifo-source',
    lambda:m._host_session(root,plan,'native',case,case['case_id']),api_expected=True)
cost=m.summarize_phases(m.PhaseLedger(root/'ledger').records())
print(json.dumps({'status':result['status'],'artifact_correct':result['artifact_correct'],
    'known_total_tokens':cost['known_total_tokens'],'total_tokens':cost['total_tokens']}))
'''
    child=subprocess.Popen([sys.executable,'-B','-c',program,str(tmp_path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    timed_out=False
    try:
        try:
            stdout,stderr=child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            timed_out=True
            child.kill()
            stdout,stderr=child.communicate(timeout=3)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
    assert not timed_out,'post-host source FIFO blocked until exact child was killed'
    assert child.returncode==0,stderr
    assert json.loads(stdout)=={'status':'failed','artifact_correct':False,'known_total_tokens':11,'total_tokens':None}


@pytest.mark.parametrize('source_changed',[False,True])
def test_business_correctness_requires_source_preservation_but_not_invocation_proof(tmp_path,monkeypatch,source_changed):
    m=module()
    from rpe_harness.integrations import hermes_session
    plan=m.preregister_learning(tmp_path)
    case=plan['training'][0]
    def host(prompt,workspace,database,**options):
        source=Path(workspace)/case['case_id']/'input.csv'
        source.with_name('output.csv').write_text('order,amount\n001,10\n002,20\n')
        if source_changed:
            source.write_text('order,amount\n777,5\n')
        return {'completed':True,'exit_code':0,'session_id':'business-session','events':[],
            'usage':{'api_requests':1,'known_total_tokens':11,'total_tokens':None,'coverage_complete':False},
            'model_identity':{'provider':'test','requested_model':'test','response_models':['test']},
            'original_prompt_sha256':m._sha(prompt.encode()),'process_cleanup':{'cleanup_complete':True}}
    monkeypatch.setattr(hermes_session,'run_hermes_session',host)
    result=m._phase(tmp_path,'native','training','business-contract',
        lambda:m._host_session(tmp_path,plan,'native',case,case['case_id']),api_expected=True)
    assert result['source_preserved'] is not source_changed
    assert result['artifact_correct'] is not source_changed
    assert result['artifact_rows_correct'] is True
    assert result['workflow_verified'] is False and result['invocation_evidence'] is False
    summary=m.summarize_learning([{**result,'attempt_id':'business-contract','arm':'native','case_id':case['case_id']}],
                                m.PhaseLedger(tmp_path/'ledger').records())
    assert summary['arms']['native']['artifact_successes']==(0 if source_changed else 1)
    assert summary['cost']['known_total_tokens']==11


def test_oversized_output_never_reaches_unbounded_hash_read_or_product_oracle(tmp_path,monkeypatch):
    m=module()
    from rpe_harness.integrations import hermes_session
    from rpe_harness import tasks
    plan=m.preregister_learning(tmp_path)
    case=plan['training'][0]
    unbounded=[]
    original_read=Path.read_bytes
    def guarded_read(path):
        if path.name=='output.csv':
            unbounded.append(str(path))
            raise AssertionError('unbounded output read')
        return original_read(path)
    monkeypatch.setattr(Path,'read_bytes',guarded_read)
    oracle_calls=[]
    def forbidden_oracle(*args):
        oracle_calls.append(1)
        raise AssertionError('invalid output reached product oracle')
    monkeypatch.setattr(tasks,'verify_plan',forbidden_oracle)
    def host(prompt,workspace,database,**options):
        output=Path(workspace)/case['case_id']/'output.csv'
        with output.open('wb') as stream:
            stream.truncate(16777217)
        return {'completed':True,'exit_code':0,'session_id':'large-session','events':[],
            'usage':{'api_requests':1,'known_total_tokens':11,'total_tokens':None,'coverage_complete':False},
            'process_cleanup':{'cleanup_complete':True}}
    monkeypatch.setattr(hermes_session,'run_hermes_session',host)
    result=m._phase(tmp_path,'native','training','large-output',
        lambda:m._host_session(tmp_path,plan,'native',case,case['case_id']),api_expected=True)
    assert result['status']=='failed' and result['artifact_correct'] is False
    assert unbounded==[] and oracle_calls==[]
    assert m.summarize_phases(m.PhaseLedger(tmp_path/'ledger').records())['known_total_tokens']==11


@pytest.mark.parametrize('proof',['verified','missing','unverified','error','wrong_target','wrong_content','wrong_args','wrong_id','failed_post','duplicate'])
def test_native_write_file_requires_actual_verified_bound_result(tmp_path,monkeypatch,proof):
    m=module()
    from rpe_harness.integrations import hermes_session
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.integrations.hermes_gate import digest_json
    plan=m.preregister_learning(tmp_path)
    case=plan['training'][0]
    def host(prompt,workspace,database,**options):
        content='order,amount\n001,10\n002,20\n'
        output=Path(workspace)/case['case_id']/'output.csv'
        output.write_text(content)
        args={'path':case['case_id']+'/output.csv','content':content}
        if proof=='wrong_target':
            args['path']=case['case_id']+'/different.csv'
        if proof=='wrong_content':
            args['content']='different bytes'
        ids={'session_id':'write-session','task_id':'task-one','turn_id':'turn-one',
             'api_request_id':'api-one','tool_call_id':'call-one'}
        store=HostEventStore(database)
        store.record('pre_api_request',ids)
        store.record('post_api_request',{**ids,'response':{'assistant_message':{'tool_calls':
            [{'id':'call-one','name':'write_file','arguments':args}]}}})
        store.record('pre_tool_call',{**ids,'tool_name':'write_file','arguments_sha256':digest_json(args)})
        store.record('post_tool_call',{**ids,'tool_name':'write_file','arguments_sha256':digest_json(args),
                                      'status':'failed' if proof=='failed_post' else 'ok'})
        stream={'type':'tool_use','name':'write_file','tool_call_id':'call-one','input':args}
        response={'type':'tool_result','name':'write_file','tool_call_id':'call-one','is_error':proof=='error',
            'output':json.dumps({'verified':proof!='unverified','bytes_written':len(args['content'].encode()),
                                 'error':'write failed' if proof=='error' else None})}
        writes=hermes_session._file_write_observations([stream,response],workspace)
        if proof=='missing':
            writes=[]
        elif proof=='wrong_args':
            writes[0]['arguments_sha256']='wrong'
        elif proof=='wrong_id':
            writes[0]['tool_call_id']='other-call'
        elif proof=='duplicate':
            writes*=2
        return {'completed':True,'exit_code':0,'session_id':'write-session','events':store.list('write-session'),
            'original_prompt_sha256':m._sha(prompt.encode()),'process_cleanup':{'cleanup_complete':True},
            'model_identity':{'provider':'test','requested_model':'test','response_models':['test']},
            'stream_tool_calls':[stream],'file_write_observations':writes}
    monkeypatch.setattr(hermes_session,'run_hermes_session',host)
    result=m._host_session(tmp_path,plan,'native',case,case['case_id'])
    assert result['artifact_correct'] is True
    assert result['invocation_evidence'] is (proof=='verified')
    assert result['workflow_verified'] is (proof=='verified')


def test_host_read_boundary_blocks_prior_sibling_experiment(tmp_path, monkeypatch):
    import subprocess,sys
    from rpe_harness.integrations.hermes_session import sandbox_profile
    m=module(); root=tmp_path/'new'; plan=m.preregister_learning(root); workspace=root/'arms/native/workspace'
    own=workspace/'current.txt';own.write_text('current')
    prior=tmp_path/'old';prior.mkdir();truth=prior/'preregistration.json';truth.write_text('hidden prior evaluation')
    # Same outer ownership boundary forwarded by _host_session, not only the new root.
    from rpe_harness.integrations import hermes_session
    captured={}
    def boundary(prompt,work,database,**options):
        captured.update(options)
        return {'completed':False,'events':[],'process_cleanup':{'cleanup_complete':True}}
    monkeypatch.setattr(hermes_session,'run_hermes_session',boundary)
    m._host_session(root,plan,'native',plan['training'][0],'opened')
    assert captured['read_isolation_root']==tmp_path
    profile=tmp_path/'read-boundary.sb';profile.write_text(sandbox_profile([workspace],Path.home()/'.hermes',
        owner_read_scope=(captured['read_isolation_root'],[workspace,Path(sys.executable).resolve().parent.parent])))
    code='import json,sys;from pathlib import Path\nresult=[]\nfor p in sys.argv[1:]:\n try:Path(p).read_bytes();result.append(True)\n except PermissionError:result.append(False)\nprint(json.dumps(result))'
    process=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),sys.executable,'-B','-c',code,str(own),str(truth)],
        cwd=workspace,capture_output=True,text=True,timeout=10)
    assert process.returncode==0,process.stderr
    assert json.loads(process.stdout)==[True,False]
