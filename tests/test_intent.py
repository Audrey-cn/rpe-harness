import copy
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest


def prepare(tmp_path, *, session='s1', source='source-1', catalog=None):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    path = tmp_path / 'input.csv'
    if not path.exists():
        path.write_text('id,value\n1,a\n1,b\n2,c\n')
    extracted = {'goal': '按 id 去重，保留顺序', 'capability': 'tabular.deduplicate',
                 'constraints': ['preserve_original', 'preserve_order'],
                 'execution': {'input_path': str(path), 'output_path': str(tmp_path / 'out.csv'),
                               'key_columns': ['id']}}
    return interpret_intent(extracted, catalog=catalog or CapabilityCatalog(), workspace=tmp_path,
                            session_id=session, source_ref=source), extracted


def test_ready_uses_exact_v2_contract_and_real_probe(tmp_path):
    from rpe_harness.tasks import plan_from_request
    result, extracted = prepare(tmp_path)
    assert result['status'] == 'ready'
    assert result['request']['schema_version'] == 2
    assert result['request']['execution'] == extracted['execution']
    assert plan_from_request(result['request']).input_sha256 == result['input_sha256']
    assert not (tmp_path / 'out.csv').exists()
    extracted['execution']['key_columns'] = ['wrong']
    assert result['request']['execution']['key_columns'] == ['id']


@pytest.mark.parametrize('mutate,status,missing', [
    (lambda x: x.update(extra='ignored?'), 'rejected', None),
    (lambda x: x.update(capability='pdf.summarize'), 'unsupported', None),
    (lambda x: x.update(constraints=['sort_rows']), 'unsupported', None),
    (lambda x: x.pop('constraints'), 'needs_context', 'constraints'),
    (lambda x: x['execution'].pop('key_columns'), 'needs_context', 'execution.key_columns'),
    (lambda x: x['execution'].update(key_columns=['absent']), 'rejected', None),
    (lambda x: x['execution'].update(sort=True), 'rejected', None),
])
def test_bad_extraction_does_not_silently_repair(tmp_path, mutate, status, missing):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    _, extracted = prepare(tmp_path)
    mutate(extracted)
    result = interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
                              session_id='s1', source_ref='source-1')
    assert result['status'] == status
    if missing:
        assert missing in result['missing']
    assert 'request' not in result


def test_source_conflict_and_source_fact_omission_are_not_guessed(tmp_path):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    _, extracted = prepare(tmp_path)
    result = interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s1', source_ref='source-1', source_facts={'execution': {'key_columns': ['value']}})
    assert result['status'] == 'conflict'
    extracted['execution'].pop('key_columns')
    result = interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s1', source_ref='source-1', source_facts={'execution': {'key_columns': ['id']}})
    assert result['status'] == 'needs_context'


def test_workspace_symlink_and_invalid_source_are_rejected(tmp_path):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    _, extracted = prepare(tmp_path)
    (tmp_path / 'link').symlink_to(tmp_path, target_is_directory=True)
    extracted['execution']['input_path'] = str(tmp_path / 'link/input.csv')
    assert interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s', source_ref='r')['status'] == 'rejected'
    extracted['execution']['input_path'] = str(tmp_path.parent / 'outside.csv')
    assert interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s', source_ref='r')['status'] == 'rejected'
    extracted['execution']['input_path'] = str(tmp_path / 'input.csv')
    (tmp_path / 'input.csv').write_text('not,a,csv\nwrong,shape\n')
    assert interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s', source_ref='r')['status'] == 'rejected'


def test_minimal_handoff_persists_and_refuses_session_catalog_or_source_changes(tmp_path):
    from rpe_harness.intent import MinimalStateStore
    result, _ = prepare(tmp_path)
    store = MinimalStateStore(tmp_path / 'state.sqlite')
    handoff = store.capture(result, session_id='s1', source_ref='source-1')
    assert set(handoff) == {'state_id', 'capability', 'constraints', 'catalog_sha256', 'status'}
    assert 'input_path' not in json.dumps(handoff)
    assert MinimalStateStore(tmp_path / 'state.sqlite').resolve(handoff['state_id'], session_id='s1',
        catalog_sha256=result['catalog_sha256'])['request'] == result['request']
    for session, catalog in [('other', result['catalog_sha256']), ('s1', '0' * 64)]:
        with pytest.raises(ValueError):
            store.resolve(handoff['state_id'], session_id=session, catalog_sha256=catalog)
    (tmp_path / 'input.csv').write_text('id,value\n3,changed\n')
    with pytest.raises(ValueError, match='source|input'):
        store.resolve(handoff['state_id'], session_id='s1', catalog_sha256=result['catalog_sha256'])


def test_tampered_result_and_persisted_request_are_refused(tmp_path):
    from rpe_harness.intent import MinimalStateStore
    result, _ = prepare(tmp_path)
    store = MinimalStateStore(tmp_path / 'state.sqlite')
    tampered = copy.deepcopy(result)
    tampered['request']['execution']['key_columns'] = ['value']
    with pytest.raises(ValueError, match='integrity'):
        store.capture(tampered, session_id='s1', source_ref='source-1')
    handoff = store.capture(result, session_id='s1', source_ref='source-1')
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE rpe_minimal_states SET document=replace(CAST(document AS TEXT),'按 id','按 value')")
    with pytest.raises(ValueError, match='integrity'):
        store.resolve(handoff['state_id'], session_id='s1', catalog_sha256=result['catalog_sha256'])


def test_repeated_a_b_a_and_concurrent_sessions_do_not_share_state(tmp_path):
    from rpe_harness.intent import MinimalStateStore
    store = MinimalStateStore(tmp_path / 'state.sqlite')
    def capture(session):
        result, _ = prepare(tmp_path, session=session)
        return store.capture(result, session_id=session, source_ref='source-1')
    a, b, again = capture('a'), capture('b'), capture('a')
    assert a == again and a['state_id'] != b['state_id']
    with ThreadPoolExecutor(4) as pool:
        rows = list(pool.map(capture, ['c', 'd', 'e', 'f']))
    assert len({r['state_id'] for r in rows}) == 4


def test_input_bytes_and_depth_are_bounded(tmp_path):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    _, extracted = prepare(tmp_path)
    extracted['goal'] = '中' * 22000
    assert interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s1', source_ref='source-1')['status'] == 'rejected'
    value = {}
    for _ in range(30):
        value = {'nested': value}
    extracted['goal'] = 'valid'
    extracted['execution'] = value
    assert interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s1', source_ref='source-1')['status'] == 'rejected'


@pytest.mark.parametrize('delimiter,status', [('TAB', 'ready'), ('\\t', 'ready'),
                                             ('\t', 'ready'), ('xx', 'rejected')])
def test_tab_alias_is_explicit_and_other_multibyte_delimiters_are_rejected(tmp_path, delimiter, status):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    _, extracted = prepare(tmp_path)
    (tmp_path / 'input.csv').write_text('id\tvalue\n1\ta\n1\tb\n')
    extracted['execution']['delimiter'] = delimiter
    result = interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
                              session_id='s', source_ref='r')
    assert result['status'] == status
    if status == 'ready':
        assert result['request']['execution']['delimiter'] == '\t'
        assert bool(result.get('normalizations')) == (delimiter != '\t')


def test_source_constraints_cannot_be_omitted_or_malformed(tmp_path):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    _, extracted = prepare(tmp_path)
    extracted['constraints'] = []
    assert interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s', source_ref='r', source_facts={'constraints': ['preserve_order']})['status'] == 'conflict'
    assert interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s', source_ref='r', source_facts={'constraints': 42})['status'] == 'rejected'


def test_xlsx_binding_is_parent_owned_and_current_revocation_invalidates_state(tmp_path):
    from test_capability_catalog import trusted_catalog
    from openpyxl import Workbook
    from rpe_harness.intent import MinimalStateStore, interpret_intent
    catalog, skills = trusted_catalog(tmp_path)
    workbook = Workbook()
    workbook.active.append(['id', 'value'])
    workbook.active.append([1, 'x'])
    workbook.save(tmp_path / 'book.xlsx')
    workbook.close()
    extracted = {'goal': 'List sheets', 'capability': 'xlsx.inspect',
        'constraints': ['preserve_original'], 'execution': {'input_path': str(tmp_path / 'book.xlsx'),
        'output_path': str(tmp_path / 'out.json'), 'mode': 'sheets'}}
    result = interpret_intent(extracted, catalog=catalog, workspace=tmp_path, session_id='s', source_ref='r')
    assert result['status'] == 'ready'
    assert 'skill_binding' in result['request']['execution'] and 'skill_binding' not in extracted['execution']
    store = MinimalStateStore(tmp_path / 'state.sqlite')
    handoff = store.capture(result, session_id='s', source_ref='r')
    extracted['execution']['skill_binding'] = result['request']['execution']['skill_binding']
    assert interpret_intent(extracted, catalog=catalog, workspace=tmp_path,
        session_id='s', source_ref='r')['status'] == 'rejected'
    with sqlite3.connect(skills.db_path) as db:
        db.execute('DELETE FROM rpe_skill_trust')
    with pytest.raises(ValueError, match='trust|catalog'):
        store.resolve(handoff['state_id'], session_id='s', catalog_sha256=result['catalog_sha256'])


def test_ready_request_uses_real_execution_and_independent_json_oracle(tmp_path):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import MinimalStateStore, interpret_intent
    from rpe_harness.runtime import HarnessRuntime
    source = tmp_path / 'input.json'
    source.write_text('{"a": [1, 2], "b": true}')
    result = interpret_intent({'goal': 'minify', 'capability': 'json.normalize', 'constraints': [],
        'execution': {'input_path': str(source), 'output_path': str(tmp_path / 'out.json'), 'style': 'minify'}},
        catalog=CapabilityCatalog(), workspace=tmp_path, session_id='s', source_ref='r')
    store = MinimalStateStore(tmp_path / 'state.sqlite')
    handoff = store.capture(result, session_id='s', source_ref='r')
    anchored = store.resolve(handoff['state_id'], session_id='s', catalog_sha256=result['catalog_sha256'])
    runtime = HarnessRuntime(db_path=tmp_path / 'runs.sqlite')
    outcome = runtime.run(anchored['request'])
    assert outcome['status'] == 'succeeded' and outcome['verification']['passed']
    assert (tmp_path / 'out.json').read_text() == '{"a":[1,2],"b":true}\n'
    assert store.resolve(handoff['state_id'], session_id='s', catalog_sha256=result['catalog_sha256'])['request'] == anchored['request']
    assert runtime.run(anchored['request'])['run_id'] == outcome['run_id']


def test_large_ready_state_is_rejected_before_runtime_not_truncated(tmp_path):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    _, extracted = prepare(tmp_path)
    extracted['goal'] = '中' * 6000
    result = interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
                              session_id='s', source_ref='r')
    assert result['status'] == 'rejected' and 'request' not in result
    extracted['goal'] = 'valid'
    extracted['constraints'] = ['preserve_order'] * 1500
    result = interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
                              session_id='s', source_ref='r')
    assert result['status'] == 'rejected' and 'request' not in result


def test_source_sheet_omission_cannot_fall_back_to_other_active_sheet(tmp_path):
    from openpyxl import Workbook
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    workbook = Workbook()
    workbook.active.title = 'Wrong'
    workbook.active.append(['id', 'value'])
    workbook.active.append([1, 'wrong'])
    workbook.create_sheet('Requested').append(['id', 'value'])
    workbook['Requested'].append([2, 'requested'])
    workbook.save(tmp_path / 'book.xlsx')
    workbook.close()
    extracted = {'goal': 'Deduplicate requested sheet', 'capability': 'tabular.deduplicate',
        'constraints': ['preserve_original'], 'execution': {'input_path': str(tmp_path / 'book.xlsx'),
        'output_path': str(tmp_path / 'out.xlsx'), 'key_columns': ['id']}}
    arguments = {'catalog': CapabilityCatalog(), 'workspace': tmp_path, 'session_id': 's',
                 'source_ref': 'r', 'source_facts': {'execution': {'sheet': 'Requested'}}}
    result = interpret_intent(extracted, **arguments)
    assert result['status'] == 'needs_context'
    assert 'execution.sheet' in result['missing'] and 'request' not in result
    assert 'sheet' not in extracted['execution']
    extracted['execution']['sheet'] = 'Wrong'
    assert interpret_intent(extracted, **arguments)['status'] == 'conflict'
    extracted['execution']['sheet'] = 'Requested'
    assert interpret_intent(extracted, **arguments)['status'] == 'ready'


@pytest.mark.parametrize('field,value', [('keep', 'first'), ('encoding', 'utf-8-sig'), ('delimiter', ',')])
def test_even_matching_contract_defaults_require_explicit_source_option(tmp_path, field, value):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    _, extracted = prepare(tmp_path)
    result = interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s', source_ref='r', source_facts={'execution': {field: value}})
    assert result['status'] == 'needs_context'
    assert 'execution.' + field in result['missing'] and 'request' not in result


@pytest.mark.parametrize('field', ['goal', 'capability', 'constraints', 'execution'])
def test_top_level_source_fields_are_not_filled_when_extraction_omits_them(tmp_path, field):
    from rpe_harness.capability_catalog import CapabilityCatalog
    from rpe_harness.intent import interpret_intent
    _, extracted = prepare(tmp_path)
    fact = extracted.pop(field)
    result = interpret_intent(extracted, catalog=CapabilityCatalog(), workspace=tmp_path,
        session_id='s', source_ref='r', source_facts={field: fact})
    assert result['status'] == 'needs_context'
    assert field in result['missing'] and 'request' not in result
