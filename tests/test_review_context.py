"""Explicit source imports stay bounded, session-local and independently verifiable."""
import copy
import importlib
import importlib.util
import sqlite3

import pytest


def api():
    assert importlib.util.find_spec('rpe_harness.review_context'), 'review context bridge is absent'
    return importlib.import_module('rpe_harness.review_context')


def sources():
    return [{'source': 'user-attachment', 'event_id': 'decision-1',
             'payload': {'text': '此前的无关聊天。' * 1000 + '\n业务键为订单号。', 'private': 'not for model'},
             'facts': {'key_columns': ['订单号']}}]


def test_import_projects_only_explicit_fact_and_literal_excerpt(tmp_path):
    bridge = api()
    original = sources()
    bundle = bridge.prepare_context(tmp_path / 'state.sqlite', original, 'native-1')
    assert bundle['session_id'] == 'native-1'
    assert len(bundle['source_refs']) == 1
    model = bundle['model_sources'][0]
    assert model['facts'] == {'key_columns': ['订单号']}
    assert model['quotes']['key_columns'] == '订单号'
    assert 'private' not in str(model) and '无关聊天' not in str(model)
    original[0]['facts']['key_columns'] = ['篡改']
    assert bridge.validate_context(tmp_path / 'state.sqlite', bundle, 'native-1') == bundle
    assert bridge.prepare_context(tmp_path / 'state.sqlite', sources(), 'native-1') == bundle
    other = bridge.prepare_context(tmp_path / 'state.sqlite', sources(), 'native-2')
    assert other['source_refs'] != bundle['source_refs']


@pytest.mark.parametrize('alter', [
    lambda value: value[0].update(session_id='other'),
    lambda value: value[0]['facts'].update(shell='rm -rf /'),
    lambda value: value[0]['facts'].update(capability='json.normalize'),
    lambda value: value[0].update(payload={'text': 1}),
    lambda value: value[0].update(facts=[]),
    lambda value: value.extend(copy.deepcopy(value) * 8),
])
def test_invalid_imports_are_rejected_before_any_source_write(tmp_path, alter):
    bridge = api()
    value = sources()
    alter(value)
    with pytest.raises(ValueError):
        bridge.validate_sources(value)
    with pytest.raises(ValueError):
        bridge.prepare_context(tmp_path / 'state.sqlite', value, 'native-1')
    assert not (tmp_path / 'state.sqlite').exists()


@pytest.mark.parametrize('fault', ['session', 'ref', 'projection', 'source'])
def test_bundle_and_live_source_must_still_match(tmp_path, fault):
    bridge = api()
    path = tmp_path / 'state.sqlite'
    bundle = bridge.prepare_context(path, sources(), 'native-1')
    if fault == 'session':
        bundle['session_id'] = 'native-2'
    elif fault == 'ref':
        bundle['source_refs'][0]['sha256'] = '0' * 64
    elif fault == 'projection':
        bundle['model_sources'][0]['facts']['key_columns'] = ['hidden']
    else:
        with sqlite3.connect(path) as db:
            db.execute('UPDATE context_sources SET facts_json=?', ('{"key_columns":["hidden"]}',))
    with pytest.raises(ValueError):
        bridge.validate_context(path, bundle, 'native-1')


def test_source_can_explicitly_supply_the_supported_tab_delimiter(tmp_path):
    bridge = api()
    value = [{'source': 'explicit', 'event_id': 'tab',
              'payload': {'text': '使用真正制表符\t分隔。'}, 'facts': {'delimiter': '\t'}}]
    bundle = bridge.prepare_context(tmp_path / 'state.sqlite', value, 's')
    assert bundle['model_sources'][0]['facts'] == {'delimiter': '\t'}
    assert bundle['model_sources'][0]['quotes'] == {'delimiter': '\t'}
