import sys,json,subprocess,hashlib,zipfile,os
from pathlib import Path
import rpe_harness,openpyxl
from rpe_harness.experiments.hermes_native import build_cases,validate_case
from rpe_harness.storage import RunStore
root=Path("/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T/rpe-v12-development-20261014-fp1l7bvp")/'installed-contract-release';root.mkdir()
db=root/'runs.sqlite'
temporary=root/'tmp';temporary.mkdir()
child_env={**os.environ,'TMPDIR':str(temporary),'TMP':str(temporary),'TEMP':str(temporary),'PYTHONDONTWRITEBYTECODE':'1'}
wheel=Path("/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T/rpe-v12-development-20261014-fp1l7bvp")/'wheel-release/rpe_harness-1.2.0-py3-none-any.whl'
site=Path(rpe_harness.__file__).parent.parent
z=zipfile.ZipFile(wheel)
matches={name:z.read(name)==(site/name).read_bytes() for name in z.namelist() if not name.endswith('/') and not name.endswith('/RECORD')}
assert all(matches.values()),matches
checks=[]
for case in build_cases(root/'fixtures'):
    workspace=Path(case['source_path']).parent
    execution={'input_path':str(workspace/case['input_path']),'output_path':str(workspace/case['output_path']),'key_columns':case['key_columns'],'encoding':case['encoding'],'delimiter':case['delimiter']}
    if case.get('sheet'):execution['sheet']=case['sheet']
    request={'schema_version':2,'event_id':'installed-'+case['id'],'capability':'tabular.deduplicate','goal':'按明确关键列去重并保留首条','constraints':['preserve_original','preserve_order'],'execution':execution}
    path=workspace/'request.json';path.write_text(json.dumps(request,ensure_ascii=False))
    command=[sys.executable,'-I','-B','-m','rpe_harness','run','--request',str(path),'--db',str(db)]
    proc=subprocess.run(command,cwd=workspace,env=child_env,capture_output=True,text=True,timeout=30)
    result=json.loads(proc.stdout)
    verification=result.get('verification') or {}
    if case['expected']=='success':
        from rpe_harness.experiments.hermes_native import _manual_truth
        passed=proc.returncode==0 and result['status']=='succeeded' and verification.get('passed') is True and _manual_truth(case,workspace/case['output_path'])
    else:
        from rpe_harness.experiments.hermes_native import _reason_matches
        passed=proc.returncode==2 and result['status']=='rejected' and not (workspace/case['output_path']).exists() and _reason_matches(case,(result.get('error') or {}).get('message'))
    unchanged=hashlib.sha256((workspace/case['input_path']).read_bytes()).hexdigest()==case['source_sha256']
    replay=subprocess.run(command,cwd=workspace,env=child_env,capture_output=True,text=True,timeout=30)
    replay_result=json.loads(replay.stdout)
    same=replay_result['run_id']==result['run_id'] and replay_result.get('artifact')==result.get('artifact')
    checks.append({'case_id':case['id'],'passed':passed and unchanged and same,'status':result['status'],'returncode':proc.returncode,'run_id':result['run_id'],'source_unchanged':unchanged,'replay_same_run_artifact':same,'verification':verification,'error':result.get('error'),'artifact':result.get('artifact')})
    (workspace/'run.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
report={'version':rpe_harness.__version__,'module_path':rpe_harness.__file__,'python':sys.version,'source_checkout_used':False,'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'installed_member_count':len(matches),'installed_members_match':all(matches.values()),'dependency':{'openpyxl':openpyxl.__version__,'module_path':openpyxl.__file__,'fresh_network_dependency_install_tested':True},'checks':checks,'passed':all(c['passed'] for c in checks)}
(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'passed':report['passed'],'checks':[{k:v for k,v in c.items() if k in ('case_id','passed','status','replay_same_run_artifact')} for c in checks],'wheel_sha256':report['wheel_sha256'],'installed_members_match':report['installed_members_match']},ensure_ascii=False))
assert report['passed']
