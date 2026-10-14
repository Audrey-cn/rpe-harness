"""Run the installed Hermes host with an owned home and bounded process group.

Only Xiaomi's existing API-key provider is supported by this launcher. OAuth
refresh state is deliberately never cloned. The native plugin can also be used
directly with other providers under the host's own configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid


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
                    protected_write_roots=()):
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
                   package / 'integrations/hermes.py', package / 'integrations/hermes_bootstrap.py']
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
            +native_scope+owned_scope+denied_scope+protected_scope+'\n')


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


def prepare_home(session_root, workspace, db_path, *, tool_enabled, settings, entry_mode='optional'):
    if entry_mode not in ('optional','strict','skills') or (entry_mode in ('strict','skills') and not tool_enabled):
        raise ValueError('strict entry requires the Harness tool')
    if settings.get('provider') != 'xiaomi':
        raise ValueError('isolated launcher currently requires xiaomi API-key provider')
    if not all(isinstance(settings.get(k), str) and settings[k]
               for k in ['model', 'base_url', 'api_key']):
        raise ValueError('missing model, plan endpoint or API key')
    from urllib.parse import urlsplit
    url = urlsplit(settings['base_url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('provider endpoint must be a plain HTTPS URL')
    if any('\n' in settings[k] or '\r' in settings[k] for k in ['api_key', 'base_url']):
        raise ValueError('invalid credential assignment')
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
        'platform_toolsets': {'cli': (['rpe_harness','skills'] if entry_mode=='skills' else
                                     ['rpe_harness'] if entry_mode=='strict' else
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
    if entry_mode in ('strict','skills'):
        config['tools'] = {'tool_search':{'enabled':'off'}}
    (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
    plugin = home / 'plugins/rpe-harness'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text('name: rpe-harness\nversion: 1.6.0\ndescription: Durable RPE task and host observations\n', encoding='utf-8')
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
        if not isinstance(args, dict) or not isinstance(args.get('path'), str) or not isinstance(args.get('content'), str):
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
        except (ValueError, TypeError, OSError):
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
                       read_isolation_root=None, deny_read_roots=(), max_turns=12):
    if type(max_turns) is not int or not 1 <= max_turns <= 12:
        raise ValueError('max_turns must be an integer between 1 and 12')
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').exists():
        raise ValueError('isolated host launcher requires macOS sandbox-exec')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 1_048_576:
        raise ValueError('prompt must be nonempty and at most 1 MiB')
    if not 1 <= timeout <= 1800:
        raise ValueError('timeout must be between 1 and 1800 seconds')
    if entry_mode not in ('optional','strict','skills') or (entry_mode in ('strict','skills') and not tool_enabled):
        raise ValueError('strict entry requires the Harness tool')
    if entry_mode == 'skills' and catalog_root is None:
        raise ValueError('skills entry requires an owned skill catalog')
    if type(max_task_attempts) is not int or not 1 <= max_task_attempts <= 20:
        raise ValueError('task attempt limit must be between 1 and 20')
    owner_root, workspace = Path(owner_root).resolve(), Path(workspace).resolve()
    if not workspace.is_dir():
        raise ValueError('workspace must be an existing directory')
    original_prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
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
    settings = _read_host_settings(hermes_root, host_python)
    session_root = owner_root / ('session-'+uuid.uuid4().hex)
    home = prepare_home(session_root, workspace, Path(db_path).resolve(),
                        tool_enabled=tool_enabled, settings=settings,entry_mode=entry_mode)
    skill_config_path = None
    snapshot = None
    if catalog_root is not None:
        from ..skill_catalog import SkillCatalog
        catalog = SkillCatalog(db_path, catalog_root)
        snapshot = catalog.materialize(home / 'skills', snapshot_id=skill_snapshot_id)
        (home / '.no-bundled-skills').touch(mode=0o600)
        config = {'catalog_root':str(Path(catalog_root).resolve()),
                  'snapshot_id':snapshot['snapshot_id'],
                  'materialized_root':snapshot['materialized_path'],
                  'entries':[{key:entry[key] for key in ('skill_id','load_name','skill_path',
                     'skill_sha256','script_path','script_sha256') if key in entry}
                     for entry in snapshot['entries']]}
        skill_config_path = session_root / 'skill-config.json'
        config_text=json.dumps(config,ensure_ascii=False)
        if len(config_text.encode())>8*1_048_576:
            raise ValueError('materialized skill configuration exceeds 8 MiB')
        skill_config_path.write_text(config_text,encoding='utf-8')
        os.chmod(skill_config_path,0o600)
        if not tool_enabled:
            settings_path = home / 'config.yaml'
            native_config = json.loads(settings_path.read_text())
            native_config.setdefault('tools',{}).setdefault('tool_search',{})['enabled'] = 'off'
            settings_path.write_text(json.dumps(native_config,ensure_ascii=False))
    fixed_path = None
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
                                       protected_write_roots=[home/'skills',skill_config_path] if snapshot else []))
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
    if fixed_path is not None:
        env['RPE_HERMES_REQUIRED_REQUEST_PATH'] = str(fixed_path)
        env['RPE_HERMES_INPUT_SHA256'] = fixed_input_sha
    if skill_config_path is not None:
        env['RPE_HERMES_SKILL_CONFIG'] = str(skill_config_path)
    session_token = uuid.uuid4().hex
    env['RPE_HERMES_SESSION_TOKEN'] = session_token
    if not tool_enabled:
        env.pop('PYTHONPATH', None)
    env['PATH'] = str(Path(sys.executable).parent)+os.pathsep+env.get('PATH', '/usr/bin:/bin')
    tools = ('rpe_harness,skills' if entry_mode=='skills' else
             'rpe_harness' if entry_mode=='strict' else
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
            broker_environment = broker_stack.enter_context(execution_broker(
                db_path=db_path, workspace=workspace, python=sys.executable,extension=extension))
            env.update(broker_environment)
        with stdout_path.open('w') as output, stderr_path.open('w') as errors:
            process = subprocess.Popen(command, cwd=workspace, env=env, stdout=output, stderr=errors,
                                       start_new_session=True)
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
    external_cleanup = getattr(broker_environment, 'cleanup', {
        'tracked_children': 0, 'remaining_children': [], 'cleanup_complete': True, 'errors': []})
    cleanup_errors.extend(external_cleanup['errors'])
    cleanup_complete = (not cleanup_errors and not remaining_descendants and process.poll() is not None
                        and external_cleanup['cleanup_complete'])
    records = []
    for line in stdout_path.read_text(errors='replace').splitlines():
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
        except ValueError:
            pass
    terminal = next((r for r in reversed(records) if r.get('type') == 'result'), {})
    events = [e for e in event_store.list() if e['ingest_event_id'] not in previous_ids]
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
              'stream_tool_calls': [r for r in records if r.get('type') == 'tool_use'],
              'file_write_observations': _file_write_observations(records, workspace)}
    # Defense in depth: a provider error might include its key. Owned raw logs
    # are needed only for debugging and are deleted by caller after acceptance.
    secret = settings['api_key']
    for path in [stdout_path, stderr_path]:
        data = path.read_text(errors='replace')
        if secret in data:
            path.write_text(data.replace(secret, '[REDACTED]'))
    if secret in json.dumps(result, ensure_ascii=False):
        raise ValueError('credential appeared in host response; private output redacted')
    return result
