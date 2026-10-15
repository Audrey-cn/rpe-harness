from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3

import pytest


RAW = '请把 input.json 压缩为 out.json，保留所有值。'


def draft(**changes):
    value = {'capability': 'json.normalize', 'constraints': [],
             'execution': {'input_path': 'input.json', 'output_path': 'out.json', 'style': 'minify'},
             'requirements': [
                 {'text': '压缩 JSON 并保留值', 'quote': '压缩', 'fields': ['/capability', '/execution/style']},
                 {'text': '读取 input.json', 'quote': 'input.json', 'fields': ['/execution/input_path']},
                 {'text': '保存 out.json', 'quote': 'out.json', 'fields': ['/execution/output_path']},
                 {'text': '没有额外约束', 'quote': None, 'fields': ['/constraints']}],
             'unresolved': []}
    value.update(changes)
    return value


def proposed(tmp_path, candidate=None, raw=RAW):
    from rpe_harness.proposal import create_proposal
    source = tmp_path / 'input.json'
    if not source.exists():
        source.write_text('{ "名字" : [1, 2] }')
    return create_proposal(raw, candidate if candidate is not None else draft(),
        db_path=tmp_path / 'state.sqlite', workspace=tmp_path, session_id='s', source_ref='current:user')


def confirmed(tmp_path):
    from rpe_harness.proposal import ProposalStore
    proposal = proposed(tmp_path)
    store = ProposalStore(tmp_path / 'state.sqlite')
    confirmation = store.confirm(proposal['proposal_id'], session_id='s',
        expected_proposal_sha256=proposal['proposal_sha256'], actor='human:audrey')
    return store, proposal, confirmation


def test_proposal_preserves_source_and_review_without_authorizing_or_executing(tmp_path):
    from rpe_harness.proposal import ProposalStore
    result = proposed(tmp_path)
    assert result['status'] == 'awaiting_confirmation'
    assert result['source']['raw'] == RAW and result['draft'] == draft()
    assert result['source']['source_sha256'] == hashlib.sha256(RAW.encode()).hexdigest()
    row = result['requirements'][0]
    assert RAW[slice(*row['span'])] == row['quote'] == '压缩'
    assert result['requirements'][-1]['origin'] == 'proposed_default'
    assert '语义完整' in result['summary'] and RAW in result['summary']
    assert 'proposed_default' in result['summary'] and 'minify' in result['summary']
    assert ProposalStore(tmp_path / 'state.sqlite').show(result['proposal_id'], session_id='s') == result
    assert not (tmp_path / 'out.json').exists()
    assert 'confirmation_id' not in result


@pytest.mark.parametrize('alter,status', [
    (lambda value: value.update(confirmed=True), 'needs_context'),
    (lambda value: value.pop('unresolved'), 'needs_context'),
    (lambda value: value['execution'].pop('style'), 'needs_context'),
    (lambda value: value['requirements'].pop(1), 'needs_context'),
    (lambda value: value['requirements'][0].update(quote='并删除退款'), 'needs_context'),
    (lambda value: value['requirements'][0].update(fields=['/execution/hidden']), 'needs_context'),
    (lambda value: value.update(unresolved=['另有删除退款要求，未实现']), 'needs_context'),
    (lambda value: value.update(capability='json.delete_refunds'), 'unsupported'),
    (lambda value: value['execution'].update(style='delete'), 'unsupported'),
])
def test_incomplete_unsupported_or_model_authorized_drafts_cannot_be_confirmed(tmp_path, alter, status):
    from rpe_harness.proposal import ProposalStore
    candidate = draft()
    alter(candidate)
    proposal = proposed(tmp_path, candidate)
    assert proposal['status'] == status
    with pytest.raises(ValueError):
        ProposalStore(tmp_path / 'state.sqlite').confirm(proposal['proposal_id'], session_id='s',
            expected_proposal_sha256=proposal['proposal_sha256'], actor='human')
    assert not (tmp_path / 'out.json').exists()


def test_confirmation_is_bound_to_exact_digest_actor_session_and_immutable_source(tmp_path):
    from rpe_harness.proposal import ProposalStore
    proposal = proposed(tmp_path)
    store = ProposalStore(tmp_path / 'state.sqlite')
    for session, digest, actor in [('other', proposal['proposal_sha256'], 'human'),
            ('s', '0' * 64, 'human'), ('s', proposal['proposal_sha256'], '')]:
        with pytest.raises(ValueError):
            store.confirm(proposal['proposal_id'], session_id=session,
                expected_proposal_sha256=digest, actor=actor)
    confirmation = store.confirm(proposal['proposal_id'], session_id='s',
        expected_proposal_sha256=proposal['proposal_sha256'], actor='human')
    assert confirmation['status'] == 'confirmed'
    assert store.confirm(proposal['proposal_id'], session_id='s',
        expected_proposal_sha256=proposal['proposal_sha256'], actor='human') == confirmation
    with pytest.raises(ValueError):
        store.confirm(proposal['proposal_id'], session_id='s',
            expected_proposal_sha256=proposal['proposal_sha256'], actor='model')
    changed = proposed(tmp_path, raw=RAW + ' ')
    assert changed['proposal_id'] != proposal['proposal_id']
    assert 'confirmation_id' not in changed


def test_confirmation_cannot_be_used_as_a_proposal_and_blocks_stale_input_catalog(tmp_path):
    store, proposal, confirmation = confirmed(tmp_path)
    with pytest.raises(ValueError):
        store.resolve(proposal['proposal_id'], session_id='s', catalog_sha256=proposal['catalog_sha256'])
    for session, catalog in [('other', proposal['catalog_sha256']), ('s', '0' * 64)]:
        with pytest.raises(ValueError):
            store.resolve(confirmation['confirmation_id'], session_id=session, catalog_sha256=catalog)
    resolved = store.resolve(confirmation['confirmation_id'], session_id='s',
        catalog_sha256=proposal['catalog_sha256'])
    assert resolved['request']['goal'] == 'Execute json.normalize'
    assert resolved['draft'] == draft() and resolved['source']['raw'] == RAW
    (tmp_path / 'input.json').write_text('[99]')
    with pytest.raises(ValueError):
        store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path)
    assert not (tmp_path / 'out.json').exists()


def test_digest_checks_detect_persisted_source_draft_summary_and_confirmation_tampering(tmp_path):
    store, proposal, confirmation = confirmed(tmp_path)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE rpe_proposals SET document=replace(CAST(document AS TEXT),'minify','pretty')")
    with pytest.raises(ValueError, match='integrity'):
        store.show(proposal['proposal_id'], session_id='s')
    with pytest.raises(ValueError, match='integrity'):
        store.resolve(confirmation['confirmation_id'], session_id='s', catalog_sha256=proposal['catalog_sha256'])


def test_review_escapes_model_and_source_terminal_control_sequences(tmp_path):
    candidate = draft()
    candidate['requirements'][0]['text'] += '\x1b[2J\rFORGED\u202e'
    proposal = proposed(tmp_path, candidate, RAW + '\x1b[2J')
    assert '\x1b' not in proposal['summary'] and '\r' not in proposal['summary']
    assert '\u202e' not in proposal['summary']
    assert '\\u001b' in proposal['summary'] and '\\u202e' in proposal['summary']


def test_confirm_revalidates_input_before_recording_authority(tmp_path):
    from rpe_harness.proposal import ProposalStore
    proposal = proposed(tmp_path)
    (tmp_path / 'input.json').write_text('[3]')
    with pytest.raises(ValueError):
        ProposalStore(tmp_path / 'state.sqlite').confirm(proposal['proposal_id'], session_id='s',
            expected_proposal_sha256=proposal['proposal_sha256'], actor='human')


def test_execute_uses_one_bounded_worker_event_for_concurrent_and_repeated_calls(tmp_path):
    store, proposal, confirmation = confirmed(tmp_path)
    def run(_):
        return store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path, timeout=15)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, range(2)))
    completed = [result for result in results if result['status'] == 'succeeded']
    assert completed and all(result['status'] in ('succeeded', 'running') for result in results)
    replay = run(None)
    assert replay['status'] == 'succeeded' and replay['run_id'] == completed[0]['run_id']
    assert replay['confirmation_id'] == confirmation['confirmation_id']
    assert (tmp_path / 'out.json').read_text() == '{"名字":[1,2]}\n'
    with sqlite3.connect(store.db_path) as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 1


@pytest.mark.parametrize('kind', ['existing', 'symlink', 'parent_symlink', 'wrong_workspace'])
def test_changed_output_or_workspace_fails_before_execution(tmp_path, kind):
    store, _, confirmation = confirmed(tmp_path)
    output = tmp_path / 'out.json'
    workspace = tmp_path
    if kind == 'existing':
        output.write_text('keep')
    elif kind == 'symlink':
        output.symlink_to(tmp_path / 'input.json')
    elif kind == 'parent_symlink':
        child = tmp_path / 'moved'
        child.mkdir()
        workspace = child
    else:
        workspace = tmp_path.parent
    with pytest.raises(ValueError):
        store.execute(confirmation['confirmation_id'], session_id='s', workspace=workspace)


def test_bounds_and_invalid_rows_are_visible_without_becoming_confirmable(tmp_path):
    for candidate in (draft(requirements=draft()['requirements'] * 9),
            draft(requirements=[{'text': 'x', 'quote': '', 'fields': ['/capability']}]),
            draft(unresolved=['x' * 65536])):
        result = proposed(tmp_path, candidate)
        assert result['status'] == 'needs_context' and result['missing']
    result = proposed(tmp_path, raw='x' * 65537)
    assert result['status'] == 'needs_context'


def test_implicit_executor_defaults_are_shown_for_review(tmp_path):
    from rpe_harness.proposal import create_proposal
    (tmp_path / 'input.csv').write_text('id,value\n1,a\n1,b\n')
    candidate = {'capability': 'tabular.deduplicate', 'constraints': [],
        'execution': {'input_path': 'input.csv', 'output_path': 'out.csv', 'key_columns': ['id']},
        'requirements': [{'text': '按 id 去重', 'quote': '去重', 'fields': ['/capability', '/constraints',
            '/execution/input_path', '/execution/output_path', '/execution/key_columns']}], 'unresolved': []}
    proposal = create_proposal('请去重', candidate, db_path=tmp_path / 'state.sqlite', workspace=tmp_path,
        session_id='s', source_ref='r')
    assert proposal['status'] == 'awaiting_confirmation'
    assert proposal['machine_contract']['execution']['keep'] == 'first'
    assert 'utf-8' in proposal['summary'] and '执行默认值' in proposal['summary']


@pytest.mark.parametrize('failure', ['interpreter', 'binding'])
def test_worker_admission_failure_after_claim_is_terminal_and_cannot_republish(tmp_path, monkeypatch, failure):
    from rpe_harness.path_binding import BoundExecution
    store, _, confirmation = confirmed(tmp_path)
    kwargs = {}
    if failure == 'interpreter':
        kwargs['python'] = tmp_path / 'missing-python'
    else:
        def unavailable(*args, **kwargs):
            raise ValueError('bound directory changed after validation')
        monkeypatch.setattr(BoundExecution, 'capture', unavailable)
    result = store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path, **kwargs)
    assert result['status'] == 'failed'
    assert result['worker_cleanup']['cleanup_complete']
    assert store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path) == result
    assert not (tmp_path / 'out.json').exists()


@pytest.mark.parametrize('value', [None, 3, True, {}, 'preserve_original'])
def test_invalid_constraint_types_are_visible_nonconfirmable_reviews(tmp_path, value):
    proposal = proposed(tmp_path, draft(constraints=value))
    assert proposal['status'] == 'needs_context'


@pytest.mark.parametrize('when', ['before_confirm', 'before_execute'])
def test_replaced_output_directory_invalidates_original_proposal_authority(tmp_path, when):
    from rpe_harness.proposal import ProposalStore
    output_dir = tmp_path / 'out'
    output_dir.mkdir()
    candidate = draft()
    candidate['execution']['output_path'] = 'out/out.json'
    proposal = proposed(tmp_path, candidate)
    store = ProposalStore(tmp_path / 'state.sqlite')
    confirmation = None
    if when == 'before_execute':
        confirmation = store.confirm(proposal['proposal_id'], session_id='s',
            expected_proposal_sha256=proposal['proposal_sha256'], actor='human')
    output_dir.rename(tmp_path / 'original-out')
    output_dir.mkdir()
    with pytest.raises(ValueError, match='path|directory'):
        if confirmation is None:
            store.confirm(proposal['proposal_id'], session_id='s',
                expected_proposal_sha256=proposal['proposal_sha256'], actor='human')
        else:
            store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path)
    assert not (output_dir / 'out.json').exists()
    assert not (tmp_path / 'original-out' / 'out.json').exists()


def test_directory_swap_after_claim_is_rejected_before_worker_publication(tmp_path, monkeypatch):
    from rpe_harness.path_binding import BoundExecution
    from rpe_harness.proposal import ProposalStore
    (tmp_path / 'out').mkdir()
    candidate = draft()
    candidate['execution']['output_path'] = 'out/out.json'
    proposal = proposed(tmp_path, candidate)
    store = ProposalStore(tmp_path / 'state.sqlite')
    confirmation = store.confirm(proposal['proposal_id'], session_id='s',
        expected_proposal_sha256=proposal['proposal_sha256'], actor='simulated_fixture_reviewer')
    original = BoundExecution.capture
    def raced(*args, **kwargs):
        (tmp_path / 'out').rename(tmp_path / 'old-out')
        (tmp_path / 'out').mkdir()
        return original(*args, **kwargs)
    monkeypatch.setattr(BoundExecution, 'capture', raced)
    result = store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path)
    assert result['status'] == 'failed'
    assert not (tmp_path / 'out/out.json').exists()
    assert not (tmp_path / 'old-out/out.json').exists()


def test_shared_worker_lock_wait_is_bounded_and_consumed_as_terminal_failure(tmp_path):
    import fcntl
    store, _, confirmation = confirmed(tmp_path)
    with (tmp_path / 'state.sqlite.review-worker.lock').open('wb') as locked:
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path, timeout=1)
    assert result['status'] == 'failed' and result['error']['code'] == 'harness_timeout'
    assert result['worker_cleanup']['cleanup_complete']
    assert store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path) == result
    assert not (tmp_path / 'out.json').exists()
