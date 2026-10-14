"""Native skills workflow. Source discovery and context resolution stay in the parent.

The isolated Hermes process reads only its trusted configuration and materialized
skill bytes; it never instantiates the catalog or context source store.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import uuid

from .hermes_gate import digest_json, _same_call

IDS = ('session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id')
PHASE_TO_TOOL = {'discover': 'rpe_discover', 'view': 'skill_view',
                 'context': 'rpe_context', 'task': 'rpe_task', 'none': 'none'}
CONSTRAINT_FLAGS = {
    'xlsx.inspect': ['preserve_original'],
    'tabular.deduplicate': ['preserve_original', 'preserve_order', 'preserve_formulas'],
}
CONSTRAINT_DESCRIPTION = 'Use only these fixed contract flags. Put higher-level business requirements in goal, not constraints.'


def _read_owned_skill(path, root):
    """Walk only materialized directories by descriptor; never follow aliases."""
    path, root = Path(path), Path(root)
    if not root.is_absolute() or root != root.resolve() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('materialized root is not canonical')
    parts = path.relative_to(root).parts
    if not parts or any(part in ('.', '..') for part in parts):
        raise ValueError('invalid materialized skill path')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            info = os.fstat(file_descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ValueError('materialized skill is not a bounded regular file')
            with os.fdopen(os.dup(file_descriptor), 'rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError('materialized skill exceeds limit')
            return raw
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def project_observer_tools(tools, mode):
    """Pinned JSON projection of the official Hermes hook serializer, not API semantics."""
    if mode == 'plain':
        return tools
    if mode not in ('sdk-default', 'sdk-reduced'):
        raise ValueError('unknown SDK observer projection')
    string_cap, sequence_cap = (8000, 200) if mode == 'sdk-default' else (1000, 50)

    def project(value, depth=0):
        if depth > 8:
            return f'<{type(value).__name__} depth limit>'
        if type(value) is str:
            return value if len(value) <= string_cap else value[:string_cap] + f'...[truncated {len(value)-string_cap} chars]'
        if type(value) is list:
            result = [project(item, depth + 1) for item in value[:sequence_cap]]
            if len(value) > sequence_cap:
                result.append({'_truncated_items': len(value) - sequence_cap})
            return result
        if type(value) is dict:
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= sequence_cap:
                    result['_truncated_items'] = len(value) - sequence_cap
                    break
                sensitive = key.lower().replace('-', '_')
                result[key] = '<redacted>' if sensitive in {
                    'api_key', 'authorization', 'proxy_authorization', 'cookie', 'set_cookie'} or sensitive.endswith('_api_key') else project(item, depth + 1)
            return result
        return value
    return project({'method': 'POST', 'body': {'tools': tools}})['body']['tools']


def _observer_policy(request):
    try:
        from agent.api_request_hooks import ApiRequestHooksMixin
    except ImportError:
        return 'plain', digest_json(request['tools'])
    serialized = ApiRequestHooksMixin()._api_request_payload_for_hook(request)
    tools = (serialized.get('body') or {}).get('tools')
    if not isinstance(tools, list):
        raise ValueError('SDK observer tool schema was truncated as a whole')
    digest = digest_json(tools)
    for mode in ('sdk-default', 'sdk-reduced'):
        if digest_json(project_observer_tools(request['tools'], mode)) == digest:
            return mode, digest
    raise ValueError('unsupported SDK observer projection')


class WorkflowStore:
    """Small durable entry state shared with the parent, with owned JSON snapshots."""
    def __init__(self, path):
        self.path = Path(path)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS rpe_skill_workflows (
                entry_key TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                state_json TEXT NOT NULL, state_sha256 TEXT NOT NULL,
                PRIMARY KEY(entry_key,session_id,turn_id))''')

    def get(self, native, *, create=True):
        key = tuple(native.get(k) for k in ('entry_key', 'session_id', 'turn_id'))
        if any(not isinstance(value, str) or not value for value in key):
            raise ValueError('workflow identity missing')
        with sqlite3.connect(self.path, timeout=10) as db:
            row = db.execute('SELECT state_json,state_sha256 FROM rpe_skill_workflows WHERE '
                             'entry_key=? AND session_id=? AND turn_id=?', key).fetchone()
        if row is None:
            if not create:
                raise ValueError('workflow state missing')
            state = dict(zip(('entry_key', 'session_id', 'turn_id'), key), task_id=native.get('task_id'),
                         phase='discover', attempts=0, policies={})
            self.put(state)
            return state
        state = json.loads(row[0])
        if digest_json(state) != row[1] or any(state.get(k) != native.get(k) for k in
                                              ('entry_key', 'session_id', 'turn_id', 'task_id')):
            raise ValueError('workflow state identity or hash mismatch')
        return state

    def put(self, state):
        raw = json.dumps(state, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        if len(raw.encode()) > 256 * 1024:
            raise ValueError('workflow state budget exceeded')
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('INSERT INTO rpe_skill_workflows VALUES(?,?,?,?,?) ON CONFLICT(entry_key,session_id,turn_id) '
                       'DO UPDATE SET state_json=excluded.state_json,state_sha256=excluded.state_sha256',
                       (*(state[k] for k in ('entry_key', 'session_id', 'turn_id')), raw, digest_json(state)))


def _identity(context, entry_key, *, api=False):
    keys = IDS[:-1] if api else IDS
    if any(not isinstance(context.get(k), str) or not context[k] for k in keys):
        raise ValueError('actual native identity required')
    return {k: context[k] for k in keys} | {'entry_key': entry_key}


def _actual_policy(store, native, name, *, require_pre=True):
    events = store.list(native['session_id'])
    requests = [e for e in events if e['kind'] == 'pre_api_request' and
                all(e.get(k) == native[k] for k in IDS[:-1])]
    if (len(requests) != 1 or requests[0].get('request_tool_choice') != name or
            requests[0].get('request_parallel_tool_calls') is not False or
            requests[0].get('request_tool_names') != [name]):
        raise ValueError('actual native sole-tool policy missing')
    pres = [e for e in events if e['kind'] == 'pre_tool_call' and e.get('tool_name') == name
            and _same_call(e, native)]
    outcomes = [e for e in events if e['kind'] == 'post_api_request' and
                all(e.get(k) == native[k] for k in IDS[:-1])]
    if ((require_pre and len(pres) != 1) or len(pres) > 1 or len(outcomes) != 1 or outcomes[0].get('model_tool_calls') !=
            [{'tool_call_id': native['tool_call_id'], 'tool_name': name}]):
        raise ValueError('actual provider call and native pre-tool identity differ')
    return requests[0]


def _raw_policy(store, native, state):
    planned = state['policies'].get(native['api_request_id'])
    rows = [e for e in store.list(native['session_id']) if e['kind'] == 'rpe_skill_api_checked' and
            all(e.get(k) == native[k] for k in IDS[:-1])]
    if (not planned or len(rows) != 1 or rows[0]['ingest_event_id'] != planned.get('raw_receipt_id') or
            rows[0].get('request_tools_sha256') != planned['raw_tools_sha256'] or
            rows[0].get('arguments_sha256') != planned['tools_sha256'] or
            rows[0].get('request_tool_choice') != planned['choice'] or
            rows[0].get('request_parallel_tool_calls') is not False or
            rows[0].get('status') != planned['projection_mode']):
        raise ValueError('official raw API execution proof missing')


def _compact_skill(row):
    keys = ('snapshot_id', 'skill_id', 'name', 'load_name', 'skill_sha256', 'script_sha256',
            'adapter_status', 'executable', 'temperature', 'capabilities', 'skill_binding')
    return {key: row[key] for key in keys if key in row}


def _paths(facts, workspace):
    from .hermes import _workspace_path
    if not isinstance(facts, dict):
        raise ValueError('facts must be an object')
    copied = json.loads(json.dumps(facts, allow_nan=False))
    for key in ('input_path', 'output_path'):
        if key in copied:
            copied[key] = _workspace_path(copied[key], workspace)
    return copied


def skill_extension(db_path, workspace, *, catalog_root, snapshot_id, context_sources):
    """Trusted parent callback. Native receipt validation precedes this boundary."""
    from .hermes import HostEventStore
    from ..skill_catalog import SkillCatalog
    from ..context import ContextStore, requirements_for
    directory = Path(workspace).resolve()
    catalog = SkillCatalog(db_path, catalog_root)
    contexts = ContextStore(db_path)
    workflows = WorkflowStore(db_path)
    events = HostEventStore(db_path)
    sources = json.loads(json.dumps(context_sources, allow_nan=False))
    if not isinstance(sources, list) or len(sources) > 8:
        raise ValueError('at most eight trusted context sources are supported')
    lock = threading.RLock()

    def fresh_selected(state):
        row = catalog.lookup(state['selected']['skill_id'], snapshot_id=snapshot_id)
        selected = state['selected']
        if (row.get('adapter_status') != 'trusted' or not row.get('executable') or
                any(row.get(k) != selected.get(k) for k in
                    ('snapshot_id', 'skill_id', 'skill_sha256', 'script_sha256', 'load_name')) or
                state['capability'] not in row['capabilities']):
            raise ValueError('selected skill source changed or adapter unavailable')
        return row

    def extension(operation, arguments, native):
        if operation not in {'discover', 'lookup', 'context', 'revalidate'} or type(arguments) is not dict:
            raise ValueError('unsupported skill RPC')
        native = dict(native)
        _identity(native, native.get('entry_key'))
        expected = {'discover': 'rpe_discover', 'lookup': 'skill_view',
                    'context': 'rpe_context', 'revalidate': 'rpe_task'}[operation]
        if native.get('tool_name') != expected or native.get('arguments_sha256') != digest_json(arguments):
            raise ValueError('skill RPC identity or argument hash mismatch')
        _actual_policy(events, native, expected)
        with lock:
            state = workflows.get(native)
            _raw_policy(events, native, state)
            if PHASE_TO_TOOL.get(state['phase']) != expected:
                raise ValueError('workflow phase does not admit RPC')
            state.setdefault('rpc_requests', {})[native['tool_call_id'] + ':' + operation] = {
                'operation': operation, 'arguments': arguments, 'native': native}
            workflows.put(state)
            if operation == 'discover':
                if set(arguments) - {'q', 'input_format', 'capability'}:
                    raise ValueError('unknown discovery parameters')
                capability = arguments.get('capability', 'xlsx.inspect')
                result = catalog.query(arguments.get('q'), input_format=arguments.get('input_format'),
                                       capability=capability, snapshot_id=snapshot_id, limit=5)
                candidates = [_compact_skill(row) for row in result['candidates']]
                adapted = [row for row in candidates if row.get('executable') is True and
                           row.get('adapter_status') == 'trusted' and capability in row.get('capabilities', [])]
                if not adapted:
                    state.update(phase='none', rejection='no_adapter', snapshot_id=snapshot_id)
                    workflows.put(state)
                    return {'status': 'rejected', 'error': {'code': 'no_adapter'}, 'candidates': candidates}
                selected = adapted[0]
                state.update(phase='view', selected=selected, capability=capability, snapshot_id=snapshot_id,
                             discover_native={k: native[k] for k in IDS})
                workflows.put(state)
                return {'status': 'discovered', 'snapshot_id': snapshot_id, 'selected': selected,
                        'candidates': candidates, 'capability': capability,
                        'execution_kind': 'native_helper' if capability == 'xlsx.inspect' else 'rpe_contract'}
            row = fresh_selected(state)
            if operation == 'lookup':
                if set(arguments) != {'skill_id'} or arguments['skill_id'] != row['skill_id']:
                    raise ValueError('lookup differs from discovered skill')
                return _compact_skill(row)
            loaded = next((e for e in events.list(native['session_id']) if e['kind'] == 'rpe_skill_loaded'
                           and e['ingest_event_id'] == state.get('load_receipt_id')), None)
            if (loaded is None or not _same_call(loaded, state.get('load_native', {})) or
                    any(loaded.get(k) != row.get(k) for k in
                        ('snapshot_id', 'skill_id', 'skill_sha256', 'script_sha256'))):
                raise ValueError('linked native load receipt missing')
            if operation == 'context':
                if set(arguments) - {'facts', 'source_refs'}:
                    raise ValueError('unknown context parameters')
                facts = _paths(arguments.get('facts', {}), directory)
                refs = arguments.get('source_refs')
                if refs is None:
                    refs = []
                    for item in sources:
                        if not isinstance(item, dict) or set(item) != {'source', 'event_id', 'payload', 'facts'}:
                            raise ValueError('invalid trusted context source')
                        # Original event IDs remain evidence while capture identities are session-local.
                        event_id = item['event_id'] + ':' + digest_json([state['entry_key'], native['session_id']])[:16]
                        ref = contexts.capture(source=item['source'], event_id=event_id,
                            session_id=native['session_id'], payload={'original_event_id': item['event_id'],
                            'event': item['payload']}, facts=_paths(item['facts'], directory))
                        refs.append(ref.to_dict())
                result = contexts.recover(requirement=requirements_for(state['capability']),
                                          session_id=native['session_id'], facts=facts, source_refs=refs)
                correctable = result['status'] == 'rejected' and result['issues'] and all(
                    issue['code'] in {'invalid_fact', 'invalid_request', 'limit_exceeded'} for issue in result['issues'])
                state.update(recovery_id=result['recovery_id'], context_status=result['status'],
                             phase='task' if result['status'] == 'ready' else 'context' if correctable else 'none',
                             context_native={k: native[k] for k in IDS})
                workflows.put(state)
                return result
            if (set(arguments) != {'recovery_id', 'load_receipt_id', 'facts'} or
                    arguments['recovery_id'] != state.get('recovery_id') or
                    arguments['load_receipt_id'] != state['load_receipt_id']):
                raise ValueError('revalidation differs from loaded context')
            fresh = contexts.revalidate_recovery(arguments['recovery_id'], session_id=native['session_id'])
            current = _paths(arguments['facts'], directory)
            if fresh['status'] == 'ready' and any(k in fresh['facts'] and
                                                 digest_json(v) != digest_json(fresh['facts'][k])
                                                 for k, v in current.items()):
                raise ValueError('task facts differ from recovered context')
            state.update(recovery_id=fresh['recovery_id'], context_status=fresh['status'])
            workflows.put(state)
            return {**fresh, 'skill_binding': row['skill_binding'], 'capability': state['capability'],
                    'load_receipt_id': state['load_receipt_id'],
                    'execution_kind': 'native_helper' if state['capability'] == 'xlsx.inspect' else 'rpe_contract'}
    return extension


def _skill_exchange(operation, arguments, *, store, native):
    from urllib.parse import urlsplit, urlunsplit
    from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener
    from urllib.error import HTTPError
    url = os.environ.get('RPE_HERMES_BROKER_URL', '')
    token = os.environ.get('RPE_HERMES_BROKER_TOKEN', '')
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path != '/task' or
            parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None or
            parsed.port is None or not 1 <= parsed.port <= 65535 or not token or len(token) > 256 or
            any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise ValueError('invalid parent skill endpoint')
    native = {**native, 'arguments_sha256': digest_json(arguments)}
    receipt = store.record('rpe_skill_rpc_received', native)
    body = json.dumps({'operation': operation, 'arguments': arguments,
                       'receipt_id': receipt['ingest_event_id'], 'native': native},
                      allow_nan=False, ensure_ascii=False).encode()
    if len(body) > 1_048_576:
        raise ValueError('skill RPC request exceeds limit')

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = Request(urlunsplit(parsed._replace(path='/skill')), data=body,
                      headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
                      method='POST')
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
            if response.status != 200 or response.headers.get_content_type() != 'application/json':
                raise ValueError('invalid parent skill response')
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError('skill RPC response exceeds limit')
    except HTTPError as error:
        error.close()
        raise ValueError('parent rejected skill RPC') from None
    result = json.loads(raw)
    if isinstance(result, dict) and set(result) == {'result'}:
        result = result['result']
    if type(result) is not dict:
        raise ValueError('invalid parent skill result')
    return result, receipt['ingest_event_id']


def _configuration():
    path = Path(os.environ.get('RPE_HERMES_SKILL_CONFIG', ''))
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1_048_576:
        raise ValueError('trusted skills configuration missing')
    config = json.loads(path.read_text())
    materialized = Path(config['materialized_root'])
    if (not materialized.is_absolute() or materialized.is_symlink() or
            not materialized.resolve().is_relative_to(path.parent.resolve())):
        raise ValueError('materialized skills must remain in the session root')
    entries = config['entries']
    if not isinstance(entries, list) or len(entries) > 2048:
        raise ValueError('invalid materialized entries')
    config['config_path'] = str(path)
    return config


def register_skill_workflow(ctx, *, store, workspace, python, error_path):
    from .hermes import _CALL_CONTEXT, _execute, _mark_observer_failure
    config = _configuration()
    workspace = Path(workspace).resolve()
    entry_key = os.environ.get('RPE_HERMES_ENTRY_KEY')
    if not isinstance(entry_key, str) or not entry_key:
        raise ValueError('skills workflow requires a trusted entry key')
    workflows = WorkflowStore(store.path)
    lock = threading.RLock()
    required = None
    fixed = os.environ.get('RPE_HERMES_REQUIRED_REQUEST_PATH')
    if fixed:
        path = Path(fixed)
        if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 1_048_576:
            raise ValueError('invalid fixed request file')
        required = json.loads(path.read_text())
    input_sha = os.environ.get('RPE_HERMES_INPUT_SHA256')

    def fail(state, code, *, field=None):
        error = {'code': code}
        if field is not None:
            error['field'] = field
            state['last_error'] = error
        state['attempts'] = min(3, state['attempts'] + 1)
        if state['attempts'] >= 3:
            state['phase'] = 'none'
        workflows.put(state)
        return json.dumps({'status': 'rejected', 'error': error})

    def policy(request, **context):
        try:
            native = _identity(context, entry_key, api=True)
            with lock:
                state = workflows.get(native)
                state['config_path'] = config['config_path']
                name = PHASE_TO_TOOL[state['phase']]
                rewritten = dict(request)
                chosen = [tool for tool in request.get('tools', []) if isinstance(tool, dict) and
                          (tool.get('function') or {}).get('name') == ('rpe_task' if name == 'none' else name)]
                if len(chosen) != 1:
                    raise ValueError('current phase tool schema unavailable or ambiguous')
                chosen = json.loads(json.dumps(chosen, allow_nan=False))
                if name in ('rpe_task', 'none'):
                    chosen[0]['function']['parameters']['properties']['constraints'] = {
                        'type': 'array', 'description': CONSTRAINT_DESCRIPTION,
                        'items': {'type': 'string', 'enum': CONSTRAINT_FLAGS[
                            state.get('capability', 'tabular.deduplicate')]}}
                if name == 'rpe_context':
                    facts = {'input_path': {'type': 'string', 'description': 'Workspace-relative input file path.'},
                             'output_path': {'type': 'string', 'description': 'New workspace-relative output file path.'}}
                    if state['capability'] == 'xlsx.inspect':
                        facts['mode'] = {'type': 'string', 'enum': ['rows', 'sheets', 'formulas'],
                                         'description': 'Inspection mode; use rows for workbook cell values.'}
                    else:
                        facts['key_columns'] = {'type': 'array', 'minItems': 1,
                            'items': {'type': 'string'}, 'description': 'Explicit user-selected deduplication columns; never guess.'}
                    chosen[0]['function']['parameters']['properties']['facts'] = {
                        'type': 'object', 'properties': facts, 'additionalProperties': False}
                hint = {
                    'discover': 'Call rpe_discover once to select the trusted capability for this task.',
                    'view': 'The skill is already discovered. Next call native skill_view with exactly name=' +
                            repr(state.get('selected', {}).get('load_name')) + '. Do not rediscover.',
                    'context': 'Next call rpe_context. Its facts are execution parameters (input_path, output_path, and ' +
                        ('mode rows/sheets/formulas' if state.get('capability') == 'xlsx.inspect' else 'explicit key_columns') +
                        '), not descriptions of workbook data. Omit source_refs to use trusted context sources; '
                        'skill snapshot hashes are not context SourceRef objects.',
                    'task': 'Next call rpe_task for the selected capability. Recovered required parameters may be omitted. '
                        'Use optional sheet only as specified by the task. The task result must be independently verified.',
                    'none': 'The workflow is terminal. Do not call tools. Give a concise final reply using the last durable '
                        'task/context status; a rejected or needs_context result does not imply an output exists.'}[state['phase']]
                rewritten['messages'] = list(request.get('messages', [])) + [{'role': 'system', 'content': hint}]
                rewritten.update(tools=chosen, parallel_tool_calls=False,
                                 tool_choice='none' if name == 'none' else
                                 {'type': 'function', 'function': {'name': name}})
                mode, observed_digest = _observer_policy(rewritten)
                state['policies'][native['api_request_id']] = {'choice': name,
                    'tools_sha256': observed_digest, 'raw_tools_sha256': digest_json(chosen),
                    'tools_schema': chosen, 'projection_mode': mode, 'phase': state['phase']}
                if len(state['policies']) > 64:
                    raise ValueError('workflow API budget exceeded')
                workflows.put(state)
            return {'request': rewritten, 'source': 'rpe_harness', 'reason': 'skills_workflow'}
        except (ValueError, TypeError, OSError, sqlite3.Error):
            _mark_observer_failure(error_path, 'skills_policy')
            raise

    def api_execution(request, next_call, **context):
        native = _identity(context, entry_key, api=True)
        with lock:
            state = workflows.get(native)
            planned = state['policies'].get(native['api_request_id'])
            choice = request.get('tool_choice')
            choice = (choice.get('function') or {}).get('name') if isinstance(choice, dict) else choice
            if (not planned or choice != planned['choice'] or request.get('parallel_tool_calls') is not False or
                    digest_json(request.get('tools')) != planned['raw_tools_sha256'] or planned.get('raw_receipt_id')):
                raise ValueError('actual raw API schema or identity differs from policy')
            record = store.record('rpe_skill_api_checked', {**native, 'request': {'body': request},
                'arguments_sha256': planned['tools_sha256'], 'status': planned['projection_mode'],
                'workflow_phase': planned['phase']})
            planned['raw_receipt_id'] = record['ingest_event_id']
            workflows.put(state)
        return next_call(request)

    def metadata(native, state):
        row = state.get('selected', {})
        return {**native, **{k: row[k] for k in ('snapshot_id', 'skill_id', 'skill_sha256', 'script_sha256')
                            if k in row}, 'workflow_phase': state['phase']}

    def rpc(operation, arguments, native):
        return _skill_exchange(operation, arguments, store=store, native=native)

    def handle(name, args, task_id=None, session_id=None):
        identity = {**_CALL_CONTEXT.get(), 'tool_name': name, 'entry_key': entry_key}
        if identity.get('session_id') != session_id or identity.get('task_id') != task_id:
            return json.dumps({'status': 'rejected', 'error': {'code': 'entry_context_missing'}})
        state = workflows.get(identity)
        try:
            if name == 'rpe_discover':
                found, receipt_id = rpc('discover', args, identity)
                state = workflows.get(identity)
                event = store.record('rpe_skill_discovered', {**metadata(identity, state),
                                     'receipt_id': receipt_id, 'status': found['status']})
                state['discover_receipt_id'] = event['ingest_event_id']
                workflows.put(state)
                return json.dumps(found)
            if name == 'rpe_context':
                result, receipt_id = rpc('context', args, identity)
                state = workflows.get(identity)
                event = store.record('rpe_context_recovered', {**metadata(identity, state),
                    'receipt_id': receipt_id, 'recovery_id': result['recovery_id'],
                    'context_status': result['status'], 'load_receipt_id': state['load_receipt_id']})
                state['context_receipt_id'] = event['ingest_event_id']
                correctable = result['status'] == 'rejected' and result['issues'] and all(
                    issue['code'] in {'invalid_fact', 'invalid_request', 'limit_exceeded'} for issue in result['issues'])
                if correctable:
                    state['attempts'] = min(3, state['attempts'] + 1)
                    state['phase'] = 'context' if state['attempts'] < 3 else 'none'
                workflows.put(state)
                return json.dumps(result)
            allowed = {'goal', 'constraints', 'input_path', 'output_path', 'capability', 'sheet',
                       'key_columns', 'keep', 'encoding', 'delimiter', 'mode'}
            if type(args) is not dict or set(args) - allowed or not isinstance(args.get('goal'), str) or not args['goal'].strip():
                raise ValueError('invalid task parameters')
            capability = state['capability']
            if args.get('capability', capability) != capability:
                raise ValueError('task capability differs from discovery')
            constraints = args.get('constraints', [])
            if (type(constraints) is not list or
                    any(type(flag) is not str or flag not in CONSTRAINT_FLAGS[capability] for flag in constraints)):
                return fail(state, 'invalid_task_arguments', field='constraints')
            necessary = ('input_path', 'output_path', 'mode' if capability == 'xlsx.inspect' else 'key_columns')
            fresh, receipt_id = rpc('revalidate', {'recovery_id': state['recovery_id'],
                'load_receipt_id': state['load_receipt_id'], 'facts': {k: args[k] for k in necessary if k in args}}, identity)
            state = workflows.get(identity)
            store.record('rpe_context_recovered', {**metadata(identity, state), 'receipt_id': receipt_id,
                'recovery_id': fresh['recovery_id'], 'context_status': fresh['status'],
                'load_receipt_id': state['load_receipt_id']})
            if fresh['status'] != 'ready':
                state.update(phase='none', context_status=fresh['status'])
                workflows.put(state)
                return json.dumps({'status': 'rejected', 'error': {'code': 'context_not_ready'}})
            execution = {**fresh['facts'], **{k: v for k, v in args.items() if k not in {'goal', 'constraints', 'capability'}}}
            if capability == 'xlsx.inspect':
                execution['skill_binding'] = fresh['skill_binding']
            payload = {'schema_version': 2, 'event_id': 'skill-' + uuid.uuid4().hex,
                       'capability': capability, 'goal': args['goal'], 'constraints': args.get('constraints', []),
                       'execution': execution}
            result = json.loads(_execute({'request': payload}, store=store, workspace=workspace, python=python,
                                task_id=task_id, session_id=session_id, entry_key=entry_key,
                                required_request=required, input_sha=input_sha, task_attempt=state['attempts'] + 1))
            state = workflows.get(identity)
            if result.get('run_id') and result['status'] in {'succeeded', 'rejected'}:
                state.update(phase='none', task_result=result, task_native={k: identity[k] for k in IDS})
                workflows.put(state)
                return json.dumps(result)
            return fail(state, (result.get('error') or {}).get('code', 'task_not_verified'))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            state = workflows.get(identity)
            return fail(state, 'workflow_arguments_invalid')

    def middleware(tool_name, args, next_call, **context):
        try:
            native = {**_identity(context, entry_key), 'tool_name': tool_name}
            with lock:
                state = workflows.get(native)
                if tool_name != PHASE_TO_TOOL.get(state['phase']) or tool_name == 'none':
                    return fail(state, 'workflow_phase_invalid')
                observed = _actual_policy(store, native, tool_name, require_pre=False)
                _raw_policy(store, native, state)
                policy_row = state['policies'].get(native['api_request_id'])
                if (not policy_row or policy_row['choice'] != tool_name or
                        policy_row['tools_sha256'] != observed.get('request_tools_sha256')):
                    return json.dumps({'status': 'rejected', 'error': {'code': 'workflow_policy_invalid'}})
                token = _CALL_CONTEXT.set({k: native[k] for k in IDS})
                try:
                    if tool_name != 'skill_view':
                        return next_call(args)
                    selected = state['selected']
                    if type(args) is not dict or set(args) != {'name'} or args['name'] != selected['load_name']:
                        return fail(state, 'native_load_arguments_invalid')
                    matching = [item for item in config['entries'] if item.get('load_name') == selected['load_name']]
                    if len(matching) != 1 or matching[0]['skill_id'] != selected['skill_id']:
                        raise ValueError('materialized load name ambiguous')
                    path = Path(config['materialized_root']) / selected['skill_id'] / 'SKILL.md'
                    if hashlib.sha256(_read_owned_skill(path, config['materialized_root'])).hexdigest() != selected['skill_sha256']:
                        raise ValueError('materialized bytes changed')
                    raw = next_call(args)
                    if isinstance(raw, str) and len(raw.encode()) > 1_048_576:
                        raise ValueError('native skill result exceeds limit')
                    result = json.loads(raw) if isinstance(raw, str) else raw
                    row, lookup_receipt = rpc('lookup', {'skill_id': selected['skill_id']}, native)
                    state = workflows.get(native)
                    if (not isinstance(result, dict) or result.get('success') is not True or
                            result.get('setup_needed') is not False or result.get('name') != row['name'] or
                            result.get('_source_path') != str(path) or not isinstance(result.get('content'), str) or
                            hashlib.sha256(result['content'].encode()).hexdigest() != row['skill_sha256']):
                        return fail(state, 'native_load_source_mismatch')
                    event = store.record('rpe_skill_loaded', {**metadata(native, state), 'receipt_id': lookup_receipt,
                                         'arguments_sha256': digest_json(args),
                                         'status': 'loaded'})
                    state.update(phase='context', load_receipt_id=event['ingest_event_id'],
                                 load_native={k: native[k] for k in IDS}, materialized_path=str(path),
                                 materialized_root=config['materialized_root'], loaded_content_sha256=row['skill_sha256'])
                    workflows.put(state)
                    return raw if isinstance(raw, str) else json.dumps(raw)
                finally:
                    _CALL_CONTEXT.reset(token)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return json.dumps({'status': 'rejected', 'error': {'code': 'workflow_identity_invalid'}})

    ctx.register_middleware('llm_request', policy)
    ctx.register_middleware('llm_execution', api_execution)
    ctx.register_middleware('tool_execution', middleware)
    properties = {
        'rpe_discover': {'q': {'type': 'string', 'maxLength': 4096}, 'input_format': {'type': 'string'},
                         'capability': {'type': 'string', 'enum': ['xlsx.inspect', 'tabular.deduplicate']}},
        'rpe_context': {'facts': {'type': 'object', 'description': 'Required execution parameters, not business row data.'},
                        'source_refs': {'type': 'array', 'maxItems': 8,
                            'description': 'Omit to use trusted session sources. Explicit [] selects none. Never use skill/snapshot hashes.',
                            'items': {'type': 'object', 'properties': {'source': {'type': 'string'},
                                'event_id': {'type': 'string'}, 'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
                                'required': ['source', 'event_id', 'sha256'], 'additionalProperties': False}}},
        'rpe_task': {'goal': {'type': 'string'}, 'constraints': {'type': 'array',
                        'description': CONSTRAINT_DESCRIPTION,
                        'items': {'type': 'string', 'enum': CONSTRAINT_FLAGS['tabular.deduplicate']}},
                     'input_path': {'type': 'string'}, 'output_path': {'type': 'string'},
                     'key_columns': {'type': 'array', 'items': {'type': 'string'}},
                     'mode': {'type': 'string', 'enum': ['rows', 'sheets', 'formulas']},
                     'sheet': {'type': 'string'}, 'keep': {'type': 'string', 'enum': ['first']},
                     'encoding': {'type': 'string'}, 'delimiter': {'type': 'string'},
                     'capability': {'type': 'string', 'enum': ['xlsx.inspect', 'tabular.deduplicate']}}}
    for name, fields in properties.items():
        def handler(args, task_id=None, session_id=None, _name=name, **kwargs):
            return handle(_name, args, task_id=task_id, session_id=session_id)
        ctx.register_tool(name=name, toolset='rpe_harness', handler=handler, schema={
            'name': name, 'description': {'rpe_discover': 'Find a trusted skill for the natural task. Discovery grants no execution.',
                'rpe_context': 'Recover required explicit facts from trusted sources. Never guess missing key columns.',
                'rpe_task': 'Execute the loaded skill through its bound RPE contract with independently verified output. '
                            'Required parameters recovered by rpe_context may be omitted; preserve its facts.'}[name],
            'parameters': {'type': 'object', 'properties': fields, 'additionalProperties': False,
                           'required': ['q'] if name == 'rpe_discover' else ['goal'] if name == 'rpe_task' else []}})
