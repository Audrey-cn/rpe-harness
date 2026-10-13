"""Predeclared repeated real-host pairs; failure and unknown usage remain visible."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

from .hermes_native import build_cases, run_comparison, _save_report


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _usage_complete(attempt):
    usage = attempt.get('usage', {})
    return usage.get('coverage_complete') is True and _number(usage.get('total_tokens'))


def _summary(attempts):
    result = {}
    for arm in ('native', 'integrated'):
        selected = [a for a in attempts if a['arm'] == arm]
        known = [a for a in selected if _usage_complete(a)]
        result[arm] = {
            'attempts': len(selected),
            'accepted': sum(a['validation']['passed'] is True for a in selected),
            'supported_successes': sum(a['validation']['passed'] is True and a['expected'] == 'success' for a in selected),
            'correct_rejections': sum(a['validation']['passed'] is True and a['expected'] == 'rejected' for a in selected),
            'completed': sum(a['completed'] is True for a in selected),
            'timeouts': sum(a['timed_out'] is True for a in selected),
            'complete_usage_attempts': len(known),
            'unknown_usage_attempts': len(selected) - len(known),
            'complete_usage_token_subtotal': sum(a['usage']['total_tokens'] for a in known),
            'total_tokens': sum(a['usage']['total_tokens'] for a in known) if len(known) == len(selected) and selected else None,
            'observed_elapsed_seconds': round(sum(a['elapsed_seconds'] for a in selected if _number(a.get('elapsed_seconds'))), 3),
        }
    return result


def _paired_success_summary(attempts, pairs):
    comparable = []
    for first, second, proof in zip(attempts[::2], attempts[1::2], pairs):
        if (all(proof.get(key) is True for key in
                ('model_identity_consistent', 'prompt_identical', 'input_identical'))
                and first['expected'] == 'success' and second['expected'] == 'success'
                and first['validation']['passed'] and second['validation']['passed']
                and _usage_complete(first) and _usage_complete(second)
                and _number(first.get('elapsed_seconds')) and _number(second.get('elapsed_seconds'))):
            comparable.append({a['arm']: a for a in (first, second)})
    return {'count': len(comparable),
        **{arm: {'total_tokens': sum(pair[arm]['usage']['total_tokens'] for pair in comparable),
                 'elapsed_seconds': round(sum(pair[arm]['elapsed_seconds'] for pair in comparable), 3)}
           for arm in ('native', 'integrated')}}


def run_repeated_comparison(root: Path | str, report_path: Path | str,
                            session_runner: Callable[..., dict[str, Any]], *,
                            repetitions: int = 3) -> dict[str, Any]:
    """Repeat fixed fixtures without replacing failures or changing the selection budget."""
    if type(repetitions) is not int or not 2 <= repetitions <= 10:
        raise ValueError('repetitions must be an integer from 2 to 10')
    root = Path(root)
    if root.is_symlink():
        raise ValueError('repeated comparison root must not be a symlink')
    root.mkdir(parents=True, exist_ok=False)
    root = root.resolve()
    cases = build_cases(root / 'fixtures')
    report_path = Path(report_path)
    report = {
        'experiment': 'hermes-native-v1.2-repeated', 'repetitions': repetitions,
        'case_count': len(cases), 'planned_attempts': repetitions * len(cases) * 2,
        'attempts': [], 'rounds': [], 'pairs': [], 'completed': False,
        'native_hermes_baseline_measured': False,
        'interpretation': 'Independent fresh sessions, not stateful conversation turns. '
                          'Required-entry policy is part of the integrated product. '
                          'All attempts count; missing usage is unknown; no causal or general-agent claim.',
    }
    _save_report(report_path, report)
    for index in range(repetitions):
        previous = list(report['attempts'])

        def checkpoint(current):
            report['attempts'] = previous + [
                {**a, 'round': index + 1,
                 'attempt_id': f"r{index + 1}-{a['case_id']}-{a['arm']}"}
                for a in current['attempts']]
            report['summary'] = _summary(report['attempts'])
            _save_report(report_path, report)

        round_report = run_comparison(root / f'round-{index + 1}',
            root / f'round-{index + 1}.json', session_runner, cases=cases,
            arm_offset=index % 2, checkpoint=checkpoint)
        report['rounds'].append({
            'round': index + 1, 'summary': round_report['summary'],
            'native_hermes_baseline_measured': round_report['native_hermes_baseline_measured'],
            'owned_state_root': round_report['owned_state_root'],
        })
        report['pairs'].extend({**pair, 'round': index + 1} for pair in round_report['pairs'])
        _save_report(report_path, report)
    report['completed'] = len(report['attempts']) == report['planned_attempts']
    native = [a for a in report['attempts'] if a['arm'] == 'native']
    report['native_hermes_baseline_measured'] = (report['completed'] and all(
        r['native_hermes_baseline_measured'] for r in report['rounds'])
        and all(_usage_complete(a) for a in native)
        and all(all(p.get(key) is True for key in
                    ('model_identity_consistent', 'prompt_identical', 'input_identical'))
                for p in report['pairs']))
    report['both_successful_complete_pairs'] = _paired_success_summary(report['attempts'], report['pairs'])
    _save_report(report_path, report)
    return report
