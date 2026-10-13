from pathlib import Path
import hashlib
import json
import shutil
import time
import rpe_harness
from rpe_harness.experiments.hermes_native import build_cases,validate_case
from rpe_harness.integrations import hermes_session as session
from rpe_harness.integrations.hermes_gate import evaluate_session

ROOT=Path('/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T/rpe-v12-development-20261014-fp1l7bvp')
OUT=ROOT/'probes/xlsx-entry-diagnostic'
ORIGINAL=ROOT/'installed-contract-release/hermes-anchored/fixtures'
MANIFEST=Path('/Users/audrey/项目/rpe-harness/reports/v1.2.0/evidence/frozen-source-manifest.json')
assert '/release-venv/' in rpe_harness.__file__
manifest=json.loads(MANIFEST.read_text())
assert manifest['wheel_sha256']=='97794dfa0b9c92294c32b6b9337b7d99aae927d1c9187efe58de73faa733b1ca'
package=Path(rpe_harness.__file__).parent
def check_source():
    actual={name:hashlib.sha256((package/Path(name).relative_to('src/rpe_harness')).read_bytes()).hexdigest()
            for name in manifest['files']}
    assert actual==manifest['files']
    assert len(actual)==32
    return actual
before=check_source()

WRAPPER='''
import os as _diag_os,json as _diag_json
from pathlib import Path as _diag_Path
from functools import wraps as _diag_wraps
from rpe_harness.integrations import hermes as _diag_hermes,hermes_gate as _diag_gate
_diag_path=DIAGNOSTIC_PATH
def _diag_shape(value):
    if not isinstance(value,dict): return {'type':type(value).__name__}
    answer={'type':'dict','keys':sorted(value),'types':{key:type(item).__name__ for key,item in value.items()}}
    for key in ('request','execution','state'):
        if isinstance(value.get(key),dict): answer[key]=_diag_shape(value[key])
    return answer
def _diag_emit(record):
    context=_diag_hermes._CALL_CONTEXT.get()
    record['native']={key:context[key] for key in ('session_id','task_id','turn_id','api_request_id','tool_call_id') if isinstance(context.get(key),str)}
    fd=_diag_os.open(_diag_path,_diag_os.O_WRONLY|_diag_os.O_APPEND|_diag_os.O_CREAT,0o600)
    with _diag_os.fdopen(fd,'w') as stream: stream.write(_diag_json.dumps(record,ensure_ascii=False,allow_nan=False)+'\\n')
def _diag_wrap(module,name):
    original=getattr(module,name)
    @_diag_wraps(original)
    def call(*args,**kwargs):
        record={'stage':name,'arguments':_diag_shape(args[0] if args else kwargs),
                'keyword_names':sorted(kwargs)}
        try:
            answer=original(*args,**kwargs)
        except Exception as error:
            trace=error.__traceback__
            while trace.tb_next: trace=trace.tb_next
            record.update(outcome='exception',exception_type=type(error).__name__,
                origin={'file':_diag_Path(trace.tb_frame.f_code.co_filename).name,
                        'function':trace.tb_frame.f_code.co_name,'line':trace.tb_lineno})
            _diag_emit(record)
            raise
        record['outcome']='ok'
        _diag_emit(record)
        return answer
    setattr(module,name,call)
_diag_wrap(_diag_hermes,'_prepare_request')
_diag_wrap(_diag_gate,'contract_for')
_diag_wrap(_diag_gate,'request_input_sha')
'''
original_prepare=session.prepare_home
def instrument_home(*args,**kwargs):
    home=original_prepare(*args,**kwargs)
    plugin=home/'plugins/rpe-harness/__init__.py'
    diagnostic=home.parent/'validation-diagnostic.jsonl'
    plugin.write_text(plugin.read_text()+WRAPPER.replace('DIAGNOSTIC_PATH',repr(str(diagnostic))))
    return home
session.prepare_home=instrument_home

cases=[case for case in build_cases(OUT/'fixtures') if case['id'] in ('xlsx_typed_keys','xlsx_formulas')]
results=[]
for case in cases:
    workspace=Path(case['source_path']).parent
    source=workspace/case['input_path']
    shutil.copyfile(ORIGINAL/case['id']/case['input_path'],source)
    case['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
    original=json.loads((ORIGINAL/case['id']/'request.json').read_text())
    original['event_id']='diagnostic-'+case['id']
    original['execution'].update(input_path=str(source),output_path=str(workspace/case['output_path']))
    (workspace/'request.json').write_text(json.dumps(original,ensure_ascii=False,indent=2))
    started=time.monotonic()
    print(json.dumps({'case_id':case['id'],'stage':'starting_authorized_diagnostic_session'}),flush=True)
    host=session.run_hermes_session(case['prompt'],workspace,OUT/'state/runs.sqlite',tool_enabled=True,
        owner_root=OUT/'owned-sessions',timeout=120,entry_mode='strict',required_request=original)
    gate=evaluate_session(host,OUT/'state/runs.sqlite',workspace,required_request=original)
    host['entry_validation']=gate
    truth=validate_case({**case,'tool_enabled':True,'db_path':str(OUT/'state/runs.sqlite')},workspace,host)
    owned=Path(host['session_root'])
    diagnostics=[]
    path=owned/'validation-diagnostic.jsonl'
    if path.exists(): diagnostics=[json.loads(line) for line in path.read_text().splitlines()]
    record={'case_id':case['id'],'source_sha256':case['source_sha256'],'elapsed_seconds':round(time.monotonic()-started,3),
      'diagnostics':diagnostics,'gate':gate,'manual_validation':truth,
      'host':{key:host.get(key) for key in ('completed','exit_code','timed_out','elapsed_seconds','usage','events',
          'model_identity','process_cleanup','observer_errors','session_id','prompt_sha256','original_prompt_sha256')},
      'session_raw_removed':False}
    assert owned.is_relative_to(OUT/'owned-sessions')
    assert host['process_cleanup']['cleanup_complete'] is True
    shutil.rmtree(owned)
    record['session_raw_removed']=not owned.exists()
    results.append(record)
    report={'experiment':'installed977_xlsx_validation_diagnostic','wheel_sha256':manifest['wheel_sha256'],
      'source_checkout_used':False,'module_path':rpe_harness.__file__,'source32_before':before,
      'source32_after':check_source(),'model_sessions':len(results),'cases':results,
      'completed':len(results)==2,'does_not_replace_original_six':True}
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'case_id':case['id'],'completed':host['completed'],'gate':gate['status'],
      'exceptions':[{'stage':d['stage'],'type':d.get('exception_type'),'origin':d.get('origin'),
                     'argument_keys':d['arguments'].get('keys'),'argument_types':d['arguments'].get('types')}
                    for d in diagnostics if d['outcome']=='exception'],
      'usage':host['usage'],'cleanup_complete':host['process_cleanup']['cleanup_complete']},ensure_ascii=False),flush=True)
