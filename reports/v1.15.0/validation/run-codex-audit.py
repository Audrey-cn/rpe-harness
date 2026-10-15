#!/usr/bin/env python3
"""One preregistered Codex audit attempt; no model retries or business reruns."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
OWNED = Path('/Users/audrey/项目/缓存/test/rpe-v115-0c69450e782d4a1dacc032e7668c4ead')
WORKTREE = Path('/Users/audrey/.codex/worktrees/rpe-v114/rpe-harness')
REPORT = WORKTREE/'reports/v1.15.0'
EXE = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
sys.path.insert(0, str(WORKTREE/'src'))
CODE_MODE_CLOSED = ('Code Mode is unavailable because code-mode host is disabled. '
    'Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`.')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save_once(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def auth_secrets(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r'(?i)token|secret|api.?key|password', key) and isinstance(item, str) and item:
                found.append(item)
            found.extend(auth_secrets(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(auth_secrets(item))
    return found


def kill_group(process):
    process.poll()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            raise


def main():
    from rpe_harness.experiments.agent_audit import audit_spec, _redactor, _verdict
    registration = json.loads((REPORT/'preregistration.json').read_bytes())
    if registration['independent_audit_budget']['cli_processes'] != 1:
        raise ValueError('one preregistered independent CLI required')
    bundle_raw = (OWNED/'audit-blind-bundle.json').read_bytes()
    bundle = json.loads(bundle_raw)
    if len(bundle.get('cases', [])) != 6:
        raise ValueError('exactly six supplied evidence cases required')
    spec = audit_spec(bundle, profile='structured-v2')
    old = ('Return your final verdict by calling StructuredOutput exactly once. Do not output '
           'analysis, prose, or a text copy of the JSON.')
    contract = spec['system'].replace(old, 'Return only the final JSON object matching the output schema. '
        'Do not call tools, read files, browse, send messages, or modify anything. Use only the supplied bundle as evidence.')
    if contract == spec['system']:
        raise ValueError('Claude-specific output instruction was not replaced')
    payload = encoded({'audit_contract': contract, 'bundle': bundle})
    attempt = OWNED/'codex-audit'
    attempt.mkdir(mode=0o700)
    directories = {'HOME': 'home', 'CODEX_HOME': 'codex-home', 'XDG_CACHE_HOME': 'cache',
                   'XDG_CONFIG_HOME': 'config', 'XDG_DATA_HOME': 'data', 'TMPDIR': 'tmp'}
    for name in directories.values():
        (attempt/name).mkdir(mode=0o700)
    schema_path = attempt/'schema.json'
    last_path = attempt/'last-message.json'
    save_once(schema_path, encoded(spec['schema']))
    save_once(attempt/'input.json', payload)
    env = {key: value for key, value in os.environ.items() if key in {
        'PATH', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LC_ALL', 'LC_CTYPE'}}
    env.update({key: str(attempt/name) for key, name in directories.items()})
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    features = ['shell_tool', 'apps', 'plugins', 'remote_plugin', 'hooks', 'multi_agent',
        'goals', 'sleep_tool', 'view_image', 'image_generation', 'computer_use', 'browser_use',
        'browser_use_external', 'browser_annotation_api', 'in_app_browser', 'workspace_dependencies',
        'skill_search', 'shell_snapshot', 'code_mode_host', 'tool_suggest', 'unbounded_connection_retries']
    configs = {'model_provider': 'openai', 'model_reasoning_effort': 'low', 'model_verbosity': 'low',
        'web_search': 'disabled', 'cli_auth_credentials_store': 'file', 'forced_login_method': 'chatgpt',
        'project_doc_max_bytes': 0, 'developer_instructions': contract}
    args = [EXE, '--no-daemon', '-a', 'never', 'exec', '--ignore-user-config', '--ignore-rules',
        '--ephemeral', '--skip-git-repo-check', '--sandbox', 'read-only', '--model', 'gpt-6.1-sol',
        '--json', '--color', 'never', '--output-schema', str(schema_path),
        '--output-last-message', str(last_path), '-C', str(attempt)]
    for key, value in configs.items():
        args.extend(['-c', key+'='+json.dumps(value)])
    for feature in features:
        args.extend(['--disable', feature])
    args.append('-')
    claim = {'schema_version': 'rpe.v115-codex-audit-claim/v1', 'profile': 'codex-structured-v1',
        'host': 'codex-cli', 'cli_version': subprocess.check_output([EXE, '--version'], text=True).strip(), 'executable': EXE,
        'requested_model': 'gpt-6.1-sol', 'provider': 'openai', 'auth_mode': 'chatgpt',
        'input_sha256': sha(payload), 'bundle_sha256': sha(bundle_raw),
        'schema_sha256': sha(encoded(spec['schema'])), 'contract_sha256': sha(contract.encode()),
        'timeout_seconds': 180, 'cli_process_limit': 1, 'wrapper_retries': 0,
        'max_output_tokens_hard_limit': None, 'cli_max_budget_usd': None,
        'provider_api_limit_verified': False, 'all_builtin_tools_disabled_verified': False,
        'disabled_features': features, 'config_overrides': configs, 'argv': args,
        'runtime_directories': {key: str(attempt/name) for key, name in directories.items()},
        'claimed_at': time.time()}
    save_once(attempt/'claim.json', encoded(claim))
    auth_path = attempt/'codex-home/auth.json'
    source_auth = json.loads(Path('/Users/audrey/.codex/auth.json').read_bytes())
    secrets = auth_secrets(source_auth)
    save_once(auth_path, encoded(source_auth))
    record = {'schema_version': 'rpe.v115-codex-audit/v1', 'profile': claim['profile'],
        'host': claim['host'], 'requested_model': claim['requested_model'], 'status': 'failed',
        'error_code': None, 'exit_code': None, 'verdict': None, 'usage': None,
        'num_turns': None, 'input_sha256': claim['input_sha256'], 'bundle_sha256': claim['bundle_sha256'],
        'schema_sha256': claim['schema_sha256'], 'cli_estimated_cost_usd': None,
        'verified_bill_usd': None, 'physical_provider_requests': None, 'raw_directory': str(attempt)}
    started = time.monotonic()
    process = None
    try:
        with (attempt/'raw.stdout').open('xb') as stdout, (attempt/'raw.stderr').open('xb') as stderr:
            try:
                process = subprocess.Popen(args, cwd=attempt, env=env, stdin=subprocess.PIPE,
                    stdout=stdout, stderr=stderr, start_new_session=True)
                process.communicate(payload, timeout=180)
                record['exit_code'] = process.returncode
            except subprocess.TimeoutExpired:
                record.update(status='timed_out', error_code='timeout')
                kill_group(process)
                process.wait(timeout=5)
                record['exit_code'] = process.returncode
            except OSError:
                record['error_code'] = 'cli_unavailable' if process is None else 'process_io_error'
            finally:
                if process is not None:
                    kill_group(process)
                    process.wait(timeout=5)
    finally:
        if auth_path.exists():
            try:
                secrets.extend(auth_secrets(json.loads(auth_path.read_bytes())))
            except (OSError, ValueError):
                pass
            auth_path.unlink()
    redact = _redactor({f'AUTH_TOKEN_{i}': value for i, value in enumerate(secrets)})
    record['elapsed_seconds'] = time.monotonic()-started
    events = []
    parse_errors = 0
    for line in (attempt/'raw.stdout').read_bytes().splitlines():
        try:
            value = json.loads(line, parse_constant=lambda _: None)
            if isinstance(value, dict):
                events.append(value)
            else:
                parse_errors += 1
        except (ValueError, UnicodeError):
            parse_errors += 1
    completed = [event for event in events if event.get('type') == 'turn.completed']
    record['num_turns'] = len(completed) if events else None
    record['turn_started_count'] = sum(event.get('type') == 'turn.started' for event in events)
    record['event_types'] = {kind: sum(event.get('type') == kind for event in events)
                             for kind in sorted({str(event.get('type')) for event in events})}
    items = [event.get('item') for event in events if isinstance(event.get('item'), dict)]
    harmless = {'agent_message', 'reasoning', 'context_compaction'}
    # CLI startup diagnostics are not tool execution. Only the exact observed
    # fail-closed diagnostic is nonfatal; unknown error items still fail audit.
    diagnostic_items = [item for item in items if item.get('type') == 'error']
    nonfatal = [item for item in diagnostic_items if item.get('message') == CODE_MODE_CLOSED]
    unexpected_errors = [item for item in diagnostic_items if item.get('message') != CODE_MODE_CLOSED]
    tool_items = [item for item in items if item.get('type') not in harmless | {'error'}]
    record['observed_tool_item_count'] = len(tool_items)
    record['observed_tool_item_types'] = sorted({str(item.get('type')) for item in tool_items})
    record['nonfatal_cli_diagnostics'] = [{'type': 'error', 'code': 'code_mode_host_disabled',
        'message': item['message']} for item in nonfatal]
    record['unexpected_cli_error_count'] = len(unexpected_errors)
    record['jsonl_parse_errors'] = parse_errors
    if len(completed) == 1 and isinstance(completed[0].get('usage'), dict):
        record['usage'] = completed[0]['usage']
    record['errors'] = [event.get('message', event.get('error')) for event in events
                        if event.get('type') in {'error', 'turn.failed'}][:16]
    for name in ('stdout', 'stderr'):
        raw = (attempt/('raw.'+name)).read_bytes()
        record[name+'_sha256'] = sha(raw)
        record[name+'_bytes'] = len(raw)
    record['auth_copy_deleted'] = not auth_path.exists()
    verdict = None
    if last_path.is_file() and last_path.stat().st_size <= 1024*1024:
        try:
            verdict = _verdict(json.loads(last_path.read_bytes()),
                {case['evidence_id'] for case in bundle['cases']}, profile='structured-v2')
        except (ValueError, UnicodeError):
            pass
    if record['error_code'] is None:
        if record['exit_code'] != 0:
            record['error_code'] = 'cli_failed'
        elif tool_items or unexpected_errors or parse_errors or len(completed) != 1 or verdict is None:
            record.update(status='invalid_output', error_code='invalid_verdict_or_unexpected_tool_use')
        else:
            record.update(status='completed', verdict=verdict)
    record = redact(record)
    public = {'claim': redact(claim), 'result': record, 'bundle': bundle, 'schema': spec['schema'],
        'runtime': {'cli_version': claim['cli_version'], 'disabled_features': features,
            'all_builtin_tools_disabled_verified': False, 'network_tools_requested_disabled': True,
            'auth_copy_deleted': record['auth_copy_deleted'], 'provider_calls_from_preflight': 0,
            'source_code_changed': False}}
    for name, value in public.items():
        save_once(REPORT/('codex-audit-'+name+'.json'), encoded(value))
    save_once(attempt/'record.json', encoded(record))
    leaked = []
    for path in REPORT.glob('codex-audit-*.json'):
        raw = path.read_bytes()
        if any(value.encode() in raw for value in secrets if value):
            leaked.append(path.name)
    if leaked:
        raise ValueError('credential scan failed for public audit reports')
    print(json.dumps({key: record.get(key) for key in ('status', 'error_code', 'exit_code',
        'elapsed_seconds', 'num_turns', 'usage', 'observed_tool_item_count', 'auth_copy_deleted')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
