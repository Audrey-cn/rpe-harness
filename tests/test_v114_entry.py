import json
from types import SimpleNamespace

import pytest

PROFILE = {'schema_version': 'rpe.hermes-request-budget/v1', 'provider': 'xiaomi',
    'model': 'mimo-v2.6-pro', 'max_completion_tokens': 2400, 'thinking_type': 'disabled',
    'request_timeout_seconds': 90, 'stale_timeout_seconds': 90}


def test_isolated_home_opt_in_timeout_and_default_compatibility(tmp_path):
    from rpe_harness.integrations.hermes_session import prepare_home
    settings = {'provider': 'xiaomi', 'model': 'mimo-v2.6-pro', 'base_url': 'https://example.test/v1', 'api_key': 'dummy'}
    for enabled in (False, True):
        home = prepare_home(tmp_path / str(enabled), tmp_path, tmp_path/'db',
            tool_enabled=enabled, settings=settings, request_budget=PROFILE)
        config = json.loads((home/'config.yaml').read_text())
        assert config['providers']['xiaomi']['request_timeout_seconds'] == 90
        assert config['providers']['xiaomi']['stale_timeout_seconds'] == 90
    home = prepare_home(tmp_path/'legacy', tmp_path, tmp_path/'db', tool_enabled=False, settings=settings)
    assert 'providers' not in json.loads((home/'config.yaml').read_text())


def test_public_budget_file_does_not_change_business_source(tmp_path, monkeypatch):
    import argparse

    from rpe_harness import proposal_cli
    parser = argparse.ArgumentParser()
    proposal_cli.register_review(parser.add_subparsers(dest='command'))
    path = tmp_path/'budget.json'
    path.write_text(json.dumps(PROFILE))
    args = parser.parse_args(['review','draft','--text','当前原文','--workspace',str(tmp_path),
        '--db',str(tmp_path/'db'), '--request-budget',str(path)])
    called=[]
    from rpe_harness.integrations import hermes_review
    monkeypatch.setattr(hermes_review,'run_hermes_review',lambda raw,*a,**kw: called.append((raw,kw)) or {})
    proposal_cli.dispatch_review(args)
    assert called[0][0] == '当前原文'
    assert called[0][1]['request_budget'] == PROFILE
    path.write_text('{}')
    with pytest.raises(ValueError):
        proposal_cli.dispatch_review(args)
    assert len(called) == 1


def test_native_finish_disabled_is_noop(monkeypatch):
    from rpe_harness.integrations.hermes_native_finish import install_native_finish_adapter
    monkeypatch.delenv('RPE_HERMES_NATIVE_FINISH_ENABLED',raising=False)
    install_native_finish_adapter()


def native_finish_fixture(tmp_path):
    from rpe_harness.integrations.hermes import _metadata
    from rpe_harness.integrations.hermes_native import NativeEvidenceObserver
    from rpe_harness.integrations.hermes_native_finish import NativeTurnFinisher
    ids={'session_id': 's','task_id': 'task','turn_id': 'turn','api_request_id': 'api'}
    call=dict(**ids,tool_call_id='call',tool_name='write_file',arguments_sha256='a'*64)
    config={'workspace': str(tmp_path),'draft_name': 'draft.json'}
    finisher=NativeTurnFinisher(config)
    observer=NativeEvidenceObserver(config)
    finisher.record('pre_api_request',ids)
    finisher.record('post_api_request',{**ids,'model_tool_calls':[{'tool_call_id': 'call','tool_name': 'write_file'}]})
    finisher.record('pre_tool_call',call)
    observer.before(call)
    (tmp_path/'draft.json').write_text('{"unresolved":["no key"]}')
    post={**call,'status':'ok','result':'{"success":true}'}
    finisher.record('post_tool_call',_metadata(post))
    for row in observer.after(post):
        kind=row.pop('kind')
        finisher.record(kind,_metadata(row))
    agent=SimpleNamespace(session_id='s',_current_turn_id='turn',_current_api_request_id='api')
    return finisher,agent,[{'role': 'tool','tool_call_id': 'call'}]


def test_native_finish_records_candidate_without_business_acceptance(tmp_path):
    finisher,agent,messages=native_finish_fixture(tmp_path)
    result=finisher.finish(agent,effective_task_id='task',messages=messages)
    assert result['status']=='native_artifact_recorded' and result['accepted'] is False
    assert result['artifact_name']=='draft.json'


@pytest.mark.parametrize('fault',['missing_api','wrong_api','wrong_tail','failed_tool','changed_file','wrong_args','missing_batch_post'])
def test_native_finish_rejects_incomplete_or_changed_chain(tmp_path,fault):
    finisher,agent,messages=native_finish_fixture(tmp_path)
    if fault=='missing_api':
        finisher.rows=[r for r in finisher.rows if r['kind']!='post_api_request']
    elif fault=='wrong_api':
        agent._current_api_request_id='other'
    elif fault=='wrong_tail':
        messages[0]['tool_call_id']='other'
    elif fault=='failed_tool':
        next(r for r in finisher.rows if r['kind']=='post_tool_call')['status']='failed'
    elif fault=='changed_file':
        (tmp_path/'draft.json').write_text('{}')
    elif fault=='wrong_args':
        next(r for r in finisher.rows if r['kind']=='pre_tool_call')['arguments_sha256']='b'*64
    else:
        next(r for r in finisher.rows if r['kind']=='post_api_request')['model_tool_calls'].append(
            {'tool_call_id': 'extra','tool_name': 'terminal'})
    assert finisher.finish(agent,effective_task_id='task',messages=messages) is None
