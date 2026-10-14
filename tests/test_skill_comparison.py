"""No-model checks of preregistration, independent truth, and resume semantics."""
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import shlex
import subprocess

import pytest


def test_comparison_module_exists():
    assert importlib.util.find_spec('rpe_harness.experiments.skill_comparison') is not None


@pytest.fixture
def api():
    return importlib.import_module('rpe_harness.experiments.skill_comparison')


@pytest.fixture
def prepared(api, tmp_path):
    from rpe_harness.skill_catalog import SkillCatalog
    source = tmp_path / 'source'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx',
                    source / 'productivity/xlsx')
    catalog = SkillCatalog(tmp_path / 'db.sqlite', tmp_path / 'catalog')
    snapshot = catalog.sync(source)['snapshot_id']
    catalog.trust_adapter('productivity/xlsx', snapshot)
    plan = api.preregister(db=tmp_path / 'db.sqlite', owned_root=tmp_path / 'owned',
        catalog_root=tmp_path / 'catalog', hermes_root=tmp_path / 'hermes',
        report_dir=tmp_path / 'reports', snapshot_id=snapshot, timeout=60)
    return plan


def test_preregistration_is_frozen_paired_and_bounded(api, prepared):
    assert len(prepared['sessions']) == 24
    assert [row['arm'] for row in prepared['sessions']] == ['native', 'rpe', 'rpe', 'native'] * 6
    assert len(prepared['directed_probes']) == 2
    assert prepared['comparison_scope'] == '24 supported XLSX sessions; directed probes reported separately'
    assert 'integrations/hermes.py' in prepared['source_module_sha256']
    assert 'xlsx_resource.py' in prepared['source_module_sha256']
    assert 'runtime.py' in prepared['source_module_sha256']
    assert len({row['case_id'] for row in prepared['cases']}) == 6
    assert sum(case['temperature'] == 'cold' for case in prepared['cases']) == 1
    for left, right in zip(prepared['sessions'][::2], prepared['sessions'][1::2]):
        assert left['prompt_sha256'] == right['prompt_sha256']
        assert left['input_sha256'] == right['input_sha256']
    assert all(Path(row['workspace']).is_relative_to(prepared['owned_root'])
               for row in prepared['sessions'])
    assert json.loads((Path(prepared['report_dir']) / 'preregistration.json').read_text()) == prepared
    assert prepared['helper_python_version']['openpyxl']
    assert prepared['helper_python'] in (Path(prepared['sessions'][0]['workspace']) / 'prompt.txt').read_text()
    with pytest.raises(ValueError, match='frozen'):
        api.preregister(db=prepared['db'], owned_root=prepared['owned_root'],
            catalog_root=prepared['catalog_root'], hermes_root=prepared['hermes_root'],
            report_dir=prepared['report_dir'], snapshot_id=prepared['snapshot_id'], timeout=61)


def test_oracle_preserves_types_dates_empty_shapes_and_formula_order(api, prepared):
    by_id = {row['case_id']: row for row in prepared['cases']}
    mixed = by_id['rows_mixed']['expected']
    assert mixed['rows'][1] == ['001', 7, True, None, '2026-01-02T00:00:00']
    assert by_id['rows_empty']['expected'] == {'sheet': 'Empty', 'rows': []}
    assert by_id['rows_second']['expected']['sheet'] == 'Second'
    assert by_id['sheets_names']['expected']['defined_names'] == {'Total': "'Main'!$A$1"}
    formulas = by_id['formulas_all']['expected']['formulas']
    assert [(row['sheet'], row['cell']) for row in formulas] == [('Main', 'B1'), ('Main', 'A2'), ('Second', 'A1')]
    assert all(row['cached'] is None for row in formulas)
    assert not api.exact_json({'v': True}, {'v': 1})
    assert not api.exact_json({'rows': [[1], [2]]}, {'rows': [[2], [1]]})


def test_resume_keeps_failures_and_saves_each_session(api, prepared):
    calls = []
    def fail(prompt, workspace, db, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('controlled failure')
    first = api.run_comparison(prepared, max_sessions=2, runner=fail)
    assert first['completed_sessions'] == 2
    assert first['accepted_sessions'] == 0
    assert calls[0]['tool_enabled'] is False and calls[0]['entry_mode'] == 'optional'
    assert calls[1]['tool_enabled'] is True and calls[1]['entry_mode'] == 'skills'
    assert all(call['skill_snapshot_id'] == prepared['snapshot_id'] for call in calls)
    ids = [record['trial_id'] for record in first['records']]
    second = api.run_comparison(prepared, max_sessions=1, runner=fail)
    assert second['completed_sessions'] == 3
    assert [record['trial_id'] for record in second['records'][:2]] == ids
    assert len(calls) == 3
    assert all(record['usage']['total_tokens'] is None for record in second['records'])
    assert all(record['billed_cost'] is None for record in second['records'])


def test_native_artifact_truth_requires_native_skill_load(api, prepared):
    def native(prompt, workspace, db, **kwargs):
        case = prepared['cases'][0]
        (Path(workspace) / 'output.json').write_text(json.dumps(case['expected']))
        return {'completed': True, 'exit_code': 0, 'session_id': 'native-1',
                'process_cleanup': {'cleanup_complete': True}, 'events': [], 'usage': {}}
    summary = api.run_comparison(prepared, max_sessions=1, runner=native)
    assert summary['records'][0]['artifact_exact'] is True
    assert summary['records'][0]['accepted'] is False


def test_malformed_output_still_preserves_actual_usage(api, prepared):
    def native(prompt, workspace, db, **kwargs):
        (Path(workspace) / 'output.json').write_text('malformed')
        return {'session_id': 'observed-failure', 'usage': {'total_tokens': 123},
                'elapsed_seconds': 4.5, 'model_identity': {'requested_model': 'test-model'}}
    record = api.run_comparison(prepared, max_sessions=1, runner=native)['records'][0]
    assert record['status'] == 'failed'
    assert record['usage']['total_tokens'] == 123
    assert record['elapsed_seconds'] == 4.5


@pytest.mark.parametrize('fault', [None, 'wrong_skill', 'forged_command', 'mismatched_post', 'wrong_content',
    'full_ids', 'partial_ids', 'wrong_stream_id', 'extra_stream', 'missing_stream', 'duplicate_pre',
    'missing_api', 'cross_api', 'other_post_args', 'empty_stream_id', 'diagnostic_suffix',
    'duplicate_model', 'duplicate_post', 'missing_identity', 'incomplete_api', 'same_args', 'same_args_full_ids'])
def test_native_gate_requires_bound_skill_and_original_helper(api, prepared, fault):
    from rpe_harness.integrations.hermes import HostEventStore
    from rpe_harness.integrations.hermes_gate import digest_json
    from rpe_harness.skill_catalog import SkillCatalog
    trial, case = prepared['sessions'][0], prepared['cases'][0]
    session_root = Path(prepared['owned_root']) / 'native-evidence'
    view = SkillCatalog(prepared['db'], prepared['catalog_root']).materialize(session_root / 'home/skills')
    source = Path(view['materialized_path']) / 'productivity/xlsx/SKILL.md'
    helper = source.parent / 'scripts/xlsx_read.py'
    workspace = Path(trial['workspace'])
    argv = [prepared['helper_python'], str(helper), 'input.xlsx', '--json']
    with (workspace / 'output.json').open('w') as output:
        subprocess.run(argv, cwd=workspace, stdout=output, check=True)
    store = HostEventStore(prepared['db'])
    load_args = {'name': 'xlsx'}
    command = shlex.join(argv) + ' > output.json'
    if fault == 'forged_command':
        command = 'echo fabricated > output.json # ' + str(helper)
    if fault == 'diagnostic_suffix':
        command += '; echo "exit=$?"; ls -la output.json'
    terminal_args = {'command': command}
    common = {'session_id': 'native-proof', 'task_id': 'task', 'turn_id': 'turn', 'api_request_id': 'api'}
    load = {**common, 'tool_call_id': 'load', 'tool_name': 'skill_view',
            'arguments_sha256': digest_json(load_args)}
    terminal = {**common, 'tool_call_id': 'execute', 'tool_name': 'terminal',
                'arguments_sha256': digest_json(terminal_args)}
    if fault == 'cross_api':
        terminal['api_request_id'] = 'wrong-api'
    if fault == 'missing_identity':
        terminal.pop('turn_id')
    calls = [{'tool_name': 'skill_view', 'tool_call_id': 'load'},
             {'tool_name': 'terminal', 'tool_call_id': 'execute'}]
    other_args = {'path': 'output.json'}
    other = {**common, 'tool_call_id': 'other', 'tool_name': 'read_file',
             'arguments_sha256': digest_json(other_args)}
    if fault in ('other_post_args', 'same_args', 'same_args_full_ids'):
        calls.append({'tool_name': 'read_file', 'tool_call_id': 'other'})
    if fault in ('same_args', 'same_args_full_ids'):
        calls.append({'tool_name': 'read_file', 'tool_call_id': 'other-2'})
    if fault == 'duplicate_model':
        calls.append(dict(calls[0]))
    store.record('pre_api_request', common)
    if fault == 'incomplete_api':
        store.record('pre_api_request', {**common, 'api_request_id': 'incomplete-api'})
    if fault != 'missing_api':
        store.record('post_api_request', {**common, 'response': {'assistant_message': {
            'tool_calls': [{'id': call['tool_call_id'], 'name': call['tool_name']} for call in calls]}}})
    store.record('pre_tool_call', load)
    store.record('post_tool_call', {**load, 'status': 'loaded', 'skill_id':
        'other/skill' if fault == 'wrong_skill' else 'productivity/xlsx',
        'snapshot_id': prepared['snapshot_id'], 'skill_source_path': str(source),
        'content_sha256': '0' * 64 if fault == 'wrong_content' else prepared['skill_sha256']})
    store.record('pre_tool_call', terminal)
    if fault == 'duplicate_pre':
        store.record('pre_tool_call', terminal)
    store.record('post_tool_call', {**terminal, 'status': 'succeeded',
        'tool_call_id': 'another-call' if fault == 'mismatched_post' else 'execute'})
    if fault == 'duplicate_post':
        store.record('post_tool_call', {**terminal, 'status': 'succeeded'})
    if fault == 'other_post_args':
        store.record('pre_tool_call', other)
        store.record('post_tool_call', {**other, 'arguments_sha256': '0' * 64})
    if fault in ('same_args', 'same_args_full_ids'):
        for call_id in ('other', 'other-2'):
            store.record('pre_tool_call', {**other, 'tool_call_id': call_id})
            store.record('post_tool_call', {**other, 'tool_call_id': call_id, 'status': 'ok'})
    streams = [{'type': 'tool_use', 'name': 'skill_view', 'input': load_args},
               {'type': 'tool_use', 'name': 'terminal', 'input': terminal_args}]
    if fault == 'full_ids':
        streams[0]['tool_call_id'], streams[1]['tool_call_id'] = 'load', 'execute'
    elif fault == 'partial_ids':
        streams[0]['tool_call_id'] = 'load'
    elif fault == 'empty_stream_id':
        streams[0]['tool_call_id'] = ''
    elif fault == 'wrong_stream_id':
        streams[0]['tool_call_id'], streams[1]['tool_call_id'] = 'wrong', 'execute'
    elif fault == 'extra_stream':
        streams.append(dict(streams[0]))
    elif fault == 'missing_stream':
        streams.pop()
    elif fault == 'other_post_args':
        streams.append({'type': 'tool_use', 'name': 'read_file', 'input': other_args})
    elif fault in ('same_args', 'same_args_full_ids'):
        streams.extend([{'type': 'tool_use', 'name': 'read_file', 'input': other_args},
                        {'type': 'tool_use', 'name': 'read_file', 'input': other_args}])
        if fault == 'same_args_full_ids':
            for stream, call_id in zip(streams, ('load', 'execute', 'other', 'other-2')):
                stream['tool_call_id'] = call_id
    result = {'session_id': 'native-proof', 'session_root': str(session_root),
        'events': store.list(session_id='native-proof'),
        'stream_tool_calls': streams}
    gate = api._native_gate(result, prepared, trial, case)
    assert gate['verified'] is (fault in (None, 'full_ids', 'same_args', 'same_args_full_ids')), gate
    if gate['verified']:
        assert gate['stream_identity_policy'] == ('complete_actual_call_ids'
            if fault in ('full_ids', 'same_args_full_ids') else
            'counted_name_argument_equivalence_classes_without_stream_ids')
