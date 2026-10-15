"""Public consumer checked against the real review producer contract."""
from test_hermes_review import RAW, review, draft, dispatch, host_result
from test_proposal_cli import parser


def test_public_draft_accepts_actual_gate_and_wrapper(review, monkeypatch):
    from rpe_harness.integrations import hermes_session
    from rpe_harness.proposal_cli import dispatch_review
    dispatch(review, draft())
    monkeypatch.setattr(hermes_session, 'run_hermes_session', lambda *a, **kw: host_result(review))
    result = dispatch_review(parser().parse_args(['review', 'draft', '--text', RAW,
        '--workspace', str(review['workspace']), '--db', str(review['db'])]))
    assert result['status'] == 'awaiting_confirmation', result
    assert result['proposal_verification']['verified'] is True
    assert result['proposal_verification']['proposal_verified'] is True
    assert RAW in result['summary']
    assert not (review['workspace'] / 'out.csv').exists()
