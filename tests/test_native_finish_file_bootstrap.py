"""Real isolated Hermes CLI/tool/observer/end-turn; only HTTP responses are offline."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from test_hermes_request_budget import budget_profile, offline_provider_key, runtime_environment


@pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
    reason='installed Hermes native macOS file bootstrap')
@pytest.mark.parametrize('target', ['draft.json', 'output.csv'])
def test_native_file_bootstrap_stops_after_real_terminal_artifact_with_one_sdk_request(tmp_path,
        target):
    import rpe_harness
    from rpe_harness.integrations.hermes_session import sandbox_profile
    interpreter, environment = runtime_environment(tmp_path)
    hermes_root = interpreter.parents[2]
    source = Path(rpe_harness.__file__).resolve().parent.parent
    bootstrap = source / 'rpe_harness/integrations/hermes_bootstrap.py'
    home = Path(environment['HERMES_HOME'])
    config = json.loads((home / 'config.yaml').read_text())
    config.update(plugins={'enabled': ['rpe-harness'], 'auto_update_check_hours': 0},
        terminal={'backend': 'local', 'cwd': str(tmp_path), 'temp_dir': str(home / 'terminal-tmp')},
        approvals={'single_query_mode': 'approve'}, tools={'tool_search': {'enabled': 'off'}})
    (home / 'config.yaml').write_text(json.dumps(config))
    plugin = home / 'plugins/rpe-harness'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text('name: rpe-harness\nversion: 1.14.0\ndescription: Offline native receipts\n')
    (plugin / '__init__.py').write_text('import os,sys\n'
        'sys.path.insert(0,os.environ["RPE_HARNESS_IMPORT_ROOT"])\n'
        'from rpe_harness.integrations.hermes import register\n'
        'sys.path.pop(0)\n')
    evidence = tmp_path / 'native-evidence.json'
    business = target == 'output.csv'
    evidence.write_text(json.dumps({'workspace': str(tmp_path),
        **({'artifact_name': target} if business else {'draft_name': target})}))
    draft = ('id,value\na,first\n' if business else
        '{"unresolved":["Business key still requires explicit confirmation."]}')
    command = shlex.quote(str(interpreter)) + ' -B -c ' + shlex.quote(
        'from pathlib import Path;Path(' + repr(target) + ').write_text(' + repr(draft) + ')')
    args = tmp_path / 'tool-args.json'
    args.write_text(json.dumps({'command': command, 'timeout': 5}))
    environment.update(PYTHONPATH=str(hermes_root),
        RPE_HERMES_INSTALL_ROOT=str(hermes_root), RPE_HERMES_TOOL_ENABLED='0',
        RPE_HERMES_ENTRY_MODE='optional', RPE_HARNESS_IMPORT_ROOT=str(source),
        RPE_HERMES_DB=str(tmp_path / 'events.sqlite'), RPE_HERMES_WORKSPACE=str(tmp_path),
        RPE_HERMES_PROCESS_PATH=str(home / 'processes.jsonl'),
        RPE_HERMES_ERROR_PATH=str(home / 'observer-errors.jsonl'),
        RPE_HERMES_NATIVE_EVIDENCE_CONFIG=str(evidence), RPE_HERMES_NATIVE_FINISH_ENABLED='1')
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('Write the authorized fixture to ' + target + ' using the terminal.')
    profile = tmp_path / 'bootstrap.sb'
    profile.write_text(sandbox_profile([tmp_path], Path.home() / '.hermes', hermes_root,
        tmp_path / 'events.sqlite', read_roots=[interpreter.parent.parent,
            Path.home() / '.hermes/tools'], native_import_root=source,
        owner_read_scope=(tmp_path.parent, [tmp_path])))
    key_fd = None
    if business:
        key_fd, writer = os.pipe()
        os.write(writer, bytes(range(32)))
        os.close(writer)
        environment['RPE_HERMES_NATIVE_EXECUTION_KEY_FD'] = str(key_fd)
    try:
        with offline_provider_key(environment) as provider_fd:
            child = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), str(interpreter),
                '-B', '-c', _PROGRAM, str(bootstrap), str(prompt), str(args)], env=environment,
                cwd=tmp_path, capture_output=True, text=True, timeout=45,
                pass_fds=(provider_fd, key_fd) if key_fd is not None else (provider_fd,))
    finally:
        if key_fd is not None:
            os.close(key_fd)
    (tmp_path / 'native-finish.stdout.log').write_text(child.stdout)
    (tmp_path / 'native-finish.stderr.log').write_text(child.stderr)
    assert child.returncode == 0, child.stderr[-6000:] + child.stdout[-4000:]
    records = []
    for line in child.stdout.splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            pass
    result = next(row for row in records if row.get('type') == 'result')
    terminal = json.loads(result['text'])
    assert result['exit_code'] == 0
    assert terminal == {'status': 'native_artifact_recorded', 'artifact_name': target,
        'content_sha256': hashlib.sha256(draft.encode()).hexdigest(), 'accepted': False}
    proof = records[-1]
    assert proof['synthetic_api_requests'] == 1 and proof['provider_or_model_requests'] == 0
    assert proof['pre_api_requests'] == proof['post_api_requests'] == 1
    assert proof['pre_tool_calls'] == proof['post_tool_calls'] == 1
    assert proof['native_artifact_receipts'] == proof['session_end_events'] == 1
    assert proof['child_harness_importable'] is False and proof['sdk_preloaded_at_entry'] is False
    assert proof['receipt_authenticated'] is business
    assert proof['key_fd_consumed'] is business
    assert (tmp_path / target).read_text() == draft
    if not business:
        assert not (tmp_path / 'output.csv').exists()


@pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
    reason='installed Hermes native macOS session launcher')
def test_parent_launcher_accepts_same_process_hermes_terminal_receipt(tmp_path, monkeypatch):
    from rpe_harness.integrations import hermes_session
    from rpe_harness.integrations.hermes_native import evaluate_artifact_receipt
    interpreter, _ = runtime_environment(tmp_path)
    hermes_root = interpreter.parents[2]
    if not (hermes_root / 'hermes_cli/gateway_chat_startup.py').is_file():
        pytest.skip('gateway-backed Hermes launcher unavailable')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    content = 'id,value\na,first\n'
    (workspace / 'fixture.csv').write_text(content)
    terminal_probe = workspace / 'credential-boundary.py'
    terminal_probe.write_text('''import ctypes, os, shutil, subprocess
from pathlib import Path
assert not (Path(os.environ['HERMES_HOME']) / '.env').exists()
assert 'XIAOMI_API_KEY' not in os.environ
assert 'RPE_HERMES_PROVIDER_KEY_FD' not in os.environ
pid = int(Path('host-pid').read_text())
try:
    inspected = subprocess.run(['/bin/ps', 'eww', '-p', str(pid)], capture_output=True, timeout=2)
    assert b'offline-not-secret' not in inspected.stdout
except PermissionError:
    pass
# Darwin's argument/environment API also must not reveal the key even when the
# sandbox permits inspecting this same-sandbox parent process.
sysctl = ctypes.CDLL(None, use_errno=True).sysctl
mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
buffer = ctypes.create_string_buffer(262144)
length = ctypes.c_size_t(len(buffer))
if sysctl(mib, 3, buffer, ctypes.byref(length), None, 0) == 0:
    assert b'offline-not-secret' not in buffer.raw[:length.value]
shutil.copyfile('fixture.csv', 'output.csv')
''')
    arguments = {'command': f'{shlex.quote(str(interpreter))} credential-boundary.py', 'timeout': 5}
    args = workspace / 'offline-tool-arguments.json'
    args.write_text(json.dumps(arguments))
    wire_path = workspace / 'offline-wire.json'
    shim = workspace / 'hermes-python-with-offline-transport'
    # The parent uses its actual command, profile, inherited FD and cleanup.
    # The executable shim only injects the same provider-response stub used above.
    prelude = _PROGRAM[:_PROGRAM.index("assert 'run_agent' not in sys.modules")]
    shim.write_text(f'#!{interpreter} -B\n' + prelude.replace('open(sys.argv[3])', f'open({str(args)!r})')
        + '\nimport runpy\nassert sys.argv[1] == "-B"\n'
        'open("host-pid", "w").write(str(os.getpid()))\n'
        'sys.argv = sys.argv[2:]\n'
        'try:\n    runpy.run_path(sys.argv[0], run_name="__main__")\n'
        f'finally:\n    open({str(wire_path)!r}, "w").write(json.dumps(wire))\n')
    shim.chmod(0o700)
    monkeypatch.setattr(hermes_session, '_installed_python', lambda _: shim)
    result = hermes_session.run_hermes_session('Write the authorized fixture to output.csv using the terminal.',
        workspace, tmp_path / 'events.sqlite', tool_enabled=False, owner_root=tmp_path / 'sessions',
        read_isolation_root=tmp_path, hermes_root=hermes_root, public_contract='clean',
        max_task_attempts=1, max_turns=8, timeout=30, native_finish_enabled=True,
        native_evidence={'artifact_name': 'output.csv'}, request_budget=budget_profile(),
        host_settings={'model': 'mimo-v2.6-pro', 'provider': 'xiaomi',
            'api_key': 'offline-not-secret', 'base_url': 'https://example.invalid/v1'})
    session = Path(result['session_root'])
    assert result['completed'], (session / 'stderr.log').read_text()
    assert not result['timed_out'] and result['exit_code'] == 0
    assert result['max_task_attempts'] == 1 and result['max_turns'] == 8
    assert result['process_cleanup']['cleanup_complete'] and not result['process_cleanup']['remaining_descendants']
    assert result['model_identity']['response_models'] == ['mimo-v2.6-pro']
    assert len(json.loads(wire_path.read_text())) == 1
    assert (workspace / 'output.csv').read_text() == content
    [call] = result['stream_tool_calls']
    assert call['name'] == 'terminal' and call['input'] == arguments
    [pre_api] = [event for event in result['events'] if event['kind'] == 'pre_api_request']
    [post_api] = [event for event in result['events'] if event['kind'] == 'post_api_request']
    [receipt] = [event for event in result['events'] if event['kind'] == 'native_artifact_observation']
    [end] = [event for event in result['events'] if event['kind'] == 'on_session_end']
    assert result['session_id'] == pre_api['session_id'] == post_api['session_id'] == receipt['session_id'] == end['session_id']
    assert pre_api['api_request_id'] == post_api['api_request_id'] == receipt['api_request_id']
    assert receipt['tool_call_id'] == 'actual-terminal-call'
    config = json.loads((session / 'native-evidence-config.json').read_text())
    key = (session / 'native-execution-auth.key').read_bytes()
    verdict = evaluate_artifact_receipt(receipt, config, key)
    assert verdict['verified'] and verdict['receipt_authenticated']


_PROGRAM = r'''import importlib.util,json,os,socket,subprocess,sys
def deny(*args,**kwargs): raise AssertionError('network disabled in native finish smoke')
socket.socket.connect=deny
socket.create_connection=deny
import httpx
wire=[]
ancillary=[]
tool_args=json.loads(open(sys.argv[3]).read())
def offline_transport(self,request):
    if request.url.host!='example.invalid' or request.url.path!='/v1/chat/completions':
        ancillary.append({'method':request.method,'host':request.url.host,'path':request.url.path})
        return httpx.Response(200,json={},request=request)
    assert request.headers.get('authorization')=='Bearer offline-not-secret'
    body=json.loads(request.content)
    wire.append({'max_completion_tokens':body.get('max_completion_tokens'),'thinking':body.get('thinking'),
        'stream':body.get('stream'),'timeout':request.extensions['timeout']})
    assert len(wire)==1,'another provider request after native draft receipt'
    assert body['stream'] is True and body['max_completion_tokens']==2400,body
    assert body['thinking']=={'type':'disabled'} and 'max_tokens' not in body,body
    assert request.extensions['timeout']=={'connect':60.0,'read':90.0,'write':90.0,'pool':60.0}
    base={'id':'offline-native-tool-response','created':0,'object':'chat.completion.chunk','model':'mimo-v2.6-pro'}
    chunks=[{**base,'choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[
        {'index':0,'id':'actual-terminal-call','type':'function','function':{'name':'terminal',
            'arguments':json.dumps(tool_args)}}]},'finish_reason':None}]},
        {**base,'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]},
        {**base,'choices':[],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}]
    data=''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
    return httpx.Response(200,headers={'content-type':'text/event-stream'},content=data.encode(),request=request)
httpx.HTTPTransport.handle_request=offline_transport
assert 'run_agent' not in sys.modules and 'openai' not in sys.modules
assert importlib.util.find_spec('rpe_harness') is None,'Harness exposed at native entry'
initial_pythonpath=os.environ['PYTHONPATH']
key_requested='RPE_HERMES_NATIVE_EXECUTION_KEY_FD' in os.environ
spec=importlib.util.spec_from_file_location('actual_native_finish_file_bootstrap',sys.argv[1])
bootstrap=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
try:
    status=bootstrap.main(['chat','--cli','--query-file',sys.argv[2],'--oneshot','-Q',
        '--format','stream-json','--source','rpe-harness','--provider','xiaomi',
        '--model','mimo-v2.6-pro','--toolsets','terminal,file','--max-turns','8',
        '--run-budget','180','--ignore-rules'])
except SystemExit as exc:
    status=exc.code
assert status in (None,0),(status,wire)
assert len(wire)==1,wire
assert os.environ['PYTHONPATH']==initial_pythonpath
assert os.environ['RPE_HARNESS_IMPORT_ROOT'] not in sys.path
child=subprocess.run([sys.executable,'-B','-c',
    "import importlib.util,os;assert importlib.util.find_spec('rpe_harness') is None;assert 'RPE_HERMES_NATIVE_EXECUTION_KEY_FD' not in os.environ"],
    capture_output=True,text=True,timeout=5)
assert child.returncode==0,child.stderr
from rpe_harness.integrations.hermes import HostEventStore
events=HostEventStore(os.environ['RPE_HERMES_DB']).list()
count=lambda kind:sum(row['kind']==kind for row in events)
assert count('api_request_error')==0,events
post=next(row for row in events if row['kind']=='post_tool_call')
receipt=next(row for row in events if row['kind']=='native_artifact_observation')
assert post['tool_name']=='terminal' and post['tool_exit_code']==0,post
assert receipt['verified'] is True and receipt['status']=='created',receipt
assert receipt['tool_call_id']==post['tool_call_id']=='actual-terminal-call'
assert receipt['arguments_sha256']==post['arguments_sha256']
authenticated=False
if key_requested:
    assert 'RPE_HERMES_NATIVE_EXECUTION_KEY_FD' not in os.environ
    from rpe_harness.integrations.hermes_native import evaluate_artifact_receipt
    from rpe_harness.integrations.hermes_native_process import _SIGNING_KEY
    verdict=evaluate_artifact_receipt(receipt,json.loads(open(os.environ['RPE_HERMES_NATIVE_EVIDENCE_CONFIG']).read()),_SIGNING_KEY)
    assert verdict['verified'] is True and verdict['receipt_authenticated'] is True,verdict
    authenticated=True
print(json.dumps({'file_bootstrap':True,'sdk_preloaded_at_entry':False,'child_harness_importable':False,
    'synthetic_api_requests':len(wire),'provider_or_model_requests':0,'pre_api_requests':count('pre_api_request'),
    'post_api_requests':count('post_api_request'),'pre_tool_calls':count('pre_tool_call'),
    'post_tool_calls':count('post_tool_call'),'native_artifact_receipts':count('native_artifact_observation'),
    'session_end_events':count('on_session_end'),'wire':wire,'offline_ancillary_requests':ancillary,
    'receipt_authenticated':authenticated,'key_fd_consumed':key_requested}))
'''
