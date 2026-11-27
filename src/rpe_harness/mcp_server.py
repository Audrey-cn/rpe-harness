"""Zero-dependency Model Context Protocol (MCP) JSON-RPC 2.0 server over stdio.

Security boundary:
- Only model-safe read/proposal tools are exposed (`rpe_capabilities`,
  `rpe_start_task`, `rpe_show_task`, `rpe_reconcile_task`).
- Privileged state transitions (`confirm`, `execute`, `cancel`, `handoff`) are
  deliberately NOT exposed to MCP clients. Approval must happen out-of-band in
  the trusted host (CLI, HTTP UI, or host approval callback).
- Trusted `owner`, `session_id`, and `workspace` are bound from server startup
  configuration and cannot be overridden by model tool arguments.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .host_api import HostAPI
from .runtime import default_db_path

PROTOCOL_VERSION = '2024-11-05'
MAX_STDIN_LINE_BYTES = 1024 * 1024

_TOOLS: list[dict[str, Any]] = [
    {
        'name': 'rpe_capabilities',
        'description': (
            'Inspect RPE-Harness host capabilities, available deterministic contracts, '
            'sandbox backend, and trust boundary flags.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False,
        },
    },
    {
        'name': 'rpe_start_task',
        'description': (
            'Create a reviewable task proposal and frozen digest without confirming or '
            'executing it. The user/host must explicitly confirm and execute out-of-band.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'task': {
                    'type': 'string',
                    'description': 'Natural-language task description or structured instruction.',
                },
                'draft': {
                    'type': 'object',
                    'description': 'Optional structured draft fields for the task.',
                },
            },
            'required': ['task'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'rpe_show_task',
        'description': 'Inspect the current status, digest, confirmation receipt, and artifact of an operation.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'operation_id': {
                    'type': 'string',
                    'description': 'The operation_id returned by rpe_start_task.',
                },
            },
            'required': ['operation_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'rpe_reconcile_task',
        'description': 'Reconcile and verify durable task state, run linkage, and recovery status.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'operation_id': {
                    'type': 'string',
                    'description': 'The operation_id to reconcile.',
                },
            },
            'required': ['operation_id'],
            'additionalProperties': False,
        },
    },
]


class StdioMCPServer:
    """Pure stdlib JSON-RPC 2.0 MCP server bound to one trusted host identity."""

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        owner: str = 'mcp-host',
        session_id: str = 'mcp-session',
        workspace: Path | str | None = None,
        catalog_root: Path | str | None = None,
        skill_snapshot_id: str | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve() if db_path else default_db_path()
        self.owner = owner
        self.session_id = session_id
        self.workspace = Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve()
        self.catalog_root = Path(catalog_root).expanduser().absolute() if catalog_root is not None else None
        self.skill_snapshot_id = skill_snapshot_id
        self.api = HostAPI(self.db_path)

    def _tool_result(self, payload: Any, *, is_error: bool = False) -> dict[str, Any]:
        return {
            'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}],
            'structuredContent': payload,
            'isError': is_error,
        }

    def _call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return self._tool_result({'error': 'tool arguments must be a JSON object'}, is_error=True)

        if name == 'rpe_capabilities':
            if arguments:
                return self._tool_result({'error': 'rpe_capabilities accepts no arguments'}, is_error=True)
            return self._tool_result(self.api.capabilities())

        if name == 'rpe_start_task':
            forbidden_keys = set(arguments) - {'task', 'draft'}
            if forbidden_keys:
                return self._tool_result(
                    {'error': f'unsupported or privileged arguments rejected: {sorted(forbidden_keys)}'},
                    is_error=True,
                )
            task = arguments.get('task')
            if not isinstance(task, str) or not task.strip():
                return self._tool_result({'error': 'task must be a nonempty string'}, is_error=True)
            req: dict[str, Any] = {'task': task}
            if self.catalog_root is not None:
                req['catalog_root'] = str(self.catalog_root)
            if self.skill_snapshot_id is not None:
                req['skill_snapshot_id'] = self.skill_snapshot_id
            if 'draft' in arguments:
                if not isinstance(arguments['draft'], dict):
                    return self._tool_result({'error': 'draft must be a JSON object'}, is_error=True)
                req['draft'] = arguments['draft']
            host_context = {
                'owner': self.owner,
                'session_id': self.session_id,
                'workspace': str(self.workspace),
                'source_ref': 'mcp-stdio',
            }
            result = self.api.start_task(req, host_context)
            return self._tool_result(result)

        if name == 'rpe_show_task':
            if set(arguments) != {'operation_id'} or not isinstance(arguments.get('operation_id'), str):
                return self._tool_result({'error': 'operation_id string is required'}, is_error=True)
            result = self.api.show_task(arguments['operation_id'], self.owner)
            return self._tool_result(result, is_error=(result.get('status') == 'forbidden'))

        if name == 'rpe_reconcile_task':
            if set(arguments) != {'operation_id'} or not isinstance(arguments.get('operation_id'), str):
                return self._tool_result({'error': 'operation_id string is required'}, is_error=True)
            result = self.api.reconcile_task(arguments['operation_id'], self.owner)
            return self._tool_result(result, is_error=(result.get('status') == 'forbidden'))

        raise KeyError(f'tool not available over MCP (privileged transitions stay host-side): {name}')

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            return {
                'jsonrpc': '2.0',
                'id': message.get('id') if isinstance(message, dict) else None,
                'error': {'code': -32600, 'message': 'Invalid JSON-RPC 2.0 request'},
            }
        msg_id = message.get('id')
        method = message.get('method')
        params = message.get('params') or {}

        # Notifications have no id and expect no response.
        if msg_id is None and isinstance(method, str) and method.startswith('notifications/'):
            return None

        if method == 'initialize':
            client_version = params.get('protocolVersion') if isinstance(params, dict) else None
            return {
                'jsonrpc': '2.0',
                'id': msg_id,
                'result': {
                    'protocolVersion': client_version or PROTOCOL_VERSION,
                    'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'rpe-harness', 'version': __version__},
                },
            }

        if method == 'ping':
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': {}}

        if method == 'tools/list':
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': {'tools': _TOOLS}}

        if method == 'tools/call':
            if not isinstance(params, dict) or not isinstance(params.get('name'), str):
                return {
                    'jsonrpc': '2.0',
                    'id': msg_id,
                    'error': {'code': -32602, 'message': 'tools/call requires string name'},
                }
            try:
                tool_res = self._call_tool(params['name'], params.get('arguments'))
                return {'jsonrpc': '2.0', 'id': msg_id, 'result': tool_res}
            except KeyError as exc:
                return {
                    'jsonrpc': '2.0',
                    'id': msg_id,
                    'error': {'code': -32601, 'message': str(exc)},
                }
            except (ValueError, TypeError, OSError) as exc:
                return {
                    'jsonrpc': '2.0',
                    'id': msg_id,
                    'result': self._tool_result({'error': str(exc)}, is_error=True),
                }

        return {
            'jsonrpc': '2.0',
            'id': msg_id,
            'error': {'code': -32601, 'message': f'Method not found: {method}'},
        }

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
        while True:
            # TextIO limits characters, so also enforce the UTF-8 byte budget.
            # Drain an oversized record in bounded chunks before accepting the
            # next record; one hostile line must produce only one parse error.
            raw_line = stdin.readline(MAX_STDIN_LINE_BYTES + 1)
            if not raw_line:
                break
            try:
                if (len(raw_line) > MAX_STDIN_LINE_BYTES or
                        len(raw_line.encode('utf-8')) > MAX_STDIN_LINE_BYTES):
                    while raw_line and not raw_line.endswith('\n'):
                        raw_line = stdin.readline(MAX_STDIN_LINE_BYTES + 1)
                    raise ValueError('stdin line exceeds 1 MiB')
                line = raw_line.strip()
                if not line:
                    continue
                message = json.loads(line)
                response = self.handle_message(message)
            except (json.JSONDecodeError, RecursionError, ValueError) as exc:
                response: dict[str, Any] | None = {
                    'jsonrpc': '2.0',
                    'id': None,
                    'error': {'code': -32700, 'message': f'Parse error: {exc}'},
                }
            if response is not None:
                # Escaping also makes unpaired surrogate IDs safe on UTF-8 stdio.
                stdout.write(json.dumps(response, ensure_ascii=True) + '\n')
                stdout.flush()
        return 0


def register_mcp(subparsers) -> None:
    mcp = subparsers.add_parser(
        'mcp',
        help='run zero-dependency stdio MCP server exposing model-safe proposal/read tools only',
    )
    mcp.add_argument('--db', help='SQLite path; default ~/.local/share/rpe-harness/runs.sqlite')
    mcp.add_argument('--owner', default=os.environ.get('RPE_MCP_OWNER', 'mcp-host'),
                     help='trusted host owner bound to this MCP server process')
    mcp.add_argument('--session-id', default=os.environ.get('RPE_MCP_SESSION_ID', 'mcp-session'),
                     help='trusted host session_id bound to this MCP server process')
    mcp.add_argument('--workspace', default=os.environ.get('RPE_MCP_WORKSPACE'),
                     help='trusted host workspace directory (default: current working directory)')
    mcp.add_argument('--catalog-root', help='trusted local skill catalog root')
    mcp.add_argument('--skill-snapshot-id', help='trusted skill snapshot SHA256')


def dispatch_mcp(args) -> int:
    server = StdioMCPServer(
        db_path=args.db,
        owner=args.owner,
        session_id=args.session_id,
        workspace=args.workspace,
        catalog_root=args.catalog_root,
        skill_snapshot_id=args.skill_snapshot_id,
    )
    return server.serve()
