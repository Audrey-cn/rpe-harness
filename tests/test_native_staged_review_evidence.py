"""New staged native gate with real helper/kernel/tool observations, no provider."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_review_native_evidence import helper_execution_protocol


@pytest.fixture
def authenticated_staged_execution(helper_execution_protocol, monkeypatch):
    from rpe_harness.integrations import hermes_native_process as native
    from rpe_harness.integrations.hermes import HostEventStore, register
    plan, trial, session, source, command = helper_execution_protocol
    workspace = Path(trial['workspace'])
    (workspace/'output.json').unlink()
    plan['native_stdout_name'] = 'helper-stdout.json'
    case = next(row for row in plan['cases'] if row['case_id'] == trial['case_id'])
    config = {'workspace': str(workspace), 'helper_path': str(source.parent/'scripts/xlsx_read.py'),
        'skill_path': str(source), 'snapshot_id': plan['skill_snapshot_id'],
        'skill_id': 'productivity/xlsx', 'helper_python': plan['helper_python'],
        'helper_python_sha256': plan['environment']['helper_python_identity']['executable_sha256'],
        'skill_sha256': plan['skill_sha256'], 'script_sha256': plan['script_sha256'],
        'input_name': case['input_name'], 'input_sha256': trial['input_sha256'],
        'output_name': case['output_name'], 'stdout_name': plan['native_stdout_name'],
        'parameters': case['parameters']}
    config_path = session/'native-execution-config.json'
    config_path.write_text(json.dumps(config))
    key = b'v'*32
    (session/'native-execution-auth.key').write_bytes(key)
    monkeypatch.setattr(native, '_SIGNING_KEY', key)
    for name, value in {'RPE_HERMES_DB': trial['db'], 'RPE_HERMES_WORKSPACE': str(workspace),
        'RPE_HERMES_TOOL_ENABLED': '0', 'RPE_HERMES_NATIVE_EXECUTION_CONFIG': str(config_path)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('RPE_HERMES_NATIVE_EVIDENCE_CONFIG', raising=False)
    class Context:
        def __init__(self): self.hooks = {}
        def register_hook(self, name, callback): self.hooks[name] = callback
    context = Context()
    register(context)
    ids = {'session_id': 'staged-authenticated-unit', 'task_id': 'task', 'turn_id': 'turn', 'api_request_id': 'api'}
    context.hooks['pre_api_request'](**ids)
    context.hooks['post_api_request'](**ids, response={'assistant_message': {'tool_calls': [
        {'id': 'view', 'name': 'skill_view'}, {'id': 'execute', 'name': 'terminal'}]}})
    view = {**ids, 'tool_call_id': 'view', 'tool_name': 'skill_view', 'args': {'name': 'xlsx'}}
    context.hooks['pre_tool_call'](**view)
    context.hooks['post_tool_call'](**view, status='loaded', skill_id='productivity/xlsx',
        snapshot_id=plan['skill_snapshot_id'], content_sha256=plan['skill_sha256'], skill_source_path=str(source))
    command = command[:command.index(' > output.json')]+' > helper-stdout.json; cp helper-stdout.json output.json'
    execute = {**ids, 'tool_call_id': 'execute', 'tool_name': 'terminal', 'args': {'command': command}}
    context.hooks['pre_tool_call'](**execute)
    child = subprocess.run(['/bin/bash', '-c', command], cwd=workspace, capture_output=True, text=True,
        env={key: value for key, value in os.environ.items() if not key.startswith(('PYTHON', 'DYLD_', 'LD_'))})
    assert child.returncode == 0, child.stderr
    context.hooks['post_tool_call'](**execute, status='ok', result=json.dumps({'exit_code': child.returncode}))
    store = HostEventStore(trial['db'])
    result = {'session_id': ids['session_id'], 'session_root': str(session), 'events': store.list(ids['session_id']),
        'stream_tool_calls': [{'name': 'skill_view', 'input': view['args']}, {'name': 'terminal', 'input': execute['args']}]}
    receipt, = [row for row in result['events'] if row['kind'] == 'native_helper_process_observation']
    assert receipt['verified'] is True, receipt
    return result, plan, trial, store


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
def test_v115_gate_accepts_authenticated_stage_and_final_business_output(authenticated_staged_execution):
    from rpe_harness.experiments import review_native_evidence as review
    result, plan, trial, _store = authenticated_staged_execution
    gate = review.evaluate_native_review_execution_v115(result, plan, trial)
    assert gate['verified'] is True and gate['receipt_authenticated'] is True, gate
    assert gate['artifact_binding'] == 'same_call_final_bytes_equal_observed_stdout_not_exclusive_writer'
    assert gate['stdout_sha256'] == gate['output_sha256']


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
@pytest.mark.parametrize('version', ['v113', 'v114'])
def test_old_native_gate_does_not_accept_new_staged_protocol(authenticated_staged_execution, version):
    from rpe_harness.experiments import review_native_evidence as review
    result, plan, trial, _store = authenticated_staged_execution
    gate = getattr(review, 'evaluate_native_review_execution_'+version)(result, plan, trial)
    assert gate['verified'] is False, gate
    assert gate['errors'] == ['native_process_configuration_changed'], gate


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
@pytest.mark.parametrize('fault', ['stage_bytes', 'stage_inode', 'stage_missing', 'stage_hardlink',
    'final_bytes', 'wrong_plan_stdout', 'missing_plan_stdout', 'receipt_stdout_hash',
    'receipt_stdout_path', 'receipt_binding', 'receipt_kernel_exit'])
def test_v115_gate_rejects_tampering_or_missing_staged_contract(authenticated_staged_execution, fault):
    from rpe_harness.experiments import review_native_evidence as review
    from rpe_harness.integrations.hermes_native_process import receipt_signature
    result, plan, trial, store = authenticated_staged_execution
    stage = Path(trial['workspace'])/'helper-stdout.json'
    if fault == 'stage_bytes':
        stage.write_bytes(b'changed')
    elif fault == 'stage_inode':
        replacement = stage.with_name('replacement.json')
        replacement.write_bytes(stage.read_bytes())
        replacement.replace(stage)
    elif fault == 'stage_missing':
        stage.unlink()
    elif fault == 'stage_hardlink':
        os.link(stage, stage.with_name('alias.json'))
    elif fault == 'final_bytes':
        (Path(trial['workspace'])/'output.json').write_bytes(b'changed')
    elif fault == 'wrong_plan_stdout':
        plan['native_stdout_name'] = 'other.json'
    elif fault == 'missing_plan_stdout':
        del plan['native_stdout_name']
    else:
        row = next(row for row in result['events'] if row['kind'] == 'native_helper_process_observation')
        with store._connect() as database:
            raw, = database.execute('SELECT metadata_json FROM hermes_host_events WHERE ingest_event_id=?',
                (row['ingest_event_id'],)).fetchone()
            metadata = json.loads(raw)
            field, value = {'receipt_stdout_hash': ('stdout_sha256', '0'*64),
                'receipt_stdout_path': ('stdout_path_sha256', '0'*64),
                'receipt_binding': ('artifact_binding', 'final_bytes_on_observed_stdout_inode_not_exclusive_writer'),
                'receipt_kernel_exit': ('kernel_exit_observed', False)}[fault]
            metadata[field] = value
            # A valid host signature is still insufficient when paths, hashes,
            # proof scope or the required actual kernel exit do not bind.
            metadata.pop('process_receipt_hmac_sha256')
            metadata['process_receipt_hmac_sha256'] = receipt_signature(metadata, b'v'*32)
            database.execute('UPDATE hermes_host_events SET metadata_json=? WHERE ingest_event_id=?',
                (json.dumps(metadata), row['ingest_event_id']))
        result['events'] = store.list(result['session_id'])
    gate = review.evaluate_native_review_execution_v115(result, plan, trial)
    assert gate['verified'] is False, gate
