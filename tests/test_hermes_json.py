"""Explicit JSON Hermes entry, contract identity, and independent final gate."""
import json
import sys

import pytest

from rpe_harness.integrations.hermes import (
    _prepare_request, _recorded_contract, HostEventStore,
)
from rpe_harness.integrations.hermes_gate import contract_for, digest_json, evaluate_session
from test_hermes_integration import configured_plugin
from test_hermes_gate import strict_plugin, ids, api_request, response, execute, completed


def flat(style='pretty'):
    return {'capability':'json.normalize', 'input_path':'source.json',
            'output_path':'result.json', 'style':style, 'goal':'normalize private-business-goal',
            'constraints':['preserve_original','preserve_order']}


def test_public_schema_has_explicit_capability_and_style(tmp_path, monkeypatch):
    context, _, _ = configured_plugin(tmp_path, monkeypatch)
    schema = context.tools['rpe_task']['schema']['parameters']
    assert schema['required'] == ['input_path','output_path','goal']
    assert schema['properties']['capability']['enum'] == ['tabular.deduplicate','json.normalize']
    assert schema['properties']['style']['enum'] == ['pretty','minify']
    assert schema['additionalProperties'] is False
    assert 'style' in context.tools['rpe_task']['schema']['description']


@pytest.mark.parametrize('style',['pretty','minify'])
def test_flat_json_preserves_exact_execution_shape_and_recorded_identity(tmp_path, style):
    prepared = _prepare_request(flat(style), tmp_path, 'session')
    assert prepared['capability'] == 'json.normalize'
    assert set(prepared['execution']) == {'input_path','output_path','style'}
    normalized = contract_for(prepared, tmp_path)
    assert normalized == _recorded_contract(prepared)
    assert normalized['execution'] == {'input_path':str(tmp_path/'source.json'),
        'output_path':str(tmp_path/'result.json'),'style':style}
    assert normalized['contract_version'] == 1


def test_style_is_part_of_contract_hash(tmp_path):
    payloads = [_prepare_request(flat(style), tmp_path, None) for style in ('pretty','minify')]
    assert digest_json(contract_for(payloads[0],tmp_path)) != digest_json(contract_for(payloads[1],tmp_path))


@pytest.mark.parametrize('damage,field',[
    ('missing_style','style'),('invalid_style','style'),('tabular_option','key_columns'),
    ('tabular_style','style'),('unknown_capability','capability'),('missing_columns','key_columns'),
    ('unknown_execution','indent'),('unknown_body','indent'),('mixed_envelope','style'),
    ('legacy_json','schema_version'),('json_formula_constraint','constraints'),
])
def test_schema_rejection_is_correctable_and_metadata_only(tmp_path, monkeypatch, damage, field):
    context, workspace, db = configured_plugin(tmp_path, monkeypatch)
    (workspace/'source.json').write_text('{"private-business-value":1}')
    args = flat()
    if damage == 'missing_style':
        args.pop('style')
    elif damage == 'invalid_style':
        args['style'] = 'private-business-value'
    elif damage == 'tabular_option':
        args['key_columns'] = ['private-business-value']
    elif damage == 'tabular_style':
        args['capability'] = 'tabular.deduplicate'
        args['key_columns']=['id']
    elif damage == 'unknown_capability':
        args['capability'] = 'private-business-value'
    elif damage == 'missing_columns':
        args.pop('capability')
        args.pop('style')
    elif damage == 'json_formula_constraint':
        args['constraints'] = ['preserve_formulas']
    else:
        request = {'schema_version':2,'event_id':'event','capability':'json.normalize',
            'goal':'private-business-goal','execution':{'input_path':'source.json',
                'output_path':'result.json','style':'pretty'}}
        args = {'request':request}
        if damage == 'unknown_execution':
            request['execution']['indent'] = 'private-business-value'
        elif damage == 'unknown_body':
            request['indent'] = 'private-business-value'
        elif damage == 'mixed_envelope':
            args['style'] = 'private-business-value'
        else:
            request['schema_version'] = 1
            request['state'] = {'required_capability':'json.normalize','goal':'private-business-goal'}
    result = json.loads(context.tools['rpe_task']['handler'](args, session_id='s'))
    assert result['status'] == 'rejected'
    assert result['error']['code'] == 'invalid_task_arguments'
    assert result['error']['fields'] == [field]
    assert set(result['error']) == {'code','message','type','fields','allowed_parameters'}
    assert 'private-business' not in json.dumps(result)
    assert not (workspace/'result.json').exists()
    saved = HostEventStore(db).list('s')
    assert 'private-business' not in json.dumps(saved)
    assert [e['kind'] for e in saved] == ['rpe_task_received','rpe_task_returned']


@pytest.mark.parametrize('field',['indent','key_columns'])
def test_contract_normalizers_never_drop_execution_fields(tmp_path, field):
    request = {'schema_version':2,'capability':'json.normalize','execution':{
        'input_path':str(tmp_path/'source.json'),'output_path':str(tmp_path/'result.json'),
        'style':'pretty',field:[]}}
    for normalize in (lambda p:contract_for(p,tmp_path), _recorded_contract):
        with pytest.raises(ValueError):
            normalize(request)


def test_missing_capability_retains_old_tabular_contract(tmp_path):
    request = _prepare_request({'input_path':'source.csv','output_path':'out.csv',
        'key_columns':['id'],'goal':'retain first'},tmp_path,None)
    assert request['capability']=='tabular.deduplicate'
    assert contract_for(request,tmp_path) == _recorded_contract(request)
    assert contract_for(request,tmp_path)['execution'] == {'input_path':str(tmp_path/'source.csv'),
        'output_path':str(tmp_path/'out.csv'),'key_columns':['id'],'sheet':None,
        'keep':'first','encoding':'utf-8-sig','delimiter':','}


def test_json_receipt_hashing_enforces_input_budget_before_executor(tmp_path, monkeypatch):
    context,workspace,db = configured_plugin(tmp_path,monkeypatch)
    (workspace/'source.json').write_bytes(b' ' * 1_048_577)
    def forbidden(*args,**kwargs):
        pytest.fail('oversized input must not start an executor')
    monkeypatch.setattr('rpe_harness.integrations.hermes.subprocess.run',forbidden)
    result = json.loads(context.tools['rpe_task']['handler'](flat(),session_id='s'))
    assert result['status']=='rejected'
    assert not (workspace/'result.json').exists()
    assert 'input_sha256' not in result


@pytest.mark.parametrize('style,expected',[
    ('pretty',b'{\n  "a": [\n    1e0,\n    -0\n  ],\n  "b": "x\\u0061"\n}\n'),
    ('minify',b'{"a":[1e0,-0],"b":"x\\u0061"}\n'),
])
def test_real_broker_json_cli_and_strict_gate(tmp_path, monkeypatch, style, expected):
    from rpe_harness.integrations.hermes_broker import execution_broker
    context,workspace,db = strict_plugin(tmp_path,monkeypatch)
    original = b' { "a" : [ 1e0, -0 ], "b" : "x\\u0061" } '
    (workspace/'source.json').write_bytes(original)
    request = _prepare_request(flat(style),workspace,None)
    first = ids()
    with execution_broker(db_path=db,workspace=workspace,python=sys.executable) as env:
        for key,value in env.items():
            monkeypatch.setenv(key,value)
        assert api_request(context,first)['tool_choice']['function']['name']=='rpe_task'
        response(context,first,[('call-one','rpe_task')])
        result = execute(context,flat(style),first)
        assert result['status']=='succeeded',result
        final = ids(call='',api='api-final')
        assert api_request(context,final)['tool_choice']=='none'
        response(context,final,[])
    assert env.cleanup['cleanup_complete'] is True
    assert env.cleanup['tracked_children']==1
    assert (workspace/'result.json').read_bytes()==expected
    assert (workspace/'source.json').read_bytes()==original
    verdict=evaluate_session(completed(context,db),db,workspace,required_request=request)
    assert verdict['status']=='succeeded' and verdict['verified'] is True,verdict
    assert verdict['errors']==[]
    assert verdict['verification']['plan_sha256']==result['verification']['plan_sha256']
    # Independent final acceptance rereads the artifact, not the assistant summary.
    (workspace/'result.json').write_bytes(b'null\n')
    damaged=evaluate_session(completed(context,db),db,workspace,required_request=request)
    assert damaged['verified'] is False
    assert 'artifact_changed' in damaged['errors']


def test_json_missing_style_can_be_corrected_with_observable_strict_policy(tmp_path, monkeypatch):
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    (workspace/'source.json').write_text('{"a":1}')
    bad=flat()
    bad.pop('style')
    first=ids()
    api_request(context,first)
    response(context,first,[('call-one','rpe_task')])
    rejection=execute(context,bad,first)
    assert rejection['error']['fields']==['style']
    second=ids(call='call-two',api='api-two')
    assert api_request(context,second)['tool_choice']['function']['name']=='rpe_task'
    response(context,second,[('call-two','rpe_task')])
    success=execute(context,flat(),second)
    assert success['status']=='succeeded',success
    final=ids(call='',api='api-final')
    assert api_request(context,final)['tool_choice']=='none'
    response(context,final,[])
    verdict=evaluate_session(completed(context,db),db,workspace)
    assert verdict['verified'] is True and verdict['errors']==[],verdict


def test_fixed_json_style_cannot_be_reinterpreted(tmp_path, monkeypatch):
    workspace=tmp_path/'workspace'
    workspace.mkdir()
    (workspace/'source.json').write_text('{"a":1}')
    fixed=_prepare_request(flat('pretty'),workspace,None)
    fixed_path=tmp_path/'fixed.json'
    fixed_path.write_text(json.dumps(fixed))
    monkeypatch.setenv('RPE_HERMES_REQUIRED_REQUEST_PATH',str(fixed_path))
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    result=execute(context,flat('minify'),ids())
    assert result['error']['code']=='request_contract_mismatch'
    assert not (workspace/'result.json').exists()
    assert api_request(context,ids(api='api-correction'))['tool_choice']['function']['name']=='rpe_task'


def test_json_plan_style_drift_fails_even_if_redundant_hashes_are_replaced(tmp_path, monkeypatch):
    import sqlite3
    from rpe_harness.tasks import plan_from_dict
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    (workspace/'source.json').write_text('{"a":1}')
    first=ids()
    api_request(context,first)
    response(context,first,[('call-one','rpe_task')])
    result=execute(context,flat(),first)
    assert result['status']=='succeeded',result
    final=ids(call='',api='api-final')
    api_request(context,final)
    response(context,final,[])
    host=completed(context,db)
    with sqlite3.connect(db) as connection:
        row=connection.execute("SELECT id,evidence_json FROM steps WHERE stage='plan'").fetchone()
        evidence=json.loads(row[1])
        evidence['plan']['style']='minify'
        evidence['plan_sha256']=plan_from_dict(evidence['plan']).sha256
        connection.execute('UPDATE steps SET evidence_json=? WHERE id=?',(json.dumps(evidence),row[0]))
        row=connection.execute('SELECT artifact_json,verification_json FROM runs WHERE run_id=?',(result['run_id'],)).fetchone()
        values=[json.loads(value) for value in row]
        for value in values:
            value['plan_sha256']=evidence['plan_sha256']
        connection.execute('UPDATE runs SET artifact_json=?,verification_json=? WHERE run_id=?',
            (json.dumps(values[0]),json.dumps(values[1]),result['run_id']))
    verdict=evaluate_session(host,db,workspace)
    assert verdict['verified'] is False
    assert 'plan_contract_mismatch' in verdict['errors']


def test_missing_tabular_keys_can_be_corrected_and_final_gate_accepts_chain(tmp_path, monkeypatch):
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    (workspace/'source.csv').write_text('id,value\na,1\na,2\n')
    args={'input_path':'source.csv','output_path':'result.csv','goal':'retain first'}
    first=ids()
    api_request(context,first)
    response(context,first,[('call-one','rpe_task')])
    rejection=execute(context,args,first)
    assert rejection['error']['fields']==['key_columns']
    second=ids(call='call-two',api='api-two')
    assert api_request(context,second)['tool_choice']['function']['name']=='rpe_task'
    response(context,second,[('call-two','rpe_task')])
    success=execute(context,{**args,'key_columns':['id']},second)
    assert success['status']=='succeeded',success
    final=ids(call='',api='api-final')
    api_request(context,final)
    response(context,final,[])
    verdict=evaluate_session(completed(context,db),db,workspace)
    assert verdict['verified'] is True and verdict['errors']==[],verdict


def test_final_gate_bounds_json_source_hash_read_even_for_rejected_run(tmp_path, monkeypatch):
    from rpe_harness.integrations.hermes_gate import _verify_run
    payload=_prepare_request(flat(),tmp_path,None)
    (tmp_path/'source.json').write_bytes(b' ' * 1_048_577)
    receipt={'contract_sha256':digest_json(contract_for(payload,tmp_path)),
             'input_sha256':'0'*64}
    run={'event':payload,'status':'rejected','error':{'code':'invalid_json'}}
    original_read=type(tmp_path).read_bytes
    def unbounded_read(path):
        if path == tmp_path/'source.json':
            pytest.fail('final JSON gate must not use an unbounded source read')
        return original_read(path)
    monkeypatch.setattr('pathlib.Path.read_bytes',unbounded_read)
    with pytest.raises(ValueError):
        _verify_run(run,receipt,tmp_path,None)


def test_final_gate_bounds_json_output_hash_read(tmp_path, monkeypatch):
    from rpe_harness.integrations.hermes_gate import _verify_run, read_run
    context,workspace,db=strict_plugin(tmp_path,monkeypatch)
    (workspace/'source.json').write_text('{"a":1}')
    identity=ids()
    api_request(context,identity)
    response(context,identity,[('call-one','rpe_task')])
    result=execute(context,flat(),identity)
    assert result['status']=='succeeded',result
    receipt=next(e for e in HostEventStore(db).list('session-one') if e['kind']=='rpe_task_received')
    run=read_run(db,result['run_id'])
    output=workspace/'result.json'
    output.write_bytes(b' ' * (16*1_048_576+1))
    original_read=type(tmp_path).read_bytes
    def unbounded_read(path):
        if path == output:
            pytest.fail('final JSON gate must not use an unbounded output read')
        return original_read(path)
    monkeypatch.setattr('pathlib.Path.read_bytes',unbounded_read)
    with pytest.raises(ValueError):
        _verify_run(run,receipt,workspace,None)
