"""The host facade preserves proposal authority and the existing run ledger."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import sqlite3
import subprocess
import sys
import threading

import pytest

from rpe_harness.host_api import HostAPI, TrustedHostAction
from rpe_harness.proposal import ProposalStore
from rpe_harness.storage import RunStore


RAW = 'Normalize input.json to out.json, minify and preserve all values.'


def proposal_draft():
    return {
        'capability': 'json.normalize', 'constraints': [],
        'execution': {'input_path': 'input.json', 'output_path': 'out.json', 'style': 'minify'},
        'requirements': [
            {'text': 'Normalize JSON', 'quote': 'Normalize', 'fields': ['/capability']},
            {'text': 'Read input.json', 'quote': 'input.json', 'fields': ['/execution/input_path']},
            {'text': 'Write out.json', 'quote': 'out.json', 'fields': ['/execution/output_path']},
            {'text': 'Minify', 'quote': 'minify', 'fields': ['/execution/style']},
            {'text': 'No extra constraints', 'quote': None, 'fields': ['/constraints']},
        ],
        'unresolved': [],
    }


@pytest.fixture
def facade(tmp_path):
    (tmp_path / 'input.json').write_text('{ "name" : [1, 2] }', encoding='utf-8')
    return HostAPI(tmp_path / 'state.sqlite')


def start(facade, tmp_path, *, owner='alice', session='s', candidate=None):
    return facade.start_task(
        {'raw': RAW, 'draft': proposal_draft() if candidate is None else candidate},
        {'owner': owner, 'session_id': session, 'workspace': str(tmp_path),
         'source_ref': 'host:user', 'host': 'test'},
    )


def action(operation, *, owner='alice', **changes):
    values = {'owner': owner, 'operation_id': operation['operation_id'],
              'generation': operation['generation'], 'digest': operation['digest'],
              'actor': 'human:test'}
    values.update(changes)
    return TrustedHostAction(**values)


def confirm(facade, operation):
    return facade.confirm_task(operation['operation_id'], operation['digest'], action(operation))


def claim_confirmed(facade, tmp_path, operation, *, status='running'):
    receipt = confirm(facade, operation)
    assert receipt['status'] == 'confirmed'
    proposals = ProposalStore(tmp_path / 'state.sqlite')
    proposal = proposals.show(operation['proposal_id'], session_id='s')
    resolved = proposals.resolve(receipt['confirmation_id'], session_id='s',
                                 catalog_sha256=proposal['catalog_sha256'])
    request = copy.deepcopy(resolved['request'])
    request['event_id'] = receipt['event_id']
    store = RunStore(tmp_path / 'state.sqlite')
    run, fresh = store.claim_event('local', request['event_id'], request)
    assert fresh
    if status == 'running':
        store.mark_running(run['run_id'])
    elif status != 'received':
        store.finish(run['run_id'], status)
    return receipt, store, store.get(run['run_id'])


def test_start_and_show_preserve_draft_source_and_same_database_without_authority(facade, tmp_path):
    request = {'raw': RAW, 'draft': proposal_draft()}
    context = {'owner': 'alice', 'session_id': 's', 'workspace': str(tmp_path),
               'source_ref': 'host:user', 'host': 'test'}
    operation = facade.start_task(request, context)
    assert operation['operation_id'] and operation['generation'] == 1
    assert operation['status'] == 'awaiting_confirmation'
    shown = facade.show_task(operation['operation_id'], 'alice')
    assert shown['draft'] == proposal_draft()
    assert shown['source']['raw'] == RAW
    assert shown['digest'] == shown['proposal_id'] == operation['digest']
    assert shown['host_context'] == context
    assert shown['run_id'] is None and shown['confirmation_id'] is None
    assert shown['sources'] is not None
    request['draft']['execution']['style'] = 'pretty'
    context['owner'] = 'mallory'
    assert facade.show_task(operation['operation_id'], 'alice')['draft'] == proposal_draft()
    with sqlite3.connect(tmp_path / 'state.sqlite') as db:
        assert db.execute('SELECT count(*) FROM rpe_proposals').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM rpe_confirmations').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM rpe_proposal_executions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
    assert not (tmp_path / 'out.json').exists()


def test_start_incomplete_draft_stays_reviewable_and_cannot_confirm(facade, tmp_path):
    candidate = proposal_draft()
    candidate['unresolved'] = ['Which values should be preserved?']
    operation = start(facade, tmp_path, candidate=candidate)
    assert operation['status'] == 'needs_context'
    shown = facade.show_task(operation['operation_id'], 'alice')
    assert shown['draft']['unresolved'] == candidate['unresolved']
    result = confirm(facade, operation)
    assert result['status'] != 'confirmed'
    assert not (tmp_path / 'out.json').exists()


def test_confirmation_is_explicit_digest_bound_and_persists_across_restart(facade, tmp_path):
    operation = start(facade, tmp_path)
    receipt = confirm(facade, operation)
    assert receipt['status'] == 'confirmed'
    assert receipt['generation'] == operation['generation'] + 1
    assert receipt['confirmation_id']
    assert receipt['event_id'] == 'review-' + receipt['confirmation_id']
    reopened = HostAPI(tmp_path / 'state.sqlite')
    shown = reopened.show_task(operation['operation_id'], 'alice')
    assert shown['generation'] == receipt['generation']
    assert shown['confirmation_id'] == receipt['confirmation_id']
    assert shown['status'] == 'confirmed'
    assert not (tmp_path / 'out.json').exists()


@pytest.mark.parametrize('method', ['show', 'reconcile', 'cancel', 'confirm'])
def test_owner_isolation_rejects_without_disclosing_draft_or_mutating(facade, tmp_path, method):
    operation = start(facade, tmp_path)
    identity = operation['operation_id']
    if method == 'show':
        result = facade.show_task(identity, 'mallory')
    elif method == 'reconcile':
        result = facade.reconcile_task(identity, 'mallory')
    elif method == 'cancel':
        result = facade.cancel_task(identity, operation['generation'], owner='mallory')
    else:
        result = facade.confirm_task(identity, operation['digest'], action(operation, owner='mallory'))
    assert result['status'] == 'forbidden'
    assert 'draft' not in result and RAW not in json.dumps(result)
    current = facade.show_task(identity, 'alice')
    assert current['generation'] == operation['generation']
    assert current['status'] == 'awaiting_confirmation'


@pytest.mark.parametrize('forgery', [None, True, 'confirmed', {}, {'trusted': True},
                                    {'owner': 'alice', 'action': 'confirm', 'actor': 'human:test'}])
def test_model_serializable_actions_cannot_confirm(facade, tmp_path, forgery):
    operation = start(facade, tmp_path)
    result = facade.confirm_task(operation['operation_id'], operation['digest'], forgery)
    assert result['status'] == 'forbidden'
    assert facade.show_task(operation['operation_id'], 'alice')['confirmation_id'] is None


@pytest.mark.parametrize('changed', [{'operation_id': 'another-operation'},
                                   {'owner': 'another-owner'}, {'action': 'start'}])
def test_trusted_action_must_bind_current_operation_owner_and_action(facade, tmp_path, changed):
    operation = start(facade, tmp_path)
    result = facade.confirm_task(operation['operation_id'], operation['digest'],
                                action(operation, **changed))
    assert result['status'] == 'forbidden'
    assert facade.show_task(operation['operation_id'], 'alice')['confirmation_id'] is None


@pytest.mark.parametrize('kind', ['argument', 'action', 'generation'])
def test_stale_digest_and_generation_cannot_confirm(facade, tmp_path, kind):
    operation = start(facade, tmp_path)
    digest = '0' * 64 if kind == 'argument' else operation['digest']
    changes = {'digest': '0' * 64} if kind == 'action' else (
        {'generation': operation['generation'] + 1} if kind == 'generation' else {})
    result = facade.confirm_task(operation['operation_id'], digest, action(operation, **changes))
    assert result['status'] == 'stale'
    assert facade.show_task(operation['operation_id'], 'alice')['confirmation_id'] is None


def test_source_mutation_before_confirmation_never_grants_authority(facade, tmp_path):
    operation = start(facade, tmp_path)
    (tmp_path / 'input.json').write_text('[99]')
    result = confirm(facade, operation)
    assert result['status'] == 'stale'
    assert facade.show_task(operation['operation_id'], 'alice')['confirmation_id'] is None


def test_cancel_generation_mismatch_is_nonmutating(facade, tmp_path):
    operation = start(facade, tmp_path)
    result = facade.cancel_task(operation['operation_id'], operation['generation'] + 1,
                                owner='alice', reason='changed scope')
    assert result['status'] == 'stale'
    shown = facade.show_task(operation['operation_id'], 'alice')
    assert shown['generation'] == operation['generation']
    assert shown['status'] == 'awaiting_confirmation'


def test_cancel_unconfirmed_draft_fences_later_confirmation(facade, tmp_path):
    operation = start(facade, tmp_path)
    cancelled = facade.cancel_task(operation['operation_id'], operation['generation'],
                                   owner='alice', reason='changed scope')
    assert cancelled['status'] == 'cancelled'
    assert cancelled['generation'] == operation['generation'] + 1
    current = facade.show_task(operation['operation_id'], 'alice')
    assert current['status'] == 'cancelled'
    assert facade.confirm_task(operation['operation_id'], operation['digest'], action(current))['status'] != 'confirmed'
    assert not (tmp_path / 'out.json').exists()


def test_cancel_confirmed_before_worker_admission_fences_existing_execution_ledger(facade, tmp_path):
    operation = start(facade, tmp_path)
    receipt = confirm(facade, operation)
    cancelled = facade.cancel_task(operation['operation_id'], receipt['generation'], owner='alice')
    assert cancelled['status'] == 'cancelled'
    replay = ProposalStore(tmp_path / 'state.sqlite').execute(receipt['confirmation_id'],
                                                            session_id='s', workspace=tmp_path)
    assert replay['status'] == 'cancelled'
    assert not (tmp_path / 'out.json').exists()
    with sqlite3.connect(tmp_path / 'state.sqlite') as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM rpe_proposal_executions').fetchone()[0] == 1


@pytest.mark.parametrize('status', ['received', 'running', 'interrupted'])
def test_cancel_delegates_existing_run_and_reconcile_reports_actual_cancelled_state(facade, tmp_path, status):
    operation = start(facade, tmp_path)
    receipt, store, run = claim_confirmed(facade, tmp_path, operation, status=status)
    try:
        cancelled = facade.cancel_task(operation['operation_id'], receipt['generation'],
                                       owner='alice', reason='user stopped task')
        assert cancelled['status'] == 'cancelled'
        saved = store.get(run['run_id'])
        assert saved['status'] == 'cancelled'
        assert saved['steps'][-1]['evidence']['reason'] == 'user stopped task'
        reconciled = facade.reconcile_task(operation['operation_id'], 'alice')
        assert reconciled['status'] == 'cancelled'
        assert reconciled['run_id'] == saved['run_id']
        assert reconciled['allowed_recovery_actions'] == []
        assert 'cleanup_state' in reconciled
        assert not (tmp_path / 'out.json').exists()
    finally:
        store._release_lease(run['run_id'])


def test_publish_wins_cancel_serializes_and_returns_too_late(facade, tmp_path):
    operation = start(facade, tmp_path)
    receipt, store, run = claim_confirmed(facade, tmp_path, operation)
    published, release = threading.Event(), threading.Event()
    output = tmp_path / 'out.json'
    parent = tmp_path.stat()
    store.step(run['run_id'], 'publish', {'output_path': str(output),
        'output_parent_identity': [parent.st_dev, parent.st_ino]})

    def publisher():
        output.write_text('[1,2]\n')
        published.set()
        assert release.wait(10)

    try:
        with ThreadPoolExecutor(2) as pool:
            publication = pool.submit(store.complete_publication, run['run_id'], publisher,
                verification={'passed': True}, artifact={'published': True, 'path': str(output)})
            assert published.wait(10)
            cancellation = pool.submit(facade.cancel_task, operation['operation_id'],
                                        receipt['generation'], owner='alice', reason='late cancel')
            release.set()
            saved = publication.result(timeout=10)
            result = cancellation.result(timeout=10)
        assert result['status'] == 'too_late'
        assert store.get(run['run_id']) == saved
        reconciled = facade.reconcile_task(operation['operation_id'], 'alice')
        assert reconciled['status'] == 'succeeded'
        assert reconciled['artifact']['path'] == str(output)
        assert reconciled['allowed_recovery_actions'] == []
        assert output.read_text() == '[1,2]\n'
    finally:
        release.set()
        store._release_lease(run['run_id'])


def test_uncertain_publication_is_not_misreported_as_success_or_cancelled(facade, tmp_path):
    operation = start(facade, tmp_path)
    receipt, store, run = claim_confirmed(facade, tmp_path, operation)
    output = tmp_path / 'out.json'
    output.write_text('unverified bytes')
    parent = tmp_path.stat()
    store.step(run['run_id'], 'publish', {'output_path': str(output),
        'output_parent_identity': [parent.st_dev, parent.st_ino]})
    store.finish(run['run_id'], 'interrupted', error={'code': 'owner_process_exited'})
    result = facade.cancel_task(operation['operation_id'], receipt['generation'], owner='alice')
    assert result['status'] == 'too_late'
    reconciled = facade.reconcile_task(operation['operation_id'], 'alice')
    assert reconciled['status'] == 'interrupted'
    assert reconciled.get('artifact') is None
    assert 'retry' not in reconciled['allowed_recovery_actions']
    assert output.read_text() == 'unverified bytes'


def test_reconcile_does_not_infer_success_from_preexisting_output_or_replay_confirmation(facade, tmp_path, monkeypatch):
    operation = start(facade, tmp_path)
    receipt = confirm(facade, operation)
    output = tmp_path / 'out.json'
    output.write_text('private unrelated bytes')

    def forbidden_execution(*args, **kwargs):
        raise AssertionError('reconcile must not execute or replay confirmation')

    monkeypatch.setattr(ProposalStore, 'execute', forbidden_execution)
    result = facade.reconcile_task(operation['operation_id'], 'alice')
    assert result['status'] == 'confirmed'
    assert result['confirmation_id'] == receipt['confirmation_id']
    assert result['run_id'] is None and result.get('artifact') is None
    assert output.read_text() == 'private unrelated bytes'
    with sqlite3.connect(tmp_path / 'state.sqlite') as db:
        assert db.execute('SELECT count(*) FROM rpe_proposal_executions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 0


def test_real_proposal_worker_execution_is_discovered_without_duplicate_ledgers(facade, tmp_path):
    operation = start(facade, tmp_path)
    receipt = confirm(facade, operation)
    result = ProposalStore(tmp_path / 'state.sqlite').execute(receipt['confirmation_id'],
                                                            session_id='s', workspace=tmp_path, timeout=15)
    assert result['status'] == 'succeeded'
    reconciled = HostAPI(tmp_path / 'state.sqlite').reconcile_task(operation['operation_id'], 'alice')
    assert reconciled['status'] == 'succeeded'
    assert reconciled['run_id'] == result['run_id']
    assert reconciled['artifact'] == result['artifact']
    assert reconciled['allowed_recovery_actions'] == []
    assert (tmp_path / 'out.json').read_text() == '{"name":[1,2]}\n'
    with sqlite3.connect(tmp_path / 'state.sqlite') as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM rpe_confirmations').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM rpe_proposal_executions').fetchone()[0] == 1


def test_host_api_import_has_no_hermes_or_agent_loop_dependency():
    program = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if 'hermes' in name.lower() or name in {'run_agent', 'agent'}:
        raise AssertionError('host_api imported an agent runtime: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import rpe_harness.host_api
'''
    child = subprocess.run([sys.executable, '-B', '-c', program], capture_output=True,
                           text=True, timeout=10)
    assert child.returncode == 0, child.stderr
