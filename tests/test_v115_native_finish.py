"""Stage-aware post-tool stop uses real helper exits and authenticated receipts."""
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

from test_native_staged_process_evidence import invoke, staged_case as staged_case


@pytest.fixture
def staged_finish_receipt(staged_case, monkeypatch):
    from rpe_harness.integrations import hermes_native_process as native
    config, argv = staged_case
    monkeypatch.setattr(native, '_SIGNING_KEY', b'f'*32)
    child, receipt = invoke(config, shlex.join(argv)+' > helper-stdout.json; cp helper-stdout.json output.json')
    assert child.returncode == 0 and receipt['verified'] is True, receipt
    return config, receipt


def finish(config, receipt):
    from rpe_harness.integrations.hermes import _metadata
    from rpe_harness.integrations.hermes_native_finish import NativeTurnFinisher
    ids = {key: receipt[key] for key in ('session_id', 'task_id', 'turn_id', 'api_request_id')}
    call = {**ids, **{key: receipt[key] for key in ('tool_call_id', 'tool_name', 'arguments_sha256')}}
    finisher = NativeTurnFinisher(execution_config=config)
    finisher.record('pre_api_request', _metadata(ids))
    finisher.record('post_api_request', _metadata({**ids, 'response': {'assistant_message': {'tool_calls': [
        {'id': call['tool_call_id'], 'name': call['tool_name']}]}}}))
    finisher.record('pre_tool_call', _metadata(call))
    finisher.record('post_tool_call', _metadata({**call, 'status': 'succeeded', 'tool_exit_code': 0}))
    finisher.record(receipt['kind'], _metadata(receipt))
    agent = SimpleNamespace(session_id=ids['session_id'], _current_turn_id=ids['turn_id'],
        _current_api_request_id=ids['api_request_id'])
    return finisher.finish(agent, effective_task_id=ids['task_id'],
        messages=[{'role': 'tool', 'tool_call_id': call['tool_call_id'], 'content': '{}'}])


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
def test_staged_native_finish_stops_on_complete_authenticated_same_call(staged_finish_receipt):
    config, receipt = staged_finish_receipt
    assert finish(config, receipt) == {'status': 'native_artifact_recorded',
        'artifact_name': 'output.json', 'content_sha256': receipt['output_sha256'], 'accepted': False}


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
@pytest.mark.parametrize('fault', ['changed_stage', 'deleted_stage', 'replaced_stage', 'metadata_omission'])
def test_staged_native_finish_rechecks_source_and_all_signed_metadata(staged_finish_receipt, monkeypatch, fault):
    from rpe_harness.integrations import hermes
    config, receipt = staged_finish_receipt
    # The identical real call and complete API/tool/tail chain are sufficient
    # before each fault. A rejection below therefore exercises this boundary.
    assert finish(config, receipt) is not None
    stage = Path(config['workspace'])/config['stdout_name']
    if fault == 'changed_stage':
        stage.write_bytes(b'changed')
    elif fault == 'deleted_stage':
        stage.unlink()
    elif fault == 'replaced_stage':
        replacement = stage.with_name('replacement.json')
        replacement.write_bytes(stage.read_bytes())
        replacement.replace(stage)
    else:
        monkeypatch.setattr(hermes, '_TEXT_FIELDS', hermes._TEXT_FIELDS - {'stdout_sha256'})
    assert finish(config, receipt) is None
