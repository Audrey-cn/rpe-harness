"""Fresh 1.14 fixtures and bounded requests over the existing review runner."""
from __future__ import annotations

import argparse
import codecs
import json
import os
from pathlib import Path


def fixtures(directory):
    cases = [
        {'case_id': 'invoice-ledger', 'capability': 'tabular.deduplicate',
         'task': '把 input.csv 按货主和发票的组合去重，00042与42是不同发票。保留首条整行与原行顺序，使用UTF-8逗号CSV另存 output.csv，保留输入文件。',
         'source_text': '货主,发票,应收\n青禾,00042,12.5\n远山,00042,6\n青禾,00042,88\n青禾,42,9\n',
         'parameters': {'key_columns': ['货主', '发票'], 'encoding': 'utf-8', 'delimiter': ',', 'keep': 'first'},
         'expected_rows': [['货主', '发票', '应收'], ['青禾', '00042', '12.5'], ['远山', '00042', '6'], ['青禾', '42', '9']]},
        {'case_id': 'cold-quality', 'capability': 'xlsx.inspect',
         'task': '请发现、恢复并读取真实电子表格技能。只读 input.xlsx，检查模式和工作表按照本次明确附件的决定。通过原始 xlsx_read.py 将完整原helper JSON另存 output.json，保留输入、原行顺序和JSON类型，不重写helper。',
         'parameters': {'mode': 'rows', 'sheet': '质检'},
         'sources': [{'source': 'user-quality-inspection', 'event_id': 'quality-mode-choice',
             'payload': {'text': '本次质检表检查决定：mode=rows；sheet=质检。'},
             'facts': {'mode': 'rows', 'sheet': '质检'}}],
         'expected': {'sheet': '质检', 'rows': [['批号', '合格件', '已复核'], ['QC21', 11, True], ['QC22', 2.5, False]]}},
        {'case_id': 'depot-tabs', 'capability': 'tabular.deduplicate',
         'task': '按本次明确附件中的复合业务键，对 input.csv 去重，保留首条整行和原行顺序。使用真正的TAB字符分隔、UTF-8编码另存 output.csv，保留输入文件。不要猜业务键，要求清单注明附件来源。',
         'source_text': '仓点\t交接单\t备注\n南仓\tH31\t首批\n北仓\tH31\t异仓\n南仓\tH31\t重复\n南仓\tH32\t次批\n',
         'parameters': {'key_columns': ['仓点', '交接单'], 'encoding': 'utf-8', 'delimiter': '\t', 'keep': 'first'},
         'sources': [{'source': 'user-handover-decision', 'event_id': 'handover-key-approved',
             'payload': {'text': '去重业务唯一键已决定：仓点与交接单的组合。'},
             'facts': {'key_columns': ['仓点', '交接单']}}],
         'expected_rows': [['仓点', '交接单', '备注'], ['南仓', 'H31', '首批'], ['北仓', 'H31', '异仓'], ['南仓', 'H32', '次批']]},
        {'case_id': 'pending-member-key', 'capability': 'tabular.deduplicate',
         'task': '把 input.csv 按业务唯一键去重，保留首条整行和原行顺序，UTF-8逗号CSV将来另存 output.csv，输入文件保留。请看明确附件；若唯一键仍未决定，指出缺少业务键，不能猜列、确认或产出业务文件。',
         'source_text': '会员号,邮箱,称呼\nM21,a@example.invalid,小林\nM22,a@example.invalid,小叶\nM21,b@example.invalid,小林\n',
         'parameters': {'encoding': 'utf-8', 'delimiter': ',', 'keep': 'first'},
         'sources': [{'source': 'user-member-key-pending', 'event_id': 'member-key-unapproved',
             'payload': {'text': '唯一键仍未决定，会员号、邮箱和称呼均未获批。'}, 'facts': {}}]},
    ]
    for case in cases:
        _materialize(directory, case)
    return cases


def _materialize(directory, case):
    from .address_comparison import _sha
    cold = case['capability'] == 'xlsx.inspect'
    probe = case.get('category') == 'contract_probe' or case['case_id'] == 'pending-member-key'
    case.update(input_name='input.xlsx' if cold else 'input.csv',
        output_name='output.json' if cold else 'output.csv', use_skills=cold,
        sources=case.get('sources', []), constraints=['preserve_original'] if cold else ['preserve_original', 'preserve_order'],
        expected_status='needs_context' if probe else 'ready', category='contract_probe' if probe else 'production')
    source = directory/(case['case_id']+Path(case['input_name']).suffix)
    if cold:
        from openpyxl import Workbook
        book = Workbook()
        book.active.title = '跳过此页'
        book.active.append(['不是本次检查目标'])
        sheet = book.create_sheet(case['parameters']['sheet'])
        for row in case['expected']['rows']:
            sheet.append(row)
        book.save(source)
        book.close()
    else:
        source.write_bytes(case['source_text'].encode())
    case.update(source_path=str(source), input_sha256=_sha(source.read_bytes()))
    return case


def pilot_fixture(directory):
    return _materialize(directory, {'case_id': 'pilot-cold-recheck', 'capability': 'xlsx.inspect',
        'task': '发现并恢复真实电子表格技能，只读 input.xlsx。模式和工作表按明确附件的决定恢复，要求清单注明出处；通过原始 xlsx_read.py 将完整原helper JSON另存 output.json，保留输入、行顺序和类型，不重写helper。',
        'parameters': {'mode': 'rows', 'sheet': '复核'},
        'sources': [{'source': 'user-recheck-choice', 'event_id': 'pilot-recheck-mode',
            'payload': {'text': '本次复核检查决定：mode=rows；sheet=复核。'},
            'facts': {'mode': 'rows', 'sheet': '复核'}}],
        'expected': {'sheet': '复核', 'rows': [['序列', '温度', '已核'], ['PX61', 7, True], ['PX62', 1.25, False]]}})


def request_budget_profile():
    from ..integrations.hermes_request_budget import validate_request_budget_profile
    return validate_request_budget_profile({'schema_version': 'rpe.hermes-request-budget/v1',
        'provider': 'xiaomi', 'model': 'mimo-v2.6-pro', 'max_completion_tokens': 2400,
        'thinking_type': 'disabled', 'request_timeout_seconds': 90, 'stale_timeout_seconds': 90})


def registration_options(root):
    from .address_comparison import _json, _sha
    profile = request_budget_profile()
    raw = _json(profile).encode()
    path = root/'request-budget.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {'request_budget': profile, 'request_budget_path': str(path),
        'request_budget_sha256': _sha(raw), 'max_draft_sessions': 10,
        'max_native_execution_sessions': 4, 'max_external_native_execution_sessions': 1,
        'max_total_native_execution_sessions': 5,
        'independent_audit_budget': {'cli_processes': 2, 'probe_cli_processes': 1,
            'final_cli_processes': 1, 'profile': 'structured-v2', 'timeout': 180,
            'requested_max_turns': 4, 'requested_max_output_tokens': 2400,
            'cli_estimated_usd': .40, 'provider_api_limit_verified': False}}


def validate_registration_budget(plan):
    from .address_comparison import _json, _sha
    expected = request_budget_profile()
    if (plan.get('request_budget') != expected
            or plan.get('request_budget_sha256') != _sha(_json(expected).encode())
            or any(type(plan.get(key)) is not int or plan[key] != limit for key, limit in (
                ('max_draft_sessions', 10), ('max_native_execution_sessions', 4),
                ('max_external_native_execution_sessions', 1), ('max_total_native_execution_sessions', 5)))):
        raise ValueError('v114_request_budget_changed')
    budget = plan.get('independent_audit_budget')
    if budget != {'cli_processes': 2, 'probe_cli_processes': 1, 'final_cli_processes': 1,
            'profile': 'structured-v2', 'timeout': 180, 'requested_max_turns': 4,
            'requested_max_output_tokens': 2400, 'cli_estimated_usd': .40,
            'provider_api_limit_verified': False}:
        raise ValueError('v114_independent_audit_budget_changed')


def verify_request_budget_file(plan, path):
    from ..path_binding import read_regular_file
    from .address_comparison import _sha, _unique_pairs
    validate_registration_budget(plan)
    raw = read_regular_file(path, max_bytes=16384)
    if (_sha(raw) != plan['request_budget_sha256']
            or json.loads(raw, object_pairs_hook=_unique_pairs) != plan['request_budget']):
        raise ValueError('v114_request_budget_file_changed')


def equivalent_encoding(actual, expected):
    try:
        return isinstance(actual, str) and codecs.lookup(actual).name == codecs.lookup(expected).name
    except (LookupError, TypeError):
        return False


def native_runtime_note(*, cold_skill):
    note = ('\n\nNative runtime availability for this arm: '
        'rpe_propose and all rpe_* tools are unavailable; do not search for them. '
        'Follow the public contract fallback: write draft.json directly with native tools. ')
    if cold_skill:
        note += ('The supplied restore_command restores only the archived skill explicitly requested by original_request. '
            'Running it in the current HERMES_HOME, then calling skill_view, is permitted runtime preparation, '
            'not business-task execution or dependency installation. ')
    return note + ('This runtime note adds no business fields, business execution authority or approval; '
        'cite only original_request and its explicitly authorized source quotes as requirements.')


def native_execution_options(plan, trial):
    cases = plan['cases']+([plan['pilot_case']] if plan.get('pilot_case') else [])
    case = next(case for case in cases if case['case_id'] == trial['case_id'])
    if case['capability'] != 'xlsx.inspect':
        return {'native_evidence': {'artifact_name': case['output_name']}}
    from .review_v113_comparison import native_execution_options as xlsx_options
    return xlsx_options({**plan, 'cases': cases}, trial)


def native_execution_summary(gate):
    from .review_v113_comparison import native_execution_summary as existing_summary
    return {**existing_summary(gate), **{key: gate[key] for key in ('artifact_call_id',) if key in gate}}


def request_budget_observation(result, diagnostics):
    """Retain actual durable pre-request limits, without prompts, IDs or values from a plan."""
    events = result.get('events') or []
    requests = [row for row in events if type(row) is dict and row.get('kind') == 'pre_api_request']
    rows = []
    for event in requests[:256]:
        cap = event.get('request_max_completion_tokens')
        thinking = event.get('request_thinking_type')
        rows.append({'max_completion_tokens': cap if type(cap) is int and cap >= 0 else None,
            'thinking_type': 'disabled' if thinking == 'disabled' else 'other' if isinstance(thinking, str) else None})
    proof = {'api_requests': len(requests), 'requests': rows, 'truncated': len(requests) > 256,
        'durable_observations_match': diagnostics.get('durable_observations_match') is True}
    proof['conformant'] = bool(rows) and not proof['truncated'] and proof['durable_observations_match'] and all(
        row == {'max_completion_tokens': 2400, 'thinking_type': 'disabled'} for row in rows)
    return proof


def verify_budget_observation(proof):
    if (type(proof) is not dict or set(proof) != {'api_requests', 'requests', 'truncated',
            'durable_observations_match', 'conformant'} or type(proof['api_requests']) is not int
            or proof['api_requests'] < 0 or type(proof['requests']) is not list
            or any(type(proof[key]) is not bool for key in ('truncated', 'durable_observations_match', 'conformant'))
            or len(proof['requests']) != min(proof['api_requests'], 256)
            or proof['truncated'] != (proof['api_requests'] > 256)):
        raise ValueError('v114_request_budget_observation_invalid')
    for row in proof['requests']:
        if (type(row) is not dict or set(row) != {'max_completion_tokens', 'thinking_type'}
                or not (row['max_completion_tokens'] is None or
                    type(row['max_completion_tokens']) is int and row['max_completion_tokens'] >= 0)
                or row['thinking_type'] not in (None, 'disabled', 'other')):
            raise ValueError('v114_request_budget_observation_invalid')
    conformant = bool(proof['requests']) and not proof['truncated'] and proof['durable_observations_match'] and all(
        row == {'max_completion_tokens': 2400, 'thinking_type': 'disabled'} for row in proof['requests'])
    if proof['conformant'] != conformant:
        raise ValueError('v114_request_budget_observation_changed')
    return conformant


def verify_record_budgets(record):
    if record.get('status') == 'incomplete':
        return
    observations = record['request_budget_observations']
    draft_ok = verify_budget_observation(observations['draft'])
    if record.get('comparable') is True and not draft_ok:
        raise ValueError('v114_draft_request_budget_unproven')
    if 'execute' in observations:
        execution_ok = verify_budget_observation(observations['execute'])
        if record.get('execution_verified') is True and not execution_ok:
            raise ValueError('v114_execution_request_budget_unproven')
    elif record.get('execution_kind') == 'approved_native' and record.get('execution_verified') is True:
        raise ValueError('v114_execution_request_budget_missing')


def verify_v114_evidence(destination):
    from .review_v111_comparison import verify_review_evidence
    return verify_review_evidence(destination, profile='review-v114')


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
            hermes_root=args.hermes_root, profile='review-v114')
    elif args.operation == 'freeze':
        value = shared.freeze_formal(args.root)
    elif args.operation in {'pilot', 'formal'}:
        value = shared.run_v111_comparison(args.root, formal=args.operation == 'formal', max_sessions=args.max_sessions)
    elif args.operation == 'summary':
        value = shared.summarize_v111_comparison(args.root)
    elif args.operation == 'export':
        value = shared.export_v111_evidence(args.root, args.destination)
    else:
        value = verify_v114_evidence(args.destination)
    from .address_comparison import _json
    print(_json(value))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
