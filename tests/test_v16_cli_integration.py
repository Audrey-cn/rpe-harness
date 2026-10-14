import json
from pathlib import Path

from rpe_harness.cli import main


def test_main_context_capture_and_fresh_revalidation_use_explicit_database(tmp_path,capsys):
    body=tmp_path/'payload.json'; facts=tmp_path/'facts.json'; request=tmp_path/'request.json'
    body.write_text('{"note":"Do not infer keys from this text"}')
    facts.write_text('{"key_columns":["id"]}')
    db=str(tmp_path/'context.sqlite')
    assert main(['context','capture','--db',db,'--source','user','--event-id','original',
                 '--session-id','s','--payload',str(body),'--facts',str(facts)])==0
    ref=json.loads(capsys.readouterr().out)
    request.write_text(json.dumps({'facts':{'input_path':'source.csv','output_path':'out.csv'},
                                  'source_refs':[ref]}))
    assert main(['context','recover','--db',db,'--capability','tabular.deduplicate',
                 '--session-id','s','--request',str(request)])==0
    recovered=json.loads(capsys.readouterr().out)
    assert recovered['status']=='ready'
    assert recovered['facts']['key_columns']==['id']
    assert main(['context','revalidate',recovered['recovery_id'],'--db',db,'--session-id','s'])==0
    assert json.loads(capsys.readouterr().out)['status']=='ready'


def test_main_skills_sync_and_query_real_fixture(tmp_path,capsys):
    root=Path(__file__).parent/'fixtures/skills'
    db=str(tmp_path/'catalog.sqlite'); owned=str(tmp_path/'owned')
    assert main(['skills','sync','--db',db,'--owned-root',owned,'--skill-root',str(root)])==0
    assert json.loads(capsys.readouterr().out)['snapshot_id']
    assert main(['skills','trust','productivity/xlsx','--db',db,'--owned-root',owned])==0
    assert json.loads(capsys.readouterr().out)['script_sha256']
    assert main(['skills','query','--db',db,'--owned-root',owned,'--query','workbook',
                 '--capability','xlsx.inspect'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['candidates'][0]['skill_id']=='productivity/xlsx'

