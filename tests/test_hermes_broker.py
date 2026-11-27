import importlib
import json
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


def broker_api():
    name='rpe_harness.integrations.hermes_broker'
    assert importlib.util.find_spec(name) is not None,'external execution broker is required'
    return importlib.import_module(name)


@pytest.fixture
def admitted_task(tmp_path):
    from rpe_harness.integrations.hermes import HostEventStore,_prepare_request
    from rpe_harness.integrations.hermes_gate import contract_for,digest_json,request_input_sha
    workspace=tmp_path/'business-workspace'
    workspace.mkdir()
    source=workspace/'source.csv'
    source.write_text('id,value\na,first\na,later\nb,second\n')
    request={'schema_version':2,'event_id':'broker-real-cli','capability':'tabular.deduplicate',
        'goal':'retain first','constraints':['preserve_original','preserve_order'],
        'execution':{'input_path':str(source),'output_path':str(workspace/'result.csv'),'key_columns':['id']}}
    native={key:key+'-native' for key in ('session_id','task_id','turn_id','api_request_id','tool_call_id')}
    payload=_prepare_request({'request':request},workspace,native['session_id'])
    native.update(tool_name='rpe_task',contract_sha256=digest_json(contract_for(payload,workspace)),
                  input_sha256=request_input_sha(payload,workspace))
    db=tmp_path/'state/runs.sqlite'
    receipt=HostEventStore(db).record('rpe_task_received',native)
    return workspace,db,{'payload':payload,'receipt_id':receipt['ingest_event_id'],'native':native}


def post(env,body,*,token=None,path=None,content_type='application/json',headers=None):
    encoded=body if isinstance(body,bytes) else json.dumps(body).encode()
    request=Request(path or env['RPE_HERMES_BROKER_URL'],data=encoded,method='POST',headers={
        'Content-Type':content_type,'Authorization':'Bearer '+(token or env['RPE_HERMES_BROKER_TOKEN']),
        **(headers or {})})
    try:
        response=urlopen(request,timeout=10)
    except HTTPError as error:
        response=error
    with response:
        return response.status,json.load(response)


def test_broker_executes_fixed_cli_from_durable_receipt(admitted_task):
    workspace,db,body=admitted_task
    module=broker_api()
    with module.execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        assert env['RPE_HERMES_BROKER_URL'].startswith('http://127.0.0.1:')
        assert len(env['RPE_HERMES_BROKER_TOKEN'])>=32
        status,answer=post(env,body)
    assert status==200,answer
    assert answer['returncode']==0 and answer['result']['status']=='succeeded'
    assert answer['result']['verification']['passed'] is True
    assert (workspace/'result.csv').read_text(encoding='utf-8-sig')=='id,value\na,first\nb,second\n'


@pytest.mark.parametrize('damage,expected',[
    ('auth',401),('auth_nonascii',401),('route',404),('content_type',415),('oversized',413),
    ('malformed',400),('extra_field',400),('wrong_receipt',403),('wrong_native',403),
    ('wrong_contract',403),('changed_input',403),('escape_path',403),('symlink_path',403),
])
def test_broker_rejects_untrusted_or_unbound_requests_before_execution(admitted_task,tmp_path,damage,expected):
    workspace,db,body=admitted_task
    module=broker_api()
    kwargs={}
    if damage=='wrong_receipt':
        body['receipt_id']='missing'
    elif damage=='wrong_native':
        body['native']['tool_call_id']='other-call'
    elif damage=='wrong_contract':
        body['payload']['execution']['key_columns']=['value']
    elif damage=='changed_input':
        (workspace/'source.csv').write_text('changed')
    elif damage=='escape_path':
        body['payload']['execution']['output_path']=str(tmp_path/'outside.csv')
    elif damage=='symlink_path':
        (workspace/'link.csv').symlink_to(workspace/'source.csv')
        body['payload']['execution']['input_path']=str(workspace/'link.csv')
    elif damage=='malformed':
        body=b'{'
    elif damage=='extra_field':
        body['argv']=['unsafe']
    elif damage=='oversized':
        body=b'{}'
        kwargs['headers']={'Content-Length':'1048577'}
    elif damage=='content_type':
        kwargs['content_type']='text/plain'
    elif damage=='auth':
        kwargs['token']='incorrect'
    elif damage=='auth_nonascii':
        kwargs['token']='invalid-é'
    with module.execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        if damage=='route':
            kwargs['path']=env['RPE_HERMES_BROKER_URL']+'/other'
        status,answer=post(env,body,**kwargs)
    assert status==expected,answer
    assert isinstance(answer.get('error',{}).get('code'),str)
    assert not (workspace/'result.csv').exists()
    import sqlite3
    with sqlite3.connect(db) as connection:
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'runs' not in tables,'rejected broker input must not start Harness'


def test_broker_rejects_actual_none_even_when_receipt_exists(admitted_task):
    from rpe_harness.integrations.hermes import HostEventStore
    workspace,db,body=admitted_task
    body['native']['entry_key']='strict-entry'
    store=HostEventStore(db)
    receipt=store.record('rpe_task_received',body['native'])
    body['receipt_id']=receipt['ingest_event_id']
    store.record('pre_api_request',{**body['native'],'request':{'body':{'tool_choice':'none',
        'parallel_tool_calls':False,'tools':[{'function':{'name':'rpe_task'}}]}}})
    with broker_api().execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        status,answer=post(env,body)
    assert status==403 and answer['error']['code']=='broker_receipt_invalid'
    assert not (workspace/'result.csv').exists()


@pytest.mark.parametrize('strict',[False,True])
def test_optional_allows_two_tasks_while_strict_closes_its_turn(admitted_task,strict):
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.integrations.hermes_gate import contract_for,digest_json
    workspace,db,first=admitted_task
    store=HostEventStore(db)
    if strict:
        first['native']['entry_key']='stable-strict-entry'
        first['payload']['event_id']='entry-'+digest_json([first['native']['entry_key'],
            first['native']['contract_sha256'],first['native']['input_sha256']])
        first['receipt_id']=store.record('rpe_task_received',first['native'])['ingest_event_id']
        store.record('pre_api_request',{**first['native'],'request':{'body':{
            'tool_choice':{'type':'function','function':{'name':'rpe_task'}},'parallel_tool_calls':False,
            'tools':[{'function':{'name':'rpe_task'}}]}}})
    second=json.loads(json.dumps(first))
    second['native']['tool_call_id']='different-call-same-turn'
    second['payload']['execution']['output_path']=str(workspace/'second.csv')
    second['native']['contract_sha256']=digest_json(contract_for(second['payload'],workspace))
    second['payload']['event_id']=('entry-'+digest_json([second['native']['entry_key'],
        second['native']['contract_sha256'],second['native']['input_sha256']]) if strict else 'second-optional-task')
    second['receipt_id']=store.record('rpe_task_received',second['native'])['ingest_event_id']
    with broker_api().execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        status,answer=post(env,first)
        assert status==200 and answer['result']['status']=='succeeded',answer
        status,answer=post(env,second)
    assert status==(403 if strict else 200),answer
    assert (workspace/'second.csv').exists() is (not strict)


def test_same_receipt_replay_is_read_only_and_changed_event_is_rejected(admitted_task):
    import sqlite3
    workspace,db,body=admitted_task
    with broker_api().execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        first=post(env,body)
        assert post(env,body)==first
        body['payload']['event_id']='reinterpreted-event'
        status,answer=post(env,body)
    assert status==403 and answer['error']['code']=='broker_receipt_changed'
    assert env.cleanup['cleanup_complete'] is True
    assert env.cleanup['tracked_children']==1
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==1


def test_broker_launch_failure_replays_terminal_response_and_allows_new_task(admitted_task,monkeypatch):
    from rpe_harness.integrations.hermes import HostEventStore
    workspace,db,body=admitted_task
    module=broker_api()
    original=module.subprocess.Popen
    launches=[]
    def launch(argv,*args,**kwargs):
        if argv[:5]==[sys.executable,'-B','-m','rpe_harness','events']:
            launches.append(argv)
            if len(launches)==1:
                raise OSError('injected fixed CLI launch failure')
        return original(argv,*args,**kwargs)
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    with module.execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        first=post(env,body)
        assert first==(502,{'error':{'code':'harness_launch_error'}})
        assert post(env,body)==first
        changed=json.loads(json.dumps(body))
        changed['payload']['event_id']='changed-failed-envelope'
        assert post(env,changed)==(403,{'error':{'code':'broker_receipt_changed'}})
        next_task=json.loads(json.dumps(body))
        next_task['payload']['event_id']='task-after-launch-failure'
        next_task['native'].update(task_id='next-task',turn_id='next-turn',
                                   api_request_id='next-api',tool_call_id='next-call')
        next_task['receipt_id']=HostEventStore(db).record(
            'rpe_task_received',next_task['native'])['ingest_event_id']
        status,answer=post(env,next_task)
        assert status==200 and answer['result']['status']=='succeeded',answer
    assert len(launches)==2
    assert (workspace/'result.csv').read_text(encoding='utf-8-sig')=='id,value\na,first\nb,second\n'
    assert env.cleanup['cleanup_complete'] is True
    assert env.cleanup['tracked_children']==1


def test_broker_context_closes_a_half_open_http_worker(admitted_task):
    import socket
    from urllib.parse import urlsplit
    workspace,db,_=admitted_task
    context=broker_api().execution_broker(db_path=db,workspace=workspace,python=sys.executable)
    env=context.__enter__()
    endpoint=urlsplit(env['RPE_HERMES_BROKER_URL'])
    connection=socket.create_connection((endpoint.hostname,endpoint.port),timeout=2)
    time.sleep(0.1)
    completed=threading.Event()
    def close_context():
        context.__exit__(None,None,None)
        completed.set()
    closer=threading.Thread(target=close_context)
    closer.start()
    try:
        finished=completed.wait(timeout=4)
    finally:
        connection.close()
        closer.join(timeout=4)
    assert finished,'a half-open request must not hold the broker context forever'
    assert env.cleanup['cleanup_complete'] is True


def test_broker_context_closes_a_continuously_dripping_request_line(admitted_task):
    import socket
    from urllib.parse import urlsplit
    workspace,db,_=admitted_task
    context=broker_api().execution_broker(db_path=db,workspace=workspace,python=sys.executable)
    env=context.__enter__()
    endpoint=urlsplit(env['RPE_HERMES_BROKER_URL'])
    connection=socket.create_connection((endpoint.hostname,endpoint.port),timeout=2)
    stop=threading.Event()
    def drip():
        while not stop.wait(0.05):
            try:
                connection.sendall(b'P')
            except OSError:
                return
    writer=threading.Thread(target=drip)
    writer.start()
    time.sleep(0.1)
    completed=threading.Event()
    def close_context():
        context.__exit__(None,None,None)
        completed.set()
    closer=threading.Thread(target=close_context)
    closer.start()
    try:
        finished=completed.wait(timeout=1)
    finally:
        stop.set()
        connection.close()
        writer.join(timeout=2)
        closer.join(timeout=4)
    assert finished,'context exit must close an accepted socket despite continuous input'
    assert env.cleanup['cleanup_complete'] is True


def test_broker_shutdown_is_bounded_and_reports_a_blocked_validation_worker(admitted_task,monkeypatch):
    workspace,db,body=admitted_task
    module=broker_api()
    entered,release,closed=threading.Event(),threading.Event(),threading.Event()
    original=module._validated_request
    def blocked(*args):
        entered.set()
        release.wait(timeout=10)
        return original(*args)
    monkeypatch.setattr(module,'_validated_request',blocked)
    context=module.execution_broker(db_path=db,workspace=workspace,python=sys.executable)
    env=context.__enter__()
    def request():
        try:
            post(env,body)
        except OSError:
            pass
    client=threading.Thread(target=request)
    client.start()
    assert entered.wait(timeout=2)
    def close_context():
        context.__exit__(None,None,None)
        closed.set()
    closer=threading.Thread(target=close_context)
    closer.start()
    try:
        finished=closed.wait(timeout=4)
    finally:
        release.set()
        client.join(timeout=3)
        closer.join(timeout=3)
    assert finished,'broker worker joins must have a deadline'
    assert env.cleanup['cleanup_complete'] is False
    assert any(error['error_type']=='BrokerWorkerAlive' for error in env.cleanup['errors'])
    assert env.cleanup['tracked_children']==0
    assert not (workspace/'result.csv').exists()


def test_broker_child_uses_a_private_bound_temp_directory_removed_on_exit(admitted_task,monkeypatch):
    workspace,db,body=admitted_task
    module=broker_api()
    original=module.subprocess.Popen
    def launch(argv,*args,**kwargs):
        if argv[:5]!=[sys.executable,'-B','-m','rpe_harness','events']:
            return original(argv,*args,**kwargs)
        code='''import json,tempfile,pathlib,os
path=pathlib.Path(tempfile.mkstemp()[1])
path.write_text('private temporary execution bytes')
print(json.dumps({'status':'error','temporary_file':str(path),
 'temp_values':[os.environ.get(k) for k in ['TMPDIR','TMP','TEMP']]}))
'''
        return original([sys.executable,'-B','-c',code],*args,**kwargs)
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    with module.execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        status,answer=post(env,body)
        assert status==200
        file=Path(answer['result']['temporary_file'])
        assert file.is_relative_to(db.parent) and file.read_text()=='private temporary execution bytes'
        directory=file.parent
        assert directory.stat().st_mode & 0o777==0o700
        assert answer['result']['temp_values']==[str(directory)]*3
    assert not directory.exists()
    assert env.cleanup['cleanup_complete'] is True


def test_broker_cleanup_query_failure_still_stops_known_detached_child(admitted_task,tmp_path,monkeypatch):
    import os
    import signal
    from rpe_harness.integrations import hermes_session
    workspace,db,body=admitted_task
    module=broker_api()
    original_popen=module.subprocess.Popen
    original_capture=hermes_session._capture_descendants
    pidfile=tmp_path/'owned-broker-processes.json'
    captured,fault=threading.Event(),threading.Event()
    def launch(argv,*args,**kwargs):
        if argv[:5]!=[sys.executable,'-B','-m','rpe_harness','events']:
            return original_popen(argv,*args,**kwargs)
        code="import subprocess,os,json,pathlib,time; p=subprocess.Popen(['/bin/sleep','30'],start_new_session=True); pathlib.Path("+repr(str(pidfile))+").write_text(json.dumps([os.getpid(),p.pid])); time.sleep(30)"
        return original_popen([sys.executable,'-B','-c',code],*args,**kwargs)
    def capture(*args):
        if fault.is_set():
            raise OSError('owned injected query failure')
        original_capture(*args)
        if args[2]:
            captured.set()
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    monkeypatch.setattr(hermes_session,'_capture_descendants',capture)
    def request(env):
        try:
            post(env,body)
        except OSError:
            pass
    try:
        with module.execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
            client=threading.Thread(target=lambda:request(env))
            client.start()
            assert captured.wait(timeout=3)
            fault.set()
        client.join(timeout=3)
        assert env.cleanup['cleanup_complete'] is False
        assert env.cleanup['errors']
        for pid in json.loads(pidfile.read_text()):
            with pytest.raises(ProcessLookupError):
                os.kill(pid,0)
    finally:
        if pidfile.exists():
            for pid in json.loads(pidfile.read_text()):
                try:
                    os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize('cancel_on_exit',[False,True])
def test_broker_stops_owned_child_group_on_timeout_or_context_exit(admitted_task,tmp_path,monkeypatch,cancel_on_exit):
    import os
    workspace,db,body=admitted_task
    module=broker_api()
    pidfile=tmp_path/'owned-child-pid'
    original=module.subprocess.Popen
    processes=[]
    def launch(argv,*args,**kwargs):
        if argv[:5]!=[sys.executable,'-B','-m','rpe_harness','events']:
            return original(argv,*args,**kwargs)
        assert argv==[sys.executable,'-B','-m','rpe_harness','events','submit','--event','-','--db',str(db)]
        code='import os,time,pathlib; pathlib.Path('+repr(str(pidfile))+').write_text(str(os.getpid())); time.sleep(30)'
        process=original([sys.executable,'-B','-c',code],*args,**kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    response,transport_errors=[],[]
    def request_task(env):
        try:
            response.append(post(env,body))
        except OSError as error:
            transport_errors.append(type(error).__name__)
    with module.execution_broker(db_path=db,workspace=workspace,python=sys.executable,
                                 request_timeout=0.3 if not cancel_on_exit else 120) as env:
        thread=threading.Thread(target=lambda:request_task(env))
        thread.start()
        deadline=time.monotonic()+3
        while not pidfile.exists() and time.monotonic()<deadline:
            time.sleep(0.01)
        assert pidfile.exists()
        if not cancel_on_exit:
            thread.join(timeout=3)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert processes and all(process.poll() is not None for process in processes)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()),0)
    if cancel_on_exit:
        assert (response and response[0][0] in (503,504)) or transport_errors
    else:
        assert response[0][0]==504 and not transport_errors
    assert env.cleanup['cleanup_complete'] is True
    assert env.cleanup['remaining_children']==[]


def test_broker_directory_swap_after_validation_cannot_publish_outside(admitted_task,tmp_path,monkeypatch):
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.integrations.hermes_gate import contract_for,digest_json
    workspace,db,body=admitted_task
    output_dir=workspace/'output'
    output_dir.mkdir()
    outside=tmp_path/'outside'
    outside.mkdir()
    body['payload']['execution']['output_path']=str(output_dir/'result.csv')
    body['native']['contract_sha256']=digest_json(contract_for(body['payload'],workspace))
    body['receipt_id']=HostEventStore(db).record('rpe_task_received',body['native'])['ingest_event_id']
    module=broker_api()
    original=module._validated_request
    def swap(*args):
        result=original(*args)
        output_dir.rename(workspace/'original-output')
        output_dir.symlink_to(outside,target_is_directory=True)
        return result
    monkeypatch.setattr(module,'_validated_request',swap)
    with module.execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        status,answer=post(env,body)
    assert not (outside/'result.csv').exists(),(status,answer)
    assert not list(outside.iterdir())
    assert status in {200,403},answer
    if status==200:
        assert answer['result']['status']!='succeeded',answer


@pytest.mark.parametrize('phase',['prepare','publish'])
@pytest.mark.parametrize('input_format',['csv','xlsx'])
def test_broker_child_directory_swap_keeps_writes_bound(admitted_task,tmp_path,monkeypatch,phase,input_format):
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.integrations.hermes_gate import contract_for,digest_json,request_input_sha
    workspace,db,body=admitted_task
    output_dir=workspace/'output'
    output_dir.mkdir()
    outside=tmp_path/'outside'
    outside.mkdir()
    if input_format=='xlsx':
        from openpyxl import Workbook
        book=Workbook()
        book.active.append(['id','value'])
        for row in [('a','first'),('a','later'),('b','second')]:
            book.active.append(row)
        book.save(workspace/'source.xlsx')
        book.close()
        body['payload']['execution']['input_path']=str(workspace/'source.xlsx')
    body['payload']['execution']['output_path']=str(output_dir/('result.'+input_format))
    body['native']['contract_sha256']=digest_json(contract_for(body['payload'],workspace))
    body['native']['input_sha256']=request_input_sha(body['payload'],workspace)
    body['receipt_id']=HostEventStore(db).record('rpe_task_received',body['native'])['ingest_event_id']
    module=broker_api()
    original=module.subprocess.Popen
    def launch(argv,*args,**kwargs):
        if argv[:5]!=[sys.executable,'-B','-m','rpe_harness','events']:
            return original(argv,*args,**kwargs)
        function='_new_staging' if phase=='prepare' else 'publish_output'
        script=f'''import runpy,sys
from pathlib import Path
from rpe_harness import execution
original=execution.{function}
def swap(*args,**kwargs):
    Path({str(output_dir)!r}).rename({str(workspace/'original-output')!r})
    Path({str(output_dir)!r}).symlink_to({str(outside)!r},target_is_directory=True)
    return original(*args,**kwargs)
execution.{function}=swap
sys.argv=['rpe_harness',*{argv[4:]!r}]
runpy.run_module('rpe_harness',run_name='__main__')
'''
        return original([sys.executable,'-B','-c',script],*args,**kwargs)
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    with module.execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        status,answer=post(env,body)
    assert not list(outside.iterdir()),(status,answer)
    assert status==200,answer
    assert answer['result']['status']!='succeeded',answer


def test_broker_input_mutation_after_fd_capture_rejects_receipt_drift(admitted_task,monkeypatch):
    workspace,db,body=admitted_task
    module=broker_api()
    original=module.subprocess.Popen
    def launch(argv,*args,**kwargs):
        if argv[:5]==[sys.executable,'-B','-m','rpe_harness','events']:
            (workspace/'source.csv').write_text('id,value\na,altered-after-admission\n')
        return original(argv,*args,**kwargs)
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    with module.execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        status,answer=post(env,body)
    assert status==200,answer
    assert answer['result']['status']=='rejected',answer
    assert not (workspace/'result.csv').exists()
