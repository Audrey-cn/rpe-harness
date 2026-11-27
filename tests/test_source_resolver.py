"""C2: exact authorized source resolution, without inference or broad disclosure."""
import hashlib
import json
import sqlite3

import pytest

from rpe_harness.context import ContextStore, SourceRef
from rpe_harness.source_resolver import MAX_FETCHES, MAX_RESULT_BYTES, SourceResolver


@pytest.fixture
def store(tmp_path):
    return ContextStore(tmp_path / 'context.sqlite')


def capture(store, *, event='event-1', session='session-a', facts=None, payload=None):
    return store.capture(source='selected-document', event_id=event, session_id=session,
                         payload=payload if payload is not None else {'text': 'private body'},
                         facts=facts if facts is not None else {'key_columns': ['customer_id']})


def resolver(store, refs, *, job='job-a', session='session-a'):
    return SourceResolver(store, authorizations=[
        {'job_id': job, 'session_id': session, 'source_ref': ref} for ref in refs])


def resolve(api, refs, field='key_columns', *, job='job-a', session='session-a'):
    return api.resolve(field, refs, job, session)


def assert_empty(result, status, fetches):
    assert result['status'] == status
    assert 'value' not in result
    assert result['fetch_count'] == fetches
    assert result['fetch_limit'] == MAX_FETCHES == 2
    assert 'private body' not in json.dumps(result)


def test_found_has_only_exact_fact_and_reference_provenance(store):
    ref = capture(store, facts={'key_columns': ['customer_id'], 'email': 'private@example.com',
                                'execution': {'api_key': 'secret-key'}, 'goal': 'private goal'})
    result = resolve(resolver(store, [ref]), [ref.to_dict()])
    assert result == {'status': 'found', 'field': 'key_columns', 'value': ['customer_id'],
                      'provenance': [ref.to_dict()], 'fetch_count': 1, 'fetch_limit': 2,
                      'reason': 'authorized_fact'}
    assert not any(private in json.dumps(result)
                   for private in ('private@example.com', 'secret-key', 'private goal'))


def test_matching_sources_preserve_both_origins_and_consume_two_fetches(store):
    first = capture(store)
    second = capture(store, event='event-2')
    result = resolve(resolver(store, [first, second]), [first, second])
    assert result['status'] == 'found'
    assert result['value'] == ['customer_id']
    assert result['provenance'] == [first.to_dict(), second.to_dict()]
    assert result['fetch_count'] == 2


@pytest.mark.parametrize('field,value', [
    ('goal', 'Deduplicate selected rows'), ('capability', 'json.normalize'),
    ('constraints', []), ('constraints', ['preserve_original']),
    ('execution.key_columns', ['customer_id']), ('style', 'pretty'),
    ('execution.input_path', '/workspace/input.csv'), ('delimiter', '\t'),
])
def test_registered_leaf_fields_and_empty_explicit_constraints(store, field, value):
    name = field.removeprefix('execution.')
    ref = capture(store, facts={name: value})
    result = resolve(resolver(store, [ref]), [ref], field)
    assert result['status'] == 'found'
    assert result['value'] == value


def test_nested_intent_execution_is_projected_to_only_requested_leaf(store):
    ref = capture(store, facts={'execution': {'key_columns': ['customer_id'],
                                             'private': {'token': 'never-disclose'}}})
    result = resolve(resolver(store, [ref]), [ref], 'execution.key_columns')
    assert result['status'] == 'found'
    assert result['value'] == ['customer_id']
    assert 'never-disclose' not in json.dumps(result)


def test_missing_constraint_is_needs_user_even_when_text_suggests_it(store):
    ref = capture(store, facts={'goal': 'preserve_original'},
                  payload={'text': 'Use preserve_original and assume all missing constraints'})
    result = resolve(resolver(store, [ref]), [ref], 'constraints')
    assert_empty(result, 'needs_user', 1)
    assert result['reason'] == 'missing_field'
    assert result['provenance'] == []


def test_no_refs_never_scans_private_session_history(store, monkeypatch):
    ref = capture(store)
    api = resolver(store, [ref])
    monkeypatch.setattr(store, 'resolve', lambda *a, **k: pytest.fail('unexpected source read'))
    assert_empty(resolve(api, []), 'needs_user', 0)


def test_conflicting_authorized_sources_never_choose_a_value(store):
    first = capture(store)
    second = capture(store, event='event-2', facts={'key_columns': ['email']})
    result = resolve(resolver(store, [first, second]), [first, second])
    assert_empty(result, 'conflict', 2)
    assert result['provenance'] == [first.to_dict(), second.to_dict()]
    assert 'email' not in json.dumps(result)


def test_conflicting_flat_and_nested_explicit_values_are_not_silently_selected(store):
    ref = capture(store, facts={'key_columns': ['id'], 'execution': {'key_columns': ['email']}})
    result = resolve(resolver(store, [ref]), [ref])
    assert_empty(result, 'conflict', 1)


@pytest.mark.parametrize('column,value', [
    ('payload_json', '{"text":"mutated original bytes"}'),
    ('facts_json', '{"key_columns":["private-other-key"]}'),
    ('payload_hash', '0' * 64), ('received_at', 1),
])
def test_mutated_snapshot_bytes_hash_and_timestamp_are_stale(store, column, value):
    ref = capture(store)
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE context_sources SET ' + column + '=?', (value,))
    result = resolve(resolver(store, [ref]), [ref])
    assert_empty(result, 'stale', 1)
    assert result['reason'] == 'hash_mismatch'


def test_original_text_byte_changes_are_stale_even_if_facts_unchanged(store):
    ref = capture(store, payload={'text': '{ "key_columns": ["customer_id"] }'})
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE context_sources SET payload_json=?',
                   (json.dumps({'text': '{"key_columns":["customer_id"]}'}),))
    assert_empty(resolve(resolver(store, [ref]), [ref]), 'stale', 1)


def test_changed_reference_sha_is_stale_without_fetch(store, monkeypatch):
    ref = capture(store)
    changed = ref.to_dict()
    changed['sha256'] = '0' * 64
    api = resolver(store, [ref])
    monkeypatch.setattr(store, 'resolve', lambda *a, **k: pytest.fail('unexpected source read'))
    assert_empty(resolve(api, [changed]), 'stale', 0)


@pytest.mark.parametrize('job,session', [
    ('job-b', 'session-a'), ('job-a', 'session-b'), ('job-b', 'session-b'),
])
def test_cross_job_or_session_is_forbidden_before_fetch(store, monkeypatch, job, session):
    ref = capture(store)
    api = resolver(store, [ref])
    monkeypatch.setattr(store, 'resolve', lambda *a, **k: pytest.fail('unexpected source read'))
    result = resolve(api, [ref], job=job, session=session)
    assert_empty(result, 'forbidden', 0)


def test_unauthorized_source_forbids_whole_set_before_reading_first(store, monkeypatch):
    allowed = capture(store)
    private = capture(store, event='unrelated-private')
    api = resolver(store, [allowed])
    monkeypatch.setattr(store, 'resolve', lambda *a, **k: pytest.fail('unexpected source read'))
    result = resolve(api, [allowed, private])
    assert_empty(result, 'forbidden', 0)
    assert result['provenance'] == []


def test_forged_authorization_fields_in_request_do_not_grant_access(store):
    ref = capture(store).to_dict()
    ref.update(job_id='job-a', session_id='session-a', authorized=True)
    result = resolve(SourceResolver(store), [ref])
    assert_empty(result, 'forbidden', 0)


def test_context_store_session_binding_overrules_incorrect_host_grant(store):
    ref = capture(store, session='session-b')
    result = resolve(resolver(store, [ref]), [ref])
    assert_empty(result, 'forbidden', 1)


def test_immutable_authorizations_and_owned_results(store):
    ref = capture(store)
    grant = {'job_id': 'job-a', 'session_id': 'session-a', 'source_ref': ref.to_dict()}
    api = SourceResolver(store, authorizations=[grant])
    grant['job_id'] = 'job-b'
    grant['source_ref']['sha256'] = '0' * 64
    result = resolve(api, [ref])
    assert result['status'] == 'found'
    result['value'].append('private-other')
    result['provenance'][0]['sha256'] = '0' * 64
    assert resolve(api, [ref])['value'] == ['customer_id']
    assert_empty(resolve(api, [ref], job='job-b'), 'forbidden', 0)


def test_two_targeted_reads_never_read_unrelated_rows_or_save_recovery(store, monkeypatch):
    first = capture(store)
    second = capture(store, event='event-2')
    capture(store, event='private-event', facts={'key_columns': ['private-key']})
    original, queries = store._connect, []

    def traced_connect():
        context = original()

        class Traced:
            def __enter__(self):
                db = context.__enter__()
                db.set_trace_callback(queries.append)
                return db

            def __exit__(self, *args):
                return context.__exit__(*args)

        return Traced()

    monkeypatch.setattr(store, '_connect', traced_connect)
    result = resolve(resolver(store, [first, second]), [first, second])
    assert result['status'] == 'found'
    reads = [query for query in queries if query.startswith('SELECT')]
    assert len(reads) == result['fetch_count'] == 2
    assert all('WHERE source=' in query and 'AND event_id=' in query for query in reads)
    assert not any('private-event' in query for query in reads)
    assert not any(query.startswith(('INSERT', 'UPDATE')) for query in queries)


def test_more_than_two_refs_requires_narrowing_without_partial_read(store, monkeypatch):
    refs = [capture(store, event='event-' + str(index)) for index in range(3)]
    api = resolver(store, refs)
    monkeypatch.setattr(store, 'resolve', lambda *a, **k: pytest.fail('unexpected source read'))
    result = resolve(api, refs)
    assert_empty(result, 'needs_user', 0)
    assert result['reason'] == 'fetch_budget_exceeded'


def test_duplicate_refs_are_one_targeted_fetch(store):
    ref = capture(store)
    result = resolve(resolver(store, [ref]), [ref, ref.to_dict(), ref])
    assert result['status'] == 'found'
    assert result['fetch_count'] == 1
    assert result['provenance'] == [ref.to_dict()]


def test_missing_selected_source_does_not_accept_other_source_partial_success(store):
    first = capture(store)
    missing = SourceRef(first.source, 'missing', '0' * 64)
    result = resolve(resolver(store, [first, missing]), [first, missing])
    assert_empty(result, 'needs_user', 2)
    assert result['reason'] == 'source_missing'


def test_corrupt_selected_source_does_not_accept_other_source_partial_success(store):
    first = capture(store)
    corrupt = capture(store, event='event-2')
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE context_sources SET facts_json=? WHERE event_id=?',
                   ('{"key_columns":["secret"]}', corrupt.event_id))
    result = resolve(resolver(store, [first, corrupt]), [first, corrupt])
    assert_empty(result, 'stale', 2)
    assert result['provenance'] == []


@pytest.mark.parametrize('field', ['private_email', 'execution', 'execution.api_key',
                                 'payload', 'facts', {}, None])
def test_private_or_object_fields_cannot_be_requested(store, field):
    ref = capture(store, facts={'private_email': 'never@example.com'})
    result = resolve(resolver(store, [ref]), [ref], field)
    assert_empty(result, 'forbidden', 0)
    assert 'never@example.com' not in json.dumps(result)


@pytest.mark.parametrize('value', [None, {}, {'private': 'secret'}, [], ['id', 'id'],
                                 ['id', {'secret': 'private'}], 'customer_id', True, 1])
def test_invalid_or_object_fact_under_allowed_field_cannot_leak(store, value):
    ref = capture(store, facts={'key_columns': value})
    result = resolve(resolver(store, [ref]), [ref])
    assert_empty(result, 'needs_user', 1)
    assert result['reason'] == 'invalid_fact'
    assert 'secret' not in json.dumps(result)


def test_result_size_includes_large_provenance_and_only_one_requested_field(store):
    refs = [store.capture(source='s' * 1024, event_id=str(n) + 'e' * 1023,
                           session_id='session-a', payload={}, facts={'goal': 'x' * 16000})
            for n in range(2)]
    result = resolve(resolver(store, refs), refs, 'goal')
    assert result['status'] == 'found'
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_RESULT_BYTES


def test_future_snapshot_is_stale_even_with_valid_hash(store):
    ref = capture(store)
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM context_sources').fetchone()
        content = {'source': row['source'], 'event_id': row['event_id'],
                   'session_id': row['session_id'], 'payload': json.loads(row['payload_json']),
                   'facts': json.loads(row['facts_json']), 'received_at': 9999999999.0}
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        sha = hashlib.sha256(raw.encode()).hexdigest()
        db.execute('UPDATE context_sources SET received_at=?,payload_hash=?',
                   (content['received_at'], sha))
    ref = SourceRef(ref.source, ref.event_id, sha)
    result = resolve(resolver(store, [ref]), [ref])
    assert_empty(result, 'stale', 1)
    assert result['reason'] == 'source_future'


def test_reopen_context_store_uses_same_hash_source_semantics(store):
    ref = capture(store)
    api = SourceResolver(store.path, authorizations=[
        {'job_id': 'job-a', 'session_id': 'session-a', 'source_ref': ref.to_dict()}])
    assert resolve(api, [ref])['status'] == 'found'


@pytest.mark.parametrize('grants', [{}, [None], [{'job_id': 'job-a'}],
                                    [{'job_id': '', 'session_id': 'a', 'source_ref': {}}]])
def test_malformed_host_grants_are_rejected_without_creating_database(tmp_path, grants):
    path = tmp_path / 'absent.sqlite'
    with pytest.raises(ValueError):
        SourceResolver(path, authorizations=grants)
    assert not path.exists()


def test_conflicting_hashes_in_host_grants_are_rejected(store):
    ref = capture(store)
    changed = SourceRef(ref.source, ref.event_id, '0' * 64)
    with pytest.raises(ValueError, match='conflicting hashes'):
        resolver(store, [ref, changed])
