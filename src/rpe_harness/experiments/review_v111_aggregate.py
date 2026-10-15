"""Portable aggregate consistency checks, independent of the comparison runner."""
from __future__ import annotations

import re

from ..storage import canonical_json
from .cost_ledger import summarize_phases

_ARMS = ('native', 'review')
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _same(actual, expected, name):
    if canonical_json(actual) != canonical_json(expected):
        raise ValueError('aggregate_changed:' + name)


def verify_aggregates(plan, summary, phases, formal_freeze=None):
    """Recompute reported counts, pair conclusions and phase cost allocations.

    This verifies arithmetic over recorded observations, not their runtime truth.
    Driver timing cannot be recomputed without exported driver receipts.
    """
    try:
        if plan.get('profile') == 'review-v114':
            from .review_v114_comparison import validate_registration_budget
            validate_registration_budget(plan)
            if (sum(phase['phase'] == 'draft' for phase in phases) > 10
                    or sum(phase['phase'] == 'execute' and phase['arm'] == 'native' for phase in phases) > 4):
                raise ValueError('v114_finite_budget_exceeded')
        elif plan.get('profile') == 'review-v115':
            from .review_v115_comparison import validate_registration_budget
            validate_registration_budget(plan)
            if (sum(phase['phase'] == 'draft' for phase in phases) > 6
                    or sum(phase['phase'] == 'execute' and phase['arm'] == 'native' for phase in phases) > 2):
                raise ValueError('v115_finite_budget_exceeded')
        registration = plan['controller_registration_sha256']
        _same(summary['fingerprint'], registration, 'registration')
        trials = plan['sessions'] + plan['pilots']
        indexed = {trial['trial_id']: trial for trial in trials}
        if len(indexed) != len(trials):
            raise ValueError('aggregate_population_duplicate')
        records = summary['records']
        if len({row['trial_id'] for row in records}) != len(records):
            raise ValueError('aggregate_record_duplicate')
        for row in records:
            trial = indexed[row['trial_id']]
            if (row['arm'] not in _ARMS or row['phase'] not in {'pilot', 'formal'}
                    or type(row['accepted']) is not bool):
                raise ValueError('aggregate_record_invalid')
            for key in ('arm', 'phase', 'case_id', 'category'):
                _same(row[key], trial[key], 'record_' + key)

        formal = [row for row in records if row['phase'] == 'formal']
        _same(summary['formal_attempts'], len(formal), 'formal_attempts')
        _same(summary['pilot_attempts'], len(records) - len(formal), 'pilot_attempts')
        _same(summary['native_execution_sessions'], sum(
            phase['phase'] == 'execute' and phase['arm'] == 'native' for phase in phases),
            'native_execution_sessions')

        expected_arms = {}
        for arm in _ARMS:
            rows = [row for row in formal if row['arm'] == arm]
            expected_arms[arm] = {
                'formal_attempts': len(rows),
                'complete_chain': sum(row['accepted'] is True for row in rows),
                'production_complete_chain': sum(row['accepted'] is True for row in rows
                                                 if row['category'] == 'production'),
                'probe_correct': sum(row['accepted'] is True and row.get('probe_correct') is True
                                     for row in rows if row['category'] == 'contract_probe'),
                'costs': summarize_phases(phase for phase in phases if phase['arm'] == arm),
                'formal_costs': summarize_phases(phase for phase in phases if phase['arm'] == arm
                                                 and phase['attempt_id'].startswith('formal-'))}
        _same(summary['by_arm'], expected_arms, 'by_arm')

        expected_pairs = []
        for case in plan['cases']:
            rows = {row['arm']: row for row in formal if row['case_id'] == case['case_id']}
            comparable = bool(set(rows) == set(_ARMS) and all(
                row.get('comparable') for row in rows.values())
                and rows['native'].get('model_identity') == rows['review'].get('model_identity'))
            expected_pairs.append({'case_id': case['case_id'], 'comparable': comparable,
                **{arm + '_accepted': rows[arm].get('accepted') if arm in rows else None for arm in _ARMS}})
        _same(summary['pairs'], expected_pairs, 'pairs')
        expected_count = 6 if plan.get('profile') == 'review-v115' else 8
        _same(summary['comparison_ready'], len(formal) == expected_count and all(
            pair['comparable'] for pair in expected_pairs), 'comparison_ready')
        for key, selected in (
                ('costs', phases),
                ('formal_costs', [phase for phase in phases if phase['attempt_id'].startswith('formal-')]),
                ('pilot_costs', [phase for phase in phases if phase['attempt_id'].startswith('pilot-')]),
                ('shared_costs', [phase for phase in phases if phase['arm'] == 'shared'])):
            _same(summary[key], summarize_phases(selected), key)

        if formal_freeze is not None:
            _same(formal_freeze['registration_sha256'], registration, 'formal_registration')
            for field in ('public_contract_sha256', 'execution_public_contract_sha256'):
                if not isinstance(formal_freeze[field], str) or not _SHA.fullmatch(formal_freeze[field]):
                    raise ValueError('formal_contract_hash_invalid')
                _same(formal_freeze[field], plan[field], 'formal_' + field)
            if plan.get('profile') == 'review-v115':
                for field in ('native_public_contract_sha256', 'staged_execution_public_contract_sha256'):
                    if not isinstance(formal_freeze[field], str) or not _SHA.fullmatch(formal_freeze[field]):
                        raise ValueError('v115_formal_contract_hash_invalid')
                    _same(formal_freeze[field], plan[field], 'formal_' + field)
            sources = formal_freeze['environment']['source_sha256']
            if (type(sources) is not dict or 'experiments/review_v111_comparison.py' not in sources
                    or any(type(name) is not str or not name.endswith('.py') or
                           type(digest) is not str or not _SHA.fullmatch(digest)
                           for name, digest in sources.items())):
                raise ValueError('formal_source_hashes_invalid')
            # Source hashes may legitimately change after a failed pilot. The
            # formal source mapping is retained as its own frozen observation;
            # it must not be replaced with the registration's pilot mapping.
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('aggregate_evidence_invalid') from error
