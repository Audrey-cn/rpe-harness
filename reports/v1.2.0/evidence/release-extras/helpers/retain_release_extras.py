"""Audit-only retention. Run AFTER Root explicitly completes all release probes.

This script never calls a model, runs a probe, opens credentials or copies SQLite.
Use owned/release-venv/bin/python -I -B and the explicit readiness acknowledgement.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import zipfile


DEFAULT_OWNED = '/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T/rpe-v12-development-20261014-fp1l7bvp'
DEFAULT_REPOSITORY = '/Users/audrey/项目/rpe-harness'
OLD_WHEELS = {
    '97794dfa0b9c92294c32b6b9337b7d99aae927d1c9187efe58de73faa733b1ca': 'post-broker pre-parameter-feedback; anchored four-of-six and same-source XLSX two-of-two diagnostic',
    'bee526c7cb37844d2036602a3732b678c1d365f6e832d33c936733afbc47e1fc': 'formal 36 / pre-admission-guard',
    'b9ab6a7e5ec85d59c544f813239e90fabb2178e2c1c1594f083b9f10f53517a7': 'pre-runtime-sandbox-fix',
    'd495baa79c78bdf3855aaa62ffff8758d5af4ed4f252db7a9e778926c2cf4689': 'pre-external-executor',
}
CANDIDATE_SHA = 'ed3882aee3ce8e036071effbf11da6df46e276cb75c3055056c483c5f233698e'
CASES = {'csv_chinese', 'csv_multiline', 'xlsx_typed_keys', 'xlsx_formulas',
         'reject_json', 'reject_formula_dependency'}
PHASES = {'after_execution', 'before_publication_intent', 'after_publication'}
EXTENSIONS = {'.csv', '.xlsx', '.json', '.py'}
FORBIDDEN_COMPONENTS = {'home', 'session', 'sessions', 'cache', 'skills', 'auth',
                        'config', 'state', 'transcripts', 'chat', '__pycache__'}
FORBIDDEN_KEYS = {'api_key', 'access_token', 'refresh_token', 'authorization', 'credentials',
                  'password', 'secret', 'bearer', 'token', 'messages', 'prompt', 'system_prompt',
                  'assistant_message', 'stdout', 'stderr', 'conversation', 'history',
                  'raw_response', 'transcript', 'raw_chat', 'environment', 'env'}
RUN_KEYS = {'run_id', 'status', 'stage', 'owner_pid', 'started_at', 'finished_at', 'skill_id',
            'attempted', 'verification_ok', 'source', 'event_id', 'payload_hash', 'state',
            'verification', 'artifact', 'error', 'event', 'steps', 'retry_of', 'attempt_number',
            'total_attempts', 'recovery'}
EVENT_KEYS = {'capability', 'constraints', 'event_id', 'execution', 'goal', 'schema_version',
              'session_id', 'source_ref'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode()


def safe_json(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace('-', '_')
            require(normalized not in FORBIDDEN_KEYS and not normalized.endswith('_api_key')
                    and not normalized.endswith('_access_token'), 'unsafe JSON field: ' + str(key))
            safe_json(child)
    elif isinstance(value, list):
        for child in value:
            safe_json(child)


def no_symlinks(path, boundary):
    path = Path(os.path.abspath(path))
    require(path.is_relative_to(boundary), 'path is outside declared boundary')
    current = boundary
    require(not current.is_symlink(), 'boundary must not be a symlink')
    for part in path.relative_to(boundary).parts:
        current /= part
        require(not current.is_symlink(), 'symlink is not allowed: ' + str(current))
    return path


def tree_sha(files):
    # Same exact relative-name/bytes digest convention as entry_reset_parent.py.
    return sha(json.dumps(files, separators=(',', ':')).encode())


def readonly_run(database, run_id):
    """Use installed RunStore.get without its mutating constructor/connect."""
    from rpe_harness.storage import RunStore

    require(re.fullmatch('[a-f0-9]{32}', run_id) is not None, 'invalid run ID')

    @contextmanager
    def read_connection():
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        try:
            yield connection
        finally:
            connection.close()

    store = object.__new__(RunStore)
    store.path = database
    store.connect = read_connection
    return store.get(run_id)


class Retention:
    def __init__(self, owned, repository):
        self.owned = owned
        self.repository = repository
        self.report_root = repository / 'reports/v1.2.0'
        self.evidence = self.report_root / 'evidence/release-extras'
        self.artifacts = self.report_root / 'artifacts/release-extras'
        self.pending = {}
        self.files = []
        self.index = []

    def read(self, path, *, database=False):
        path = no_symlinks(path, self.owned)
        require(path.is_file(), 'required evidence file is missing: ' + str(path))
        if database:
            require(path.name == 'runs.sqlite', 'only explicit synthetic run databases are readable')
            return path
        require(path.suffix in EXTENSIONS, 'source extension is not allowlisted')
        require(not any(part.lower() in FORBIDDEN_COMPONENTS or part.startswith('.')
                        for part in path.relative_to(self.owned).parts), 'excluded source path')
        maximum = 16 * 1024 * 1024 if path.suffix in {'.csv', '.xlsx'} else 4 * 1024 * 1024
        require(path.stat().st_size <= maximum, 'evidence file exceeds retention bound')
        return path.read_bytes()

    def json(self, path):
        data = self.read(path)
        value = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
        safe_json(value)
        return value

    def add(self, target, data, role, *, source=None, original_path=None, generated=False):
        target = no_symlinks(target, self.repository)
        require(target.is_relative_to(self.evidence) or target.is_relative_to(self.artifacts),
                'retention target is outside extras namespaces')
        require(target.suffix in EXTENSIONS or target.name == 'README.md', 'target extension not allowed')
        if target.suffix == '.json':
            safe_json(json.loads(data))
        if target in self.pending:
            require(self.pending[target] == data, 'duplicate target with differing evidence')
            return
        self.pending[target] = data
        entry = {'path': str(target.relative_to(self.report_root)), 'role': role,
                 'bytes': len(data), 'sha256': sha(data), 'generated': generated}
        if source is not None:
            entry['source_relative_to_owned'] = str(source.relative_to(self.owned))
        self.files.append(entry)
        if original_path:
            self.index.append({'original_temporary_path': str(original_path),
                               'retained_path': str(target.relative_to(self.report_root)),
                               'sha256': sha(data), 'role': role})

    def copy(self, source, target, role):
        data = self.read(source)
        self.add(target, data, role, source=source, original_path=source)
        return data

    def run_record(self, run, target, *, workspace, database=None, source=None):
        require(isinstance(run, dict) and set(run).issubset(RUN_KEYS), 'unexpected run-record fields')
        require(re.fullmatch('[a-f0-9]{32}', run.get('run_id', '')) is not None, 'invalid run-record ID')
        require(isinstance(run.get('event'), dict) and set(run['event']).issubset(EVENT_KEYS),
                'unexpected business event fields')
        require(run['event'].get('schema_version') == 2 and
                run['event'].get('capability') == 'tabular.deduplicate', 'not an allowlisted synthetic contract')
        execution = run['event'].get('execution') or {}
        for key in ('input_path', 'output_path'):
            path = no_symlinks(Path(execution[key]), self.owned)
            require(path.is_relative_to(workspace), 'run is unrelated to the synthetic fixture workspace')
        if run.get('status') == 'succeeded':
            verification, artifact = run.get('verification') or {}, run.get('artifact') or {}
            require(verification.get('passed') is True and artifact.get('published') is True,
                    'successful run lacks published verification')
            input_hash = sha(self.read(Path(execution['input_path'])))
            output_hash = sha(self.read(Path(execution['output_path'])))
            require(input_hash == artifact.get('input_sha256') == verification.get('input_sha256'),
                    'successful source no longer matches durable hashes')
            require(output_hash == artifact.get('sha256') == verification.get('output_sha256'),
                    'successful output no longer matches durable hashes')
        safe_json(run)
        if source:
            self.copy(source, target, 'safe_durable_run_record')
        else:
            self.add(target, encoded(run), 'readonly_RunStore_get_export', generated=True)
            self.index.append({'original_temporary_path': str(database), 'run_id': run['run_id'],
                               'retained_path': str(target.relative_to(self.report_root)),
                               'role': 'readonly_RunStore_get_export'})
        if database:
            self.add(target.parent / 'request.json', encoded(run['event']),
                     'durable_business_request_export', generated=True)

    def workspace(self, source, target, names):
        no_symlinks(source, self.owned)
        require(source.is_dir(), 'required synthetic workspace is missing')
        for name in names:
            file = source / name
            if file.exists():
                self.copy(file, target / name, 'synthetic_business_fixture')

    def flush(self):
        # Validate every existing destination before any mutation; never replace history.
        for path, data in self.pending.items():
            no_symlinks(path, self.repository)
            if path.exists():
                require(path.is_file() and path.read_bytes() == data,
                        'refusing to replace different retained evidence: ' + str(path))
        for path, data in self.pending.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                with path.open('xb') as stream:
                    stream.write(data)
        for path, data in self.pending.items():
            require(sha(path.read_bytes()) == sha(data), 'retained hash mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owned-root', default=DEFAULT_OWNED)
    parser.add_argument('--repository', default=DEFAULT_REPOSITORY)
    parser.add_argument('--ready-after-root-probes', action='store_true')
    parser.add_argument('--wheel-sha256', required=True)
    args = parser.parse_args()
    require(args.ready_after_root_probes, 'Root must explicitly confirm all probes are done')
    owned, repository = Path(args.owned_root).resolve(strict=True), Path(args.repository).resolve(strict=True)
    require(sys.flags.isolated == 1 and sys.dont_write_bytecode is True, 'run with release Python -I -B')
    require(Path(sys.prefix).resolve() == (owned / 'release-venv').resolve(), 'release-venv Python required')
    require(re.fullmatch('[a-f0-9]{64}', args.wheel_sha256) is not None and
            args.wheel_sha256 not in OLD_WHEELS, 'explicit final wheel SHA is required; old snapshots are historical')
    retain = Retention(owned, repository)
    import rpe_harness
    package = Path(rpe_harness.__file__).resolve().parent
    require(package.is_relative_to(owned / 'release-venv'), 'installed release package required')
    wheel = no_symlinks(owned / 'wheel-release/rpe_harness-1.2.0-py3-none-any.whl', owned)
    require(wheel.is_file() and sha(wheel.read_bytes()) == args.wheel_sha256, 'final wheel bytes mismatch')
    source_identity = []
    with zipfile.ZipFile(wheel) as archive:
        members = sorted(name for name in archive.namelist() if name.startswith('rpe_harness/') and name.endswith('.py'))
        require(len(members) == 32, 'final package must contain exactly 32 production Python sources')
        for name in members:
            relative = Path(name).relative_to('rpe_harness')
            installed = no_symlinks(package / relative, owned)
            checkout = no_symlinks(repository / 'src' / name, repository)
            data = archive.read(name)
            require(installed.read_bytes() == checkout.read_bytes() == data, 'source/wheel/install differs: ' + name)
            source_identity.append({'path': name, 'sha256': sha(data)})
        require(len(list((repository / 'src/rpe_harness').rglob('*.py'))) == 32, 'checkout source count differs')
    package_tree_sha = tree_sha([(str(Path(row['path']).relative_to('rpe_harness')), row['sha256'])
                                for row in source_identity])
    state = owned / 'installed-contract-release'
    local = retain.json(state / 'report.json')
    require(local['wheel_sha256'] == args.wheel_sha256 and local['installed_members_match'] is True,
            'local install identity mismatch')
    require(len(local['checks']) == 6 and {c['case_id'] for c in local['checks']} == CASES,
            'local six-case probe has not completed')
    retain.copy(state / 'report.json', retain.evidence / 'installed-contract.json', 'compact_installed_contract_report')
    for check in local['checks']:
        workspace = state / 'fixtures' / check['case_id']
        destination = retain.artifacts / 'installed-contract' / check['case_id']
        retain.workspace(workspace, destination, ('input.csv', 'input.xlsx', 'input.json', 'output.csv', 'output.xlsx', 'request.json'))
        run = retain.json(workspace / 'run.json')
        require(run['run_id'] == check['run_id'], 'local run ID mismatch')
        retain.run_record(run, destination / 'run.json', workspace=workspace, source=workspace / 'run.json')

    anchored_path = state / 'hermes-anchored/report.json'
    anchored = retain.json(anchored_path)
    require(anchored['completed'] is True and len(anchored['cases']) == 6 and
            {c['case_id'] for c in anchored['cases']} == CASES, 'anchored six-case probe has not completed')
    require(anchored['wheel_sha256'] == args.wheel_sha256 and
            anchored['candidate_generated_sha256'] == CANDIDATE_SHA, 'anchored identity mismatch')
    retain.copy(anchored_path, retain.evidence / 'hermes-anchored.json', 'compact_real_host_anchored_report')
    for case in anchored['cases']:
        workspace = state / 'hermes-anchored/fixtures' / case['case_id']
        destination = retain.artifacts / 'hermes-anchored' / case['case_id']
        retain.workspace(workspace, destination, ('input.csv', 'input.xlsx', 'input.json', 'output.csv', 'output.xlsx', 'request.json'))
        require(sha(retain.read(workspace / ('input.' + ('json' if case['case_id'] == 'reject_json' else
                'csv' if case['case_id'].startswith('csv_') else 'xlsx')))) == case['source_sha256'],
                'anchored input hash mismatch')
        for identifier in case['run_ids']:
            source = workspace / (identifier + '-run.json')
            run = retain.json(source)
            require(run['run_id'] == identifier, 'anchored run ID mismatch')
            retain.run_record(run, destination / 'runs' / identifier / 'run.json', workspace=workspace, source=source)

    feedback_path = state / 'real-feedback-evolution.json'
    feedback = retain.json(feedback_path)
    require(feedback['wheel_sha256'] == args.wheel_sha256 and
            feedback['generated_cli_candidate_code_sha256'] == CANDIDATE_SHA,
            'real-feedback cycle identity mismatch')
    accepted = {identifier for case in anchored['cases'] if case['expected'] == 'success' and
                case['entry_accepted'] is True and case['validation']['passed'] is True for identifier in case['run_ids']}
    require(set(feedback['accepted_real_hermes_success_run_ids']).issubset(accepted) and
            feedback['proposer_bound_real_csv_run_ids'], 'feedback is not bound to accepted live sessions')
    require(feedback['actual_followup']['actual_executor']['code_sha256'] == CANDIDATE_SHA,
            'followup did not use the recorded generated code')
    retain.copy(feedback_path, retain.evidence / 'real-feedback-evolution.json', 'compact_real_feedback_cycle_report')
    workspace = state / 'real-feedback-followup'
    destination = retain.artifacts / 'real-feedback-followup'
    retain.workspace(workspace, destination, ('orders.csv', 'out.csv', 'request.json'))
    followup = retain.json(workspace / 'run.json')
    require(followup['run_id'] == feedback['actual_followup']['run_id'], 'followup run ID mismatch')
    retain.run_record(followup, destination / 'run.json', workspace=workspace, source=workspace / 'run.json')
    candidate_path = owned / 'codex-candidate-2/workspace/candidate.py'
    require(sha(retain.read(candidate_path)) == CANDIDATE_SHA, 'CLI-generated candidate bytes differ')
    retain.copy(candidate_path, retain.artifacts / 'generated-csv-executor.py', 'earlier_isolated_CLI_generated_candidate')

    sdk_reports = []
    current_sdk_reports = 0
    for source in sorted((owned / 'probes').glob('entry-reset-*/report.json')):
        report = retain.json(source)
        require(report['probe'] == 'same_agent_two_round_entry_reset' and report['formal_experiment'] is False,
                'unexpected SDK probe report')
        current = report.get('package_source_sha256') == package_tree_sha
        current_sdk_reports += int(current)
        identity = source.parent.name
        require(re.fullmatch('entry-reset-[a-f0-9]+', identity) is not None, 'invalid SDK probe directory')
        retain.copy(source, retain.evidence / 'sdk-two-rounds' / identity / 'report.json',
                    'compact_same_agent_report_final_source' if current else 'compact_same_agent_report_historical_source')
        workspace = no_symlinks(source.parent / 'workspace', owned)
        destination = retain.artifacts / 'sdk-two-rounds' / identity
        retain.workspace(workspace, destination, ('first.csv', 'first-out.csv', 'second.csv', 'second-out.csv'))
        for truth in report.get('csv_truth', []):
            require(truth['round'] in {1, 2}, 'invalid SDK truth round')
            name = 'first' if truth['round'] == 1 else 'second'
            require(sha(retain.read(workspace / (name + '.csv'))) == truth['input_sha256'],
                    'SDK source hash differs from compact report')
            if truth.get('output_sha256'):
                require(sha(retain.read(workspace / (name + '-out.csv'))) == truth['output_sha256'],
                        'SDK output hash differs from compact report')
        database = retain.read(source.parent / 'runs.sqlite', database=True)
        for item in report.get('sdk_result', {}).get('rounds', []):
            for identifier in item.get('run_ids', []):
                if identifier:
                    run = readonly_run(database, identifier)
                    retain.run_record(run, destination / 'runs' / identifier / 'run.json', workspace=workspace, database=database)
        sdk_reports.append({'path': str(source.relative_to(owned)), 'matches_final_source': current,
                            'accepted': report.get('accepted'), 'cleanup_complete':
                            report.get('host', {}).get('process_cleanup', {}).get('cleanup_complete')})
    require(current_sdk_reports >= 1, 'no completed SDK probe report for final package source')

    crash_path = owned / 'review/installed-crash-release/installed-crash-evidence.json'
    crash = retain.json(crash_path)
    require(len(crash['phases']) == 3 and {p['phase'] for p in crash['phases']} == PHASES,
            'final crash probe has not completed all three phases')
    crash_root = no_symlinks(Path(crash['root']), owned)
    require(crash_root.parent == crash_path.parent and crash_root.name.startswith('run-'), 'invalid crash fixture root')
    for identity in crash['installed_modules'].values():
        path = no_symlinks(Path(identity['path']), owned)
        require(path.is_relative_to(package) and sha(path.read_bytes()) == identity['sha256'],
                'crash probe installed source identity mismatch')
    retain.copy(crash_path, retain.evidence / 'installed-crash/installed-crash-evidence.json', 'compact_installed_crash_report')
    for phase in crash['phases']:
        workspace = crash_root / phase['phase']
        destination = retain.artifacts / 'installed-crash' / phase['phase']
        retain.workspace(workspace, destination, ('source.csv', 'result.csv', 'request.json'))
        if phase.get('passed') is True:
            require(sha(retain.read(workspace / 'source.csv')) == phase['source_sha256'],
                    'crash source hash differs from compact report')
            require(sha(retain.read(workspace / 'result.csv')) == phase['published_sha256'],
                    'crash output hash differs from compact report')
        identifiers = [phase.get('original_run_id'), phase.get('successor_run_id')]
        if any(identifiers):
            database = retain.read(workspace / 'runs.sqlite', database=True)
            for identifier in identifiers:
                if identifier:
                    run = readonly_run(database, identifier)
                    retain.run_record(run, destination / 'runs' / identifier / 'run.json', workspace=workspace, database=database)

    helpers = ['check_installed_release.py', 'check_anchored_release.py', 'check_real_feedback_cycle.py',
               'probes/entry_reset_parent.py', 'probes/entry_reset_sdk.py',
               'review/installed-crash-release/reproduce_installed_crash.py']
    for name in helpers:
        retain.copy(owned / name, retain.evidence / 'helpers' / Path(name).name, 'byte_identical_reproduction_helper')
    retain.copy(Path(__file__).absolute(), retain.evidence / 'helpers/retain_release_extras.py', 'byte_identical_retention_helper')
    identity = {'scope': 'final release extras; historical 36-session comparison remains separate',
                'wheel_sha256': args.wheel_sha256, 'production_python_source_count': 32,
                'wheel_checkout_installed_sources_identical': True, 'source_files': source_identity,
                'package_tree_sha256': package_tree_sha, 'candidate_generated_sha256': CANDIDATE_SHA,
                'old_snapshot_wheels': OLD_WHEELS,
                'installed_contract_passed': local['passed'], 'anchored_completed': anchored['completed'],
                'anchored_accepted': anchored['accepted'], 'sdk_probes': sdk_reports,
                'crash_passed': crash['passed'], 'retention_model_or_api_calls': 0}
    retain.add(retain.evidence / 'source-identity.json', encoded(identity), 'final_source_identity', generated=True)
    retain.add(retain.evidence / 'fixture-index.json', encoded({'paths_relative_to': 'reports/v1.2.0',
               'original_reports_unmodified': True, 'entries': retain.index}), 'relative_fixture_index', generated=True)
    readme = '''# 最终安装包补充证据的保留范围

本目录保留最终安装包的本地六类契约、真实 Hermes 固定契约六场、同一 Agent 两轮续接、真实已验收宿主反馈绑定的离线升级周期，以及三个真实 owner-crash 阶段。业务文件是人工合成夹具；报告中的 accepted/passed/cleanup 字段原样保留，复制不把失败改成通过。运行/模型调用是否通过应按各原报告读取，文件保留本身不是发布无阻断结论。

最终 wheel SHA256：`''' + args.wheel_sha256 + '''`。32 份生产 Python 源逐字节匹配 wheel、工作树和安装目录；[身份记录](source-identity.json) 保留精确 hash。此前正式36场使用 bee526；b9ab 与 d495 为后续历史快照，977为参数反馈修复前的中间快照，原六场4/6与同源另两场诊断2/2均独立保留。各旧快照不能当作当前包的最终验证。原历史报告、失败启动记录和36场业务数据未覆盖。

[原临时路径到保留文件的索引](fixture-index.json) 相对于 reports/v1.2.0；[业务数据目录](../../artifacts/release-extras/) 不保留 SQLite、Hermes home、raw stdout/stderr、聊天历史、缓存、skills、配置或凭据。RunStore 记录通过 mode=ro/query_only 的 get 读取，跳过建库构造器；数据库和合成业务请求的原路径不改写。临时路径清理后可能失效；改路径重做任务会产生新的计划 hash，不能冒充原执行。

复现辅助源在 helpers/，保持原始字节。先在新的仓库外 owned root/release-venv 安装指定 wheel，再复制 helpers 到 owned root，entry_reset_parent.py 与 entry_reset_sdk.py 放 owned root/probes，crash 脚本放 owned root/review/installed-crash-release。前三个 check 脚本有原 owned root 字面路径，必须在复制件中替换为新 owned root；保留原件勿修改。使用 release-venv/bin/python -I -B。entry_reset_parent.py 显式提供 --owner-root 和 --hermes-root；其 SDK 子脚本只能由父脚本启动。各 check 依次使用新的 installed-contract-release，生成器候选放 codex-candidate-2/workspace/candidate.py 且其 hash 需匹配身份记录。crash 从脚本目录运行。本目录不提供自动模型重跑；真实 Hermes check/SDK 复现会使用操作者自己的已授权本机宿主配置。

保留脚本只在全部探针结束后由 Root 执行，要求 --ready-after-root-probes --wheel-sha256 HASH；它不发起模型/API，不重跑探针，不删除 owned 文件或数据库，不修改生产源/主报告/文档。每份保留文件大小和 SHA256 见 retention-manifest.json；manifest 不自包含自己的 hash。
'''
    retain.add(retain.evidence / 'README.md', readme.encode(), 'retention_and_reproduction_scope', generated=True)
    manifest = {'scope': 'final release extras; copy/export only', 'identity': identity,
                'files': list(retain.files), 'generated_manifest_not_self_hashed': True,
                'source_reports_unmodified': True, 'history_overwritten': False,
                'model_or_api_calls': 0, 'source_database_copy_count': 0,
                'excluded': ['.env', 'auth', 'configuration', 'Hermes home', 'SQLite files',
                             'raw chats', 'stdout/stderr', 'logs', 'transcripts', 'skills', 'cache']}
    retain.add(retain.evidence / 'retention-manifest.json', encoded(manifest), 'per_file_SHA_manifest', generated=True)
    retain.flush()
    print(json.dumps({'retained_files': len(retain.pending), 'wheel_sha256': args.wheel_sha256,
                      'source_count': 32, 'model_or_api_calls': 0, 'database_copies': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
