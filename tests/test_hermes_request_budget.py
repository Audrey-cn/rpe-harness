"""Matched process-local limits must reach Hermes's own request builder."""
import copy
import importlib
import importlib.util
import json
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


def budget_profile(**changes):
    return {'schema_version': 'rpe.hermes-request-budget/v1', 'provider': 'xiaomi',
        'model': 'mimo-v2.6-pro', 'max_completion_tokens': 2400,
        'thinking_type': 'disabled', 'request_timeout_seconds': 90,
        'stale_timeout_seconds': 90, **changes}


def adapter():
    assert importlib.util.find_spec('rpe_harness.integrations.hermes_request_budget') is not None, (
        'the process-local request budget adapter is missing')
    return importlib.import_module('rpe_harness.integrations.hermes_request_budget')


def installed(tmp_path, monkeypatch, *, profile=None, request_timeout=90, stale_timeout=90):
    api = adapter()
    path = tmp_path / 'request-budget.json'
    path.write_text(json.dumps(profile or budget_profile()))
    monkeypatch.setenv('RPE_HERMES_REQUEST_BUDGET_CONFIG', str(path))
    class Agent:
        provider = 'xiaomi'
        model = 'mimo-v2.6-pro'
        api_mode = 'chat_completions'

        def _build_api_kwargs(self, api_messages, tools_for_api=None):
            return {'model': self.model, 'messages': api_messages, 'tools': tools_for_api,
                'tool_choice': 'auto', 'parallel_tool_calls': False, 'max_tokens': 131072,
                'extra_body': {'max_completion_tokens': 8192, 'thinking': {'type': 'enabled'}}}
    modules = {'run_agent': SimpleNamespace(AIAgent=Agent), 'hermes_cli.timeouts':
        SimpleNamespace(get_provider_request_timeout=lambda provider, model: request_timeout,
            get_provider_stale_timeout=lambda provider, model: stale_timeout)}
    original_import = importlib.import_module
    monkeypatch.setattr(api.importlib, 'import_module', lambda name:
        modules[name] if name in modules else original_import(name))
    api.install_request_budget_adapter()
    return api, Agent, path


def test_budget_adapter_keeps_native_inputs_and_bounds_transport(tmp_path, monkeypatch):
    api, Agent, _ = installed(tmp_path, monkeypatch)
    messages = [{'role': 'user', 'content': 'current authorized request'}]
    tools = [{'type': 'function', 'function': {'name': 'write_file'}}]
    before = copy.deepcopy((messages, tools))
    result = Agent()._build_api_kwargs(messages, tools_for_api=tools)
    assert result['messages'] is messages and result['tools'] is tools
    assert (messages, tools) == before
    assert result['tool_choice'] == 'auto' and result['parallel_tool_calls'] is False
    assert result['max_completion_tokens'] == 2400 and 'max_tokens' not in result
    assert result['extra_body'] == {'thinking': {'type': 'disabled'}}
    assert result['timeout'] == 90
    once = Agent._build_api_kwargs
    api.install_request_budget_adapter()
    assert Agent._build_api_kwargs is once


@pytest.mark.parametrize('changed', [
    {'provider': 'custom'}, {'model': 'other-model'}, {'api_mode': 'codex_responses'}])
def test_budget_route_drift_is_blocked_before_a_native_request(tmp_path, monkeypatch, changed):
    api, Agent, _ = installed(tmp_path, monkeypatch)
    agent = Agent()
    for key, value in changed.items():
        setattr(agent, key, value)
    with pytest.raises(api.RequestBudgetBlocked):
        agent._build_api_kwargs([])


@pytest.mark.parametrize('request_timeout,stale_timeout', [(None, 90), (120, 90), (90, None), (90, 120)])
def test_budget_does_not_claim_unapplied_native_socket_limits(tmp_path, monkeypatch,
        request_timeout, stale_timeout):
    api, Agent, _ = installed(tmp_path, monkeypatch,
        request_timeout=request_timeout, stale_timeout=stale_timeout)
    with pytest.raises(api.RequestBudgetBlocked):
        Agent()._build_api_kwargs([])


@pytest.mark.parametrize('changes', [
    {'max_completion_tokens': True}, {'max_completion_tokens': 0},
    {'max_completion_tokens': 2401}, {'thinking_type': 'enabled'},
    {'request_timeout_seconds': float('inf')}, {'request_timeout_seconds': 181},
    {'stale_timeout_seconds': False}, {'provider': 'custom'}, {'model': ''},
    {'tools': []}, {'schema_version': 'other'}])
def test_invalid_budget_profile_never_becomes_request_authority(changes):
    with pytest.raises(ValueError):
        adapter().validate_request_budget_profile(budget_profile(**changes))


def test_unconfigured_adapter_does_not_import_or_patch_the_host(monkeypatch):
    api = adapter()
    monkeypatch.delenv('RPE_HERMES_REQUEST_BUDGET_CONFIG', raising=False)
    monkeypatch.setattr(api.importlib, 'import_module', lambda name: pytest.fail('host imported'))
    api.install_request_budget_adapter()


def test_changed_profile_cannot_silently_repatch_a_running_host(tmp_path, monkeypatch):
    api, Agent, path = installed(tmp_path, monkeypatch)
    path.write_text(json.dumps(budget_profile(max_completion_tokens=1000)))
    with pytest.raises(api.RequestBudgetBlocked):
        api.install_request_budget_adapter()
    assert Agent()._build_api_kwargs([])['max_completion_tokens'] == 2400


def test_link_or_duplicate_profile_is_rejected_before_host_import(tmp_path, monkeypatch):
    api = adapter()
    regular = tmp_path / 'regular.json'
    regular.write_text(json.dumps(budget_profile()))
    linked = tmp_path / 'linked.json'
    linked.symlink_to(regular)
    monkeypatch.setattr(api.importlib, 'import_module', lambda name: pytest.fail('host imported'))
    for path in (linked, regular):
        monkeypatch.setenv('RPE_HERMES_REQUEST_BUDGET_CONFIG', str(path))
        if path == regular:
            regular.write_text('{"provider":"xiaomi","provider":"custom"}')
        with pytest.raises(api.RequestBudgetBlocked):
            api.install_request_budget_adapter()


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='FIFO unavailable')
def test_fifo_profile_is_rejected_without_waiting_for_a_writer(tmp_path):
    fifo = tmp_path / 'profile.fifo'
    os.mkfifo(fifo)
    child = subprocess.run([sys.executable, '-B', '-c', '''
from rpe_harness.integrations.hermes_request_budget import install_request_budget_adapter, RequestBudgetBlocked
try: install_request_budget_adapter()
except RequestBudgetBlocked: print('blocked')
else: raise AssertionError('FIFO profile was admitted')
'''], env={**os.environ, 'RPE_HERMES_REQUEST_BUDGET_CONFIG': str(fifo),
        'PYTHONDONTWRITEBYTECODE': '1'}, cwd=tmp_path, capture_output=True, text=True, timeout=2)
    assert child.returncode == 0 and child.stdout.strip() == 'blocked', child.stderr


def runtime_environment(tmp_path):
    """All host state belongs to pytest's explicitly owned temporary root."""
    import rpe_harness
    root = Path(os.environ.get('RPE_HERMES_SOURCE', '/Users/audrey/.hermes/hermes-agent'))
    interpreter = root / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('installed Hermes runtime unavailable')
    home = tmp_path / 'home'
    home.mkdir()
    for name in ('runtime', 'cache', 'tmp'):
        (home / name).mkdir()
    (home / '.no-bundled-skills').touch()
    (home / 'config.yaml').write_text(json.dumps({
        'model': {'provider': 'xiaomi', 'default': 'mimo-v2.6-pro',
            'base_url': 'https://example.invalid/v1'},
        'providers': {'xiaomi': {'request_timeout_seconds': 90, 'stale_timeout_seconds': 90}},
        'agent': {'max_turns': 8, 'api_max_retries': 1, 'auto_recovery_cycles': 0,
            'environment_probe': False}, 'memory': {'memory_enabled': False,
            'user_profile_enabled': False, 'provider': ''}, 'plugins': {'enabled': []},
        'auxiliary': {'title_generation': {'enabled': False},
            'background_review': {'enabled': False}}, 'compression': {'enabled': False}}))
    profile = tmp_path / 'request-budget.json'
    profile.write_text(json.dumps(budget_profile()))
    environment = {key: value for key, value in os.environ.items() if key in {
        'USER', 'LOGNAME', 'SHELL', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE'}}
    environment.update(HOME=str(home), HERMES_HOME=str(home), XDG_CACHE_HOME=str(home / 'cache'),
        HERMES_RUNTIME_DIR=str(home / 'runtime'), TMPDIR=str(home / 'tmp'),
        TMP=str(home / 'tmp'), TEMP=str(home / 'tmp'), UV_CACHE_DIR=str(home / 'cache/uv'),
        HERMES_DISABLE_LAZY_INSTALLS='1', HERMES_ENABLE_PROJECT_PLUGINS='0',
        PYTHONDONTWRITEBYTECODE='1', RPE_HERMES_REQUEST_BUDGET_CONFIG=str(profile),
        PYTHONPATH=str(Path(rpe_harness.__file__).resolve().parent.parent) + os.pathsep + str(root))
    return interpreter, environment


@contextmanager
def offline_provider_key(environment):
    reader, writer = os.pipe()
    try:
        with os.fdopen(writer, 'wb') as stream:
            stream.write(b'offline-not-secret')
        environment['RPE_HERMES_PROVIDER_KEY_FD'] = str(reader)
        yield reader
    finally:
        os.close(reader)


@pytest.mark.parametrize('tool_enabled', ['0', '1'])
def test_installed_streaming_sdk_observes_the_same_bounded_wire_for_both_arms(tmp_path,
        tool_enabled):
    interpreter, environment = runtime_environment(tmp_path)
    environment['RPE_HERMES_TOOL_ENABLED'] = tool_enabled
    child = subprocess.run([str(interpreter), '-B', '-c', _STREAM_WIRE_PROGRAM],
        env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr[-6000:] + child.stdout[-2000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result['network_calls'] == result['model_calls'] == 0
    assert result['synthetic_api_calls'] == 1
    assert result['wire'] == [{'model': 'mimo-v2.6-pro', 'stream': True,
        'max_completion_tokens': 2400, 'thinking': {'type': 'disabled'},
        'timeout': {'connect': 60.0, 'pool': 60.0, 'read': 90.0, 'write': 90.0}}]


@pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
    reason='installed Hermes native macOS file bootstrap')
def test_file_bootstrap_installs_budget_without_preloading_sdk_or_harness(tmp_path):
    import rpe_harness
    from rpe_harness.integrations.hermes_session import sandbox_profile
    interpreter, environment = runtime_environment(tmp_path)
    hermes_root = interpreter.parents[2]
    source = Path(rpe_harness.__file__).resolve().parent.parent
    bootstrap = source / 'rpe_harness/integrations/hermes_bootstrap.py'
    home = Path(environment['HERMES_HOME'])
    environment.update(PYTHONPATH=str(hermes_root),
        RPE_HERMES_INSTALL_ROOT=str(hermes_root), RPE_HERMES_TOOL_ENABLED='0',
        RPE_HERMES_ENTRY_MODE='optional', RPE_HARNESS_IMPORT_ROOT=str(source),
        RPE_HERMES_DB=str(tmp_path / 'events.sqlite'), RPE_HERMES_WORKSPACE=str(tmp_path),
        RPE_HERMES_PROCESS_PATH=str(home / 'processes.jsonl'))
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('fixture')
    profile = tmp_path / 'bootstrap.sb'
    profile.write_text(sandbox_profile([tmp_path], Path.home() / '.hermes', hermes_root,
        tmp_path / 'events.sqlite', read_roots=[interpreter.parent.parent,
            Path.home() / '.hermes/tools'], native_import_root=source,
        owner_read_scope=(tmp_path.parent, [tmp_path])))
    # The provider alone is simulated. CLI dispatch, host construction, builder,
    # streaming, SDK serialization and macOS import restrictions remain real.
    program = _STREAM_WIRE_PROGRAM[:_STREAM_WIRE_PROGRAM.index(
        'from rpe_harness.integrations.hermes_request_budget')] + _FILE_BOOTSTRAP_TAIL
    with offline_provider_key(environment) as key_fd:
        child = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), str(interpreter),
            '-B', '-c', program, str(bootstrap), str(prompt)], env=environment, cwd=tmp_path,
            capture_output=True, text=True, timeout=45, pass_fds=(key_fd,))
    (tmp_path / 'file-bootstrap.stderr.log').write_text(child.stderr)
    (tmp_path / 'file-bootstrap.stdout.log').write_text(child.stdout)
    assert child.returncode == 0, child.stderr[-6000:] + child.stdout[-3000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result['file_bootstrap'] is True and result['sdk_preloaded_at_entry'] is False
    assert result['child_harness_importable'] is False
    assert result['wire'] == [{'model': 'mimo-v2.6-pro', 'stream': True,
        'max_completion_tokens': 2400, 'thinking': {'type': 'disabled'},
        'timeout': {'connect': 60.0, 'pool': 60.0, 'read': 90.0, 'write': 90.0}}]


_STREAM_WIRE_PROGRAM = r'''import json,socket,sys
def deny(*args,**kwargs): raise AssertionError('network disabled in SDK wire smoke')
socket.socket.connect=deny
socket.create_connection=deny
import httpx
wire=[]
def offline_transport(self,request):
    body=json.loads(request.content)
    assert body['stream'] is True,body
    assert body['max_completion_tokens']==2400,body
    assert 'max_tokens' not in body and 'max_output_tokens' not in body,body
    assert body['thinking']=={'type':'disabled'},body
    wire.append({key:body[key] for key in ('model','stream','max_completion_tokens','thinking')})
    wire[-1]['timeout']=request.extensions['timeout']
    base={'id':'offline-response','created':0,'object':'chat.completion.chunk','model':'mimo-v2.6-pro'}
    chunks=[{**base,'choices':[{'index':0,'delta':{'role':'assistant','content':'offline fixture'},'finish_reason':None}]},
        {**base,'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]},
        {**base,'choices':[],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}]
    data=''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
    return httpx.Response(200,headers={'content-type':'text/event-stream'},content=data.encode(),request=request)
httpx.HTTPTransport.handle_request=offline_transport
assert 'run_agent' not in sys.modules and 'openai' not in sys.modules
from rpe_harness.integrations.hermes_request_budget import install_request_budget_adapter
install_request_budget_adapter()
from run_agent import AIAgent
agent=AIAgent(api_key='offline-not-secret',base_url='https://example.invalid/v1',provider='xiaomi',
    api_mode='chat_completions',model='mimo-v2.6-pro',max_iterations=8,run_budget_seconds=180,
    max_tokens=131072,request_overrides={'extra_body':{'max_tokens':65536,'thinking':{'type':'enabled'}}},
    quiet_mode=True,skip_context_files=True,skip_memory=True,save_trajectories=False,
    session_id='request-budget-wire',platform='cli',enabled_toolsets=[],stream_delta_callback=lambda value:None)
agent._ephemeral_max_output_tokens=262144
agent._cached_system_prompt='Offline real Hermes streaming SDK smoke.'
agent._use_prompt_caching=False
agent.compression_enabled=False
agent._fallback_chain=[]
result=agent.run_conversation('fixture',task_id='request-budget-task')
assert result['completed'] is True,result
assert result['api_calls']==len(wire)==1,result
print(json.dumps({'network_calls':0,'model_calls':0,'synthetic_api_calls':1,'wire':wire}))
'''


_FILE_BOOTSTRAP_TAIL = r'''import importlib.util,os,subprocess
assert importlib.util.find_spec('rpe_harness') is None,'Harness exposed at native entry'
initial_pythonpath=os.environ['PYTHONPATH']
spec=importlib.util.spec_from_file_location('actual_file_bootstrap',sys.argv[1])
bootstrap=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
try:
    status=bootstrap.main(['chat','--cli','--query-file',sys.argv[2],'--oneshot','-Q',
        '--format','stream-json','--source','rpe-harness','--provider','xiaomi',
        '--model','mimo-v2.6-pro','--toolsets','terminal,file','--max-turns','8',
        '--run-budget','180','--ignore-rules'])
except SystemExit as exc:
    status=exc.code
assert status in (None,0),status
assert os.environ['PYTHONPATH']==initial_pythonpath
assert os.environ['RPE_HARNESS_IMPORT_ROOT'] not in sys.path
child=subprocess.run([sys.executable,'-B','-c',
    "import importlib.util;assert importlib.util.find_spec('rpe_harness') is None"],
    capture_output=True,text=True,timeout=5)
assert child.returncode==0,child.stderr
assert len(wire)==1,wire
print(json.dumps({'file_bootstrap':True,'sdk_preloaded_at_entry':False,
    'child_harness_importable':False,'wire':wire,'network_calls':0,'model_calls':0}))
'''
