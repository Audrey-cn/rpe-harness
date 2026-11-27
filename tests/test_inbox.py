"""Tests for local approval inbox (CLI + loopback Web UI + CSRF protection)."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from rpe_harness.host_api import HostAPI
from rpe_harness.inbox import InboxHTTPServer, approve_and_execute, list_pending, reject_operation
from rpe_harness.mcp_server import StdioMCPServer


def _seed_pending(tmp_path: Path, out_name: str = 'out.json') -> tuple[Path, dict]:
    db = tmp_path / 'runs.sqlite'
    src = tmp_path / 'input.json'
    if not src.exists():
        src.write_text('{ "hello" : [1, 2] }\n', encoding='utf-8')
    mcp = StdioMCPServer(db_path=db, owner='alice', session_id='s1', workspace=tmp_path)
    res = mcp.handle_message({
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {
            'name': 'rpe_start_task',
            'arguments': {
                'task': f'Normalize input.json to {out_name}, minify and preserve all values.',
                'draft': {
                    'capability': 'json.normalize',
                    'constraints': [],
                    'execution': {'input_path': 'input.json', 'output_path': out_name, 'style': 'minify'},
                    'requirements': [
                        {'text': 'Normalize', 'quote': 'Normalize', 'fields': ['/capability']},
                        {'text': 'Input', 'quote': 'input.json', 'fields': ['/execution/input_path']},
                        {'text': 'Output', 'quote': out_name, 'fields': ['/execution/output_path']},
                        {'text': 'Minify', 'quote': 'minify', 'fields': ['/execution/style']},
                        {'text': 'No extra', 'quote': None, 'fields': ['/constraints']},
                    ],
                    'unresolved': [],
                },
            },
        },
    })
    return db, res['result']['structuredContent']


def test_inbox_list_approve_and_reject_helpers(tmp_path: Path):
    db, op1 = _seed_pending(tmp_path, 'out1.json')
    _, op2 = _seed_pending(tmp_path, 'out2.json')
    api = HostAPI(db)

    pending = list_pending(api, owner='alice')
    assert {p['operation_id'] for p in pending} == {op1['operation_id'], op2['operation_id']}

    done = approve_and_execute(
        api,
        operation_id=op1['operation_id'],
        owner='alice',
        digest=op1['digest'],
        generation=op1['generation'],
    )
    assert done['status'] == 'succeeded'
    assert (tmp_path / 'out1.json').read_text(encoding='utf-8').strip() == '{"hello":[1,2]}'

    cancelled = reject_operation(
        api,
        operation_id=op2['operation_id'],
        owner='alice',
        generation=op2['generation'],
    )
    assert cancelled['status'] == 'cancelled'
    assert not (tmp_path / 'out2.json').exists()
    assert list_pending(api, owner='alice') == []


def test_inbox_web_server_enforces_loopback_and_csrf_and_executes(tmp_path: Path):
    db, op = _seed_pending(tmp_path, 'web_out.json')
    api = HostAPI(db)

    with pytest.raises(ValueError, match='loopback'):
        InboxHTTPServer(('0.0.0.0', 0), api)

    server = InboxHTTPServer(('127.0.0.1', 0), api, owner='alice')
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection('127.0.0.1', port, timeout=10)
        conn.request('GET', '/')
        r_html = conn.getresponse()
        body = r_html.read().decode('utf-8')
        assert r_html.status == 200
        assert 'Local Approval Inbox' in body

        conn.request('GET', '/api/pending')
        r_list = conn.getresponse()
        items = json.loads(r_list.read().decode('utf-8'))['pending']
        assert len(items) == 1 and items[0]['operation_id'] == op['operation_id']

        # Missing CSRF must return 403
        payload = json.dumps({
            'operation_id': op['operation_id'],
            'owner': 'alice',
            'digest': op['digest'],
            'generation': op['generation'],
        })
        conn.request('POST', '/api/approve', body=payload, headers={'Content-Type': 'application/json'})
        r_forbid = conn.getresponse()
        r_forbid.read()
        assert r_forbid.status == 403

        # Valid CSRF approves and executes
        conn.request(
            'POST', '/api/approve', body=payload,
            headers={'Content-Type': 'application/json', 'X-RPE-CSRF': server.csrf_token},
        )
        r_ok = conn.getresponse()
        approved = json.loads(r_ok.read().decode('utf-8'))
        assert r_ok.status == 200
        assert approved['status'] == 'succeeded'
        assert (tmp_path / 'web_out.json').exists()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
