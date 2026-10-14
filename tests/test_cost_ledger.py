import pytest


def ledger_module():
    from rpe_harness.experiments import cost_ledger
    return cost_ledger


def usage(tokens):
    return {'api_requests': 1, 'known_total_tokens': tokens,
            'total_tokens': tokens, 'coverage_complete': True,
            'missing_observations': 0}


def test_reservation_and_completion_are_immutable_and_recover_pending(tmp_path):
    module = ledger_module()
    ledger = module.PhaseLedger(tmp_path)
    ledger.reserve('learned', 'generation', 'attempt-1')
    with pytest.raises(FileExistsError):
        ledger.reserve('learned', 'generation', 'attempt-1')
    assert module.PhaseLedger(tmp_path).records()[0]['status'] == 'incomplete'
    ledger.finish('attempt-1', {'status': 'failed', 'elapsed_seconds': 2.0,
                              'usage': usage(17), 'error_code': 'generation_timed_out'})
    with pytest.raises(FileExistsError):
        ledger.finish('attempt-1', {'status': 'completed', 'elapsed_seconds': 1.0,
                                  'usage': usage(1)})
    assert ledger.records()[0]['status'] == 'failed'


def test_total_includes_failed_generation_validation_and_production(tmp_path):
    module = ledger_module()
    ledger = module.PhaseLedger(tmp_path)
    for name, phase, tokens in [('failed', 'generation', 17), ('retry', 'generation', 23),
                                 ('production', 'production', 41)]:
        ledger.reserve('learned', phase, name)
        ledger.finish(name, {'status': 'failed' if name == 'failed' else 'completed',
                            'elapsed_seconds': 2.0, 'usage': usage(tokens)})
    ledger.reserve('learned', 'qualification', 'qualification', api_expected=False)
    ledger.finish('qualification', {'status': 'completed', 'elapsed_seconds': 3.0})
    summary = module.summarize_phases(ledger.records())
    assert summary['total_tokens'] == 81 and summary['known_total_tokens'] == 81
    assert summary['api_requests'] == 3 and summary['elapsed_seconds_sum'] == 9.0
    assert summary['failed_phases'] == 1 and summary['phase_count'] == 4
    assert summary['billing_verified_cost'] is None


def test_missing_usage_and_unfinished_attempt_do_not_become_zero_cost(tmp_path):
    module = ledger_module()
    ledger = module.PhaseLedger(tmp_path)
    ledger.reserve('learned', 'generation', 'timed-out')
    ledger.finish('timed-out', {'status': 'timed_out', 'elapsed_seconds': 5.0,
                              'usage': {'known_total_tokens': 19, 'total_tokens': None,
                                        'api_requests': 2, 'coverage_complete': False,
                                        'missing_observations': 1}})
    ledger.reserve('learned', 'generation', 'never-finished')
    summary = module.summarize_phases(ledger.records())
    assert summary['total_tokens'] is None and summary['known_total_tokens'] == 19
    assert summary['api_requests'] is None and summary['known_api_requests'] == 2
    assert summary['usage_complete'] is False and summary['incomplete_phases'] == 1
    assert summary['elapsed_seconds_sum'] is None and summary['known_elapsed_seconds_sum'] == 5.0


def test_research_and_shared_allocations_are_explicit_and_rss_not_added(tmp_path):
    module = ledger_module()
    ledger = module.PhaseLedger(tmp_path)
    for name, arm, scope, peak in [('train', 'shared_rpe', 'operational', 90),
                                    ('g0', 'research', 'research', 110),
                                    ('reuse', 'learned', 'operational', 80)]:
        ledger.reserve(arm, 'training', name, api_expected=False, scope=scope)
        ledger.finish(name, {'status': 'completed', 'elapsed_seconds': 1.0,
                            'observed_peak_rss_bytes': peak})
    whole = module.summarize_phases(ledger.records())
    operational = module.summarize_phases([r for r in ledger.records() if r['scope'] == 'operational'])
    assert whole['phase_count'] == 3 and operational['phase_count'] == 2
    assert whole['observed_peak_rss_bytes'] == 110
    assert operational['observed_peak_rss_bytes'] == 90


@pytest.mark.parametrize('name', ['../outside', '/outside', '', 'a/b', 'a'*129])
def test_unsafe_attempt_names_are_rejected_before_writing(tmp_path, name):
    with pytest.raises(ValueError):
        ledger_module().PhaseLedger(tmp_path).reserve('native', 'production', name)
    assert not list(tmp_path.rglob('*.json'))


def test_raw_private_payload_negative_cost_and_model_text_are_not_persisted(tmp_path):
    ledger = ledger_module().PhaseLedger(tmp_path)
    ledger.reserve('native', 'production', 'case')
    for outcome in [
        {'status': 'failed', 'elapsed_seconds': 1.0, 'error_message': 'PRIVATE_SECRET'},
        {'status': 'failed', 'elapsed_seconds': -1.0},
        {'status': 'failed', 'elapsed_seconds': 1.0, 'usage': {'total_tokens': -2}},
        {'status': 'failed', 'elapsed_seconds': 1.0, 'usage': {'raw_response': 'PRIVATE_SECRET'}},
    ]:
        with pytest.raises(ValueError):
            ledger.finish('case', outcome)
    assert 'PRIVATE_SECRET' not in ''.join(p.read_text() for p in tmp_path.rglob('*.json'))


def test_symlink_ledger_root_is_rejected(tmp_path):
    real = tmp_path/'real'
    real.mkdir()
    link = tmp_path/'link'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        ledger_module().PhaseLedger(link)


def test_inconsistent_complete_usage_cannot_become_reported_savings(tmp_path):
    ledger = ledger_module().PhaseLedger(tmp_path)
    ledger.reserve('learned', 'generation', 'case')
    for value in [{'coverage_complete': True, 'total_tokens': None},
                  {'coverage_complete': True, 'total_tokens': 25, 'known_total_tokens': 10},
                  {'coverage_complete': True, 'total_tokens': 25, 'missing_observations': 1}]:
        with pytest.raises(ValueError):
            ledger.finish('case', {'status': 'completed', 'elapsed_seconds': 1.0, 'usage': value})


def test_cached_tokens_are_separate_and_absent_cache_write_stays_unknown(tmp_path):
    module = ledger_module()
    ledger = module.PhaseLedger(tmp_path)
    ledger.reserve('learned', 'production', 'case')
    value = {**usage(200), 'cache_read_tokens': 150, 'cache_write_tokens': None,
             'input_tokens': 30, 'output_tokens': 20}
    ledger.finish('case', {'status': 'completed', 'elapsed_seconds': 1.0, 'usage': value})
    summary = module.summarize_phases(ledger.records())
    assert summary['total_tokens'] == 200 and summary['cache_read_tokens'] == 150
    assert summary['cache_write_tokens'] is None and summary['known_cache_write_tokens'] == 0
