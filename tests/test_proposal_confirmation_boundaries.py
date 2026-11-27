"""A saved confirmation authorizes only its reviewed immutable proposal."""
import io
import json
import sqlite3
import sys

import pytest
from test_proposal import RAW, draft, proposed


@pytest.mark.parametrize('change', ['source', 'execution'])
def test_changed_review_requires_its_own_explicit_digest(tmp_path, change):
    from rpe_harness.proposal import ProposalStore

    original = proposed(tmp_path)
    candidate, raw = draft(), RAW + '\n请再次核对完整要求。'
    if change == 'execution':
        candidate['execution']['output_path'] = 'revised.json'
        candidate['requirements'][2]['quote'] = 'revised.json'
        raw = RAW.replace('out.json', 'revised.json')
    revised = proposed(tmp_path, candidate, raw)
    assert revised['status'] == 'awaiting_confirmation'
    assert revised['proposal_sha256'] != original['proposal_sha256']
    store = ProposalStore(tmp_path / 'state.sqlite')

    for target, digest in [(revised, original['proposal_sha256']),
                           (original, revised['proposal_sha256'])]:
        with pytest.raises(ValueError, match='digest'):
            store.confirm(target['proposal_id'], session_id='s',
                expected_proposal_sha256=digest, actor='human:reviewer')
    with sqlite3.connect(store.db_path) as db:
        assert db.execute('SELECT count(*) FROM rpe_confirmations').fetchone()[0] == 0

    confirmation = store.confirm(revised['proposal_id'], session_id='s',
        expected_proposal_sha256=revised['proposal_sha256'], actor='human:reviewer')
    result = store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path)
    assert result['status'] == 'succeeded'
    expected_output = tmp_path / ('revised.json' if change == 'execution' else 'out.json')
    assert expected_output.read_text() == '{"名字":[1,2]}\n'
    if change == 'execution':
        assert not (tmp_path / 'out.json').exists()
    assert result['proposal_id'] == revised['proposal_id']


@pytest.mark.parametrize('digest', [None, '', False, 0, {}, '0' * 64])
def test_confirmation_never_fills_in_an_absent_or_invalid_expected_digest(tmp_path, digest):
    from rpe_harness.proposal import ProposalStore

    proposal = proposed(tmp_path)
    store = ProposalStore(tmp_path / 'state.sqlite')
    with pytest.raises(ValueError, match='digest'):
        store.confirm(proposal['proposal_id'], session_id='s',
            expected_proposal_sha256=digest, actor='human:reviewer')
    with sqlite3.connect(store.db_path) as db:
        assert db.execute('SELECT count(*) FROM rpe_confirmations').fetchone()[0] == 0
    assert not (tmp_path / 'out.json').exists()


def test_matching_review_digest_cannot_confirm_input_changed_since_display(tmp_path):
    from rpe_harness.proposal import ProposalStore

    proposal = proposed(tmp_path)
    store = ProposalStore(tmp_path / 'state.sqlite')
    displayed = store.show(proposal['proposal_id'], session_id='s')
    (tmp_path / 'input.json').write_text('[99]')
    with pytest.raises(ValueError, match='input|source'):
        store.confirm(displayed['proposal_id'], session_id='s',
            expected_proposal_sha256=displayed['proposal_sha256'], actor='human:reviewer')
    with sqlite3.connect(store.db_path) as db:
        assert db.execute('SELECT count(*) FROM rpe_confirmations').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM rpe_proposal_executions').fetchone()[0] == 0
    assert not (tmp_path / 'out.json').exists()


def test_public_confirmation_rejects_stale_or_implicit_acceptance(tmp_path, monkeypatch, capsys):
    from rpe_harness.cli import main

    original = proposed(tmp_path)
    current = proposed(tmp_path, raw=RAW + '\n请再次核对完整要求。')
    command = ['review', 'confirm', current['proposal_id'], '--json',
               '--db', str(tmp_path / 'state.sqlite'), '--actor', 'human:reviewer']
    monkeypatch.setattr(sys, 'stdin', io.StringIO('确认\n'))
    for arguments in (['--accept-digest', original['proposal_sha256']], []):
        assert main(command + arguments) == 2
        rejected = json.loads(capsys.readouterr().out)
        assert rejected['status'] == 'error'
        assert rejected['error_code'] == 'invalid_request'
        assert 'confirmation_id' not in rejected
        with sqlite3.connect(tmp_path / 'state.sqlite') as db:
            assert db.execute('SELECT count(*) FROM rpe_confirmations').fetchone()[0] == 0

    assert main(command + ['--accept-digest', current['proposal_sha256']]) == 0
    confirmed = json.loads(capsys.readouterr().out)
    assert confirmed['status'] == 'confirmed'
    assert confirmed['proposal_id'] == current['proposal_id']
    assert confirmed['proposal_sha256'] == current['proposal_sha256']
    assert not (tmp_path / 'out.json').exists()


def test_confirmation_cannot_be_executed_in_another_session_or_retargeted(tmp_path):
    from rpe_harness.proposal import ProposalStore, create_proposal

    original = proposed(tmp_path)
    store = ProposalStore(tmp_path / 'state.sqlite')
    confirmation = store.confirm(original['proposal_id'], session_id='s',
        expected_proposal_sha256=original['proposal_sha256'], actor='human:reviewer')
    candidate = draft()
    candidate['execution']['output_path'] = 'other.json'
    candidate['requirements'][2]['quote'] = 'other.json'
    other = create_proposal(RAW.replace('out.json', 'other.json'), candidate,
        db_path=store.db_path, workspace=tmp_path, session_id='other-session',
        source_ref='other:user')
    assert other['status'] == 'awaiting_confirmation'

    with pytest.raises(ValueError, match='session'):
        store.execute(confirmation['confirmation_id'], session_id='other-session', workspace=tmp_path)
    for retargeting in ({'proposal_id': other['proposal_id']},
                        {'payload': {'execution': {'output_path': str(tmp_path / 'other.json')}}}):
        with pytest.raises(TypeError):
            store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path,
                          **retargeting)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute('SELECT count(*) FROM rpe_proposal_executions').fetchone()[0] == 0
    assert not (tmp_path / 'out.json').exists()
    assert not (tmp_path / 'other.json').exists()

    own_confirmation = store.confirm(other['proposal_id'], session_id='other-session',
        expected_proposal_sha256=other['proposal_sha256'], actor='human:reviewer')
    own_result = store.execute(own_confirmation['confirmation_id'], session_id='other-session',
                              workspace=tmp_path)
    assert own_result['status'] == 'succeeded'
    assert own_result['proposal_id'] == other['proposal_id']
    assert (tmp_path / 'other.json').read_text() == '{"名字":[1,2]}\n'
    assert not (tmp_path / 'out.json').exists()
