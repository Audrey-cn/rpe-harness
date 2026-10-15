"""Public entry can accept prose without users composing machine contracts."""
import pytest

from rpe_harness.cli import main


def test_review_cli_is_discoverable_and_describes_plain_text_drafting(capsys):
    with pytest.raises(SystemExit) as exited:
        main(['review', 'draft', '--help'])
    assert exited.value.code == 0
    output = capsys.readouterr().out
    assert '--text' in output
    assert '--source' in output


def test_review_cli_has_distinct_confirmation_and_run_commands(capsys):
    for operation in ('confirm', 'run'):
        with pytest.raises(SystemExit) as exited:
            main(['review', operation, '--help'])
        assert exited.value.code == 0
        assert operation in capsys.readouterr().out


def test_review_execution_failure_has_a_nonzero_exit_status(monkeypatch, capsys):
    from rpe_harness import proposal_cli
    monkeypatch.setattr(proposal_cli, 'dispatch_review', lambda args: {'status': 'failed'})
    assert main(['review', 'run', 'a' * 64]) == 1
    assert 'failed' in capsys.readouterr().out


def test_review_json_output_is_structured_and_preserves_verification(monkeypatch, capsys):
    import json
    from rpe_harness import proposal_cli
    monkeypatch.setattr(proposal_cli, 'dispatch_review', lambda args: {
        'status': 'needs_context', 'summary': '可读摘要',
        'proposal_verification': {'verified': False, 'proposal_verified': False},
        'host_observation': {'usage': {'total_tokens': None, 'known_total_tokens': 23}}})
    assert main(['review', 'draft', '--text', '明确请求', '--json']) == 3
    result = json.loads(capsys.readouterr().out)
    assert result['summary'] == '可读摘要'
    assert result['host_observation']['usage']['total_tokens'] is None
    assert result['host_observation']['usage']['known_total_tokens'] == 23
