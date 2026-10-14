import importlib
import json
import os

import pytest

from rpe_harness.path_binding import BoundExecution, read_source
from rpe_harness.runtime import HarnessRuntime


def request(root, *, event='json-1', style='pretty', output='out.json'):
    source = root / 'input.json'
    if not source.exists():
        source.write_text('{ "数字": [1, 1.0, 1e0, -0], "文本":"\\u4e2d", "真":true }', encoding='utf-8')
    return {'schema_version': 2, 'event_id': event, 'capability': 'json.normalize',
            'contract_version': 1, 'goal': '仅格式化JSON并保留所有数值词法',
            'constraints': ['preserve_original', 'preserve_order'],
            'execution': {'input_path': str(source), 'output_path': str(root / output), 'style': style}}


def test_json_real_file_run_and_replay_have_plan_bound_byte_metrics(tmp_path):
    original = request(tmp_path)
    before = (tmp_path / 'input.json').read_bytes()
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    result = runtime.run(original)
    assert result['status'] == 'succeeded'
    assert result['artifact']['format'] == 'json'
    assert result['artifact']['actual_executor']['executor_id'] == 'builtin/json-normalize'
    assert 'input_rows' not in result['artifact']
    assert result['artifact']['input_bytes'] == len(before)
    assert (tmp_path / 'input.json').read_bytes() == before
    assert b'1.0' in (tmp_path / 'out.json').read_bytes()
    assert runtime.run(original)['run_id'] == result['run_id']
    from rpe_harness.storage import EventConflict
    changed = json.loads(json.dumps(original))
    changed['execution']['style'] = 'minify'
    with pytest.raises(EventConflict):
        runtime.run(changed)


def test_bounded_source_read_rejects_large_file_and_symlink(tmp_path):
    source = tmp_path / 'file.json'
    source.write_bytes(b'123456789')
    try:
        read_source(source, max_bytes=4)
    except TypeError:
        pytest.fail('bounded descriptor source reads are unavailable')
    except ValueError:
        pass
    else:
        pytest.fail('source exceeded its read budget')
    link = tmp_path / 'link.json'
    link.symlink_to(source)
    with pytest.raises((ValueError, OSError)):
        read_source(link, max_bytes=20)


def test_json_feedback_survives_new_runtime_and_cannot_become_csv_recipe(tmp_path):
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    result = runtime.run(request(tmp_path))
    assert result['status'] == 'succeeded'
    from rpe_harness.evolution import record_verified_feedback, cycle, EvolutionStore
    feedback = record_verified_feedback(result['run_id'], tmp_path / 'runs.sqlite')
    assert feedback['plan']['capability'] == 'json.normalize'
    assert HarnessRuntime(db_path=tmp_path / 'runs.sqlite').store.get(result['run_id'])['status'] == 'succeeded'
    summary = cycle(tmp_path / 'runs.sqlite', tmp_path)
    assert summary['verified_feedback_count'] == 1
    assert EvolutionStore(tmp_path / 'runs.sqlite').get_active('recipe', 'recipe') is None
    (tmp_path / 'out.json').write_text('true\n')
    assert record_verified_feedback(result['run_id'], tmp_path / 'runs.sqlite') is None


def test_json_recovery_requires_same_plan_and_no_publication(tmp_path, monkeypatch):
    from rpe_harness import runtime as module
    from rpe_harness.recovery import recover_run
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    real_prepare = module.prepare_plan
    def unavailable(plan):
        raise OSError('temporary producer unavailable')
    monkeypatch.setattr(module, 'prepare_plan', unavailable)
    failed = runtime.run(request(tmp_path))
    assert failed['status'] == 'failed' and not (tmp_path / 'out.json').exists()
    monkeypatch.setattr(module, 'prepare_plan', real_prepare)
    recovered = recover_run(runtime, failed['run_id'])
    assert recovered['status'] == 'succeeded'
    assert recover_run(runtime, failed['run_id'])['run_id'] == recovered['run_id']
    with pytest.raises(ValueError, match='failed|interrupted|publication'):
        recover_run(runtime, recovered['run_id'])


def test_bound_source_read_enforces_budget_without_resolving_replaced_names(tmp_path):
    import hashlib
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    source = workspace / 'input.json'
    source.write_bytes(b'123456')
    stage = tmp_path / 'state'
    stage.mkdir()
    binding = BoundExecution.capture({'execution': {'input_path': str(source),
        'output_path': str(workspace / 'out.json')}}, workspace, stage,
        hashlib.sha256(source.read_bytes()).hexdigest())
    try:
        source.rename(workspace / 'original.json')
        source.symlink_to(tmp_path / 'missing')
        with binding.activate():
            assert read_source(source, max_bytes=6) == b'123456'
            with pytest.raises(ValueError, match='budget'):
                read_source(source, max_bytes=5)
    finally:
        binding.close()


def test_json_process_exit_before_execution_recovers_on_restart(tmp_path):
    import subprocess
    import sys
    from rpe_harness.recovery import recover_run
    payload = request(tmp_path, style='minify')
    file = tmp_path / 'request.json'
    file.write_text(json.dumps(payload))
    program = '''import json,os,sys
from rpe_harness.runtime import HarnessRuntime
runtime=HarnessRuntime(db_path=sys.argv[2])
original=runtime.store.step
def checkpoint(run_id,stage,*args,**kwargs):
    original(run_id,stage,*args,**kwargs)
    if stage=='execute': os._exit(77)
runtime.store.step=checkpoint
runtime.run(json.load(open(sys.argv[1])))
'''
    child = subprocess.run([sys.executable, '-B', '-c', program, str(file),
        str(tmp_path / 'runs.sqlite')], capture_output=True, text=True, timeout=20)
    assert child.returncode == 77
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    interrupted = runtime.store.list()[0]
    assert interrupted['status'] == 'interrupted' and not (tmp_path / 'out.json').exists()
    restored = recover_run(runtime, interrupted['run_id'])
    assert restored['status'] == 'succeeded'
    assert (tmp_path / 'out.json').read_text() == '{"数字":[1,1.0,1e0,-0],"文本":"\\u4e2d","真":true}\n'


@pytest.mark.parametrize('changed', ['input.json', 'out.json'])
def test_json_feedback_rejects_grown_files_without_unbounded_preliminary_read(tmp_path, monkeypatch, changed):
    from pathlib import Path
    from rpe_harness.evolution import record_verified_feedback
    result = HarnessRuntime(db_path=tmp_path / 'runs.sqlite').run(request(tmp_path))
    grown = tmp_path / changed
    budget = 1024 * 1024 if changed == 'input.json' else 16 * 1024 * 1024
    with grown.open('wb') as stream:
        stream.truncate(budget + 1)
    old_read = Path.read_bytes
    def prohibit_unbounded(path):
        if path == grown:
            raise AssertionError('unbounded preliminary file read')
        return old_read(path)
    monkeypatch.setattr(Path, 'read_bytes', prohibit_unbounded)
    assert record_verified_feedback(result['run_id'], tmp_path / 'runs.sqlite') is None


def test_json_recovery_rejects_grown_source_before_allocating_it(tmp_path, monkeypatch):
    from pathlib import Path
    from rpe_harness import runtime as module
    from rpe_harness.recovery import recover_run
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    def unavailable(plan):
        raise OSError('temporary producer unavailable')
    monkeypatch.setattr(module, 'prepare_plan', unavailable)
    failed = runtime.run(request(tmp_path))
    source = tmp_path / 'input.json'
    with source.open('wb') as stream:
        stream.truncate(1024 * 1024 + 1)
    old_read = Path.read_bytes
    def prohibit_unbounded(path):
        if path == source:
            raise AssertionError('unbounded recovery source read')
        return old_read(path)
    monkeypatch.setattr(Path, 'read_bytes', prohibit_unbounded)
    with pytest.raises(ValueError, match='budget'):
        recover_run(runtime, failed['run_id'])


@pytest.mark.parametrize('changed', ['source', 'staging'])
def test_json_publication_keeps_read_budgets_after_oracle_seal(tmp_path, monkeypatch, changed):
    from pathlib import Path
    from rpe_harness.execution import publish_output, cleanup_staging
    from rpe_harness import execution
    from rpe_harness.tasks import plan_from_request, prepare_plan, verify_plan
    plan = plan_from_request(request(tmp_path))
    prepared = prepare_plan(plan)
    assert verify_plan(prepared, plan)['passed'] is True
    path = Path(plan.input_path if changed == 'source' else prepared['staging_path'])
    budget = 1024 * 1024 if changed == 'source' else 16 * 1024 * 1024
    with path.open('wb') as stream:
        stream.truncate(budget + 1)
    original_read = Path.read_bytes
    def prohibit_unbounded(file):
        if file == path:
            raise AssertionError('unbounded publication read')
        return original_read(file)
    monkeypatch.setattr(Path, 'read_bytes', prohibit_unbounded)
    original_source = execution.read_source
    def require_budget(file, max_bytes=None):
        if Path(file) == path and max_bytes is None:
            raise AssertionError('unbounded bound source read')
        return original_source(file, max_bytes=max_bytes)
    monkeypatch.setattr(execution, 'read_source', require_budget)
    try:
        with pytest.raises(ValueError, match='budget'):
            publish_output(prepared, plan)
        assert not Path(plan.output_path).exists()
    finally:
        cleanup_staging(prepared)
