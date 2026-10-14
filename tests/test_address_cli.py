import json
from types import SimpleNamespace

import pytest


def test_cli_catalog_and_minimal_prepare_show_share_snapshot(tmp_path, capsys):
    from rpe_harness.cli import main
    source = tmp_path / 'input.json'
    source.write_text('{ "x": [1, 2] }')
    extracted = tmp_path / 'extracted.json'
    extracted.write_text(json.dumps({'goal': '紧凑格式化 JSON', 'capability': 'json.normalize',
        'constraints': ['preserve_original'], 'execution': {'input_path': str(source),
        'output_path': str(tmp_path / 'out.json'), 'style': 'minify'}}))
    db = tmp_path / 'state.sqlite'
    assert main(['capabilities', 'resolve', '--capability', 'json.normalize']) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert main(['intent', 'prepare', '--db', str(db), '--workspace', str(tmp_path),
                 '--session-id', 's', '--source-ref', 'prompt:123', '--extracted', str(extracted)]) == 0
    handoff = json.loads(capsys.readouterr().out)
    assert set(handoff) == {'state_id', 'capability', 'constraints', 'catalog_sha256', 'status'}
    assert handoff['catalog_sha256'] == catalog['catalog_sha256']
    assert not (tmp_path / 'out.json').exists()
    assert main(['intent', 'show', handoff['state_id'], '--db', str(db), '--session-id', 's']) == 0
    assert json.loads(capsys.readouterr().out) == handoff
    assert main(['intent', 'show', handoff['state_id'], '--db', str(db), '--session-id', 'other']) == 2


def test_cli_needs_context_and_unsupported_have_distinct_nonzero_codes(tmp_path, capsys):
    from rpe_harness.cli import main
    assert main(['capabilities', 'resolve']) == 3
    assert json.loads(capsys.readouterr().out)['status'] == 'needs_context'
    assert main(['capabilities', 'resolve', '--capability', 'json.normalize',
                 '--constraint', 'preserve_formulas']) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'unsupported'


def test_http_public_entry_uses_same_catalog_without_running_business_task(tmp_path):
    from rpe_harness.server import HarnessHandler
    from rpe_harness.runtime import HarnessRuntime
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import MinimalStateStore
    runtime = HarnessRuntime(db_path=tmp_path / 'state.sqlite')
    handler = HarnessHandler.__new__(HarnessHandler)
    handler.server = SimpleNamespace(runtime=runtime, catalog_root=None, skill_snapshot_id=None)
    handler.path = '/capabilities'
    code, listing = handler._handle(None)
    assert code == 200 and listing['catalog_sha256'] == CapabilityCatalog().snapshot_id
    handler.path = '/capabilities/resolve'
    assert handler._handle({'capability': 'json.normalize'})[1]['status'] == 'ready'
    with pytest.raises(ValueError):
        handler._handle({'capability': 'json.normalize', 'unknown': True})
    source = tmp_path / 'input.json'
    source.write_text('[1, 2]')
    handler.path = '/intent/prepare'
    code, handoff = handler._handle({'workspace': str(tmp_path), 'session_id': 's', 'source_ref': 'r',
        'extracted': {'goal': 'compact', 'capability': 'json.normalize', 'constraints': [],
        'execution': {'input_path': str(source), 'output_path': str(tmp_path / 'out.json'), 'style': 'minify'}}})
    assert code == 200 and set(handoff) == {'state_id', 'capability', 'constraints', 'catalog_sha256', 'status'}
    assert not (tmp_path / 'out.json').exists()
    anchored = MinimalStateStore(runtime.store.path).resolve(handoff['state_id'], session_id='s',
                                                               catalog_sha256=listing['catalog_sha256'])
    assert anchored['request']['capability'] == 'json.normalize'
