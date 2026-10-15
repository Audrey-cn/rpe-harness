"""Provider-free review completion must follow durable native tool receipts."""
import importlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_hermes_review import review, dispatch, draft, host_result
from test_review_skills import skill_env, skill_review


def native_agent(**changes):
    return SimpleNamespace(session_id='review-session', _current_turn_id='review-turn',
        _current_api_request_id='api-1', **changes)


def finish(env, *, agent=None, messages=None):
    return env['api'].finish_review_turn(agent or native_agent(), effective_task_id='review-task',
        messages=messages if messages is not None else [
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'native tool result'}])


@pytest.mark.parametrize('unresolved', [False, True])
def test_durable_proposal_finishes_locally_without_an_api_or_approval(review, unresolved):
    candidate = draft()
    if unresolved:
        candidate['unresolved'] = ['Unsupported email request remains unresolved.']
    proposal = dispatch(review, candidate)
    before = review['store'].list('review-session')
    terminal = finish(review)
    assert terminal == {key: proposal[key] for key in
        ('status', 'proposal_id', 'proposal_sha256', 'missing', 'errors') if key in proposal}
    after = review['store'].list('review-session')
    assert [e for e in after if e['kind'].endswith('api_request')] == [
        e for e in before if e['kind'].endswith('api_request')]
    assert not (review['workspace'] / 'out.csv').exists()
    with sqlite3.connect(review['db']) as db:
        assert db.execute('SELECT COUNT(*) FROM rpe_confirmations').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM rpe_proposal_executions').fetchone()[0] == 0
    assert review['api'].evaluate_review_session(host_result(review), review['db'],
        review['workspace'])['proposal_verified'] is True


@pytest.mark.parametrize('kind', ['post_tool_call', 'rpe_review_proposed',
    'rpe_review_arguments_received', 'rpe_review_arguments_checked', 'rpe_skill_rpc_received'])
def test_missing_durable_receipt_cannot_finish_review(review, kind):
    dispatch(review, draft())
    with sqlite3.connect(review['db']) as db:
        db.execute('DELETE FROM hermes_host_events WHERE kind=?', (kind,))
    with pytest.raises(review['api'].ReviewIngressBlocked):
        finish(review)


@pytest.mark.parametrize('fault', ['tool_id', 'api_id', 'tail_role', 'proposal_document'])
def test_mismatched_native_tail_or_proposal_cannot_finish_review(review, fault):
    dispatch(review, draft())
    agent, messages = native_agent(), [{'role': 'tool', 'tool_call_id': 'call-1'}]
    if fault == 'tool_id':
        messages[0]['tool_call_id'] = 'other-call'
    elif fault == 'api_id':
        agent._current_api_request_id = 'other-api'
    elif fault == 'tail_role':
        messages[0]['role'] = 'assistant'
    else:
        with sqlite3.connect(review['db']) as db:
            db.execute('UPDATE rpe_proposals SET document=?', (b'{}',))
    with pytest.raises(review['api'].ReviewIngressBlocked):
        finish(review, agent=agent, messages=messages)


def test_active_review_and_missing_callback_cannot_signal_success(review, monkeypatch):
    ids = dict(session_id='review-session', task_id='review-task', turn_id='review-turn',
               api_request_id='api-1')
    review['sdk'].middleware['llm_request'](request={'tools': []}, **ids)
    assert finish(review) is None
    monkeypatch.setattr(review['api'], '_TURN_FINISHER', None)
    assert finish(review) is None


def test_invalid_earlier_multicall_response_is_never_excused_by_a_proposal(review):
    dispatch(review, draft())
    with sqlite3.connect(review['db']) as db:
        row = db.execute("SELECT id,metadata_json FROM hermes_host_events WHERE kind='post_api_request'").fetchone()
        event = json.loads(row[1])
        event['model_tool_calls'].append({'tool_call_id': 'extra', 'tool_name': 'rpe_propose'})
        db.execute('UPDATE hermes_host_events SET metadata_json=? WHERE id=?', (json.dumps(event), row[0]))
    with pytest.raises(review['api'].ReviewIngressBlocked):
        finish(review)
    verdict = review['api'].evaluate_review_session(host_result(review), review['db'], review['workspace'])
    assert verdict['proposal_verified'] is False
    assert 'review_model_policy_ignored' in verdict['errors']


def test_installed_hermes_native_turn_stops_after_proposal_without_second_provider_request(review):
    """Real installed loop, middleware, executor and finalizer; provider alone is stubbed."""
    import subprocess
    from test_hermes_review import RAW, _sdk_home
    interpreter, environment = _sdk_home(review, 'native-turn-finish')
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    program = _NATIVE_TURN_PROGRAM
    child = subprocess.run([str(interpreter), '-B', '-c', program, json.dumps(draft()), RAW],
        cwd=review['workspace'], env=environment, capture_output=True, text=True, timeout=45)
    assert child.returncode == 0, child.stderr[-7000:] + child.stdout[-2000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result['provider_calls'] == result['api_calls'] == 1
    assert result['completed'] is True and result['tail_roles'] == ['tool', 'assistant']
    verdict = review['api'].evaluate_review_session(host_result(review), review['db'], review['workspace'])
    assert verdict['proposal_verified'] is True, verdict
    assert verdict['reporting_api_requests'] == 0
    events = review['store'].list('review-session')
    assert sum(row['kind'] == 'pre_api_request' for row in events) == 1
    assert sum(row['kind'] == 'post_api_request' for row in events) == 1
    assert any(row['kind'] == 'on_session_end' for row in events)
    assert not (review['workspace'] / 'out.csv').exists()


_NATIVE_TURN_PROGRAM = r'''import json,socket,sys
from types import SimpleNamespace
from hermes_cli.plugins import get_plugin_manager
manager=get_plugin_manager();manager.discover_and_load()
assert manager._plugins['rpe-harness'].enabled,manager._plugins['rpe-harness'].error
from rpe_harness.integrations.hermes_bootstrap import install_clean_ingress_guard
from rpe_harness.integrations.hermes_review_finish import install_review_finish_adapter
install_clean_ingress_guard();install_review_finish_adapter()
from run_agent import AIAgent
from openai.types.chat import ChatCompletion
from unittest.mock import MagicMock
steps=json.loads(sys.argv[3]) if len(sys.argv)>3 else [['rpe_propose',json.loads(sys.argv[1])]]
calls=[]
def deny_network(self,address): raise AssertionError('network disabled in provider-free smoke')
socket.socket.connect=deny_network
agent=AIAgent(api_key='provider-free-test-key',base_url='http://127.0.0.1:9/v1',
    provider='openai',api_mode='chat_completions',model='test-model',max_iterations=5,
    quiet_mode=True,skip_context_files=True,skip_memory=True,save_trajectories=False,
    session_id='review-session',platform='cli',enabled_toolsets=['rpe_harness','skills'] if len(steps)>1 else ['rpe_harness'])
def provider(**request):
    calls.append(request)
    assert len(calls)<=len(steps),'extra provider request after durable proposal'
    name,args=steps[len(calls)-1]
    assert request['tool_choice']=='auto',request['tool_choice']
    assert [tool['function']['name'] for tool in request['tools']]==[name]
    return ChatCompletion.model_validate({'id':'provider-free-response','created':0,
        'model':'test-model','object':'chat.completion','choices':[{'index':0,
        'finish_reason':'tool_calls','message':{'role':'assistant','content':None,
        'tool_calls':[{'id':'native-call-'+str(len(calls)),'type':'function','function':{
            'name':name,'arguments':json.dumps(args)}}]}}]})
agent.client=MagicMock()
agent.client.chat.completions.create.side_effect=provider
agent._disable_streaming=True
agent._cached_system_prompt='Provider-free installed Hermes review smoke.'
agent._use_prompt_caching=False
agent.compression_enabled=False
agent._fallback_chain=[]
result=agent.run_conversation(sys.argv[2],task_id='review-task')
assert len(calls)==len(steps),len(calls)
assert result['completed'] is True,result
assert result['api_calls']==len(steps),result
terminal=json.loads(result['final_response'])
assert terminal['status']=='awaiting_confirmation',terminal
assert result['messages'][-1]['role']=='assistant',result['messages'][-1]
assert result['messages'][-2]['role']=='tool',result['messages'][-2]
print(json.dumps({'provider_calls':len(calls),'terminal':terminal,'completed':result['completed'],
    'api_calls':result['api_calls'],'tail_roles':[row['role'] for row in result['messages'][-2:]]}))
'''


@pytest.mark.parametrize('skill_review', [{'raw':
    'Inspect source.xlsx rows, save output.json, preserve original.'}], indirect=True)
def test_installed_cold_review_finishes_after_four_native_provider_requests(skill_review):
    import shutil
    import subprocess
    from openpyxl import Workbook
    from test_hermes_review import _sdk_home
    book = Workbook()
    book.active.append(['id', 'flag'])
    book.active.append([1, True])
    book.save(skill_review['workspace'] / 'source.xlsx')
    book.close()
    interpreter, environment = _sdk_home(skill_review, 'cold-native-turn-finish')
    home = Path(environment['HERMES_HOME'])
    (home / '.no-bundled-skills').touch()
    config = json.loads(skill_review['config'].read_text())
    old_root, new_root = config['skills']['materialized_path'], str(home / 'skills')
    shutil.move(old_root, new_root)
    config['skills']['materialized_path'] = new_root
    for row in config['skills']['entries']:
        for key in ('native_skill_path', 'archive_path'):
            if row.get(key):
                row[key] = new_root + row[key][len(old_root):]
    skill_review['config'].write_text(json.dumps(config))
    candidate = {'capability': 'xlsx.inspect', 'constraints': ['preserve_original'],
        'execution': {'input_path': 'source.xlsx', 'output_path': 'output.json', 'mode': 'rows'},
        'requirements': [{'text': 'Inspect workbook as requested.',
            'quote': config['raw'], 'fields': ['/capability', '/constraints',
                '/execution/input_path', '/execution/output_path', '/execution/mode']}],
        'unresolved': []}
    steps = [['rpe_discover', {'q': 'Excel spreadsheet'}],
        ['rpe_select', {'skill_id': 'productivity/xlsx'}], ['skill_view', {'name': 'xlsx'}],
        ['rpe_propose', candidate]]
    source_before = (skill_review['source'] / 'productivity/xlsx/SKILL.md').read_bytes()
    child = subprocess.run([str(interpreter), '-B', '-c', _NATIVE_TURN_PROGRAM,
        json.dumps(candidate), config['raw'], json.dumps(steps)], cwd=skill_review['workspace'],
        env=environment, capture_output=True, text=True, timeout=45)
    assert child.returncode == 0, child.stderr[-7000:] + child.stdout[-2000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result['provider_calls'] == result['api_calls'] == 4
    assert result['completed'] is True
    verdict = skill_review['api'].evaluate_review_session(host_result(skill_review),
        skill_review['db'], skill_review['workspace'], catalog_root=skill_review['owned'])
    assert verdict['proposal_verified'] is True, verdict
    assert verdict['reporting_api_requests'] == 0
    assert verdict['cold_skill_restores'] == 1
    assert verdict['native_discovery_calls'] == verdict['native_selection_calls'] == verdict['native_skill_view_calls'] == 1
    assert (skill_review['source'] / 'productivity/xlsx/SKILL.md').read_bytes() == source_before
    assert not (skill_review['workspace'] / 'output.json').exists()
    with sqlite3.connect(skill_review['db']) as db:
        assert db.execute('SELECT COUNT(*) FROM rpe_confirmations').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM rpe_proposal_executions').fetchone()[0] == 0


def _install_fake_native_seam(monkeypatch, result):
    from rpe_harness.integrations import hermes_review, hermes_review_finish
    def native(agent, *, messages, active_system_prompt, conversation_history,
               compression_attempts, current_turn_user_idx, effective_task_id, **context):
        return SimpleNamespace(end_turn=False)
    rounds = SimpleNamespace(compress_after_tool_results=native)
    preflight = SimpleNamespace(PostToolCompressionVerdict=lambda **value: SimpleNamespace(**value))
    original_import = importlib.import_module
    monkeypatch.setattr(hermes_review_finish.importlib, 'import_module', lambda name:
        {'agent.turn_tool_round': rounds, 'agent.turn_preflight': preflight}.get(name) or original_import(name))
    monkeypatch.setattr(hermes_review, 'finish_review_turn', lambda *args, **kwargs: result)
    monkeypatch.setenv('RPE_HERMES_ENTRY_MODE', 'review')
    hermes_review_finish.install_review_finish_adapter()
    installed = rounds.compress_after_tool_results
    hermes_review_finish.install_review_finish_adapter()
    assert rounds.compress_after_tool_results is installed
    return installed


def test_adapter_missing_terminal_status_keeps_native_compression_path(monkeypatch):
    installed = _install_fake_native_seam(monkeypatch, None)
    verdict = installed(native_agent(), messages=[], effective_task_id='review-task',
        active_system_prompt='system', conversation_history=[], compression_attempts=0,
        current_turn_user_idx=0)
    assert verdict.end_turn is False


@pytest.mark.parametrize('invalid', [False, {}, {'status': 'confirmed'}])
def test_adapter_invalid_terminal_value_cannot_stop_successfully(monkeypatch, invalid):
    from rpe_harness.integrations.hermes_review import ReviewIngressBlocked
    installed = _install_fake_native_seam(monkeypatch, invalid)
    with pytest.raises(ReviewIngressBlocked):
        installed(native_agent(), messages=[], effective_task_id='review-task',
            active_system_prompt='system', conversation_history=[], compression_attempts=0,
            current_turn_user_idx=0)


def test_rejected_earlier_discovery_never_becomes_verified_after_later_proposal(skill_review):
    from test_review_skills import step
    ids = dict(session_id='review-session', task_id='review-task', turn_id='review-turn',
               api_request_id='api-1')
    request = skill_review['sdk'].middleware['llm_request'](request={'tools': []}, **ids)['request']
    skill_review['store'].record('pre_api_request', {**ids, 'request': {'body': request}})
    skill_review['sdk'].middleware['llm_execution'](request=request, next_call=lambda value: value, **ids)
    skill_review['store'].record('post_api_request', {**ids, 'response': {'assistant_message': {
        'tool_calls': [{'id': call, 'name': 'rpe_discover'} for call in ('rejected-1', 'rejected-2')]}}})
    for call in ('rejected-1', 'rejected-2'):
        response = skill_review['sdk'].middleware['tool_execution'](tool_name='rpe_discover',
            args={'q': 'Excel spreadsheet'}, next_call=lambda args: pytest.fail('invalid discovery executed'),
            tool_call_id=call, **ids)
        assert json.loads(response)['error']['code'] == 'review_identity_invalid'
    skill_review['counter'] = 1
    step(skill_review, 'rpe_discover', {'q': 'watercolor landscape'})
    step(skill_review, 'rpe_select', {'skill_id': '__no_skill__'})
    assert step(skill_review, 'rpe_propose', draft())['status'] == 'awaiting_confirmation'
    agent = native_agent()
    agent._current_api_request_id = 'api-4'
    with pytest.raises(skill_review['api'].ReviewIngressBlocked):
        finish(skill_review, agent=agent, messages=[{'role': 'tool', 'tool_call_id': 'call-4'}])
    verdict = skill_review['api'].evaluate_review_session(host_result(skill_review), skill_review['db'],
        skill_review['workspace'], catalog_root=skill_review['owned'])
    assert verdict['proposal_verified'] is False
    assert 'review_model_policy_ignored' in verdict['errors']
    assert not (skill_review['workspace'] / 'out.csv').exists()
