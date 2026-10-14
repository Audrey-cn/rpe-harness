#!/usr/bin/env python3
"""Two separately registered repair attempts; formal results are never rescored."""
from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys

from rpe_harness.experiments.address_comparison import (
    _MARKER, _check_freeze, _environment, _export_bytes, _json, _observe, _read,
    _safe_path, _save, _sha, _trial, score_address_artifact)
from rpe_harness.experiments.cost_ledger import PhaseLedger, summarize_phases
from rpe_harness.skill_catalog import SkillCatalog, XLSX_SKILL_ID

SCRIPT = Path(__file__).absolute()
ROOT = SCRIPT.parents[1] / 'repair-smoke'


def prepare(args):
    if ROOT.exists() and any(ROOT.iterdir()):
        raise ValueError('repair-smoke must be new; never replace a registered attempt')
    ROOT.mkdir(parents=True, exist_ok=True)
    for name in ('fixtures', 'records', 'workspaces', 'sessions', 'catalog', 'databases'):
        (ROOT / name).mkdir()
    _save(ROOT / _MARKER, {'root': str(ROOT), 'scope': 'postformal_repair_smoke_only'})
    catalog = SkillCatalog(ROOT / 'catalog.sqlite', ROOT / 'catalog')
    snapshot = catalog.sync(args.skill_root)['snapshot_id']
    binding = catalog.trust_adapter(XLSX_SKILL_ID, snapshot)
    skill = catalog.lookup(XLSX_SKILL_ID, snapshot)
    cases = [
        {'case_id': 'repair-csv-new', 'capability': 'tabular.deduplicate', 'input_name': 'input.csv',
         'output_name': 'output.csv', 'expected_status': 'ready', 'category': 'production',
         'parameters': {'key_columns': ['门店', '批次'], 'delimiter': ',', 'encoding': 'utf-8', 'keep': 'first'},
         'constraints': ['preserve_original', 'preserve_order'],
         'expected_rows': [['门店', '批次', '品名'], ['北仓', '007', '甲'], ['南仓', '007', '乙'],
                           ['北仓', '7', '丙'], ['南仓', '0007', '丁']],
         'task': '请将input.csv按“门店”和“批次”两列组合去重，同组合保留第一次出现的完整行，保持表头、所有单元格及原来顺序。批次是精确字符串，007、7和0007不同。文件是utf-8编码的逗号CSV，请另存为output.csv。'},
        {'case_id': 'repair-xlsx-new', 'capability': 'xlsx.inspect', 'input_name': 'input.xlsx',
         'output_name': 'output.json', 'expected_status': 'ready', 'category': 'production',
         'parameters': {'mode': 'rows', 'sheet': '库存'}, 'constraints': ['preserve_original'],
         'expected': {'sheet': '库存', 'rows': [['货号', '件数', '核对'], ['0009', 12, True],
                                               ['螺钉', 3.75, False], ['数量零', 0, True]]},
         'task': '请用已审核XLSX技能的原始xlsx_read.py helper，只读读取input.xlsx的“库存”工作表全部行，将其--json --sheet 库存的原始输出结构保存为output.json。保留字段、行顺序和JSON中的字符串、整数、小数、布尔类型，不重写helper。'}]
    (ROOT / 'fixtures/repair-csv-new.csv').write_text(
        '门店,批次,品名\n北仓,007,甲\n南仓,007,乙\n北仓,007,后续\n北仓,7,丙\n南仓,0007,丁\n', encoding='utf-8')
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.active.title = '忽略'
    workbook.active.append(['不要读取此表'])
    sheet = workbook.create_sheet('库存')
    for row in cases[1]['expected']['rows']:
        sheet.append(row)
    workbook.save(ROOT / 'fixtures/repair-xlsx-new.xlsx')
    workbook.close()
    trials = []
    helper = str(Path(sys.executable).absolute())
    for case in cases:
        path = ROOT / 'fixtures' / (case['case_id'] + Path(case['input_name']).suffix)
        case.update(source_path=str(path), input_sha256=_sha(path.read_bytes()))
        trial = _trial(ROOT, case, 'address', 'repair_smoke', helper, binding)
        trial['db'] = str(ROOT / 'databases' / (trial['trial_id'] + '.sqlite'))
        with sqlite3.connect(catalog.db_path) as original, sqlite3.connect(trial['db']) as target:
            original.backup(target)
        trials.append(trial)
    environment = _environment(helper, args.hermes_root)
    plan = {'schema_version': 'rpe.postformal-repair-smoke/v1', 'root': str(ROOT),
        'scope': 'two address-only repair checks; no new native comparison or formal rescoring',
        'maximum_attempts': 2, 'failure_policy': 'reserve before launch; never retry or replace',
        'controller_sha256': _sha(SCRIPT.read_bytes()), 'db': str(catalog.db_path),
        'catalog_root': str(ROOT / 'catalog'), 'skill_root': str(_safe_path(args.skill_root)),
        'hermes_root': str(_safe_path(args.hermes_root)), 'helper_python': helper,
        'timeout': 180, 'max_turns': 8, 'skill_snapshot_id': snapshot,
        'skill_sha256': skill['skill_sha256'], 'script_sha256': skill['script_sha256'],
        'environment': environment, 'source_python_count': len(environment['source_sha256']),
        'cases': cases, 'sessions': trials, 'pilots': []}
    PhaseLedger(ROOT / 'ledger')
    return _save(ROOT / 'preregistration.json', plan)


def load():
    if _read(ROOT / _MARKER)['root'] != str(ROOT):
        raise ValueError('repair owner differs')
    plan = _read(ROOT / 'preregistration.json')
    if plan['controller_sha256'] != _sha(SCRIPT.read_bytes()):
        raise ValueError('frozen controller changed')
    return plan


def summary(plan):
    phases = PhaseLedger(ROOT / 'ledger').records()
    records = [_read(ROOT / 'records' / (row['attempt_id'] + '.json'))
               if (ROOT / 'records' / (row['attempt_id'] + '.json')).exists() else
               {'trial_id': row['attempt_id'], 'status': 'incomplete', 'accepted': False} for row in phases]
    return {'scope': plan['scope'], 'registration_sha256': plan['sha256'], 'maximum_attempts': 2,
            'reserved_attempts': len(phases), 'costs': summarize_phases(phases), 'records': records}


def run(plan, maximum):
    from rpe_harness.integrations.hermes_session import run_hermes_session
    ledger = PhaseLedger(ROOT / 'ledger')
    reserved = {row['attempt_id'] for row in ledger.records()}
    allowed = {trial['trial_id'] for trial in plan['sessions']}
    if len(reserved) > 2 or not reserved <= allowed or not 0 <= maximum <= 2:
        raise ValueError('repair budget is two total attempts including failures and unknowns')
    def runner(*arguments, **kwargs):
        kwargs['deny_read_roots'] = [*kwargs['deny_read_roots'], SCRIPT, SCRIPT.parent,
                                    ROOT.parent / 'formal-comparison', ROOT.parent / 'comparison']
        return run_hermes_session(*arguments, **kwargs)
    launched = 0
    for trial in plan['sessions']:
        if trial['trial_id'] in reserved or launched >= min(maximum, 2 - len(reserved)):
            continue
        _check_freeze(plan)
        ledger.reserve('address', 'repair_smoke', trial['trial_id'])
        launched += 1
        record = _observe(plan, trial, runner)
        record['context_minimized'] = record['gate'].get('context_minimized') is True if record.get('gate') else False
        record['accepted'] = bool(record['accepted'] and record['context_minimized'])
        if not record['accepted'] and record['status'] == 'completed':
            record['status'] = 'failed'
        _save(ROOT / 'records' / (trial['trial_id'] + '.json'), record)
        outcome = ({key: record[key] for key in
            ('status', 'elapsed_seconds', 'usage', 'model_identity')} |
            {'artifact_correct': record['business_correct'], 'workflow_verified': record['invocation_evidence']})
        if record.get('error_code'):
            outcome['error_code'] = record['error_code']
        ledger.finish(trial['trial_id'], outcome)
    return summary(plan)


def export(plan, destination):
    destination = _safe_path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('public export must be new')
    destination.mkdir(parents=True, exist_ok=True)
    _save(destination / 'summary.json', summary(plan))
    _save(destination / 'cost-phases.json', {'records': PhaseLedger(ROOT / 'ledger').records()})
    _save(destination / 'registration.json', {key: plan[key] for key in
        ('scope', 'maximum_attempts', 'failure_policy', 'controller_sha256', 'source_python_count',
         'environment', 'skill_snapshot_id', 'skill_sha256', 'script_sha256')} |
        {'registration_sha256': plan['sha256']})
    for trial, case in zip(plan['sessions'], plan['cases']):
        fresh = score_address_artifact(plan, trial)
        folder = destination / trial['trial_id']
        folder.mkdir()
        _save(folder / 'public-task.json', {key: case[key] for key in case if key != 'source_path'})
        _save(folder / 'artifact-verification.json', fresh)
        if fresh['business_correct'] and fresh['product_oracle_passed']:
            raw, metadata = _export_bytes(Path(trial['workspace']) / case['output_name'], case['output_name'])
            if raw is not None and metadata['sha256'] == fresh['artifact_sha256']:
                (folder / case['output_name']).write_bytes(raw)
    return {'exported': str(destination), 'raw_history_exported': False, 'databases_exported': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'summary', 'export'))
    parser.add_argument('--skill-root', default=str(Path.home() / '.hermes/skills'))
    parser.add_argument('--hermes-root', default=str(Path.home() / '.hermes/hermes-agent'))
    parser.add_argument('--max-sessions', type=int, default=2)
    parser.add_argument('--destination', default=str(ROOT / 'public-export'))
    args = parser.parse_args()
    value = prepare(args) if args.command == 'prepare' else run(load(), args.max_sessions) if args.command == 'run' else summary(load()) if args.command == 'summary' else export(load(), args.destination)
    print(_json(value))
