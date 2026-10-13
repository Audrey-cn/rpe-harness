"""Independent loopback tests: real CLI/candidate processes, no model requests."""
import contextlib
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
from urllib.parse import urlsplit

import pytest

REPO = Path('/Users/audrey/项目/rpe-harness')
sys.path.insert(0, str(REPO/'tests'))
from test_candidates import candidate, GOOD_CODE
from probe_support import outer_run, task
from rpe_harness.evolution import EvolutionStore, apply_candidate, validate_candidate
from rpe_harness.integrations.hermes import HostEventStore, _prepare_request
from rpe_harness.integrations.hermes_gate import contract_for, digest_json, request_input_sha

ROWS = []
REPORT = Path(__file__).parent/'broker-probe-detail.json'


@pytest.fixture(autouse=True)
def save_evidence():
    yield
    REPORT.write_text(json.dumps({'no_models': True, 'daily_writes': False,
                                  'probes': ROWS}, ensure_ascii=False, indent=2))


def broker_api():
    from rpe_harness.integrations.hermes_broker import execution_broker
    return execution_broker


def profile(root,db,workspace):
    from rpe_harness.integrations.hermes_session import sandbox_profile
    workspace.mkdir(exist_ok=True)
    session_root=root/'session';session_root.mkdir()
    policy=sandbox_profile([session_root,workspace],root/'daily-sentinel',database=db,
        owner_read_scope=(root,[session_root,workspace,db,REPO/'src',
            Path(sys.executable).absolute().parent.parent]))
    return policy,[],[]


def runs(db):
    with sqlite3.connect(db) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone():
            return 0
        return c.execute('SELECT count(*) FROM runs').fetchone()[0]


def admitted(root, *, strict=False, named=True):
    workspace=root/'workspace'
    workspace.mkdir(parents=True)
    request=task(workspace)
    native={'session_id':'independent-session', 'task_id':'independent-task',
            'turn_id':'independent-turn', 'api_request_id':'independent-api',
            'tool_call_id':'independent-call', 'tool_name':'rpe_task'}
    payload=_prepare_request({'request':request},workspace,native['session_id'])
    native.update(contract_sha256=digest_json(contract_for(payload,workspace)),
                  input_sha256=request_input_sha(payload,workspace))
    db=root/'events.sqlite'
    store=HostEventStore(db)
    if strict:
        native['entry_key']='independent-strict-entry'
        payload['event_id']='entry-'+digest_json([native['entry_key'],native['contract_sha256'],native['input_sha256']])
        body={'tool_choice':{'type':'function','function':{'name':'rpe_task'}} if named else 'none',
              'parallel_tool_calls':False,'tools':[{'type':'function','function':{'name':'rpe_task'}}]}
        store.record('pre_api_request',{**native,'request':{'body':body}})
        store.record('post_api_request',{**native,'response':{'assistant_message':{
            'tool_calls':[{'id':native['tool_call_id'],'name':'rpe_task'}]}}})
        store.record('pre_tool_call',native)
    receipt=store.record('rpe_task_received',native)
    return workspace,db,{'payload':payload,'receipt_id':receipt['ingest_event_id'],'native':native}


def post(env, body, *, auth=True, route='/task', method='POST', headers=None, headers_only=False):
    address=urlsplit(env['RPE_HERMES_BROKER_URL'])
    assert address.scheme=='http' and address.hostname=='127.0.0.1' and address.port
    encoded=body if isinstance(body,bytes) else json.dumps(body,allow_nan=False).encode()
    options={'Content-Type':'application/json'}
    if auth: options['Authorization']='Bearer '+env['RPE_HERMES_BROKER_TOKEN']
    options.update(headers or {})
    connection=http.client.HTTPConnection('127.0.0.1',address.port,timeout=8)
    try:
        if headers_only:
            connection.putrequest(method,route)
            for name,value in options.items(): connection.putheader(name,value)
            connection.putheader('Content-Length',str(len(encoded)))
            connection.endheaders()
        else:
            connection.request(method,route,body=encoded,headers=options)
        response=connection.getresponse()
        raw=response.read(1_048_577)
        result=json.loads(raw)
        return response.status,result
    finally:
        connection.close()


def test_strict_receipt_cannot_change_event_id_to_create_fresh_run(tmp_path):
    workspace,db,body=admitted(tmp_path,strict=True)
    body['payload']['event_id']='unbound-fresh-event'
    with broker_api()(db_path=db,workspace=workspace,python=sys.executable) as env:
        status,answer=post(env,body)
    ROWS.append({'probe':'strict_event_id_binding','status':status,'answer':answer})
    assert status==403,answer
    assert runs(db)==0 and not (workspace/'result-new.csv').exists()


def test_receipt_duplicate_is_readonly_and_mutated_replay_is_rejected(tmp_path):
    workspace,db,body=admitted(tmp_path,strict=True)
    with broker_api()(db_path=db,workspace=workspace,python=sys.executable) as env:
        first_status,first=post(env,body)
        assert first_status==200,first
        old=first['result']['run_id']
        second_status,second=post(env,body)
        assert second_status==200 and second['result']['run_id']==old,second
        body['payload']['event_id']='changed-replay-event'
        third_status,third=post(env,body)
    ROWS.append({'probe':'receipt_replay_binding','statuses':[first_status,second_status,third_status]})
    assert third_status in (403,409),third
    assert runs(db)==1


@pytest.mark.parametrize('kind',['none','missing_policy','unlinked_receipt','outside_path'])
def test_no_execution_for_unbound_or_closed_entry(tmp_path,kind):
    workspace,db,body=admitted(tmp_path,strict=(kind!='missing_policy'),named=(kind!='none'))
    if kind=='missing_policy':
        body['native']['entry_key']='missing-proof'
        receipt=HostEventStore(db).record('rpe_task_received',body['native'])
        body['receipt_id']=receipt['ingest_event_id']
    elif kind=='unlinked_receipt': body['native']['tool_call_id']='forged-new-call'
    elif kind=='outside_path': body['payload']['execution']['output_path']=str(tmp_path/'outside.csv')
    with broker_api()(db_path=db,workspace=workspace,python=sys.executable) as env:
        status,answer=post(env,body)
    ROWS.append({'probe':'no_execution_'+kind,'status':status,'answer':answer})
    assert status in (403,409),answer
    assert runs(db)==0
    assert not (workspace/'result-new.csv').exists()


def test_domain_rejection_retains_proof_and_preserves_existing_output(tmp_path):
    workspace,db,body=admitted(tmp_path,strict=True)
    (workspace/'result-new.csv').write_text('existing owned output')
    with broker_api()(db_path=db,workspace=workspace,python=sys.executable) as env:
        status,answer=post(env,body)
    ROWS.append({'probe':'domain_rejection_no_overwrite','status':status,
                 'result_status':answer.get('result',{}).get('status')})
    assert status==200 and answer['result']['status']=='rejected',answer
    assert answer['result']['run_id'] and runs(db)==1
    assert (workspace/'result-new.csv').read_text()=='existing owned output'


@pytest.mark.parametrize('kind',['auth_missing','auth_nonascii','get','route','transfer_encoding','large','bad_json'])
def test_http_failure_is_bounded_and_server_stays_alive(tmp_path,kind):
    workspace,db,body=admitted(tmp_path)
    options={}
    if kind=='auth_missing': options['auth']=False
    elif kind=='auth_nonascii': options['headers']={'Authorization':'Bearer é'}
    elif kind=='get': options['method']='GET'
    elif kind=='route': options['route']='/task?extra=true'
    elif kind=='transfer_encoding': options['headers']={'Transfer-Encoding':'chunked'}
    elif kind=='large': body=b'x'*1_048_577;options['headers_only']=True
    elif kind=='bad_json': body=b'{'
    with broker_api()(db_path=db,workspace=workspace,python=sys.executable) as env:
        status,answer=post(env,body,**options)
        live_status,live=post(env,{},auth=False)
    ROWS.append({'probe':'http_'+kind,'status':status,'alive_status':live_status})
    assert status>=400 and live_status==401,answer
    assert runs(db)==0


def bridge_program():
    return '''import sys,os,json
sys.path.insert(0,os.environ['RPE_HARNESS_IMPORT_ROOT'])
from rpe_harness.integrations.hermes import register
from pathlib import Path
try: Path(os.environ['INDEPENDENT_DENIED_ARTIFACT']).read_bytes()
except PermissionError: pass
else: raise AssertionError('host unnecessarily gained candidate artifact access')
class Context:
    def __init__(self): self.tools={};self.hooks={};self.middleware={}
    def register_tool(self,**kw): self.tools[kw['name']]=kw
    def register_hook(self,name,callback): self.hooks[name]=callback
    def register_middleware(self,name,callback): self.middleware[name]=callback
c=Context();register(c)
who=dict(session_id='independent-session',task_id='independent-task',turn_id='independent-turn',api_request_id='independent-api',tool_call_id='independent-call')
body=dict(tools=[dict(type='function',function=c.tools['rpe_task']['schema'])])
body=c.middleware['llm_request'](request=body,**who)['request']
c.hooks['pre_api_request'](request={'body':body},**who)
c.hooks['post_api_request'](response={'assistant_message':{'tool_calls':[{'id':who['tool_call_id'],'name':'rpe_task'}]}},**who)
c.hooks['pre_tool_call'](tool_name='rpe_task',**who)
raw=c.middleware['tool_execution'](tool_name='rpe_task',args={'request':json.loads(os.environ['INDEPENDENT_REQUEST'])},next_call=lambda a:c.tools['rpe_task']['handler'](a,task_id=who['task_id'],session_id=who['session_id']),**who)
c.hooks['post_tool_call'](tool_name='rpe_task',result=raw,status='ok',**who)
print(raw)
'''


@pytest.mark.parametrize('sleep',[False,True])
def test_actual_outer_host_broker_uses_candidate_or_true_timeout(tmp_path,sleep,monkeypatch):
    content=GOOD_CODE
    if sleep:
        content=content.replace('def prepare(plan, staging_path):',
            'def prepare(plan, staging_path):\n'
            '    if plan["output_path"].endswith("production-sleep.csv"):\n'
            '        import time\n        time.sleep(60)')
    state=tmp_path/'persistent'
    db,proposal=candidate(state,content)
    assert validate_candidate(proposal['proposal_id'],db)['passed']
    version=apply_candidate(proposal['proposal_id'],db)
    workspace=tmp_path/'workspace'
    policy,_,_=profile(tmp_path,db,workspace)
    request=task(workspace,'production-sleep.csv' if sleep else 'result-new.csv')
    from probe_support import session
    extra={'RPE_HERMES_DB':str(db),'RPE_HERMES_WORKSPACE':str(workspace),
        'RPE_HARNESS_PYTHON':sys.executable,'RPE_HARNESS_IMPORT_ROOT':str(REPO/'src'),
        'RPE_HERMES_TOOL_ENABLED':'1','RPE_HERMES_ENTRY_MODE':'strict',
        'RPE_HERMES_ENTRY_KEY':'independent-strict-entry','INDEPENDENT_REQUEST':json.dumps(request),
        'INDEPENDENT_DENIED_ARTIFACT':proposal['artifact_path']}
    # outer_run restricts environment; supply only this owned broker capability.
    original=session.subprocess.Popen
    cli_tempdirs=[]
    def launch(argv,*a,**kw):
        if argv and argv[0]=='/usr/bin/sandbox-exec' and '-c' in argv: kw['env'].update(extra)
        if '-m' in argv and 'rpe_harness' in argv:
            temporary=kw['env'].get('TMPDIR')
            assert temporary and Path(temporary).is_relative_to(tmp_path)
            assert kw['env'].get('TMP')==temporary and kw['env'].get('TEMP')==temporary
            assert Path(temporary).is_dir()
            cli_tempdirs.append(temporary)
        return original(argv,*a,**kw)
    with broker_api()(db_path=db,workspace=workspace,python=sys.executable) as broker_env:
        extra.update(broker_env)
        with monkeypatch.context() as scoped:
            scoped.setattr(session.subprocess,'Popen',launch)
            result=outer_run(policy,bridge_program(),workspace)
    assert result['exit_code']==0,result
    answer=json.loads(result['stdout'])
    assert answer['status']=='succeeded',answer
    # The durable public answer carries actual executor identity in artifact.
    from rpe_harness.integrations.hermes_gate import read_run
    durable=read_run(db,answer['run_id'])
    executor=durable['artifact']['actual_executor']
    state=EvolutionStore(db).get_version(version['version_id'])
    if sleep:
        assert executor['kind']=='builtin',executor
        assert state['state']=='quarantine' and 'timeout' in state['reason'].lower(),state
    else:
        assert executor['kind']=='candidate',executor
        assert executor['candidate_version_id']==version['version_id']
        assert executor['code_sha256']==proposal['content_hash']
        assert state['state']=='active',state
    assert not result['cleanup_intervention_pids'],result
    assert cli_tempdirs and all(not Path(p).exists() for p in cli_tempdirs)
    assert Path(request['execution']['output_path']).read_text(encoding='utf-8-sig')=='id,value\na,first\nb,second\n'
    ROWS.append({'probe':'actual_nested_broker_timeout' if sleep else 'actual_nested_broker_candidate',
                 'executor':executor,'candidate_state':state['state'],
                 'quarantine_reason':state.get('reason'),'exit_code':result['exit_code'],
                 'elapsed_s':result['elapsed_s'],'cleanup_intervention_pids':result['cleanup_intervention_pids']})
    ROWS[-1]['owned_cli_tempdirs_removed']=True


@pytest.mark.parametrize('capture_error',[False,True])
def test_context_exit_stops_candidate_in_separate_process_group(tmp_path,monkeypatch,capture_error):
    from rpe_harness.integrations import hermes_session as session
    from rpe_harness.integrations import hermes_broker as module
    code=GOOD_CODE.replace('def prepare(plan, staging_path):',
        'def prepare(plan, staging_path):\n'
        '    if plan["output_path"].endswith("production-sleep.csv"):\n'
        '        open(staging_path,"w").write("owned-candidate-running")\n'
        '        import time\n        time.sleep(60)')
    state=tmp_path/'persistent'
    db,proposal=candidate(state,code)
    assert validate_candidate(proposal['proposal_id'],db)['passed']
    apply_candidate(proposal['proposal_id'],db)
    workspace=tmp_path/'workspace';workspace.mkdir()
    payload=_prepare_request({'request':task(workspace,'production-sleep.csv')},workspace,'independent-session')
    native={'session_id':'independent-session','task_id':'independent-task','turn_id':'independent-turn',
            'api_request_id':'independent-api','tool_call_id':'independent-call','tool_name':'rpe_task',
            'contract_sha256':digest_json(contract_for(payload,workspace)),
            'input_sha256':request_input_sha(payload,workspace)}
    receipt=HostEventStore(db).record('rpe_task_received',native)
    body={'payload':payload,'receipt_id':receipt['ingest_event_id'],'native':native}
    processes=[]
    original=module.subprocess.Popen
    def launch(argv,*a,**kw):
        process=original(argv,*a,**kw)
        if '-m' in argv and 'rpe_harness' in argv: processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    capture_original=session._capture_descendants
    fault=threading.Event();capture_errors=[];broker_maps=[]
    def capture(pid,identity,owned_map):
        if owned_map not in broker_maps: broker_maps.append(owned_map)
        if fault.is_set():
            capture_errors.append('controlled_partial_capture_error')
            raise OSError('owned test cannot refresh the already-known process tree')
        return capture_original(pid,identity,owned_map)
    if capture_error: monkeypatch.setattr(session,'_capture_descendants',capture)
    response=[];errors=[];owned={};remaining=[]
    try:
        with broker_api()(db_path=db,workspace=workspace,python=sys.executable) as env:
            def request_worker():
                try: response.append(post(env,body))
                except Exception as error: errors.append(type(error).__name__)
            thread=threading.Thread(target=request_worker)
            thread.start()
            deadline=time.monotonic()+3
            marker=False
            while time.monotonic()<deadline:
                marker=any(p.is_file() and p.read_text()=='owned-candidate-running'
                           for p in (state/'evolution-sandboxes').glob('csv-*/staging.csv'))
                for process in processes:
                    ident=session._process_table().get(process.pid,(None,None))[1]
                    if ident:
                        owned[process.pid]=ident
                        capture_original(process.pid,ident,owned)
                if marker: break
                time.sleep(.05)
            assert marker,'actual candidate did not reach production sleep'
            assert len(owned)>=2,'CLI and its separately grouped candidate must both be observed'
            if capture_error:
                # Let the normal broker monitor record the candidate before faulting its refresh.
                deadline=time.monotonic()+1
                while not any(m for m in broker_maps) and time.monotonic()<deadline: time.sleep(.03)
                assert any(m for m in broker_maps),'broker must have a known detached child'
                fault.set()
            exited_started=time.monotonic()
        exited_elapsed=time.monotonic()-exited_started
        thread.join(timeout=3)
        remaining=session._remaining_descendants(owned)
        ROWS.append({'probe':'context_cancellation_partial_capture_error' if capture_error else 'context_cancellation_separate_candidate_group',
            'observed_pids':sorted(owned),'remaining_after_context':remaining,
            'context_exit_seconds':exited_elapsed,'response_statuses':[x[0] for x in response],
            'connection_errors':errors,'external_cleanup':env.cleanup})
        assert not thread.is_alive() and not remaining
        if capture_error:
            assert capture_errors and env.cleanup['cleanup_complete'] is False
            assert env.cleanup['errors'],env.cleanup
        else: assert env.cleanup['cleanup_complete'] is True,env.cleanup
        assert not (workspace/'production-sleep.csv').exists()
    finally:
        session._stop_descendants(owned)
        assert not session._remaining_descendants(owned),'owned test cleanup failed'


def test_half_open_loopback_connection_cannot_block_context_shutdown(tmp_path):
    workspace,db,_=admitted(tmp_path)
    sock=None
    before={t.ident for t in threading.enumerate()}
    try:
        with broker_api()(db_path=db,workspace=workspace,python=sys.executable) as env:
            address=urlsplit(env['RPE_HERMES_BROKER_URL'])
            sock=socket.create_connection(('127.0.0.1',address.port),timeout=2)
            time.sleep(.1)
            started=time.monotonic()
        elapsed=time.monotonic()-started
        lingering=[t.name for t in threading.enumerate() if t.ident not in before]
        ROWS.append({'probe':'half_open_shutdown_bounded','elapsed_s':elapsed,
                     'lingering_threads':lingering,'cleanup':env.cleanup})
        assert elapsed<4.5 and not lingering
        assert env.cleanup['cleanup_complete'] and runs(db)==0
    finally:
        if sock: sock.close()


def test_slowdrip_partial_request_is_actively_closed_on_context_exit(tmp_path):
    workspace,db,_=admitted(tmp_path)
    context=broker_api()(db_path=db,workspace=workspace,python=sys.executable)
    env=context.__enter__()
    address=urlsplit(env['RPE_HERMES_BROKER_URL'])
    sock=socket.create_connection(('127.0.0.1',address.port),timeout=2)
    stopping=threading.Event();errors=[]
    def drip():
        try:
            while not stopping.is_set():
                sock.sendall(b'X');stopping.wait(.1)
        except OSError: pass
    def close():
        try: context.__exit__(None,None,None)
        except Exception as error: errors.append(type(error).__name__)
    sender=threading.Thread(target=drip);sender.start();time.sleep(.1)
    started=time.monotonic()
    closer=threading.Thread(target=close);closer.start()
    try:
        closer.join(1)
        finished=not closer.is_alive()
        ROWS.append({'probe':'slowdrip_actively_cancelled','context_completed_within_1s':finished,
                     'elapsed_s':time.monotonic()-started,'errors':errors})
        assert finished and not errors
        assert env.cleanup['cleanup_complete'] and runs(db)==0
    finally:
        stopping.set()
        try: sock.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        sock.close();sender.join(1);closer.join(4)
        assert not sender.is_alive() and not closer.is_alive(),'owned socket/thread cleanup failed'
