"""No-model acceptance for bounded registration, oracles, costs and refusal evidence."""
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_address_comparison_exists():
    assert importlib.util.find_spec('rpe_harness.experiments.address_comparison') is not None


@pytest.fixture
def api():
    return importlib.import_module('rpe_harness.experiments.address_comparison')


@pytest.fixture
def registered(api, tmp_path):
    source = tmp_path / 'source'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx',
                    source / 'productivity/xlsx')
    plan = api.preregister_address_comparison(tmp_path / 'comparison', skill_root=source,
        hermes_root=tmp_path / 'hermes', timeout=60, max_turns=6)
    return plan


def test_registration_pairs_public_inputs_without_exposing_oracle(api, registered):
    plan = registered
    assert len(plan['sessions']) == 12
    assert len(plan['pilots']) == 2
    assert {c['capability'] for c in plan['cases'] if c['expected_status'] == 'ready'} == {
        'tabular.deduplicate', 'json.normalize', 'xlsx.inspect'}
    assert {c['expected_status'] for c in plan['cases']} == {'ready','needs_context','unsupported'}
    assert plan['coverage_limits']
    for left, right in zip(plan['sessions'][::2], plan['sessions'][1::2]):
        assert {left['arm'], right['arm']} == {'native','address'}
        assert left['prompt_sha256'] == right['prompt_sha256']
        assert left['input_sha256'] == right['input_sha256']
        for trial in (left, right):
            workspace = Path(trial['workspace'])
            assert {p.name for p in workspace.iterdir()} == {'prompt.txt', trial['input_name']}
            assert 'expected_rows' not in (workspace/'prompt.txt').read_text()
    tsv = next(c for c in plan['cases'] if c['case_id']=='tsv-tab')
    assert b'\t' in Path(tsv['source_path']).read_bytes()
    assert b'\\t' not in Path(tsv['source_path']).read_bytes()
    assert plan['skill_snapshot_id'] and plan['script_sha256']
    assert api.preregister_address_comparison(plan['root'], skill_root=plan['skill_root'],
        hermes_root=plan['hermes_root'], timeout=60, max_turns=6) == plan
    with pytest.raises(ValueError, match='frozen'):
        api.preregister_address_comparison(plan['root'], skill_root=plan['skill_root'],
            hermes_root=plan['hermes_root'], timeout=61, max_turns=6)


def test_bounded_launches_keep_failures_and_never_receive_standard_answer(api, registered):
    calls = []
    def fail(prompt, workspace, db, **kwargs):
        assert 'required_request' not in kwargs
        assert kwargs['skill_snapshot_id'] == registered['skill_snapshot_id']
        assert kwargs['max_task_attempts'] == 1
        assert Path(workspace).is_relative_to(registered['root'])
        calls.append((prompt, kwargs))
        raise RuntimeError('external model transport unavailable')
    first = api.run_address_comparison(registered['root'], timeout=60, formal=True,
        max_sessions=2, runner=fail)
    assert first['formal_attempts'] == 2
    assert first['accepted_sessions'] == 0
    assert calls[0][0] == calls[1][0]
    assert calls[0][1]['entry_mode'] == 'optional'
    assert calls[0][1]['preserve_native_tool_search'] is True
    assert calls[1][1]['entry_mode'] == 'address'
    assert first['costs']['phase_count'] == 2
    assert first['costs']['failed_phases'] == 2
    assert first['costs']['total_tokens'] is None
    again = api.run_address_comparison(registered['root'], timeout=60, formal=True,
        max_sessions=1, runner=fail)
    assert again['formal_attempts'] == 3
    assert len(calls) == 3
    with pytest.raises(ValueError, match='frozen'):
        api.run_address_comparison(registered['root'], timeout=61, formal=True, runner=fail)


def test_pilots_are_separate_bounded_and_always_costed(api, registered):
    def fail(*args, **kwargs):
        raise RuntimeError('failed pilot')
    summary = api.run_address_comparison(registered['root'], timeout=60,
        max_sessions=2, runner=fail)
    assert summary['pilot_attempts'] == 2
    assert summary['formal_attempts'] == 0
    assert summary['costs']['phase_count'] == 2
    assert api.run_address_comparison(registered['root'], timeout=60,
        max_sessions=2, runner=fail)['pilot_attempts'] == 2
    with pytest.raises(ValueError, match='12'):
        api.run_address_comparison(registered['root'], timeout=60, formal=True,
            max_sessions=13, runner=fail)


def test_interrupted_reservation_is_unknown_and_cannot_be_replayed(api, registered):
    from rpe_harness.experiments.cost_ledger import PhaseLedger
    trial = registered['sessions'][0]
    ledger = PhaseLedger(Path(registered['root'])/'ledger')
    ledger.reserve(trial['arm'], 'formal', trial['trial_id'])
    def unexpected(*args, **kwargs):
        pytest.fail('a reserved attempt was replayed')
    summary = api.run_address_comparison(registered['root'], timeout=60, formal=True,
        max_sessions=0, runner=unexpected)
    assert summary['formal_attempts'] == 1
    assert summary['records'][0]['status'] == 'incomplete'
    assert summary['costs']['incomplete_phases'] == 1
    assert summary['costs']['total_tokens'] is None


def test_business_truth_is_separate_from_invocation_and_preserves_paid_observations(api, registered):
    def correct_but_unobserved(prompt, workspace, db, **kwargs):
        (Path(workspace)/'output.csv').write_text('地区,编号,金额\n华东,001,10\n华西,001,20\n华东,1,30\n', encoding='utf-8')
        return {'completed':True,'exit_code':0,'elapsed_seconds':1.25,
            'session_id':'unobserved','process_cleanup':{'cleanup_complete':True},
            'original_prompt_sha256':registered['sessions'][0]['prompt_sha256'],
            'prompt_sha256':registered['sessions'][0]['prompt_sha256'],
            'catalog_snapshot_id':registered['skill_snapshot_id'],
            'model_identity':{'provider':'test','requested_model':'test','response_models':['test']},
            'usage':{'api_requests':1,'total_tokens':37,'known_total_tokens':37,
                     'coverage_complete':True,'missing_observations':0}}
    summary = api.run_address_comparison(registered['root'], timeout=60, formal=True,
        max_sessions=1, runner=correct_but_unobserved)
    record = summary['records'][0]
    assert record['business_correct'] is True
    assert record['product_oracle_passed'] is True
    assert record['source_preserved'] is True
    assert record['invocation_evidence'] is False
    assert record['gate']['error_codes'] == ['native_durable_observations_missing']
    assert record['accepted'] is False
    assert summary['costs']['total_tokens'] == 37
    assert record['billed_cost'] is None


def test_truth_catches_wrong_composite_key_raw_json_and_xlsx_types(api, registered):
    cases = {c['case_id']:c for c in registered['cases']}
    for name, content, expected in [
        ('csv-zh-composite','地区,编号,金额\n华东,001,10\n华东,1,30\n',False),
        ('tsv-tab','code\tnote\nA\tfirst\nB\tsecond\n',True),
        ('json-lossless','{"名字":"小雨","金额":120,"flags":[true,null,false],"nested":{"z":2,"a":"\\u4e2d"}}',False),
        ('json-lossless','{"名字":"小雨","金额":1.20e+2,"flags":[true,null,false],"nested":{"z":2,"a":"\\u4e2d"}}\n',True),
        ('xlsx-original',json.dumps({'sheet':'明细','rows':[['编号','金额','确认'],['001',7,True],['中文',2.5,False]]}),True),
        ('xlsx-original',json.dumps({'sheet':'明细','rows':[['编号','金额','确认'],['001',7,1],['中文',2.5,False]]}),False),
    ]:
        case = cases[name]
        trial = next(t for t in registered['sessions'] if t['case_id']==name)
        target = Path(trial['workspace'])/case['output_name']
        target.write_text(content, encoding='utf-8')
        verdict = api.score_address_artifact(registered, trial)
        assert verdict['business_correct'] is expected
        assert verdict['product_oracle_passed'] is expected
        target.unlink()


def test_refusal_prose_or_unobserved_file_does_not_count(api, registered):
    trial = next(t for t in registered['sessions'] if t['case_id']=='missing-keys')
    workspace = Path(trial['workspace'])
    (workspace/'terminal.json').write_text('{"status":"needs_context","missing":["key_columns"]}')
    result = {'completed':True,'exit_code':0,'final_response':'needs_context', 'events':[],
              'process_cleanup':{'cleanup_complete':True}}
    verdict = api.score_address_artifact(registered, trial)
    assert verdict['business_correct'] is True
    gate = api.evaluate_native_address_session(result, registered, trial)
    assert gate['verified'] is False
    (workspace/'output.csv').write_text('invented output')
    assert api.score_address_artifact(registered, trial)['business_correct'] is False


def test_changed_registration_input_and_symlink_root_are_rejected(api, registered, tmp_path):
    source = Path(registered['cases'][0]['source_path'])
    source.write_text('tampered')
    with pytest.raises(ValueError, match='frozen'):
        api.run_address_comparison(registered['root'], timeout=60, max_sessions=0)
    link = tmp_path/'alias'
    link.symlink_to(registered['root'], target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        api.preregister_address_comparison(link, skill_root=registered['skill_root'])
    with pytest.raises(ValueError, match='owned'):
        api.preregister_address_comparison(tmp_path, skill_root=registered['skill_root'])


def test_export_keeps_public_inputs_costs_and_hashes_outside_owned_root(api, registered, tmp_path):
    destination = tmp_path/'public'
    exported = api.export_address_evidence(registered['root'], destination)
    assert exported['formal_attempts'] == 0
    assert (destination/'summary.json').is_file()
    assert (destination/'preregistration.json').is_file()
    assert (destination/'fixtures/csv-zh-composite.csv').is_file()
    public = (destination/'preregistration.json').read_text()
    assert registered['root'] not in public
    assert registered['skill_root'] not in public
    assert 'expected_rows' in public
    with pytest.raises(ValueError, match='outside'):
        api.export_address_evidence(registered['root'], Path(registered['root'])/'export')


def test_cli_summary_never_launches_model(api, registered):
    process = subprocess.run([registered['helper_python'],'-B',
        str(Path(__file__).parents[1]/'scripts/v18_address_comparison.py'),
        'summary','--root',registered['root']],capture_output=True,text=True,check=True)
    assert json.loads(process.stdout)['formal_attempts'] == 0


def test_production_prompts_do_not_require_extra_native_status_publication(api, registered):
    for trial in registered['sessions']:
        prompt = (Path(trial['workspace'])/'prompt.txt').read_text()
        assert ('terminal.json' in prompt) is (trial['category']=='contract_probe')


def test_sessions_have_isolated_databases_and_no_cross_arm_history(api, registered):
    databases = [t['db'] for t in registered['sessions']+registered['pilots']]
    assert len(set(databases)) == 14
    assert all(Path(db).is_relative_to(registered['root']) for db in databases)
    from rpe_harness.integrations.hermes import HostEventStore
    first,second = registered['sessions'][:2]
    HostEventStore(first['db']).record('on_session_start',{'session_id':'private-session'})
    assert HostEventStore(second['db']).list() == []


def _native_write_result(plan, trial, filename, content):
    """A real event store plus explicit observed file-tool output; no model call."""
    import hashlib
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.integrations.hermes_gate import digest_json
    database = trial.get('db',plan['db'])
    store = HostEventStore(database)
    args = {'path':filename,'content':content}
    ids = {'session_id':trial['trial_id'],'task_id':'task','turn_id':'turn','api_request_id':'api','tool_call_id':'write'}
    store.record('pre_api_request',ids)
    store.record('post_api_request',{**ids,'response':{'assistant_message':{'tool_calls':[{'id':'write','name':'write_file'}]}}})
    call = {**ids,'tool_name':'write_file','arguments_sha256':digest_json(args)}
    store.record('pre_tool_call',call)
    (Path(trial['workspace'])/filename).write_text(content,encoding='utf-8')
    store.record('post_tool_call',{**call,'status':'succeeded'})
    return {'completed':True,'exit_code':0,'session_id':trial['trial_id'],
        'events':store.list(trial['trial_id']),
        'stream_tool_calls':[{'type':'tool_use','name':'write_file','tool_call_id':'write','input':args}],
        'file_write_observations':[{'tool_call_id':'write','verified':True,'bytes_written':len(content.encode()),
            'content_sha256':hashlib.sha256(content.encode()).hexdigest(),
            'arguments_sha256':digest_json(args),
            'target_path_sha256':hashlib.sha256(str(Path(trial['workspace'])/filename).encode()).hexdigest()}],
        'process_cleanup':{'cleanup_complete':True},'elapsed_seconds':1,
        'prompt_sha256':trial['prompt_sha256'],'original_prompt_sha256':trial['prompt_sha256'],
        'catalog_snapshot_id':plan['skill_snapshot_id'],
        'usage':{'api_requests':1,'known_total_tokens':10,'total_tokens':10,'coverage_complete':True,'missing_observations':0},
        'model_identity':{'provider':'test','requested_model':'m','response_models':['m'],'base_url':'https://private.invalid/key'}}


@pytest.mark.parametrize('fault',[None,'forged_file_hash','wrong_call_id','mismatched_post','duplicate_stream','prose_only'])
def test_native_refusal_needs_real_structured_publication_links(api, registered, fault):
    trial = next(t for t in registered['sessions'] if t['case_id']=='missing-keys' and t['arm']=='native')
    result = _native_write_result(registered,trial,'terminal.json','{"status":"needs_context","missing":["key_columns"]}')
    if fault=='forged_file_hash':
        result['file_write_observations'][0]['content_sha256'] = '0'*64
    elif fault=='wrong_call_id':
        result['stream_tool_calls'][0]['tool_call_id'] = 'unrelated'
    elif fault=='mismatched_post':
        result['events'][-1]['arguments_sha256'] = '0'*64
    elif fault=='duplicate_stream':
        result['stream_tool_calls'] *= 2
    elif fault=='prose_only':
        result['events'] = []
    verdict = api.evaluate_native_address_session(result,registered,trial)
    assert verdict['verified'] is (fault is None)
    assert api.score_address_artifact(registered,trial)['business_correct'] is True


def test_real_native_publication_is_counted_but_private_identity_is_not_exported(api, registered, tmp_path):
    trial = registered['sessions'][0]
    def observed(prompt, workspace, db, **kwargs):
        assert db==trial['db']
        return _native_write_result(registered,trial,'output.csv','地区,编号,金额\n华东,001,10\n华西,001,20\n华东,1,30\n')
    summary = api.run_address_comparison(registered['root'],timeout=60,formal=True,max_sessions=1,runner=observed)
    assert summary['production_artifact_successes'] == 1
    assert summary['records'][0]['model_identity'] == {'provider':'test','requested_model':'m','response_models':['m']}
    assert summary['costs']['total_tokens'] == 10
    api.export_address_evidence(registered['root'],tmp_path/'evidence')
    assert 'private.invalid' not in (tmp_path/'evidence/summary.json').read_text()


def test_failed_postlaunch_observation_preserves_known_cost_lower_bound(api, registered):
    from rpe_harness.integrations.hermes_session import SessionObservationError
    def broken(*args,**kwargs):
        raise SessionObservationError({'usage':{'api_requests':2,'known_total_tokens':71,
            'total_tokens':71,'coverage_complete':True,'missing_observations':0},
            'model_identity':{'provider':'test','requested_model':'m','response_models':['m']},
            'elapsed_seconds':2.5,'session_id':'failed-observation'})
    summary = api.run_address_comparison(registered['root'],timeout=60,formal=True,max_sessions=1,runner=broken)
    assert summary['records'][0]['accepted'] is False
    assert summary['costs']['total_tokens'] is None
    assert summary['costs']['known_total_tokens'] == 71
    assert summary['costs']['known_api_requests'] == 2


def test_address_terminal_verification_is_separate_from_business_artifacts(api, registered):
    trial = next(t for t in registered['sessions'] if t['case_id']=='missing-keys' and t['arm']=='address')
    assert api._address_contract(registered,trial,{'verified':False,'rejection_verified':True,
        'status':'accepted_rejection','rejection_reason':'needs_context','missing':['key_columns'],'errors':[]}) is True
    assert api._address_contract(registered,trial,{'verified':False,'rejection_verified':True,
        'status':'accepted_rejection','rejection_reason':'needs_context','missing':[],'errors':[]}) is False


def test_summary_reports_each_arm_and_only_comparable_completed_pairs(api, registered):
    def fail(*args, **kwargs):
        raise RuntimeError('observed wiring failure')
    summary = api.run_address_comparison(registered['root'],timeout=60,formal=True,max_sessions=2,runner=fail)
    assert summary['by_arm']['native']['formal_attempts'] == 1
    assert summary['by_arm']['address']['formal_attempts'] == 1
    assert summary['by_arm']['native']['production_artifact_successes'] == 0
    assert summary['by_arm']['native']['costs']['phase_count'] == 1
    pair = summary['pairs'][0]
    assert pair['case_id'] == 'csv-zh-composite'
    assert pair['observed_arms'] == ['native','address']
    assert pair['comparable'] is False
    assert pair['native_accepted'] is False and pair['address_accepted'] is False
    assert summary['model_visible_handoff_observation_coverage'] == 0


def test_raw_api_context_evidence_is_distinct_from_local_state_size(api, registered, monkeypatch):
    from rpe_harness.integrations import hermes_address
    trial = next(t for t in registered['sessions'] if t['case_id']=='csv-zh-composite' and t['arm']=='address')
    def returned(*args, **kwargs):
        return {'completed':False}
    monkeypatch.setattr(hermes_address,'evaluate_address_session',lambda *args,**kwargs: {
        'verified':False,'errors':[],'handoff_bytes':120,'handoff_sha256':'a'*64,
        'context_minimized':True,'task_context_bytes':470,'task_context_sha256':'b'*64})
    record = api._observe(registered,trial,returned)
    assert record['minimal_rpc_handoff_bytes'] == 120
    assert record['model_visible_handoff_bytes'] == 470
    assert record['model_visible_handoff_sha256'] == 'b'*64
    assert record['accepted'] is False
    monkeypatch.setattr(hermes_address,'evaluate_address_session',lambda *args,**kwargs: {
        'verified':False,'errors':[],'handoff_bytes':120,'handoff_sha256':'a'*64})
    assert api._observe(registered,trial,returned)['model_visible_handoff_bytes'] is None


def test_complete_formal_budget_cannot_launch_a_thirteenth_session(api, registered):
    count = 0
    def fail(*args,**kwargs):
        nonlocal count
        count += 1
        raise RuntimeError('all failures remain')
    summary = api.run_address_comparison(registered['root'],timeout=60,formal=True,runner=fail)
    assert count==12 and summary['formal_attempts']==12
    assert summary['costs']['phase_count']==12
    again = api.run_address_comparison(registered['root'],timeout=60,formal=True,runner=fail)
    assert count==12 and again['formal_attempts']==12
    assert again['costs']['failed_phases']==12


def test_export_preserves_public_prompt_and_hash_without_controller_paths(api, registered, tmp_path):
    destination = tmp_path/'public-prompts'
    api.export_address_evidence(registered['root'],destination)
    prompt = (destination/'prompts/csv-zh-composite.txt').read_text()
    assert '地区和编号合起来才算同一笔' in prompt
    assert registered['helper_python'] not in prompt
    assert '<frozen-helper-python>' in prompt
    public = json.loads((destination/'preregistration.json').read_text())
    assert public['prompt_redaction_policy']


@pytest.mark.parametrize('python_flags,load_first',[(flags,True) for flags in
    ([],['-B'],['-u'],['-B','-u'],['-u','-B'],['-Bu'])]+[(['-u'],False)])
def test_xlsx_native_proof_runs_the_original_snapshotted_helper(api, registered, python_flags, load_first):
    import shlex
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.integrations.hermes_gate import digest_json
    from rpe_harness.skill_catalog import SkillCatalog
    trial = next(t for t in registered['sessions'] if t['case_id']=='xlsx-original' and t['arm']=='native')
    session = Path(registered['root'])/'sessions'/'xlsx-helper-proof'
    catalog = SkillCatalog(trial['db'],registered['catalog_root'])
    materialized = catalog.materialize(session/'home/skills',registered['skill_snapshot_id'])
    source = Path(materialized['materialized_path'])/'productivity/xlsx/SKILL.md'
    helper = source.parent/'scripts/xlsx_read.py'
    workspace = Path(trial['workspace'])
    argv = [registered['helper_python'],*python_flags,str(helper),'input.xlsx','--json','--sheet','明细']
    with (workspace/'output.json').open('w') as output:
        subprocess.run(argv,cwd=workspace,stdout=output,check=True)
    store = HostEventStore(trial['db'])
    common = {'session_id':'xlsx-proof','task_id':'task','turn_id':'turn','api_request_id':'api'}
    load_args = {'name':'xlsx'}
    terminal_args = {'command':shlex.join(argv)+' > output.json'}
    store.record('pre_api_request',common)
    store.record('post_api_request',{**common,'response':{'assistant_message':{'tool_calls':[
        {'id':'load','name':'skill_view'},{'id':'execute','name':'terminal'}]}}})
    load = {**common,'tool_call_id':'load','tool_name':'skill_view','arguments_sha256':digest_json(load_args)}
    load_post = {**load,'status':'loaded','skill_id':'productivity/xlsx',
        'snapshot_id':registered['skill_snapshot_id'],'skill_source_path':str(source),
        'content_sha256':registered['skill_sha256']}
    execute = {**common,'tool_call_id':'execute','tool_name':'terminal','arguments_sha256':digest_json(terminal_args)}
    calls = [(load,load_post),(execute,{**execute,'status':'succeeded','tool_exit_code':0})]
    for pre,post in calls if load_first else reversed(calls):
        store.record('pre_tool_call',pre)
        store.record('post_tool_call',post)
    result = {'session_id':'xlsx-proof','session_root':str(session),'events':store.list('xlsx-proof'),
        'stream_tool_calls':[{'name':'skill_view','tool_call_id':'load','input':load_args},
                             {'name':'terminal','tool_call_id':'execute','input':terminal_args}]}
    assert api.evaluate_native_address_session(result,registered,trial)['verified'] is load_first
    assert api.score_address_artifact(registered,trial)['business_correct'] is True
    assert api.score_address_artifact(registered,trial)['product_oracle_passed'] is True
    # The same correct bytes with an unobserved or substituted helper are insufficient.
    result['stream_tool_calls'][-1]['input'] = {'command':'echo fabricated > output.json'}
    assert api.evaluate_native_address_session(result,registered,trial)['verified'] is False


def test_requested_model_without_response_identity_is_not_a_comparable_pair(api, registered):
    trial = registered['sessions'][0]
    def unobserved_model(*args, **kwargs):
        result = _native_write_result(registered,trial,'output.csv','地区,编号,金额\n华东,001,10\n华西,001,20\n华东,1,30\n')
        result['model_identity']['response_models'] = []
        return result
    summary = api.run_address_comparison(registered['root'],timeout=60,formal=True,max_sessions=1,runner=unobserved_model)
    assert summary['records'][0]['business_correct'] is True
    assert summary['records'][0]['comparable'] is False
    assert summary['production_artifact_successes'] == 0
    assert summary['model_identity_coverage'] == 0


def test_real_intent_missing_fields_reach_contract_score_without_false_failure(api, registered):
    from rpe_harness.capability_catalog import build_catalog
    from rpe_harness.intent import interpret_intent
    trial = next(t for t in registered['sessions'] if t['case_id']=='missing-keys' and t['arm']=='address')
    interpreted = interpret_intent({'goal':'需要业务唯一键','capability':'tabular.deduplicate',
        'constraints':['preserve_original'],'execution':{'input_path':'input.csv','output_path':'output.csv'}},
        catalog=build_catalog(),workspace=trial['workspace'],session_id='actual-intent',source_ref='prompt:public')
    assert interpreted['status']=='needs_context'
    assert interpreted['missing']==['execution.key_columns']
    gate = {'verified':False,'rejection_verified':True,'status':'accepted_rejection',
        'rejection_reason':interpreted['status'],'missing':interpreted['missing'],'errors':[]}
    assert api._address_contract(registered,trial,gate) is True
    gate['missing'] = ['execution.delimiter']
    assert api._address_contract(registered,trial,gate) is False


@pytest.mark.parametrize('after_success',[False,True])
def test_export_never_publishes_failed_or_changed_artifact_bytes(api, registered, tmp_path, after_success):
    import hashlib
    trial = registered['sessions'][0]
    sentinel = b'PRIVATE_TOKEN_SENTINEL /Users/audrey/private\n'
    def returned(*args,**kwargs):
        if after_success:
            return _native_write_result(registered,trial,'output.csv','地区,编号,金额\n华东,001,10\n华西,001,20\n华东,1,30\n')
        (Path(trial['workspace'])/'output.csv').write_bytes(sentinel)
        raise RuntimeError('failed attempt contains private-looking text')
    summary = api.run_address_comparison(registered['root'],timeout=60,formal=True,max_sessions=1,runner=returned)
    assert summary['records'][0]['accepted'] is after_success
    (Path(trial['workspace'])/'output.csv').write_bytes(sentinel)
    destination = tmp_path/'safe-export'
    api.export_address_evidence(registered['root'],destination)
    folder = destination/'artifacts'/trial['trial_id']
    assert not (folder/'output.csv').exists()
    manifest = json.loads((folder/'manifest.json').read_text())
    rejected = next(a for a in manifest['artifacts'] if a['name']=='output.csv')
    assert rejected['sha256']==hashlib.sha256(sentinel).hexdigest()
    assert rejected['size_bytes']==len(sentinel)
    assert rejected['raw_exported'] is False and rejected['reason']
    for file in destination.rglob('*'):
        if file.is_file():
            assert b'PRIVATE_TOKEN_SENTINEL' not in file.read_bytes()
            assert b'/Users/audrey/private' not in file.read_bytes()


def test_duplicate_terminal_keys_cannot_hide_private_bytes_behind_valid_status(api, registered, tmp_path):
    trial = next(t for t in registered['sessions'] if t['case_id']=='missing-keys' and t['arm']=='native')
    terminal = '{"status":"PRIVATE_TOKEN_SENTINEL","status":"needs_context","missing":["key_columns"]}'
    _native_write_result(registered,trial,'terminal.json',terminal)
    assert api.score_address_artifact(registered,trial)['business_correct'] is False


def test_export_rechecks_public_fixture_and_prompt_before_copying_bytes(api, registered, tmp_path):
    trial = registered['sessions'][0]
    (Path(trial['workspace'])/'prompt.txt').write_text('PRIVATE_TOKEN_SENTINEL')
    Path(registered['cases'][0]['source_path']).write_text('PRIVATE_TOKEN_SENTINEL')
    destination = tmp_path/'changed-public-input'
    api.export_address_evidence(registered['root'],destination)
    assert not (destination/'fixtures/csv-zh-composite.csv').exists()
    assert not (destination/'prompts/csv-zh-composite.txt').exists()
    for path in destination.rglob('*'):
        if path.is_file():
            assert b'PRIVATE_TOKEN_SENTINEL' not in path.read_bytes()


@pytest.fixture
def host_checkout(tmp_path):
    """Own Git checkout + tiny stdlib-only venv, with no real Hermes state."""
    import sys
    host = tmp_path/'host-checkout'
    host.mkdir()
    (host/'pm').mkdir()
    (host/'pm/__init__.py').write_text('')
    (host/'pm/environments.py').write_text('def project_python(root):\n    return root / ".venv/bin/python"\n')
    (host/'hermes_cli.py').write_text('VALUE = 1\n')
    (host/'.gitignore').write_text('.venv/\n.env\nmemory/\n.hermes/\n')
    for args in (['init','--quiet'],['add','.'],['-c','user.name=Test','-c','user.email=test@example.invalid','commit','--quiet','-m','fixture']):
        subprocess.run(['git','-C',str(host),*args],check=True,capture_output=True)
    subprocess.run([sys.executable,'-B','-m','venv','--without-pip',str(host/'.venv')],check=True,capture_output=True)
    site = next((host/'.venv/lib').glob('python*/site-packages'))
    dist = site/'host_fixture-1.0.dist-info'
    dist.mkdir()
    (dist/'METADATA').write_text('Metadata-Version: 2.1\nName: Host-Fixture\nVersion: 1.0\n')
    return host


@pytest.mark.parametrize('change',['tracked','untracked_runtime','head','distribution'])
def test_frozen_host_code_and_dependencies_are_rechecked_before_session(api, registered, host_checkout, change):
    plan = api.preregister_address_comparison(Path(registered['root']).parent/'host-comparison',
        skill_root=registered['skill_root'],hermes_root=host_checkout,timeout=60,max_turns=6)
    host = plan['environment']['host_runtime']
    assert host['git_head'] and host['tracked_diff_sha256']
    assert host['bootstrap_python']['installed_distributions_sha256']
    assert host['selected_python']['installed_distributions_sha256']
    if change=='tracked':
        (host_checkout/'hermes_cli.py').write_text('VALUE = 2\n')
    elif change=='untracked_runtime':
        (host_checkout/'extra_adapter.py').write_text('VALUE = 3\n')
    elif change=='head':
        subprocess.run(['git','-C',str(host_checkout),'-c','user.name=Test','-c','user.email=test@example.invalid',
            'commit','--quiet','--allow-empty','-m','new head'],check=True,capture_output=True)
    else:
        metadata = next((host_checkout/'.venv/lib').glob('python*/site-packages/host_fixture-1.0.dist-info/METADATA'))
        metadata.write_text('Metadata-Version: 2.1\nName: Host-Fixture\nVersion: 2.0\n')
    def forbidden(*args,**kwargs):
        pytest.fail('host drift must stop before a paid model attempt')
    with pytest.raises(ValueError,match='frozen'):
        api.run_address_comparison(plan['root'],timeout=60,formal=True,max_sessions=1,runner=forbidden)
    assert api.summarize_address_comparison(plan['root'])['formal_attempts']==0


def test_host_fingerprint_ignores_daily_memory_and_credentials(api, host_checkout):
    before = api._host_fingerprint(host_checkout)
    (host_checkout/'.env').write_text('PRIVATE_TOKEN_SENTINEL')
    (host_checkout/'memory').mkdir()
    (host_checkout/'memory/private.py').write_text('PRIVATE_TOKEN_SENTINEL')
    (host_checkout/'.hermes').mkdir()
    (host_checkout/'.hermes/auth.json').write_text('PRIVATE_TOKEN_SENTINEL')
    assert api._host_fingerprint(host_checkout)==before


def test_summary_separates_real_business_truth_from_workflow_and_contract_probes(api, registered):
    trials = {Path(t['workspace']).name:t for t in registered['sessions']}
    def observed(prompt,workspace,db,**kwargs):
        trial = trials[Path(workspace).name]
        if trial['case_id']=='csv-zh-composite':
            result = _native_write_result(registered,trial,'output.csv','地区,编号,金额\n华东,001,10\n华西,001,20\n华东,1,30\n')
            if trial['arm']=='address':
                # Real correct bytes are still business-correct when there is
                # no address-tool chain. The real address gate must reject it.
                result['events'] = []
            return result
        if trial['arm']=='native' and trial['category']=='contract_probe':
            content = ('{"status":"needs_context","missing":["execution.key_columns"]}'
                if trial['case_id']=='missing-keys' else '{"status":"unsupported"}')
            return _native_write_result(registered,trial,'terminal.json',content)
        raise RuntimeError('other fixed attempts remain failed')
    summary = api.run_address_comparison(registered['root'],timeout=60,formal=True,runner=observed)
    assert summary['production_business_correct']==2
    assert summary['production_complete_chain']==1
    assert summary['production_invocation_verified']==1
    assert summary['contract_probe_correct']==2
    assert summary['contract_probe_complete_chain']==2
    assert summary['by_arm']['address']['production_business_correct']==1
    assert summary['by_arm']['address']['production_complete_chain']==0
    assert summary['by_arm']['native']['production_business_correct']==1
    assert summary['by_arm']['native']['contract_probe_correct']==2
    assert summary['production_artifact_successes']==summary['production_complete_chain']
    assert summary['metric_definitions']['deprecated_aliases']['production_artifact_successes']=='production_complete_chain'
    assert summary['by_arm']['native']['business_correct_observations']==3
    assert 'business_correct_observations' in summary['metric_definitions']['deprecated_aliases']
