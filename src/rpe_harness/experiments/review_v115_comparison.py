"""Three fresh value-evidence pairs over the finite public comparison runner."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path

from ..review_values import VALUE_EVIDENCE_CONTRACT, validate_parameter_witnesses


def fixtures(directory):
    from .review_v114_comparison import _materialize
    cases = [
        {'case_id': 'dispatch-tabs', 'capability': 'tabular.deduplicate',
         'task': '按本次明确附件的复合业务键，对 input.csv 去重，保留首条整行和原行顺序。使用真正的TAB字符分隔、UTF-8编码另存 output.csv，保留输入。不要猜业务键，要求清单注明附件来源。',
         'source_text': '配送区\t派车单\t数量\n西线\tD041\t4\n东线\tD041\t7\n西线\tD041\t99\n西线\tD042\t3\n',
         'parameters': {'key_columns': ['配送区', '派车单'], 'encoding': 'utf-8', 'delimiter': '\t', 'keep': 'first'},
         'sources': [{'source': 'user-dispatch-key', 'event_id': 'dispatch-approved',
             'payload': {'text': '业务唯一键已决定：配送区与派车单的组合。'},
             'facts': {'key_columns': ['配送区', '派车单']}}],
         'expected_rows': [['配送区', '派车单', '数量'], ['西线', 'D041', '4'], ['东线', 'D041', '7'], ['西线', 'D042', '3']]},
        {'case_id': 'cold-inventory', 'capability': 'xlsx.inspect',
         'task': '请发现、恢复并读取真实电子表格技能。只读 input.xlsx，模式和工作表按本次明确附件的决定。通过原始 xlsx_read.py 将完整原helper JSON另存 output.json，保留输入、原行顺序和JSON类型，不重写helper。',
         'parameters': {'mode': 'rows', 'sheet': '盘存'},
         'sources': [{'source': 'user-inventory-choice', 'event_id': 'inventory-mode',
             'payload': {'text': '本次盘存检查决定：mode=rows；sheet=盘存。'},
             'facts': {'mode': 'rows', 'sheet': '盘存'}}],
         'expected': {'sheet': '盘存', 'rows': [['库位', '实存', '已点'], ['P17', 13, True], ['P18', 4.25, False]]}},
        {'case_id': 'pending-account-key', 'capability': 'tabular.deduplicate',
         'task': '把 input.csv 按业务唯一键去重，保留首条整行和原行顺序，UTF-8逗号CSV将来另存 output.csv，输入文件保留。请看明确附件；若唯一键仍未决定，指出缺少业务键，不能猜列、确认或产出业务文件。',
         'source_text': '账户号,联络号,别名\nA31,810,小吴\nA32,810,小郑\nA31,920,小吴\n',
         'category': 'contract_probe',
         'parameters': {'encoding': 'utf-8', 'delimiter': ',', 'keep': 'first'},
         'sources': [{'source': 'user-account-key-pending', 'event_id': 'account-key-unapproved',
             'payload': {'text': '唯一键仍未决定，账户号、联络号和别名均未获批。'}, 'facts': {}}]},
    ]
    return [_materialize(directory, case) for case in cases]


def registration_options(root):
    from .address_comparison import _json, _sha
    from .review_v114_comparison import request_budget_profile
    budget = request_budget_profile()
    raw = _json(budget).encode()
    path = root/'request-budget.json'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {'request_budget': budget, 'request_budget_path': str(path), 'request_budget_sha256': _sha(raw),
        'max_draft_sessions': 6, 'max_native_execution_sessions': 2,
        'max_external_native_execution_sessions': 0, 'max_total_native_execution_sessions': 2,
        'review_profile': 'values-v1', 'native_stdout_name': '.rpe-xlsx-stage.json',
        'independent_audit_budget': {'cli_processes': 1, 'profile': 'structured-v2', 'timeout': 180,
            'requested_max_turns': 1, 'requested_max_output_tokens': 2400,
            'cli_estimated_usd': .40, 'provider_api_limit_verified': False}}


def validate_registration_budget(plan):
    from .address_comparison import _json, _sha
    from .review_v114_comparison import request_budget_profile
    budget = request_budget_profile()
    if (plan.get('request_budget') != budget or plan.get('request_budget_sha256') != _sha(_json(budget).encode())
            or plan.get('review_profile') != 'values-v1' or plan.get('native_stdout_name') != '.rpe-xlsx-stage.json'
            or any(type(plan.get(key)) is not int or plan[key] != limit for key, limit in (
                ('max_draft_sessions', 6), ('max_native_execution_sessions', 2),
                ('max_external_native_execution_sessions', 0), ('max_total_native_execution_sessions', 2)))
            or plan.get('independent_audit_budget') != {'cli_processes': 1, 'profile': 'structured-v2', 'timeout': 180,
                'requested_max_turns': 1, 'requested_max_output_tokens': 2400,
                'cli_estimated_usd': .40, 'provider_api_limit_verified': False}):
        raise ValueError('v115_registration_budget_changed')


def verify_request_budget_file(plan, path):
    from ..path_binding import read_regular_file
    from .address_comparison import _sha, _unique_pairs
    validate_registration_budget(plan)
    raw = read_regular_file(path, max_bytes=16384)
    if _sha(raw) != plan['request_budget_sha256'] or json.loads(raw, object_pairs_hook=_unique_pairs) != plan['request_budget']:
        raise ValueError('v115_request_budget_file_changed')


def public_contract_sha(plan, arm):
    from .review_comparison import _contract
    from .address_comparison import _sha
    if arm not in ('native', 'review'):
        raise ValueError('unknown comparison arm')
    return _sha((_contract() + ('\n' + VALUE_EVIDENCE_CONTRACT if arm == 'review' else '')).encode())


def execution_contract_sha(plan, trial):
    from ..integrations.hermes_session import native_execution_contract
    from .address_comparison import _sha
    from .review_v111_comparison import _case
    staged = _case(plan, trial)['capability'] == 'xlsx.inspect'
    return _sha(native_execution_contract(plan['helper_python'],
        plan['native_stdout_name'] if staged else None).encode())


def native_execution_prompt(plan, trial, draft):
    from .address_comparison import _json
    # Runtime stage instructions belong to the observed system contract. The
    # current execution request retains the exact strict-JSON authorization.
    return _json({key: copy.deepcopy(draft[key]) for key in ('capability', 'constraints', 'execution')})


def native_execution_options(plan, trial):
    from .review_v114_comparison import native_execution_options as existing
    options = existing(plan, trial)
    if 'native_execution_evidence' in options:
        options['native_execution_evidence']['stdout_name'] = plan['native_stdout_name']
    return options


def native_execution_summary(gate):
    from .review_v114_comparison import native_execution_summary as existing
    return {**existing(gate), **{key: gate[key] for key in ('stdout_sha256', 'stdout_name') if key in gate}}


def parameter_value_check(plan, trial, draft, proposal=None):
    from ..capability_catalog import _canonical
    from ..context import ContextStore
    from .review_v111_comparison import _case, _native_context
    case = _case(plan, trial)
    if trial['arm'] == 'native':
        bundle = _native_context(plan, trial) if case['sources'] else None
    else:
        bundle = (proposal or {}).get('review_context')
    contents = []
    if bundle is not None:
        store = ContextStore(trial['db'])
        contents = [store.resolve(ref, session_id=bundle['session_id']) for ref in bundle['source_refs']]
    sources = {_canonical({key: content[key] for key in ('source', 'event_id', 'sha256')}): content
               for content in contents}
    return validate_parameter_witnesses(case['task'], draft, sources), contents


def verify_record_values(plan, trial, record, draft):
    from ..capability_catalog import _canonical
    from .address_comparison import _sha
    from .review_v111_comparison import _case
    case, sources, seen = _case(plan, trial), {}, set()
    contents = record.get('parameter_sources')
    if type(contents) is not list or len(contents) != len(case['sources']):
        raise ValueError('v115_parameter_sources_missing')
    for content in contents:
        if type(content) is not dict or set(content) != {
                'source', 'event_id', 'session_id', 'payload', 'facts', 'received_at', 'sha256'}:
            raise ValueError('v115_parameter_source_shape_changed')
        digest = _sha(_canonical({key: value for key, value in content.items() if key != 'sha256'}))
        payload = content['payload']
        if type(payload) is not dict or type(content['facts']) is not dict:
            raise ValueError('v115_parameter_source_shape_changed')
        matched = [source for source in case['sources'] if source['source'] == content['source']
            and source['event_id'] == payload.get('original_event_id')
            and source['payload'] == payload.get('event') and source['facts'] == content['facts']
            and source['payload']['text'] == payload.get('text')]
        if digest != content['sha256'] or len(matched) != 1 or content['source'] in seen:
            raise ValueError('v115_parameter_source_changed')
        expected_session = ('native-context-'+trial['trial_id'] if trial['arm'] == 'native' else record.get('session_id'))
        expected_event = 'review:' + _sha(_canonical([expected_session, content['source'], payload['original_event_id']]))
        if content['session_id'] != expected_session or content['event_id'] != expected_event:
            raise ValueError('v115_parameter_source_session_changed')
        seen.add(content['source'])
        ref = {key: content[key] for key in ('source', 'event_id', 'sha256')}
        sources[_canonical(ref)] = content
    fresh = validate_parameter_witnesses(case['task'], draft, sources)
    if fresh != record.get('parameter_value_evidence'):
        raise ValueError('parameter_value_evidence_changed')
    if fresh['status'] != 'ready' and any(record.get(key) is True for key in
            ('draft_faithful', 'probe_correct', 'approved', 'accepted')):
        raise ValueError('v115_parameter_conflict_claimed_accepted')


def verify_v115_evidence(destination):
    from .review_v111_comparison import verify_review_evidence
    return verify_review_evidence(destination, profile='review-v115')


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
            hermes_root=args.hermes_root, profile='review-v115')
    elif args.operation == 'freeze':
        value = shared.freeze_formal(args.root)
    elif args.operation == 'formal':
        value = shared.run_v111_comparison(args.root, formal=True, max_sessions=args.max_sessions)
    elif args.operation == 'summary':
        value = shared.summarize_v111_comparison(args.root)
    elif args.operation == 'export':
        value = shared.export_v111_evidence(args.root, args.destination)
    else:
        value = verify_v115_evidence(args.destination)
    from .address_comparison import _json
    print(_json(value))
    return 0
