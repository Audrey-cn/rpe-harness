"""Training exports remain authentic; failed generation keeps original evidence."""
import copy
import hashlib
import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from rpe_harness.cli import main
from rpe_harness.evolution import EvolutionStore
from rpe_harness.evolution.training_feedback import build_training_packet
from rpe_harness.storage import canonical_json
from test_evolution import verified_run
from test_worker import session_result


CODE = 'def prepare(plan, staging_path):\n    raise ValueError("training failure")\n'


@pytest.mark.parametrize('learning', [False, True])
def test_producer_worker_inlines_public_abi_without_read_tools(tmp_path, monkeypatch, learning):
    worker, database, run, packet = setup(tmp_path)
    def generate(prompt, workspace, host_db, **options):
        assert options['entry_mode'] == 'producer' and options['tool_enabled'] is False
        assert 'Read feedback.json' not in prompt
        public, _ = json.JSONDecoder().raw_decode(prompt.split('PUBLIC_ABI_JSON\n')[1])
        assert public['contract']['capability'] == 'tabular.deduplicate'
        if learning:
            assert public['verified_feedback'] == [{}]
        else:
            assert public['verified_feedback'][0]['verified'] is True
        assert ('PUBLIC_DATA_JSON\n' in prompt) is learning
        if learning:
            data = json.loads(prompt.split('PUBLIC_DATA_JSON\n')[1])
            assert data['training_packet'] == packet and data['previous_candidate'] == CODE
            assert data['training_feedback']['outcomes'][0]['candidate_invoked'] is True
        (Path(workspace) / 'candidate.py').write_text(CODE)
        return session_result(events=[{'kind': 'pre_api_request', 'request_tool_choice': 'write_file',
            'request_parallel_tool_calls': False, 'request_tool_names': ['write_file'], 'request_tools_sha256': 'abc'}])
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    options = dict(training_packet=packet, previous_candidate=CODE) if learning else {}
    report = worker.run_proposal_worker(database, tmp_path, kind='csv_executor', producer_only=True, **options)
    assert report['producer_only'] is True and report['entry_mode'] == 'producer'
    assert report['observations'][0]['request_tool_choice'] == 'write_file'
    assert report['observations'][0]['request_parallel_tool_calls'] is False
    assert report['observations'][0]['request_tool_names'] == ['write_file']


@pytest.mark.parametrize('value', [1, 'yes', None])
def test_producer_worker_requires_strict_bool(tmp_path, value):
    worker = importlib.import_module('rpe_harness.evolution.worker')
    with pytest.raises(ValueError):
        worker.run_proposal_worker(tmp_path / 'db.sqlite', tmp_path / 'new', producer_only=value)
    assert list(tmp_path.iterdir()) == []


def test_cli_producer_only_flag_is_explicit_and_retained(tmp_path, monkeypatch, capsys):
    worker, database, _, _ = setup(tmp_path)
    def generate(prompt, workspace, host_db, **options):
        assert options['entry_mode'] == 'producer'
        (Path(workspace) / 'candidate.py').write_text(CODE)
        return session_result()
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    status = main(['evolution', 'worker', '--db', str(database), '--owned-root', str(tmp_path),
        '--kind', 'csv_executor', '--producer-only'])
    report = json.loads(capsys.readouterr().out)
    assert status == 0 and report['status'] == 'proposed'
    assert report['producer_only'] is True and report['entry_mode'] == 'producer'


def setup(root):
    database, run = verified_run(root)
    packet = build_training_packet(database, [run['run_id']], authorize_data_export=True)
    return importlib.import_module('rpe_harness.evolution.worker'), database, run, packet


def test_exact_authorized_examples_and_real_previous_failures_enter_prompt(tmp_path, monkeypatch):
    worker, database, run, packet = setup(tmp_path)
    # Newer real evidence must not silently replace the authorized run.
    verified_run(tmp_path, event_id='newer', output='newer.csv')
    extra = tmp_path.parent / 'other-arm'
    prompts = []
    def generate(prompt, workspace, host_db, **options):
        prompts.append(prompt)
        assert options['max_turns'] == 6
        assert extra in options['deny_read_roots']
        assert worker._deny_read_roots()[0] in options['deny_read_roots']
        assert packet['examples'][0]['source_text'] in json.loads(prompt.split('PUBLIC_DATA_JSON\n')[1])['training_packet']['examples'][0]['source_text']
        public = json.loads(prompt.split('PUBLIC_DATA_JSON\n')[1])
        assert public['previous_candidate'] == CODE
        assert public['training_feedback']['outcomes'][0]['candidate_invoked'] is True
        assert public['training_feedback']['outcomes'][0]['correct'] is False
        assert public['training_feedback']['outcomes'][0]['rule_failures'] == ['candidate_rejected']
        assert str(database) not in prompt and str(tmp_path / 'orders.csv') not in prompt
        (Path(workspace) / 'candidate.py').write_text(CODE)
        return session_result()
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path, kind='csv_executor',
        training_packet=packet, previous_candidate=CODE, deny_read_roots=[extra])
    assert report['completed'] and len(prompts) == 1
    assert report['evidence_run_ids'] == [run['run_id']]
    assert report['training_packet_sha256'] == packet['packet_sha256']
    assert report['training_feedback']['candidate_sha256'] == hashlib.sha256(CODE.encode()).hexdigest()
    assert report['phase_durations']['training_probe_seconds'] >= 0
    assert report['elapsed_seconds'] >= report['phase_durations']['training_probe_seconds']
    assert EvolutionStore(database).get_proposal(report['proposal_id'])['state'] == 'proposed'


@pytest.mark.parametrize('options', [
    {'max_turns': True}, {'max_turns': 0}, {'max_turns': 13}, {'max_turns': 1.5},
    {'previous_candidate': CODE}, {'previous_candidate': b'bytes'},
    {'training_packet': {}}, {'deny_read_roots': 'not-a-list'},
])
def test_invalid_learning_options_reject_before_owned_writes(tmp_path, options):
    worker = importlib.import_module('rpe_harness.evolution.worker')
    with pytest.raises(ValueError):
        worker.run_proposal_worker(tmp_path / 'db.sqlite', tmp_path / 'new-root', **options)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('damage', ['fake', 'source', 'json_kind', 'oversize_code', 'empty_code'])
def test_learning_rejects_untrusted_inputs_before_launcher_or_workspace(tmp_path, monkeypatch, damage):
    worker, database, _, packet = setup(tmp_path)
    kind, previous = 'csv_executor', CODE
    if damage == 'fake':
        packet = copy.deepcopy(packet)
        packet['examples'][0]['expected_rows'][1][1] = 'fabricated'
        packet['packet_sha256'] = hashlib.sha256(canonical_json({k:v for k,v in packet.items() if k != 'packet_sha256'}).encode()).hexdigest()
    elif damage == 'source':
        (tmp_path / 'orders.csv').write_text('changed')
    elif damage == 'json_kind':
        kind = 'json_executor'
    elif damage == 'oversize_code':
        previous = 'x' * 65537
    else:
        previous = ''
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    monkeypatch.setattr(worker, 'run_hermes_session', lambda *a, **k: pytest.fail('invalid input reached model'))
    with pytest.raises(ValueError):
        worker.run_proposal_worker(database, tmp_path, kind=kind, training_packet=packet, previous_candidate=previous)
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before


@pytest.mark.parametrize('failure', ['timeout', 'model_error', 'launcher_error', 'cleanup'])
def test_failed_generation_retains_raw_code_without_proposal(tmp_path, monkeypatch, failure):
    worker, database, _, packet = setup(tmp_path)
    raw = b'# raw CRLF\r\ndef prepare(plan, staging_path):\r\n    pass\r\n'
    def generate(prompt, workspace, host_db, **options):
        (Path(workspace) / 'candidate.py').write_bytes(raw)
        (Path(options['owner_root']) / 'private-history').write_text('SECRET')
        if failure == 'launcher_error':
            raise RuntimeError('PRIVATE_SECRET')
        result = session_result()
        if failure == 'timeout':
            result.update(completed=False, timed_out=True, exit_code=-9)
        elif failure == 'model_error':
            result.update(completed=False, exit_code=1)
        else:
            result['process_cleanup']['cleanup_complete'] = False
        return result
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path, kind='csv_executor', training_packet=packet)
    assert report['proposal_id'] is None and not report['completed']
    assert Path(report['code_artifact_path']).read_bytes() == raw
    assert report['code_artifact_sha256'] == hashlib.sha256(raw).hexdigest()
    assert report['candidate_ast_sha256']
    assert report['generation_link']['proven'] is False
    assert report['usage'] is None and report['workspace_cleaned']
    assert not list(tmp_path.glob('proposal-worker-*'))
    assert 'PRIVATE_SECRET' not in Path(report['report_path']).read_text()


def test_post_generation_packet_drift_prevents_proposal_and_retains_code(tmp_path, monkeypatch):
    worker, database, _, packet = setup(tmp_path)
    def generate(prompt, workspace, host_db, **options):
        (Path(workspace) / 'candidate.py').write_text(CODE)
        (tmp_path / 'result.csv').write_text('drift')
        return session_result()
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path, kind='csv_executor', training_packet=packet)
    assert report['error']['code'] == 'verified_feedback_changed'
    assert report['proposal_id'] is None
    assert Path(report['code_artifact_path']).read_text() == CODE


@pytest.mark.parametrize('damage', [None, 'api_missing', 'pretool_missing', 'args_mismatch', 'content_mismatch', 'tool_failure', 'missing_ids', 'wrong_path', 'file_result_missing', 'unverified_result', 'short_write', 'duplicate_tool', 'duplicate_api', 'api_error'])
def test_generation_identity_links_only_actual_api_and_filewrite_content(tmp_path, monkeypatch, damage):
    worker, database, _, packet = setup(tmp_path)
    identity = {'session_id':'s1', 'task_id':'task1', 'turn_id':'turn1', 'api_request_id':'api1'}
    call = {**identity, 'tool_call_id':'write1', 'tool_name':'write_file'}
    def generate(prompt, workspace, host_db, **options):
        (Path(workspace) / 'candidate.py').write_text(CODE)
        args = {'path':'candidate.py', 'content':CODE}
        hashed = hashlib.sha256(canonical_json(args).encode()).hexdigest()
        events = [
            {'kind':'pre_api_request', **identity},
            {'kind':'post_api_request', **identity, 'model_tool_calls':[{'tool_call_id':'write1','tool_name':'write_file'}]},
            {'kind':'pre_tool_call', **call, 'arguments_sha256':hashed},
            {'kind':'post_tool_call', **call, 'arguments_sha256':hashed, 'status':'ok', 'duration_ms':12, 'result':'SECRET'},
            {'kind':'on_session_end', 'session_id':'s1', 'turn_exit_reason':'completed'},
        ]
        if damage == 'api_missing':
            events[1]['model_tool_calls'] = []
        elif damage == 'pretool_missing':
            events[2]['kind'] = 'unrelated'
        elif damage == 'args_mismatch':
            events[3]['arguments_sha256'] = '0' * 64
        elif damage == 'content_mismatch':
            args['content'] = 'different'
        elif damage == 'tool_failure':
            events[3]['status'] = 'error'
        elif damage == 'missing_ids':
            del events[3]['task_id']
        elif damage == 'wrong_path':
            args['path'] = 'other.py'
        elif damage == 'duplicate_tool':
            events.append(dict(events[3]))
        elif damage == 'duplicate_api':
            events.append(dict(events[1]))
        elif damage == 'api_error':
            events.append({'kind':'api_request_error',**identity,'error_type':'APIError'})
        writes = [{'tool_call_id':'write1','arguments_sha256':hashed,
            'content_sha256':hashlib.sha256(CODE.encode()).hexdigest(),
            'target_path_sha256':hashlib.sha256(str(Path(workspace)/'candidate.py').encode()).hexdigest(),
            'verified':True,'bytes_written':len(CODE.encode())}]
        if damage == 'file_result_missing':
            writes = []
        elif damage == 'unverified_result':
            writes[0]['verified'] = False
        elif damage == 'short_write':
            writes[0]['bytes_written'] -= 1
        return session_result(session_id='s1', events=events, file_write_observations=writes,
            stream_tool_calls=[{'type':'tool_use','name':'write_file','tool_call_id':'write1','input':args}])
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path, kind='csv_executor', training_packet=packet)
    assert report['generation_link']['proven'] is (damage is None)
    if damage is None:
        assert report['generation_link']['api_request_id'] == 'api1'
        assert report['generation_link']['content_sha256'] == report['candidate_sha256']
    assert report['observations'][3]['duration_ms'] == 12
    assert report['termination_stage'] == 'completed'
    saved = Path(report['report_path']).read_text()
    assert 'SECRET' not in saved and '"input"' not in saved


@pytest.mark.parametrize('relative', [False, True])
def test_cli_bounded_packet_and_previous_code_files(tmp_path, monkeypatch, capsys, relative):
    worker, database, _, packet = setup(tmp_path)
    packetfile, codefile = tmp_path / 'packet.json', tmp_path / 'prior.py'
    packetfile.write_text(canonical_json(packet))
    codefile.write_text(CODE)
    def generate(prompt, workspace, host_db, **options):
        assert options['max_turns'] == 2
        (Path(workspace) / 'candidate.py').write_text(CODE)
        return session_result()
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    monkeypatch.chdir(tmp_path)
    rc = main(['evolution','worker','--db',str(database),'--owned-root',str(tmp_path),
        '--kind','csv_executor','--training-packet',packetfile.name if relative else str(packetfile),
        '--previous-candidate',codefile.name if relative else str(codefile),'--max-turns','2'])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report['training_packet_sha256'] == packet['packet_sha256']


def test_launcher_extracts_verified_file_write_without_raw_payload(tmp_path):
    launcher = importlib.import_module('rpe_harness.integrations.hermes_session')
    extract = getattr(launcher, '_file_write_observations', None)
    assert callable(extract), 'verified file-write observations are missing'
    args = {'path':'candidate.py','content':CODE}
    records = [{'type':'tool_use','name':'write_file','tool_call_id':'file1','input':args},
        {'type':'tool_result','name':'write_file','tool_call_id':'file1','duration_ms':12,'is_error':False,
         'output':json.dumps({'verified':True,'bytes_written':len(CODE.encode()),'lint':'PRIVATE_PAYLOAD'})}]
    writes = extract(records, tmp_path)
    assert writes == [{'tool_call_id':'file1','arguments_sha256':hashlib.sha256(canonical_json(args).encode()).hexdigest(),
        'content_sha256':hashlib.sha256(CODE.encode()).hexdigest(),
        'target_path_sha256':hashlib.sha256(str(tmp_path/'candidate.py').encode()).hexdigest(),
        'bytes_written':len(CODE.encode()),'verified':True,'duration_ms':12}]
    assert 'PRIVATE_PAYLOAD' not in json.dumps(writes)
    records[1]['output'] = json.dumps({'verified':False,'bytes_written':len(CODE.encode())})
    assert extract(records, tmp_path) == []
    records[1]['output'] = json.dumps({'verified':True,'bytes_written':len(CODE.encode()),'error':'SECRET'})
    assert extract(records, tmp_path) == []


@pytest.mark.parametrize('turns', [False, 0, 13, 2.5])
def test_launcher_invalid_max_turns_before_any_setup(tmp_path, monkeypatch, turns):
    launcher = importlib.import_module('rpe_harness.integrations.hermes_session')
    monkeypatch.setattr(launcher, '_installed_python', lambda *a: pytest.fail('invalid turns read host settings'))
    with pytest.raises(ValueError):
        launcher.run_hermes_session('test', tmp_path, tmp_path/'db.sqlite',
            tool_enabled=False, owner_root=tmp_path/'new-owner', max_turns=turns)
    assert list(tmp_path.iterdir()) == []


def test_syntax_invalid_code_is_untrusted_proposal_without_ast_claim(tmp_path, monkeypatch):
    worker, database, _, packet = setup(tmp_path)
    def generate(prompt, workspace, host_db, **options):
        (Path(workspace) / 'candidate.py').write_bytes(b'def invalid(:\r\n')
        return session_result()
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path, kind='csv_executor', training_packet=packet)
    assert report['completed'] and report['proposal_id']
    assert report['candidate_ast_sha256'] is None
    assert Path(report['code_artifact_path']).read_bytes() == b'def invalid(:\r\n'
    assert EvolutionStore(database).get_proposal(report['proposal_id'])['validation'] is None


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(), reason='macOS real host launcher')
@pytest.mark.parametrize('turns', [None, 2])
def test_real_launcher_forwards_turn_bound_without_model(tmp_path, monkeypatch, turns):
    from rpe_harness.integrations import hermes, hermes_session as launcher
    integrations = tmp_path / 'runtime/lib/python/site-packages/rpe_harness/integrations'
    integrations.mkdir(parents=True)
    import_root = integrations.parents[1]
    (integrations.parent / '__init__.py').write_text('')
    (integrations / '__init__.py').write_text('')
    shutil.copyfile(hermes.__file__, integrations / 'hermes.py')
    program = '''import json,os,sys
sys.path.insert(0,IMPORT_ROOT)
from rpe_harness.integrations.hermes import HostEventStore
HostEventStore(os.environ['RPE_HERMES_DB']).record('on_session_end',{'session_id':'turn-probe','completed':True})
print(json.dumps({'type':'result','session_id':'turn-probe','text':json.dumps({'turns':sys.argv[sys.argv.index('--max-turns')+1],'tools':sys.argv[sys.argv.index('--toolsets')+1]})}))
'''
    (integrations / 'hermes_bootstrap.py').write_text('IMPORT_ROOT=' + repr(str(import_root)) + '\n' + program)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(launcher, '__file__', str(integrations / 'hermes_session.py'))
    monkeypatch.setattr(launcher, '_installed_python', lambda *a: Path(sys.executable).resolve())
    monkeypatch.setattr(launcher, '_read_host_settings', lambda *a: {'provider':'xiaomi','model':'no-model',
        'base_url':'https://example.invalid', 'api_key':'isolated-no-model-test-key'})
    options = {} if turns is None else {'max_turns':turns}
    result = launcher.run_hermes_session('No model: check turn bound', workspace, tmp_path/'host.sqlite',
        tool_enabled=False, owner_root=tmp_path/'owner', hermes_root=tmp_path/'fake-host', timeout=5, **options)
    try:
        assert result['completed'], result
        assert json.loads(result['final_response']) == {'turns':'12' if turns is None else '2', 'tools':'terminal,file'}
        assert result['usage']['api_requests'] == 0
    finally:
        shutil.rmtree(result['session_root'])


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(), reason='macOS real generator read boundary')
def test_worker_denies_original_training_source_outside_owned_root_and_explicit_other_roots(tmp_path, monkeypatch):
    from rpe_harness.integrations import hermes, hermes_session as launcher
    from rpe_harness.runtime import HarnessRuntime
    owned = tmp_path / 'owned'
    owned.mkdir()
    source = tmp_path / 'external-training.csv'
    source.write_text('id,value\n1,first\n1,later\n')
    database = owned / 'runs.sqlite'
    run = HarnessRuntime(db_path=database).run({'schema_version':1,'event_id':'external-training',
        'state':{'task_type':'data.processing','goal':'deduplicate','input_format':'csv',
                 'required_capability':'tabular.deduplicate','constraints':['preserve_original','preserve_order']},
        'execution':{'input_path':str(source),'output_path':str(owned/'output.csv'),'key_columns':['id']}})
    assert run['status'] == 'succeeded'
    packet = build_training_packet(database, [run['run_id']], authorize_data_export=True)
    extra = tmp_path / 'evaluator-and-other-arm'
    extra.mkdir()
    (extra / 'private.py').write_text('PRIVATE_ORACLE')
    integrations = tmp_path / 'runtime/lib/python/site-packages/rpe_harness/integrations'
    integrations.mkdir(parents=True)
    import_root = integrations.parents[1]
    (integrations.parent / '__init__.py').write_text('')
    (integrations / '__init__.py').write_text('')
    shutil.copyfile(hermes.__file__, integrations / 'hermes.py')
    program = '''import json,os,sys
from pathlib import Path
sys.path.insert(0,IMPORT_ROOT)
from rpe_harness.integrations.hermes import HostEventStore
denied=[]
for path in PRIVATE_PATHS:
    try: Path(path).read_bytes(); denied.append(False)
    except PermissionError: denied.append(True)
assert denied == [True,True,True], denied
workspace=Path(os.environ['RPE_HERMES_WORKSPACE'])
assert json.loads((workspace/'feedback.json').read_text())['kind']=='csv_executor'
(workspace/'candidate.py').write_text(CODE)
HostEventStore(os.environ['RPE_HERMES_DB']).record('on_session_end',{'session_id':'external-source-probe','completed':True})
print(json.dumps({'type':'result','session_id':'external-source-probe','text':'denied original source and explicit roots'}))
'''
    (integrations / 'hermes_bootstrap.py').write_text('IMPORT_ROOT=' + repr(str(import_root)) + '\nPRIVATE_PATHS=' + repr([str(source),str(database),str(extra/'private.py')]) + '\nCODE=' + repr(CODE) + '\n' + program)
    monkeypatch.setattr(launcher, '__file__', str(integrations / 'hermes_session.py'))
    monkeypatch.setattr(launcher, '_installed_python', lambda *a: Path(sys.executable).resolve())
    monkeypatch.setattr(launcher, '_read_host_settings', lambda *a: {'provider':'xiaomi','model':'no-model',
        'base_url':'https://example.invalid', 'api_key':'isolated-no-model-test-key'})
    worker = importlib.import_module('rpe_harness.evolution.worker')
    report = worker.run_proposal_worker(database, owned, kind='csv_executor', training_packet=packet,
        deny_read_roots=[extra], hermes_root=tmp_path/'fake-host', timeout=5)
    assert report['completed'], report
    assert report['usage']['api_requests'] == 0
    assert report['generation_link']['proven'] is False
    assert report['workspace_cleaned'] and not list(owned.glob('proposal-worker-*'))


def test_unresolvable_tool_path_keeps_receipt_code_and_cleans_private_root(tmp_path, monkeypatch):
    worker, database, _, packet = setup(tmp_path)
    def generate(prompt, workspace, host_db, **options):
        (Path(workspace) / 'candidate.py').write_text(CODE)
        (Path(workspace) / 'loop').symlink_to('loop')
        return session_result(stream_tool_calls=[{'type':'tool_use','name':'write_file','tool_call_id':'bad-path',
            'input':{'path':'loop/candidate.py','content':CODE}}])
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path, kind='csv_executor', training_packet=packet)
    assert report['generation_link']['proven'] is False
    assert report['workspace_cleaned'] and not list(tmp_path.glob('proposal-worker-*'))
    assert Path(report['code_artifact_path']).read_text() == CODE
    assert Path(report['report_path']).is_file()


def test_launcher_unresolvable_verified_write_path_is_unproven(tmp_path):
    launcher = importlib.import_module('rpe_harness.integrations.hermes_session')
    (tmp_path / 'loop').symlink_to('loop')
    records = [{'type':'tool_use','name':'write_file','tool_call_id':'bad-path',
                'input':{'path':'loop/candidate.py','content':CODE}},
        {'type':'tool_result','name':'write_file','tool_call_id':'bad-path','is_error':False,
         'output':json.dumps({'verified':True,'bytes_written':len(CODE.encode())})}]
    assert launcher._file_write_observations(records, tmp_path) == []


def test_unexpected_provenance_failure_cannot_skip_private_cleanup_or_receipt(tmp_path, monkeypatch):
    worker, database, _, packet = setup(tmp_path)
    def generate(prompt, workspace, host_db, **options):
        (Path(workspace) / 'candidate.py').write_text(CODE)
        return session_result()
    def fail(*args):
        raise RuntimeError('PRIVATE_DIAGNOSTIC_FAILURE')
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    monkeypatch.setattr(worker, '_generation_link', fail)
    report = worker.run_proposal_worker(database, tmp_path, kind='csv_executor', training_packet=packet)
    assert report['generation_link']['proven'] is False
    assert report['error']['code'] == 'provenance_diagnostics_failed'
    assert report['proposal_id'] is None
    assert report['workspace_cleaned'] and not list(tmp_path.glob('proposal-worker-*'))
    assert Path(report['code_artifact_path']).read_text() == CODE
    assert 'PRIVATE_DIAGNOSTIC_FAILURE' not in Path(report['report_path']).read_text()


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(), reason='macOS real postlaunch diagnostics')
@pytest.mark.parametrize('fault', ['credential', 'extractor', 'redaction', 'stdout_read', 'metadata', 'terminal_identity', 'public_terminal_identity'])
def test_postlaunch_failure_preserves_observed_cost_identity_and_cleanup_without_secrets(tmp_path, monkeypatch, fault):
    from rpe_harness.integrations import hermes, hermes_session as launcher
    worker, database, _, packet = setup(tmp_path / 'owned')
    owned = database.parent
    integrations = tmp_path / 'runtime/lib/python/site-packages/rpe_harness/integrations'
    integrations.mkdir(parents=True)
    import_root = integrations.parents[1]
    (integrations.parent / '__init__.py').write_text('')
    (integrations / '__init__.py').write_text('')
    shutil.copyfile(hermes.__file__, integrations / 'hermes.py')
    dummy = 'isolated-dummy-key-for-redaction-test'
    program = '''import json,os,sys
from pathlib import Path
sys.path.insert(0,IMPORT_ROOT)
from rpe_harness.integrations.hermes import HostEventStore
store=HostEventStore(os.environ['RPE_HERMES_DB'])
identity={'session_id':'observed-session','task_id':'observed-task','turn_id':'observed-turn','api_request_id':'observed-api'}
store.record('pre_api_request',identity)
store.record('post_api_request',{**identity,'response_model':RESPONSE_MODEL,'usage':{'total_tokens':11,'present_fields':['total_tokens'],'raw_provider_usage':{'total_tokens':11}}})
store.record('on_session_end',{'session_id':'observed-session','completed':True})
(Path(os.environ['RPE_HERMES_WORKSPACE'])/'candidate.py').write_text(CODE)
print(json.dumps({'type':'result','session_id':TERMINAL_SESSION,'text':RESULT_TEXT}))
'''
    (integrations / 'hermes_bootstrap.py').write_text('IMPORT_ROOT=' + repr(str(import_root)) + '\nCODE=' + repr(CODE) +
        '\nRESULT_TEXT=' + repr(dummy if fault in {'credential','redaction'} else 'safe-response') +
        '\nRESPONSE_MODEL=' + repr(dummy if fault == 'metadata' else 'observed-model') +
        '\nTERMINAL_SESSION=' + repr(dummy if fault == 'terminal_identity' else
            'wrong-public-session' if fault == 'public_terminal_identity' else 'observed-session') + '\n' + program)
    monkeypatch.setattr(launcher, '__file__', str(integrations / 'hermes_session.py'))
    monkeypatch.setattr(launcher, '_installed_python', lambda *a: Path(sys.executable).resolve())
    monkeypatch.setattr(launcher, '_read_host_settings', lambda *a: {'provider':'xiaomi','model':'no-model',
        'base_url':'https://example.invalid', 'api_key':dummy})
    if fault == 'extractor':
        def fail(*args):
            raise RuntimeError('PRIVATE_EXTRACTOR_FAILURE')
        monkeypatch.setattr(launcher, '_file_write_observations', fail)
    elif fault == 'redaction':
        original = Path.write_text
        def fail_redaction(path, data, *args, **kwargs):
            if path.name == 'stdout.jsonl' and '[REDACTED]' in data:
                raise OSError('PRIVATE_REDACTION_FAILURE')
            return original(path, data, *args, **kwargs)
        monkeypatch.setattr(Path, 'write_text', fail_redaction)
    elif fault == 'stdout_read':
        original = Path.read_text
        def fail_read(path, *args, **kwargs):
            if path.name == 'stdout.jsonl':
                raise OSError('PRIVATE_TRANSCRIPT_READ_FAILURE')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', fail_read)
    carriers = []
    def launch(*args, **kwargs):
        if fault == 'public_terminal_identity':
            # Previous observations in this DB must not inflate this attempt.
            store = hermes.HostEventStore(args[2])
            prior = {'session_id':'observed-session','task_id':'previous-task',
                     'turn_id':'previous-turn','api_request_id':'previous-api'}
            store.record('pre_api_request', prior)
            store.record('post_api_request', {**prior,'usage':{'total_tokens':997,
                'present_fields':['total_tokens'],'raw_provider_usage':{'total_tokens':997}}})
        try:
            return launcher.run_hermes_session(*args, **kwargs)
        except Exception as error:
            carriers.append(error)
            raise
    monkeypatch.setattr(worker, 'run_hermes_session', launch)
    report = worker.run_proposal_worker(database, owned, kind='csv_executor', training_packet=packet,
        hermes_root=tmp_path/'fake-host', timeout=5)
    assert report['usage'] is not None
    assert report['usage']['known_total_tokens'] == 11
    assert report['usage']['api_requests'] == 1
    assert report['usage']['total_tokens'] is None and report['usage']['coverage_complete'] is False
    assert report['session_id'] == 'observed-session'
    assert report['model_identity']['response_models'] == ([] if fault == 'metadata' else ['observed-model'])
    assert any(e.get('api_request_id') == 'observed-api' for e in report['observations'])
    assert not any(e.get('api_request_id') == 'previous-api' for e in report['observations'])
    assert report['process_cleanup']['cleanup_complete'] is True
    assert report['process_cleanup']['remaining_descendants'] == []
    assert report['workspace_cleaned'] and not list(owned.glob('proposal-worker-*'))
    assert report['proposal_id'] is None and not report['completed']
    assert Path(report['code_artifact_path']).read_text() == CODE
    serialized = Path(report['report_path']).read_text()
    assert dummy not in serialized and 'PRIVATE_' not in serialized
    assert len(carriers) == 1
    assert isinstance(carriers[0], getattr(launcher, 'SessionObservationError', ()))
    assert dummy not in str(carriers[0])
    assert dummy not in json.dumps(carriers[0].diagnostics)
    if fault == 'public_terminal_identity':
        assert carriers[0].code == 'terminal_session_mismatch'
        assert report['error']['reason'] == 'terminal_session_mismatch'
    assert 'stream_tool_calls' not in carriers[0].diagnostics and 'final_response' not in carriers[0].diagnostics


def test_safe_observation_error_preserves_pids_and_drops_malformed_private_fields():
    launcher = importlib.import_module('rpe_harness.integrations.hermes_session')
    secret = 'dummy-private-key'
    error = launcher.SessionObservationError({'session_id':'known-session',
        'session_root':'/tmp/' + 'x' * 280,
        'usage':{'api_requests':1,'known_total_tokens':11,'total_tokens':11,'raw_provider_usage':{'secret':secret}},
        'model_identity':{'provider':'xiaomi','requested_model':'known-model','response_models':[secret,'known-model',{}]},
        'events':[{'kind':'post_api_request','api_request_id':'known-api','status':secret,'arguments':secret,
                   'duration_ms':{},'model_tool_calls':[None,{'tool_call_id':'known-call','tool_name':'write_file'}]}],
        'process_cleanup':{'tracked_descendants':2,'signaled_descendants':0,'remaining_descendants':[12345,23456],
            'cleanup_complete':False,'external_executor_cleanup_complete':True,
            'external_executor_tracked_children':1,'external_executor_remaining_children':[34567],
            'errors':[{'stage':'verify_known_descendants','error_type':'ValueError','message':secret}]},
        'final_response':secret,'stream_tool_calls':[{'input':secret}]}, secret=secret)
    assert secret not in str(error) and secret not in json.dumps(error.diagnostics)
    assert error.diagnostics['process_cleanup']['remaining_descendants'] == [12345,23456]
    assert error.diagnostics['process_cleanup']['external_executor_remaining_children'] == [34567]
    assert error.diagnostics['model_identity']['response_models'] == ['known-model']
    assert error.diagnostics['usage']['known_total_tokens'] == 11
    assert error.diagnostics['usage']['total_tokens'] is None
    assert len(error.diagnostics['session_root']) > 256
    assert 'raw_provider_usage' not in error.diagnostics['usage']
    assert 'duration_ms' not in error.diagnostics['events'][0]
