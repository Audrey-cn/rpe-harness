"""Fresh, single-attempt 1.13 fixtures over the existing finite review runner."""
from __future__ import annotations

import argparse
from pathlib import Path


def fixtures(directory):
    cases = [
        {'case_id': 'receipt-comma', 'capability': 'tabular.deduplicate',
         'task': '把 input.csv 按库区和收据号的组合去重，0007与7是不同收据。保留首条整行与原行顺序，用UTF-8逗号CSV另存 output.csv，保留原文件。',
         'source_text': '库区,收据号,数量\n东库,0007,2\n西库,0007,5\n东库,0007,99\n东库,7,8\n',
         'parameters': {'key_columns': ['库区', '收据号'], 'encoding': 'utf-8', 'delimiter': ',', 'keep': 'first'},
         'expected_rows': [['库区', '收据号', '数量'], ['东库', '0007', '2'], ['西库', '0007', '5'], ['东库', '7', '8']]},
        {'case_id': 'cold-shipment', 'capability': 'xlsx.inspect',
         'task': '请发现、恢复并读取真实电子表格技能。只读 input.xlsx，模式和工作表按本次明确附件的决定。通过原始 xlsx_read.py 将完整原helper JSON另存 output.json，保留输入、原行顺序和类型，不重写helper。',
         'parameters': {'mode': 'rows', 'sheet': '发运'},
         'sources': [{'source': 'user-shipment-decision', 'event_id': 'inspection-choice',
             'payload': {'text': '本次检查决定：mode=rows；sheet=发运。'},
             'facts': {'mode': 'rows', 'sheet': '发运'}}],
         'expected': {'sheet': '发运', 'rows': [['箱号', '件数', '已装'], ['C08', 9, True], ['D09', 3.75, False]]}},
        {'case_id': 'returns-tabs', 'capability': 'tabular.deduplicate',
         'task': '按本次附件明确给出的复合业务键，对 input.csv 去重保留首条和原行顺序。使用真正的TAB字符分隔、UTF-8编码另存 output.csv，保留原文件。不要猜业务键，要求清单注明来源。',
         'source_text': '渠道\t退货单\t说明\n网店\tR08\t首条\n门店\tR08\t另一渠道\n网店\tR08\t重复\n网店\tR09\t新单\n',
         'parameters': {'key_columns': ['渠道', '退货单'], 'encoding': 'utf-8', 'delimiter': '\t', 'keep': 'first'},
         'sources': [{'source': 'user-returns-decision', 'event_id': 'approved-key',
             'payload': {'text': '去重业务唯一键已决定：渠道与退货单的组合。'},
             'facts': {'key_columns': ['渠道', '退货单']}}],
         'expected_rows': [['渠道', '退货单', '说明'], ['网店', 'R08', '首条'], ['门店', 'R08', '另一渠道'], ['网店', 'R09', '新单']]},
        {'case_id': 'undecided-staff-key', 'capability': 'tabular.deduplicate',
         'task': '把 input.csv 按业务唯一键去重，保留首条和原行顺序，UTF-8逗号CSV将来另存 output.csv，原文件保留。请看明确附件；若唯一键仍未决定，指出缺少业务键，不能猜列、确认或产出业务文件。',
         'source_text': '工号,电话,姓名\n02,700,小陈\n03,700,小何\n02,800,小陈\n',
         'parameters': {'encoding': 'utf-8', 'delimiter': ',', 'keep': 'first'},
         'sources': [{'source': 'user-staff-key-pending', 'event_id': 'not-approved',
             'payload': {'text': '唯一键仍未决定，工号、电话和姓名均未获批。'}, 'facts': {}}]},
    ]
    from openpyxl import Workbook
    from .address_comparison import _sha
    for case in cases:
        cold = case['capability'] == 'xlsx.inspect'
        probe = case['case_id'] == 'undecided-staff-key'
        case.update(input_name='input.xlsx' if cold else 'input.csv',
            output_name='output.json' if cold else 'output.csv', use_skills=cold,
            sources=case.get('sources', []), constraints=['preserve_original'] if cold else ['preserve_original', 'preserve_order'],
            expected_status='needs_context' if probe else 'ready', category='contract_probe' if probe else 'production')
        source = directory / (case['case_id'] + Path(case['input_name']).suffix)
        if cold:
            book = Workbook()
            book.active.title = '忽略此页'
            book.active.append(['不是目标数据'])
            sheet = book.create_sheet('发运')
            for row in case['expected']['rows']:
                sheet.append(row)
            book.save(source)
            book.close()
        else:
            source.write_bytes(case['source_text'].encode())
        case.update(source_path=str(source), input_sha256=_sha(source.read_bytes()))
    return cases


def native_execution_options(plan, trial):
    case = next(case for case in plan['cases'] if case['case_id'] == trial['case_id'])
    if case['capability'] != 'xlsx.inspect':
        return {}
    from ..skill_catalog import XLSX_SKILL_ID
    return {'native_execution_evidence': {
        'skill_id': XLSX_SKILL_ID, 'helper_python': plan['helper_python'],
        'helper_python_sha256': plan['environment']['helper_python_identity']['executable_sha256'],
        'skill_sha256': plan['skill_sha256'], 'script_sha256': plan['script_sha256'],
        'input_name': case['input_name'], 'input_sha256': trial['input_sha256'],
        'output_name': case['output_name'], 'parameters': dict(case['parameters'])}}


def native_execution_summary(gate):
    """Retain observed exit/proof scope, never the private authentication key/env."""
    return {key: gate[key] for key in (
        'receipt_authenticated', 'kernel_exit_observed', 'process_returncode',
        'artifact_binding', 'output_sha256', 'process_argv_sha256',
        'helper_call_id', 'arguments_sha256') if key in gate}


def verify_v113_evidence(destination):
    from .review_v111_comparison import verify_review_evidence
    return verify_review_evidence(destination, profile='review-v113')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('register', 'freeze', 'formal', 'summary', 'export', 'verify'))
    parser.add_argument('--root', type=Path)
    parser.add_argument('--skill-root', type=Path)
    parser.add_argument('--hermes-root', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--max-sessions', type=int)
    args = parser.parse_args(argv)
    from . import review_v111_comparison as shared
    if args.operation == 'register':
        value = shared.preregister_v111_comparison(args.root, skill_root=args.skill_root,
            hermes_root=args.hermes_root, profile='review-v113')
    elif args.operation == 'freeze':
        value = shared.freeze_formal(args.root)
    elif args.operation == 'formal':
        value = shared.run_v111_comparison(args.root, formal=True, max_sessions=args.max_sessions)
    elif args.operation == 'summary':
        value = shared.summarize_v111_comparison(args.root)
    elif args.operation == 'export':
        value = shared.export_v111_evidence(args.root, args.destination)
    else:
        value = verify_v113_evidence(args.destination)
    from .address_comparison import _json
    print(_json(value))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
