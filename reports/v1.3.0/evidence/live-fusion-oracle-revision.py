#!/usr/bin/env python3
"""Revise the handwritten XLSX oracle without rerunning or modifying host/product.

The input fixture explicitly put duplicate rows at 4 and 6 and retained row 5.
The registered XLSX contract clears duplicate rows; it preserves coordinates.
This script retains initial checker failure evidence and checks literal cells.
"""
import argparse
import hashlib
import json
from pathlib import Path

from openpyxl import load_workbook
from rpe_harness.integrations.hermes_gate import evaluate_session


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(root):
    root=root.resolve()
    evidence=root/'evidence'
    path=evidence/'live-fusion.json'
    initial_path=evidence/'live-fusion-initial.json'
    if not initial_path.exists():
        initial_path.write_bytes(path.read_bytes())
    report=json.loads(initial_path.read_text())
    session=next(s for s in report['sessions'] if s['format']=='xlsx')
    session['initial_oracle_passed']=session['passed']
    session['initial_manual_checks']=session['manual_checks']
    # Literal independent truth declared from fixture rows and the XLSX contract.
    expected_rows=[['订单号','地区','金额','合计'],['001','华东',10,'=C2*2'],
                   ['002','华南',20,'=C3*2'],[None,None,None,None],
                   ['003','华北',30,'=C5*2'],[None,None,None,None]]
    expected_cells={f'{column}{row}':value for row,values in enumerate(expected_rows,1)
                    for column,value in zip('ABCD',values)}
    source=root/'live-acceptance/xlsx/订单.xlsx'
    output=root/'live-acceptance/xlsx/去重结果.xlsx'
    book=load_workbook(output,data_only=False,keep_links=False)
    checks={
        'source_unchanged':sha(source)==session['initial_manual_checks']['source_sha256'],
        'output_hash_matches_persisted_artifact':sha(output)==session['durable_run']['artifact']['sha256'],
        'literal_cells_match':all(book['订单'][cell].value==value for cell,value in expected_cells.items()),
        'sheet_names_preserved':book.sheetnames==['订单','说明'],
        'other_sheet_literal_cells_match':all(book['说明'][cell].value==value for cell,value in {
            'A1':'项目','B1':'内容','C1':None,'A2':'保留','B2':'此工作表应原样保留','C2':'=1+2'}.items()),
        'retained_formula_coordinates_match':book['订单']['D2'].value=='=C2*2' and
            book['订单']['D3'].value=='=C3*2' and book['订单']['D5'].value=='=C5*2',
        'duplicate_row_4_cleared':all(book['订单'][f'{column}4'].value is None for column in 'ABCD'),
        'duplicate_row_6_cleared':all(book['订单'][f'{column}6'].value is None for column in 'ABCD'),
        'freeze_panes_preserved':book['订单'].freeze_panes=='A2',
        'other_sheet_number_format_preserved':book['说明']['B2'].number_format=='@',
        'builtin_used':session['durable_run']['artifact']['actual_executor']['kind']=='builtin'}
    book.close()
    session['oracle_revision']={'revision':2,'reason':'initial runner assumed compacted rows; registered XLSX contract clears duplicate cells and preserves row coordinates',
                               'expected_cells':expected_cells,'checks':checks,
                               'model_rerun':False,'product_or_wheel_changed':False}
    session['gate_recheck']=evaluate_session(session,root/'installed-acceptance-2/state/runs.sqlite',
        root/'live-acceptance/xlsx',required_request=session['required_request'],require_policy=True)
    session['passed']=all(checks.values()) and session['gate_recheck']['verified'] is True
    report['initial_report_passed']=report['passed']
    report['initial_report_sha256']=sha(initial_path)
    report['oracle_revision_runner_sha256']=sha(Path(__file__))
    report['model_sessions_rerun']=False
    report['model_call_attempts']={s['format']:[{
        'task_attempt':e.get('task_attempt'),'status':e.get('status'),'error_code':e.get('error_code'),
        'run_id':e.get('run_id')} for e in s['events'] if e['kind']=='rpe_task_returned']
        for s in report['sessions']}
    report['passed']=(all(s['passed'] for s in report['sessions']) and not report['exceptions']
                      and report['rollback']['rolled_back'] is True and report['process_cleanup_complete']
                      and report['session_roots_remaining']==0 and report['remaining_owned_processes']==0)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'initial_report_passed':report['initial_report_passed'],
                      'literal_cell_checks':checks,'gate_errors':session['gate_recheck']['errors']},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owned-root',type=Path,required=True)
    raise SystemExit(main(parser.parse_args().owned_root))
