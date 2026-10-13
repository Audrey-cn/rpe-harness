"""Native Hermes plugin bridge with durable, metadata-only observations."""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TEXT_FIELDS = frozenset({
    'session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id', 'tool_name',
    'model', 'provider', 'response_model', 'api_mode', 'platform', 'status', 'run_id',
    'error_type', 'error_code', 'turn_exit_reason', 'telemetry_schema_version',
    'finish_reason', 'parent_session_id',
})
_NUMBER_FIELDS = frozenset({
    'duration_ms', 'api_duration', 'api_call_count', 'started_at', 'ended_at',
    'first_chunk_at', 'message_count', 'assistant_content_chars', 'assistant_tool_call_count',
})
_BOOL_FIELDS = frozenset({'completed', 'failed', 'interrupted', 'is_first_turn'})
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
    if not isinstance(payload, dict):
        raise ValueError('request must be a v1 JSON object')
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


def _execute(args, *, store, workspace, python, task_id=None, session_id=None) -> str:
    native = {'tool_name': 'rpe_task', 'task_id': task_id, 'session_id': session_id}
    # Failure to persist receipt propagates; no child may execute without durable admission.
    receipt = store.record('rpe_task_received', native)
    try:
        payload = _prepare_request(args, workspace, session_id)
    except (ValueError, TypeError, OSError):
        result = {'status': 'rejected', 'error': {'code': 'workspace_violation',
                  'message': 'request paths must be valid files inside the configured workspace '
                             'without symlinks'}}
    else:
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parents[2])
        env['PYTHONPATH'] = package_root
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        argv = [python, '-B', '-m', 'rpe_harness', 'events', 'submit', '--event', '-',
                '--db', str(store.path)]
        try:
            child = subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
                                   capture_output=True, text=True, cwd=workspace, env=env, timeout=120)
            decoded = json.loads(child.stdout)
            if (not isinstance(decoded, dict)
                    or decoded.get('status') not in {'succeeded', 'rejected', 'failed', 'interrupted',
                                                    'received', 'running', 'error'}
                    or (decoded.get('status') == 'succeeded' and child.returncode != 0)):
                raise ValueError('invalid Harness response')
            if decoded['status'] == 'succeeded' and (
                    not isinstance(decoded.get('run_id'), str) or not decoded['run_id']
                    or not isinstance(decoded.get('verification'), dict)
                    or decoded['verification'].get('passed') is not True
                    or not isinstance(decoded.get('artifact'), dict)
                    or decoded['artifact'].get('published') is not True):
                raise ValueError('Harness success has no verified published artifact')
            result = _compact(decoded)
        except subprocess.TimeoutExpired:
            result = {'status': 'failed', 'error': {'code': 'harness_timeout',
                      'message': 'Harness execution timed out'}}
        except (ValueError, TypeError):
            result = {'status': 'failed', 'error': {'code': 'harness_protocol_error',
                      'message': 'Harness did not return a valid JSON result'}}
        except OSError:
            result = {'status': 'failed', 'error': {'code': 'harness_launch_error',
                      'message': 'Harness process could not start'}}
    error = result.get('error') or {}
    store.record('rpe_task_returned', {**native, 'run_id': result.get('run_id'),
                 'status': result.get('status'), 'error_code': error.get('code')})
    result['receipt_id'] = receipt['ingest_event_id']
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
                if isinstance(result, dict) and isinstance(result.get('run_id'), str):
                    payload['run_id'] = result['run_id']
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

    def handle(args, task_id=None, session_id=None, **kwargs):
        return _execute(args, store=store, workspace=workspace, python=python,
                        task_id=task_id, session_id=session_id)

    request_schema = {
        'type': 'object',
        'properties': {
            'schema_version': {'type': 'integer', 'const': 1},
            'event_id': {'type': 'string', 'minLength': 1},
            'session_id': {'type': 'string'},
            'state': {
                'type': 'object',
                'properties': {
                    'task_type': {'type': 'string', 'enum': ['data.processing', 'file.operation']},
                    'goal': {'type': 'string', 'minLength': 1},
                    'input_format': {'type': 'string', 'minLength': 1,
                                     'description': 'Input extension; supported execution formats '
                                                    'are csv and xlsx. Unsupported formats are rejected.'},
                    'required_capability': {'type': 'string', 'const': 'tabular.deduplicate'},
                    'constraints': {'type': 'array', 'items': {
                        'type': 'string', 'enum': ['preserve_original', 'preserve_order',
                                                  'preserve_formulas']}},
                    'session_id': {'type': 'string'},
                },
                'required': ['task_type', 'goal', 'input_format', 'required_capability'],
                'additionalProperties': False,
            },
            'execution': {
                'type': 'object',
                'properties': {
                    'input_path': {'type': 'string', 'minLength': 1},
                    'output_path': {'type': 'string', 'minLength': 1},
                    'key_columns': {'type': 'array', 'minItems': 1,
                                    'items': {'type': 'string', 'minLength': 1}},
                    'keep': {'type': 'string', 'enum': ['first']},
                    'encoding': {'type': 'string'},
                    'delimiter': {'type': 'string'},
                    'sheet': {'type': 'string'},
                },
                'required': ['input_path', 'output_path', 'key_columns'],
                'additionalProperties': False,
            },
        },
        'required': ['schema_version', 'event_id', 'state', 'execution'],
        'additionalProperties': False,
    }
    ctx.register_tool(name='rpe_task', toolset='rpe_harness', handler=handle, schema={
        'name': 'rpe_task',
        'description': 'Deduplicate a CSV or XLSX by explicit key columns, preserving the first '
                       'records and original input. Verifies the new output. Pass a version 1 '
                       'request with schema_version=1, a unique event_id, state (task_type '
                       'data.processing, nonempty goal, input_format csv or xlsx, required_capability '
                       'tabular.deduplicate, constraints as an array of strings), and execution '
                       '(input_path, output_path, '
                       'key_columns; optional sheet and keep=first). Paths are workspace relative.',
        'parameters': {'type': 'object', 'properties': {'request': request_schema},
                       'required': ['request'], 'additionalProperties': False},
    })
