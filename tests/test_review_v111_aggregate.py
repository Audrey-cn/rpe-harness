"""Public aggregate conclusions must agree with records even after outer rehashing."""
import copy
import hashlib
import importlib
import importlib.util

import pytest

from rpe_harness.experiments.cost_ledger import summarize_phases
from rpe_harness.storage import canonical_json


def verifier():
    assert importlib.util.find_spec('rpe_harness.experiments.review_v111_aggregate'), 'aggregate verifier is absent'
    return importlib.import_module('rpe_harness.experiments.review_v111_aggregate').verify_aggregates


@pytest.fixture
def evidence():
    cases = [{'case_id': name} for name in ('complete', 'cold', 'source', 'missing')]
    trials = [{'trial_id': 'formal-' + case['case_id'] + '-' + arm, 'case_id': case['case_id'],
        'phase': 'formal', 'arm': arm, 'category': 'contract_probe' if case['case_id'] == 'missing' else 'production'}
        for case in cases for arm in ('native', 'review')]
    pilots = [{'trial_id': 'pilot-cold-' + arm, 'case_id': 'pilot-cold', 'phase': 'pilot',
               'arm': arm, 'category': 'production'} for arm in ('native', 'review')]
    plan = {'controller_registration_sha256': 'a' * 64, 'cases': cases, 'sessions': trials, 'pilots': pilots,
        'public_contract_sha256': 'b' * 64, 'execution_public_contract_sha256': 'c' * 64,
        'environment': {'source_sha256': {'experiments/review_v111_comparison.py': 'd' * 64}}}
    records = []
    for trial, accepted in ((trials[0], True), (trials[1], False), (trials[6], True), (pilots[1], False)):
        records.append({**trial, 'accepted': accepted, 'comparable': True, 'model_identity': {'provider': 'same'},
                        'probe_correct': accepted and trial['category'] == 'contract_probe'})
    phases = [{'attempt_id': r['trial_id'] + '-draft', 'arm': r['arm'], 'phase': 'draft',
               'status': 'completed', 'api_expected': True, 'elapsed_seconds': 2,
               'usage': {'api_requests': 1, 'coverage_complete': True, 'total_tokens': 10,
                         'known_total_tokens': 10}} for r in records]
    phases.append({'attempt_id': trials[0]['trial_id'] + '-execute', 'arm': 'native', 'phase': 'execute',
                   'status': 'completed', 'api_expected': True, 'elapsed_seconds': 3,
                   'usage': {'api_requests': 1, 'coverage_complete': True, 'total_tokens': 20,
                             'known_total_tokens': 20}})
    summary = {'fingerprint': 'a' * 64, 'records': records, 'formal_attempts': 3, 'pilot_attempts': 1,
        'native_execution_sessions': 1, 'comparison_ready': False, 'by_arm': {
            'native': {'formal_attempts': 2, 'complete_chain': 2, 'production_complete_chain': 1, 'probe_correct': 1,
                'costs': summarize_phases([phases[0], phases[2], phases[4]]),
                'formal_costs': summarize_phases([phases[0], phases[2], phases[4]])},
            'review': {'formal_attempts': 1, 'complete_chain': 0, 'production_complete_chain': 0, 'probe_correct': 0,
                'costs': summarize_phases([phases[1], phases[3]]),
                'formal_costs': summarize_phases([phases[1]])}},
        'pairs': [{'case_id': 'complete', 'comparable': True, 'native_accepted': True, 'review_accepted': False},
            {'case_id': 'cold', 'comparable': False, 'native_accepted': None, 'review_accepted': None},
            {'case_id': 'source', 'comparable': False, 'native_accepted': None, 'review_accepted': None},
            {'case_id': 'missing', 'comparable': False, 'native_accepted': True, 'review_accepted': None}],
        'costs': summarize_phases(phases), 'formal_costs': summarize_phases([phases[0], phases[1], phases[2], phases[4]]),
        'pilot_costs': summarize_phases([phases[3]]), 'shared_costs': summarize_phases([])}
    freeze = {'registration_sha256': 'a' * 64, 'environment': copy.deepcopy(plan['environment']),
        'public_contract_sha256': 'b' * 64, 'execution_public_contract_sha256': 'c' * 64}
    return plan, summary, phases, freeze


def test_recomputes_partial_formal_and_pilot_aggregates(evidence):
    assert verifier()(*evidence) is None


@pytest.mark.parametrize('fault', ['by_arm', 'pairs', 'ready', 'formal_count', 'pilot_count', 'native_execution',
                                  'arm_cost', 'formal_cost', 'pilot_cost', 'model_pair', 'duplicate_record'])
def test_changed_summary_is_rejected_even_with_a_new_outer_hash(evidence, fault):
    plan, summary, phases, freeze = evidence
    if fault == 'by_arm':
        summary['by_arm']['review']['complete_chain'] = 4
    elif fault == 'pairs':
        summary['pairs'][0]['review_accepted'] = True
    elif fault == 'ready':
        summary['comparison_ready'] = True
    elif fault == 'formal_count':
        summary['formal_attempts'] = 8
    elif fault == 'pilot_count':
        summary['pilot_attempts'] = 2
    elif fault == 'native_execution':
        summary['native_execution_sessions'] = 0
    elif fault == 'arm_cost':
        summary['by_arm']['native']['costs']['known_total_tokens'] = 0
    elif fault == 'formal_cost':
        summary['formal_costs']['known_total_tokens'] = 0
    elif fault == 'pilot_cost':
        summary['pilot_costs']['known_total_tokens'] = 0
    elif fault == 'model_pair':
        summary['records'][1]['model_identity'] = {'provider': 'different'}
    else:
        summary['records'].append(copy.deepcopy(summary['records'][0]))
    summary['sha256'] = hashlib.sha256(canonical_json(summary).encode()).hexdigest()
    with pytest.raises(ValueError):
        verifier()(plan, summary, phases, freeze)


@pytest.mark.parametrize('field', ['registration_sha256', 'public_contract_sha256', 'execution_public_contract_sha256', 'source'])
def test_formal_freeze_is_bound_to_public_registration(evidence, field):
    plan, summary, phases, freeze = evidence
    if field == 'source':
        freeze['environment']['source_sha256'] = {}
    else:
        freeze[field] = 'f' * 64
    with pytest.raises(ValueError):
        verifier()(plan, summary, phases, freeze)
