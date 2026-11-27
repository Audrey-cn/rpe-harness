"""A single pre-dispatch protocol recovery retains every rejected response."""
import json
import sqlite3
import subprocess

import pytest

from test_hermes_review import review as review, draft, host_result, _sdk_home, RAW
from test_hermes_review_finish import _NATIVE_TURN_PROGRAM, native_agent
from test_review_skills import skill_env as skill_env, skill_review as skill_review, step


def invalid_response(env, *, number=1, calls=None):
    ids = dict(session_id='review-session', task_id='review-task', turn_id='review-turn',
               api_request_id=f'api-{number}')
    request = env['sdk'].middleware['llm_request'](request={'tools': []}, **ids)['request']
    env['store'].record('pre_api_request', {**ids, 'request': {'body': request}})
    env['sdk'].middleware['llm_execution'](request=request, next_call=lambda value: value, **ids)
    response = {'content': 'Original uncommitted reply.', 'tool_calls': calls or [],
                'finish_reason': 'tool_calls' if calls else 'stop'}
    env['store'].record('post_api_request', {**ids, 'usage': {'total_tokens': 7,
        'present_fields': ['total_tokens']}, 'response': {'assistant_message': response}})
    return response


@pytest.mark.parametrize('calls', [[], [
    {'id': 'bad-1', 'name': 'rpe_propose', 'arguments': json.dumps(draft())},
    {'id': 'bad-2', 'name': 'rpe_propose', 'arguments': json.dumps(draft())}]])
def test_initial_protocol_failure_can_recover_once_with_original_response(review, calls):
    response = invalid_response(review, calls=calls)
    assert review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
        api_request_id='api-1', response=response, intake_action='fallthrough') is True
    state = review['api'].ReviewWorkflowStore(review['db']).get({
        'entry_key': 'review-entry', 'session_id': 'review-session',
        'task_id': 'review-task', 'turn_id': 'review-turn'})
    assert state['protocol_recovery']['response'] == response
    assert state['phase'] == 'propose'
    assert not (review['workspace'] / 'out.csv').exists()
    with sqlite3.connect(review['db']) as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='rpe_proposals'").fetchone()[0] == 0
    second = invalid_response(review, number=2)
    second_agent = native_agent()
    second_agent._current_api_request_id = 'api-2'
    with pytest.raises(review['api'].ReviewIngressBlocked, match='protocol'):
        review['api'].recover_review_response(second_agent, effective_task_id='review-task',
            api_request_id='api-2', response=second, intake_action='fallthrough')
    from rpe_harness.integrations.hermes_session import aggregate_usage
    usage = aggregate_usage(review['store'].list('review-session'))
    assert usage['api_requests'] == 2
    assert usage['known_total_tokens'] == 14


def test_protocol_recovery_after_committed_discovery_is_blocked(skill_review):
    step(skill_review, 'rpe_discover', {'q': 'watercolor landscape'})
    response = invalid_response(skill_review, number=2)
    agent = native_agent()
    agent._current_api_request_id = 'api-2'
    with pytest.raises(skill_review['api'].ReviewIngressBlocked, match='protocol'):
        skill_review['api'].recover_review_response(agent, effective_task_id='review-task',
            api_request_id='api-2', response=response, intake_action='fallthrough')


@pytest.mark.parametrize('fault', ['agent_api_id', 'length', 'content_filter', 'dispatch'])
def test_recovery_rejects_wrong_identity_truncation_refusal_or_prior_dispatch(review, fault):
    response = invalid_response(review)
    agent = native_agent()
    if fault == 'agent_api_id':
        agent._current_api_request_id = 'other-api'
    elif fault == 'dispatch':
        review['store'].record('pre_tool_call', dict(session_id='review-session', task_id='review-task',
            turn_id='review-turn', api_request_id='api-1', tool_call_id='bad-dispatch', tool_name='rpe_propose'))
    else:
        response['finish_reason'] = fault
    with pytest.raises(review['api'].ReviewIngressBlocked, match='protocol'):
        review['api'].recover_review_response(agent, effective_task_id='review-task',
            api_request_id='api-1', response=response, intake_action='fallthrough')


@pytest.mark.parametrize('count', [0, 1, 2])
def test_structured_refusal_blocks_even_nominal_stop_and_valid_single_tool(review, count):
    calls = [{'id': f'bad-{i}', 'name': 'rpe_propose', 'arguments': '{}'} for i in range(count)]
    response = invalid_response(review, calls=calls)
    response['provider_data'] = {'refusal': 'Explicit structured refusal.'}
    with pytest.raises(review['api'].ReviewIngressBlocked, match='protocol'):
        review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
            api_request_id='api-1', response=response, intake_action='fallthrough')
    assert not [e for e in review['store'].list('review-session')
                if e['kind'] in ('pre_tool_call', 'rpe_review_protocol_recovery')]


def recovered_proposal(env):
    response = invalid_response(env)
    assert env['api'].recover_review_response(native_agent(), effective_task_id='review-task',
        api_request_id='api-1', response=response, intake_action='fallthrough')
    env['counter'] = 1
    if 'owned' in env:
        step(env, 'rpe_discover', {'q': 'watercolor landscape'})
        step(env, 'rpe_select', {'skill_id': '__no_skill__'})
    assert step(env, 'rpe_propose', draft())['status'] == 'awaiting_confirmation'
    return env['api'].evaluate_review_session(host_result(env), env['db'], env['workspace'],
        **({'catalog_root': env['owned']} if 'owned' in env else {}))


def test_recovered_discovery_counts_only_actual_native_tool_dispatch(skill_review):
    verdict = recovered_proposal(skill_review)
    assert verdict['proposal_verified'] is True, verdict
    assert verdict['protocol_recoveries'] == 1
    assert verdict['native_discovery_calls'] == verdict['native_selection_calls'] == 1


@pytest.mark.parametrize('fault', ['response', 'receipt', 'request_id', 'dispatch', 'retry_marker',
                                  'structured_refusal', 'native_continue'])
def test_final_gate_rejects_tampered_recovery_proofs(review, fault):
    assert recovered_proposal(review)['proposal_verified'] is True
    from rpe_harness.integrations.hermes_gate import digest_json
    with sqlite3.connect(review['db']) as db:
        if fault == 'receipt':
            db.execute("DELETE FROM hermes_host_events WHERE kind='rpe_review_protocol_recovery'")
        elif fault == 'dispatch':
            review['store'].record('pre_tool_call', dict(session_id='review-session', task_id='review-task',
                turn_id='review-turn', api_request_id='api-1', tool_call_id='bad-dispatch', tool_name='rpe_propose'))
        else:
            state = json.loads(db.execute('SELECT state_json FROM rpe_skill_workflows').fetchone()[0])
            if fault == 'response':
                state['protocol_recovery']['response']['content'] = 'Replaced original reply.'
            elif fault == 'request_id':
                state['protocol_recovery']['native']['api_request_id'] = 'other-api'
            elif fault == 'retry_marker':
                state['policies']['api-2']['protocol_recovery'] = False
            else:
                if fault == 'structured_refusal':
                    state['protocol_recovery']['response']['provider_data'] = {'refusal': 'declined'}
                else:
                    state['protocol_recovery']['intake_action'] = 'continue'
                # Even a matching digest cannot make an excluded response class eligible.
                event_id, raw = db.execute("SELECT id,metadata_json FROM hermes_host_events "
                    "WHERE kind='rpe_review_protocol_recovery'").fetchone()
                event = json.loads(raw)
                event['content_sha256'] = digest_json(state['protocol_recovery']['response'])
                db.execute('UPDATE hermes_host_events SET metadata_json=? WHERE id=?', (json.dumps(event), event_id))
            db.execute('UPDATE rpe_skill_workflows SET state_json=?,state_sha256=?',
                (json.dumps(state), digest_json(state)))
    verdict = review['api'].evaluate_review_session(host_result(review), review['db'], review['workspace'])
    assert verdict['proposal_verified'] is False


@pytest.mark.parametrize('fault', ['refusal_missing', 'refusal_single', 'refusal_surplus', 'native_continue'])
def test_installed_native_refusal_or_incomplete_intake_stops_without_protocol_recovery(review, fault):
    interpreter, environment = _sdk_home(review, f'excluded-{fault}')
    program = _NATIVE_TURN_PROGRAM.replace(
        'install_clean_ingress_guard();install_review_finish_adapter()',
        'install_clean_ingress_guard();install_review_finish_adapter();'
        'from rpe_harness.integrations.hermes_review_finish import install_review_response_adapter;'
        'install_review_response_adapter()')
    program = program.replace("name,args=steps[len(calls)-1]", "name,args=steps[len(calls)-1]\n"
        "    count={'refusal_missing':0,'refusal_single':1,'refusal_surplus':2,'native_continue':0}[sys.argv[4]]\n"
        "    bad=[{'id':'bad-'+str(i),'type':'function','function':{'name':name,'arguments':json.dumps(args)}} "
        "for i in range(count)]\n"
        "    message={'role':'assistant','content':'<REASONING_SCRATCHPAD>unfinished' if "
        "sys.argv[4]=='native_continue' else 'Explanation accompanying refusal.','tool_calls':bad}\n"
        "    if sys.argv[4]!='native_continue': message['refusal']='Explicit structured refusal.'\n"
        "    return ChatCompletion.model_validate({'id':'excluded-response','created':0,'model':'test-model',"
        "'object':'chat.completion','choices':[{'index':0,'finish_reason':'stop','message':message}]})\n")
    child = subprocess.run([str(interpreter), '-B', '-c', program, json.dumps(draft()), RAW,
        json.dumps([['rpe_propose', draft()]]), fault], cwd=review['workspace'], env=environment,
        capture_output=True, text=True, timeout=45)
    assert child.returncode != 0
    assert 'ReviewIngressBlocked' in child.stderr, child.stderr[-7000:] + child.stdout[-2000:]
    events = review['store'].list('review-session')
    assert len([e for e in events if e['kind'] == 'post_api_request']) == 1
    assert not [e for e in events if e['kind'] in ('pre_tool_call', 'rpe_review_protocol_recovery')]
    assert not (review['workspace'] / 'out.csv').exists()


@pytest.mark.parametrize('fault', ['missing', 'surplus'])
def test_installed_review_recovers_before_dispatch_and_finishes_with_two_observed_requests(review, fault):
    interpreter, environment = _sdk_home(review, f'protocol-{fault}')
    program = _NATIVE_TURN_PROGRAM.replace(
        'install_clean_ingress_guard();install_review_finish_adapter()',
        'install_clean_ingress_guard();install_review_finish_adapter();'
        'from rpe_harness.integrations.hermes_review_finish import install_review_response_adapter;'
        'install_review_response_adapter()')
    program = program.replace("name,args=steps[len(calls)-1]", "name,args=steps[len(calls)-1]\n"
        "    if len(calls)==1:\n"
        "        wrong=[] if sys.argv[4]=='missing' else [{'id':'bad-'+str(i),'type':'function',"
        "'function':{'name':name,'arguments':json.dumps(args)}} for i in range(2)]\n"
        "        return ChatCompletion.model_validate({'id':'bad-response','created':0,'model':'test-model',"
        "'object':'chat.completion','choices':[{'index':0,'finish_reason':'stop' if not wrong else 'tool_calls',"
        "'message':{'role':'assistant','content':'Keep this rejected reply.','tool_calls':wrong}}]})")
    steps = [['rpe_propose', draft()], ['rpe_propose', draft()]]
    child = subprocess.run([str(interpreter), '-B', '-c', program, json.dumps(draft()), RAW,
        json.dumps(steps), fault], cwd=review['workspace'], env=environment,
        capture_output=True, text=True, timeout=45)
    assert child.returncode == 0, child.stderr[-7000:] + child.stdout[-2000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result['provider_calls'] == result['api_calls'] == 2
    events = review['store'].list('review-session')
    assert len([e for e in events if e['kind'] == 'post_api_request']) == 2
    assert len([e for e in events if e['kind'] == 'pre_tool_call']) == 1
    verdict = review['api'].evaluate_review_session(host_result(review), review['db'], review['workspace'])
    assert verdict['proposal_verified'] is True, verdict
    assert verdict['protocol_recoveries'] == 1
    with sqlite3.connect(review['db']) as db:
        state = json.loads(db.execute('SELECT state_json FROM rpe_skill_workflows').fetchone()[0])
        assert state['protocol_recovery']['response']['content'] == 'Keep this rejected reply.'
        assert db.execute('SELECT COUNT(*) FROM rpe_confirmations').fetchone()[0] == 0
    assert not (review['workspace'] / 'out.csv').exists()


def test_second_missing_tool_persists_native_verdict_and_specific_failure(review, monkeypatch):
    journal = review['config'].parent / 'native-diagnostics.jsonl'
    monkeypatch.setenv('RPE_HERMES_REVIEW_DIAGNOSTICS', str(journal))
    first = invalid_response(review)
    assert review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
        api_request_id='api-1', response=first, intake_action='fallthrough')
    second = invalid_response(review, number=2)
    agent = native_agent()
    agent._current_api_request_id = 'api-2'
    with pytest.raises(review['api'].ReviewIngressBlocked):
        review['api'].recover_review_response(agent, effective_task_id='review-task',
            api_request_id='api-2', response=second, intake_action='fallthrough')
    assert journal.is_file(), 'terminal native verdict must survive the abort'
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[-1]['native_action'] == 'fallthrough'
    assert rows[-1]['finish_reason'] == 'stop'
    assert rows[-1]['tool_call_count'] == 0
    assert rows[-1]['expected_tool'] == 'rpe_propose'
    assert rows[-1]['rpe_action'] == 'blocked'
    assert rows[-1]['failure_code'] == 'review_protocol_recovery_not_allowed'
    assert 'Original uncommitted reply.' not in journal.read_text()
    assert len([e for e in review['store'].list('review-session') if e['kind'] == 'pre_api_request']) == 2
    assert len([e for e in review['store'].list('review-session') if e['kind'] == 'rpe_review_protocol_recovery']) == 1
    assert not [e for e in review['store'].list('review-session') if e['kind'] == 'pre_tool_call']


def test_public_second_missing_tool_diagnostics_outlive_default_home(review, monkeypatch):
    from pathlib import Path
    from rpe_harness.proposal_cli import dispatch_review
    from test_proposal_cli import parser
    from rpe_harness.integrations import hermes_session
    from rpe_harness.integrations.review_diagnostics import collect_diagnostics
    homes = []
    def host(raw, workspace, database, **kwargs):
        home = kwargs['owner_root']
        homes.append(home)
        journal = home/'native-diagnostics.jsonl'
        monkeypatch.setenv('RPE_HERMES_REVIEW_DIAGNOSTICS', str(journal))
        first = invalid_response(review)
        assert review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
            api_request_id='api-1', response=first, intake_action='fallthrough')
        second = invalid_response(review, number=2)
        agent = native_agent()
        agent._current_api_request_id = 'api-2'
        with pytest.raises(review['api'].ReviewIngressBlocked):
            review['api'].recover_review_response(agent, effective_task_id='review-task',
                api_request_id='api-2', response=second, intake_action='fallthrough')
        result = {**host_result(review), 'completed': False, 'exit_code': 1}
        result['usage'] = hermes_session.aggregate_usage(result['events'])
        result['review_diagnostics'] = collect_diagnostics(journal, result)
        return result
    monkeypatch.setattr(hermes_session, 'run_hermes_session', host)
    result = dispatch_review(parser().parse_args(['review', 'draft', '--db', str(review['db']),
        '--workspace', str(review['workspace']), '--text', RAW]))
    assert result['status'] == 'unverified'
    assert result['failure'] == {'domain': 'protocol', 'code': 'review_protocol_recovery_not_allowed',
                                 'stage': 'response_guard', 'retryable': False}
    assert result['host_observation']['usage']['known_total_tokens'] == 14
    assert not homes[0].exists()
    artifact = Path(result['diagnostics_ref']['path'])
    assert json.loads(artifact.read_text()) == result['review_diagnostics']
    assert len(result['review_diagnostics']['responses']) == 2
    assert result['review_diagnostics']['responses'][-1]['native_action'] == 'fallthrough'
    assert not [e for e in result['host_observation']['events'] if e['kind'] == 'pre_tool_call']


@pytest.mark.parametrize('finish_reason', ['length', 'content_filter'])
def test_installed_preintake_incomplete_never_continues_or_dispatches(review, finish_reason):
    from pathlib import Path
    interpreter, environment = _sdk_home(review, 'preintake-' + finish_reason)
    program = _NATIVE_TURN_PROGRAM.replace(
        'install_clean_ingress_guard();install_review_finish_adapter()',
        'install_clean_ingress_guard();install_review_finish_adapter();'
        'from rpe_harness.integrations.hermes_review_finish import install_review_response_adapter;'
        'install_review_response_adapter();'
        'from rpe_harness.integrations.hermes_bootstrap import install_usage_telemetry;install_usage_telemetry()')
    program = program.replace('name,args=steps[len(calls)-1]', 'name,args=steps[len(calls)-1]\n'
        '    if len(calls)==1:\n'
        "        return ChatCompletion.model_validate({'id':'bad-response','created':0,'model':'test-model',"
        "'object':'chat.completion','usage':{'prompt_tokens':3,'completion_tokens':4,'total_tokens':7},"
        "'choices':[{'index':0,'finish_reason':sys.argv[4],"
        "'message':{'role':'assistant','content':'PRIVATE_TRUNCATED_PROSE','tool_calls':[]}}]})")
    program = program.replace("result=agent.run_conversation(sys.argv[2],task_id='review-task')",
        "from rpe_harness.integrations.hermes_review import ReviewIngressBlocked\n"
        "try:\n    result=agent.run_conversation(sys.argv[2],task_id='review-task')\n"
        "except ReviewIngressBlocked:\n    print(json.dumps({'blocked':True,'provider_calls':len(calls)}));sys.exit(0)\n"
        "print(json.dumps({'blocked':False,'provider_calls':len(calls)}));sys.exit(0)")
    child = subprocess.run([str(interpreter), '-B', '-c', program, json.dumps(draft()), RAW,
        json.dumps([['rpe_propose', draft()], ['rpe_propose', draft()]]), finish_reason],
        cwd=review['workspace'], env=environment, capture_output=True, text=True, timeout=45)
    assert child.returncode == 0, child.stderr[-7000:] + child.stdout[-2000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result == {'blocked': True, 'provider_calls': 1}, result
    events = review['store'].list('review-session')
    assert len([e for e in events if e['kind'] == 'pre_api_request']) == 1
    assert not [e for e in events if e['kind'] in ('pre_tool_call', 'rpe_review_protocol_recovery')]
    outcomes = [e for e in events if e['kind'] in ('post_api_request', 'api_request_error')]
    assert len(outcomes) == 1 and outcomes[0]['error_type'] == 'RPEReviewResponseCheckBlocked'
    from rpe_harness.integrations.hermes_session import aggregate_usage
    usage = aggregate_usage(events)
    assert usage['known_total_tokens'] == 7 and usage['api_requests'] == 1
    assert usage['requests_with_usage'] == 1 and usage['api_errors'] == 1
    journal = Path(environment['RPE_HERMES_REVIEW_DIAGNOSTICS'])
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert rows[-1]['stage'] == 'response_check'
    assert rows[-1]['native_action'] is None and rows[-1]['intake'] == 'not_reached'
    assert rows[-1]['finish_reason'] == finish_reason and rows[-1]['tool_call_count'] == 0
    assert rows[-1]['expected_tool'] == 'rpe_propose'
    assert rows[-1]['failure_code'] == 'review_native_response_before_intake_blocked'
    assert 'PRIVATE_TRUNCATED_PROSE' not in journal.read_text()
    assert not (review['workspace']/'out.csv').exists()


def test_invalid_raw_argument_capture_retains_specific_content_free_failure(review, monkeypatch):
    journal = review['config'].parent/'argument-diagnostics.jsonl'
    monkeypatch.setenv('RPE_HERMES_REVIEW_DIAGNOSTICS', str(journal))
    response = invalid_response(review, calls=[{'id': 'call-1', 'name': 'rpe_propose', 'arguments': '{}'}])
    assert review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
        api_request_id='api-1', response=response, intake_action='fallthrough') is False
    ids = dict(session_id='review-session', task_id='review-task', turn_id='review-turn',
               api_request_id='api-1', tool_call_id='call-1')
    with pytest.raises(review['api'].ReviewIngressBlocked):
        review['sdk'].hooks['pre_tool_call'](tool_name='rpe_propose', args=['PRIVATE_ARGUMENT'], **ids)
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert rows[-1]['stage'] == 'argument_capture'
    assert rows[-1]['failure_code'] == 'review_raw_arguments_invalid'
    assert rows[-1]['rpe_action'] == 'blocked'
    assert 'PRIVATE_ARGUMENT' not in journal.read_text()


def test_argument_admission_rejection_is_diagnostic_not_business_context(review, monkeypatch):
    from test_hermes_review import dispatch
    journal = review['config'].parent/'argument-diagnostics.jsonl'
    monkeypatch.setenv('RPE_HERMES_REVIEW_DIAGNOSTICS', str(journal))
    candidate = draft()
    altered = {**candidate, 'capability': 'PRIVATE_UNREGISTERED_CAPABILITY'}
    assert dispatch(review, candidate, effective_arguments=altered)['status'] == 'rejected'
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert rows[-1]['stage'] == 'argument_admission'
    assert rows[-1]['failure_code'] == 'review_argument_admission_failed'
    assert 'PRIVATE_UNREGISTERED_CAPABILITY' not in journal.read_text()


def test_installed_malformed_argument_json_does_not_trigger_native_retry(review):
    from pathlib import Path
    interpreter, environment = _sdk_home(review, 'invalid-json-arguments')
    program = _NATIVE_TURN_PROGRAM.replace(
        'install_clean_ingress_guard();install_review_finish_adapter()',
        'install_clean_ingress_guard();install_review_finish_adapter();'
        'from rpe_harness.integrations.hermes_review_finish import install_review_response_adapter;'
        'install_review_response_adapter()')
    program = program.replace('name,args=steps[len(calls)-1]', 'name,args=steps[len(calls)-1]\n'
        '    if len(calls)==1:\n'
        "        return ChatCompletion.model_validate({'id':'bad-json-response','created':0,'model':'test-model',"
        "'object':'chat.completion','choices':[{'index':0,'finish_reason':'tool_calls',"
        "'message':{'role':'assistant','content':None,'tool_calls':[{'id':'bad-json-call','type':'function',"
        "'function':{'name':name,'arguments':'{bad}'}}]}}]})")
    program = program.replace("result=agent.run_conversation(sys.argv[2],task_id='review-task')",
        "from rpe_harness.integrations.hermes_review import ReviewIngressBlocked\n"
        "try:\n    result=agent.run_conversation(sys.argv[2],task_id='review-task')\n"
        "except ReviewIngressBlocked:\n    print(json.dumps({'blocked':True,'provider_calls':len(calls)}));sys.exit(0)\n"
        "print(json.dumps({'blocked':False,'provider_calls':len(calls)}));sys.exit(0)")
    child = subprocess.run([str(interpreter), '-B', '-c', program, json.dumps(draft()), RAW,
        json.dumps([['rpe_propose', draft()], ['rpe_propose', draft()]])],
        cwd=review['workspace'], env=environment, capture_output=True, text=True, timeout=45)
    assert child.returncode == 0, child.stderr[-7000:] + child.stdout[-2000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    assert result == {'blocked': True, 'provider_calls': 1}, result
    events = review['store'].list('review-session')
    assert len([e for e in events if e['kind'] == 'pre_api_request']) == 1
    assert not [e for e in events if e['kind'] in ('pre_tool_call', 'rpe_review_protocol_recovery')]
    rows = [json.loads(line) for line in Path(environment['RPE_HERMES_REVIEW_DIAGNOSTICS']).read_text().splitlines()]
    assert rows[-1]['stage'] == 'response_guard'
    assert rows[-1]['failure_code'] == 'review_protocol_arguments_invalid'
    assert rows[-1]['native_action'] == 'fallthrough'
    assert not (review['workspace']/'out.csv').exists()


@pytest.mark.parametrize('fault', ['no_recovery', 'receipt'])
def test_same_phase_repeat_requires_prior_authorized_recovery_receipt(review, monkeypatch, fault):
    journal = review['config'].parent/'repeat-diagnostics.jsonl'
    monkeypatch.setenv('RPE_HERMES_REVIEW_DIAGNOSTICS', str(journal))
    response = invalid_response(review)
    if fault == 'receipt':
        assert review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
            api_request_id='api-1', response=response, intake_action='fallthrough')
        from rpe_harness.integrations.hermes_gate import digest_json
        with sqlite3.connect(review['db']) as db:
            state = json.loads(db.execute('SELECT state_json FROM rpe_skill_workflows').fetchone()[0])
            state['protocol_recovery']['receipt_id'] = 'forged-recovery-receipt'
            db.execute('UPDATE rpe_skill_workflows SET state_json=?,state_sha256=?',
                       (json.dumps(state), digest_json(state)))
    ids = dict(session_id='review-session', task_id='review-task', turn_id='review-turn', api_request_id='api-2')
    with pytest.raises(review['api'].ReviewIngressBlocked) as raised:
        review['sdk'].middleware['llm_request'](request={'tools': []}, **ids)
    assert raised.value.code == 'review_unexpected_phase_repeat'
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert rows[-1]['stage'] == 'request_policy'
    assert rows[-1]['failure_code'] == 'review_unexpected_phase_repeat'
    with sqlite3.connect(review['db']) as db:
        state = json.loads(db.execute('SELECT state_json FROM rpe_skill_workflows').fetchone()[0])
    assert set(state['policies']) == {'api-1'}


@pytest.mark.parametrize('raw', ['{bad}', '[]', 'null', '{"v":NaN}', '{"x":1,"x":2}', '{"v":1e400}', '{"x":"' + 'a'*65536 + '"}'],
                         ids=['malformed', 'array', 'null', 'nonfinite', 'duplicate', 'overflow', 'oversize'])
def test_raw_argument_syntax_and_size_reject_without_modifying_response(review, raw):
    import copy
    response = invalid_response(review, calls=[{'id': 'call-1', 'name': 'rpe_propose', 'arguments': raw}])
    original = copy.deepcopy(response)
    with pytest.raises(review['api'].ReviewIngressBlocked) as raised:
        review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
            api_request_id='api-1', response=response, intake_action='fallthrough')
    assert raised.value.code == 'review_protocol_arguments_invalid'
    assert response == original
    assert not [e for e in review['store'].list('review-session') if e['kind'] == 'rpe_review_protocol_recovery']


@pytest.mark.parametrize('fault', ['type', 'required', 'unique', 'unknown_keyword'])
def test_multicall_recovery_checks_original_advertised_proposal_schema(review, fault):
    import copy
    candidate = draft()
    if fault == 'type':
        candidate['execution']['output_path'] = 42
    elif fault == 'required':
        candidate.pop('constraints')
    elif fault == 'unique':
        candidate['requirements'][0]['fields'] = ['/capability', '/capability']
    else:
        review['sdk'].tools['rpe_propose']['schema']['parameters']['not'] = {}
    response = invalid_response(review, calls=[{'id': 'c1', 'name': 'rpe_propose', 'arguments': json.dumps(candidate)},
                                             {'id': 'c2', 'name': 'rpe_propose', 'arguments': json.dumps(draft())}])
    original = copy.deepcopy(response)
    with pytest.raises(review['api'].ReviewIngressBlocked) as raised:
        review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
            api_request_id='api-1', response=response, intake_action='fallthrough')
    assert raised.value.code == 'review_protocol_arguments_invalid'
    assert response == original
    assert not [e for e in review['store'].list('review-session') if e['kind'] == 'rpe_review_protocol_recovery']


@pytest.mark.parametrize('query,eligible', [('watercolor landscape', True), (3, False), ('', False), ('a'*4097, False)],
                         ids=['valid', 'type', 'empty', 'oversize'])
def test_discovery_multicall_schema_and_identity_are_checked_before_recovery(skill_review, query, eligible):
    response = invalid_response(skill_review, calls=[
        {'id': 'd1', 'name': 'rpe_discover', 'arguments': json.dumps({'q': query})},
        {'id': 'd2', 'name': 'rpe_discover', 'arguments': json.dumps({'q': 'landscape'})}])
    if eligible:
        assert skill_review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
            api_request_id='api-1', response=response, intake_action='fallthrough') is True
    else:
        with pytest.raises(skill_review['api'].ReviewIngressBlocked) as raised:
            skill_review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
                api_request_id='api-1', response=response, intake_action='fallthrough')
        assert raised.value.code == 'review_protocol_arguments_invalid'
    assert len([e for e in skill_review['store'].list('review-session') if e['kind'] == 'rpe_review_protocol_recovery']) == int(eligible)


def test_multicall_business_incomplete_proposal_is_not_filled_or_schema_rejected(review):
    candidate = draft()
    candidate['execution'].pop('key_columns')
    candidate['requirements'] = [row for row in candidate['requirements'] if row['fields'] != ['/execution/key_columns']]
    candidate['unresolved'] = ['Which business columns define duplicates?']
    response = invalid_response(review, calls=[
        {'id': 'p1', 'name': 'rpe_propose', 'arguments': json.dumps(candidate)},
        {'id': 'p2', 'name': 'rpe_propose', 'arguments': json.dumps(draft())}])
    assert review['api'].recover_review_response(native_agent(), effective_task_id='review-task',
        api_request_id='api-1', response=response, intake_action='fallthrough') is True
    assert 'key_columns' not in json.loads(response['tool_calls'][0]['arguments'])['execution']
