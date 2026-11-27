"""G2 Gate tests for cross-host handoff, source re-authorization, and confirmation invalidation."""
from __future__ import annotations

import json
import subprocess
import sys

from rpe_harness.host_api import HostAPI, TrustedHostAction


def _start_op(api, workspace, owner='hermes-alice', session_id='s-hermes', host_id='hermes', filename='input.json'):
    src = workspace / filename
    src.write_text('{ "name" : [1, 2] }', encoding='utf-8')
    raw = f'Normalize {filename} to out.json, minify and preserve all values.'
    draft = {
        'capability': 'json.normalize',
        'constraints': [],
        'execution': {'input_path': filename, 'output_path': 'out.json', 'style': 'minify'},
        'requirements': [
            {'text': 'Normalize JSON', 'quote': 'Normalize', 'fields': ['/capability']},
            {'text': f'Read {filename}', 'quote': filename, 'fields': ['/execution/input_path']},
            {'text': 'Write out.json', 'quote': 'out.json', 'fields': ['/execution/output_path']},
            {'text': 'Minify', 'quote': 'minify', 'fields': ['/execution/style']},
            {'text': 'No extra constraints', 'quote': None, 'fields': ['/constraints']},
        ],
        'unresolved': [],
    }
    return api.start_task(
        {'raw': raw, 'draft': draft},
        {'owner': owner, 'session_id': session_id, 'workspace': str(workspace), 'host_id': host_id},
    ), src


def test_cross_host_handoff_preserves_lineage_remaps_paths_and_invalidates_prior_confirmation(tmp_path):
    ws_a = tmp_path / 'host_a'
    ws_b = tmp_path / 'host_b'
    ws_a.mkdir()
    ws_b.mkdir()
    api = HostAPI(tmp_path / 'state.sqlite')

    op_a, src_a = _start_op(api, ws_a, filename='input.json')
    (ws_b / 'migrated.json').write_text(src_a.read_text(encoding='utf-8'), encoding='utf-8')

    # Confirm on Host A
    confirm_a = api.confirm_task(
        op_a['operation_id'],
        op_a['digest'],
        TrustedHostAction(owner='hermes-alice', operation_id=op_a['operation_id'],
                          generation=op_a['generation'], digest=op_a['digest'], actor='alice'),
    )
    assert confirm_a['confirmation_id']

    # Hand off to DSH (Host B) with path remapping and re-authorized sources
    op_b = api.handoff_task(
        op_a['operation_id'],
        'hermes-alice',
        {'owner': 'dsh-bob', 'session_id': 's-dsh', 'workspace': str(ws_b), 'host_id': 'dsh'},
        authorized_sources=[{'source_id': 'doc-1', 'authorized': True, 'session_id': 's-dsh', 'owner': 'dsh-bob'}],
        path_mapping={'input.json': 'migrated.json'},
    )
    assert op_b['operation_id'] != op_a['operation_id']
    assert op_b['owner'] == 'dsh-bob'
    assert op_b['generation'] == 1
    assert op_b['confirmation_id'] is None
    assert op_b['draft']['execution']['input_path'] == 'migrated.json'
    assert 'migrated.json' in op_b['source']['raw']
    lineage = op_b['handoff_lineage']
    assert lineage['handoff_from_operation_id'] == op_a['operation_id']
    assert lineage['handoff_from_host'] == 'hermes'
    assert lineage['prior_confirmation_invalidated'] is True

    # G2 negative #2: replaying Host A's digest/confirmation on Host B must fail
    replay = api.confirm_task(
        op_b['operation_id'],
        op_a['digest'],
        TrustedHostAction(owner='dsh-bob', operation_id=op_b['operation_id'],
                          generation=1, digest=op_a['digest'], actor='bob'),
    )
    assert replay['status'] == 'stale'

    # Host A owner cannot inspect Host B operation
    assert api.show_task(op_b['operation_id'], 'hermes-alice') == {'status': 'forbidden'}


def test_cross_host_handoff_rejects_unauthorized_revoked_and_cross_session_sources(tmp_path):
    api = HostAPI(tmp_path / 'state.sqlite')
    op_a, _ = _start_op(api, tmp_path)
    target = {'owner': 'dsh-bob', 'session_id': 's-dsh', 'workspace': str(tmp_path), 'host_id': 'dsh'}

    for bad_source in (
        [{'source_id': 's1', 'revoked': True, 'session_id': 's-dsh'}],
        [{'source_id': 's2', 'authorized': False, 'session_id': 's-dsh'}],
        [{'source_id': 's3', 'authorized': True, 'session_id': 's-hermes'}],
        [{'source_id': 's4', 'authorized': True, 'session_id': 's-dsh', 'owner': 'hermes-alice'}],
    ):
        assert api.handoff_task(op_a['operation_id'], 'hermes-alice', target,
                                authorized_sources=bad_source) == {'status': 'forbidden'}

    assert api.handoff_task(op_a['operation_id'], 'intruder', target) == {'status': 'forbidden'}


def test_cli_host_handoff_roundtrip(tmp_path):
    db = tmp_path / 'runs.sqlite'
    api = HostAPI(db)
    op_a, _ = _start_op(api, tmp_path)
    ctx_file = tmp_path / 'target.json'
    ctx_file.write_text(json.dumps({
        'owner': 'dsh-bob', 'session_id': 's-dsh', 'workspace': str(tmp_path), 'host_id': 'dsh',
    }), encoding='utf-8')
    completed = subprocess.run(
        [sys.executable, '-B', '-m', 'rpe_harness', 'host', 'handoff', op_a['operation_id'],
         '--source-owner', 'hermes-alice', '--target-context', str(ctx_file), '--db', str(db)],
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload['owner'] == 'dsh-bob'
    assert payload['confirmation_id'] is None
    assert payload['handoff_lineage']['handoff_from_operation_id'] == op_a['operation_id']
