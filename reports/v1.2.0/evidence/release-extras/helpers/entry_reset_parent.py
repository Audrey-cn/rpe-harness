"""Root-executed live continuation probe; never part of the formal 36 sessions.

Run with the release-venv interpreter after the formal model runs finish. The
parent's authorized installed launcher reads settings only at execution time.
"""
from __future__ import annotations

import argparse
import atexit
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid


PREFIX='RPE_ENTRY_RESET_RESULT '


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_sha(directory):
    return hashlib.sha256(json.dumps([(str(p.relative_to(directory)),sha_file(p))
        for p in sorted(directory.rglob('*.py'))],separators=(',',':')).encode()).hexdigest()


def run(args):
    from rpe_harness.integrations import hermes,hermes_session as launcher
    from rpe_harness.integrations.hermes_gate import contract_for,evaluate_session,read_run
    owner=Path(args.owner_root).resolve(strict=True)
    if not Path(sys.executable).absolute().is_relative_to(owner/'release-venv'):
        raise ValueError('run this probe with the release-venv interpreter')
    import_root=Path(hermes.__file__).resolve().parents[2]
    if not import_root.is_relative_to(owner/'release-venv'):
        raise ValueError('the installed release package must be active; unset PYTHONPATH')
    install=Path(args.hermes_root).resolve(strict=True)
    root=owner/'probes'/(('sdk-startup-diagnostic-' if args.startup_only else 'entry-reset-')+uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    workspace=root/'workspace'
    workspace.mkdir(mode=0o700)
    db=root/'runs.sqlite'
    package_sha_before=tree_sha(import_root/'rpe_harness')
    sdk_files=[install/'run_agent.py',install/'hermes_bootstrap.py',
               install/'agent/turn_facade.py',install/'agent/turn_context.py']
    sdk_sha_before={str(p):sha_file(p) for p in sdk_files}
    host_python=launcher._installed_python(install)
    # Root explicitly executes this parent. It uses the existing launcher reader;
    # no key, credential body or credential-bearing argv is logged or retained.
    settings=launcher._read_host_settings(install,host_python)
    session=root/'session'
    home=launcher.prepare_home(session,workspace,db,tool_enabled=True,
                               settings=settings,entry_mode='strict')
    for name in ('tmp','runtime','terminal-tmp'):
        (session/name).mkdir(mode=0o700)
    fixtures=[('first','id,value\na,10\na,99\nb,20\n',[['id','value'],['a','10'],['b','20']]),
              ('second','id,value\nx,30\ny,40\nx,88\n',[['id','value'],['x','30'],['y','40']])]
    rounds=[]
    for name,text,truth in fixtures:
        source=workspace/(name+'.csv')
        output=workspace/(name+'-out.csv')
        source.write_text(text,encoding='utf-8')
        business={'input_path':str(source),'output_path':str(output),'key_columns':['id'],
                  'goal':'retain the first record for each id',
                  'constraints':['preserve_original','preserve_order']}
        prompt='请调用任务工具，按照以下业务参数完成这一轮 CSV 去重，并报告工具的最终结果：\n'+json.dumps(business,ensure_ascii=False)
        expected={'schema_version':2,'event_id':'probe-'+name,'capability':'tabular.deduplicate',
                  'goal':business['goal'],'constraints':business['constraints'],
                  'execution':{k:v for k,v in business.items() if k not in ('goal','constraints')}}
        rounds.append({'name':name,'prompt':prompt,'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
                       'expected_request':expected,'input_sha256':sha_file(source),'truth':truth})
    tasks={'model':settings['model'],'base_url':settings['base_url'],'rounds':rounds}
    (session/'tasks.json').write_text(json.dumps(tasks,ensure_ascii=False))
    child_script=Path(__file__).with_name('entry_reset_sdk.py').resolve(strict=True)
    profile=session/'hermes.sb'
    profile_body=launcher.sandbox_profile([session,workspace],Path.home()/'.hermes',install,db,
        [host_python.parent.parent,Path.home()/'.hermes/tools'],
        owner_read_scope=(owner,[session,workspace,db,import_root,
            Path(sys.executable).absolute().parent.parent,child_script]))
    child_path=json.dumps(str(child_script),ensure_ascii=False)
    assert profile_body.count('(subpath '+child_path+')')==1
    # The SDK helper is one known file, never a readable probes directory.
    profile.write_text(profile_body.replace('(subpath '+child_path+')','(literal '+child_path+')'))
    env={k:v for k,v in os.environ.items() if k in {
        'HOME','USER','LOGNAME','SHELL','PATH','LANG','LC_ALL','LC_CTYPE','SSL_CERT_FILE',
        'SSL_CERT_DIR','REQUESTS_CA_BUNDLE','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY'}}
    token=uuid.uuid4().hex
    env.update({'HERMES_HOME':str(home),'HERMES_RUNTIME_DIR':str(session/'runtime'),
        'HERMES_GUEST_ONBOARDING':'0','HERMES_ENABLE_PROJECT_PLUGINS':'0',
        'HERMES_DISABLE_LAZY_INSTALLS':'1','PYTHONDONTWRITEBYTECODE':'1',
        'TMPDIR':str(session/'tmp'),'TMP':str(session/'tmp'),'TEMP':str(session/'tmp'),
        'RPE_HERMES_INSTALL_ROOT':str(install),'RPE_HERMES_DB':str(db),
        'RPE_HERMES_WORKSPACE':str(workspace),'RPE_HERMES_TOOL_ENABLED':'1',
        'RPE_HERMES_ENTRY_MODE':'strict','RPE_HERMES_ENTRY_KEY':uuid.uuid4().hex,
        'RPE_HERMES_MAX_TASK_ATTEMPTS':'3','RPE_HERMES_SESSION_TOKEN':token,
        'RPE_HERMES_ERROR_PATH':str(session/'observer-errors.jsonl'),
        'RPE_HERMES_PROCESS_PATH':str(session/'child-processes.jsonl'),
        'RPE_HARNESS_PYTHON':sys.executable,'RPE_HARNESS_IMPORT_ROOT':str(import_root),
        'RPE_PROBE_RUN_ROOT':str(root),'PYTHONPATH':str(import_root)})
    if args.startup_only:
        env['RPE_PROBE_STARTUP_ONLY']='1'
    env['PATH']=str(Path(sys.executable).parent)+os.pathsep+env.get('PATH','/usr/bin:/bin')
    store=hermes.HostEventStore(db)
    started=time.monotonic()
    timed_out=False
    owned={}
    cleanup_errors=[]
    signaled=[]
    remaining=[]
    stdout=session/'stdout.log'
    stderr=session/'stderr.log'
    from rpe_harness.integrations.hermes_broker import execution_broker
    with execution_broker(db_path=db, workspace=workspace, python=sys.executable) as broker_env:
        env.update(broker_env)
        with stdout.open('w') as out,stderr.open('w') as err:
            process=subprocess.Popen(['/usr/bin/sandbox-exec','-f',str(profile),str(host_python),'-B',
                                      str(child_script)],cwd=workspace,env=env,stdout=out,stderr=err,
                                     start_new_session=True)
            identity=None
            try:
                try:
                    identity=launcher._process_table().get(process.pid,(None,None))[1]
                except Exception as error:
                    cleanup_errors.append({'stage':'initial_identity','error_type':type(error).__name__})
                deadline=started+(60 if args.startup_only else 360)
                while process.poll() is None:
                    if identity:
                        try:
                            launcher._capture_descendants(process.pid,identity,owned)
                        except Exception as error:
                            cleanup_errors.append({'stage':'capture','error_type':type(error).__name__})
                    if time.monotonic()>=deadline:
                        timed_out=True
                        break
                    try:
                        process.wait(timeout=min(1,max(0.01,deadline-time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if identity:
                    try:
                        launcher._capture_descendants(process.pid,identity,owned)
                    except Exception as error:
                        cleanup_errors.append({'stage':'final_capture','error_type':type(error).__name__})
                if process.poll() is None:
                    try:
                        launcher._stop_group(process)
                    except Exception as error:
                        cleanup_errors.append({'stage':'stop_group','error_type':type(error).__name__})
                for stage,operation in [
                    ('registry',lambda:owned.update(launcher._registered_processes(session/'child-processes.jsonl',process.pid))),
                    ('tags',lambda:owned.update(launcher._tagged_processes(token)))]:
                    try:
                        operation()
                    except Exception as error:
                        cleanup_errors.append({'stage':stage,'error_type':type(error).__name__})
                try:
                    signaled=launcher._stop_descendants(owned)
                except Exception as error:
                    cleanup_errors.append({'stage':'stop_descendants','error_type':type(error).__name__})
                try:
                    remaining=launcher._remaining_descendants(owned)
                except Exception as error:
                    cleanup_errors.append({'stage':'verify_descendants','error_type':type(error).__name__})
    cleanup={'cleanup_complete':not cleanup_errors and not remaining and process.poll() is not None
             and broker_env.cleanup.get('cleanup_complete') is True,
             'external_executor':broker_env.cleanup,
             'tracked_descendants':len(owned),'signaled_descendants':len(signaled),
             'remaining_descendants':remaining,'errors':cleanup_errors}
    if cleanup['cleanup_complete']:
        # Even a later parsing/acceptance failure must delete the copied key and
        # raw transcript, after the exact host and owned descendants are stopped.
        atexit.register(shutil.rmtree,session,ignore_errors=True)
    sdk_result={}
    for line in stdout.read_text(errors='replace').splitlines():
        if line.startswith(PREFIX):
            try:
                sdk_result=json.loads(line[len(PREFIX):])
            except ValueError:
                pass
    session_id=sdk_result.get('session_id')
    events=store.list(session_id) if session_id else store.list()
    errors=session/'observer-errors.jsonl'
    observer_errors=len(errors.read_text().splitlines()) if errors.exists() else 0
    host={'completed':sdk_result.get('completed') is True,'exit_code':process.returncode,
          'timed_out':timed_out,'observer_errors':observer_errors,'session_id':session_id,
          'events':events,'process_cleanup':cleanup}
    gate=evaluate_session(host,db,workspace)
    truth_results=[]
    for index,(expected,observed) in enumerate(zip(rounds,sdk_result.get('rounds') or []),1):
        execution=expected['expected_request']['execution']
        source=Path(execution['input_path'])
        output=Path(execution['output_path'])
        records=None
        if output.is_file():
            with output.open(encoding='utf-8-sig',newline='') as stream:
                records=list(csv.reader(stream))
        runs=observed.get('run_ids') or []
        try:
            actual=read_run(db,runs[0]) if len(runs)==1 and runs[0] else None
        except (ValueError,OSError,KeyError):
            actual=None
        truth_results.append({'round':index,'source_unchanged':sha_file(source)==expected['input_sha256'],
            'manual_csv_truth_matches':records==expected['truth'],
            'request_matches':actual is not None and contract_for(actual['event'],workspace)==
                contract_for(expected['expected_request'],workspace),
            'prompt_sha256':expected['prompt_sha256'],'input_sha256':expected['input_sha256'],
            'output_sha256':sha_file(output) if output.is_file() else None})
    sdk_rounds=sdk_result.get('rounds') or []
    distinct_turns=(len(sdk_rounds)==2 and all(len(r.get('turn_ids') or [])==1 for r in sdk_rounds)
                    and sdk_rounds[0]['turn_ids']!=sdk_rounds[1]['turn_ids'])
    reset=(distinct_turns and all(r.get('request_choices')==['rpe_task','none'] and
                                len(r.get('tool_call_ids') or [])==1 for r in sdk_rounds))
    continuation=(sdk_result.get('agent_instance_count')==1 and len(sdk_rounds)==2 and
                  sdk_rounds[0].get('returned_message_count',0)>0 and
                  sdk_rounds[1].get('history_messages_passed')==sdk_rounds[0]['returned_message_count'])
    source_unchanged=(package_sha_before==tree_sha(import_root/'rpe_harness') and
                      sdk_sha_before=={str(p):sha_file(p) for p in sdk_files})
    report={'probe':'same_agent_two_round_entry_reset','formal_experiment':False,
            'startup_only':args.startup_only,
            'model_deadline_seconds':None if args.startup_only else 360,
            'startup_deadline_seconds':60 if args.startup_only else None,
            'per_round_budget_seconds':None if args.startup_only else 180,
            'elapsed_seconds':round(time.monotonic()-started,3),'sdk_result':sdk_result,
            'entry_reset_observed':reset,'unmodified_history_continuation_observed':continuation,'entry_validation':gate,'csv_truth':truth_results,
            'host':host,'usage':launcher.aggregate_usage(events),'source_unchanged':source_unchanged,
            'package_source_sha256':package_sha_before,'sdk_source_sha256':sdk_sha_before,
            'accepted':gate.get('verified') is True and reset and continuation and source_unchanged and
                len(truth_results)==2 and all(t['source_unchanged'] and t['manual_csv_truth_matches'] and
                t['request_matches'] for t in truth_results)}
    # The only surviving report fields are compact metadata and synthetic CSV truth.
    encoded=json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)
    if settings['api_key'] in encoded:
        raise ValueError('credential appeared in safe report')
    (root/'report.json').write_text(encoded,encoding='utf-8')
    if cleanup['cleanup_complete']:
        shutil.rmtree(session)
    else:
        report['session_temp_retained_for_cleanup']=True
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(root/'report.json'),'accepted':report['accepted'],
                     'cleanup_complete':cleanup['cleanup_complete'],
                     'startup_diagnostic':sdk_result if args.startup_only else None},ensure_ascii=False))
    if args.startup_only:
        return 0 if (sdk_result.get('startup_completed') is True and cleanup['cleanup_complete']
                     and launcher.aggregate_usage(events)['api_requests']==0) else 2
    return 0 if report['accepted'] else 2


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Live isolated two-round Hermes SDK probe (Root execution only)')
    parser.add_argument('--owner-root',required=True)
    parser.add_argument('--hermes-root',default='/Users/audrey/.hermes/hermes-agent')
    parser.add_argument('--startup-only',action='store_true',help='Stop before AIAgent construction; no model calls')
    args=parser.parse_args()
    try:
        raise SystemExit(run(args))
    except Exception as error:
        print(json.dumps({'completed':False,'error_type':type(error).__name__}),file=sys.stderr)
        raise SystemExit(3)
