"""Run the installed Hermes host with an owned home and bounded process group.

Only Xiaomi's existing API-key provider is supported by this launcher. OAuth
refresh state is deliberately never cloned. The native plugin can also be used
directly with other providers under the host's own configuration.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from contextlib import ExitStack
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from .. import __version__


_SAFE_TEXT = ('kind', 'session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id',
    'tool_name', 'provider', 'model', 'response_model', 'status', 'error_type', 'error_code',
    'turn_exit_reason', 'finish_reason', 'arguments_sha256', 'content_sha256', 'request_tools_sha256',
    'target_path_sha256', 'request_tool_choice', 'request_thinking_type',
    'state_id', 'catalog_sha256', 'request_messages_sha256')


def _safe_diagnostics(value, secret):
    """Only bounded public metadata may cross a failed observation boundary."""
    def safe_text(item):
        return item[:256] if isinstance(item, str) and (not secret or secret not in item) else None
    def number(item):
        return (type(item) is int and abs(item) <= 2**63 - 1) or (type(item) is float and math.isfinite(item))
    def rows(items):
        result = []
        for row in items[:256] if isinstance(items, list) else []:
            if not isinstance(row, dict):
                continue
            saved = {key: safe_text(row[key]) for key in _SAFE_TEXT if safe_text(row.get(key)) is not None}
            saved.update({key: row[key] for key in ('duration_ms','api_duration','bytes_written','tool_exit_code','status_code','request_max_completion_tokens','request_messages_bytes')
                          if number(row.get(key))})
            saved.update({key: row[key] for key in ('completed','failed','interrupted','verified','request_parallel_tool_calls') if type(row.get(key)) is bool})
            if isinstance(row.get('request_tool_names'), list):
                saved['request_tool_names'] = [safe_text(name) for name in row['request_tool_names'][:32]
                    if safe_text(name) is not None]
            calls = row.get('model_tool_calls')
            if isinstance(calls, list):
                saved['model_tool_calls'] = [{key: safe_text(call[key]) for key in ('tool_call_id','tool_name')
                    if safe_text(call.get(key)) is not None} for call in calls[:32] if isinstance(call, dict)]
            result.append(saved)
        return result
    result = {'completed': False, 'session_id': safe_text(value.get('session_id')),
              'events': rows(value.get('events')), 'file_write_observations': rows(value.get('file_write_observations')),
              'usage': None, 'model_identity': None, 'process_cleanup': None}
    for key in ('exit_code','observer_errors','elapsed_seconds','max_turns','max_task_attempts'):
        if number(value.get(key)):
            result[key] = value[key]
    for key in ('timed_out','contract_prompt_injected'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if isinstance(value.get('session_root'), str) and len(value['session_root']) <= 4096 and (not secret or secret not in value['session_root']):
        result['session_root'] = value['session_root']
    for key in ('prompt_sha256','original_prompt_sha256','input_sha256','entry_mode','catalog_snapshot_id','public_contract_sha256'):
        if safe_text(value.get(key)) is not None:
            result[key] = safe_text(value[key])
    usage = value.get('usage')
    if isinstance(usage, dict):
        allowed = {'api_requests','missing_observations','requests_with_usage','api_errors','coverage_complete',
            'known_total_tokens','prompt_tokens','output_tokens','input_tokens','total_tokens',
            'cache_read_tokens','cache_write_tokens','reasoning_tokens'}
        result['usage'] = {key: item for key,item in usage.items() if key in allowed
            and (item is None or number(item) or type(item) is bool)}
        # Finalization failure means overall observation coverage is unproven.
        # Already observed costs remain known; unknown completion never becomes zero.
        result['usage'].update(coverage_complete=False, total_tokens=None)
    identity = value.get('model_identity')
    if isinstance(identity, dict):
        result['model_identity'] = {key:safe_text(identity[key]) for key in ('provider','requested_model')
                                   if safe_text(identity.get(key)) is not None}
        models = identity.get('response_models')
        result['model_identity']['response_models'] = [safe_text(item) for item in models[:16]
            if safe_text(item) is not None] if isinstance(models, list) else []
    cleanup = value.get('process_cleanup')
    if isinstance(cleanup, dict):
        saved = {key:cleanup[key] for key in ('cleanup_complete','external_executor_cleanup_complete')
                 if type(cleanup.get(key)) is bool}
        saved.update({key:cleanup[key] for key in ('tracked_descendants','signaled_descendants','external_executor_tracked_children')
                      if type(cleanup.get(key)) is int and 0 <= cleanup[key] <= 2**63 - 1})
        for key in ('remaining_descendants','external_executor_remaining_children'):
            items = cleanup.get(key)
            saved[key] = [item for item in items[:64] if type(item) is int and 1 < item <= 2**31 - 1] if isinstance(items,list) else None
        errors = cleanup.get('errors')
        saved['errors'] = [{key:safe_text(row[key]) for key in ('stage','error_type') if safe_text(row.get(key)) is not None}
            for row in errors[:32] if isinstance(row,dict)] if isinstance(errors,list) else None
        result['process_cleanup'] = saved
    return result


class SessionObservationError(ValueError):
    """A failed postlaunch gate with safe, already observed session facts."""
    def __init__(self, diagnostics, *, secret='', code='postlaunch_diagnostics_failed', error_type=None):
        super().__init__('host observation finalization failed')
        self.diagnostics = _safe_diagnostics(diagnostics, secret)
        self.code = code
        self.error_type = error_type


def aggregate_usage(events):
    requests = [e for e in events if e.get('kind') in {'post_api_request', 'api_request_error'}]
    outcomes = {e.get('api_request_id') for e in requests if e.get('api_request_id')}
    missing = {e['api_request_id'] for e in events if e.get('kind') == 'pre_api_request'
               and e.get('api_request_id') and e['api_request_id'] not in outcomes}
    requests += [{'kind': 'missing_api_outcome', 'api_request_id': request_id, 'usage': None}
                 for request_id in missing]
    usages = [e.get('usage') for e in requests]

    def reported(u, markers):
        return isinstance(u, dict) and any(p in markers for p in u.get('present_fields', []))

    def counter(value):
        return value if type(value) is int and value >= 0 else None

    def raw_counter(u, paths):
        if not isinstance(u, dict):
            return None
        for path in paths:
            if not reported(u, {path}):
                continue
            value = u.get('raw_provider_usage')
            for part in path.split('.'):
                value = value.get(part) if isinstance(value, dict) else None
            if counter(value) is not None:
                return value
        return None

    prompt_paths = ('prompt_tokens', 'promptTokenCount', 'input_tokens')
    output_paths = ('completion_tokens', 'output_tokens', 'candidatesTokenCount')
    total_paths = ('total_tokens', 'totalTokenCount')
    bucket_paths = {
        'cache_read_tokens': ('prompt_tokens_details.cached_tokens', 'input_tokens_details.cached_tokens',
                             'cache_read_input_tokens', 'prompt_cache_hit_tokens', 'cached_tokens',
                             'cache_read_tokens', 'cached_input_tokens', 'cachedContentTokenCount'),
        'cache_write_tokens': ('prompt_tokens_details.cache_write_tokens',
                              'prompt_tokens_details.cache_creation_input_tokens',
                              'input_tokens_details.cache_write_tokens',
                              'input_tokens_details.cache_creation_tokens',
                              'cache_creation_input_tokens', 'cache_write_tokens', 'cache_creation_tokens'),
        'reasoning_tokens': ('completion_tokens_details.reasoning_tokens',
                             'output_tokens_details.reasoning_tokens', 'reasoning_tokens', 'thoughtsTokenCount'),
    }

    def core_value(u, field, paths):
        if not reported(u, paths):
            return None
        # Anthropic input is uncached while canonical prompt includes its cache
        # buckets; use the host's prompt bucket for that alias.
        raw = raw_counter(u, paths if field != 'prompt_tokens' else paths[:2])
        return raw if raw is not None else counter(u.get(field))

    def aligned_bucket(u, field, paths):
        if not reported(u, paths):
            return None
        normalized = counter(u.get(field))
        raw = raw_counter(u, paths)
        if isinstance(u.get('raw_provider_usage'), dict) and raw != normalized:
            return None
        return normalized

    values = []
    for u in usages:
        row = {field: None for field in ('prompt_tokens', 'output_tokens', 'input_tokens',
                                         'total_tokens', *bucket_paths)}
        if isinstance(u, dict):
            row['prompt_tokens'] = core_value(u, 'prompt_tokens', prompt_paths)
            row['output_tokens'] = core_value(u, 'output_tokens', output_paths)
            row['total_tokens'] = raw_counter(u, total_paths)
            if (row['total_tokens'] is None and reported(u, total_paths)
                    and not isinstance(u.get('raw_provider_usage'), dict)):
                row['total_tokens'] = counter(u.get('total_tokens'))
            if (row['total_tokens'] is None and row['prompt_tokens'] is not None
                    and row['output_tokens'] is not None):
                row['total_tokens'] = row['prompt_tokens'] + row['output_tokens']
            for field, paths in bucket_paths.items():
                row[field] = aligned_bucket(u, field, paths)
            input_markers = ('input_tokens', 'prompt_cache_miss_tokens', *bucket_paths['cache_read_tokens'])
            if reported(u, input_markers):
                row['input_tokens'] = counter(u.get('input_tokens'))
                raw_miss = raw_counter(u, ('prompt_cache_miss_tokens',))
                if (reported(u, {'prompt_cache_miss_tokens'})
                        and raw_miss != row['input_tokens']):
                    row['input_tokens'] = None
                if any(reported(u, bucket_paths[field]) and row[field] is None
                       for field in ('cache_read_tokens', 'cache_write_tokens')):
                    row['input_tokens'] = None
        values.append(row)

    core_markers = {'prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens',
                    'output_tokens', 'promptTokenCount', 'candidatesTokenCount', 'totalTokenCount'}
    complete = bool(requests) and all(row['total_tokens'] is not None for row in values)
    result = {'api_requests': len(requests),
              'missing_observations': len(missing),
              'requests_with_usage': sum(reported(u, core_markers) for u in usages),
              'api_errors': sum(e.get('kind') == 'api_request_error' for e in requests),
              'coverage_complete': complete,
              'known_total_tokens': sum(row['total_tokens'] for row in values
                                        if row['total_tokens'] is not None)}
    for field in ('prompt_tokens', 'output_tokens', 'input_tokens', 'total_tokens', *bucket_paths):
        result[field] = (sum(row[field] for row in values)
                         if values and all(row[field] is not None for row in values) else None)
    return result


def sandbox_profile(write_roots, real_home, install_root=None, database=None, read_roots=(),
                    native_import_root=None, owner_read_scope=None, deny_read_roots=(),
                    protected_write_roots=(), protected_read_roots=()):
    """macOS denies take precedence over allows; scope both writes and signals."""
    def quote(value):
        return json.dumps(str(value), ensure_ascii=False)
    roots = ' '.join('(subpath '+quote(Path(p).resolve())+')' for p in write_roots)
    if database:
        database = Path(database).resolve()
        roots += ' ' + ' '.join('(literal '+quote(str(database)+suffix)+')'
                                for suffix in ['', '-wal', '-shm', '-journal'])
        roots += ' (subpath '+quote(database.parent / ('.'+database.name+'.leases'))+')'
    real_home = Path(real_home).resolve()
    credentials = '\n'.join('(deny file-read* (literal '+quote(real_home / name)+'))'
                            for name in ['.env', 'auth.json', 'config.yaml'])
    private_state = ''
    if install_root:
        allowed = ' '.join('(subpath '+quote(Path(p).resolve())+')'
                           for p in [install_root, *read_roots])
        private_state = ('(deny file-read* (require-all (subpath '+quote(real_home)+') '
                         '(require-not (require-any '+allowed+'))))')
    native_scope = ''
    if native_import_root:
        import_root = Path(native_import_root).resolve()
        package = import_root / 'rpe_harness'
        repository = import_root.parent if import_root.name == 'src' else package
        minimal = [import_root, package, package / 'integrations',
                   package / '__init__.py', package / 'integrations/__init__.py',
                   package / 'integrations/hermes.py', package / 'integrations/hermes_bootstrap.py',
                   package / 'integrations/hermes_native.py', package / 'integrations/hermes_native_process.py',
                   package / 'integrations/hermes_review_finish.py',
                   package / 'integrations/hermes_request_budget.py', package / 'integrations/hermes_native_finish.py']
        allowed = ' '.join('(literal '+quote(p)+')' for p in minimal)
        native_scope = ('(deny file-read-data (require-all (subpath '+quote(repository)+') '
                        '(require-not (require-any '+allowed+'))))\n')
    denied_scope = ''
    for denied_root in deny_read_roots:
        exceptions = ''
        if native_import_root:
            exceptions = ' (require-not (require-any '+allowed+'))'
        denied_scope += ('(deny file-read-data (require-all (subpath '+
                         quote(Path(denied_root).resolve())+')'+exceptions+'))\n')
    owned_scope = ''
    if owner_read_scope:
        owner, allowed_paths = owner_read_scope
        owner = Path(owner).resolve()
        allowed_paths = list(allowed_paths)
        if database:
            allowed_paths += [Path(str(database)+suffix) for suffix in ['-wal', '-shm', '-journal']]
            allowed_paths += [database.parent / ('.'+database.name+'.leases')]
        allowed = ' '.join('(subpath '+quote(Path(p).resolve())+')' for p in allowed_paths)
        ancestors = set()
        for item in allowed_paths:
            path = Path(item).resolve()
            cursor = path if path.is_dir() else path.parent
            while cursor.is_relative_to(owner):
                ancestors.add(cursor)
                if cursor==owner:break
                cursor=cursor.parent
        allowed += ' '+' '.join('(literal '+quote(p)+')' for p in sorted(ancestors))
        owned_scope = ('(deny file-read-data (require-all (subpath '+quote(Path(owner).resolve())+') '
                       '(require-not (require-any '+allowed+'))))\n')
    protected_scope = '\n'.join('(deny file-write* (subpath '+quote(Path(p).resolve())+'))'
                               for p in protected_write_roots)
    protected_ancestors = set()
    write_paths = [Path(p).resolve() for p in write_roots]
    for item in protected_write_roots:
        cursor=Path(item).resolve().parent
        while any(cursor.is_relative_to(root) for root in write_paths):
            protected_ancestors.add(cursor)
            cursor=cursor.parent
    protected_scope += '\n'+'\n'.join('(deny file-write* (literal '+quote(p)+'))'
                                      for p in sorted(protected_ancestors))
    return ('(version 1)\n(allow default)\n'
            '(deny file-write* (require-not (require-any '+roots+' (literal "/dev/null"))))\n'
            '(deny signal (require-not (target same-sandbox)))\n'+credentials+'\n'+private_state+'\n'
            +native_scope+owned_scope+denied_scope+protected_scope+'\n'+
            '\n'.join('(deny file-read* (subpath '+quote(Path(p).resolve())+'))'
                      for p in protected_read_roots)+'\n')


def _installed_python(hermes_root):
    code = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
            'from pm.environments import project_python; print(project_python(Path(sys.argv[1])))')
    process = subprocess.run([str(hermes_root / '.venv/bin/python'), '-B', '-c', code, str(hermes_root)],
                             capture_output=True, text=True, timeout=15)
    path = Path(process.stdout.strip())
    if process.returncode or not path.is_absolute() or not path.is_file():
        raise ValueError('cannot locate installed Hermes dependency environment')
    return path


def _read_host_settings(hermes_root, host_python=None):
    # Capture in memory; this subprocess output contains the key and must never
    # be forwarded to diagnostics, prompts, reports or terminal output.
    code = '''import json,pathlib,sys
sys.path.insert(0,sys.argv[1])
import hermes_yaml as yaml
from dotenv import dotenv_values
p=pathlib.Path.home()/'.hermes'
c=yaml.safe_load((p/'config.yaml').read_text()) or {}
e=dotenv_values(p/'.env',interpolate=False)
m=c.get('model',{})
print(json.dumps({'provider':m.get('provider'),'model':m.get('default'),
'base_url':m.get('base_url') or e.get('XIAOMI_BASE_URL'), 'api_key':e.get('XIAOMI_API_KEY')}))
'''
    process = subprocess.run([str(host_python or _installed_python(hermes_root)), '-B', '-c', code, str(hermes_root)],
                             capture_output=True, text=True, timeout=15)
    if process.returncode:
        raise ValueError('cannot read installed Hermes API-key settings')
    try:
        return json.loads(process.stdout)
    except ValueError as exc:
        raise ValueError('invalid installed Hermes settings') from exc


def _validated_host_settings(settings, *, request_budget=None):
    """Validate the existing isolated Xiaomi profile without exposing its values."""
    if type(settings) is not dict:
        raise ValueError('isolated host settings must be an object')
    if settings.get('provider') != 'xiaomi':
        raise ValueError('isolated launcher currently requires xiaomi API-key provider')
    if (set(settings) != {'model', 'provider', 'base_url', 'api_key'}
            or not all(type(settings.get(key)) is str and settings[key]
                       for key in ('model', 'provider', 'base_url', 'api_key'))
            or len(json.dumps(settings, ensure_ascii=False).encode()) > 16384):
        raise ValueError('host settings require exactly four bounded nonempty string fields')
    from urllib.parse import urlsplit
    try:
        url = urlsplit(settings['base_url'])
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError('invalid endpoint')
    except ValueError:
        raise ValueError('provider endpoint must be a plain HTTPS URL') from None
    if any('\n' in settings[key] or '\r' in settings[key] for key in ('api_key', 'base_url')):
        raise ValueError('invalid credential assignment')
    if request_budget is not None:
        from .hermes_request_budget import validate_request_budget_profile
        request_budget = validate_request_budget_profile(request_budget)
        if request_budget['model'] != settings['model']:
            raise ValueError('request budget model differs from installed host settings')
    return dict(settings)


def _load_host_settings(path):
    """Read a caller-owned private file in the parent only; never log its content."""
    from ..path_binding import read_regular_file
    def unique_fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate host settings field')
            value[key] = item
        return value
    try:
        raw = read_regular_file(Path(path).absolute(), max_bytes=16384)
        return _validated_host_settings(json.loads(raw, object_pairs_hook=unique_fields))
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('invalid explicit host settings file') from None


def native_execution_contract(helper_python, stdout_name=None):
    """A runtime delivery protocol, separate from the approved JSON business task."""
    from .hermes_clean import CLEAN_SOURCE_CONTRACT
    contract = CLEAN_SOURCE_CONTRACT.replace('{helper_python}', str(helper_python))
    if stdout_name is None:
        return contract
    if (type(stdout_name) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', stdout_name)
            or stdout_name in ('.', '..')):
        raise ValueError('native stdout name must be a bounded workspace basename')
    return contract + ('\nRuntime delivery evidence protocol (no additional business authority): '
        'For xlsx.inspect, load the real xlsx skill through native skill_view before execution. '
        'If archived, restore it through the installed Hermes curator CLI, then read it. '
        'Use the original helper with the approved input/mode/sheet; do not rewrite the helper. '
        'In ONE terminal tool call, redirect helper stdout to the fresh workspace file '
        + json.dumps(stdout_name) + ', check successful helper exit, then COPY its unchanged bytes '
        'to the approved final output. Keep the stdout file unchanged until verification; '
        'do not rename, delete, link or overwrite it. Both files must be absent before that call. '
        'This protocol binds observed stdout bytes to the final file within one tool call; '
        'it does not claim an exclusive writer or authorize any other business action.')


def prepare_home(session_root, workspace, db_path, *, tool_enabled, settings, entry_mode='optional',
                 preserve_native_tool_search=False, public_contract=None, request_budget=None,
                 native_stdout_name=None):
    if entry_mode not in ('optional','strict','skills','address','clean','review','producer') or (entry_mode in ('strict','skills','address','clean','review') and not tool_enabled):
        raise ValueError('strict entry requires the Harness tool')
    if public_contract not in (None, 'clean', 'review'):
        raise ValueError('unknown public contract')
    if entry_mode == 'producer' and tool_enabled:
        raise ValueError('producer entry requires the Harness tool disabled')
    settings = _validated_host_settings(settings, request_budget=request_budget)
    session_root = Path(session_root)
    session_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    home = session_root / 'home'
    home.mkdir(mode=0o700)
    credential_path = home / '.env'
    fd = os.open(credential_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write('XIAOMI_API_KEY='+json.dumps(settings['api_key'])+'\n')
        stream.write('XIAOMI_BASE_URL='+json.dumps(settings['base_url'])+'\n')
    config = {
        'model': {'default': settings['model'], 'provider': 'xiaomi', 'base_url': settings['base_url']},
        'agent': {'max_turns': 12, 'run_budget_seconds': 180, 'api_max_retries': 1,
                  'auto_recovery_cycles': 0, 'environment_probe': False},
        'terminal': {'backend': 'local', 'cwd': str(workspace), 'temp_dir': str(session_root / 'terminal-tmp')},
        'platform_toolsets': {'cli': (['rpe_producer_only'] if entry_mode=='producer' else
                                     ['rpe_harness','skills'] if entry_mode=='skills' else
                                     ['rpe_harness'] if entry_mode in ('strict','address','clean','review') else
                                     ['terminal','file']+(['rpe_harness'] if tool_enabled else []))},
        'memory': {'memory_enabled': False, 'user_profile_enabled': False, 'provider': ''},
        'skills': {'external_dirs': [], 'project_discovery': False, 'auto_load': []},
        'curator': {'enabled': False}, 'compression': {'enabled': False},
        'auxiliary': {'title_generation': {'enabled': False, 'model_upgrade_enabled': False},
                      'background_review': {'enabled': False}},
        'plugins': {'enabled': ['rpe-harness'], 'auto_update_check_hours': 0, 'auto_apply': False},
        'mcp_servers': {}, 'hooks': {}, 'fallback_providers': [], 'fallback_model': [],
        'nous': {'guest': False}, 'telemetry': {'shared_metrics': {'enabled': False}},
    }
    if request_budget is not None:
        config['providers'] = {'xiaomi': {key: request_budget[key] for key in
            ('request_timeout_seconds', 'stale_timeout_seconds')}}
    # Address is a fixed two-tool strategy. Hermes snapshots valid_tool_names
    # before request middleware; its plugin deferral must be disabled here so
    # the native validator admits both registered tools. The native control arm
    # continues to use its existing search/progressive-disclosure configuration.
    if entry_mode in ('address','clean','review') or (entry_mode in ('strict','skills','producer') and not preserve_native_tool_search):
        config['tools'] = {'tool_search':{'enabled':'off'}}
    if public_contract == 'clean' or entry_mode == 'clean':
        config['agent']['system_prompt'] = native_execution_contract(sys.executable, native_stdout_name)
    elif native_stdout_name is not None:
        raise ValueError('native stdout protocol requires the clean execution contract')
    if public_contract == 'review' or entry_mode == 'review':
        from .hermes_review import REVIEW_CONTRACT
        config['agent']['system_prompt'] = REVIEW_CONTRACT
        # These are caller-authorized isolated review sessions. Native controls
        # need their normal program tools; the task's confirmation is separate.
        config['approvals'] = {'single_query_mode': 'approve'}
    (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
    plugin = home / 'plugins/rpe-harness'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text(f'name: rpe-harness\nversion: {__version__}\ndescription: Durable RPE task and host observations\n', encoding='utf-8')
    (plugin / '__init__.py').write_text(
        'import os, sys\nsys.path.insert(0, os.environ["RPE_HARNESS_IMPORT_ROOT"])\n'
        'from rpe_harness.integrations.hermes import register\n', encoding='utf-8')
    return home


def _stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _process_table():
    """Read PID, parent and kernel-reported start time, without command/env contents."""
    output = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,lstart='],
                            capture_output=True, text=True, timeout=5, check=True).stdout
    table = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 7 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), ' '.join(parts[2:]))
    return table


def _capture_descendants(pid, start_time, owned):
    table = _process_table()
    if table.get(pid, (None, None))[1] != start_time:
        return
    parents = {pid}
    while True:
        found = {child for child, (parent, _) in table.items() if parent in parents}
        new = found - parents
        for child in new:
            owned[child] = table[child][1]
        if not new:
            break
        parents.update(new)


def _stop_descendants(owned):
    signaled = []
    for sig in [signal.SIGTERM, signal.SIGKILL]:
        table = _process_table()
        survivors = [pid for pid, identity in owned.items()
                     if table.get(pid, (None, None))[1] == identity and pid != os.getpid()]
        if not survivors:
            break
        for pid in survivors:
            try:
                os.kill(pid, sig)
                if pid not in signaled:
                    signaled.append(pid)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            time.sleep(0.3)
    return signaled


def _tagged_processes(token):
    # Inspect in memory only. Never persist or expose other processes' command
    # lines/environments. The unique inherited tag covers short-lived parents
    # whose detached child escaped the periodic ancestry snapshot.
    output = subprocess.run(['/bin/ps', '-Eww', '-U', str(os.getuid()), '-o', 'pid=,command='],
                            capture_output=True, text=True, errors='replace', timeout=5, check=True).stdout
    marker = 'RPE_HERMES_SESSION_TOKEN='+token
    tagged = set()
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and marker in parts[1]:
            tagged.add(int(parts[0]))
    table = _process_table()
    return {pid: table[pid][1] for pid in tagged if pid in table and pid != os.getpid()}


def _remaining_descendants(owned):
    deadline = time.monotonic()+3
    while True:
        table = _process_table()
        remaining = [pid for pid, identity in owned.items()
                     if table.get(pid, (None, None))[1] == identity and pid != os.getpid()]
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.1)


def _registered_processes(path, host_pid):
    owned = {}
    if not path.exists():
        return owned
    for line in path.read_text(encoding='utf-8').splitlines():
        record = json.loads(line)
        if (not isinstance(record, dict) or type(record.get('pid')) is not int or record['pid'] <= 1
                or record.get('parent_pid') != host_pid
                or not isinstance(record.get('lstart'), str) or not record['lstart']):
            raise ValueError('invalid owned process registration')
        owned[record['pid']] = record['lstart']
    return owned


def _file_write_observations(records, workspace):
    """Extract verified write facts, discarding tool result prose and arguments."""
    starts = {}
    for record in records:
        if not isinstance(record, dict) or record.get('type') != 'tool_use' or record.get('name') != 'write_file':
            continue
        identifier = record.get('tool_call_id')
        if isinstance(identifier, str) and identifier:
            starts.setdefault(identifier, []).append(record)
    result = []
    for record in records:
        if (not isinstance(record, dict) or record.get('type') != 'tool_result'
                or record.get('name') != 'write_file' or record.get('is_error') is not False):
            continue
        identifier = record.get('tool_call_id')
        matches = starts.get(identifier, []) if isinstance(identifier, str) else []
        if len(matches) != 1:
            continue
        args = matches[0].get('input')
        if (not isinstance(args, dict) or not isinstance(args.get('path'), str)
                or len(args['path']) > 4096 or not isinstance(args.get('content'), str)):
            continue
        try:
            output = json.loads(record.get('output', ''))
            raw = args['content'].encode('utf-8')
            if (not isinstance(output, dict) or output.get('verified') is not True or output.get('error')
                    or type(output.get('bytes_written')) is not int or output['bytes_written'] != len(raw)):
                continue
            path = Path(args['path'])
            path = path if path.is_absolute() else Path(workspace) / path
            observed = {'tool_call_id': identifier[:256], 'verified': True, 'bytes_written': len(raw),
                'content_sha256': hashlib.sha256(raw).hexdigest(),
                'arguments_sha256': hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False,
                    allow_nan=False, separators=(',', ':')).encode()).hexdigest(),
                'target_path_sha256': hashlib.sha256(str(path.resolve()).encode()).hexdigest()}
        except (ValueError, TypeError, OSError, RuntimeError):
            continue
        if type(record.get('duration_ms')) is int and record['duration_ms'] >= 0:
            observed['duration_ms'] = record['duration_ms']
        result.append(observed)
        if len(result) == 256:
            break
    return result


def run_hermes_session(prompt, workspace, db_path, *, tool_enabled, owner_root,
                       timeout=180, hermes_root=None, entry_mode='optional',
                       required_request=None, max_task_attempts=3, catalog_root=None,
                       skill_snapshot_id=None, context_sources=None,
                       read_isolation_root=None, deny_read_roots=(), max_turns=12,
                       preserve_native_tool_search=False, public_contract=None, native_evidence=None,
                       native_execution_evidence=None, request_budget=None, native_finish_enabled=False,
                       review_profile=None, host_settings=None):
    if type(max_turns) is not int or not 1 <= max_turns <= 12:
        raise ValueError('max_turns must be an integer between 1 and 12')
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').exists():
        raise ValueError('isolated host launcher requires macOS sandbox-exec')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 1_048_576:
        raise ValueError('prompt must be nonempty and at most 1 MiB')
    if not 1 <= timeout <= 1800:
        raise ValueError('timeout must be between 1 and 1800 seconds')
    if entry_mode not in ('optional','strict','skills','address','clean','review','producer') or (entry_mode in ('strict','skills','address','clean','review') and not tool_enabled):
        raise ValueError('strict entry requires the Harness tool')
    if public_contract not in (None, 'clean', 'review'):
        raise ValueError('unknown public contract')
    if review_profile not in (None, 'values-v1') or (review_profile is not None and entry_mode != 'review'):
        raise ValueError('review profile requires the public review entry')
    artifact_mode = type(native_evidence) is dict and 'artifact_name' in native_evidence
    if native_evidence is not None and (tool_enabled or public_contract != ('clean' if artifact_mode else 'review')):
        raise ValueError('native evidence requires the corresponding native review or clean session')
    if type(native_finish_enabled) is not bool or (native_finish_enabled and (tool_enabled
            or (native_evidence is None and native_execution_evidence is None))):
        raise ValueError('native finish requires opted-in native observation')
    if request_budget is not None:
        if entry_mode == 'producer':
            raise ValueError('producer does not support the frozen request budget')
        from .hermes_request_budget import validate_request_budget_profile
        request_budget = validate_request_budget_profile(request_budget)
    if host_settings is not None:
        host_settings = _validated_host_settings(host_settings, request_budget=request_budget)
    if native_execution_evidence is not None and (tool_enabled or public_contract != 'clean'):
        raise ValueError('native execution evidence requires a native clean execution session')
    if native_execution_evidence is not None and type(native_execution_evidence) is not dict:
        raise ValueError('native execution options must be an object')
    native_stdout_name = (native_execution_evidence or {}).get('stdout_name')
    if entry_mode == 'clean' and (required_request is not None or context_sources not in (None, [])
                                  or len(prompt.encode()) > 65536):
        raise ValueError(entry_mode + ' requires only the exact current source, at most 64 KiB; no hidden contract or context')
    if entry_mode == 'review':
        if required_request is not None or len(prompt.encode()) > 65536:
            raise ValueError('review requires the exact current source; no hidden contract')
        from ..review_context import validate_sources
        try:
            context_sources = validate_sources([] if context_sources is None else context_sources)
        except (TypeError, ValueError) as error:
            raise ValueError('review context source validation failed: ' + str(error)) from error
    if entry_mode == 'producer' and (tool_enabled or catalog_root is not None or skill_snapshot_id is not None
                                    or context_sources is not None or required_request is not None):
        raise ValueError('producer entry requires no Harness tool, fixed task, or skill catalog context')
    if entry_mode == 'skills' and catalog_root is None:
        raise ValueError('skills entry requires an owned skill catalog')
    if entry_mode == 'address' and context_sources not in (None, []):
        raise ValueError('address does not consume context_sources; provide explicit task facts or '
                         'trusted address_extension source_facts instead')
    if type(max_task_attempts) is not int or not 1 <= max_task_attempts <= 20:
        raise ValueError('task attempt limit must be between 1 and 20')
    owner_root, workspace = Path(owner_root).resolve(), Path(workspace).resolve()
    if not workspace.is_dir():
        raise ValueError('workspace must be an existing directory')
    original_prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
    public_contract_sha = None
    if public_contract == 'clean' or entry_mode == 'clean':
        public_contract_sha = hashlib.sha256(native_execution_contract(
            sys.executable, native_stdout_name).encode()).hexdigest()
    if public_contract == 'review' or entry_mode == 'review':
        from .hermes_review import REVIEW_CONTRACT
        contract = REVIEW_CONTRACT
        if review_profile == 'values-v1':
            from ..review_values import VALUE_EVIDENCE_CONTRACT
            contract += '\n' + VALUE_EVIDENCE_CONTRACT
        public_contract_sha = hashlib.sha256(contract.encode()).hexdigest()
    fixed_request, fixed_input_sha = None, None
    if required_request is not None:
        from .hermes import _prepare_request
        from .hermes_gate import request_input_sha
        fixed_request = _prepare_request({'request':required_request},workspace,None)
        fixed_input_sha = request_input_sha(fixed_request,workspace)
        prompt += ('\nThe following fixed task contract is authoritative. Extract its business '
                   'parameters for the task tool; do not change its execution parameters.\n'+
                   json.dumps(fixed_request,ensure_ascii=False,allow_nan=False))
        if len(prompt.encode()) > 1_048_576:
            raise ValueError('prompt with fixed contract exceeds 1 MiB')
    owner_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    hermes_root = Path(hermes_root or Path.home() / '.hermes/hermes-agent').resolve()
    host_python = _installed_python(hermes_root)
    settings = (_read_host_settings(hermes_root, host_python)
                if host_settings is None else host_settings)
    session_root = owner_root / ('session-'+uuid.uuid4().hex)
    home = prepare_home(session_root, workspace, Path(db_path).resolve(),
                        tool_enabled=tool_enabled, settings=settings,entry_mode=entry_mode,
                        preserve_native_tool_search=preserve_native_tool_search, public_contract=public_contract,
                        request_budget=request_budget,
                        **({'native_stdout_name': native_stdout_name} if native_stdout_name is not None else {}))
    request_budget_path = None
    if request_budget is not None:
        request_budget_path = session_root/'request-budget.json'
        request_budget_path.write_text(json.dumps(request_budget, sort_keys=True, allow_nan=False))
        os.chmod(request_budget_path, 0o600)
    skill_config_path = None
    snapshot = None
    if catalog_root is not None:
        from ..skill_catalog import SkillCatalog
        catalog = SkillCatalog(db_path, catalog_root)
        snapshot = catalog.materialize(home / 'skills', snapshot_id=skill_snapshot_id)
        if entry_mode == 'review' or public_contract == 'review':
            from .review_skills import prepare_review_skills
            snapshot = prepare_review_skills(snapshot)
        (home / '.no-bundled-skills').touch(mode=0o600)
        config = {'catalog_root':str(Path(catalog_root).resolve()),
                  'snapshot_id':snapshot['snapshot_id'],
                  'materialized_root':snapshot['materialized_path'],
                  'entries':[{key:entry[key] for key in ('skill_id','load_name','skill_path',
                     'skill_sha256','script_path','script_sha256') if key in entry}
                     for entry in snapshot['entries']]}
        for projected, entry in zip(config['entries'], snapshot['entries']):
            if entry.get('native_skill_path'):
                projected.update(skill_path=entry['native_skill_path'], load_name=entry['native_load_name'])
        skill_config_path = session_root / 'skill-config.json'
        config_text=json.dumps(config,ensure_ascii=False)
        if len(config_text.encode())>8*1_048_576:
            raise ValueError('materialized skill configuration exceeds 8 MiB')
        skill_config_path.write_text(config_text,encoding='utf-8')
        os.chmod(skill_config_path,0o600)
        if not tool_enabled and not preserve_native_tool_search:
            settings_path = home / 'config.yaml'
            native_config = json.loads(settings_path.read_text())
            native_config.setdefault('tools',{}).setdefault('tool_search',{})['enabled'] = 'off'
            settings_path.write_text(json.dumps(native_config,ensure_ascii=False))
        if entry_mode == 'review':
            settings_path = home / 'config.yaml'
            review_settings = json.loads(settings_path.read_text())
            review_settings['platform_toolsets']['cli'] = ['rpe_harness', 'skills']
            settings_path.write_text(json.dumps(review_settings, ensure_ascii=False))
    address_config_path = None
    if entry_mode == 'address':
        from .hermes_address import _catalog, _model_catalog_entries
        unified = _catalog(db_path, catalog_root, snapshot['snapshot_id'] if snapshot else None)
        address_config_path = session_root / 'address-config.json'
        address_config_path.write_text(json.dumps({'catalog_sha256':unified.snapshot_id,
            'entries':_model_catalog_entries(unified.entries())},ensure_ascii=False,allow_nan=False),encoding='utf-8')
        os.chmod(address_config_path,0o600)
    clean_config_path = None
    if entry_mode == 'clean':
        from .hermes_address import _catalog, _model_catalog_entries
        unified = _catalog(db_path, catalog_root, snapshot['snapshot_id'] if snapshot else None)
        clean_config_path = session_root / 'clean-config.json'
        clean_config_path.write_text(json.dumps({'catalog_sha256': unified.snapshot_id,
            'entries': _model_catalog_entries(unified.entries()), 'raw': prompt,
            'source_ref': 'user_prompt:' + original_prompt_sha}, ensure_ascii=False, allow_nan=False))
        os.chmod(clean_config_path, 0o600)
    review_config_path = None
    if entry_mode == 'review':
        from .hermes_address import _catalog, _model_catalog_entries
        unified = _catalog(db_path, catalog_root, snapshot['snapshot_id'] if snapshot else None)
        review_config_path = session_root / 'review-config.json'
        review_config_path.write_text(json.dumps({'catalog_sha256': unified.snapshot_id,
            'entries': _model_catalog_entries(unified.entries()), 'raw': prompt,
            'source_ref': 'user_prompt:' + original_prompt_sha,
            **({'review_profile': review_profile} if review_profile is not None else {}),
            **({'skills': snapshot} if snapshot else {}),
            **({'context_sources': context_sources} if context_sources else {})}, ensure_ascii=False, allow_nan=False))
        os.chmod(review_config_path, 0o600)
    fixed_path = None
    native_evidence_path = None
    if native_evidence is not None:
        from .hermes_native import native_evidence_config
        native_evidence_path = session_root/'native-evidence-config.json'
        native_evidence_path.write_text(json.dumps(native_evidence_config(
            workspace, native_evidence, snapshot), ensure_ascii=False, allow_nan=False), encoding='utf-8')
        os.chmod(native_evidence_path, 0o600)
    native_execution_path = native_execution_key_path = None
    native_execution_key = None
    if native_execution_evidence is not None:
        from .hermes_native_process import native_execution_evidence_config
        native_execution_path = session_root/'native-execution-config.json'
        native_execution_path.write_text(json.dumps(native_execution_evidence_config(
            workspace, native_execution_evidence, snapshot), ensure_ascii=False, allow_nan=False))
        os.chmod(native_execution_path, 0o600)
    if native_execution_evidence is not None or artifact_mode:
        native_execution_key = os.urandom(32)
        native_execution_key_path = session_root/'native-execution-auth.key'
        descriptor = os.open(native_execution_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as key_file:
            key_file.write(native_execution_key)
    if fixed_request is not None:
        fixed_path = session_root/'fixed-request.json'
        descriptor = os.open(fixed_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w') as stream:
            json.dump(fixed_request,stream,ensure_ascii=False,allow_nan=False)
    for name in ['tmp', 'runtime', 'terminal-tmp']:
        (session_root / name).mkdir(mode=0o700)
    prompt_file = session_root / 'prompt.txt'
    prompt_file.write_text(prompt, encoding='utf-8')
    db_path = Path(db_path).resolve()
    # SQLite creates known siblings, not an unrestricted writable DB directory.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from .hermes import HostEventStore
    event_store = HostEventStore(db_path)
    previous_ids = {e['ingest_event_id'] for e in event_store.list()}
    profile = session_root / 'hermes.sb'
    import_root = Path(__file__).resolve().parents[2]
    profile.write_text(sandbox_profile([session_root, workspace],
                                       Path.home() / '.hermes', hermes_root, db_path,
                                       [host_python.parent.parent, Path.home() / '.hermes/tools'],
                                       native_import_root=None if tool_enabled else import_root,
                                       owner_read_scope=(read_isolation_root or owner_root, [session_root, workspace, db_path,
                                           import_root, Path(sys.executable).absolute().parent.parent]),
                                       deny_read_roots=deny_read_roots,
                                       protected_write_roots=(([skill_config_path] if entry_mode == 'review'
                                           or public_contract == 'review' else [home/'skills', skill_config_path])
                                           if snapshot else []) + ([native_evidence_path] if native_evidence_path else [])
                                           + ([native_execution_path] if native_execution_path else [])
                                           + ([native_execution_key_path] if native_execution_key_path else [])
                                           + ([request_budget_path, home/'config.yaml'] if request_budget_path else []),
                                       protected_read_roots=([native_execution_key_path] if native_execution_key_path else [])))
    env = {k: v for k, v in os.environ.items() if k in {
        'HOME', 'USER', 'LOGNAME', 'SHELL', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE',
        'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE',
        'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    }}
    env.update({'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(session_root / 'runtime'),
                'HERMES_GUEST_ONBOARDING': '0', 'HERMES_ENABLE_PROJECT_PLUGINS': '0',
                'HERMES_DISABLE_LAZY_INSTALLS': '1',
                'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(session_root / 'tmp'),
                'TMP': str(session_root / 'tmp'), 'TEMP': str(session_root / 'tmp'),
                'RPE_HERMES_INSTALL_ROOT': str(hermes_root), 'RPE_HERMES_DB': str(db_path),
                'RPE_HERMES_WORKSPACE': str(workspace),
                'RPE_HERMES_TOOL_ENABLED': '1' if tool_enabled else '0',
                'RPE_HERMES_ENTRY_MODE':entry_mode,
                'RPE_HERMES_ENTRY_KEY':uuid.uuid4().hex,
                'RPE_HERMES_MAX_TASK_ATTEMPTS':str(max_task_attempts),
                'RPE_HERMES_ERROR_PATH': str(session_root / 'observer-errors.jsonl'),
                'RPE_HERMES_PROCESS_PATH': str(session_root / 'child-processes.jsonl'),
                'RPE_HARNESS_PYTHON': sys.executable, 'RPE_HARNESS_IMPORT_ROOT': str(import_root),
                'PYTHONPATH': str(import_root)})
    if entry_mode == 'review':
        env['RPE_HERMES_REVIEW_DIAGNOSTICS'] = str(session_root / 'review-diagnostics.jsonl')
    if request_budget_path is not None:
        env['RPE_HERMES_REQUEST_BUDGET_CONFIG'] = str(request_budget_path)
    if native_finish_enabled:
        env['RPE_HERMES_NATIVE_FINISH_ENABLED'] = '1'
    if fixed_path is not None:
        env['RPE_HERMES_REQUIRED_REQUEST_PATH'] = str(fixed_path)
        env['RPE_HERMES_INPUT_SHA256'] = fixed_input_sha
    if skill_config_path is not None:
        env['RPE_HERMES_SKILL_CONFIG'] = str(skill_config_path)
    if native_evidence_path is not None:
        env['RPE_HERMES_NATIVE_EVIDENCE_CONFIG'] = str(native_evidence_path)
    if native_execution_path is not None:
        env['RPE_HERMES_NATIVE_EXECUTION_CONFIG'] = str(native_execution_path)
    if address_config_path is not None:
        env['RPE_HERMES_ADDRESS_CONFIG'] = str(address_config_path)
    if clean_config_path is not None:
        env['RPE_HERMES_CLEAN_CONFIG'] = str(clean_config_path)
    if review_config_path is not None:
        env['RPE_HERMES_REVIEW_CONFIG'] = str(review_config_path)
    session_token = uuid.uuid4().hex
    env['RPE_HERMES_SESSION_TOKEN'] = session_token
    if not tool_enabled:
        # Child native commands need Hermes' real module/dependency environment,
        # without exposing the Harness implementation through PYTHONPATH.
        env['PYTHONPATH'] = str(hermes_root)
    runtime_paths = [str(Path(sys.executable).parent)]
    if not tool_enabled:
        runtime_paths.insert(0, str(host_python.parent))
    env['PATH'] = os.pathsep.join(runtime_paths)+os.pathsep+env.get('PATH', '/usr/bin:/bin')
    tools = ('rpe_producer_only' if entry_mode=='producer' else
             'rpe_harness,skills' if entry_mode=='skills' or entry_mode=='review' and snapshot else
             'rpe_harness' if entry_mode in ('strict','address','clean','review') else
             'terminal,file'+(',skills' if snapshot else '')+(',rpe_harness' if tool_enabled else ''))
    command = ['/usr/bin/sandbox-exec', '-f', str(profile),
               str(host_python), '-B',
               str(Path(__file__).with_name('hermes_bootstrap.py')),
               'chat', '--cli', '--query-file', str(prompt_file), '--oneshot', '-Q',
               '--format', 'stream-json', '--source', 'rpe-harness', '--provider', 'xiaomi',
               '--model', settings['model'], '--toolsets', tools, '--max-turns', str(max_turns),
               '--run-budget', str(int(timeout)), '--ignore-rules']
    stdout_path, stderr_path = session_root / 'stdout.jsonl', session_root / 'stderr.log'
    started = time.monotonic()
    timed_out = False
    broker_stack = ExitStack()
    broker_environment = {}
    try:
        if tool_enabled:
            from .hermes_broker import execution_broker
            extension = None
            if entry_mode == 'skills':
                from .hermes_skills import skill_extension
                extension = skill_extension(db_path,workspace,catalog_root=catalog_root,
                    snapshot_id=snapshot['snapshot_id'],context_sources=context_sources or [])
            if entry_mode == 'address':
                from .hermes_address import address_extension
                extension = address_extension(db_path,workspace,catalog_root=catalog_root,
                    snapshot_id=snapshot['snapshot_id'] if snapshot else None,
                    source_ref='user_prompt:'+original_prompt_sha)
            if entry_mode == 'clean':
                from .hermes_clean import clean_extension
                extension = clean_extension(db_path, workspace, raw=prompt,
                    source_ref='user_prompt:' + original_prompt_sha, catalog_root=catalog_root,
                    snapshot_id=snapshot['snapshot_id'] if snapshot else None)
            if entry_mode == 'review':
                from .hermes_review import review_extension
                extension = review_extension(db_path, workspace, raw=prompt,
                    source_ref='user_prompt:' + original_prompt_sha, catalog_root=catalog_root,
                    snapshot_id=snapshot['snapshot_id'] if snapshot else None,
                    context_sources=context_sources,
                    **({'review_profile': review_profile} if review_profile is not None else {}))
            broker_environment = broker_stack.enter_context(execution_broker(
                db_path=db_path, workspace=workspace, python=sys.executable,extension=extension, review_auto=entry_mode == 'review'))
            env.update(broker_environment)
        with stdout_path.open('w') as output, stderr_path.open('w') as errors:
            key_read = None
            try:
                if native_execution_key is not None:
                    key_read, key_write = os.pipe()
                    try:
                        os.write(key_write, native_execution_key)
                    finally:
                        os.close(key_write)
                    env['RPE_HERMES_NATIVE_EXECUTION_KEY_FD'] = str(key_read)
                process = subprocess.Popen(command, cwd=workspace, env=env, stdout=output, stderr=errors,
                    start_new_session=True, **({'pass_fds': (key_read,)} if key_read is not None else {}))
            finally:
                if key_read is not None:
                    os.close(key_read)
            owned_descendants = {}
            start_identity = None
            cleanup_errors = []
            signaled_descendants, remaining_descendants = [], []
            deadline = started + timeout + 20
            try:
                try:
                    start_identity = _process_table().get(process.pid, (None, None))[1]
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    cleanup_errors.append({'stage': 'initial_process_query', 'error_type': type(exc).__name__})
                while process.poll() is None:
                    if start_identity:
                        try:
                            _capture_descendants(process.pid, start_identity, owned_descendants)
                        except (OSError, ValueError, subprocess.SubprocessError) as exc:
                            cleanup_errors.append({'stage': 'capture_descendants', 'error_type': type(exc).__name__})
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        process.wait(timeout=min(1, remaining))
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                # Also stop owned terminal descendants after an ordinary host exit.
                # Discovery errors must never bypass stopping the exact owned host.
                try:
                    if start_identity:
                        try:
                            _capture_descendants(process.pid, start_identity, owned_descendants)
                        except (OSError, ValueError, subprocess.SubprocessError) as exc:
                            cleanup_errors.append({'stage': 'final_capture_descendants', 'error_type': type(exc).__name__})
                finally:
                    if process.poll() is None:
                        try:
                            _stop_group(process)
                        except (OSError, ValueError, subprocess.SubprocessError) as exc:
                            cleanup_errors.append({'stage': 'stop_host_group', 'error_type': type(exc).__name__})
                broker_stack.close()
                try:
                    owned_descendants.update(_registered_processes(session_root/'child-processes.jsonl', process.pid))
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    cleanup_errors.append({'stage': 'read_process_registry', 'error_type': type(exc).__name__})
                try:
                    owned_descendants.update(_tagged_processes(session_token))
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    cleanup_errors.append({'stage': 'query_tagged_processes', 'error_type': type(exc).__name__})
                try:
                    signaled_descendants = _stop_descendants(owned_descendants)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    cleanup_errors.append({'stage': 'stop_known_descendants', 'error_type': type(exc).__name__})
                try:
                    remaining_descendants = _remaining_descendants(owned_descendants)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    cleanup_errors.append({'stage': 'verify_known_descendants', 'error_type': type(exc).__name__})
    finally:
        broker_stack.close()
    # Preserve actual cleanup and observation facts even if private transcript
    # parsing, extraction or credential redaction fails after the process exits.
    partial = {'completed': False, 'exit_code': process.returncode, 'timed_out': timed_out,
        'session_id': None, 'usage': None, 'events': [], 'session_root': str(session_root),
        'elapsed_seconds': round(time.monotonic()-started, 3),
        'public_contract_sha256': public_contract_sha,
        'model_identity': {'provider': settings['provider'], 'requested_model': settings['model'],
                           'response_models': []},
        'process_cleanup': {'tracked_descendants': len(owned_descendants),
            'signaled_descendants': len(signaled_descendants),
            'remaining_descendants': remaining_descendants, 'cleanup_complete': False,
            'external_executor_cleanup_complete': False,
            'external_executor_tracked_children': 0,
            'external_executor_remaining_children': [], 'errors': cleanup_errors}}
    observation_error_code = 'postlaunch_diagnostics_failed'
    try:
        external_cleanup = getattr(broker_environment, 'cleanup', {
            'tracked_children': 0, 'remaining_children': [], 'cleanup_complete': True, 'errors': []})
        cleanup_errors.extend(external_cleanup['errors'])
        cleanup_complete = (not cleanup_errors and not remaining_descendants and process.poll() is not None
                            and external_cleanup['cleanup_complete'])
        partial['process_cleanup'].update(
            cleanup_complete=cleanup_complete,
            external_executor_cleanup_complete=external_cleanup['cleanup_complete'],
            external_executor_tracked_children=external_cleanup['tracked_children'],
            external_executor_remaining_children=external_cleanup['remaining_children'],
            errors=cleanup_errors)
        observed_events = [e for e in event_store.list() if e['ingest_event_id'] not in previous_ids]
        partial.update(events=observed_events,
            session_id=next((e.get('session_id') for e in observed_events if e.get('session_id')), None))
        partial['model_identity']['response_models'] = sorted({e['response_model'] for e in observed_events if isinstance(e.get('response_model'), str)})
        partial['usage'] = aggregate_usage(observed_events)
        records = []
        for line in stdout_path.read_text(errors='replace').splitlines():
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except ValueError:
                pass
        terminal = next((r for r in reversed(records) if r.get('type') == 'result'), {})
        terminal_session = terminal.get('session_id')
        observed_sessions = {e['session_id'] for e in observed_events
                             if isinstance(e.get('session_id'), str) and e['session_id']}
        if observed_sessions and terminal_session and (not isinstance(terminal_session, str)
                or terminal_session not in observed_sessions):
            observation_error_code = 'terminal_session_mismatch'
            raise ValueError('terminal session does not match newly observed DB events')
        events = observed_events
        session_id = terminal.get('session_id') or next((e.get('session_id') for e in events if e.get('session_id')), None)
        events = [e for e in events if e.get('session_id') == session_id]
        ended = any(e.get('kind') == 'on_session_end' and e.get('session_id') == session_id
                    for e in events)
        observer_error_path = session_root / 'observer-errors.jsonl'
        observer_errors = (len(observer_error_path.read_text().splitlines())
                           if observer_error_path.exists() else 0)
        usage = aggregate_usage(events)
        if observer_errors:
            usage.update(coverage_complete=False, prompt_tokens=None, output_tokens=None,
                         total_tokens=None, input_tokens=None, cache_read_tokens=None,
                         cache_write_tokens=None, reasoning_tokens=None)
        result = {'exit_code': process.returncode, 'timed_out': timed_out,
                  'completed': bool(terminal) and ended and not timed_out and process.returncode == 0
                               and not observer_errors and cleanup_complete,
                  'final_response': terminal.get('text', ''), 'session_id': session_id,
                  'events': events, 'usage': usage, 'observer_errors': observer_errors,
                  'elapsed_seconds': round(time.monotonic()-started, 3),
                  'model_identity': {'provider': settings['provider'], 'requested_model': settings['model'],
                                     'base_url': settings['base_url'],
                                     'response_models': sorted({e['response_model'] for e in events if isinstance(e.get('response_model'), str)})},
                  'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                  'original_prompt_sha256':original_prompt_sha,
                  'public_contract_sha256':public_contract_sha,
                  'contract_prompt_injected':fixed_request is not None,
                  'entry_mode':entry_mode,'max_task_attempts':max_task_attempts,'max_turns':max_turns,
                  'input_sha256':fixed_input_sha,
                  'session_root': str(session_root),
                  'catalog_snapshot_id': snapshot['snapshot_id'] if snapshot else None,
                  'process_cleanup': {'tracked_descendants': len(owned_descendants),
                                      'signaled_descendants': len(signaled_descendants),
                                      'remaining_descendants': remaining_descendants,
                                      'cleanup_complete': cleanup_complete,
                                      'external_executor_cleanup_complete': external_cleanup['cleanup_complete'],
                                      'external_executor_tracked_children': external_cleanup['tracked_children'],
                                      'external_executor_remaining_children': external_cleanup['remaining_children'],
                                      'errors': cleanup_errors},
                  'stream_tool_calls': [r for r in records if r.get('type') == 'tool_use']}
        # A malformed/untrusted terminal identity must not replace observations
        # already read from the trusted DB with an empty filtered session.
        partial.update({key: value for key, value in result.items()
                        if key not in {'events', 'usage', 'session_id', 'model_identity'}})
        result['file_write_observations'] = _file_write_observations(records, workspace)
        # Defense in depth: a provider error might include its key. Owned raw logs
        # are needed only for debugging and are deleted by caller after acceptance.
        secret = settings['api_key']
        for path in [stdout_path, stderr_path]:
            data = path.read_text(errors='replace')
            if secret in data:
                path.write_text(data.replace(secret, '[REDACTED]'))
        if secret in json.dumps(result, ensure_ascii=False):
            observation_error_code = 'credential_in_host_response'
            raise ValueError('credential appeared in host response; private output redacted')
        if entry_mode == 'review':
            from .review_diagnostics import collect_diagnostics
            result['review_diagnostics'] = collect_diagnostics(session_root / 'review-diagnostics.jsonl', result, secret=secret)
        return result
    except Exception as error:
        partial['elapsed_seconds'] = round(time.monotonic()-started, 3)
        carrier = SessionObservationError(partial, secret=settings['api_key'],
            code=observation_error_code, error_type=type(error).__name__)
        if entry_mode == 'review':
            from .review_diagnostics import collect_diagnostics, failure
            carrier.diagnostics['failure'] = failure(observation_error_code, domain='observation', stage='finalization')
            carrier.diagnostics['review_diagnostics'] = collect_diagnostics(session_root / 'review-diagnostics.jsonl', carrier.diagnostics, secret=settings['api_key'])
        raise carrier from None
