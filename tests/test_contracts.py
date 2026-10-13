"""Execution authority and original-request binding, without a host or model."""
import importlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from rpe_harness.runtime import HarnessRuntime


def contract_module():
    assert importlib.util.find_spec('rpe_harness.contracts'), 'immutable execution contracts are missing'
    return importlib.import_module('rpe_harness.contracts')


def request(tmp_path, *, version=2, event_id='contracts-1', suffix='csv'):
    source = tmp_path / ('source.' + suffix)
    if suffix == 'csv':
        source.write_text('id,group\n1,A\n2,A\n1,B\n', encoding='utf-8-sig')
    else:
        from openpyxl import Workbook
        book = Workbook()
        book.active.title = 'Orders'
        for row in [['id', 'group'], ['1', 'A'], ['2', 'A'], ['1', 'B']]:
            book.active.append(row)
        book.create_sheet('Other').append(['untouched'])
        book.save(source)
        book.close()
    payload = {'schema_version': version, 'event_id': event_id,
               'execution': {'input_path': str(source), 'output_path': str(tmp_path / ('result.' + suffix)),
                             'key_columns': ['id']}}
    if version == 1:
        payload['state'] = {'task_type': 'data.processing', 'goal': '保留首条',
                            'required_capability': 'tabular.deduplicate', 'input_format': suffix,
                            'constraints': ['preserve_original', 'preserve_order']}
    else:
        payload.update(capability='tabular.deduplicate', goal='保留首条',
                       constraints=['preserve_original', 'preserve_order'])
    return payload


@pytest.mark.parametrize('task_type', [None, 'unknown', 'document.writing'])
def test_v2_explicit_capability_executes_without_classification(tmp_path, monkeypatch, task_type):
    payload = request(tmp_path)
    if task_type is not None:
        payload['task_type'] = task_type
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    monkeypatch.setattr(runtime.router, 'route', lambda *a, **k: pytest.fail('advisory classification gates execution'))
    result = runtime.run(payload)
    assert result['status'] == 'succeeded'
    assert (tmp_path / 'result.csv').read_text(encoding='utf-8-sig') == 'id,group\n1,A\n2,A\n'
    plan = next(step['evidence'] for step in result['steps'] if step['stage'] == 'plan')
    assert result['verification']['plan_sha256'] == plan['plan_sha256']
    assert result['artifact']['plan_sha256'] == plan['plan_sha256']


def test_external_metadata_cannot_replace_registered_executor(tmp_path):
    payload = request(tmp_path)
    metadata = tmp_path / 'metadata'
    metadata.mkdir()
    (metadata / 'skill_index.json').write_text(json.dumps({'skills': [{
        'skill_id': 'aaa/unregistered', 'name': 'CSV', 'description': 'metadata only',
        'task_types': ['data.processing'], 'input_formats': ['csv'], 'priority': 99,
        'capabilities': ['tabular.deduplicate']}]}))
    runtime = HarnessRuntime(data_dir=metadata, db_path=tmp_path / 'runs.sqlite')
    result = runtime.run(payload)
    assert result['status'] == 'succeeded'
    assert result['skill_id'] == 'builtin/csv-deduplicate'


def test_original_request_keys_cannot_be_replaced_by_writer(tmp_path, monkeypatch):
    from rpe_harness import execution
    payload = request(tmp_path, version=1)
    original = execution.prepare_deduplication

    def substitute(*args, **kwargs):
        prepared = original(*args, **kwargs)
        prepared['key_columns'] = ['group']
        Path(prepared['staging_path']).write_text('id,group\n1,A\n1,B\n', encoding='utf-8-sig')
        return prepared

    monkeypatch.setattr(execution, 'prepare_deduplication', substitute)
    result = HarnessRuntime(db_path=tmp_path / 'runs.sqlite').run(payload)
    assert result['status'] == 'failed'
    assert result['stage'] == 'verify'
    assert result['verification']['passed'] is False
    assert not (tmp_path / 'result.csv').exists()


def test_plan_is_deeply_frozen_and_v1_v2_share_semantic_sha(tmp_path):
    module = contract_module()
    v1 = request(tmp_path, version=1)
    v2 = request(tmp_path)
    v2.update(goal='另一种表述', session_id='new-host-session', source_ref='host-event')
    first = module.ExecutionPlan.from_request(v1)
    second = module.ExecutionPlan.from_request(v2)
    assert first.sha256 == second.sha256
    assert isinstance(first.key_columns, tuple) and isinstance(first.constraints, tuple)
    with pytest.raises(FrozenInstanceError):
        first.key_columns = ('group',)
    v1['execution']['key_columns'].append('group')
    assert first.key_columns == ('id',)
    assert module.ExecutionPlan.from_dict(first.to_dict()).sha256 == first.sha256


@pytest.mark.parametrize('field,value', [
    ('key_columns', ['group']), ('format', 'xlsx'), ('input_sha256', '0' * 64),
    ('sheet', 'Other'), ('keep', 'last'), ('encoding', 'utf-16'),
    ('delimiter', ';'), ('constraints', ['preserve_formulas']),
    ('output_path', 'elsewhere.csv'), ('input_path', 'other.csv'),
])
def test_every_producer_parameter_is_bound_to_original_plan(tmp_path, field, value):
    from rpe_harness import execution, verification
    plan = contract_module().ExecutionPlan.from_request(request(tmp_path))
    prepared = execution.prepare_deduplication(**plan.execution_kwargs())
    try:
        if field in {'input_path', 'output_path'}:
            value = str(tmp_path / value)
            if field == 'input_path':
                Path(value).write_bytes(Path(plan.input_path).read_bytes())
        prepared[field] = value
        report = verification.verify_deduplication(prepared, plan=plan)
        assert report['passed'] is False
        assert report['checks']['request_bound'] is False
        with pytest.raises(ValueError):
            execution.publish_output(prepared, plan=plan)
        assert not Path(plan.output_path).exists()
    finally:
        execution.cleanup_staging(prepared)


def test_input_change_after_plan_is_not_reinterpreted_as_new_truth(tmp_path):
    from rpe_harness import execution, verification
    plan = contract_module().ExecutionPlan.from_request(request(tmp_path))
    Path(plan.input_path).write_text('id,group\n1,Z\n2,Y\n', encoding='utf-8-sig')
    prepared = execution.prepare_deduplication(**plan.execution_kwargs())
    try:
        assert verification.verify_deduplication(prepared, plan=plan)['passed'] is False
        with pytest.raises(ValueError):
            execution.publish_output(prepared, plan=plan)
    finally:
        execution.cleanup_staging(prepared)


def test_xlsx_default_sheet_is_frozen_before_writer(tmp_path):
    from rpe_harness import execution, verification
    plan = contract_module().ExecutionPlan.from_request(request(tmp_path, suffix='xlsx'))
    assert plan.sheet == 'Orders'
    prepared = execution.prepare_deduplication(**plan.execution_kwargs())
    try:
        prepared['sheet'] = 'Other'
        assert verification.verify_deduplication(prepared, plan=plan)['passed'] is False
    finally:
        execution.cleanup_staging(prepared)


@pytest.mark.parametrize('mutation', ['capability', 'constraint', 'key', 'format', 'keep'])
def test_v2_invalid_contract_never_calls_writer(tmp_path, monkeypatch, mutation):
    from rpe_harness import execution
    payload = request(tmp_path)
    if mutation == 'capability':
        payload['capability'] = 'file.delete'
    elif mutation == 'constraint':
        payload['constraints'].append('send_email')
    elif mutation == 'key':
        payload['execution']['key_columns'] = ['missing']
    elif mutation == 'format':
        payload['input_format'] = 'xlsx'
    else:
        payload['execution']['keep'] = 'last'
    monkeypatch.setattr(execution, 'prepare_deduplication', lambda *a, **k: pytest.fail('invalid contract executed'))
    result = HarnessRuntime(db_path=tmp_path / 'runs.sqlite').run(payload)
    assert result['status'] == 'rejected'
    assert not (tmp_path / 'result.csv').exists()


def test_v1_unknown_classification_remains_strict(tmp_path):
    payload = request(tmp_path, version=1)
    payload['state']['task_type'] = 'unknown'
    result = HarnessRuntime(db_path=tmp_path / 'runs.sqlite').run(payload)
    assert result['status'] == 'rejected'


def test_v2_schema_requires_business_contract_not_category():
    schema = contract_module().request_schema()
    assert schema['properties']['schema_version']['const'] == 2
    assert {'event_id', 'capability', 'goal', 'execution'} <= set(schema['required'])
    assert 'task_type' not in schema['required']


def test_mutating_caller_payload_after_admission_cannot_replace_saved_request(tmp_path, monkeypatch):
    payload = request(tmp_path)
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    claim = runtime.store.claim_event

    def mutate_caller(source, event_id, submitted):
        admitted = claim(source, event_id, submitted)
        payload['execution']['key_columns'] = ['group']
        return admitted

    monkeypatch.setattr(runtime.store, 'claim_event', mutate_caller)
    result = runtime.run(payload)
    assert result['status'] == 'succeeded'
    assert result['event']['execution']['key_columns'] == ['id']
    assert Path(result['artifact']['path']).read_text(encoding='utf-8-sig') == 'id,group\n1,A\n2,A\n'


def test_parameter_swap_after_good_verification_cannot_publish(tmp_path, monkeypatch):
    from rpe_harness import verification
    original = verification.verify_deduplication

    def mutate_verified(prepared, plan=None):
        verdict = original(prepared, plan=plan)
        assert verdict['passed']
        prepared['key_columns'] = ['group']
        return verdict

    monkeypatch.setattr(verification, 'verify_deduplication', mutate_verified)
    result = HarnessRuntime(db_path=tmp_path / 'runs.sqlite').run(request(tmp_path))
    assert result['status'] == 'failed' and result['stage'] == 'publish'
    assert not (tmp_path / 'result.csv').exists()


@pytest.mark.parametrize('filename,contents', [
    ('skill_index.json', '{invalid json'),
    ('skill_annotations.json', '{"annotations": {"a": []}}'),
    ('causal_graph.json', '{invalid graph'),
    ('causal_graph.json', '{"profiles":{"a":{"skill_id":"a","alternatives":123}}}'),
])
def test_invalid_advisory_data_cannot_disable_business_execution(tmp_path, filename, contents):
    payload = request(tmp_path)
    metadata = tmp_path / 'metadata'
    metadata.mkdir()
    if filename == 'skill_annotations.json':
        (metadata / 'skill_index.json').write_text(json.dumps({'skills': [
            {'skill_id': 'a', 'name': 'A', 'description': 'metadata'}]}))
    (metadata / filename).write_text(contents)
    runtime = HarnessRuntime(data_dir=metadata, db_path=tmp_path / 'runs.sqlite')
    assert runtime.run(payload)['status'] == 'succeeded'
    assert runtime.stats()['metadata_issues']


def test_persisted_verifier_revision_cannot_claim_fresh_verification_by_other_code(tmp_path):
    from rpe_harness import execution, verification
    module = contract_module()
    plan = module.ExecutionPlan.from_request(request(tmp_path))
    saved = plan.to_dict()
    saved['verifier_revision'] = '0' * 64
    stale = module.ExecutionPlan.from_dict(saved)
    prepared = execution.prepare_deduplication(**stale.execution_kwargs())
    try:
        report = verification.verify_deduplication(prepared, plan=stale)
        assert report['passed'] is False
        assert report['checks']['verifier_revision'] is False
    finally:
        execution.cleanup_staging(prepared)


def test_runtime_uses_candidate_and_records_actual_code_under_frozen_plan(tmp_path, monkeypatch):
    from rpe_harness import execution
    from rpe_harness.evolution import candidates
    original_prepare = execution.prepare_deduplication

    def candidate(plan, db_path):
        prepared = original_prepare(**plan.execution_kwargs())
        prepared.update(candidate_version_id='candidate-v1', candidate_code_sha256='a' * 64)
        return prepared

    monkeypatch.setattr(candidates, 'prepare_candidate', candidate)
    monkeypatch.setattr(execution, 'prepare_deduplication', lambda **kwargs: pytest.fail('activated candidate ignored'))
    result = HarnessRuntime(db_path=tmp_path / 'runs.sqlite').run(request(tmp_path))
    assert result['status'] == 'succeeded'
    actual = result['artifact']['actual_executor']
    assert actual['kind'] == 'candidate' and actual['code_sha256'] == 'a' * 64
    assert actual['candidate_version_id'] == 'candidate-v1'
    assert actual['plan_sha256'] == result['artifact']['plan_sha256']
    assert any(step['evidence'].get('actual_executor') == actual for step in result['steps'])


def test_bad_candidate_is_quarantined_and_builtin_reuses_same_original_plan(tmp_path, monkeypatch):
    from rpe_harness import execution
    from rpe_harness.evolution import candidates
    original_prepare = execution.prepare_deduplication
    observed = []

    def candidate(plan, db_path):
        prepared = original_prepare(**plan.execution_kwargs())
        prepared.update(candidate_version_id='bad-v1', candidate_code_sha256='b' * 64)
        Path(prepared['staging_path']).write_text('id,group\n1,A\n1,B\n', encoding='utf-8-sig')
        return prepared

    monkeypatch.setattr(candidates, 'prepare_candidate', candidate)
    monkeypatch.setattr(candidates, 'quarantine_candidate', lambda version, db, reason: observed.append((version, reason)))
    result = HarnessRuntime(db_path=tmp_path / 'runs.sqlite').run(request(tmp_path))
    assert result['status'] == 'succeeded'
    assert observed and observed[0][0] == 'bad-v1'
    recovery = [step['evidence'] for step in result['steps'] if step['stage'] == 'candidate_fallback']
    assert recovery[0]['plan_sha256'] == result['artifact']['plan_sha256']
    assert result['artifact']['actual_executor']['kind'] == 'builtin'
    assert not list(tmp_path.glob('.rpe-*'))


def test_candidate_publication_failure_never_retries_another_executor(tmp_path, monkeypatch):
    from rpe_harness import execution
    from rpe_harness.evolution import candidates
    original_prepare = execution.prepare_deduplication

    def candidate(plan, db_path):
        prepared = original_prepare(**plan.execution_kwargs())
        prepared.update(candidate_version_id='candidate-v1', candidate_code_sha256='a' * 64)
        return prepared

    monkeypatch.setattr(candidates, 'prepare_candidate', candidate)
    monkeypatch.setattr(candidates, 'quarantine_candidate', lambda *a: pytest.fail('publication ambiguity cannot trigger fallback'))
    monkeypatch.setattr(execution, 'prepare_deduplication', lambda **kwargs: pytest.fail('unsafe fallback'))
    monkeypatch.setattr(execution, 'publish_output', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('publication unknown')))
    result = HarnessRuntime(db_path=tmp_path / 'runs.sqlite').run(request(tmp_path))
    assert result['status'] == 'failed' and result['stage'] == 'publish'
    assert not (tmp_path / 'result.csv').exists()
