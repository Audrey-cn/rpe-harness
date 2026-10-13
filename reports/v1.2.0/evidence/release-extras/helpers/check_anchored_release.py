from pathlib import Path
import hashlib,json,time,shutil
import rpe_harness
from rpe_harness.experiments.hermes_native import build_cases,validate_case
from rpe_harness.integrations.hermes_session import run_hermes_session
from rpe_harness.integrations.hermes_gate import evaluate_session
from rpe_harness.storage import RunStore
from rpe_harness.evolution import EvolutionStore,propose_candidate,validate_candidate,apply_candidate,rollback,record_verified_feedback
root=Path("/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T/rpe-v12-development-20261014-fp1l7bvp");state=root/'installed-contract-release';db=state/'runs.sqlite'
out=state/'hermes-anchored';out.mkdir()
code_path=root/'codex-candidate-2/workspace/candidate.py';code=code_path.read_text()
assert '/release-venv/' in rpe_harness.__file__
candidate=None;results=[]
local=json.loads((state/'report.json').read_text())
for check in local['checks']:
 if check['passed'] and check['status']=='succeeded':
  assert record_verified_feedback(check['run_id'],db) is not None
wheel_sha=local['wheel_sha256']
for case in build_cases(out/'fixtures'):
 workspace=Path(case['source_path']).parent
 execution={'input_path':str(workspace/case['input_path']),'output_path':str(workspace/case['output_path']),'key_columns':case['key_columns'],'encoding':case['encoding'],'delimiter':case['delimiter']}
 if case.get('sheet'):execution['sheet']=case['sheet']
 request={'schema_version':2,'event_id':'anchored-'+case['id'],'capability':'tabular.deduplicate','goal':'按明确关键列保留首次完整记录','constraints':['preserve_original','preserve_order'],'execution':execution}
 if case['id']=='csv_multiline':
  evidence=[item['run_id'] for item in EvolutionStore(db).list_feedback() if item['plan']['input_format']=='csv']
  assert len(evidence)>=2
  proposal=propose_candidate(db,state,component='executor',kind='csv_executor',content=code,evidence_run_ids=evidence)
  checked=validate_candidate(proposal['proposal_id'],db);assert checked['passed']
  candidate=apply_candidate(proposal['proposal_id'],db)
 (workspace/'request.json').write_text(json.dumps(request,ensure_ascii=False,indent=2))
 host=run_hermes_session(case['prompt'],workspace,db,tool_enabled=True,owner_root=root,timeout=180,entry_mode='strict',required_request=request)
 gate=evaluate_session(host,db,workspace,required_request=request)
 host['entry_validation']=gate;truth=validate_case({**case,'tool_enabled':True,'db_path':str(db)},workspace,host)
 runs=[RunStore(db).get(e['run_id']) for e in host['events'] if e.get('kind')=='post_tool_call' and e.get('run_id')]
 unique={run['run_id']:run for run in runs};runs=list(unique.values())
 candidate_used=None
 if case['id']=='csv_multiline':
  candidate_used=any(run.get('artifact',{}).get('actual_executor',{}).get('candidate_version_id')==candidate['version_id'] and run['artifact']['actual_executor']['code_sha256']==hashlib.sha256(code_path.read_bytes()).hexdigest() for run in runs if run.get('artifact'))
  active=EvolutionStore(db).pointer('executor','csv_executor')[0]
  if active==candidate['version_id']:
   restoration=rollback(candidate['version_id'],db)
  else:
   restoration={'rolled_back':False,'active_pointer':active,'candidate_state':EvolutionStore(db).get_version(candidate['version_id'])['state']}
 else:restoration=None
 if host.get('process_cleanup',{}).get('cleanup_complete'):
  shutil.rmtree(host['session_root'])
 compact={k:host.get(k) for k in ('completed','exit_code','timed_out','elapsed_seconds','usage','events','tool_audit','model_identity','process_cleanup','observer_errors','session_id')}
 entry_ok=gate.get('harness_verified') is True if case['expected']=='success' else gate.get('status')=='rejected' and not gate.get('errors')
 result={'case_id':case['id'],'expected':case['expected'],'entry_accepted':entry_ok,'entry_validation':gate,'validation':truth,'host':compact,'run_ids':list(unique),'candidate_used':candidate_used,'rollback':restoration,'source_sha256':case['source_sha256'],'prompt_sha256':host.get('prompt_sha256'),'original_prompt_sha256':host.get('original_prompt_sha256'),'contract_prompt_injected':host.get('contract_prompt_injected')}
 for run in runs:(workspace/(run['run_id']+'-run.json')).write_text(json.dumps(run,ensure_ascii=False,indent=2))
 results.append(result)
 report={'probe':'installed_final_anchored_six_case_e2e','source_checkout_used':False,'wheel_sha256':wheel_sha,'module_path':rpe_harness.__file__,'version':rpe_harness.__version__,'candidate_generated_sha256':hashlib.sha256(code_path.read_bytes()).hexdigest(),'candidate_activation':candidate,'cases':results,'completed':len(results)==6,'accepted':len(results)==6 and all(r['validation']['passed'] and r['entry_accepted'] for r in results) and any(r['candidate_used'] is True for r in results)}
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({'case_id':case['id'],'manual_accepted':truth['passed'],'gate':gate['status'],'errors':gate.get('errors'),'candidate_used':candidate_used,'elapsed_seconds':host['elapsed_seconds']},ensure_ascii=False),flush=True)
print(json.dumps({'completed':report['completed'],'accepted':report['accepted'],'accepted_cases':sum(r['validation']['passed'] and r['entry_accepted'] for r in results)},ensure_ascii=False),flush=True)

