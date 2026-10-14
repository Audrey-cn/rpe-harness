"""Independent rescore catches SDK stream identity loss without inventing IDs."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def api():
    path = Path(__file__).parents[1] / 'scripts/v16_rescore_native.py'
    assert path.is_file(), 'supplemental rescorer absent'
    spec = importlib.util.spec_from_file_location('rescore_native', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence(api, duplicate=False):
    args = {'name': 'xlsx'}
    events, streams = [], []
    for index in range(2 if duplicate else 1):
        identity = {'session_id': 's', 'task_id': 't', 'turn_id': 'turn',
                    'api_request_id': 'api-' + str(index)}
        tool = {**identity, 'tool_call_id': 'call-' + str(index), 'tool_name': 'skill_view',
                'arguments_sha256': api.digest(args)}
        events.extend([{**identity, 'kind': 'pre_api_request'},
                       {**identity, 'kind': 'post_api_request', 'model_tool_calls': [
                           {'tool_name': 'skill_view', 'tool_call_id': tool['tool_call_id']}]},
                       {**tool, 'kind': 'pre_tool_call'}, {**tool, 'kind': 'post_tool_call', 'status': 'loaded'}])
        streams.append({'name': 'skill_view', 'input': args, 'timestamp': index})
    return events, streams


def test_module_exists():
    assert (Path(__file__).parents[1] / 'scripts/v16_rescore_native.py').is_file()


@pytest.mark.parametrize('duplicate', [False, True])
def test_stream_without_ids_matches_equivalence_classes(api, duplicate):
    events, streams = evidence(api, duplicate)
    proof = api.receipt_links(events, streams)
    assert proof['passed'] is True
    assert proof['stream_has_call_ids'] is False
    assert proof['equivalence_classes'][0]['count'] == (2 if duplicate else 1)
    assert 'tool_call_id' not in proof['equivalence_classes'][0]


@pytest.mark.parametrize('fault', ['api_id', 'post_id', 'args', 'missing_stream', 'extra_stream'])
def test_receipts_reject_broken_identity_or_multisets(api, fault):
    events, streams = evidence(api)
    if fault == 'api_id':
        events[1]['model_tool_calls'][0]['tool_call_id'] = 'wrong'
    elif fault == 'post_id':
        events[-1]['tool_call_id'] = 'wrong'
    elif fault == 'args':
        streams[0]['input'] = {'name': 'wrong'}
    elif fault == 'missing_stream':
        streams.clear()
    else:
        streams.append(dict(streams[0]))
    assert api.receipt_links(events, streams)['passed'] is False


@pytest.mark.parametrize('fault', [None, 'partial', 'wrong_id', 'empty_id', 'incomplete_api'])
def test_rescore_validates_ids_when_stream_reports_them(api, fault):
    events, streams = evidence(api, duplicate=True)
    for index, stream in enumerate(streams):
        stream['tool_call_id'] = 'call-' + str(index)
    if fault == 'partial':
        streams[1].pop('tool_call_id')
    elif fault == 'wrong_id':
        streams[1]['tool_call_id'] = 'wrong'
    elif fault == 'empty_id':
        streams[1]['tool_call_id'] = ''
    elif fault == 'incomplete_api':
        events.append({'kind': 'pre_api_request', 'session_id': 's', 'task_id': 't',
                       'turn_id': 'turn', 'api_request_id': 'missing'})
    assert api.receipt_links(events, streams)['passed'] is (fault is None)


@pytest.mark.parametrize('suffix,want', [('', 'strict'),
    ('; echo "exit=$?"; ls -la output.json input.xlsx', 'invocation_seen_success_unknown'),
    ('; echo fabricated > output.json', 'unproven'), ('; rm output.json', 'unproven')])
def test_helper_prefix_preserves_strict_shell_rules(api, suffix, want):
    command = 'cd /owned/work && /venv/python /owned/skill/scripts/xlsx_read.py input.xlsx --json > output.json'
    proof = api.helper_command(command + suffix, helper_python='/venv/python',
        helper_path='/owned/skill/scripts/xlsx_read.py', workspace='/owned/work', mode='rows', sheet=None)
    assert proof == want


def test_interrupted_attempt_is_retained_outside_quality_denominator(api, tmp_path):
    owned = tmp_path / 'owned'
    records = owned / 'records'
    records.mkdir(parents=True)
    workspace = owned / 'work'
    workspace.mkdir()
    source = workspace / 'input.xlsx'
    source.write_bytes(b'fixture')
    plan = {'fingerprint': 'frozen', 'owned_root': str(owned), 'cases': [
        {'case_id': 'rows', 'expected': {'rows': []}}], 'sessions': [
        {'trial_id': 'pending', 'arm': 'native', 'case_id': 'rows', 'workspace': str(workspace),
         'input_sha256': api.sha(source)}]}
    (records / 'pending.json').write_text(json.dumps({'status': 'interrupted', 'accepted': False}))
    path = tmp_path / 'preregistration.json'
    path.write_text(json.dumps(plan))
    report = api.rescore(path)
    assert report['recorded_sessions'] == 1
    assert report['arms']['native']['finalized_sessions'] == 0
    assert report['arms']['native']['business_artifact_accuracy'] is None
