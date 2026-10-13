"""Native Hermes plugin bridge with durable, metadata-only observations."""
from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import sqlite3
import subprocess
import time
import uuid
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TEXT_FIELDS = frozenset({
    'session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id', 'tool_name',
    'model', 'provider', 'response_model', 'api_mode', 'platform', 'status', 'run_id',
    'error_type', 'error_code', 'turn_exit_reason', 'telemetry_schema_version',
    'finish_reason', 'parent_session_id',
    'receipt_id', 'entry_key', 'contract_sha256', 'input_sha256',
    'request_tool_choice', 'request_tools_sha256',
})
_NUMBER_FIELDS = frozenset({
    'duration_ms', 'api_duration', 'api_call_count', 'started_at', 'ended_at',
    'first_chunk_at', 'message_count', 'assistant_content_chars', 'assistant_tool_call_count',
    'task_attempt',
})
_BOOL_FIELDS = frozenset({'completed', 'failed', 'interrupted', 'is_first_turn',
                          'request_parallel_tool_calls'})
_CALL_CONTEXT = ContextVar('rpe_native_tool_context', default={})
_USAGE_FIELDS = frozenset({
    'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens',
    'reasoning_tokens', 'request_count', 'prompt_tokens', 'completion_tokens', 'total_tokens',
})
_RAW_USAGE_FIELDS = _USAGE_FIELDS | frozenset({
    'input_tokens_details', 'output_tokens_details', 'prompt_tokens_details',
    'completion_tokens_details', 'cached_tokens', 'cache_creation_tokens', 'cache_write_tokens',
    'cache_read_input_tokens', 'cache_creation_input_tokens', 'audio_tokens', 'text_tokens',
    'image_tokens', 'accepted_prediction_tokens', 'rejected_prediction_tokens',
    'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'cache_creation',
    'ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens',
    'promptTokenCount', 'candidatesTokenCount', 'totalTokenCount', 'cachedContentTokenCount',
    'thoughtsTokenCount',
})
_HOOKS = ('on_session_start', 'pre_llm_call', 'post_llm_call', 'on_session_end',
          'on_session_finalize', 'pre_tool_call', 'post_tool_call', 'pre_api_request',
          'post_api_request', 'api_request_error')


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _digest_json(value):
    # Native controls can import only this observer module; hashing metadata must
    # never depend on execution or acceptance code outside that sandbox scope.
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        allow_nan=False,separators=(',',':')).encode()).hexdigest()


def _raw_usage(value: dict, depth: int = 0) -> dict:
    result = {}
    for key, item in value.items():
        if key not in _RAW_USAGE_FIELDS:
            continue
        if item is None or _number(item):
            result[key] = item
        elif isinstance(item, dict) and depth < 3:
            result[key] = _raw_usage(item, depth + 1)
    return result


def _usage_metadata(usage: dict) -> dict:
    result = {key: value for key, value in usage.items()
              if key in _USAGE_FIELDS and (value is None or _number(value))}
    raw = usage.get('raw_provider_usage')
    if isinstance(raw, dict):
        result['raw_provider_usage'] = _raw_usage(raw)
    present = usage.get('present_fields')
    if isinstance(present, list):
        result['present_fields'] = [key for key in present if isinstance(key, str)
                                    and all(part in _RAW_USAGE_FIELDS for part in key.split('.'))]
    identity = usage.get('response_identity')
    if isinstance(identity, dict):
        result['response_identity'] = {key: value for key, value in identity.items()
                                       if key in {'id', 'model', 'provider', 'object', 'created'}
                                       and (value is None or isinstance(value, str) or _number(value))}
    return result


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    saved = {key: value for key, value in payload.items()
             if key in _TEXT_FIELDS and (value is None or isinstance(value, str))}
    saved.update({key: value for key, value in payload.items()
                  if key in _NUMBER_FIELDS and (value is None or _number(value))})
    saved.update({key: value for key, value in payload.items()
                  if key in _BOOL_FIELDS and type(value) is bool})
    if 'usage' in payload:
        usage = payload['usage']
        saved['usage'] = _usage_metadata(usage) if isinstance(usage, dict) else None
    error = payload.get('error')
    if isinstance(error, dict):
        saved['error'] = {key: error[key] for key in ('type', 'code')
                          if isinstance(error.get(key), str)}
    request = payload.get('request')
    body = request.get('body') if isinstance(request, dict) else None
    if isinstance(body, dict):
        choice = body.get('tool_choice')
        if isinstance(choice, dict) and choice.get('type') == 'function':
            choice = (choice.get('function') or {}).get('name')
        if isinstance(choice, str):
            saved['request_tool_choice'] = choice
        if type(body.get('parallel_tool_calls')) is bool:
            saved['request_parallel_tool_calls'] = body['parallel_tool_calls']
        tools = body.get('tools')
        if isinstance(tools, list):
            saved['request_tool_names'] = sorted((t.get('function') or {}).get('name','')
                                                for t in tools if isinstance(t,dict))
            saved['request_tools_sha256'] = _digest_json(tools)
    response = payload.get('response')
    assistant = response.get('assistant_message') if isinstance(response, dict) else None
    if isinstance(assistant, dict) and isinstance(assistant.get('tool_calls'), list):
        saved['model_tool_calls'] = []
        for call in assistant['tool_calls']:
            call = call if isinstance(call,dict) else {}
            # Hermes serializes normalized ToolCall dataclasses as id/name; older
            # SDK-shaped hook payloads put the name inside function instead.
            function = call.get('function')
            name = call.get('name') if 'name' in call else (
                function.get('name') if isinstance(function,dict) else None)
            saved['model_tool_calls'].append({
                'tool_call_id':call.get('id') if isinstance(call.get('id'),str) else None,
                'tool_name':name if isinstance(name,str) else None})
    return saved


class HostEventStore:
    """Separate table in the Harness DB; no prompts, tool arguments or result bodies."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS hermes_host_events (
                id INTEGER PRIMARY KEY,
                ingest_event_id TEXT NOT NULL UNIQUE,
                source_event_id TEXT,
                kind TEXT NOT NULL,
                session_id TEXT,
                received_at REAL NOT NULL,
                metadata_json TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS hermes_events_session '
                       'ON hermes_host_events(session_id,id)')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def record(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(kind, str) or not kind or not isinstance(payload, dict):
            raise ValueError('event kind and payload object are required')
        metadata = _metadata(payload)
        source_id = payload.get('source_event_id', payload.get('event_id'))
        source_id = source_id if isinstance(source_id, str) and source_id else None
        event = {'ingest_event_id': uuid.uuid4().hex, 'source_event_id': source_id,
                 'kind': kind, 'received_at': time.time(), **metadata}
        encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        with self._connect() as db:
            db.execute('INSERT INTO hermes_host_events '
                       '(ingest_event_id,source_event_id,kind,session_id,received_at,metadata_json) '
                       'VALUES(?,?,?,?,?,?)',
                       (event['ingest_event_id'], source_id, kind, metadata.get('session_id'),
                        event['received_at'], encoded))
        return event

    def list(self, session_id: str | None = None) -> list[dict[str, Any]]:
        query = ('SELECT ingest_event_id,source_event_id,kind,received_at,metadata_json '
                 'FROM hermes_host_events')
        params = ()
        if session_id is not None:
            query += ' WHERE session_id=?'
            params = (session_id,)
        with self._connect() as db:
            rows = db.execute(query + ' ORDER BY id', params).fetchall()
        return [{'ingest_event_id': row[0], 'source_event_id': row[1], 'kind': row[2],
                 'received_at': row[3], **json.loads(row[4])} for row in rows]


def _workspace_path(value: Any, workspace: Path) -> str:
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('input/output paths must be nonempty strings')
    requested = Path(value)
    candidate = requested if requested.is_absolute() else workspace / requested
    # Inspect the submitted chain before resolve() would erase a symlink, even when it points inside.
    cursor = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('input/output paths must not contain symlinks')
    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace):
        raise ValueError('input/output paths must remain inside the configured workspace')
    return str(resolved)


def _prepare_request(args: dict[str, Any], workspace: Path, session_id: str | None) -> dict:
    payload = args.get('request') if isinstance(args, dict) else None
    if payload is None and isinstance(args, dict):
        allowed = {'input_path','output_path','key_columns','goal','constraints',
                   'sheet','keep','encoding','delimiter'}
        if set(args) - allowed:
            raise ValueError('unknown task arguments')
        payload = {'schema_version':2, 'event_id':'tool-'+uuid.uuid4().hex,
                   'capability':'tabular.deduplicate', 'goal':args.get('goal'),
                   'constraints':args.get('constraints',[]),
                   'execution':{key:value for key,value in args.items()
                                if key not in {'goal','constraints'}}}
    if not isinstance(payload, dict):
        raise ValueError('request must be a versioned JSON object')
    request = json.loads(json.dumps(payload, allow_nan=False))
    execution = request.get('execution')
    if not isinstance(execution, dict):
        raise ValueError('execution must be a JSON object')
    for key in ('input_path', 'output_path'):
        execution[key] = _workspace_path(execution.get(key), workspace)
    if session_id:
        request['session_id'] = session_id
        if isinstance(request.get('state'), dict):
            request['state']['session_id'] = session_id
    return request


def _compact(result: dict) -> dict:
    if result.get('status') == 'error':
        return {'status': 'rejected', 'error': {
            'code': result.get('error_code', 'harness_error'),
            'message': str(result.get('error', 'Harness rejected the request'))[:1000],
        }}
    return {key: result[key] for key in ('run_id', 'status', 'stage', 'skill_id',
                                        'verification', 'artifact', 'error') if key in result}


def _mark_observer_failure(path: Path, kind: str) -> None:
    data = (json.dumps({'kind': kind, 'time': time.time()}, separators=(',', ':')) + '\n').encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        if os.write(fd, data) != len(data):
            raise OSError('incomplete observer marker write')
        os.fsync(fd)
    finally:
        os.close(fd)


def _execution_identity(payload):
    copied=json.loads(json.dumps(payload,allow_nan=False))
    for key in ('event_id','goal','source_ref','session_id'):
        copied.pop(key,None)
    if isinstance(copied.get('state'),dict):
        for key in ('goal','source_ref','session_id'):
            copied['state'].pop(key,None)
    return copied


def _recorded_contract(payload):
    """Read original normalized semantics without consulting current file facts."""
    state=payload['state'] if payload['schema_version']==1 else payload
    execution=payload['execution']
    params={key:execution.get(key,default) for key,default in
            [('keep','first'),('encoding','utf-8-sig'),('delimiter',','),('sheet',None)]}
    params.update({key:execution[key] for key in ('input_path','output_path','key_columns')})
    return {'capability':state.get('required_capability') if payload['schema_version']==1 else state.get('capability'),
            'contract_version':payload.get('contract_version',1),
            'constraints':sorted(set(state.get('constraints',[]))),'execution':params}


def _strict_replay_payload(payload, contract, store, workspace):
    """Reuse the first durable payload only across harmless metadata changes."""
    from .hermes_gate import contract_for
    with sqlite3.connect(store.path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
            return payload
        row=db.execute("SELECT payload_json FROM events WHERE source='hermes' AND event_id=?",
                       (payload['event_id'],)).fetchone()
    if row is None:
        return payload
    original=json.loads(row[0])
    if contract_for(original,workspace)!=contract:
        raise ValueError('stable entry contract mismatch')

    goal=(payload.get('state') or {}).get('goal') if payload.get('schema_version')==1 else payload.get('goal')
    if (not isinstance(goal,str) or not goal.strip()
            or _execution_identity(original)!=_execution_identity(payload)):
        return payload
    # Preserve the originally admitted goal and provenance in the durable event.
    return original


def _broker_exchange(payload, receipt_id, native):
    """Contact only the parent-owned local task endpoint; never use proxy settings."""
    from urllib.parse import urlsplit
    from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener
    from urllib.error import HTTPError

    url = os.environ.get('RPE_HERMES_BROKER_URL', '')
    token = os.environ.get('RPE_HERMES_BROKER_TOKEN', '')
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or parsed.path != '/task' or parsed.query or parsed.fragment
            or parsed.username is not None or parsed.password is not None
            or parsed.port is None or not 1 <= parsed.port <= 65535
            or not token or len(token) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise ValueError('invalid local executor configuration')
    body = json.dumps({'payload': payload, 'receipt_id': receipt_id, 'native': native},
                      ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if len(body) > 1_048_576:
        raise ValueError('task envelope exceeds the local executor limit')

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    req = Request(url, data=body, headers={'Content-Type': 'application/json',
                  'Authorization': 'Bearer ' + token}, method='POST')
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=125) as response:
            if response.status != 200 or response.headers.get_content_type() != 'application/json':
                raise ValueError('invalid local executor response')
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError('local executor response exceeds limit')
    except HTTPError as error:
        error.close()
        if error.code == 504:
            raise TimeoutError('local executor exceeded its time budget') from None
        raise ValueError('local executor rejected the request') from None
    envelope = json.loads(raw)
    if (not isinstance(envelope, dict) or set(envelope) != {'returncode', 'result'}
            or type(envelope['returncode']) is not int or not isinstance(envelope['result'], dict)):
        raise ValueError('invalid local executor result envelope')
    return envelope


def _execute(args, *, store, workspace, python, task_id=None, session_id=None,
             entry_key=None, required_request=None, input_sha=None, task_attempt=None,
             admission_error=None, replay_result=None) -> str:
    from .hermes_gate import contract_for, digest_json, request_input_sha
    native = {**_CALL_CONTEXT.get(), 'tool_name':'rpe_task',
              'task_id':task_id, 'session_id':session_id}
    if entry_key:
        native['entry_key'] = entry_key
    if task_attempt is not None:
        native['task_attempt'] = task_attempt
    rejection = admission_error
    try:
        payload = _prepare_request(args, workspace, session_id)
        contract = contract_for(payload, workspace)
        contract_sha = digest_json(contract)
        native['contract_sha256'] = contract_sha
        native['input_sha256'] = request_input_sha(payload,workspace)
        if required_request is not None and contract != contract_for(required_request,workspace):
            rejection = rejection or ('request_contract_mismatch','arguments differ from the fixed task contract')
        if input_sha and native['input_sha256'] != input_sha:
            rejection = rejection or ('input_changed','original input hash changed')
        if entry_key:
            payload['event_id'] = 'entry-'+digest_json([entry_key,contract_sha,native['input_sha256']])
            if not rejection:
                payload = _strict_replay_payload(payload,contract,store,workspace)
    except (ValueError, TypeError, OSError):
        rejection = rejection or ('workspace_violation','request must contain valid parameters and '
                                  'regular files inside the configured workspace without symlinks')
    # The receipt and its request hash are durable before starting any executor.
    receipt = store.record('rpe_task_received', native)
    if rejection:
        result = {'status':'rejected', 'error':{'code':rejection[0],'message':rejection[1]}}
    elif replay_result is not None:
        result = _compact(replay_result)
    else:
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parents[2])
        env['PYTHONPATH'] = package_root
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        argv = [python, '-B', '-m', 'rpe_harness', 'events', 'submit', '--event', '-',
                '--db', str(store.path)]
        try:
            if ('RPE_HERMES_BROKER_URL' in os.environ or 'RPE_HERMES_BROKER_TOKEN' in os.environ):
                envelope = _broker_exchange(payload, receipt['ingest_event_id'], native)
                decoded, returncode = envelope['result'], envelope['returncode']
            else:
                child = subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
                                       capture_output=True, text=True, cwd=workspace, env=env, timeout=120)
                decoded, returncode = json.loads(child.stdout), child.returncode
            if (not isinstance(decoded, dict)
                    or decoded.get('status') not in {'succeeded', 'rejected', 'failed', 'interrupted',
                                                    'received', 'running', 'error'}
                    or (decoded.get('status') == 'succeeded' and returncode != 0)):
                raise ValueError('invalid Harness response')
            if decoded['status'] == 'succeeded' and (
                    not isinstance(decoded.get('run_id'), str) or not decoded['run_id']
                    or not isinstance(decoded.get('verification'), dict)
                    or decoded['verification'].get('passed') is not True
                    or not isinstance(decoded.get('artifact'), dict)
                    or decoded['artifact'].get('published') is not True):
                raise ValueError('Harness success has no verified published artifact')
            result = _compact(decoded)
        except (subprocess.TimeoutExpired, TimeoutError):
            result = {'status': 'failed', 'error': {'code': 'harness_timeout',
                      'message': 'Harness execution timed out'}}
        except (ValueError, TypeError):
            result = {'status': 'failed', 'error': {'code': 'harness_protocol_error',
                      'message': 'Harness did not return a valid JSON result'}}
        except OSError:
            result = {'status': 'failed', 'error': {'code': 'harness_launch_error',
                      'message': 'Harness process could not start'}}
    error = result.get('error') or {}
    store.record('rpe_task_returned', {**native, 'receipt_id':receipt['ingest_event_id'],
                 'run_id': result.get('run_id'),
                 'status': result.get('status'), 'error_code': error.get('code')})
    result['receipt_id'] = receipt['ingest_event_id']
    for key in ('contract_sha256','input_sha256'):
        if key in native:
            result[key] = native[key]
    return json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def register(ctx) -> None:
    """Register native observers in both comparison arms and an optional Harness task tool."""
    db = Path(os.environ['RPE_HERMES_DB']).expanduser()
    workspace = Path(os.environ['RPE_HERMES_WORKSPACE']).expanduser()
    if not db.is_absolute() or not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError('explicit absolute DB and existing workspace paths are required')
    workspace = workspace.resolve(strict=True)
    store = HostEventStore(db)
    error_path = Path(os.environ.get('RPE_HERMES_ERROR_PATH',
                                    str(store.path) + '.observer-errors')).expanduser()
    if not error_path.is_absolute():
        raise ValueError('RPE_HERMES_ERROR_PATH must be an absolute path')

    def observer(kind):
        def observe(**payload):
            if kind == 'post_tool_call' and payload.get('tool_name') == 'rpe_task':
                try:
                    result = json.loads(payload.get('result', ''))
                except (ValueError, TypeError):
                    result = None
                if isinstance(result, dict):
                    for key in ('run_id','receipt_id','contract_sha256','input_sha256'):
                        if isinstance(result.get(key),str):
                            payload[key] = result[key]
            try:
                store.record(kind, payload)
            except (sqlite3.Error, OSError, ValueError):
                logger.error('Hermes observer persistence failed for %s', kind)
                try:
                    _mark_observer_failure(error_path, kind)
                except (OSError, ValueError):
                    logger.error('Hermes observer failure marker could not be persisted')
                raise
            return None
        return observe

    for hook in _HOOKS:
        ctx.register_hook(hook, observer(hook))
    enabled = os.environ.get('RPE_HERMES_TOOL_ENABLED', '0')
    if enabled not in ('0', '1'):
        raise ValueError('RPE_HERMES_TOOL_ENABLED must be 0 or 1')
    if enabled == '0':
        return
    python = str(Path(os.environ['RPE_HARNESS_PYTHON']).expanduser().absolute())
    if not Path(python).is_file() or not os.access(python, os.X_OK):
        raise ValueError('RPE_HARNESS_PYTHON must point to an executable Python')

    from .hermes_gate import contract_for, read_run, request_input_sha, _same_call, _verify_run
    entry_mode = os.environ.get('RPE_HERMES_ENTRY_MODE','optional')
    if entry_mode not in ('optional','strict'):
        raise ValueError('entry mode must be optional or strict')
    strict = entry_mode == 'strict'
    entry_key = os.environ.get('RPE_HERMES_ENTRY_KEY') if strict else None
    if strict and not entry_key:
        raise ValueError('strict entry requires a trusted entry key')
    max_attempts = int(os.environ.get('RPE_HERMES_MAX_TASK_ATTEMPTS','3'))
    if not 1 <= max_attempts <= 20:
        raise ValueError('task attempt limit must be between 1 and 20')
    required_request = None
    fixed_path = os.environ.get('RPE_HERMES_REQUIRED_REQUEST_PATH')
    if fixed_path:
        path = Path(fixed_path)
        if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 1_048_576:
            raise ValueError('invalid fixed contract file')
        required_request = _prepare_request({'request':json.loads(path.read_text())},workspace,None)
        contract_for(required_request,workspace)
    input_sha = os.environ.get('RPE_HERMES_INPUT_SHA256')
    if required_request is not None and not input_sha:
        input_sha = request_input_sha(required_request,workspace)
    required_contract_sha=_digest_json(contract_for(required_request,workspace)) if required_request is not None else None
    admission_lock = threading.Lock()
    attempts = {}
    request_policies = {}

    def terminal(session_id, turn_id):
        rows = [e for e in store.list(session_id) if e.get('turn_id')==turn_id]
        for post in reversed(rows):
            if post['kind']!='post_tool_call' or post.get('tool_name')!='rpe_task' or not post.get('run_id'):
                continue
            receipt = next((e for e in rows if e['kind']=='rpe_task_received'
                            and e['ingest_event_id']==post.get('receipt_id')
                            and _same_call(post,e) and e.get('entry_key')==entry_key), None)
            returned = next((e for e in rows if e['kind']=='rpe_task_returned'
                             and e.get('receipt_id')==post.get('receipt_id')
                             and _same_call(post,e) and e.get('run_id')==post.get('run_id')), None)
            if (receipt is None or returned is None or
                    not any(e['kind']=='pre_tool_call' and _same_call(post,e) for e in rows)):
                continue
            try:
                run = read_run(store.path,post['run_id'])
                if (returned.get('status')!=run.get('status') or
                        (run.get('status')=='rejected' and returned.get('error_code')!=
                         (run.get('error') or {}).get('code'))):
                    continue
                contract_sha=receipt.get('contract_sha256')
                original_sha=receipt.get('input_sha256')
                if (not contract_sha or not original_sha or
                        _digest_json(_recorded_contract(run['event']))!=contract_sha or
                        run['event'].get('event_id')!='entry-'+_digest_json([entry_key,contract_sha,original_sha]) or
                        (required_contract_sha is not None and contract_sha!=required_contract_sha) or
                        (input_sha is not None and original_sha!=input_sha)):
                    continue
                # A linked durable terminal outcome closes admission permanently.
                # Later damage to source/output must never authorize reinterpretation.
                if run['status']=='succeeded':
                    verification,artifact=run.get('verification') or {},run.get('artifact') or {}
                    if (verification.get('passed') is not True or artifact.get('published') is not True or
                            verification.get('input_sha256')!=original_sha or artifact.get('input_sha256')!=original_sha):
                        continue
                elif run['status']!='rejected' or not (run.get('error') or {}).get('code'):
                    continue
            except (OSError,ValueError,TypeError,KeyError,AttributeError,sqlite3.Error):
                continue
            return run,post,receipt
        return None

    def policy(request, session_id='', turn_id='', **kwargs):
        if not strict:
            return None
        try:
            rewritten = dict(request)
            rewritten['parallel_tool_calls'] = False
            identity=(session_id,kwargs.get('task_id'),turn_id,kwargs.get('api_request_id'))
            if any(not isinstance(value,str) or not value for value in identity):
                raise ValueError('native API request identity required')
            choice='rpe_task' if terminal(session_id,turn_id) is None else 'none'
            rewritten['tool_choice'] = ({'type':'function','function':{'name':'rpe_task'}}
                                         if choice=='rpe_task' else 'none')
            with admission_lock:
                request_policies[identity]=choice
            return {'request':rewritten,'source':'rpe_harness','reason':'required_entry'}
        except (OSError,ValueError,TypeError,sqlite3.Error):
            _mark_observer_failure(error_path,'llm_request_policy')
            raise

    def bind_call(tool_name, args, next_call, **context):
        if tool_name!='rpe_task':
            return next_call(args)
        real = {key:context.get(key) for key in
                ('session_id','task_id','turn_id','api_request_id','tool_call_id')}
        token = _CALL_CONTEXT.set(real)
        try:
            return next_call(args)
        finally:
            _CALL_CONTEXT.reset(token)

    ctx.register_middleware('tool_execution',bind_call)
    if strict:
        ctx.register_middleware('llm_request',policy)

    def handle(args, task_id=None, session_id=None, **kwargs):
        attempt = None
        admission_error = None
        replay_result = None
        replay_input_sha = input_sha
        if strict:
            identity = _CALL_CONTEXT.get()
            turn_id = identity.get('turn_id')
            if (any(not isinstance(identity.get(key),str) or not identity[key] for key in
                    ('session_id','task_id','turn_id','api_request_id','tool_call_id'))
                    or identity.get('session_id')!=session_id or identity.get('task_id')!=task_id):
                admission_error = ('entry_context_missing','native task execution context required')
            with admission_lock:
                key = (session_id,turn_id)
                policy_key=tuple(identity.get(k) for k in ('session_id','task_id','turn_id','api_request_id'))
                observed=[e for e in store.list(session_id) if e['kind']=='pre_api_request'
                          and tuple(e.get(k) for k in ('session_id','task_id','turn_id','api_request_id'))==policy_key]
                # Actual pre-API policy has priority; an earlier rewrite cannot
                # authorize execution when another middleware changed it to none.
                actual_choice=observed[0].get('request_tool_choice') if len(observed)==1 else None
                recorded_choice=request_policies.get(policy_key)
                if actual_choice=='none' or recorded_choice=='none':
                    admission_error=admission_error or ('entry_policy_none','this API request forbids task execution')
                elif (actual_choice!='rpe_task' or recorded_choice!='rpe_task'
                      or observed[0].get('request_parallel_tool_calls') is not False
                      or observed[0].get('request_tool_names')!=['rpe_task']):
                    admission_error=admission_error or ('entry_policy_missing','complete native API policy required')
                outcome=terminal(session_id,turn_id) if not admission_error else None
                if outcome is not None:
                    run,post,receipt=outcome
                    # Re-delivery of the very same named native call is a read-only
                    # replay. New calls in this turn cannot create another contract.
                    try:
                        candidate=_prepare_request(args,workspace,session_id) if _same_call(identity,post) else None
                        valid_replay=(candidate is not None and
                            _execution_identity(candidate)==_execution_identity(run['event']) and
                            contract_for(candidate,workspace)==contract_for(run['event'],workspace) and
                            request_input_sha(candidate,workspace)==receipt.get('input_sha256'))
                        if valid_replay:
                            # Fresh files decide whether a read-only replay can
                            # report success; failure cannot reopen this turn.
                            _verify_run(run,receipt,workspace,required_request)
                    except (ValueError,TypeError,OSError):
                        valid_replay=False
                    if valid_replay:
                        replay_result=run
                        replay_input_sha=receipt['input_sha256']
                        attempt=receipt.get('task_attempt')
                    else:
                        admission_error=('entry_closed','this turn already has an acceptable durable outcome')
                if replay_result is None:
                    attempts[key] = attempts.get(key,0)+1
                    attempt = attempts[key]
                if replay_result is None and attempt > max_attempts:
                    admission_error = ('task_attempt_limit','bounded task argument attempts exhausted')
        return _execute(args, store=store, workspace=workspace, python=python,
                        task_id=task_id, session_id=session_id,entry_key=entry_key,
                        required_request=required_request,input_sha=replay_input_sha,
                        task_attempt=attempt,admission_error=admission_error,replay_result=replay_result)

    properties = {
        'input_path':{'type':'string','minLength':1},
        'output_path':{'type':'string','minLength':1},
        'key_columns':{'type':'array','minItems':1,'items':{'type':'string','minLength':1}},
        'goal':{'type':'string','minLength':1},
        'constraints':{'type':'array','items':{'type':'string','enum':[
            'preserve_original','preserve_order','preserve_formulas']}},
        'sheet':{'type':'string'},'keep':{'type':'string','enum':['first']},
        'encoding':{'type':'string'},'delimiter':{'type':'string'},
    }
    ctx.register_tool(name='rpe_task', toolset='rpe_harness', handler=handle, schema={
        'name': 'rpe_task',
        'description':'Deduplicate by explicit key columns, keeping first records and original order. '
                      'Pass input_path, a new output_path, key_columns and a short goal. Paths are '
                      'workspace relative. CSV/XLSX outputs are independently verified; unsupported '
                      'formats and unsafe formula dependencies are durably rejected. For XLSX name '
                      'the sheet and use preserve_formulas when required; do not use that constraint '
                      'for CSV. The returned durable status is authoritative.',
        'parameters':{'type':'object','properties':properties,
                      'required':['input_path','output_path','key_columns','goal'],
                      'additionalProperties':False},
    })
