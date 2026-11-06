import hashlib,json,os
import pytest
from rpe_harness.integrations.hermes import register,HostEventStore,_digest_json

class Context:
    def __init__(self):self.hooks={}
    def register_hook(self,name,callback):self.hooks[name]=callback

@pytest.mark.parametrize('shape', ['legacy', 'current'])
def test_native_observer_binds_real_skill_bytes_and_tool_arguments(tmp_path,monkeypatch,shape):
    home=tmp_path/'session/home';skill=home/'skills/productivity/xlsx/SKILL.md'
    skill.parent.mkdir(parents=True);skill.write_text('actual native skill')
    sha=hashlib.sha256(skill.read_bytes()).hexdigest()
    config=tmp_path/'session/config.json'
    document={'snapshot_id':'snapshot','materialized_root':str(home/'skills'),
      'entries':[{'skill_id':'productivity/xlsx','name':'xlsx','load_name':'productivity/xlsx',
                  'skill_path':str(skill),'skill_sha256':sha}]}
    config.write_text(json.dumps(document))
    monkeypatch.setenv('RPE_HERMES_DB',str(tmp_path/'state.sqlite'))
    monkeypatch.setenv('RPE_HERMES_WORKSPACE',str(tmp_path))
    monkeypatch.setenv('RPE_HERMES_TOOL_ENABLED','0');monkeypatch.setenv('RPE_HERMES_SKILL_CONFIG',str(config))
    ctx=Context();register(ctx)
    ids={'session_id':'s','task_id':'t','turn_id':'turn','api_request_id':'api','tool_call_id':'call'}
    args={'name':'productivity/xlsx'}
    current={'path':'productivity/xlsx/SKILL.md','skill_dir':str(skill.parent)}
    source={'_source_path':str(skill)} if shape=='legacy' else current
    result={'success':True,'name':'xlsx','content':skill.read_text(),'readiness':'ready','setup_needed':False,**source}
    ctx.hooks['pre_tool_call'](**ids,tool_name='skill_view',args=args)
    ctx.hooks['post_tool_call'](**ids,tool_name='skill_view',args=args,status='ok',
      result=json.dumps(result))
    rows=HostEventStore(tmp_path/'state.sqlite').list('s')
    assert rows[0]['arguments_sha256']==_digest_json(args)
    assert rows[1]['status']=='loaded'
    assert rows[1]['content_sha256']==sha
    assert rows[1]['skill_source_path']==str(skill)
    assert rows[1]['skill_id']=='productivity/xlsx'
    for suffix,bad in enumerate([{'content':'forged'}, {'name':'same-copy-imposter'}, {'name':None},
            {**current,'_source_path':str(tmp_path/'SKILL.md')},
            {'path':str(skill)}, {'skill_dir':str(tmp_path)}]):
        ctx.hooks['post_tool_call'](**{**ids,'tool_call_id':f'bad-{suffix}'},
          tool_name='skill_view',args=args,status='ok',result=json.dumps({**result,**bad}))
        assert HostEventStore(tmp_path/'state.sqlite').list('s')[-1]['status']!='loaded'
    document['entries'][0].pop('name')
    config.write_text(json.dumps(document))
    incomplete=Context();register(incomplete)
    incomplete.hooks['post_tool_call'](**{**ids,'tool_call_id':'no-config-name'},
      tool_name='skill_view',args=args,status='ok',result=json.dumps(result))
    assert HostEventStore(tmp_path/'state.sqlite').list('s')[-1]['status']!='loaded'
    ctx.hooks['post_tool_call'](**{**ids,'tool_call_id':'linked-file'},
      tool_name='skill_view',args={**args,'file_path':'references/other.md'},status='ok',result=json.dumps(result))
    assert HostEventStore(tmp_path/'state.sqlite').list('s')[-1]['status']!='loaded'
    identical=tmp_path/'same-content.md';identical.write_bytes(skill.read_bytes())
    skill.unlink();skill.symlink_to(identical)
    ctx.hooks['post_tool_call'](**{**ids,'tool_call_id':'symlink'},
      tool_name='skill_view',args=args,status='ok',result=json.dumps(result))
    assert HostEventStore(tmp_path/'state.sqlite').list('s')[-1]['status']!='loaded'
    skill.unlink();os.link(identical,skill)
    ctx.hooks['post_tool_call'](**{**ids,'tool_call_id':'hardlink'},
      tool_name='skill_view',args=args,status='ok',result=json.dumps(result))
    assert HostEventStore(tmp_path/'state.sqlite').list('s')[-1]['status']!='loaded'

def test_native_terminal_receipt_requires_actual_zero_exit_result(tmp_path,monkeypatch):
    monkeypatch.setenv('RPE_HERMES_DB',str(tmp_path/'state.sqlite'))
    monkeypatch.setenv('RPE_HERMES_WORKSPACE',str(tmp_path));monkeypatch.setenv('RPE_HERMES_TOOL_ENABLED','0')
    ctx=Context();register(ctx)
    for number,exit_code in enumerate((0,1)):
        ctx.hooks['post_tool_call'](session_id='s',tool_name='terminal',tool_call_id=str(number),
          args={'command':'python helper.py'},status='ok',result=json.dumps({'exit_code':exit_code,'output':'private result'}))
    rows=HostEventStore(tmp_path/'state.sqlite').list('s')
    assert rows[0]['status']=='succeeded';assert rows[1]['status']=='failed'
    assert rows[0]['tool_exit_code']==0
    assert 'private result' not in json.dumps(rows)
