import argparse
import io
import sqlite3
import sys
import types

import pytest

from test_proposal import RAW, draft, proposed


def parser():
    from rpe_harness.proposal_cli import register_review
    result = argparse.ArgumentParser()
    register_review(result.add_subparsers(dest='command', required=True))
    return result


def test_cli_draft_uses_original_prose_and_lazy_real_hermes_wrapper(tmp_path, monkeypatch):
    from rpe_harness.proposal_cli import dispatch_review
    from rpe_harness.proposal import create_proposal
    (tmp_path / 'input.json').write_text('[1,2]')
    called = []
    module = types.ModuleType('rpe_harness.integrations.hermes_review')
    def wrapper(raw, workspace, db_path, **kwargs):
        called.append((raw, workspace, db_path, kwargs))
        proposal = create_proposal(raw, draft(), db_path=db_path, workspace=workspace,
            session_id='actual-native-session', source_ref='native-current-user')
        return {'proposal': proposal, 'proposal_verification': {'verified': True, 'proposal_verified': True}}
    module.run_hermes_review = wrapper
    monkeypatch.setitem(sys.modules, module.__name__, module)
    args = parser().parse_args(['review', 'draft', '--db', str(tmp_path / 'state.sqlite'),
        '--workspace', str(tmp_path), '--text', RAW])
    result = dispatch_review(args)
    assert called[0][0] == RAW
    assert result['status'] == 'awaiting_confirmation'
    assert result['proposal_id'] in result['summary'] and RAW in result['summary']
    assert result['proposal_verification']['proposal_verified']
    assert not (tmp_path / 'out.json').exists()


def test_cli_show_infers_original_session_and_remains_read_only(tmp_path):
    from rpe_harness.proposal_cli import dispatch_review
    proposal = proposed(tmp_path)
    (tmp_path / 'input.json').unlink()
    result = dispatch_review(parser().parse_args(['review', 'show', proposal['proposal_id'],
        '--db', str(tmp_path / 'state.sqlite')]))
    assert result['status'] == 'awaiting_confirmation'
    assert RAW in result['summary'] and proposal['proposal_id'] in result['summary']


def test_cli_confirm_displays_whole_review_before_typed_confirmation(tmp_path, monkeypatch, capsys):
    from rpe_harness.proposal_cli import dispatch_review
    proposal = proposed(tmp_path)
    class Terminal(io.StringIO):
        def isatty(self):
            return True
        def readline(self, *args):
            displayed = capsys.readouterr().out
            assert RAW in displayed and '标准化执行合同' in displayed and 'proposed_default' in displayed
            return '确认\n'
    monkeypatch.setattr(sys, 'stdin', Terminal())
    result = dispatch_review(parser().parse_args(['review', 'confirm', proposal['proposal_id'],
        '--db', str(tmp_path / 'state.sqlite')]))
    assert result['status'] == 'confirmed' and result['actor'].startswith('human-cli:')
    assert result['confirmation_id'] in result['summary']
    assert not (tmp_path / 'out.json').exists()


def test_cli_confirm_does_not_treat_pipe_or_wrong_text_as_human_confirmation(tmp_path, monkeypatch):
    from rpe_harness.proposal_cli import dispatch_review
    proposal = proposed(tmp_path)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('确认\n'))
    with pytest.raises(ValueError, match='accept-digest|terminal'):
        dispatch_review(parser().parse_args(['review', 'confirm', proposal['proposal_id'],
            '--db', str(tmp_path / 'state.sqlite')]))
    with sqlite3.connect(tmp_path / 'state.sqlite') as db:
        assert db.execute('SELECT count(*) FROM rpe_confirmations').fetchone()[0] == 0


def test_cli_automation_confirmation_requires_exact_display_digest_and_labels_actor(tmp_path):
    from rpe_harness.proposal_cli import dispatch_review
    proposal = proposed(tmp_path)
    for digest in ['0' * 64, proposal['proposal_sha256']]:
        args = parser().parse_args(['review', 'confirm', proposal['proposal_id'],
            '--db', str(tmp_path / 'state.sqlite'), '--accept-digest', digest])
        if digest == '0' * 64:
            with pytest.raises(ValueError):
                dispatch_review(args)
        else:
            result = dispatch_review(args)
            assert result['actor'].startswith('trusted-application:')
            assert RAW in result['summary']


def test_cli_run_infers_immutable_workspace_and_session(tmp_path):
    from rpe_harness.proposal_cli import dispatch_review
    from rpe_harness.proposal import ProposalStore
    proposal = proposed(tmp_path)
    confirmation = ProposalStore(tmp_path / 'state.sqlite').confirm(proposal['proposal_id'], session_id='s',
        expected_proposal_sha256=proposal['proposal_sha256'], actor='simulated_fixture_reviewer')
    result = dispatch_review(parser().parse_args(['review', 'run', confirmation['confirmation_id'],
        '--db', str(tmp_path / 'state.sqlite')]))
    assert result['status'] == 'succeeded' and (tmp_path / 'out.json').exists()


def test_cli_exposes_no_handwritten_json_draft_or_model_confirmation_field():
    with pytest.raises(SystemExit):
        parser().parse_args(['review', 'draft', '--draft', 'task.json'])


def test_cli_rejects_legacy_passed_without_verified_proposal(tmp_path, monkeypatch):
    from rpe_harness.proposal_cli import dispatch_review
    module = types.ModuleType('rpe_harness.integrations.hermes_review')
    module.run_hermes_review = lambda *a, **kw: {'proposal': {'status': 'awaiting_confirmation'},
        'proposal_verification': {'passed': True, 'verified': True, 'proposal_verified': False}}
    monkeypatch.setitem(sys.modules, module.__name__, module)
    args = parser().parse_args(['review', 'draft', '--db', str(tmp_path / 'state.sqlite'),
        '--workspace', str(tmp_path), '--text', RAW])
    result = dispatch_review(args)
    assert result['status'] == 'unverified'


def test_public_unverified_exit_is_one_not_business_needs_context(tmp_path, monkeypatch, capsys):
    from rpe_harness.cli import main
    from rpe_harness.integrations import hermes_review
    from rpe_harness.integrations.hermes_session import SessionObservationError
    from rpe_harness.proposal import create_proposal
    seen = []
    def failed(*args, **kwargs):
        seen.append(kwargs['owner_root'])
        raise SessionObservationError({'usage': {'known_total_tokens': 14, 'api_requests': 2},
            'process_cleanup': {'cleanup_complete': True}, 'session_id': 'observed-session'},
            code='terminal_session_mismatch', error_type='ValueError')
    monkeypatch.setattr(hermes_review, 'run_hermes_review', failed)
    argv = ['review', 'draft', '--db', str(tmp_path / 'state.sqlite'), '--workspace', str(tmp_path),
            '--text', RAW, '--json']
    assert main(argv) == 1
    import json
    failed_result = json.loads(capsys.readouterr().out)
    assert failed_result['status'] == 'unverified'
    assert failed_result['failure'] == {'domain': 'observation', 'code': 'terminal_session_mismatch',
                                       'stage': 'finalization', 'retryable': False}
    assert failed_result['host_observation']['usage']['known_total_tokens'] == 14
    assert failed_result['host_observation']['process_cleanup']['cleanup_complete'] is True
    artifact = __import__('pathlib').Path(failed_result['diagnostics_ref']['path'])
    assert artifact.is_file() and not seen[0].exists()
    assert json.loads(artifact.read_text()) == failed_result['review_diagnostics']
    def business(raw, workspace, db_path, **kwargs):
        candidate = draft()
        candidate['execution'].pop('style')
        candidate['requirements'][0]['fields'] = ['/capability']
        proposal = create_proposal(raw, candidate, db_path=db_path, workspace=workspace,
                                   session_id='business', source_ref='current')
        return {'proposal': proposal, 'proposal_verification': {'verified': True, 'proposal_verified': True}}
    monkeypatch.setattr(hermes_review, 'run_hermes_review', business)
    assert main(argv) == 3
    business_result = json.loads(capsys.readouterr().out)
    assert business_result['status'] == 'needs_context'
    assert 'execution.style' in business_result['missing']
