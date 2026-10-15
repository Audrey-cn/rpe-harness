from pathlib import Path
import json,subprocess,sys,hashlib,zipfile
from rpe_harness import __version__
from rpe_harness.requirements import SourceStateStore, source_document
from rpe_harness.runtime import HarnessRuntime
root=Path(sys.argv[1]); wheel=Path(sys.argv[2]); checkout=Path(sys.argv[3]); target=root/'installed-check'; target.mkdir()
checks=[]
def check(name,condition):
 if not condition:raise AssertionError(name)
 checks.append(name)
def cli(*args):
 p=subprocess.run([sys.executable,'-B','-m','rpe_harness',*args],cwd=target,text=True,capture_output=True)
 return p,json.loads(p.stdout)
check('installed_version',__version__=='1.9.0')
(root/'installed-files.json').write_text('{}')
source={'capability':'json.normalize','constraints':['preserve_original','preserve_order'],'execution':{'input_path':'input.json','output_path':'output.json','style':'minify'}}
(target/'input.json').write_text(' { "n" : 1.20e+2, "s" : "\\u4e2d" } \n')
(target/'task.json').write_text(json.dumps(source))
db=target/'state.sqlite';session='installed-check';ref='user:installed-check'
p,h=cli('source','prepare','--source',str(target/'task.json'),'--workspace',str(target),'--db',str(db),'--session-id',session,'--source-ref',ref)
check('installed_source_prepare',p.returncode==0 and h['status']=='ready' and len(h['requirements_sha256'])==64)
p,shown=cli('source','show',h['state_id'],'--db',str(db),'--session-id',session)
check('installed_source_show',p.returncode==0 and shown==h)
from rpe_harness.capability_catalog import build_catalog
resolved=SourceStateStore(db).resolve(h['state_id'],session_id=session,catalog_sha256=build_catalog().snapshot_id)
check('installed_ledger_all_fields',len(resolved['requirements'])==7)
# Packaging smoke of the original execution path; clean controller/SDK separately validated.
result=HarnessRuntime(db_path=db).run(resolved['request'],source='installed-package-smoke')
check('installed_original_execution',result['status']=='succeeded')
check('installed_literal_truth',(target/'output.json').read_text()=='{"n":1.20e+2,"s":"\\u4e2d"}\n')
for name,raw,status in [('prose','normalize this and remove refunds','needs_context'),('extra',json.dumps({**source,'refund_policy':'delete'}),'unsupported'),('duplicate','{"capability":"json.normalize","capability":"tabular.deduplicate"}','rejected')]:
 (target/'bad.json').write_text(raw)
 p,v=cli('source','prepare','--source',str(target/'bad.json'),'--workspace',str(target),'--db',str(db),'--session-id',session,'--source-ref',ref)
 check('installed_'+name,v['status']==status and p.returncode in (2,3))
import rpe_harness
installed=Path(rpe_harness.__file__).parent
hashes={}
with zipfile.ZipFile(wheel) as archive:
 for path in sorted((checkout/'src/rpe_harness').rglob('*.py')):
  relative=path.relative_to(checkout/'src'); data=path.read_bytes()
  check('wheel_source_'+str(relative),archive.read(str(relative))==data)
  check('installed_source_'+str(relative),(installed/path.relative_to(checkout/'src/rpe_harness')).read_bytes()==data)
  hashes[str(relative)]=hashlib.sha256(data).hexdigest()
summary={'version':__version__,'checks_passed':len(checks),'source_python_files':len(hashes),'source_hashes':hashes,'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'model_calls':0,'limitations':'Packaging smoke uses original executor; clean native/controller proof separately accepted before packaging.'}
(root/'installed-verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in summary.items() if k!='source_hashes'}))
