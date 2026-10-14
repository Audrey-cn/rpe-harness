#!/usr/bin/env python3
"""One-shot installed-wheel Hermes acceptance; saves no model text or credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

import rpe_harness
from rpe_harness.integrations import hermes_session as host
from rpe_harness.integrations.hermes import HostEventStore
from rpe_harness.integrations.hermes_gate import evaluate_session, read_run

# Hand-authored byte truths. Never supplied in a prompt or a model-readable workspace file.
JSON_INPUT = b' { "numbers" : [9007199254740993, 1.2300, 1e999, -0], "text" : "x\\u0061  y\\n\\t", "empty" : {} } \n'
JSON_PRETTY = b'{\n  "numbers": [\n    9007199254740993,\n    1.2300,\n    1e999,\n    -0\n  ],\n  "text": "x\\u0061  y\\n\\t",\n  "empty": {}\n}\n'
JSON_MINIFY = b'{"numbers":[9007199254740993,1.2300,1e999,-0],"text":"x\\u0061  y\\n\\t","empty":{}}\n'
CSV_INPUT = 'id,region,value,note\n001,east,10,first\n002,south,20,second\n001,east,99,later\n003,north,30,last\n002,south,77,later\n'.encode('utf-8-sig')
CSV_EXPECTED = 'id,region,value,note\r\n001,east,10,first\r\n002,south,20,second\r\n003,north,30,last\r\n'.encode('utf-8-sig')


def digest_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    return digest_bytes(Path(path).read_bytes())


def explicit_path(path, root, *, exists=True):
    submitted = Path(path).expanduser()
    if not submitted.is_absolute():
        raise ValueError('explicit absolute owned path required')
    cursor = Path(submitted.anchor)
    for part in submitted.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('owned path contains a symlink')
    resolved = submitted.resolve(strict=exists)
    if not resolved.is_relative_to(root):
        raise ValueError('path escaped owned root')
    return resolved


def readonly_candidate(db, version_id):
    # Avoid schema initialization, candidate activation, and activation recovery.
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        row = connection.execute('SELECT v.state,p.kind,p.content_hash,p.validation_hash,p.validation_json '
            'FROM evolution_versions v JOIN evolution_proposals p ON p.proposal_id=v.proposal_id '
            'WHERE v.version_id=?', (version_id,)).fetchone()
        pointer = connection.execute("SELECT version_id FROM evolution_active WHERE slot='executor:json_executor'").fetchone()
    if row is None or pointer is None or pointer['version_id'] != version_id:
        raise ValueError('requested JSON candidate must already be active')
    validation = json.loads(row['validation_json'] or 'null')
    if row['state'] != 'active' or row['kind'] != 'json_executor' or not validation or validation.get('passed') is not True:
        raise ValueError('JSON candidate must be an active verified JSON executor')
    return {'version_id':version_id,'kind':row['kind'],'state':row['state'],
            'content_sha256':row['content_hash'],'certificate_sha256':row['validation_hash'],
            'fixture_checks':len(validation.get('checks',[]))}


class ProcessGuard:
    """Retain exact Popen ownership even if the launcher raises before returning evidence."""
    def __init__(self):
        self.original = host.subprocess.Popen
        self.processes = []
        self.tokens = set()
        self.errors = []

    def start(self, *args, **kwargs):
        process = self.original(*args, **kwargs)
        if kwargs.get('start_new_session') is True:
            self.processes.append(process)
            token = (kwargs.get('env') or {}).get('RPE_HERMES_SESSION_TOKEN')
            if isinstance(token,str) and token:
                self.tokens.add(token)
        return process

    def __enter__(self):
        host.subprocess.Popen = self.start
        return self

    def __exit__(self, *unused):
        host.subprocess.Popen = self.original
        owned = {}
        for token in self.tokens:
            try:
                owned.update(host._tagged_processes(token))
            except Exception as exc:
                self.errors.append({'stage':'runner_query_owned','type':type(exc).__name__})
        for process in reversed(self.processes):
            try:
                if process.poll() is None:
                    host._stop_group(process)
            except Exception as exc:
                self.errors.append({'stage':'runner_stop_group','type':type(exc).__name__})
        try:
            signaled = host._stop_descendants(owned)
            remaining = host._remaining_descendants(owned)
        except Exception as exc:
            self.errors.append({'stage':'runner_stop_owned','type':type(exc).__name__})
            signaled,remaining = [],list(owned)
        alive = [p.pid for p in self.processes if p.poll() is None]
        self.report = {'tracked_processes':len(self.processes),'signaled_descendants':len(signaled),
            'remaining_processes':sorted(set(alive+remaining)),
            'cleanup_complete':not self.errors and not alive and not remaining,'errors':self.errors}


def safe_remove_sessions(owner):
    failures = []
    for path in owner.glob('session-*'):
        try:
            if path.is_symlink() or not path.is_dir():
                raise ValueError('unexpected session root type')
            shutil.rmtree(path)
        except Exception as exc:
            failures.append({'stage':'session_root_cleanup','type':type(exc).__name__})
    return {'session_roots_remaining':len(list(owner.glob('session-*'))), 'errors':failures}


def durable_summary(run):
    error = run.get('error') or {}
    return {'run_id':run['run_id'],'status':run['status'],'verification':run['verification'],
            'artifact':run['artifact'],'step_stages':[s['stage'] for s in run['steps']],
            'error':{k:error[k] for k in ('type','code') if k in error}}


def main(args):
    root = Path(args.owned_root).expanduser().resolve(strict=True)
    if '/site-packages/' not in str(Path(rpe_harness.__file__).absolute()):
        raise ValueError('runner requires installed wheel in site-packages')
    if rpe_harness.__version__ != '1.5.0':
        raise ValueError('runner requires version 1.5.0')
    db = explicit_path(args.db,root)
    if not db.is_file():
        raise ValueError('explicit installed acceptance database is required')
    report_path = explicit_path(args.report,root,exists=False)
    if report_path.exists() or not report_path.parent.is_dir():
        raise ValueError('report must be a new file in an existing owned directory')
    hermes_root = Path(args.hermes_root).expanduser().resolve(strict=True)
    nonce = uuid.uuid4().hex
    run_root = root/('live-v15-'+nonce)
    run_root.mkdir(mode=0o700)
    workspace_root = run_root/'workspaces'; workspace_root.mkdir(mode=0o700)
    sessions_root = run_root/'sessions'; sessions_root.mkdir(mode=0o700)
    report = {'passed':False,'version':rpe_harness.__version__,'module_path':rpe_harness.__file__,
        'runner_sha256':file_sha(__file__),'run_root':str(run_root),'db_path':str(db),
        'strict_mode':True,'required_policy':True,'model_session_retries':0,
        'global_changes_by_runner':False,'candidate_preflight':None,'sessions':[],'exceptions':[]}
    # Exclusive report creation protects earlier evidence; later saves update only this owned file.
    descriptor = os.open(report_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    os.close(descriptor)
    def save():
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    save()
    try:
        candidate = readonly_candidate(db,args.candidate_version_id)
        report['candidate_preflight'] = candidate
        save()
        cases = [('json-pretty','json',JSON_INPUT,JSON_PRETTY,'pretty'),
                 ('json-minify','json',JSON_INPUT,JSON_MINIFY,'minify')]
        for name,suffix,source_bytes,expected,style in cases:
            workspace = workspace_root/name; workspace.mkdir(mode=0o700)
            session_owner = sessions_root/name; session_owner.mkdir(mode=0o700)
            source,output = workspace/('source.'+suffix), workspace/('result.'+suffix)
            source.write_bytes(source_bytes)
            request = {'schema_version':2,'event_id':'live-v15-'+nonce+'-'+name,
                'goal':'按明确契约处理文件，保留原始输入与顺序',
                'capability':'json.normalize' if style else 'tabular.deduplicate',
                'constraints':['preserve_original','preserve_order'],
                'execution':{'input_path':str(source),'output_path':str(output)}}
            if style:
                request['execution']['style'] = style
                prompt = (f'请实际调用 rpe_task，以显式 capability=json.normalize 和 style={style} '
                    f'处理工作区 {source.name}，将结果另存为 {output.name}。只改变字符串外的空白，'
                    '保留所有原始数字与字符串 token、成员和数组顺序，原始文件不可修改。根据工具返回说明结果。')
            else:
                request['execution'].update(key_columns=['id','region'],keep='first')
                prompt = (f'请实际调用 rpe_task，按 id 和 region 的组合键对工作区 {source.name} 去重，'
                    f'保留首次出现的记录和原有顺序，将结果另存为 {output.name}。'
                    '显式 capability=tabular.deduplicate，原始文件不可修改。根据工具返回说明结果。')
            record = {'case':name,'required_request':request,'passed':False,'manual_checks':{},'exceptions':[]}
            report['sessions'].append(record); save()
            print(json.dumps({'stage':'session_start','case':name}),flush=True)
            result = None
            guard = ProcessGuard()
            before_ids = {e['ingest_event_id'] for e in HostEventStore(db).list()}
            try:
                with guard:
                    result = host.run_hermes_session(prompt,workspace,db,tool_enabled=True,
                        owner_root=session_owner,timeout=180,hermes_root=hermes_root,
                        entry_mode='strict',required_request=request,max_task_attempts=3)
                record.update({key:result.get(key) for key in ('completed','exit_code','timed_out',
                    'session_id','usage','observer_errors','elapsed_seconds','model_identity',
                    'prompt_sha256','original_prompt_sha256','contract_prompt_injected','entry_mode',
                    'input_sha256','process_cleanup')})
                record['events'] = result['events']
                gate = evaluate_session(result,db,workspace,required_request=request,require_policy=True)
                record['gate'] = gate
                runs = []
                for event in result['events']:
                    run_id = event.get('run_id') if event['kind']=='rpe_task_returned' else None
                    if run_id and run_id not in runs: runs.append(run_id)
                record['durable_runs'] = [durable_summary(read_run(db,run_id)) for run_id in runs]
                run = read_run(db,gate['run_id']) if gate.get('run_id') else None
                manual = record['manual_checks']
                manual.update(source_sha256=digest_bytes(source_bytes),expected_sha256=digest_bytes(expected),
                    source_unchanged=file_sha(source)==digest_bytes(source_bytes),output_exists=output.is_file())
                if output.is_file():
                    manual['output_sha256'] = file_sha(output)
                    manual['literal_bytes_match'] = output.read_bytes()==expected
                else:
                    manual['literal_bytes_match'] = False
                actual = ((run or {}).get('artifact') or {}).get('actual_executor',{})
                manual['requested_candidate_used'] = actual.get('candidate_version_id')==args.candidate_version_id
                manual['candidate_code_sha256_match'] = actual.get('code_sha256')==candidate['content_sha256']
                checks = [value for value in manual.values() if type(value) is bool]
                record['passed'] = (gate.get('verified') is True and gate.get('errors')==[] and all(checks)
                    and (result.get('usage') or {}).get('coverage_complete') is True
                    and (result.get('process_cleanup') or {}).get('cleanup_complete') is True)
            except Exception as exc:
                record['exceptions'].append({'stage':'session','type':type(exc).__name__})
                # Host events remain metadata-only even when no launcher result could be returned.
                try:
                    record['events'] = [e for e in HostEventStore(db).list() if e['ingest_event_id'] not in before_ids]
                    record['usage'] = host.aggregate_usage(record['events'])
                except Exception as observed:
                    record['exceptions'].append({'stage':'exception_observations','type':type(observed).__name__})
            finally:
                # Record literal/source truth even when a host/gate exception interrupted reporting.
                try:
                    manual = record['manual_checks']
                    manual.update(source_sha256=digest_bytes(source_bytes),expected_sha256=digest_bytes(expected),
                        source_unchanged=source.is_file() and file_sha(source)==digest_bytes(source_bytes),
                        output_exists=output.is_file(),
                        literal_bytes_match=output.is_file() and output.read_bytes()==expected)
                    if output.is_file(): manual['output_sha256'] = file_sha(output)
                    if 'durable_runs' not in record:
                        run_ids = list(dict.fromkeys(e['run_id'] for e in record.get('events',[])
                            if e.get('kind')=='rpe_task_returned' and e.get('run_id')))
                        record['durable_runs'] = [durable_summary(read_run(db,run_id)) for run_id in run_ids]
                    record['real_rejections'] = [{key:event[key] for key in (
                        'receipt_id','run_id','error_code','error_type','task_attempt','tool_call_id','api_request_id')
                        if key in event} for event in record.get('events',[])
                        if event.get('kind')=='rpe_task_returned' and event.get('status')=='rejected']
                except Exception as exc:
                    record['exceptions'].append({'stage':'final_manual_evidence','type':type(exc).__name__})
                record['runner_process_cleanup'] = getattr(guard,'report',{'cleanup_complete':False,
                    'remaining_processes':[],'errors':[{'stage':'runner_guard_unavailable','type':'NotStarted'}]})
                cleanup = safe_remove_sessions(session_owner)
                record['session_root_removed'] = cleanup['session_roots_remaining']==0 and not cleanup['errors']
                record['exceptions'].extend(cleanup['errors'])
                record['passed'] = (record['passed'] and record['session_root_removed']
                    and record['runner_process_cleanup']['cleanup_complete'] and not record['exceptions'])
                save()
                print(json.dumps({'stage':'session_end','case':name,'passed':record['passed'],
                    'gate_errors':record.get('gate',{}).get('errors'),
                    'exception_types':[e['type'] for e in record['exceptions']]}),flush=True)
    except Exception as exc:
        report['exceptions'].append({'stage':'overall','type':type(exc).__name__})
    finally:
        for owner in sessions_root.iterdir():
            if owner.is_dir() and not owner.is_symlink():
                cleanup = safe_remove_sessions(owner)
                report['exceptions'].extend(cleanup['errors'])
        report['session_roots_remaining'] = len(list(sessions_root.glob('*/session-*')))
        report['summary'] = {s['case']:{'gate_verified':s.get('gate',{}).get('verified') is True,
            'literal_bytes_match':s['manual_checks'].get('literal_bytes_match') is True,
            'source_unchanged':s['manual_checks'].get('source_unchanged') is True,
            'coverage_complete':(s.get('usage') or {}).get('coverage_complete') is True,
            'passed':s['passed']} for s in report['sessions']}
        report['remaining_owned_processes'] = sum(len(s.get('runner_process_cleanup',{}).get('remaining_processes',[]))
            + len((s.get('process_cleanup') or {}).get('remaining_descendants',[]))
            + len((s.get('process_cleanup') or {}).get('external_executor_remaining_children',[]))
            for s in report['sessions'])
        report['process_cleanup_complete'] = len(report['sessions'])==2 and all(
            s.get('runner_process_cleanup',{}).get('cleanup_complete') is True and
            (s.get('process_cleanup') or {}).get('cleanup_complete') is True for s in report['sessions'])
        report['passed'] = (len(report['sessions'])==2 and all(s['passed'] for s in report['sessions'])
            and not report['exceptions'] and report['session_roots_remaining']==0
            and report['process_cleanup_complete'] and report['remaining_owned_processes']==0)
        save()
    print(json.dumps({'passed':report['passed'],'report':str(report_path),
        'process_cleanup_complete':report['process_cleanup_complete'],
        'remaining_owned_processes':report['remaining_owned_processes']}),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owned-root',type=Path,required=True)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--candidate-version-id',required=True)
    parser.add_argument('--hermes-root',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    raise SystemExit(main(parser.parse_args()))
