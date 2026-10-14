"""Producer provenance uses real guarded-write metadata when CLI omits IDs."""
import copy

import pytest

from rpe_harness.evolution.worker import _generation_link
from rpe_harness.integrations.hermes import HostEventStore
from test_hermes_producer import CODE, IDS, api, producer, write


def guarded_result(tmp_path, monkeypatch):
    # Exercise the actual middleware, physical file check, hooks and durable
    # metadata; only the upstream model response is represented locally.
    context, workspace, database = producer(tmp_path, monkeypatch)
    api(context)
    write(context, workspace)
    return workspace, {
        'entry_mode': 'producer', 'session_id': IDS['session_id'],
        'events': HostEventStore(database).list(IDS['session_id']),
        # Hermes CLI currently exposes args without its native call identity.
        'stream_tool_calls': [{'name': 'write_file',
            'input': {'path': 'candidate.py', 'content': CODE}}],
        'file_write_observations': [],
    }


@pytest.mark.parametrize('stream_identity', [None, 'call'])
def test_producer_links_actual_guarded_write_without_cli_result_identity(tmp_path, monkeypatch, stream_identity):
    workspace, result = guarded_result(tmp_path, monkeypatch)
    if stream_identity is not None:
        result['stream_tool_calls'][0]['tool_call_id'] = stream_identity
    proof = _generation_link(result, workspace, CODE.encode())
    assert proof['proven'] is True
    assert proof['proof_source'] == 'producer_post_tool_guard'
    assert proof['tool_call_id'] == 'call'
    assert proof['api_request_id'] == 'api'
    assert proof['file_write_verified'] is True
    assert proof['bytes_written'] == len(CODE.encode())


@pytest.mark.parametrize('damage', [
    'nonproducer', 'missing_entry', 'wrong_stream_id', 'empty_stream_id',
    'wrong_stream_content', 'wrong_stream_path', 'duplicate_stream',
    'verified_false', 'verified_nonbool', 'verified_missing',
    'wrong_bytes', 'bytes_float', 'bytes_bool', 'wrong_content_hash',
    'wrong_arguments_hash', 'wrong_target_hash', 'missing_native_id',
    'wrong_session', 'failed_tool', 'duplicate_post', 'ambiguous_post',
    'duplicate_pre', 'missing_pre', 'wrong_pre_args', 'duplicate_api',
    'missing_api', 'missing_emission', 'duplicate_emission', 'api_error',
    'wrong_request_tools', 'wrong_request_choice', 'parallel_request',
])
def test_producer_identity_fallback_rejects_missing_conflicting_or_ambiguous_proof(tmp_path, monkeypatch, damage):
    workspace, result = guarded_result(tmp_path, monkeypatch)
    events = result['events']
    stream = result['stream_tool_calls'][0]
    post = next(event for event in events if event['kind'] == 'post_tool_call')
    pre = next(event for event in events if event['kind'] == 'pre_tool_call')
    api_pre = next(event for event in events if event['kind'] == 'pre_api_request')
    api_post = next(event for event in events if event['kind'] == 'post_api_request')
    if damage == 'nonproducer':
        result['entry_mode'] = 'optional'
    elif damage == 'missing_entry':
        del result['entry_mode']
    elif damage == 'wrong_stream_id':
        stream['tool_call_id'] = 'another-real-looking-id'
    elif damage == 'empty_stream_id':
        stream['tool_call_id'] = ''
    elif damage == 'wrong_stream_content':
        stream['input']['content'] += '# changed\n'
    elif damage == 'wrong_stream_path':
        stream['input']['path'] = 'other.py'
    elif damage == 'duplicate_stream':
        result['stream_tool_calls'].append(copy.deepcopy(stream))
    elif damage.startswith('verified_'):
        if damage == 'verified_missing':
            del post['verified']
        else:
            post['verified'] = False if damage == 'verified_false' else 1
    elif damage == 'wrong_bytes':
        post['bytes_written'] -= 1
    elif damage == 'bytes_float':
        post['bytes_written'] = float(post['bytes_written'])
    elif damage == 'bytes_bool':
        post['bytes_written'] = True
    elif damage == 'wrong_content_hash':
        post['content_sha256'] = '0' * 64
    elif damage == 'wrong_arguments_hash':
        post['arguments_sha256'] = '0' * 64
    elif damage == 'wrong_target_hash':
        post['target_path_sha256'] = '0' * 64
    elif damage == 'missing_native_id':
        del post['tool_call_id']
    elif damage == 'wrong_session':
        post['session_id'] = 'other-session'
    elif damage == 'failed_tool':
        post['status'] = 'error'
    elif damage == 'duplicate_post':
        events.append(copy.deepcopy(post))
    elif damage == 'ambiguous_post':
        other = copy.deepcopy(post)
        other['tool_call_id'] = 'ambiguous-call'
        events.append(other)
    elif damage == 'duplicate_pre':
        events.append(copy.deepcopy(pre))
    elif damage == 'missing_pre':
        events.remove(pre)
    elif damage == 'wrong_pre_args':
        pre['arguments_sha256'] = '0' * 64
    elif damage == 'duplicate_api':
        events.append(copy.deepcopy(api_post))
    elif damage == 'missing_api':
        events.remove(api_pre)
    elif damage == 'missing_emission':
        api_post['model_tool_calls'] = []
    elif damage == 'duplicate_emission':
        api_post['model_tool_calls'] *= 2
    elif damage == 'api_error':
        events.append({'kind': 'api_request_error', **IDS})
    elif damage == 'wrong_request_tools':
        api_pre['request_tool_names'] = ['write_file', 'read_file']
    elif damage == 'wrong_request_choice':
        api_pre['request_tool_choice'] = 'auto'
    elif damage == 'parallel_request':
        api_pre['request_parallel_tool_calls'] = True
    assert _generation_link(result, workspace, CODE.encode())['proven'] is False
