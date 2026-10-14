"""Frozen 24-session XLSX skill comparison; importing never starts a model."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time
import hashlib
import json
from pathlib import Path
import shutil
import shlex
import sqlite3
import subprocess
import sys

from ..skill_catalog import SkillCatalog, XLSX_SKILL_ID


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)


def _source_hashes():
    package = Path(__file__).resolve().parents[1]
    return {path.relative_to(package).as_posix(): _sha(path.read_bytes())
            for path in sorted(package.rglob('*.py'))}


def _write(path, value):
    temporary = path.with_suffix(path.suffix + '.pending')
    temporary.write_text(_json(value), encoding='utf-8')
    temporary.replace(path)


def exact_json(actual, expected):
    """JSON equality includes scalar types, complete key sets, and sequence order."""
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return set(actual) == set(expected) and all(exact_json(actual[k], v) for k, v in expected.items())
    if type(expected) is list:
        return len(actual) == len(expected) and all(exact_json(a, b) for a, b in zip(actual, expected))
    return actual == expected


def oracle(path, mode, sheet=None):
    """Independent openpyxl truth; does not invoke the helper or Harness verifier."""
    from openpyxl import load_workbook
    book = load_workbook(path, data_only=False, keep_links=False)
    cached = None
    def scalar(value):
        return value.isoformat() if isinstance(value, (date, datetime, time)) else value
    try:
        if mode == 'rows':
            selected = book[sheet] if sheet else book.active
            return {'sheet': selected.title, 'rows': [list(map(scalar, row))
                    for row in selected.iter_rows(values_only=True)]}
        if mode == 'sheets':
            return {'sheets': [{'name': ws.title, 'dimensions': ws.dimensions,
                'max_row': ws.max_row, 'max_col': ws.max_column,
                'merged': [str(value) for value in ws.merged_cells.ranges],
                'charts': len(ws._charts), 'freeze_panes': ws.freeze_panes,
                'autofilter': ws.auto_filter.ref,
                'tables': {value.displayName: value.ref for value in ws.tables.values()},
                'protected': bool(ws.protection.sheet)} for ws in book.worksheets],
                'defined_names': {name: value.attr_text for name, value in book.defined_names.items()}}
        if mode != 'formulas':
            raise ValueError('unsupported oracle mode')
        cached = load_workbook(path, data_only=True, keep_links=False)
        rows = []
        for name in [sheet] if sheet else book.sheetnames:
            for row in book[name].iter_rows():
                for cell in row:
                    if cell.data_type == 'f':
                        rows.append({'sheet': name, 'cell': cell.coordinate,
                            'formula': cell.value, 'cached': scalar(cached[name][cell.coordinate].value)})
        return {'formulas': rows}
    finally:
        book.close()
        if cached is not None:
            cached.close()


def _cases(root):
    from openpyxl import Workbook
    from openpyxl.workbook.defined_name import DefinedName
    specs = [('rows_mixed', 'rows', None), ('rows_second', 'rows', 'Second'),
             ('rows_empty', 'rows', None), ('sheets_names', 'sheets', None),
             ('formulas_all', 'formulas', None), ('formulas_second', 'formulas', 'Second')]
    cases = []
    for name, mode, sheet in specs:
        book = Workbook()
        ws = book.active
        ws.title = 'Empty' if name == 'rows_empty' else 'Main'
        if name == 'rows_mixed':
            ws.append(['code', 'number', 'boolean', 'null', 'date'])
            ws.append(['001', 7, True, None, datetime(2026, 1, 2)])
            ws.append(['中文', 2.5, False, None, None])
        elif name != 'rows_empty':
            ws['A1'], ws['B1'], ws['A2'] = 3, '=A1*2', '=SUM(A1,B1)'
            other = book.create_sheet('Second')
            other['A1'], other['B1'] = '=Main!A1+1', '第二表'
        if name == 'sheets_names':
            book.defined_names.add(DefinedName('Total', attr_text="'Main'!$A$1"))
            ws.freeze_panes, ws.auto_filter.ref = 'B2', 'A1:B2'
        source = root / (name + '.xlsx')
        book.save(source)
        book.close()
        cases.append({'case_id': name, 'mode': mode, 'sheet': sheet,
            'temperature': 'cold' if name == 'rows_second' else 'warm',
            'source_path': str(source), 'input_sha256': _sha(source.read_bytes()),
            'expected': oracle(source, mode, sheet)})
    return cases


def _prompt(case, helper_python):
    return ('使用当前已安装的 xlsx 技能，先通过 skill_view 加载原技能说明，读取该说明并遵循它。'
        '原生辅助脚本路径只能取自 skill_view 返回的原技能目录。读取当前目录 input.xlsx，'
        '将原始 xlsx_read.py 对应模式输出完整写到新的 output.json，禁止修改原文件或覆盖输出。'
        f"固定模式 mode={case['mode']}，sheet={_json(case['sheet'])}。"
        'rows 使用 --json（默认 active 或指定 sheet），完整对象 {sheet,rows}；'
        'sheets 使用 --sheets，完整对象 {sheets,defined_names}；'
        'formulas 使用 --formulas，完整对象 {formulas}，默认全部表或指定表。'
        '严格保留 JSON 类型、null、日期 ISO 字符串、行与单元格顺序和所有字段。'
        '有专门任务工具时按其 discovery/load/context/task 流程执行；否则使用原生 skills/file/terminal。'
        f'Python 必须使用同一个已有 openpyxl 的解释器 {_json(helper_python)}；无需安装依赖。'
        '不得读取 Harness 实现、数据库、实验报告、oracle 或答案。最终仅报告状态和 output.json。')


def preregister(*, db, owned_root, catalog_root, hermes_root, report_dir,
                snapshot_id=None, timeout=180, helper_python=None):
    if not 1 <= timeout <= 180:
        raise ValueError('timeout must be between 1 and 180')
    paths = {key: str(Path(value).expanduser().resolve()) for key, value in
        {'db': db, 'owned_root': owned_root, 'catalog_root': catalog_root,
         'hermes_root': hermes_root, 'report_dir': report_dir}.items()}
    owned, reports = Path(paths['owned_root']), Path(paths['report_dir'])
    owned.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    catalog = SkillCatalog(paths['db'], paths['catalog_root'])
    row = catalog.lookup(XLSX_SKILL_ID, snapshot_id=snapshot_id)
    if row['adapter_status'] != 'trusted' or not row['executable']:
        raise ValueError('comparison requires the registered trusted original XLSX skill')
    # Keep the venv path, because resolving its symlink would discard venv identity.
    helper_python = str(Path(helper_python or sys.executable).expanduser().absolute())
    version = subprocess.run([helper_python, '-B', '-c',
        'import json,sys,openpyxl; print(json.dumps({"python":sys.version,"openpyxl":openpyxl.__version__}))'],
        capture_output=True, text=True, timeout=15, check=True)
    config = Path.home() / '.hermes/config.yaml'
    frozen = {**paths, 'snapshot_id': row['snapshot_id'], 'timeout': timeout,
        'skill_sha256': row['skill_sha256'], 'script_sha256': row['script_sha256'],
        'driver_sha256': _sha(Path(__file__).read_bytes()),
        'source_module_sha256': _source_hashes(),
        'helper_python': helper_python, 'helper_python_version': json.loads(version.stdout),
        'host_config_sha256': _sha(config.read_bytes()) if config.is_file() else None,
        'model_policy': 'same frozen installed Hermes configuration; record actual response identities',
        'cold_policy': 'one case is catalog-cold, never disabled; native has no temperature visibility',
        'tool_policy': 'native optional terminal/file/skills versus RPE mandatory single-stage skill workflow',
        'pilot_policy': 'pilot records remain in separate roots; formal sessions are never replaced',
        'cost_policy': 'provider-observed tokens are not billed cost; absent cost/usage remains null',
        'failure_policy': 'all attempts retained; resume skips existing trial records; no replacement',
        'oracle_policy': 'independent openpyxl; exact complete JSON shape/types/null/dates/order'}
    frozen['comparison_scope'] = '24 supported XLSX sessions; directed probes reported separately'
    frozen['directed_probes'] = [
        {'probe_id': 'missing-sheet-refusal', 'capability': 'xlsx.inspect',
         'expected': 'selected nonexistent sheet is refused and no output is published',
         'status': 'requires_separate_live_evidence'},
        {'probe_id': 'required-source-context', 'capability': 'tabular.deduplicate',
         'expected': 'missing key_columns restored only from session-bound captured source facts; '
                     'native receives identical source text and explicit source facts; '
                     'native uses CSV contract rather than claiming original XLSX-helper execution',
         'status': 'requires_separate_live_evidence'}]
    registration = reports / 'preregistration.json'
    if registration.exists():
        existing = json.loads(registration.read_text())
        if any(existing.get(key) != value for key, value in frozen.items()):
            raise ValueError('frozen preregistration differs; use a new experiment root')
        for case in existing['cases']:
            if _sha(Path(case['source_path']).read_bytes()) != case['input_sha256']:
                raise ValueError('frozen input changed')
        return existing
    fixture_root = owned / 'fixtures'
    fixture_root.mkdir()
    cases = _cases(fixture_root)
    sessions = []
    pair_index = 0
    for repeat in range(2):
        for case in cases:
            prompt = _prompt(case, helper_python)
            arms = ('native', 'rpe') if pair_index % 2 == 0 else ('rpe', 'native')
            pair_index += 1
            for arm in arms:
                trial_id = f"{case['case_id']}-r{repeat + 1}-{arm}"
                workspace = owned / 'workspaces' / trial_id
                workspace.mkdir(parents=True)
                shutil.copyfile(case['source_path'], workspace / 'input.xlsx')
                (workspace / 'prompt.txt').write_text(prompt, encoding='utf-8')
                sessions.append({'trial_id': trial_id, 'case_id': case['case_id'],
                    'repeat': repeat + 1, 'arm': arm, 'workspace': str(workspace),
                    'prompt_sha256': _sha(prompt.encode()), 'input_sha256': case['input_sha256']})
    plan = {**frozen, 'cases': cases, 'sessions': sessions}
    plan['fingerprint'] = _sha(_json(plan).encode())
    _write(registration, plan)
    (owned / 'records').mkdir()
    return plan


def _summary(plan, records):
    identities = {json.dumps(row['model_identity'], sort_keys=True) for row in records
                  if row['model_identity'] is not None}
    return {'fingerprint': plan['fingerprint'], 'snapshot_id': plan['snapshot_id'],
        'skill_sha256': plan['skill_sha256'], 'script_sha256': plan['script_sha256'],
        'planned_sessions': 24, 'completed_sessions': len(records),
        'accepted_sessions': sum(row['accepted'] for row in records),
        'source_module_sha256': plan['source_module_sha256'],
        'model_identities_consistent': len(identities) <= 1,
        'model_identity_coverage': sum(row['model_identity'] is not None for row in records),
        'tool_policy': plan['tool_policy'], 'pilot_policy': plan['pilot_policy'],
        'helper_python': plan['helper_python'], 'helper_python_version': plan['helper_python_version'],
        'comparison_scope': plan['comparison_scope'], 'directed_probes': plan['directed_probes'],
        'cold_policy': plan['cold_policy'], 'cost_policy': plan['cost_policy'], 'records': records}


def _native_gate(result, plan, trial, case):
    """Native load and direct original-helper invocation must have durable call links."""
    from ..integrations.hermes_gate import _persisted_events, _same_call, digest_json
    verdict = {'verified': False, 'errors': []}
    try:
        events = _persisted_events(plan['db'], result.get('session_id'))
        if not events or events != result.get('events'):
            raise ValueError('native_durable_observations_mismatch')
        identity_fields = ('session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id')
        pres = [event for event in events if event['kind'] == 'pre_tool_call']
        posts = [event for event in events if event['kind'] == 'post_tool_call']
        def call_identity(event):
            if any(not isinstance(event.get(key), str) or not event[key] for key in identity_fields):
                raise ValueError('native_real_call_identity_missing')
            return tuple(event[key] for key in identity_fields) + (event['tool_name'],)
        expected_calls = Counter(call_identity(event) for event in pres)
        if (expected_calls != Counter(call_identity(event) for event in posts) or
                any(count != 1 for count in expected_calls.values())):
            raise ValueError('native_real_pre_post_identity_mismatch')
        def call_with_arguments(event):
            arguments = event.get('arguments_sha256')
            if (not isinstance(arguments, str) or len(arguments) != 64 or
                    any(char not in '0123456789abcdef' for char in arguments)):
                raise ValueError('native_arguments_digest_missing')
            return call_identity(event) + (arguments,)
        if Counter(map(call_with_arguments, pres)) != Counter(map(call_with_arguments, posts)):
            raise ValueError('native_real_pre_post_arguments_mismatch')
        api_pres = Counter(tuple(event.get(key) for key in identity_fields[:-1]) for event in events
                           if event['kind'] == 'pre_api_request')
        api_outcomes = Counter(tuple(event.get(key) for key in identity_fields[:-1]) for event in events
                               if event['kind'] in {'post_api_request', 'api_request_error'})
        if (not api_pres or api_pres != api_outcomes or
                any(count != 1 or any(not key for key in identity) for identity, count in api_pres.items())):
            raise ValueError('native_actual_api_observations_incomplete')
        emitted = []
        for outcome in (event for event in events if event['kind'] == 'post_api_request'):
            api_key = tuple(outcome.get(key) for key in identity_fields[:-1])
            requests = [event for event in events if event['kind'] == 'pre_api_request' and
                        tuple(event.get(key) for key in identity_fields[:-1]) == api_key]
            if len(requests) != 1 or any(not key for key in api_key):
                raise ValueError('native_actual_api_request_link_missing')
            emitted.extend(api_key + (call.get('tool_call_id'), call.get('tool_name'))
                           for call in outcome.get('model_tool_calls', []))
        if expected_calls != Counter(emitted):
            raise ValueError('native_actual_model_call_identity_mismatch')
        streams = result.get('stream_tool_calls') or []
        if any(type(call) is not dict or type(call.get('input')) is not dict or
               not isinstance(call.get('name'), str) for call in streams):
            raise ValueError('native_stream_call_shape_invalid')
        classes = Counter((event['tool_name'], event['arguments_sha256']) for event in pres)
        streamed = Counter((call['name'], digest_json(call['input'])) for call in streams)
        if classes != streamed:
            raise ValueError('native_stream_arguments_multiset_mismatch')
        stream_id_fields = ['tool_call_id' in call for call in streams]
        identified_stream = any(stream_id_fields)
        if identified_stream:
            if not all(stream_id_fields) or any(not isinstance(call['tool_call_id'], str) or
                                               not call['tool_call_id'] for call in streams):
                raise ValueError('native_stream_call_ids_partial_or_invalid')
            actual_ids = Counter((call['name'], call['tool_call_id'], digest_json(call['input'])) for call in streams)
            expected_ids = Counter((event['tool_name'], event['tool_call_id'], event['arguments_sha256']) for event in pres)
            if actual_ids != expected_ids or any(count != 1 for count in expected_ids.values()):
                raise ValueError('native_stream_call_identity_mismatch')
        verdict['stream_identity_policy'] = ('complete_actual_call_ids' if identified_stream else
            'counted_name_argument_equivalence_classes_without_stream_ids')
        root = Path(result['session_root']).resolve()
        if not root.is_relative_to(Path(plan['owned_root']).resolve()):
            raise ValueError('native_session_root_not_owned')
        source = root / 'home/skills/productivity/xlsx/SKILL.md'
        helper = source.parent / 'scripts/xlsx_read.py'
        if (source.is_symlink() or helper.is_symlink() or
                _sha(source.read_bytes()) != plan['skill_sha256'] or
                _sha(helper.read_bytes()) != plan['script_sha256']):
            raise ValueError('native_materialized_source_changed')
        catalog = SkillCatalog(plan['db'], plan['catalog_root'])
        row = catalog.lookup(XLSX_SKILL_ID, snapshot_id=plan['snapshot_id'])
        if row['adapter_status'] != 'trusted':
            raise ValueError('native_original_source_changed')

        def paired(pre):
            posts = [event for event in events if event['kind'] == 'post_tool_call' and
                     event.get('tool_name') == pre['tool_name'] and _same_call(event, pre)]
            if len(posts) != 1 or posts[0].get('arguments_sha256') != pre.get('arguments_sha256'):
                raise ValueError('native_post_call_link_missing')
            # ID-less SDK records are equivalent arguments, never invented call IDs.
            calls = [call for call in streams if call.get('name') == pre['tool_name'] and
                     digest_json(call.get('input')) == pre.get('arguments_sha256') and
                     (not identified_stream or call['tool_call_id'] == pre['tool_call_id'])]
            if not calls:
                raise ValueError('native_stream_arguments_link_missing')
            return posts[0], calls[0]['input']

        loaded = []
        for pre in (e for e in events if e['kind'] == 'pre_tool_call' and e.get('tool_name') == 'skill_view'):
            post, args = paired(pre)
            if (args == {'name': row['load_name']} and post.get('status') == 'loaded' and
                    post.get('skill_id') == XLSX_SKILL_ID and
                    post.get('snapshot_id') == plan['snapshot_id'] and
                    post.get('content_sha256') == plan['skill_sha256'] and
                    post.get('skill_source_path') == str(source)):
                loaded.append(post)
        if not loaded:
            raise ValueError('native_matching_original_skill_load_missing')
        workspace = Path(trial['workspace']).resolve()
        flag = {'rows': '--json', 'sheets': '--sheets', 'formulas': '--formulas'}[case['mode']]
        for pre in (e for e in events if e['kind'] == 'pre_tool_call' and e.get('tool_name') == 'terminal'):
            post, args = paired(pre)
            if post.get('status') != 'succeeded' or not any(events.index(load) < events.index(pre) for load in loaded):
                continue
            command = args.get('command')
            if not isinstance(command, str) or any(char in command for char in ('$', '`', '\n')):
                continue
            lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|><')
            lexer.whitespace_split, lexer.commenters = True, ''
            tokens = list(lexer)
            if tokens[:1] == ['cd']:
                if len(tokens) < 4 or Path(tokens[1]).resolve() != workspace or tokens[2] != '&&':
                    continue
                tokens = tokens[3:]
            if tokens[:2] == [plan['helper_python'], '-B']:
                tokens.pop(1)
            if tokens[:2] != [plan['helper_python'], str(helper)] or len(tokens) < 6:
                continue
            inputs = {'input.xlsx', str(workspace / 'input.xlsx')}
            outputs = {'output.json', str(workspace / 'output.json')}
            if tokens[2] not in inputs or tokens[-2] != '>' or tokens[-1] not in outputs:
                continue
            expected_flags = [flag] + (['--sheet', case['sheet']] if case['sheet'] is not None else [])
            if tokens[3:-2] != expected_flags:
                continue
            verdict.update(verified=True, skill_id=XLSX_SKILL_ID,
                snapshot_id=plan['snapshot_id'], load_call_id=loaded[-1]['tool_call_id'],
                helper_call_id=pre['tool_call_id'], script_sha256=plan['script_sha256'])
            return verdict
        raise ValueError('native_original_helper_execution_missing')
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        verdict['errors'].append(str(error) if isinstance(error, ValueError) else type(error).__name__)
    return verdict


def run_comparison(plan, *, max_sessions=24, runner=None):
    if type(max_sessions) is not int or not 0 <= max_sessions <= 24:
        raise ValueError('max_sessions must be between 0 and 24')
    from ..integrations.hermes_session import run_hermes_session
    from ..integrations.skill_gate import evaluate_skill_session
    runner = runner or run_hermes_session
    frozen_body = {key: value for key, value in plan.items() if key != 'fingerprint'}
    if _sha(_json(frozen_body).encode()) != plan['fingerprint']:
        raise ValueError('frozen plan fingerprint differs')
    if not 1 <= plan['timeout'] <= 180 or _source_hashes() != plan['source_module_sha256']:
        raise ValueError('frozen source or timeout differs; preregister a new experiment')
    owned = Path(plan['owned_root'])
    records, launched = [], 0
    cases = {case['case_id']: case for case in plan['cases']}
    catalog = SkillCatalog(plan['db'], plan['catalog_root'])
    for trial in plan['sessions']:
        record_path = owned / 'records' / (trial['trial_id'] + '.json')
        if record_path.exists():
            saved = json.loads(record_path.read_text())
            if saved['fingerprint'] != plan['fingerprint']:
                raise ValueError('saved trial fingerprint differs')
            records.append(saved)
            continue
        if launched >= max_sessions:
            continue
        config = Path.home() / '.hermes/config.yaml'
        config_sha = _sha(config.read_bytes()) if config.is_file() else None
        if config_sha != plan['host_config_sha256']:
            raise ValueError('frozen model configuration changed')
        workspace = Path(trial['workspace'])
        case = cases[trial['case_id']]
        prompt = (workspace / 'prompt.txt').read_text()
        if _sha(prompt.encode()) != trial['prompt_sha256'] or _sha((workspace / 'input.xlsx').read_bytes()) != trial['input_sha256']:
            raise ValueError('frozen trial input or prompt changed')
        catalog.set_temperature(XLSX_SKILL_ID, case['temperature'])
        record = {**trial, 'fingerprint': plan['fingerprint'], 'temperature': case['temperature'],
            'snapshot_id': plan['snapshot_id'], 'status': 'interrupted', 'accepted': False,
            'artifact_exact': False, 'source_unchanged': False, 'session_id': None,
            'tool_names': [], 'elapsed_seconds': None, 'usage': {'total_tokens': None},
            'billed_cost': None, 'model_identity': None, 'gate': None}
        # Reserve BEFORE model invocation; a crash is retained rather than replayed.
        _write(record_path, record)
        launched += 1
        try:
            result = runner(prompt, workspace, plan['db'], tool_enabled=trial['arm'] == 'rpe',
                owner_root=owned / 'sessions', timeout=plan['timeout'],
                hermes_root=plan['hermes_root'], entry_mode='skills' if trial['arm'] == 'rpe' else 'optional',
                catalog_root=plan['catalog_root'], skill_snapshot_id=plan['snapshot_id'],
                context_sources=[], read_isolation_root=owned,
                deny_read_roots=[Path(plan['report_dir']), owned / 'records', owned / 'fixtures'])
            _write(owned / 'records' / (trial['trial_id'] + '.raw.json'), result)
            events = result.get('events', [])
            tools = [e.get('tool_name') for e in events if e.get('kind') == 'post_tool_call']
            # Retain measured usage and identity even when artifact/gate validation fails.
            record.update(session_id=result.get('session_id'),
                tool_names=[name for name in tools if name], elapsed_seconds=result.get('elapsed_seconds'),
                usage=result.get('usage') or {'total_tokens': None}, model_identity=result.get('model_identity'),
                billed_cost=result.get('billed_cost'), actual_prompt_sha256=result.get('prompt_sha256'),
                catalog_snapshot_id=result.get('catalog_snapshot_id'))
            output = workspace / 'output.json'
            exact = output.is_file() and not output.is_symlink() and exact_json(json.loads(output.read_text()), case['expected'])
            unchanged = _sha((workspace / 'input.xlsx').read_bytes()) == trial['input_sha256']
            host_ok = (result.get('completed') is True and result.get('exit_code') == 0 and
                not result.get('timed_out') and not result.get('observer_errors') and
                (result.get('process_cleanup') or {}).get('cleanup_complete') is True)
            gate = (evaluate_skill_session(result, plan['db'], workspace, catalog_root=plan['catalog_root'])
                    if trial['arm'] == 'rpe' else _native_gate(result, plan, trial, case))
            comparable = (result.get('prompt_sha256') == trial['prompt_sha256'] and
                result.get('catalog_snapshot_id') == plan['snapshot_id'] and
                result.get('model_identity') is not None and
                all(previous['model_identity'] in (None, result['model_identity']) for previous in records))
            record.update(status='completed', session_id=result.get('session_id'),
                accepted=bool(host_ok and exact and unchanged and comparable and gate.get('verified')),
                comparable=comparable,
                artifact_exact=bool(exact), source_unchanged=unchanged,
                tool_names=[name for name in tools if name], elapsed_seconds=result.get('elapsed_seconds'),
                usage=result.get('usage') or {'total_tokens': None}, model_identity=result.get('model_identity'),
                billed_cost=result.get('billed_cost'), gate=gate,
                actual_prompt_sha256=result.get('prompt_sha256'),
                output_sha256=_sha(output.read_bytes()) if output.is_file() else None)
        except Exception as error:
            # Exception messages may contain provider credentials; persist type only.
            record.update(status='failed', error_type=type(error).__name__)
        _write(record_path, record)
        records.append(record)
        _write(Path(plan['report_dir']) / 'summary.json', _summary(plan, records))
    summary = _summary(plan, records)
    _write(Path(plan['report_dir']) / 'summary.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('db', 'owned-root', 'catalog-root', 'hermes-root', 'report-dir'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--snapshot-id')
    parser.add_argument('--helper-python', type=Path)
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--max-sessions', type=int, default=24)
    parser.add_argument('--preregister-only', action='store_true')
    args = parser.parse_args(argv)
    values = vars(args)
    max_sessions, only = values.pop('max_sessions'), values.pop('preregister_only')
    plan = preregister(**values)
    print(_json({'fingerprint': plan['fingerprint'], 'planned_sessions': 24} if only
                else run_comparison(plan, max_sessions=max_sessions)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
