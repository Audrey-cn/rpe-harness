"""Tests for zero-dependency stdio MCP server and host trust boundaries."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from rpe_harness.host_api import HostAPI, TrustedHostAction
from rpe_harness.mcp_server import StdioMCPServer


def _rpc(server: StdioMCPServer, msg: dict) -> dict | None:
    return server.handle_message(msg)


def test_mcp_initialize_and_tool_surface_excludes_privileged_actions(tmp_path: Path):
    server = StdioMCPServer(db_path=tmp_path / 'runs.sqlite', owner='alice',
                            session_id='sess-1', workspace=tmp_path)
    init = _rpc(server, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                         'params': {'protocolVersion': '2024-11-05'}})
    assert init['result']['serverInfo']['name'] == 'rpe-harness'
    assert init['result']['capabilities'] == {'tools': {}}

    # Notification yields no response
    assert _rpc(server, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None
    assert _rpc(server, {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})['result'] == {}

    listed = _rpc(server, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})
    names = {tool['name'] for tool in listed['result']['tools']}
    assert names == {'rpe_capabilities', 'rpe_start_task', 'rpe_show_task', 'rpe_reconcile_task'}
    for forbidden in ('confirm', 'execute', 'cancel', 'handoff', 'rpe_confirm_task', 'rpe_execute_task'):
        assert forbidden not in names
        err = _rpc(server, {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                            'params': {'name': forbidden, 'arguments': {}}})
        assert err['error']['code'] == -32601


def test_mcp_rejects_owner_spoofing_and_enforces_host_side_approval(tmp_path: Path):
    db = tmp_path / 'runs.sqlite'
    src = tmp_path / 'input.json'
    out = tmp_path / 'out.json'
    src.write_text('{ "name" : [1, 2] }\n', encoding='utf-8')

    server = StdioMCPServer(db_path=db, owner='alice', session_id='sess-1', workspace=tmp_path)

    # Spoofing owner/session_id/workspace in tool arguments must be rejected
    spoof = _rpc(server, {
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'rpe_start_task', 'arguments': {
            'task': 'Normalize input.json to out.json, minify.', 'owner': 'bob',
        }},
    })
    assert spoof['result']['isError'] is True

    # Capabilities check
    caps = _rpc(server, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                         'params': {'name': 'rpe_capabilities', 'arguments': {}}})
    assert caps['result']['isError'] is False
    cap_data = caps['result']['structuredContent']
    assert cap_data['mcp_stdio'] is True
    assert cap_data['automatic_confirmation'] is False
    assert 'tabular.filter' in cap_data['contracts']
    assert 'json.patch' in cap_data['contracts']

    # Start task via MCP -> only creates reviewable proposal, never executes
    started = _rpc(server, {
        'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
        'params': {
            'name': 'rpe_start_task',
            'arguments': {
                'task': 'Normalize input.json to out.json, minify and preserve all values.',
                'draft': {
                    'capability': 'json.normalize',
                    'constraints': [],
                    'execution': {'input_path': 'input.json', 'output_path': 'out.json', 'style': 'minify'},
                    'requirements': [
                        {'text': 'Normalize JSON', 'quote': 'Normalize', 'fields': ['/capability']},
                        {'text': 'Read input.json', 'quote': 'input.json', 'fields': ['/execution/input_path']},
                        {'text': 'Write out.json', 'quote': 'out.json', 'fields': ['/execution/output_path']},
                        {'text': 'Minify', 'quote': 'minify', 'fields': ['/execution/style']},
                        {'text': 'No extra constraints', 'quote': None, 'fields': ['/constraints']},
                    ],
                    'unresolved': [],
                },
            },
        },
    })
    assert started['result']['isError'] is False
    op = started['result']['structuredContent']
    assert op['owner'] == 'alice'
    assert op['status'] == 'awaiting_confirmation'
    assert not out.exists()

    # Trusted host confirms & executes out-of-band
    api = HostAPI(db)
    confirmed = api.confirm_task(
        op['operation_id'], op['digest'],
        TrustedHostAction(owner='alice', operation_id=op['operation_id'],
                          generation=op['generation'], digest=op['digest']),
    )
    assert confirmed['status'] == 'confirmed'
    executed = api.execute_task(
        op['operation_id'], 'alice', confirmation_id=confirmed['confirmation_id'],
    )
    assert executed['status'] == 'succeeded', executed
    assert out.exists()
    assert out.read_text(encoding='utf-8').strip() == '{"name":[1,2]}'

    # MCP can observe final state and reconcile
    shown = _rpc(server, {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                          'params': {'name': 'rpe_show_task', 'arguments': {'operation_id': op['operation_id']}}})
    assert shown['result']['structuredContent']['status'] == 'succeeded'
    reconciled = _rpc(server, {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                               'params': {'name': 'rpe_reconcile_task', 'arguments': {'operation_id': op['operation_id']}}})
    assert reconciled['result']['structuredContent']['status'] == 'succeeded'

    # Different owner cannot see alice's task
    bob_server = StdioMCPServer(db_path=db, owner='bob', session_id='sess-2', workspace=tmp_path)
    denied = _rpc(bob_server, {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
                               'params': {'name': 'rpe_show_task', 'arguments': {'operation_id': op['operation_id']}}})
    assert denied['result']['isError'] is True
    assert denied['result']['structuredContent']['status'] == 'forbidden'


def test_mcp_cli_subprocess_stdio_roundtrip(tmp_path: Path):
    db = tmp_path / 'cli_mcp.sqlite'
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'rpe_capabilities', 'arguments': {}}},
    ]
    stdin_payload = '\n'.join(json.dumps(r) for r in requests) + '\n'
    proc = subprocess.run(
        [sys.executable, '-m', 'rpe_harness', 'mcp',
         '--db', str(db), '--owner', 'cli-host', '--session-id', 'cli-sess', '--workspace', str(tmp_path)],
        input=stdin_payload,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    assert [m['id'] for m in lines] == [1, 2, 3]
    assert len(lines[1]['result']['tools']) == 4
    assert lines[2]['result']['structuredContent']['mcp_stdio'] is True


def test_mcp_serve_handles_malformed_json_gracefully(tmp_path: Path):
    server = StdioMCPServer(db_path=tmp_path / 'err.sqlite', workspace=tmp_path)
    inp = io.StringIO('not-json\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    out = io.StringIO()
    assert server.serve(stdin=inp, stdout=out) == 0
    responses = [json.loads(line) for line in out.getvalue().strip().splitlines()]
    assert responses[0]['error']['code'] == -32700
    assert responses[1]['result'] == {}
