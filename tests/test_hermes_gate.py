import hashlib
import json

import pytest

from test_hermes_integration import configured_plugin, integration, request


def strict_plugin(tmp_path, monkeypatch, max_attempts=3):
    monkeypatch.setenv('RPE_HERMES_ENTRY_MODE', 'strict')
    monkeypatch.setenv('RPE_HERMES_ENTRY_KEY', 'trusted-entry')
    monkeypatch.setenv('RPE_HERMES_MAX_TASK_ATTEMPTS', str(max_attempts))
    return configured_plugin(tmp_path, monkeypatch)


def ids(call='call-one', turn='turn-one', api='api-one'):
    return {'session_id':'session-one', 'task_id':'task-one', 'turn_id':turn,
            'tool_call_id':call, 'api_request_id':api}


def api_request(context, identity, choice=None):
    request_body = {'model':'test', 'messages':[{'role':'user','content':'PRIVATE'}],
        'tools':[{'type':'function','function':{'name':'rpe_task','description':'PRIVATE',
                                              'parameters':{'type':'object'}}}]}
    actual = context.middleware['llm_request'](request=request_body, original_request=request_body,
        **identity)['request']
    if choice is not None:
        actual['tool_choice'] = choice
    context.hooks['pre_api_request'](request={'body':actual}, **identity)
    return actual


def execute(context, payload, identity):
    # Parameter tests still travel through the real request policy boundary.
    # No model response/event is invented by this helper.
    rows=integration().HostEventStore(__import__('os').environ['RPE_HERMES_DB']).list(identity['session_id'])
    if not any(e['kind']=='pre_api_request' and e.get('api_request_id')==identity['api_request_id'] for e in rows):
        api_request(context,identity)
    context.hooks['pre_tool_call'](tool_name='rpe_task', args=payload, **identity)
    raw = context.middleware['tool_execution'](tool_name='rpe_task', args=payload,
        next_call=lambda args: context.tools['rpe_task']['handler'](args,
            task_id=identity['task_id'], session_id=identity['session_id']), **identity)
    context.hooks['post_tool_call'](tool_name='rpe_task', args=payload, result=raw,
                                  status='ok', **identity)
    return json.loads(raw)


def response(context, identity, calls):
    context.hooks['post_api_request'](response={'assistant_message':{'tool_calls':[
        {'id':call, 'function':{'name':name, 'arguments':'PRIVATE'}} for call,name in calls]}},
        **identity)


def completed(context, db):
    context.hooks['on_session_end'](session_id='session-one', turn_id='turn-one', completed=True)
    return {'completed':True,'exit_code':0,'timed_out':False,'session_id':'session-one',
        'observer_errors':0,'events':integration().HostEventStore(db).list('session-one'),
        'process_cleanup':{'cleanup_complete':True,'remaining_descendants':[]},
        'final_response':'{"status":"succeeded"}'}


def full_success(tmp_path, monkeypatch):
    context, workspace, db = strict_plugin(tmp_path, monkeypatch)
    payload = request(workspace)
    identity = ids()
    first = api_request(context, identity)
    response(context, identity, [('call-one','rpe_task')])
    result = execute(context, {'request':payload}, identity)
    final_identity = ids(call='', api='api-final')
    final = api_request(context, final_identity)
    response(context, final_identity, [])
    return context, workspace, db, payload, result, completed(context,db), first, final


def test_policy_is_named_until_real_linked_outcome_then_none_and_resets_per_turn(tmp_path,monkeypatch):
    context,workspace,db,payload,result,host,first,final = full_success(tmp_path,monkeypatch)
    assert first['tool_choice'] == {'type':'function','function':{'name':'rpe_task'}}
    assert first['parallel_tool_calls'] is False
    assert final['tool_choice'] == 'none'
    assert api_request(context,ids(turn='turn-two',api='api-two'))['tool_choice']['function']['name']=='rpe_task'


def test_receipt_has_real_context_ids_and_post_receipt_link_without_private_bodies(tmp_path,monkeypatch):
    context,workspace,db,payload,result,host,first,final = full_success(tmp_path,monkeypatch)
    receipt = next(e for e in host['events'] if e['kind']=='rpe_task_received')
    post = next(e for e in host['events'] if e['kind']=='post_tool_call')
    assert all(receipt[key]==value for key,value in ids().items())
    assert receipt['entry_key'] == 'trusted-entry'
    assert receipt['contract_sha256']
    assert post['receipt_id']==result['receipt_id']==receipt['ingest_event_id']
    assert 'PRIVATE' not in json.dumps(host['events'])


def test_flat_business_args_construct_v2_and_stable_entry_id(tmp_path,monkeypatch):
    context,workspace,db = strict_plugin(tmp_path,monkeypatch)
    request(workspace)
    args={'input_path':'orders.csv','output_path':'out.csv','key_columns':['id'],
          'goal':'retain first order','constraints':['preserve_original','preserve_order']}
    first=execute(context,args,ids())
    second=execute(context,args,ids())
    assert first['status']==second['status']=='succeeded'
    assert first['run_id']==second['run_id']
    from rpe_harness.storage import RunStore
    saved=RunStore(db).get(first['run_id'])
    assert saved['event']['schema_version']==2
    assert saved['event']['event_id'].startswith('entry-')


def test_same_strict_contract_replays_original_payload_when_goal_changes(tmp_path,monkeypatch):
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    request(workspace)
    args={'input_path':'orders.csv','output_path':'out.csv','key_columns':['id'],
          'goal':'original goal','constraints':['preserve_original','preserve_order']}
    first=execute(context,args,ids())
    second=execute(context,{**args,'goal':'equivalent paraphrase'},ids())
    assert second['status']=='succeeded',second
    assert second['run_id']==first['run_id']
    from rpe_harness.storage import RunStore
    assert RunStore(db).get(first['run_id'])['event']['goal']=='original goal'
    assert len(RunStore(db).list())==1


def test_semantic_change_is_not_hidden_by_strict_replay(tmp_path,monkeypatch):
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    request(workspace)
    args={'input_path':'orders.csv','output_path':'out.csv','key_columns':['id'],'goal':'original goal'}
    first=execute(context,args,ids())
    second=execute(context,{**args,'key_columns':['value']},ids(call='call-two',api='api-two'))
    assert second['status']=='rejected'
    assert second.get('run_id')!=first['run_id']


def test_required_contract_mismatch_cannot_execute_or_unlock(tmp_path,monkeypatch):
    workspace=tmp_path/'workspace'
    workspace.mkdir()
    payload=request(workspace)
    fixed=tmp_path/'contract.json'
    fixed.write_text(json.dumps(payload))
    monkeypatch.setenv('RPE_HERMES_REQUIRED_REQUEST_PATH',str(fixed))
    context,actual_workspace,db=strict_plugin(tmp_path,monkeypatch)
    bad={'request':{**payload,'execution':{**payload['execution'],'key_columns':['value']}}}
    result=execute(context,bad,ids())
    assert result['error']['code']=='request_contract_mismatch'
    assert not (actual_workspace/'out.csv').exists()
    assert api_request(context,ids(api='api-correction'))['tool_choice']['function']['name']=='rpe_task'


def test_input_change_rejected_against_launcher_anchor(tmp_path,monkeypatch):
    workspace=tmp_path/'nested'/'workspace'
    workspace.mkdir(parents=True)
    payload=request(workspace)
    original=hashlib.sha256((workspace/'orders.csv').read_bytes()).hexdigest()
    contract=tmp_path/'contract.json'
    contract.write_text(json.dumps(payload))
    monkeypatch.setenv('RPE_HERMES_REQUIRED_REQUEST_PATH',str(contract))
    monkeypatch.setenv('RPE_HERMES_INPUT_SHA256',original)
    monkeypatch.setenv('RPE_HERMES_ENTRY_MODE','strict')
    monkeypatch.setenv('RPE_HERMES_ENTRY_KEY','trusted-entry')
    monkeypatch.setenv('RPE_HERMES_DB',str(tmp_path/'db.sqlite'))
    monkeypatch.setenv('RPE_HERMES_WORKSPACE',str(workspace))
    monkeypatch.setenv('RPE_HARNESS_PYTHON',__import__('sys').executable)
    monkeypatch.setenv('RPE_HERMES_TOOL_ENABLED','1')
    from test_hermes_integration import PluginContext
    context=PluginContext()
    integration().register(context)
    (workspace/'orders.csv').write_text('id,value\nx,changed\n')
    result=execute(context,{'request':payload},ids())
    assert result['error']['code']=='input_changed'
    assert not (workspace/'out.csv').exists()


def test_attempt_limit_prevents_unbounded_parameter_corrections(tmp_path,monkeypatch):
    context,workspace,db=strict_plugin(tmp_path,monkeypatch,max_attempts=1)
    request(workspace)
    bad={'input_path':'orders.csv','output_path':'../escape.csv','key_columns':['id'],'goal':'go'}
    execute(context,bad,ids())
    result=execute(context,bad,ids(call='call-two',api='api-two'))
    assert result['error']['code']=='task_attempt_limit'
    assert not (workspace/'out.csv').exists()


def test_gate_accepts_only_linked_plan_bound_actual_artifact(tmp_path,monkeypatch):
    from rpe_harness.integrations.hermes_gate import evaluate_session
    context,workspace,db,payload,result,host,first,final=full_success(tmp_path,monkeypatch)
    verdict=evaluate_session(host,db,workspace,required_request=payload)
    assert verdict['status']=='succeeded',verdict
    assert verdict['verified'] is True
    assert verdict['run_id']==result['run_id']
    assert verdict['plan_sha256']


@pytest.mark.parametrize('fault',['no_receipt','wrong_post','ignored_policy','extra_model_call',
                                 'artifact_changed','incomplete_api','foreign_run','input_changed'])
def test_gate_fails_closed_despite_successful_model_summary(tmp_path,monkeypatch,fault):
    from rpe_harness.integrations.hermes_gate import evaluate_session
    context,workspace,db,payload,result,host,first,final=full_success(tmp_path,monkeypatch)
    if fault=='no_receipt':
        host['events']=[e for e in host['events'] if e['kind']!='rpe_task_received']
    elif fault=='wrong_post':
        next(e for e in host['events'] if e['kind']=='post_tool_call')['receipt_id']='foreign'
    elif fault=='ignored_policy':
        next(e for e in host['events'] if e['kind']=='pre_api_request')['request_tool_choice']='auto'
    elif fault=='extra_model_call':
        next(e for e in host['events'] if e['kind']=='post_api_request')['model_tool_calls'].append(
            {'tool_call_id':'extra','tool_name':'write_file'})
    elif fault=='artifact_changed':
        (workspace/'out.csv').write_text('id,value\na,99\n')
    elif fault=='incomplete_api':
        host['events']=[e for e in host['events'] if e['kind']!='post_api_request']
    elif fault=='foreign_run':
        next(e for e in host['events'] if e['kind']=='post_tool_call')['run_id']='foreign'
    else:
        (workspace/'orders.csv').write_text('id,value\na,10\n')
    verdict=evaluate_session(host,db,workspace,required_request=payload)
    assert not verdict['verified']
    assert verdict['status'] in {'failed','unverified'}
    assert verdict['errors']


def test_model_text_alone_never_grants_success(tmp_path):
    from rpe_harness.integrations.hermes_gate import evaluate_session
    verdict=evaluate_session({'completed':True,'exit_code':0,'events':[],
        'final_response':'{"status":"succeeded"}'},tmp_path/'missing.sqlite',tmp_path)
    assert not verdict['verified']
    assert verdict['status']=='unverified'
    assert not (tmp_path/'missing.sqlite').exists()


def test_durable_json_business_rejection_unlocks_final_without_a_plan(tmp_path,monkeypatch):
    from rpe_harness.integrations.hermes_gate import evaluate_session
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    (workspace/'orders.json').write_text('[{"id":"a"}]')
    identity=ids()
    api_request(context,identity)
    response(context,identity,[('call-one','rpe_task')])
    outcome=execute(context,{'input_path':'orders.json','output_path':'out.json',
        'key_columns':['id'],'goal':'deduplicate'},identity)
    assert outcome['status']=='rejected'
    final_identity=ids(call='',api='api-final')
    assert api_request(context,final_identity)['tool_choice']=='none'
    response(context,final_identity,[])
    host=completed(context,db)
    host['final_response']='FALSE_MODEL_SUCCESS'
    verdict=evaluate_session(host,db,workspace)
    assert verdict['status']=='rejected',verdict
    assert verdict['errors']==[]
    assert not (workspace/'out.json').exists()


def test_ignored_none_is_still_unverified_when_extra_contract_is_blocked_before_execution(tmp_path,monkeypatch):
    from rpe_harness.integrations.hermes_gate import evaluate_session
    from rpe_harness.storage import RunStore
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    (workspace/'orders.json').write_text('[{"id":"a"}]')
    request(workspace)
    first=ids()
    api_request(context,first)
    response(context,first,[('call-one','rpe_task')])
    outcome=execute(context,{'input_path':'orders.json','output_path':'out.json',
        'key_columns':['id'],'goal':'deduplicate'},first)
    assert outcome['status']=='rejected'
    extra=ids(call='forbidden-call',api='api-final')
    assert api_request(context,extra)['tool_choice']=='none'
    response(context,extra,[('forbidden-call','rpe_task')])
    blocked=execute(context,{'input_path':'orders.csv','output_path':'forbidden.csv',
        'key_columns':['id'],'goal':'different contract'},extra)
    assert blocked.get('run_id') is None
    final=ids(call='',api='api-final-text')
    api_request(context,final)
    response(context,final,[])
    verdict=evaluate_session(completed(context,db),db,workspace)
    assert verdict['status']=='unverified'
    assert 'final_policy_ignored' in verdict['errors']
    assert len(RunStore(db).list())==1
    assert not (workspace/'forbidden.csv').exists()


def test_none_before_admission_cannot_be_hidden_by_later_valid_run(tmp_path,monkeypatch):
    from rpe_harness.integrations.hermes_gate import evaluate_session
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    payload=request(workspace)
    early=ids(call='',api='api-early')
    api_request(context,early,choice='none')
    response(context,early,[])
    identity=ids()
    api_request(context,identity)
    response(context,identity,[('call-one','rpe_task')])
    execute(context,{'request':payload},identity)
    final=ids(call='',api='api-final')
    api_request(context,final)
    response(context,final,[])
    verdict=evaluate_session(completed(context,db),db,workspace)
    assert not verdict['verified']
    assert 'policy_phase_invalid' in verdict['errors']


def test_receipt_contract_and_persisted_plan_cannot_disagree(tmp_path,monkeypatch):
    import sqlite3
    from rpe_harness.contracts import ExecutionPlan
    from rpe_harness.integrations.hermes_gate import evaluate_session
    context,workspace,db,payload,result,host,first,final=full_success(tmp_path,monkeypatch)
    # Altering all redundant plan hashes is insufficient: the original request still binds keys.
    with sqlite3.connect(db) as connection:
        row=connection.execute("SELECT id,evidence_json FROM steps WHERE stage='plan'").fetchone()
        evidence=json.loads(row[1])
        evidence['plan']['key_columns']=['value']
        evidence['plan_sha256']=ExecutionPlan.from_dict(evidence['plan']).sha256
        connection.execute('UPDATE steps SET evidence_json=? WHERE id=?',(json.dumps(evidence),row[0]))
        saved=connection.execute('SELECT artifact_json,verification_json FROM runs WHERE run_id=?',
                                 (result['run_id'],)).fetchone()
        values=[json.loads(value) for value in saved]
        for value in values:
            value['plan_sha256']=evidence['plan_sha256']
        connection.execute('UPDATE runs SET artifact_json=?,verification_json=? WHERE run_id=?',
                           (json.dumps(values[0]),json.dumps(values[1]),result['run_id']))
    verdict=evaluate_session(host,db,workspace,required_request=payload)
    assert not verdict['verified']
    assert 'plan_contract_mismatch' in verdict['errors']


def test_strict_handler_without_native_middleware_context_cannot_execute(tmp_path,monkeypatch):
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    payload=request(workspace)
    result=json.loads(context.tools['rpe_task']['handler']({'request':payload},session_id='session-one'))
    assert result['error']['code']=='entry_context_missing'
    assert not (workspace/'out.csv').exists()


def test_mismatched_post_api_context_cannot_unlock_next_request(tmp_path,monkeypatch):
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    payload=request(workspace)
    execute(context,{'request':payload},ids())
    # A post event for another API cannot establish the real receipt's outcome.
    rows=integration().HostEventStore(db).list('session-one')
    post=next(e for e in rows if e['kind']=='post_tool_call')
    import sqlite3
    with sqlite3.connect(db) as connection:
        raw=connection.execute('SELECT metadata_json FROM hermes_host_events WHERE ingest_event_id=?',
                               (post['ingest_event_id'],)).fetchone()[0]
        metadata=json.loads(raw)
        metadata['api_request_id']='foreign-api'
        connection.execute('UPDATE hermes_host_events SET metadata_json=? WHERE ingest_event_id=?',
                           (json.dumps(metadata),post['ingest_event_id']))
    assert api_request(context,ids(api='api-next'))['tool_choice']['function']['name']=='rpe_task'


def test_previous_turn_success_cannot_mask_missing_entry_on_next_turn(tmp_path,monkeypatch):
    from rpe_harness.integrations.hermes_gate import evaluate_session
    context,workspace,db,payload,result,host,first,final=full_success(tmp_path,monkeypatch)
    next_turn=ids(call='',turn='turn-two',api='api-two')
    api_request(context,next_turn,choice='none')
    response(context,next_turn,[])
    verdict=evaluate_session(completed(context,db),db,workspace)
    assert not verdict['verified']
    assert 'policy_phase_invalid' in verdict['errors']


@pytest.mark.parametrize('unsafe',[False,True])
def test_xlsx_default_sheet_and_durable_formula_rejection(tmp_path,monkeypatch,unsafe):
    from openpyxl import Workbook
    from rpe_harness.integrations.hermes_gate import evaluate_session
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    book=Workbook()
    sheet=book.active
    sheet.title='订单'
    sheet.append(['id','value','formula'])
    sheet.append(['a',10,'=B2*2'])
    sheet.append(['a',20,'=B3*2'])
    sheet.append(['b',30,'=B3' if unsafe else '=B4*2'])
    book.save(workspace/'orders.xlsx')
    book.close()
    identity=ids()
    api_request(context,identity)
    response(context,identity,[('call-one','rpe_task')])
    execute(context,{'input_path':'orders.xlsx','output_path':'out.xlsx','key_columns':['id'],
        'goal':'keep first rows','constraints':['preserve_formulas','preserve_order']},identity)
    final=ids(call='',api='api-final')
    assert api_request(context,final)['tool_choice']=='none'
    response(context,final,[])
    verdict=evaluate_session(completed(context,db),db,workspace)
    assert verdict['errors']==[],verdict
    assert verdict['status']==('rejected' if unsafe else 'succeeded')
    assert verdict['verified'] is (not unsafe)
