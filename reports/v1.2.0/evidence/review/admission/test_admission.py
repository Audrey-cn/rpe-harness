import hashlib, json, sqlite3, sys
from pathlib import Path
import pytest
from rpe_harness.integrations import hermes as h
from rpe_harness.integrations.hermes_gate import evaluate_session

class Context:
    def __init__(self): self.tools={}; self.hooks={}; self.middleware={}
    def register_tool(self,**kwargs): self.tools[kwargs['name']]=kwargs
    def register_hook(self,name,callback): self.hooks[name]=callback
    def register_middleware(self,name,callback): self.middleware[name]=callback

def setup(root,monkeypatch):
    root.mkdir(parents=True,exist_ok=True)
    workspace=root/'workspace';workspace.mkdir()
    (workspace/'source.csv').write_text('id,value\n001,first\n001,later\n002,second\n',encoding='utf-8-sig')
    db=root/'events.sqlite'
    for key,value in {'RPE_HERMES_DB':db,'RPE_HERMES_WORKSPACE':workspace,
        'RPE_HARNESS_PYTHON':sys.executable,'RPE_HERMES_TOOL_ENABLED':'1',
        'RPE_HERMES_ENTRY_MODE':'strict','RPE_HERMES_ENTRY_KEY':'review-admission-entry',
        'RPE_HERMES_MAX_TASK_ATTEMPTS':'20'}.items(): monkeypatch.setenv(key,str(value))
    monkeypatch.delenv('RPE_HERMES_REQUIRED_REQUEST_PATH',raising=False)
    monkeypatch.delenv('RPE_HERMES_INPUT_SHA256',raising=False)
    context=Context();h.register(context)
    return context,workspace,db

def identity(api='api-one',call='call-one',turn='turn-one'):
    return dict(session_id='review-session',task_id='review-task',turn_id=turn,api_request_id=api,tool_call_id=call)

def args(output='out.csv'):
    return dict(input_path='source.csv',output_path=output,key_columns=['id'],goal='retain first',constraints=['preserve_original','preserve_order'])

def api(context,who,*,rewrite=True,persist=True,change=None):
    body={'tools':[{'type':'function','function':context.tools['rpe_task']['schema']}],'tool_choice':{'type':'function','function':{'name':'rpe_task'}},'parallel_tool_calls':False}
    if rewrite: body=context.middleware['llm_request'](request=body,**who)['request']
    if change: body.update(change)
    if persist: context.hooks['pre_api_request'](request={'body':body},**who)
    return body

def response(context,who,calls):
    context.hooks['post_api_request'](response={'assistant_message':{'tool_calls':[{'id':call,'function':{'name':'rpe_task','arguments':'{}'}} for call in calls]}},**who)

def call(context,who,arguments):
    context.hooks['pre_tool_call'](tool_name='rpe_task',**who)
    raw=context.middleware['tool_execution'](tool_name='rpe_task',args=arguments,
        next_call=lambda a:context.tools['rpe_task']['handler'](a,task_id=who['task_id'],session_id=who['session_id']),**who)
    context.hooks['post_tool_call'](tool_name='rpe_task',result=raw,status='ok',**who)
    return json.loads(raw)

def runs(db):
    with sqlite3.connect(db) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone(): return 0
        return connection.execute('SELECT COUNT(*) FROM runs').fetchone()[0]

@pytest.fixture
def launches(monkeypatch):
    actual=h.subprocess.run
    launched=[]
    def count(argv,*a,**kw):
        if '-m' in argv and 'rpe_harness' in argv: launched.append(argv)
        return actual(argv,*a,**kw)
    monkeypatch.setattr(h.subprocess,'run',count)
    return launched

def initial(context,workspace,db):
    who=identity();api(context,who);response(context,who,[who['tool_call_id']])
    result=call(context,who,args())
    assert result['status']=='succeeded' and runs(db)==1
    return who,result

def test_actual_persisted_none_overrides_named_rewrite_cache(tmp_path,monkeypatch,launches):
    context,workspace,db=setup(tmp_path,monkeypatch)
    who=identity()
    body=api(context,who,change={'tool_choice':'none'})
    assert body['tool_choice']=='none'
    result=call(context,who,args())
    assert result['error']['code']=='entry_policy_none'
    assert not launches and runs(db)==0 and not (workspace/'out.csv').exists()

def test_incomplete_strict_policy_fails_before_any_child_or_run(tmp_path,monkeypatch,launches):
    for label,options in [('no_persisted_policy',dict(persist=False)),('no_rewrite_cache',dict(rewrite=False)),('parallel_not_false',dict(change={'parallel_tool_calls':True})),('wrong_tools',dict(change={'tools':[]}))]:
        context,workspace,db=setup(tmp_path/label,monkeypatch)
        who=identity();api(context,who,**options)
        result=call(context,who,args())
        assert result['error']['code']=='entry_policy_missing',result
        assert runs(db)==0 and not (workspace/'out.csv').exists()
    assert not launches

def test_terminal_turn_blocks_changed_output_with_new_call_even_stale_named(tmp_path,monkeypatch,launches):
    context,workspace,db=setup(tmp_path,monkeypatch)
    who=identity();stale=identity(api='api-stale-named',call='new-call')
    api(context,who);api(context,stale)
    response(context,who,[who['tool_call_id']])
    first=call(context,who,args())
    assert first['status']=='succeeded'
    second=call(context,stale,args('extra.csv'))
    assert second['error']['code']=='entry_closed'
    assert len(launches)==1 and runs(db)==1 and not (workspace/'extra.csv').exists()

def test_exact_native_named_call_replays_persisted_run_without_child(tmp_path,monkeypatch,launches):
    context,workspace,db=setup(tmp_path,monkeypatch)
    who,first=initial(context,workspace,db)
    source_hash=hashlib.sha256((workspace/'source.csv').read_bytes()).hexdigest()
    output_hash=hashlib.sha256((workspace/'out.csv').read_bytes()).hexdigest()
    second=call(context,who,{**args(),'goal':'equivalent wording'})
    assert second['run_id']==first['run_id'] and second['status']=='succeeded'
    assert first['input_sha256']==second['input_sha256']==source_hash
    assert len(launches)==1 and runs(db)==1
    assert hashlib.sha256((workspace/'out.csv').read_bytes()).hexdigest()==output_hash

def test_new_turn_named_policy_can_admit_new_business_contract(tmp_path,monkeypatch,launches):
    context,workspace,db=setup(tmp_path,monkeypatch)
    _,first=initial(context,workspace,db)
    who=identity(api='api-new-turn',call='call-new-turn',turn='turn-two')
    body=api(context,who)
    assert body['tool_choice']['function']['name']=='rpe_task'
    response(context,who,[who['tool_call_id']])
    second=call(context,who,args('turn-two.csv'))
    assert second['status']=='succeeded' and second['run_id']!=first['run_id']
    assert len(launches)==2 and runs(db)==2 and (workspace/'turn-two.csv').is_file()

def test_provider_ignored_none_still_fails_final_acceptance(tmp_path,monkeypatch,launches):
    context,workspace,db=setup(tmp_path,monkeypatch)
    _,first=initial(context,workspace,db)
    who=identity(api='api-none',call='provider-extra-call')
    assert api(context,who)['tool_choice']=='none'
    response(context,who,[who['tool_call_id']])
    rejected=call(context,who,args('unwanted.csv'))
    assert rejected['error']['code']=='entry_policy_none'
    context.hooks['on_session_end'](session_id='review-session',turn_id='turn-one',completed=True)
    host=dict(completed=True,exit_code=0,timed_out=False,session_id='review-session',observer_errors=0,
        events=h.HostEventStore(db).list('review-session'),process_cleanup={'cleanup_complete':True},final_response='claimed success')
    verdict=evaluate_session(host,db,workspace)
    assert verdict['verified'] is False and verdict['status']=='unverified'
    assert 'final_policy_ignored' in verdict['errors']
    assert len(launches)==1 and runs(db)==1 and not (workspace/'unwanted.csv').exists()

def test_changed_source_cannot_reinterpret_same_old_named_native_call(tmp_path,monkeypatch,launches):
    context,workspace,db=setup(tmp_path,monkeypatch)
    who,first=initial(context,workspace,db)
    output_hash=hashlib.sha256((workspace/'out.csv').read_bytes()).hexdigest()
    (workspace/'source.csv').write_text('id,value\n001,changed\n002,new\n',encoding='utf-8-sig')
    rejected=call(context,who,args())
    assert rejected['status']=='rejected'
    assert len(launches)==1 and runs(db)==1, {'launches':len(launches),'runs':runs(db),'result':rejected}
    assert hashlib.sha256((workspace/'out.csv').read_bytes()).hexdigest()==output_hash

@pytest.mark.parametrize('change',['corrupt','remove'])
def test_artifact_damage_does_not_reopen_old_named_call(tmp_path,monkeypatch,launches,change):
    context,workspace,db=setup(tmp_path,monkeypatch)
    who,first=initial(context,workspace,db)
    output=workspace/'out.csv'
    if change=='remove': output.unlink()
    else: output.write_text('corrupted output')
    rejected=call(context,who,args())
    assert rejected['status']=='rejected'
    assert len(launches)==1 and runs(db)==1, {'launches':len(launches),'runs':runs(db),'result':rejected}
    if change=='remove': assert not output.exists()
    else: assert output.read_text()=='corrupted output'
