#!/usr/bin/env python3
"""Independent four-session directed acceptance. Default: preregister, no model.

Run this outside the source tree with the frozen installed interpreter and -I.
Raw host logs remain only in owned-root; reports contain identifiers and hashes.
A reserved trial is never relaunched, including an interrupted or failed trial.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid

DIRECTED_VERSION = 'directed-valid-v2'


def recovery_rows():
    return [['id', 'value'], ['001', '首条'], ['01', '不同前导零'], ['001', '重复']]


def receipt_links(events, streams):
    path = Path(__file__).with_name('v16_rescore_native.py')
    spec = importlib.util.spec_from_file_location('directed_receipt_verification', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.receipt_links(events, streams)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def save(path, value, *, exclusive=False):
    with path.open('x' if exclusive else 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def csv_truth(rows):
    """Independent literal CSV oracle: string identity, first record, original order."""
    position = rows[0].index('id')
    seen, kept = set(), [rows[0]]
    for row in rows[1:]:
        key = row[position]
        if key not in seen:
            seen.add(key)
            kept.append(row)
    return kept


def helper_refusal_command(command, python, helper, workspace):
    """Require an actual literal original-helper command, rather than an echoed path."""
    if not isinstance(command, str) or '$' in command or '`' in command:
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|><')
    lexer.whitespace_split, lexer.commenters = True, ''
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    for index, token in enumerate(tokens):
        if token != python or index and tokens[index - 1] not in (';', '&&', '||', 'then', 'do'):
            continue
        tail = tokens[index + 1:]
        if tail[:1] == ['-B']:
            tail = tail[1:]
        if (len(tail) >= 6 and tail[0] == helper and tail[1] in ('input.xlsx', str(workspace / 'input.xlsx'))
                and tail[2:6] == ['--json', '--sheet', '__missing__', '>']):
            return True
        if len(tail) >= 5 and tail[:5] == [helper, 'input.xlsx', '--json', '--sheet', '__missing__'] and (len(tail) == 5 or tail[5] in (';', '&&', '||')):
            return True
    return False


def package_identity():
    import importlib.metadata
    import rpe_harness
    path = Path(rpe_harness.__file__).resolve()
    if 'site-packages' not in path.parts or not path.is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError('driver requires the frozen installed package via -I')
    hashes = {str(p.relative_to(path.parent)): sha(p.read_bytes()) for p in path.parent.rglob('*.py')}
    return {'path': str(path), 'version': importlib.metadata.version('rpe-harness'), 'modules': hashes}


def preregister(args):
    from openpyxl import Workbook
    from rpe_harness.skill_catalog import SkillCatalog
    owned, reports = args.owned_root.absolute(), args.report_dir.absolute()
    for directory in (owned, reports):
        if directory.is_symlink():
            raise ValueError('owned roots must not be symlinks')
        directory.mkdir(parents=True, exist_ok=True)
    catalog = SkillCatalog(args.db, args.catalog_root)
    row = catalog.lookup('productivity/xlsx')
    if not row['executable'] or row['adapter_status'] != 'trusted' or 'tabular.deduplicate' not in row['capabilities']:
        raise ValueError('trusted XLSX skill must support explicit CSV contract adaptation')
    helper = str(args.helper_python.absolute())  # Preserve venv interpreter identity.
    if helper != str(Path(sys.executable).absolute()):
        raise ValueError('native helper must use the same installed interpreter as the RPE executor')
    version = subprocess.run([helper, '-I', '-B', '-c', 'import json,sys,openpyxl;print(json.dumps({"python":sys.version,"openpyxl":openpyxl.__version__}))'], capture_output=True, text=True, check=True, timeout=15)
    config = Path.home() / '.hermes/config.yaml'
    frozen = {'directed_version': DIRECTED_VERSION, 'probe_only': args.probe_only,
        'receipt_verifier_sha256': sha(Path(__file__).with_name('v16_rescore_native.py').read_bytes()),
        'db': str(args.db.resolve()), 'owned_root': str(owned), 'report_dir': str(reports),
        'catalog_root': str(args.catalog_root.resolve()), 'hermes_root': str(args.hermes_root.resolve()),
        'helper_python': helper, 'helper_version': json.loads(version.stdout), 'snapshot_id': row['snapshot_id'],
        'skill_sha256': row['skill_sha256'], 'script_sha256': row['script_sha256'],
        'package': package_identity(), 'driver_sha256': sha(Path(__file__).read_bytes()),
        'host_config_sha256': sha(config.read_bytes()), 'timeout': 180,
        'scope': 'two directed probes x native/RPE; separate from formal24',
        'recovery_execution': 'CSV contract; native hand-written CSV, RPE tabular.deduplicate; no original XLSX-helper dedup claim',
        'failure_policy': 'reserve before launch, never replace or rerun a reserved trial'}
    registration = reports / 'preregistration.json'
    if registration.exists():
        plan = json.loads(registration.read_text())
        if sha(canonical({k: v for k, v in plan.items() if k != 'fingerprint'}).encode()) != plan.get('fingerprint'):
            raise ValueError('frozen preregistration fingerprint differs')
        if any(plan.get(k) != v for k, v in frozen.items()):
            raise ValueError('frozen preregistration differs; use another experiment root')
        return plan
    fixtures = owned / 'fixtures'
    fixtures.mkdir()
    workbook = Workbook()
    workbook.active.title = 'Data'
    workbook.active.append(['id', 'value'])
    workbook.active.append(['001', True])
    workbook.save(fixtures / 'input.xlsx')
    workbook.close()
    rows = recovery_rows()
    with (fixtures / 'input.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        csv.writer(stream).writerows(rows)
    source = {'key_columns': ['id']}
    save(fixtures / 'task-context.json', source, exclusive=True)
    common = ('先发现并通过 skill_view 加载 xlsx 技能。执行 Python 仅使用 ' + shlex.quote(helper) +
        '；不得导入 Harness 核心、调用 Harness CLI/HTTP 或验收器。原文件保持不变，禁止覆盖已有输出。')
    prompts = {
        'missing-sheet-refusal': common + '读取 input.xlsx 的 __missing__ 工作表全部行，mode=rows，输出 out.json。'
            '该工作表不存在时必须拒绝且不能生成任何 out.json；native 使用加载目录中的原 scripts/xlsx_read.py '
            'input.xlsx --json --sheet __missing__，只在成功时发布输出；有任务工具时遵循 discovery/load/context/task。',
        'required-source-context': common + '恢复上轮确认关键列，原记录在 task-context.json；'
            '对 input.csv 保留首条/原顺序/原文件/输出 out.csv；发现并加载xlsx技能后做契约去重。'
            'CSV 为 utf-8-sig，键按完整字符串相等，不能转数字或修剪空格。'
            'native 读取 task-context.json 自行使用 csv 编写去重，原 xlsx helper 无去重能力。'
            '有 RPE 工具时发现 capability=tabular.deduplicate,input_format=csv；'
            'rpe_context 只提供 input_path/output_path，不提供 key_columns 且省略 source_refs，'
            '让父侧上轮可信来源补回关键列，不能根据表头猜；之后按恢复事实执行。'}
    trials = []
    for probe in ('missing-sheet-refusal', 'required-source-context'):
        if args.probe_only is not None and args.probe_only != probe:
            continue
        for arm in ('native', 'rpe'):
            trial_id = probe + '-' + arm
            workspace = owned / 'workspaces' / trial_id
            workspace.mkdir(parents=True)
            name = 'input.xlsx' if probe == 'missing-sheet-refusal' else 'input.csv'
            (workspace / name).write_bytes((fixtures / name).read_bytes())
            if probe == 'required-source-context':
                (workspace / 'task-context.json').write_bytes((fixtures / 'task-context.json').read_bytes())
            (workspace / 'prompt.txt').write_text(prompts[probe])
            trials.append({'trial_id': trial_id, 'probe_id': probe, 'arm': arm, 'workspace': str(workspace),
                'input_name': name, 'output_name': 'out.json' if name.endswith('xlsx') else 'out.csv',
                'input_sha256': sha((workspace / name).read_bytes()), 'prompt_sha256': sha(prompts[probe].encode()),
                'context_sha256': sha((fixtures / 'task-context.json').read_bytes()) if name.endswith('csv') else None})
    plan = {**frozen, 'trials': trials, 'source_payload': source, 'csv_expected': csv_truth(rows)}
    plan['fingerprint'] = sha(canonical(plan).encode())
    (owned / 'records').mkdir()
    save(registration, plan, exclusive=True)
    return plan


def offline(plan):
    """Real separate CLI processes: no-source failure -> captured facts -> ready -> restart."""
    destination = Path(plan['owned_root']) / 'records/offline-context.json'
    if destination.exists():
        return json.loads(destination.read_text())
    root = Path(plan['owned_root']) / 'offline'
    root.mkdir()
    db, session = root / 'state.sqlite', 'directed-offline-' + uuid.uuid4().hex
    save(root / 'payload.json', plan['source_payload'], exclusive=True)
    save(root / 'facts.json', {'key_columns': ['id']}, exclusive=True)
    base = {'input_path': str(root / 'input.csv'), 'output_path': str(root / 'out.csv')}
    def invoke(arguments, request=None):
        if request is not None:
            save(root / 'request.json', request)
            arguments += ['--request', str(root / 'request.json')]
        child = subprocess.run([sys.executable, '-I', '-B', '-m', 'rpe_harness', 'context', *arguments, '--db', str(db)],
            cwd=root, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=20)
        return json.loads(child.stdout)
    missing = invoke(['recover', '--session-id', session, '--capability', 'tabular.deduplicate'], {'facts': base, 'source_refs': []})
    ref = invoke(['capture', '--session-id', session, '--source', 'user', '--event-id', 'prior-turn', '--payload', str(root / 'payload.json'), '--facts', str(root / 'facts.json')])
    ready = invoke(['recover', '--session-id', session, '--capability', 'tabular.deduplicate'], {'facts': base, 'source_refs': [ref]})
    history = invoke(['show', ready['recovery_id']])
    fresh = invoke(['revalidate', ready['recovery_id'], '--session-id', session])
    conflict = invoke(['recover', '--session-id', session, '--capability', 'tabular.deduplicate'], {'facts': {**base, 'key_columns': ['value']}, 'source_refs': [ref]})
    result = {'passed': missing['status'] == 'needs_context' and ready['status'] == fresh['status'] == 'ready'
        and history['historical'] is True and 'key_columns' in ready['recovered_fields']
        and ready['provenance']['key_columns'] == [ref] and conflict['status'] == 'conflict',
        'missing': missing, 'source_ref': ref, 'ready': ready, 'restart_history': history, 'restart_fresh': fresh, 'conflict': conflict}
    save(destination, result, exclusive=True)
    return result


def native_evidence(result, plan, trial):
    from rpe_harness.integrations.hermes_gate import _persisted_events, _same_call, digest_json
    from rpe_harness.skill_catalog import SkillCatalog
    events = _persisted_events(plan['db'], result['session_id'])
    if events != result['events']:
        raise ValueError('native durable observation mismatch')
    receipt_proof = receipt_links(events, result.get('stream_tool_calls', []))
    if not receipt_proof['passed']:
        raise ValueError('native receipt identity or stream argument multiset mismatch')
    row = SkillCatalog(plan['db'], plan['catalog_root']).lookup('productivity/xlsx', snapshot_id=plan['snapshot_id'])
    source = Path(result['session_root']) / 'home/skills/productivity/xlsx/SKILL.md'
    helper = source.parent / 'scripts/xlsx_read.py'
    if sha(source.read_bytes()) != plan['skill_sha256'] or sha(helper.read_bytes()) != plan['script_sha256']:
        raise ValueError('native materialized skill changed')
    pairs = []
    for pre in [e for e in events if e['kind'] == 'pre_tool_call']:
        apis = [e for e in events if e['kind'] == 'pre_api_request' and all(e.get(k) == pre.get(k) and pre.get(k) for k in ('session_id', 'task_id', 'turn_id', 'api_request_id'))]
        emitted = [call for e in events if e['kind'] == 'post_api_request' and all(e.get(k) == pre.get(k) for k in ('session_id', 'task_id', 'turn_id', 'api_request_id')) for call in e.get('model_tool_calls', []) if call.get('tool_call_id') == pre.get('tool_call_id') and call.get('tool_name') == pre.get('tool_name')]
        if len(apis) != 1 or len(emitted) != 1:
            raise ValueError('native actual provider call identity missing')
        posts = [e for e in events if e['kind'] == 'post_tool_call' and e.get('tool_name') == pre.get('tool_name') and _same_call(e, pre)]
        streams = [c for c in result['stream_tool_calls'] if c.get('name') == pre.get('tool_name') and
            digest_json(c.get('input')) == pre.get('arguments_sha256') and
            ('tool_call_id' not in c or c['tool_call_id'] == pre.get('tool_call_id'))]
        if len(posts) != 1 or not streams or posts[0].get('arguments_sha256') != pre.get('arguments_sha256'):
            raise ValueError('native real call/arguments linkage missing')
        pairs.append((pre, posts[0], streams[0]['input']))
    loads = [(pre, post) for pre, post, args in pairs if pre['tool_name'] == 'skill_view' and args == {'name': row['load_name']}
        and post.get('status') == 'loaded' and post.get('content_sha256') == plan['skill_sha256']
        and post.get('snapshot_id') == plan['snapshot_id'] and post.get('skill_source_path') == str(source)]
    if not loads:
        raise ValueError('native original skill load missing')
    terminals = []
    for pre, post, args in pairs:
        raw = canonical(args)
        if pre['tool_name'] == 'rpe_task' or any(word in raw.lower() for word in ('rpe_harness', 'rpe-harness', 'verification.py')):
            raise ValueError('native accessed Harness')
        if pre['tool_name'] == 'terminal' and events.index(pre) > events.index(loads[0][1]):
            command = args.get('command', '')
            if plan['helper_python'] in command and isinstance(post.get('tool_exit_code'), int):
                terminals.append((pre, post, command))
    if trial['probe_id'] == 'missing-sheet-refusal':
        terminals = [item for item in terminals if helper_refusal_command(item[2], plan['helper_python'], str(helper), Path(trial['workspace']))]
    else:
        terminals = [item for item in terminals if item[1].get('status') == 'succeeded']
        if not any('task-context.json' in canonical(args) for _, _, args in pairs):
            raise ValueError('native did not read prior-turn context')
    if not terminals:
        raise ValueError('native corresponding execution missing')
    return {'durable_linked': True, 'receipt_proof': receipt_proof,
        'load_call_id': loads[0][0]['tool_call_id'], 'execution_call_ids': [item[0]['tool_call_id'] for item in terminals],
        'execution_kind': 'original_helper_refusal' if trial['probe_id'] == 'missing-sheet-refusal' else 'native_csv_contract'}


def run(plan, max_sessions):
    from rpe_harness.integrations.hermes_session import run_hermes_session
    from rpe_harness.integrations.skill_gate import evaluate_skill_session
    from rpe_harness.context import ContextStore
    owned = Path(plan['owned_root'])
    records, launched = [], 0
    for trial in plan['trials']:
        path = owned / 'records' / (trial['trial_id'] + '.json')
        if path.exists():
            records.append(json.loads(path.read_text()))
            continue
        if launched >= max_sessions:
            continue
        if package_identity() != plan['package'] or sha((Path.home() / '.hermes/config.yaml').read_bytes()) != plan['host_config_sha256']:
            raise ValueError('frozen package/model configuration changed')
        workspace = Path(trial['workspace'])
        prompt = (workspace / 'prompt.txt').read_text()
        if sha(prompt.encode()) != trial['prompt_sha256'] or sha((workspace / trial['input_name']).read_bytes()) != trial['input_sha256']:
            raise ValueError('frozen source/prompt changed')
        record = {**trial, 'fingerprint': plan['fingerprint'], 'status': 'interrupted', 'accepted': False}
        save(path, record, exclusive=True)
        launched += 1
        try:
            sources = [{'source': 'user', 'event_id': 'prior-turn', 'payload': plan['source_payload'], 'facts': {'key_columns': ['id']}}] if trial['probe_id'] == 'required-source-context' else []
            result = run_hermes_session(prompt, workspace, plan['db'], tool_enabled=trial['arm'] == 'rpe', owner_root=owned / 'sessions',
                hermes_root=plan['hermes_root'], timeout=plan['timeout'], entry_mode='skills' if trial['arm'] == 'rpe' else 'optional',
                catalog_root=plan['catalog_root'], skill_snapshot_id=plan['snapshot_id'], context_sources=sources,
                read_isolation_root=owned, deny_read_roots=[Path(plan['report_dir']), owned / 'records', owned / 'fixtures', owned / 'offline'])
            record.update(session_id=result.get('session_id'), usage=result.get('usage'),
                elapsed_seconds=result.get('elapsed_seconds'), model_identity=result.get('model_identity'),
                billed_cost=result.get('billed_cost'), completed=result.get('completed'),
                actual_prompt_sha256=result.get('prompt_sha256'), catalog_snapshot_id=result.get('catalog_snapshot_id'))
            save(path, record)
            save(owned / 'records' / (trial['trial_id'] + '.raw.json'), result, exclusive=True)
            output = workspace / trial['output_name']
            unchanged = sha((workspace / trial['input_name']).read_bytes()) == trial['input_sha256']
            host_ok = result.get('completed') is True and result.get('exit_code') == 0 and not result.get('timed_out') and not result.get('observer_errors') and (result.get('process_cleanup') or {}).get('cleanup_complete') is True
            comparable = result.get('prompt_sha256') == trial['prompt_sha256'] and result.get('catalog_snapshot_id') == plan['snapshot_id'] and result.get('model_identity') is not None and all(r.get('model_identity') in (None, result['model_identity']) for r in records)
            no_artifact = not os.path.lexists(output)
            exact = False
            if trial['probe_id'] == 'required-source-context' and output.is_file() and not output.is_symlink():
                with output.open(encoding='utf-8-sig', newline='') as stream:
                    exact = list(csv.reader(stream, strict=True)) == plan['csv_expected']
            if trial['arm'] == 'rpe':
                gate = evaluate_skill_session(result, plan['db'], workspace, catalog_root=plan['catalog_root'])
                if trial['probe_id'] == 'missing-sheet-refusal':
                    accepted = gate.get('status') == 'rejected' and not gate['errors'] and gate.get('verified') is False and no_artifact
                else:
                    context_events = [e for e in result['events'] if e['kind'] == 'rpe_context_recovered' and e.get('tool_name') == 'rpe_context']
                    contexts = [ContextStore(plan['db']).get_recovery(e['recovery_id']) for e in context_events]
                    restored = [c for c in contexts if c['status'] == 'ready' and 'key_columns' in c['recovered_fields'] and c['facts'].get('key_columns') == ['id'] and c['provenance'].get('key_columns')]
                    if len(restored) != 1:
                        raise ValueError('key_columns were not truly source-restored')
                    evidence = restored[0]
                    source_rows = [ContextStore(plan['db']).resolve(ref, session_id=result['session_id']) for ref in evidence['provenance']['key_columns']]
                    if not all(s['payload'] == {'original_event_id': 'prior-turn', 'event': plan['source_payload']} and s['facts'] == {'key_columns': ['id']} for s in source_rows):
                        raise ValueError('source provenance differs from prior-turn facts')
                    gate['context_evidence'] = {k: evidence[k] for k in ('recovery_id', 'session_id', 'recovered_fields', 'provenance')}
                    tools = [e.get('tool_name') for e in result['events'] if e['kind'] == 'pre_tool_call']
                    accepted = gate.get('harness_verified') is True and exact and set(tools) <= {'rpe_discover', 'skill_view', 'rpe_context', 'rpe_task'}
            else:
                gate = native_evidence(result, plan, trial)
                accepted = gate['durable_linked'] and (no_artifact if trial['probe_id'] == 'missing-sheet-refusal' else exact)
            context_same = trial['context_sha256'] is None or sha((workspace / 'task-context.json').read_bytes()) == trial['context_sha256']
            record.update(status='completed', accepted=bool(accepted and host_ok and unchanged and comparable and context_same), source_unchanged=unchanged,
                comparable=comparable, context_source_unchanged=context_same, artifact_exact=exact, no_artifact=no_artifact,
                session_id=result.get('session_id'), elapsed_seconds=result.get('elapsed_seconds'), usage=result.get('usage'),
                model_identity=result.get('model_identity'), gate=gate, output_sha256=sha(output.read_bytes()) if output.is_file() else None)
        except Exception as error:
            record.update(status='failed', error_type=type(error).__name__)
        save(path, record)
        records.append(record)
        save(Path(plan['report_dir']) / 'summary.json', {'scope': plan['scope'], 'fingerprint': plan['fingerprint'], 'records': records, 'passed': len(records) == len(plan['trials']) and all(r['accepted'] for r in records)})
    return {'scope': plan['scope'], 'fingerprint': plan['fingerprint'], 'records': records, 'passed': len(records) == len(plan['trials']) and all(r['accepted'] for r in records)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('db', 'owned-root', 'catalog-root', 'hermes-root', 'report-dir', 'helper-python'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--max-sessions', type=int, choices=range(5), default=0, help='0 preregisters without a model; 4 executes all directed trials')
    parser.add_argument('--probe-only', choices=('required-source-context', 'missing-sheet-refusal'))
    args = parser.parse_args(argv)
    if not all(getattr(args, name).is_absolute() for name in ('db', 'owned_root', 'catalog_root', 'hermes_root', 'report_dir', 'helper_python')):
        parser.error('all paths must be absolute')
    plan = preregister(args)
    offline_result = offline(plan)
    if not offline_result['passed']:
        raise ValueError('offline context grounded recovery failed')
    summary = run(plan, args.max_sessions)
    save(args.report_dir / 'summary.json', {**summary, 'offline_context_passed': True})
    print(json.dumps({'preregistered': True, 'models_launched_limit': args.max_sessions, 'passed': summary['passed'],
        'records': len(summary['records']), 'report': str(args.report_dir / 'summary.json')}, ensure_ascii=False))
    return 0 if args.max_sessions == 0 or summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
