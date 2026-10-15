import pytest


def candidate(encoding):
    raw = 'Deduplicate input.csv by id, keep first, comma delimiter, UTF-8 encoding; save output.csv and preserve original.'
    execution = {'input_path': 'input.csv', 'output_path': 'output.csv',
                 'key_columns': ['id'], 'encoding': encoding, 'delimiter': ',', 'keep': 'first'}
    return raw, {'capability': 'tabular.deduplicate', 'constraints': ['preserve_original'],
        'execution': execution, 'requirements': [
            {'text': 'Current task', 'quote': raw,
             'fields': ['/capability', '/constraints', *['/execution/'+key for key in execution]]}],
        'unresolved': []}


def test_values_profile_blocks_conflicting_default_before_confirmation(tmp_path):
    from rpe_harness.proposal import create_proposal, ProposalStore
    (tmp_path/'input.csv').write_text('id,x\n001,a\n001,b\n')
    raw, draft = candidate('utf-8-sig')
    result = create_proposal(raw, draft, db_path=tmp_path/'s.db', workspace=tmp_path,
        session_id='s', source_ref='user', value_evidence=True)
    assert result['status'] == 'needs_context'
    assert result['parameter_evidence']['errors']
    assert result['draft']['execution']['encoding'] == 'utf-8-sig'
    with pytest.raises(ValueError):
        ProposalStore(tmp_path/'s.db').confirm(result['proposal_id'], session_id='s',
            expected_proposal_sha256=result['proposal_sha256'], actor='test-reviewer')
    assert not (tmp_path/'output.csv').exists()


def test_values_profile_persists_and_rechecks_valid_literal_values(tmp_path):
    from rpe_harness.proposal import create_proposal, ProposalStore
    (tmp_path/'input.csv').write_text('id,x\n001,a\n001,b\n')
    raw, draft = candidate('utf-8')
    result = create_proposal(raw, draft, db_path=tmp_path/'s.db', workspace=tmp_path,
        session_id='s', source_ref='user', value_evidence=True)
    assert result['status'] == 'awaiting_confirmation', result.get('errors')
    store = ProposalStore(tmp_path/'s.db')
    approved = store.confirm(result['proposal_id'], session_id='s',
        expected_proposal_sha256=result['proposal_sha256'], actor='test-reviewer')
    executed = store.execute(approved['confirmation_id'], session_id='s', workspace=tmp_path)
    assert executed['status'] == 'succeeded', executed
    assert (tmp_path/'output.csv').read_bytes() == b'id,x\r\n001,a\r\n'


def test_public_profile_forwards_source_without_business_rewrite(tmp_path, monkeypatch):
    import argparse
    from rpe_harness import proposal_cli
    from rpe_harness.integrations import hermes_review
    received = []
    monkeypatch.setattr(hermes_review, 'run_hermes_review',
        lambda raw, *a, **kw: received.append((raw, kw)) or {})
    parser = argparse.ArgumentParser()
    proposal_cli.register_review(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['review', 'draft', '--text', '当前原文',
        '--review-profile', 'values-v1', '--workspace', str(tmp_path), '--db', str(tmp_path/'s.db')])
    proposal_cli.dispatch_review(args)
    assert received[0][0] == '当前原文'
    assert received[0][1]['review_profile'] == 'values-v1'


def test_signed_stdout_metadata_survives_persistence_allowlist():
    from rpe_harness.integrations.hermes import _metadata
    row = {'stdout_sha256': 'a'*64, 'stdout_identity_sha256': 'b'*64,
           'stdout_path_sha256': 'c'*64, 'stdout_size': 12}
    assert _metadata(row) == row


def test_staged_runtime_contract_keeps_json_business_request_and_old_default(tmp_path):
    import json
    import sys
    from rpe_harness.integrations.hermes_clean import CLEAN_SOURCE_CONTRACT
    from rpe_harness.integrations.hermes_session import native_execution_contract, prepare_home
    legacy = CLEAN_SOURCE_CONTRACT.replace('{helper_python}', sys.executable)
    assert native_execution_contract(sys.executable) == legacy
    contract = native_execution_contract(sys.executable, '.rpe-xlsx-stage.json')
    assert contract.startswith(legacy) and '.rpe-xlsx-stage.json' in contract
    settings = {'model': 'mimo-v2.6-pro', 'provider': 'xiaomi', 'base_url': 'https://offline.invalid/v1', 'api_key': 'offline-dummy'}
    home = prepare_home(tmp_path/'session', tmp_path, tmp_path/'db', tool_enabled=False,
        settings=settings, public_contract='clean', native_stdout_name='.rpe-xlsx-stage.json')
    assert json.loads((home/'config.yaml').read_text())['agent']['system_prompt'] == contract
    with pytest.raises(ValueError):
        native_execution_contract(sys.executable, '../escape.json')
