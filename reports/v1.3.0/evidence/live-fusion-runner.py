#!/usr/bin/env python3
"""Real installed-wheel Hermes fusion acceptance; no model text or credentials saved."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import rpe_harness
from openpyxl import Workbook, load_workbook
from rpe_harness.assets.lifecycle import propose_asset
from rpe_harness.evolution import validate_candidate, apply_candidate, rollback
from rpe_harness.integrations.hermes_session import run_hermes_session
from rpe_harness.integrations.hermes_gate import evaluate_session, read_run


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(root):
    root = root.resolve()
    assert '/site-packages/' in rpe_harness.__file__, rpe_harness.__file__
    seed_report = json.loads((root/'evidence/installed-fusion.json').read_text())
    asset = next(c for c in seed_report['checks'] if c['check']=='signed_package_lifecycle')
    state = root/'installed-acceptance-2/state'
    db = state/'runs.sqlite'
    workspace_root = root/'live-acceptance'
    workspace_root.mkdir(exist_ok=False)
    sessions_root = root/'live-sessions'
    sessions_root.mkdir(exist_ok=False, mode=0o700)
    report_path = root/'evidence/live-fusion.json'
    report = {'passed':False, 'version':rpe_harness.__version__, 'module_path':rpe_harness.__file__,
              'runner_sha256':sha(__file__), 'strict_mode':True, 'required_policy':True,
              'sessions':[], 'exceptions':[], 'active_asset':None,
              'global_changes_by_runner':False}
    active = None
    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    try:
        proposal = propose_asset(asset['asset_id'], db, keyring=root/'installed-acceptance-2/test-trust.json',
                                 evidence_run_ids=[asset['seed_run_id']])
        validation = validate_candidate(proposal['proposal_id'], db)
        assert validation['passed'] and len(validation['checks'])==13
        active = apply_candidate(proposal['proposal_id'], db)
        report['active_asset'] = {'asset_id':asset['asset_id'],'proposal_id':proposal['proposal_id'],
                                  'version_id':active['version_id'], 'content_sha256':asset['content_sha256'],
                                  'fixture_checks':len(validation['checks'])}
        save()
        print(json.dumps({'stage':'active_asset','version_id':active['version_id']}), flush=True)
        for format_name in ('csv','xlsx'):
            workspace = workspace_root/format_name
            workspace.mkdir()
            source = workspace/('订单.'+format_name)
            output = workspace/('去重结果.'+format_name)
            expected = [['订单号','地区','金额','备注'],['001','华东','10','保留首条'],
                        ['002','华南','20','正常'],['003','华北','30','末尾']]
            if format_name=='csv':
                with source.open('w',encoding='utf-8-sig',newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerows([expected[0],expected[1],expected[2],
                                      ['001','华东','99','重复弃用'],expected[3],['002','华南','77','重复弃用']])
            else:
                book=Workbook()
                sheet=book.active
                sheet.title='订单'
                expected=[['订单号','地区','金额','合计'],['001','华东',10,'=C2*2'],
                          ['002','华南',20,'=C3*2'],['003','华北',30,'=C4*2']]
                for row in [expected[0],expected[1],expected[2],['001','华东',99,'=C4*2'],
                            ['003','华北',30,'=C5*2'],['002','华南',77,'=C6*2']]:
                    sheet.append(row)
                sheet['A2'].number_format='@'
                sheet.freeze_panes='A2'
                other=book.create_sheet('说明')
                other.append(['项目','内容'])
                other.append(['保留','此工作表应原样保留'])
                other['C2']='=1+2'
                other['B2'].number_format='@'
                book.save(source)
                book.close()
            source_sha=sha(source)
            request={'schema_version':2,'event_id':'live-fusion-'+format_name,
                     'goal':'按订单号和地区去重并保留首条，保留原始文件及顺序',
                     'capability':'tabular.deduplicate',
                     'constraints':['preserve_original','preserve_order'],
                     'execution':{'input_path':str(source),'output_path':str(output),
                                  'key_columns':['订单号','地区'],'keep':'first'}}
            if format_name=='xlsx':
                request['execution']['sheet']='订单'
            (workspace/'request.json').write_text(json.dumps(request,ensure_ascii=False,indent=2)+'\n')
            prompt=(f'请处理当前工作区的文件 {source.name}，按“订单号”和“地区”两列的组合键去重，'
                    f'保留首次出现的记录和原有顺序，将结果另存为 {output.name}。'
                    '原始文件不可修改。请实际调用 rpe_task 完成这个任务，并根据工具返回说明是否成功。')
            if format_name=='xlsx':
                prompt+='处理“订单”工作表，保留公式及其他工作表。'
            print(json.dumps({'stage':'session_start','format':format_name}),flush=True)
            result=None
            record={'format':format_name,'required_request':request,'manual_checks':{},'passed':False}
            report['sessions'].append(record)
            try:
                result=run_hermes_session(prompt, workspace, db, tool_enabled=True,
                    owner_root=sessions_root, timeout=180,hermes_root='/Users/audrey/.hermes/hermes-agent',
                    entry_mode='strict',required_request=request,max_task_attempts=3)
                gate=evaluate_session(result,db,workspace,required_request=request,require_policy=True)
                record.update({key:result.get(key) for key in ('completed','exit_code','timed_out','session_id',
                    'usage','observer_errors','elapsed_seconds','model_identity','prompt_sha256',
                    'original_prompt_sha256','contract_prompt_injected','entry_mode','input_sha256','process_cleanup')})
                record['events']=result['events']
                record['gate']=gate
                run_id=gate.get('run_id') or next((e.get('run_id') for e in result['events']
                            if e['kind']=='rpe_task_returned' and e.get('run_id')),None)
                if run_id:
                    run=read_run(db,run_id)
                    record['durable_run']={'run_id':run_id,'status':run['status'],
                                          'verification':run['verification'],'artifact':run['artifact'],
                                          'step_stages':[s['stage'] for s in run['steps']]}
                manual=record['manual_checks']
                manual['source_sha256']=source_sha
                manual['source_unchanged']=sha(source)==source_sha
                manual['output_exists']=output.is_file()
                if output.is_file():
                    manual['output_sha256']=sha(output)
                    if format_name=='csv':
                        with output.open(encoding='utf-8-sig',newline='') as stream:
                            manual['literal_rows_match']=list(csv.reader(stream))==expected
                        actual=(record.get('durable_run',{}).get('artifact') or {}).get('actual_executor',{})
                        manual['active_asset_used']=actual.get('candidate_version_id')==active['version_id']
                        manual['candidate_code_sha256_match']=actual.get('code_sha256')==asset['content_sha256']
                    else:
                        book=load_workbook(output,data_only=False,keep_links=False)
                        manual['literal_rows_and_formulas_match']=list(book['订单'].values)==[tuple(r) for r in expected]
                        manual['sheet_names_preserved']=book.sheetnames==['订单','说明']
                        manual['other_sheet_cells_match']=list(book['说明'].values)==[
                            ('项目','内容',None),('保留','此工作表应原样保留','=1+2')]
                        manual['freeze_panes_preserved']=book['订单'].freeze_panes=='A2'
                        manual['other_sheet_number_format_preserved']=book['说明']['B2'].number_format=='@'
                        book.close()
                        actual=(record.get('durable_run',{}).get('artifact') or {}).get('actual_executor',{})
                        manual['builtin_used']=actual.get('kind')=='builtin'
                checks=[v for v in manual.values() if type(v) is bool]
                record['passed']=(gate.get('verified') is True and bool(checks) and all(checks)
                                  and result['usage'].get('coverage_complete') is True)
            except Exception as exc:
                record['exception_type']=type(exc).__name__
            finally:
                if result is not None:
                    shutil.rmtree(result['session_root'])
                    record['session_root_removed']=not Path(result['session_root']).exists()
                else:
                    # A launcher exception can occur after it creates an isolated credential home.
                    for path in sessions_root.glob('session-*'):
                        shutil.rmtree(path)
                    record['session_root_removed']=not any(sessions_root.glob('session-*'))
                save()
                print(json.dumps({'stage':'session_end','format':format_name,'passed':record['passed'],
                                  'gate_errors':record.get('gate',{}).get('errors'),
                                  'exception_type':record.get('exception_type')}),flush=True)
    except Exception as exc:
        report['exceptions'].append({'stage':'overall','type':type(exc).__name__})
    finally:
        if active:
            try:
                rolled=rollback(active['version_id'],db)
                report['rollback']=rolled
            except Exception as exc:
                report['exceptions'].append({'stage':'rollback','type':type(exc).__name__})
        for path in sessions_root.glob('session-*'):
            shutil.rmtree(path)
        report['session_roots_remaining']=len(list(sessions_root.glob('session-*')))
        report['process_cleanup_complete']=all(s.get('process_cleanup',{}).get('cleanup_complete') is True
                                              for s in report['sessions']) and len(report['sessions'])==2
        report['remaining_owned_processes']=sum(len(s.get('process_cleanup',{}).get('remaining_descendants',[]))+
            len(s.get('process_cleanup',{}).get('external_executor_remaining_children',[]))
            for s in report['sessions'])
        report['passed']=(len(report['sessions'])==2 and all(s['passed'] for s in report['sessions'])
                          and not report['exceptions'] and report.get('rollback',{}).get('rolled_back') is True
                          and report['process_cleanup_complete'] and report['session_roots_remaining']==0
                          and report['remaining_owned_processes']==0)
        save()
    print(json.dumps({'passed':report['passed'],'report':str(report_path),
                      'process_cleanup_complete':report['process_cleanup_complete'],
                      'remaining_owned_processes':report['remaining_owned_processes']}),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owned-root',type=Path,required=True)
    raise SystemExit(main(parser.parse_args().owned_root))
