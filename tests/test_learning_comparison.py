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
        assert Path(options['read_isolation_root'])==tmp_path
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
                            capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==[True,True,False,False,False]
