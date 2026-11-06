import json
from pathlib import Path

import pytest

from rpe_harness.integrations.hermes_session import aggregate_usage, sandbox_profile, prepare_home


def test_missing_usage_is_unknown():
    result = aggregate_usage([{'kind': 'post_api_request', 'usage': None}])
    assert result['total_tokens'] is None
    assert result['requests_with_usage'] == 0


def test_uncached_input_and_cache_are_not_double_counted():
    result = aggregate_usage([{'kind': 'post_api_request', 'usage': {
        'input_tokens': 7, 'output_tokens': 3, 'prompt_tokens': 12, 'total_tokens': 15,
        'cache_read_tokens': 5, 'cache_write_tokens': 0,
        'present_fields': ['prompt_tokens', 'completion_tokens', 'prompt_tokens_details.cached_tokens'],
    }}])
    assert result['total_tokens'] == 15
    assert result['prompt_tokens'] == 12
    assert result['cache_read_tokens'] == 5
    assert result['cache_write_tokens'] is None


def test_partial_usage_total_unknown_known_sum_preserved():
    result = aggregate_usage([
        {'kind': 'post_api_request', 'usage': {'total_tokens': 20, 'prompt_tokens': 15, 'output_tokens': 5,
         'present_fields': ['prompt_tokens', 'completion_tokens', 'total_tokens']}},
        {'kind': 'post_api_request', 'usage': None},
    ])
    assert result['total_tokens'] is None
    assert result['known_total_tokens'] == 20
    assert result['api_requests'] == 2


def test_normalized_defaults_and_api_errors_are_not_complete_usage():
    result = aggregate_usage([
        {'kind': 'post_api_request', 'usage': {'total_tokens': 0, 'prompt_tokens': 0,
         'output_tokens': 0, 'present_fields': [], 'raw_provider_usage': {}}},
        {'kind': 'api_request_error', 'api_request_id': 'failed-request'},
    ])
    assert result['api_requests'] == 2
    assert result['api_errors'] == 1
    assert result['requests_with_usage'] == 0
    assert result['total_tokens'] is None
    assert not result['coverage_complete']


def test_profile_scopes_writes_and_signals(tmp_path):
    profile = sandbox_profile([tmp_path], Path('/Users/example/.hermes'))
    assert '(deny file-write*' in profile
    assert str(tmp_path.resolve()) in profile
    assert '(target same-sandbox)' in profile
    assert '/Users/example/.hermes/auth.json' in profile


def test_home_never_writes_provider_key_and_disables_background(tmp_path):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    settings = {'provider': 'xiaomi', 'model': 'mimo-v2.6-pro',
                'base_url': 'https://token-plan-cn.xiaomimimo.com/v1', 'api_key': 'test-secret'}
    home = prepare_home(tmp_path / 'session', workspace, tmp_path / 'runs.sqlite',
                        tool_enabled=True, settings=settings)
    assert not (home / '.env').exists()
    assert 'test-secret' not in (home / 'config.yaml').read_text()
    config = json.loads((home / 'config.yaml').read_text())
    assert config['plugins']['enabled'] == ['rpe-harness']
    assert config['mcp_servers'] == {}
    assert config['auxiliary']['title_generation']['enabled'] is False
    assert not (home / 'auth.json').exists()


def test_home_rejects_unsupported_provider_before_copy(tmp_path):
    with pytest.raises(ValueError, match='xiaomi'):
        prepare_home(tmp_path / 'session', tmp_path, tmp_path / 'runs.sqlite',
                     tool_enabled=True, settings={'provider': 'nous', 'api_key': 'secret'})


def test_strict_home_exposes_only_eager_task_tool(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    home = prepare_home(tmp_path/'session', workspace, tmp_path/'db.sqlite', tool_enabled=True,
                        entry_mode='strict', settings={'provider':'xiaomi', 'model':'test',
                        'base_url':'https://example.invalid/v1', 'api_key':'owned-test-key'})
    config = json.loads((home/'config.yaml').read_text())
    assert config['platform_toolsets']['cli'] == ['rpe_harness']
    assert config['tools']['tool_search']['enabled'] == 'off'


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(),reason='macOS native observer sandbox')
def test_native_observer_hashes_tools_inside_minimal_import_sandbox(tmp_path):
    import os
    import subprocess
    import sys
    source=Path(__file__).resolve().parents[1]/'src'
    workspace=tmp_path/'native'
    workspace.mkdir()
    profile=sandbox_profile([workspace],Path.home()/'.hermes',native_import_root=source)
    program='''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from rpe_harness.integrations.hermes import HostEventStore,register
class Context:
    def __init__(self): self.hooks={}
    def register_hook(self,name,callback): self.hooks[name]=callback
context=Context()
register(context)
context.hooks['pre_api_request'](session_id='native-session',api_request_id='api-one',request={
    'body':{'tool_choice':'auto','messages':[{'content':'PRIVATE_PROMPT'}],
            'tools':[{'type':'function','function':{'name':'terminal',
                     'description':'PRIVATE_SCHEMA','parameters':{'type':'object'}}}]}})
assert 'rpe_harness.integrations.hermes_gate' not in sys.modules
try:
    (Path(sys.argv[1])/'rpe_harness/integrations/hermes_gate.py').read_text()
except PermissionError:
    pass
else:
    raise AssertionError('oracle source must stay unreadable')
print(json.dumps(HostEventStore(sys.argv[2]).list('native-session')))
'''
    db=workspace/'runs.sqlite'
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','RPE_HERMES_TOOL_ENABLED':'0',
         'RPE_HERMES_DB':str(db),'RPE_HERMES_WORKSPACE':str(workspace)}
    child=subprocess.run(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-B','-c',program,
                          str(source),str(db)],env=env,cwd=workspace,capture_output=True,text=True,timeout=15)
    assert child.returncode==0,child.stderr
    rows=json.loads(child.stdout)
    assert len(rows)==1
    assert rows[0]['request_tool_names']==['terminal']
    assert len(rows[0]['request_tools_sha256'])==64
    assert 'PRIVATE' not in child.stdout


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(),reason='macOS nested installed runtime sandbox')
@pytest.mark.parametrize('tool_enabled',[True,False])
def test_owned_parent_allows_only_explicit_installed_runtime_reads(tmp_path,monkeypatch,tool_enabled):
    import shutil
    import sys
    from rpe_harness.integrations import hermes,hermes_session as module
    runtime=tmp_path/'installed-runtime'
    import_root=runtime/'lib/python/site-packages'
    package=import_root/'rpe_harness'
    integrations=package/'integrations'
    integrations.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    (integrations/'__init__.py').write_text('')
    shutil.copyfile(hermes.__file__,integrations/'hermes.py')
    (package/'contracts.py').write_text('ORACLE_READ_MUST_REMAIN_DENIED')
    sibling=tmp_path/'unrelated-owned-secret.txt'
    sibling.write_text('SIBLING_READ_MUST_REMAIN_DENIED')
    workspace=tmp_path/'workspace'
    workspace.mkdir()
    hermes_root=tmp_path/'fake-installed-hermes'
    hermes_root.mkdir()
    dependency=Path(sys.executable).absolute().parent.parent/'pyvenv.cfg'
    assert dependency.is_file(),'test requires the owned venv interpreter'
    owner=dependency.parent.parent
    assert tmp_path.resolve().is_relative_to(owner.resolve())
    bootstrap=integrations/'hermes_bootstrap.py'
    program='''import json,os,sys
from pathlib import Path
sys.path.insert(0,IMPORT_ROOT)
assert Path(DEPENDENCY).read_text()
def denied_read(path):
    try: Path(path).read_text()
    except PermissionError: return
    raise AssertionError('unexpected read permission')
denied_read(SIBLING)
if not TOOL_ENABLED: denied_read(ORACLE)
for path in (DEPENDENCY,__file__):
    try: Path(path).write_text('forbidden runtime modification')
    except PermissionError: pass
    else: raise AssertionError('unexpected runtime write permission')
from rpe_harness.integrations.hermes import HostEventStore
HostEventStore(os.environ['RPE_HERMES_DB']).record('on_session_end',{'session_id':'nested-runtime-session','completed':True})
print(json.dumps({'type':'result','session_id':'nested-runtime-session','text':'owned runtime read probe'}))
'''
    program=('IMPORT_ROOT='+repr(str(import_root))+'\nDEPENDENCY='+repr(str(dependency))+
             '\nSIBLING='+repr(str(sibling))+'\nORACLE='+repr(str(package/'contracts.py'))+
             '\nTOOL_ENABLED='+repr(tool_enabled)+'\n'+program)
    bootstrap.write_text(program)
    before_dependency=dependency.read_bytes()
    monkeypatch.setattr(module,'__file__',str(integrations/'hermes_session.py'))
    # The real host interpreter lives outside the owned Harness venv; resolve the
    # test venv's symlink to reproduce that distinct-host startup arrangement.
    monkeypatch.setattr(module,'_installed_python',lambda root:Path(sys.executable).resolve())
    monkeypatch.setattr(module,'_read_host_settings',lambda *a:{'provider':'xiaomi','model':'owned-test-model',
        'base_url':'https://example.invalid/v1','api_key':'owned-test-secret'})
    result=module.run_hermes_session('No model: exercise owned runtime read boundary',workspace,tmp_path/'runs.sqlite',
        tool_enabled=tool_enabled,owner_root=owner,hermes_root=hermes_root,timeout=5)
    try:
        assert result['completed'] is True,(result,Path(result['session_root'],'stderr.log').read_text())
        assert result['exit_code']==0
        assert result['process_cleanup']['cleanup_complete'] is True
        assert result['usage']['api_requests']==0
        assert result['events'][0]['kind']=='on_session_end'
        assert dependency.read_bytes()==before_dependency
        assert bootstrap.read_text()==program
    finally:
        shutil.rmtree(result['session_root'])


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(),reason='macOS external candidate and sandboxed host')
@pytest.mark.parametrize('separate_owner',[False,True])
def test_real_activated_candidate_runs_through_registered_tool_external_broker(tmp_path,monkeypatch,separate_owner):
    import shutil
    import sys
    import rpe_harness
    from test_candidates import candidate
    from rpe_harness.evolution import apply_candidate,validate_candidate
    from rpe_harness.integrations import hermes_session as module
    state=tmp_path/'persistent-state'
    db,proposal=candidate(state)
    assert validate_candidate(proposal['proposal_id'],db)['passed']
    active=apply_candidate(proposal['proposal_id'],db)
    runtime=tmp_path/'installed-runtime'
    import_root=runtime/'lib/python/site-packages'
    package=import_root/'rpe_harness'
    shutil.copytree(Path(rpe_harness.__file__).parent,package)
    integrations=package/'integrations'
    bootstrap=integrations/'hermes_bootstrap.py'
    workspace=tmp_path/'new-task-workspace'
    workspace.mkdir()
    source=workspace/'new.csv'
    source.write_text('id,value\na,first\na,later\nb,second\n')
    output=workspace/'new-out.csv'
    request={'schema_version':2,'event_id':'activated-host-sandbox','capability':'tabular.deduplicate',
        'goal':'retain first','constraints':['preserve_original','preserve_order'],
        'execution':{'input_path':str(source),'output_path':str(output),'key_columns':['id']}}
    sibling=state/'not-bound-private.txt'
    sibling.write_text('UNRELATED_SIBLING')
    program='''import json,os,sys
from pathlib import Path
sys.path.insert(0,IMPORT_ROOT)
from rpe_harness.integrations.hermes import HostEventStore,register
if NOT_SEPARATE:
    try: Path(SIBLING).read_text()
    except PermissionError: pass
    else: raise AssertionError('unbound owned sibling exposed')
for path in (ARTIFACT,INPUT,OUTPUT):
    try: Path(path).write_text('forbidden certificate/evidence mutation')
    except PermissionError: pass
    else: raise AssertionError('readonly candidate file became writable')
class Context:
    def __init__(self): self.hooks,self.middleware,self.tools={},{},{}
    def register_hook(self,name,callback): self.hooks[name]=callback
    def register_middleware(self,name,callback): self.middleware[name]=callback
    def register_tool(self,**values): self.tools[values['name']]=values['handler']
ctx=Context()
register(ctx)
identity={key:'candidate-test-'+key for key in ('session_id','task_id','turn_id','api_request_id','tool_call_id')}
next_call=lambda args:ctx.tools['rpe_task'](args,session_id=identity['session_id'],task_id=identity['task_id'])
answer=json.loads(ctx.middleware['tool_execution']('rpe_task',{'request':REQUEST},next_call,**identity))
ctx.hooks['post_tool_call'](**identity,tool_name='rpe_task',result=json.dumps(answer))
HostEventStore(os.environ['RPE_HERMES_DB']).record('on_session_end',{'session_id':'candidate-runtime-session','completed':answer['status']=='succeeded'})
print(json.dumps({'type':'result','session_id':'candidate-runtime-session','text':json.dumps({'status':answer['status'],'run_id':answer.get('run_id')})}))
'''
    program=('IMPORT_ROOT='+repr(str(import_root))+'\nSIBLING='+repr(str(sibling))+
        '\nARTIFACT='+repr(proposal['artifact_path'])+'\nINPUT='+repr(str(state/'orders.csv'))+
        '\nOUTPUT='+repr(str(state/'result.csv'))+'\nREQUEST='+repr(request)+
        '\nNOT_SEPARATE='+repr(not separate_owner)+'\n'+program)
    bootstrap.write_text(program)
    owner=tmp_path/'ephemeral-owner' if separate_owner else Path(sys.executable).absolute().parent.parent.parent
    monkeypatch.setattr(module,'__file__',str(integrations/'hermes_session.py'))
    monkeypatch.setattr(module,'_installed_python',lambda root:Path(sys.executable).resolve())
    monkeypatch.setattr(module,'_read_host_settings',lambda *a:{'provider':'xiaomi','model':'owned-test-model',
        'base_url':'https://example.invalid/v1','api_key':'owned-test-secret'})
    result=module.run_hermes_session('No model: exercise active producer OS boundaries',workspace,db,
        tool_enabled=True,owner_root=owner,hermes_root=tmp_path/'fake-hermes',timeout=10)
    try:
        assert result['completed'] is True,(result,Path(result['session_root'],'stderr.log').read_text())
        answer=json.loads(result['final_response'])
        from rpe_harness.storage import RunStore
        artifact=RunStore(db).get(answer['run_id'])['artifact']
        executor=artifact['actual_executor']
        assert executor['kind']=='candidate',executor
        assert executor['candidate_version_id']==active['version_id']
        assert executor['code_sha256']==proposal['content_hash']
        profile=Path(result['session_root'],'hermes.sb').read_text()
        assert str(state/'evolution-sandboxes') not in profile
        assert proposal['artifact_path'] not in profile
        assert result['process_cleanup']['external_executor_cleanup_complete'] is True
        assert result['process_cleanup']['external_executor_remaining_children']==[]
        assert output.read_text(encoding='utf-8-sig')=='id,value\na,first\nb,second\n'
        assert result['usage']['api_requests']==0
        assert result['process_cleanup']['cleanup_complete'] is True
    finally:
        shutil.rmtree(result['session_root'])


def test_descendants_track_identity_and_never_include_parent(monkeypatch):
    from rpe_harness.integrations import hermes_session as module
    table = {10: (1, 'host-start'), 20: (10, 'child-start'), 30: (20, 'grandchild-start'),
             40: (1, 'unrelated-start')}
    monkeypatch.setattr(module, '_process_table', lambda: table)
    owned = {}
    module._capture_descendants(10, 'host-start', owned)
    assert owned == {20: 'child-start', 30: 'grandchild-start'}
    table[10] = (1, 'reused-host')
    table[50] = (10, 'foreign-child')
    module._capture_descendants(10, 'host-start', owned)
    assert 50 not in owned


def test_cleanup_never_signals_reused_pid(monkeypatch):
    from rpe_harness.integrations import hermes_session as module
    monkeypatch.setattr(module, '_process_table', lambda: {20: (1, 'new-start')})
    signals = []
    monkeypatch.setattr(module.os, 'kill', lambda pid, sig: signals.append(pid))
    assert module._stop_descendants({20: 'old-start'}) == []
    assert signals == []


def test_missing_post_api_observation_cannot_shrink_denominator():
    events = [
        {'kind': 'pre_api_request', 'api_request_id': 'one'},
        {'kind': 'pre_api_request', 'api_request_id': 'two'},
        {'kind': 'post_api_request', 'api_request_id': 'one', 'usage': {
            'prompt_tokens': 5, 'output_tokens': 5, 'total_tokens': 10,
            'present_fields': ['prompt_tokens', 'completion_tokens', 'total_tokens']}},
    ]
    result = aggregate_usage(events)
    assert result['api_requests'] == 2
    assert result['missing_observations'] == 1
    assert result['total_tokens'] is None
    assert not result['coverage_complete']


def test_prompt_only_usage_does_not_make_default_output_a_known_total():
    result = aggregate_usage([{'kind': 'post_api_request', 'usage': {
        'prompt_tokens': 10, 'output_tokens': 0, 'total_tokens': 10,
        'present_fields': ['prompt_tokens'], 'raw_provider_usage': {'prompt_tokens': 10},
    }}])
    assert result['requests_with_usage'] == 1
    assert result['prompt_tokens'] == 10
    assert result['output_tokens'] is None
    assert result['total_tokens'] is None
    assert result['known_total_tokens'] == 0
    assert not result['coverage_complete']


def test_provider_total_only_overrides_zero_normalized_defaults():
    result = aggregate_usage([{'kind': 'post_api_request', 'usage': {
        'prompt_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
        'present_fields': ['total_tokens'], 'raw_provider_usage': {'total_tokens': 15},
    }}])
    assert result['total_tokens'] == result['known_total_tokens'] == 15
    assert result['coverage_complete']
    assert result['requests_with_usage'] == 1
    assert result['prompt_tokens'] is None
    assert result['output_tokens'] is None


def test_explicit_provider_total_takes_priority_over_derived_total():
    result = aggregate_usage([{'kind': 'post_api_request', 'usage': {
        'prompt_tokens': 10, 'output_tokens': 5, 'total_tokens': 15,
        'present_fields': ['prompt_tokens', 'completion_tokens', 'total_tokens'],
        'raw_provider_usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 17},
    }}])
    assert result['total_tokens'] == result['known_total_tokens'] == 17


def test_complete_prompt_output_derives_total_and_retains_explicit_zero():
    result = aggregate_usage([{'kind': 'post_api_request', 'usage': {
        'prompt_tokens': 10, 'output_tokens': 0, 'total_tokens': 10,
        'present_fields': ['prompt_tokens', 'completion_tokens'],
        'raw_provider_usage': {'prompt_tokens': 10, 'completion_tokens': 0},
    }}])
    assert result['total_tokens'] == result['known_total_tokens'] == 10
    assert result['output_tokens'] == 0
    assert result['coverage_complete']


def test_partial_usage_keeps_only_individually_known_total_sum():
    result = aggregate_usage([
        {'kind': 'post_api_request', 'usage': {'total_tokens': 0,
         'present_fields': ['total_tokens'], 'raw_provider_usage': {'total_tokens': 15}}},
        {'kind': 'post_api_request', 'usage': {'prompt_tokens': 10, 'total_tokens': 10,
         'present_fields': ['prompt_tokens'], 'raw_provider_usage': {'prompt_tokens': 10}}},
        {'kind': 'api_request_error'},
    ])
    assert result['requests_with_usage'] == 2
    assert result['known_total_tokens'] == 15
    assert result['total_tokens'] is None
    assert not result['coverage_complete']


def test_cache_hit_miss_aliases_require_alignment_with_normalized_buckets():
    usage = {'prompt_tokens': 12, 'output_tokens': 3, 'total_tokens': 15,
             'input_tokens': 7, 'cache_read_tokens': 5, 'cache_write_tokens': 0,
             'present_fields': ['prompt_tokens', 'completion_tokens',
                                'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'],
             'raw_provider_usage': {'prompt_tokens': 12, 'completion_tokens': 3,
                                    'prompt_cache_hit_tokens': 5, 'prompt_cache_miss_tokens': 7}}
    result = aggregate_usage([{'kind': 'post_api_request', 'usage': usage}])
    assert result['cache_read_tokens'] == 5
    assert result['input_tokens'] == 7
    assert result['cache_write_tokens'] is None
    usage['cache_read_tokens'] = 0
    usage['input_tokens'] = 12
    result = aggregate_usage([{'kind': 'post_api_request', 'usage': usage}])
    assert result['cache_read_tokens'] is None
    assert result['input_tokens'] is None


def test_misaligned_raw_cache_does_not_make_uncached_input_known():
    result = aggregate_usage([{'kind': 'post_api_request', 'usage': {
        'prompt_tokens': 12, 'output_tokens': 3, 'total_tokens': 15,
        'input_tokens': 12, 'cache_read_tokens': 0,
        'present_fields': ['prompt_tokens', 'completion_tokens', 'prompt_tokens_details.cached_tokens'],
        'raw_provider_usage': {'prompt_tokens': 12, 'completion_tokens': 3,
                               'prompt_tokens_details': {'cached_tokens': 5}},
    }}])
    assert result['total_tokens'] == 15
    assert result['cache_read_tokens'] is None
    assert result['input_tokens'] is None


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(), reason='macOS launcher lifecycle')
@pytest.mark.parametrize('fault,normal_exit', [
    ('registry', False), ('tag_query', False), ('tag_query', True), ('initial_process_query', False),
    ('none', True),
])
def test_cleanup_read_failure_stops_owned_host_and_known_detached_child(
        tmp_path, monkeypatch, fault, normal_exit):
    import os
    import signal
    import subprocess
    import sys
    import time
    from rpe_harness.integrations import hermes_session as module

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    original_popen = subprocess.Popen
    original_table = module._process_table
    original_clock = time.monotonic
    hosts, child_identities = [], {}
    code = '''import json,os,pathlib,subprocess,time
home=pathlib.Path(os.environ['HERMES_HOME']).parent
if os.environ['RPE_TEST_NORMAL_EXIT']=='1':
    from rpe_harness.integrations.hermes import HostEventStore
    HostEventStore(os.environ['RPE_HERMES_DB']).record('on_session_end',{'session_id':'cleanup-proof'})
    print(json.dumps({'type':'result','session_id':'cleanup-proof','exit_code':0,'text':''}),flush=True)
    (home/'probe-ready').write_text('0')
else:
    child=subprocess.Popen(['/bin/sleep','30'],start_new_session=True,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (home/'probe-ready').write_text(str(child.pid))
    time.sleep(30)
'''

    def launch(command, *args, **kwargs):
        if command[0] != '/usr/bin/sandbox-exec':
            return original_popen(command, *args, **kwargs)
        environment = dict(kwargs['env'], RPE_TEST_NORMAL_EXIT='1' if normal_exit else '0')
        kwargs['env'] = environment
        process = original_popen([sys.executable, '-B', '-c', code], *args, **kwargs)
        hosts.append(process)
        ready = Path(environment['HERMES_HOME']).parent / 'probe-ready'
        deadline = original_clock()+3
        while not ready.exists() and original_clock() < deadline:
            time.sleep(0.01)
        assert ready.exists(), 'owned fake host did not initialize'
        pid = int(ready.read_text())
        registry = Path(environment['RPE_HERMES_PROCESS_PATH'])
        if pid:
            identity = original_table()[pid][1]
            child_identities[pid] = identity
            registry.write_text('{"pid":' if fault == 'registry' else json.dumps(
                {'pid': pid, 'parent_pid': process.pid, 'lstart': identity})+'\n')
        if normal_exit:
            process.wait(timeout=3)
        return process

    monkeypatch.setattr(module.subprocess, 'Popen', launch)
    monkeypatch.setattr(module, '_installed_python', lambda _: Path(sys.executable))
    monkeypatch.setattr(module, '_read_host_settings', lambda *_: {
        'provider': 'xiaomi', 'model': 'test-model', 'base_url': 'https://example.invalid/v1',
        'api_key': 'test-key',
    })
    if fault == 'tag_query':
        def tag_failure(_):
            raise subprocess.SubprocessError('private-probe-detail')
        monkeypatch.setattr(module, '_tagged_processes', tag_failure)
    else:
        monkeypatch.setattr(module, '_tagged_processes', lambda _: {})
    if fault == 'initial_process_query':
        calls = 0
        def initial_failure():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError('private-probe-detail')
            return original_table()
        monkeypatch.setattr(module, '_process_table', initial_failure)
    clock = iter([0, 30])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(clock, original_clock()))
    try:
        result = module.run_hermes_session('Owned cleanup probe', workspace, tmp_path/'events.sqlite',
            tool_enabled=True, owner_root=tmp_path/'sessions', timeout=1)
        assert hosts[0].poll() is not None
        assert result['completed'] is (fault == 'none')
        cleanup = result['process_cleanup']
        assert cleanup['cleanup_complete'] is (fault == 'none')
        assert bool(cleanup['errors']) is (fault != 'none')
        assert cleanup['remaining_descendants'] == []
        assert not set(child_identities).intersection(original_table())
        assert 'private-probe-detail' not in json.dumps(result)
        if normal_exit:
            assert result['exit_code'] == 0 and not result['timed_out']
            assert result['session_id'] == 'cleanup-proof'
    finally:
        for process in hosts:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        table = original_table()
        for pid, identity in child_identities.items():
            if table.get(pid, (None, None))[1] == identity:
                os.kill(pid, signal.SIGKILL)
