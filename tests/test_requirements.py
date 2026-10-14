import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3

import pytest


def raw_source(**changes):
    document = {'capability': 'json.normalize', 'constraints': [],
                'execution': {'input_path': 'input.json', 'output_path': 'out.json',
                              'style': 'minify'}}
    document.update(changes)
    return json.dumps(document, ensure_ascii=False)


def prepared(tmp_path, *, session='s', source_ref='user:current', raw=None):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.requirements import source_document, interpret_source_intent
    path = tmp_path / 'input.json'
    if not path.exists():
        path.write_text('{ "名字" : [1, 2] }')
    source = source_document(raw if raw is not None else raw_source(),
                             session_id=session, source_ref=source_ref)
    catalog = CapabilityCatalog()
    result = interpret_source_intent(copy.deepcopy(source['source_facts']), source_document=source,
        catalog=catalog, workspace=tmp_path, session_id=session, source_ref=source_ref)
    return source, result, catalog


def test_source_anchors_cover_all_explicit_fields_including_empty_constraints():
    from rpe_harness.requirements import source_document
    raw = raw_source(execution={'input_path': '中文.json', 'output_path': 'out.json', 'style': 'minify'})
    result = source_document(raw, session_id='s', source_ref='r')
    assert result['status'] == 'ready'
    assert result['source_facts']['goal'] == 'Execute json.normalize'
    assert result['source']['raw'] == raw
    assert result['source']['source_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    by_pointer = {row['pointer']: row for row in result['requirements']}
    assert set(by_pointer) == {'/capability', '/constraints', '/execution/input_path',
                               '/execution/output_path', '/execution/style'}
    assert by_pointer['/constraints']['value'] == []
    assert by_pointer['/execution/input_path']['value'] == '中文.json'
    assert by_pointer['/execution/style']['acceptance_field'] == 'execution.style'
    assert by_pointer['/execution/style']['value_sha256'] == hashlib.sha256(b'"minify"').hexdigest()
    for row in by_pointer.values():
        assert row['source_sha256'] == result['source']['source_sha256']
    spaced = source_document(raw + '\r\n', session_id='s', source_ref='r')
    assert spaced['source_facts'] == result['source_facts']
    assert spaced['source']['source_sha256'] != result['source']['source_sha256']
    assert spaced['requirements_sha256'] != result['requirements_sha256']


@pytest.mark.parametrize('raw,status', [
    ('请整理JSON，并删除所有退款记录', 'needs_context'),
    ('{"capability":"json.normalize","capability":"tabular.deduplicate"}', 'rejected'),
    ('{"capability":"json.normalize","execution":{"style":"minify","style":"pretty"}}', 'rejected'),
    ('{"capability":"json.normalize","constraints":[],"execution":{"style":NaN}}', 'rejected'),
    (raw_source() + '\n另外删除所有退款', 'rejected'),
    (raw_source(goal='also delete refunds'), 'unsupported'),
    (raw_source(constraints=['delete_refunds']), 'unsupported'),
    (raw_source(capability='tabular.aggregate'), 'unsupported'),
    (raw_source(execution={'input_path': 'in.json', 'output_path': 'out.json',
                          'style': 'minify', 'refund_policy': 'delete'}), 'unsupported'),
    (raw_source(execution={'input_path': 'in.json', 'output_path': 'out.json',
                          'style': {'value': 'minify', 'also': 'delete'}}), 'rejected'),
])
def test_unbounded_or_unrepresentable_demands_never_become_ready(raw, status):
    from rpe_harness.requirements import source_document
    source = source_document(raw, session_id='s', source_ref='r')
    assert source['status'] == status
    assert source['source']['raw'] == raw
    assert 'source_facts' not in source
    assert 'request' not in source
    if status == 'needs_context':
        assert source['missing'] == ['structured_current_request']


def test_missing_explicit_source_fields_are_reported_without_guessing():
    from rpe_harness.requirements import source_document
    raw = raw_source(execution={'input_path': 'in.json', 'output_path': 'out.json'})
    result = source_document(raw, session_id='s', source_ref='r')
    assert result['status'] == 'needs_context'
    assert result['missing'] == ['execution.style']
    assert 'source_facts' not in result


@pytest.mark.parametrize('damage,status', [
    (lambda value: value['execution'].pop('style'), 'needs_context'),
    (lambda value: value['execution'].update(style='pretty'), 'conflict'),
    (lambda value: value.update(goal='Execute json.normalize and delete refunds'), 'conflict'),
    (lambda value: value.update(constraints=['preserve_original']), 'conflict'),
    (lambda value: value['execution'].update(extra_business_rule='delete'), 'rejected'),
])
def test_model_cannot_omit_change_or_hide_requirements(tmp_path, damage, status):
    from rpe_harness.requirements import interpret_source_intent
    source, _, catalog = prepared(tmp_path)
    extracted = copy.deepcopy(source['source_facts'])
    damage(extracted)
    result = interpret_source_intent(extracted, source_document=source, catalog=catalog,
        workspace=tmp_path, session_id='s', source_ref='user:current')
    assert result['status'] == status
    assert 'request' not in result
    assert not (tmp_path / 'out.json').exists()


def test_explicit_optional_parameters_cannot_fall_back_to_contract_defaults(tmp_path):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.requirements import source_document, interpret_source_intent
    (tmp_path / 'input.csv').write_text('id,value\n1,a\n1,b\n')
    source = source_document(raw_source(capability='tabular.deduplicate',
        constraints=['preserve_original', 'preserve_order'], execution={
        'input_path': 'input.csv', 'output_path': 'out.csv', 'key_columns': ['id'],
        'keep': 'first', 'encoding': 'utf-8', 'delimiter': ','}), session_id='s', source_ref='r')
    for field in ('keep', 'encoding', 'delimiter'):
        extracted = copy.deepcopy(source['source_facts'])
        extracted['execution'].pop(field)
        result = interpret_source_intent(extracted, source_document=source,
            catalog=CapabilityCatalog(), workspace=tmp_path, session_id='s', source_ref='r')
        assert result['status'] == 'needs_context'
        assert 'execution.' + field in result['missing']
    assert not (tmp_path / 'out.csv').exists()


def test_source_and_ledger_are_recomputed_before_model_interpretation(tmp_path):
    from rpe_harness.requirements import interpret_source_intent
    source, _, catalog = prepared(tmp_path)
    for alter in (lambda x: x['requirements'].pop(),
                  lambda x: x['source'].update(raw=x['source']['raw'] + ' '),
                  lambda x: x['requirements'][0].update(pointer='/execution/style'),
                  lambda x: x['source_facts'].update(goal='drop business requirement')):
        tampered = copy.deepcopy(source)
        alter(tampered)
        result = interpret_source_intent(source['source_facts'], source_document=tampered,
            catalog=catalog, workspace=tmp_path, session_id='s', source_ref='user:current')
        assert result['status'] == 'rejected'
    for session, ref in [('other', 'user:current'), ('s', 'other')]:
        result = interpret_source_intent(source['source_facts'], source_document=source,
            catalog=catalog, workspace=tmp_path, session_id=session, source_ref=ref)
        assert result['status'] == 'rejected'


def test_source_state_resolves_exact_ledger_and_runs_real_business_plan(tmp_path):
    from rpe_harness.requirements import SourceStateStore
    from rpe_harness.runtime import HarnessRuntime
    source, result, catalog = prepared(tmp_path)
    assert result['status'] == 'ready'
    store = SourceStateStore(tmp_path / 'state.sqlite')
    handoff = store.capture(source, result, session_id='s', source_ref='user:current')
    assert set(handoff) == {'state_id', 'capability', 'constraints', 'catalog_sha256',
                             'requirements_sha256', 'status'}
    assert handoff['requirements_sha256'] == source['requirements_sha256']
    assert store.show(handoff['state_id'], session_id='s') == handoff
    resolved = SourceStateStore(store.db_path).resolve(handoff['state_id'], session_id='s',
                                                       catalog_sha256=catalog.snapshot_id)
    assert resolved['source'] == source['source']
    assert resolved['requirements'] == source['requirements']
    assert resolved['request'] == result['request']
    outcome = HarnessRuntime(db_path=tmp_path / 'runs.sqlite').run(resolved['request'])
    assert outcome['status'] == 'succeeded' and outcome['verification']['passed']
    assert (tmp_path / 'out.json').read_text() == '{"名字":[1,2]}\n'


def test_source_state_rejects_input_catalog_session_and_persisted_source_tampering(tmp_path):
    from rpe_harness.requirements import SourceStateStore
    source, result, catalog = prepared(tmp_path)
    store = SourceStateStore(tmp_path / 'state.sqlite')
    handoff = store.capture(source, result, session_id='s', source_ref='user:current')
    for session, snapshot in [('other', catalog.snapshot_id), ('s', '0' * 64)]:
        with pytest.raises(ValueError):
            store.resolve(handoff['state_id'], session_id=session, catalog_sha256=snapshot)
    with pytest.raises(ValueError):
        store.capture(source, result, session_id='s', source_ref='other')
    (tmp_path / 'input.json').write_text('{"changed": true}')
    with pytest.raises(ValueError, match='input|source'):
        store.resolve(handoff['state_id'], session_id='s', catalog_sha256=catalog.snapshot_id)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE rpe_source_states SET document=replace(CAST(document AS TEXT),'minify','pretty')")
    with pytest.raises(ValueError, match='integrity'):
        store.show(handoff['state_id'], session_id='s')


def test_same_request_different_raw_source_has_distinct_binding_and_concurrent_sessions(tmp_path):
    from rpe_harness.requirements import SourceStateStore
    store = SourceStateStore(tmp_path / 'state.sqlite')
    def capture(session, raw=None):
        source, result, _ = prepared(tmp_path, session=session, raw=raw)
        return store.capture(source, result, session_id=session, source_ref='user:current')
    first, other, again = capture('a'), capture('b'), capture('a')
    assert first == again and first['state_id'] != other['state_id']
    changed_raw = capture('a', raw_source() + '\n')
    assert changed_raw['state_id'] != first['state_id']
    original = store.resolve(first['state_id'], session_id='a',
                              catalog_sha256=first['catalog_sha256'])
    assert original['source']['raw'] == raw_source()
    with ThreadPoolExecutor(4) as pool:
        concurrent = list(pool.map(capture, ['c', 'd', 'e', 'f']))
    assert len({row['state_id'] for row in concurrent}) == 4


def test_source_bytes_and_depth_are_bounded():
    from rpe_harness.requirements import source_document
    for raw in (' ' * 65537, '{"execution":' + '[' * 20 + '0' + ']' * 20 + '}'):
        result = source_document(raw, session_id='s', source_ref='r')
        assert result['status'] == 'rejected'
        assert 'source_facts' not in result


def test_ready_source_cannot_add_a_supported_option_absent_from_original(tmp_path):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.requirements import source_document, interpret_source_intent
    (tmp_path / 'input.csv').write_text('id,value\n1,a\n1,b\n')
    source = source_document(raw_source(capability='tabular.deduplicate', execution={
        'input_path': 'input.csv', 'output_path': 'out.csv', 'key_columns': ['id']}),
        session_id='s', source_ref='r')
    extracted = copy.deepcopy(source['source_facts'])
    extracted['execution']['encoding'] = 'utf-8'
    result = interpret_source_intent(extracted, source_document=source,
        catalog=CapabilityCatalog(), workspace=tmp_path, session_id='s', source_ref='r')
    assert result['status'] == 'rejected' and 'request' not in result


def test_source_and_model_have_only_documented_path_and_tab_normalizations(tmp_path):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.requirements import SourceStateStore, source_document, interpret_source_intent
    (tmp_path / 'input.csv').write_text('id\tvalue\n1\ta\n1\tb\n')
    source = source_document(raw_source(capability='tabular.deduplicate', execution={
        'input_path': 'input.csv', 'output_path': 'out.csv', 'key_columns': ['id'],
        'delimiter': 'TAB'}), session_id='s', source_ref='r')
    extracted = copy.deepcopy(source['source_facts'])
    extracted['execution'].update(input_path=str(tmp_path / 'input.csv'), delimiter='\t')
    result = interpret_source_intent(extracted, source_document=source,
        catalog=CapabilityCatalog(), workspace=tmp_path, session_id='s', source_ref='r')
    assert result['status'] == 'ready'
    store = SourceStateStore(tmp_path / 'state.sqlite')
    handoff = store.capture(source, result, session_id='s', source_ref='r')
    resolved = store.resolve(handoff['state_id'], session_id='s',
                             catalog_sha256=handoff['catalog_sha256'])
    assert resolved['source']['raw'] == source['source']['raw']
    assert resolved['request']['execution']['delimiter'] == '\t'


def test_nonready_current_source_cannot_be_upgraded_by_a_complete_model_proposal(tmp_path):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.requirements import source_document, interpret_source_intent
    source = source_document('压缩JSON并删除所有退款记录', session_id='s', source_ref='r')
    proposal = {'goal': 'Execute json.normalize', 'capability': 'json.normalize',
                'constraints': [], 'execution': {'input_path': 'input.json',
                                                  'output_path': 'out.json', 'style': 'minify'}}
    result = interpret_source_intent(proposal, source_document=source,
        catalog=CapabilityCatalog(), workspace=tmp_path, session_id='s', source_ref='r')
    assert result['status'] == 'needs_context'
    assert result['missing'] == ['structured_current_request']
    assert 'request' not in result and not (tmp_path / 'out.json').exists()


def test_source_xlsx_binding_is_inserted_by_catalog_and_current_revocation_blocks_resolve(tmp_path):
    from openpyxl import Workbook
    from test_capability_catalog import trusted_catalog
    from rpe_harness.requirements import SourceStateStore, source_document, interpret_source_intent
    catalog, skills = trusted_catalog(tmp_path)
    book = Workbook()
    book.active.append(['id', 'value'])
    book.active.append([1, 'first'])
    book.save(tmp_path / 'book.xlsx')
    book.close()
    source = source_document(raw_source(capability='xlsx.inspect', constraints=['preserve_original'],
        execution={'input_path': 'book.xlsx', 'output_path': 'out.json', 'mode': 'rows',
                   'sheet': 'Sheet'}), session_id='s', source_ref='r')
    extracted = copy.deepcopy(source['source_facts'])
    result = interpret_source_intent(extracted, source_document=source, catalog=catalog,
        workspace=tmp_path, session_id='s', source_ref='r')
    assert result['status'] == 'ready'
    assert 'skill_binding' not in extracted['execution']
    store = SourceStateStore(tmp_path / 'state.sqlite')
    handoff = store.capture(source, result, session_id='s', source_ref='r')
    assert store.resolve(handoff['state_id'], session_id='s',
                          catalog_sha256=handoff['catalog_sha256'])['request']['execution']['sheet'] == 'Sheet'
    with sqlite3.connect(skills.db_path) as db:
        db.execute('DELETE FROM rpe_skill_trust')
    with pytest.raises(ValueError, match='trust|catalog'):
        store.resolve(handoff['state_id'], session_id='s', catalog_sha256=handoff['catalog_sha256'])


@pytest.mark.parametrize('bad_source', [[], {}, {'source': []}, {'source': {}}])
def test_malformed_content_addressed_source_record_raises_value_error(tmp_path, bad_source):
    from rpe_harness.requirements import SourceStateStore
    source, result, catalog = prepared(tmp_path)
    store = SourceStateStore(tmp_path / 'state.sqlite')
    handoff = store.capture(source, result, session_id='s', source_ref='user:current')
    with sqlite3.connect(store.db_path) as db:
        binding = json.loads(db.execute('SELECT document FROM rpe_source_states WHERE state_id=?',
                                        (handoff['state_id'],)).fetchone()[0])
        binding['source_document'] = bad_source
        raw = json.dumps(binding, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        corrupted_id = hashlib.sha256(raw).hexdigest()
        db.execute('INSERT INTO rpe_source_states VALUES (?,?)', (corrupted_id, raw))
    with pytest.raises(ValueError):
        store.show(corrupted_id, session_id='s')
    with pytest.raises(ValueError):
        store.resolve(corrupted_id, session_id='s', catalog_sha256=catalog.snapshot_id)
