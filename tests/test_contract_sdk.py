"""Tests for Declarative Contract Plugin SDK (rpe.contract/v1) and built-in contracts."""
from __future__ import annotations

import json
from pathlib import Path

from rpe_harness.contract_sdk import ContractPlugin, DEFAULT_REGISTRY, PluginExecutionPlan
from rpe_harness.runtime import HarnessRuntime


def test_tabular_filter_preserves_leading_zeros_order_and_original(tmp_path: Path):
    src = tmp_path / 'users.csv'
    out = tmp_path / 'active.csv'
    original = 'id,status,code\n001,active,0042\n002,inactive,0007\n003,active,0099\n'
    src.write_text(original, encoding='utf-8')

    rt = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    res = rt.run({
        'schema_version': 2,
        'event_id': 'evt-filter-1',
        'capability': 'tabular.filter',
        'goal': 'Filter active users while preserving leading zeros',
        'constraints': ['preserve_original', 'preserve_order'],
        'execution': {
            'input_path': str(src),
            'output_path': str(out),
            'column': 'status',
            'operator': 'equals',
            'value': 'active',
        },
    })
    assert res['status'] == 'succeeded', res
    assert res['verification']['passed'] is True
    assert res['artifact']['input_rows'] == 3
    assert res['artifact']['output_rows'] == 2
    assert res['artifact']['removed_rows'] == 1
    assert src.read_text(encoding='utf-8') == original
    assert out.read_text(encoding='utf-8') == 'id,status,code\n001,active,0042\n003,active,0099\n'


def test_tabular_filter_in_and_rejects_missing_column_or_overwrite(tmp_path: Path):
    src = tmp_path / 'items.csv'
    out = tmp_path / 'picked.csv'
    src.write_text('sku,tier\nA1,gold\nB2,bronze\nC3,silver\n', encoding='utf-8')
    rt = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')

    bad = rt.run({
        'schema_version': 2,
        'event_id': 'evt-filter-bad-col',
        'capability': 'tabular.filter',
        'goal': 'Invalid column',
        'execution': {
            'input_path': str(src),
            'output_path': str(out),
            'column': 'nonexistent',
            'operator': 'not_empty',
        },
    })
    assert bad['status'] == 'rejected'
    assert not out.exists()

    ok = rt.run({
        'schema_version': 2,
        'event_id': 'evt-filter-in',
        'capability': 'tabular.filter',
        'goal': 'Pick gold and silver',
        'execution': {
            'input_path': str(src),
            'output_path': str(out),
            'column': 'tier',
            'operator': 'in',
            'values': ['gold', 'silver'],
        },
    })
    assert ok['status'] == 'succeeded'
    assert out.read_text(encoding='utf-8') == 'sku,tier\nA1,gold\nC3,silver\n'

    # Overwriting existing output is forbidden
    dup = rt.run({
        'schema_version': 2,
        'event_id': 'evt-filter-overwrite',
        'capability': 'tabular.filter',
        'goal': 'Attempt overwrite',
        'execution': {
            'input_path': str(src),
            'output_path': str(out),
            'column': 'tier',
            'operator': 'not_empty',
        },
    })
    assert dup['status'] == 'rejected'


def test_json_patch_set_delete_and_strict_type_verification(tmp_path: Path):
    src = tmp_path / 'config.json'
    out = tmp_path / 'config.patched.json'
    initial = {'service': 'rpe', 'retries': 3, 'debug': False, 'legacy': 'drop-me', 'nested': {'a': 1}}
    src.write_text(json.dumps(initial, indent=2), encoding='utf-8')

    rt = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    res = rt.run({
        'schema_version': 2,
        'event_id': 'evt-patch-1',
        'capability': 'json.patch',
        'goal': 'Enable strict mode and drop legacy key',
        'constraints': ['preserve_original'],
        'execution': {
            'input_path': str(src),
            'output_path': str(out),
            'operations': [
                {'op': 'set', 'key': 'debug', 'value': True},
                {'op': 'set', 'key': 'nested.b', 'value': [1, 2]},
                {'op': 'delete', 'key': 'legacy'},
            ],
        },
    })
    assert res['status'] == 'succeeded', res
    assert res['verification']['passed'] is True
    assert json.loads(src.read_text(encoding='utf-8')) == initial
    patched = json.loads(out.read_text(encoding='utf-8'))
    assert patched == {'service': 'rpe', 'retries': 3, 'debug': True, 'nested': {'a': 1, 'b': [1, 2]}}

    # Deleting missing key fails cleanly at admission
    bad = rt.run({
        'schema_version': 2,
        'event_id': 'evt-patch-missing',
        'capability': 'json.patch',
        'goal': 'Delete absent key',
        'execution': {
            'input_path': str(src),
            'output_path': str(tmp_path / 'never.json'),
            'operations': [{'op': 'delete', 'key': 'does.not.exist'}],
        },
    })
    assert bad['status'] == 'rejected'
    assert not (tmp_path / 'never.json').exists()


def test_custom_third_party_plugin_and_oracle_rejection_blocks_publish(tmp_path: Path):
    # 1. Honest custom plugin
    good_plugin = ContractPlugin(
        capability='acme.upper',
        executor_id='plugin/acme-upper',
        input_formats=('txt',),
        validate_options=lambda opts, raw: {},
        execute=lambda plan, raw: (raw.decode('utf-8').upper().encode('utf-8'), {}),
        verify=lambda plan, raw, staged: {
            'passed': staged == raw.decode('utf-8').upper().encode('utf-8'),
            'checks': {'uppercased': staged == raw.decode('utf-8').upper().encode('utf-8')},
        },
        description='Test uppercase contract',
    )
    # 2. Buggy plugin whose execute output fails independent oracle verification
    buggy_plugin = ContractPlugin(
        capability='acme.buggy',
        executor_id='plugin/acme-buggy',
        input_formats=('txt',),
        validate_options=lambda opts, raw: {},
        execute=lambda plan, raw: (b'CORRUPTED', {}),
        verify=lambda plan, raw, staged: {
            'passed': staged == raw,
            'checks': {'matches_expected': staged == raw},
        },
        description='Buggy contract that oracle must block',
    )
    DEFAULT_REGISTRY.register(good_plugin)
    DEFAULT_REGISTRY.register(buggy_plugin)
    try:
        src = tmp_path / 'note.txt'
        src.write_text('hello rpe\n', encoding='utf-8')
        out_ok = tmp_path / 'note.upper.txt'
        out_bad = tmp_path / 'note.buggy.txt'

        rt = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
        ok = rt.run({
            'schema_version': 2,
            'event_id': 'evt-plugin-ok',
            'capability': 'acme.upper',
            'goal': 'Uppercase text',
            'execution': {'input_path': str(src), 'output_path': str(out_ok)},
        })
        assert ok['status'] == 'succeeded'
        assert out_ok.read_text(encoding='utf-8') == 'HELLO RPE\n'

        # Roundtrip PluginExecutionPlan serialization
        plan_dict = ok['steps'][0]['evidence']['plan']
        reconstructed = PluginExecutionPlan.from_dict(plan_dict)
        assert reconstructed.sha256 == ok['artifact']['plan_sha256']

        # Buggy plugin MUST be blocked by oracle before publication
        blocked = rt.run({
            'schema_version': 2,
            'event_id': 'evt-plugin-bad',
            'capability': 'acme.buggy',
            'goal': 'Should fail oracle',
            'execution': {'input_path': str(src), 'output_path': str(out_bad)},
        })
        assert blocked['status'] == 'failed'
        assert blocked['verification']['passed'] is False
        assert not out_bad.exists()
    finally:
        DEFAULT_REGISTRY.unregister('acme.upper')
        DEFAULT_REGISTRY.unregister('acme.buggy')
