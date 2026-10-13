import hashlib
import json

import pytest


@pytest.mark.parametrize('changed', ['model_identity_consistent', 'prompt_identical', 'input_identical'])
def test_successful_pair_costs_require_proven_same_model_prompt_and_input(changed):
    from rpe_harness.experiments.hermes_repeated import _paired_success_summary
    attempts = [dict(arm=arm, expected='success', validation={'passed': True},
                     usage={'coverage_complete': True, 'total_tokens': tokens}, elapsed_seconds=1)
                for arm, tokens in [('native', 100), ('integrated', 20)]]
    pair = dict(model_identity_consistent=True, prompt_identical=True, input_identical=True)
    assert _paired_success_summary(attempts, [pair])['count'] == 1
    pair[changed] = False
    report = _paired_success_summary(attempts, [pair])
    assert report['count'] == 0
    assert report['native']['total_tokens'] == 0
    assert report['integrated']['total_tokens'] == 0
    pair[changed] = None
    assert _paired_success_summary(attempts, [pair])['count'] == 0


def test_repeated_pairs_retain_every_failure_and_alternate_first_arm(tmp_path):
    from rpe_harness.experiments import hermes_repeated

    calls = []

    def session(prompt, workspace, db_path, *, tool_enabled, owner_root):
        calls.append((tool_enabled, hashlib.sha256((workspace / next(
            p.name for p in workspace.iterdir() if p.name.startswith('input.')
        )).read_bytes()).hexdigest(), prompt))
        if len(calls) == 2:
            raise OSError('deliberate failed launch')
        return {'exit_code': 1, 'completed': False, 'events': [], 'usage': {},
                'elapsed_seconds': 1, 'final_response': 'PRIVATE-NOT-EVIDENCE',
                'process_cleanup': {'cleanup_complete': True, 'remaining_descendants': []}}

    report_path = tmp_path / 'report.json'
    result = hermes_repeated.run_repeated_comparison(
        tmp_path / 'owned', report_path, session, repetitions=3)
    assert len(calls) == 36
    assert [calls[i][0] for i in (0, 12, 24)] == [False, True, False]
    for i in range(0, 36, 2):
        assert calls[i][1:] == calls[i + 1][1:]
    for i in range(12):
        assert calls[i][1:] == calls[i + 12][1:] == calls[i + 24][1:]
    assert result['summary']['native']['attempts'] == 18
    assert result['summary']['integrated']['attempts'] == 18
    assert result['summary']['native']['accepted'] == 0
    assert result['summary']['integrated']['accepted'] == 0
    assert result['summary']['native']['complete_usage_attempts'] == 0
    assert result['native_hermes_baseline_measured'] is False
    assert len(result['attempts']) == 36
    assert sum('runner_error_type' in a for a in result['attempts']) == 1
    assert len({a['attempt_id'] for a in result['attempts']}) == 36
    saved = report_path.read_text()
    assert json.loads(saved) == result
    assert 'PRIVATE-NOT-EVIDENCE' not in saved


@pytest.mark.parametrize('repetitions', [True, 0, 1, 11, 2.5])
def test_invalid_repeat_budget_does_not_start_sessions(tmp_path, repetitions):
    from rpe_harness.experiments import hermes_repeated

    def forbidden(*args, **kwargs):
        pytest.fail('invalid budget started a host')

    with pytest.raises(ValueError):
        hermes_repeated.run_repeated_comparison(
            tmp_path / 'owned', tmp_path / 'report.json', forbidden,
            repetitions=repetitions)
    assert not (tmp_path / 'owned').exists()
