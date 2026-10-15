"""Fresh 1.12 fixtures and a thin entry point over the finite review runner."""
from __future__ import annotations

import argparse
import copy
from pathlib import Path


def fixtures(directory):
    cases = [
        {'case_id': 'comma-orders', 'capability': 'tabular.deduplicate',
         'task': '把 input.csv 按渠道和流水号的组合去重。001与1不同；保留首条和原行顺序，UTF-8逗号CSV另存 output.csv，保留原文件。',
         'source_text': '渠道,流水号,备注\n东,001,原单\n西,001,另单\n东,001,重复\n东,1,短号\n',
         'parameters': {'key_columns': ['渠道', '流水号'], 'encoding': 'utf-8', 'delimiter': ',', 'keep': 'first'},
         'expected_rows': [['渠道', '流水号', '备注'], ['东', '001', '原单'], ['西', '001', '另单'], ['东', '1', '短号']]},
        {'case_id': 'cold-stock', 'capability': 'xlsx.inspect',
         'task': '请发现、恢复并读取真实表格技能。只读 input.xlsx，检查模式与工作表使用本次附件的决定。调用原始 xlsx_read.py，按原helper的完整JSON结构另存 output.json；保留输入、行顺序和类型，不重写helper。',
         'parameters': {'mode': 'rows', 'sheet': '盘点'},
         'sources': [{'source': 'user-stock-decision', 'event_id': 'mode-and-sheet',
             'payload': {'text': '检查决定：mode=rows；sheet=盘点。'}, 'facts': {'mode': 'rows', 'sheet': '盘点'}}],
         'expected': {'sheet': '盘点', 'rows': [['货号', '余量', '可用'], ['P03', 6, True], ['Q04', 2.25, False]]}},
        {'case_id': 'source-tabs', 'capability': 'tabular.deduplicate',
         'task': '按明确附件中的复合业务键处理 input.csv，去重保留首条和原行顺序。用真正的TAB字符分隔、UTF-8编码另存 output.csv，保留原文件；业务键不要猜，草稿注明附件出处。',
         'source_text': '站点\t票号\t金额\n甲站\tT02\t3\n乙站\tT02\t8\n甲站\tT02\t90\n甲站\tT03\t4\n',
         'parameters': {'key_columns': ['站点', '票号'], 'encoding': 'utf-8', 'delimiter': '\t', 'keep': 'first'},
         'sources': [{'source': 'user-ticket-decision', 'event_id': 'composite-key',
             'payload': {'text': '唯一键已经决定：站点与票号的组合。'}, 'facts': {'key_columns': ['站点', '票号']}}],
         'expected_rows': [['站点', '票号', '金额'], ['甲站', 'T02', '3'], ['乙站', 'T02', '8'], ['甲站', 'T03', '4']]},
        {'case_id': 'missing-identity', 'capability': 'tabular.deduplicate',
         'task': '把 input.csv 按业务唯一键去重保留首条和原顺序，UTF-8逗号CSV将来另存 output.csv，保留原文件。请查看附件：如果仍未决定唯一键，必须指出缺少业务键，不能猜列、确认或产出业务文件。',
         'source_text': '编号,手机,姓名\n甲,300,小周\n乙,300,小吴\n甲,400,小周\n',
         'parameters': {'encoding': 'utf-8', 'delimiter': ',', 'keep': 'first'},
         'sources': [{'source': 'user-key-pending', 'event_id': 'not-decided',
             'payload': {'text': '唯一键未决定；编号、手机和姓名都没有获批。'}, 'facts': {}}]},
    ]
    from openpyxl import Workbook
    for case in cases:
        cold = case['capability'] == 'xlsx.inspect'
        probe = case['case_id'] == 'missing-identity'
        case.update(input_name='input.xlsx' if cold else 'input.csv',
            output_name='output.json' if cold else 'output.csv', use_skills=cold,
            sources=case.get('sources', []), constraints=['preserve_original'] if cold else ['preserve_original', 'preserve_order'],
            expected_status='needs_context' if probe else 'ready', category='contract_probe' if probe else 'production')
        source = directory / (case['case_id'] + Path(case['input_name']).suffix)
        if cold:
            book = Workbook()
            book.active.title = '不要读取'
            book.active.append(['错误工作表'])
            sheet = book.create_sheet('盘点')
            for row in case['expected']['rows']:
                sheet.append(row)
            book.save(source)
            book.close()
        else:
            source.write_bytes(case['source_text'].encode())
        from .address_comparison import _sha
        case.update(source_path=str(source), input_sha256=_sha(source.read_bytes()))
    return cases


def pilot_fixture(directory, cases):
    case = copy.deepcopy(cases[1])
    case.update(case_id='pilot-cold-context',
        task='发现并恢复真实电子表格技能，读取 input.xlsx。模式和工作表按明确附件恢复，草稿注明出处；通过原始 xlsx_read.py 将完整JSON写入 output.json，保留原文件。')
    target = directory / 'pilot-cold-context.xlsx'
    target.write_bytes(Path(case['source_path']).read_bytes())
    case['source_path'] = str(target)
    return case


def verify_v112_evidence(destination):
    from .review_v111_comparison import verify_review_evidence
    return verify_review_evidence(destination, profile='review-v112')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('register', 'freeze', 'pilot', 'formal', 'summary', 'export', 'verify'))
    parser.add_argument('--root', type=Path)
    parser.add_argument('--skill-root', type=Path)
    parser.add_argument('--hermes-root', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--max-sessions', type=int)
    args = parser.parse_args(argv)
    from . import review_v111_comparison as shared
    if args.operation == 'register':
        value = shared.preregister_v111_comparison(args.root, skill_root=args.skill_root,
            hermes_root=args.hermes_root, profile='review-v112')
    elif args.operation == 'freeze':
        value = shared.freeze_formal(args.root)
    elif args.operation in {'pilot', 'formal'}:
        value = shared.run_v111_comparison(args.root, formal=args.operation == 'formal', max_sessions=args.max_sessions)
    elif args.operation == 'summary':
        value = shared.summarize_v111_comparison(args.root)
    elif args.operation == 'export':
        value = shared.export_v111_evidence(args.root, args.destination)
    else:
        value = verify_v112_evidence(args.destination)
    from .address_comparison import _json
    print(_json(value))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
