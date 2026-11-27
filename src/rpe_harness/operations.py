"""Host-owned task identities over the existing proposal and execution ledgers.

Only the linkage and a pre-execution cancellation fence live here. Proposals,
confirmations, execution consumption, runs and recovery retain their original
SQLite authorities. A host authenticates its user before constructing a
TrustedHostAction; a JSON request can never construct that authority.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import sqlite3
import stat
import uuid

from .file_lock import NOFOLLOW, lock_exclusive, unlock
from .intent import _bounded, _identity
from .proposal import ProposalStore, create_proposal
from .skill_catalog import _absolute
from .storage import RunStore, canonical_json

_TERMINAL = {'succeeded', 'rejected', 'failed', 'interrupted', 'cancelled'}
_REQUEST_FIELDS = {'raw', 'task', 'draft', 'review_context', 'skill_selection',
                   'catalog_root', 'skill_snapshot_id', 'value_evidence'}


@dataclass(frozen=True)
class TrustedHostAction:
    """Out-of-band host assertion, scoped to one displayed operation generation.

    This is a Python application boundary, not authentication against arbitrary
    code running as the host's OS user. Do not deserialize model JSON into it.
    """
    owner: str
    operation_id: str
    generation: int
    digest: str
    actor: str = 'trusted-host'
    action: str = 'confirm'


class TaskOperations:
    """Five host-neutral methods. Construction and inspection launch no worker."""

    def __init__(self, db_path):
        self.db_path = _absolute(db_path)
        self.proposals = ProposalStore(self.db_path)
        self.runs = RunStore(self.db_path)
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS rpe_operations (
                operation_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK(generation > 0),
                host_context TEXT NOT NULL, proposal_id TEXT NOT NULL UNIQUE,
                confirmation_id TEXT, run_id TEXT,
                cancelled INTEGER NOT NULL DEFAULT 0 CHECK(cancelled IN (0,1)),
                cancel_reason TEXT)''')

    def _connect(self):
        _absolute(self.db_path)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _lock(self, operation_id):
        # Separate from SQLite: proposal revalidation uses its own connections.
        # A per-operation lock serializes generation transitions across hosts.
        _identity(operation_id, 'operation_id')
        directory = self.db_path.parent / ('.' + self.db_path.name + '.operations')
        directory.mkdir(exist_ok=True)
        name = hashlib.sha256(operation_id.encode()).hexdigest() + '.lock'
        descriptor = os.open(directory / name, os.O_RDWR | os.O_CREAT | NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError('operation lock must be a regular file')
            lock_exclusive(descriptor, blocking=True)
            yield
        finally:
            unlock(descriptor)
            os.close(descriptor)

    def _owned(self, operation_id, owner):
        _identity(operation_id, 'operation_id')
        _identity(owner, 'owner')
        with self._connect() as db:
            row = db.execute('SELECT * FROM rpe_operations WHERE operation_id=?',
                             (operation_id,)).fetchone()
        # Do not disclose whether another owner's identifier exists.
        if row is None or row['owner'] != owner:
            return None
        return dict(row)

    @staticmethod
    def _forbidden():
        return {'status': 'forbidden'}

    def _confirmation(self, row):
        # Repairable linkage: confirmation may commit just before a host crash.
        # Reading that receipt never creates or replays an approval.
        with self._connect() as db:
            receipt = db.execute('SELECT confirmation_id FROM rpe_confirmations '
                                 'WHERE proposal_id=?', (row['proposal_id'],)).fetchone()
        confirmation_id = receipt['confirmation_id'] if receipt else None
        if row['confirmation_id'] is not None and row['confirmation_id'] != confirmation_id:
            raise ValueError('operation confirmation linkage differs')
        if confirmation_id:
            receipt = self.proposals._get(confirmation_id, confirmation=True)
            context = json.loads(row['host_context'])
            if receipt['session_id'] != context['session_id']:
                raise ValueError('operation confirmation session differs')
        return confirmation_id

    def _execution(self, row, confirmation_id):
        if confirmation_id is None:
            return None, None, None
        event_id = 'review-' + confirmation_id
        with self._connect() as db:
            execution = db.execute('SELECT event_id,result FROM rpe_proposal_executions '
                                   'WHERE confirmation_id=?', (confirmation_id,)).fetchone()
            found = db.execute('SELECT r.run_id FROM runs r JOIN events e ON r.event_fk=e.id '
                               'WHERE e.event_id=? ORDER BY r.rowid DESC LIMIT 1', (event_id,)).fetchone()
        if execution and execution['event_id'] != event_id:
            raise ValueError('operation execution event differs')
        result = json.loads(execution['result']) if execution and execution['result'] is not None else None
        run_id = found['run_id'] if found else (result.get('run_id') if isinstance(result, dict) else None)
        if row['run_id'] is not None and row['run_id'] != run_id:
            raise ValueError('operation run linkage differs')
        return (self.runs.get(run_id) if run_id else None), result, execution

    def _view(self, row):
        context = json.loads(row['host_context'])
        proposal = self.proposals.show(row['proposal_id'], session_id=context['session_id'])
        confirmation_id = self._confirmation(row)
        run, execution_result, execution = self._execution(row, confirmation_id)
        if run:
            status = run['status']
        elif execution_result:
            # A parent failure or pre-admission cancellation can have no run.
            status = execution_result['status']
        elif execution:
            status = 'running'
        elif row['cancelled']:
            status = 'cancelled'
        elif confirmation_id:
            status = 'confirmed'
        else:
            status = proposal['status']
        if ((confirmation_id and row['confirmation_id'] is None) or
                (run and row['run_id'] is None)):
            with self._connect() as db:
                db.execute('UPDATE rpe_operations SET confirmation_id=?,run_id=? '
                           'WHERE operation_id=?', (confirmation_id, run['run_id'] if run else None,
                                                   row['operation_id']))
        return {'operation_id': row['operation_id'], 'owner': row['owner'],
            'generation': row['generation'], 'status': status,
            'host_context': context, 'digest': row['proposal_id'],
            'proposal_id': row['proposal_id'], 'proposal': proposal,
            'draft': copy.deepcopy(proposal['draft']), 'source': copy.deepcopy(proposal['source']),
            'sources': copy.deepcopy((proposal.get('review_context') or {}).get('source_refs', [])),
            'confirmation_id': confirmation_id,
            'event_id': 'review-' + confirmation_id if confirmation_id else None,
            'run_id': run['run_id'] if run else None, 'run': run,
            'artifact': copy.deepcopy(run['artifact']) if run else None,
            'execution_result': execution_result}

    def start_task(self, request, host_context):
        """Accept text or ``{raw|task, draft}``; save review, never authority.

        ``owner``, ``session_id`` and ``workspace`` come from the trusted host
        context. All other context metadata is bounded JSON. Optional selected
        sources use the existing review_context bundle and its hash semantics.
        """
        _bounded(host_context)
        if type(host_context) is not dict:
            raise ValueError('host_context must be an object')
        context = json.loads(canonical_json(host_context))
        for name in ('owner', 'session_id'):
            _identity(context.get(name), name)
        workspace = _absolute(context.get('workspace', ''))
        if not context.get('workspace') or not workspace.is_dir():
            raise ValueError('host workspace must be an existing directory')
        context['workspace'] = str(workspace)
        _identity(context.get('source_ref', 'host-request'), 'source_ref')
        if isinstance(request, str):
            request = {'raw': request}
        _bounded(request)
        if (type(request) is not dict or set(request) - _REQUEST_FIELDS or
                ('raw' in request and 'task' in request)):
            raise ValueError('request requires raw or task, an optional draft, and proposal options only')
        request = copy.deepcopy(request)
        raw = request.get('raw', request.get('task'))
        operation_id = uuid.uuid4().hex
        # Identical drafts from two operations must not share approval authority.
        source_ref = 'host:' + hashlib.sha256(canonical_json(
            [context.get('source_ref', 'host-request'), operation_id]).encode()).hexdigest()
        options = {key: request[key] for key in _REQUEST_FIELDS - {'raw', 'task', 'draft'}
                   if key in request}
        proposal = create_proposal(raw, request.get('draft', {}), db_path=self.db_path,
            workspace=workspace, session_id=context['session_id'], source_ref=source_ref, **options)
        if 'proposal_id' not in proposal:
            # Invalid raw is not accepted as a durable operation.
            return proposal
        with self._connect() as db:
            db.execute('INSERT INTO rpe_operations '
                       '(operation_id,owner,generation,host_context,proposal_id) VALUES(?,?,?,?,?)',
                       (operation_id, context['owner'], 1, canonical_json(context), proposal['proposal_id']))
        return self.show_task(operation_id, context['owner'])

    def show_task(self, operation_id, owner):
        row = self._owned(operation_id, owner)
        return self._view(row) if row else self._forbidden()

    def confirm_task(self, operation_id, digest, trusted_action):
        """Record approval only for an authenticated host's displayed generation."""
        if type(trusted_action) is not TrustedHostAction:
            return self._forbidden()
        action = trusted_action
        if (action.operation_id != operation_id or action.action != 'confirm' or
                type(action.generation) is not int):
            return self._forbidden()
        row = self._owned(operation_id, action.owner)
        if row is None:
            return self._forbidden()
        with self._lock(operation_id):
            row = self._owned(operation_id, action.owner)
            if (digest != row['proposal_id'] or action.digest != row['proposal_id'] or
                    action.generation != row['generation']):
                return {'status': 'stale', 'operation_id': operation_id, 'generation': row['generation']}
            view = self._view(row)
            if view['status'] == 'cancelled':
                return {'status': 'cancelled', 'operation_id': operation_id,
                        'generation': row['generation']}
            if view['confirmation_id']:
                # Never grant a fresh generation or replay an existing receipt.
                return {'status': 'stale', 'operation_id': operation_id, 'generation': row['generation']}
            try:
                receipt = self.proposals.confirm(row['proposal_id'],
                    session_id=view['host_context']['session_id'],
                    expected_proposal_sha256=digest, actor=action.actor)
            except (ValueError, OSError):
                # Validation can fail after review (e.g. input mutation). Keep
                # private implementation errors out of host response payloads.
                return {'status': 'stale', 'operation_id': operation_id, 'generation': row['generation']}
            with self._connect() as db:
                db.execute('UPDATE rpe_operations SET confirmation_id=?,generation=generation+1 '
                           'WHERE operation_id=?', (receipt['confirmation_id'], operation_id))
            return {**receipt, 'operation_id': operation_id, 'owner': row['owner'],
                    'generation': row['generation'] + 1, 'digest': digest,
                    'event_id': 'review-' + receipt['confirmation_id']}

    def cancel_task(self, operation_id, expected_generation, *, owner, reason=None):
        """Fence pending work or delegate publication arbitration to RunStore."""
        row = self._owned(operation_id, owner)
        if row is None:
            return self._forbidden()
        if type(expected_generation) is not int:
            raise ValueError('expected_generation must be an integer')
        if reason is not None and (not isinstance(reason, str) or not reason.strip()
                or len(reason.encode('utf-8')) > 4096):
            raise ValueError('reason must be nonempty UTF-8 text of at most 4096 bytes')
        with self._lock(operation_id):
            row = self._owned(operation_id, owner)
            if expected_generation != row['generation']:
                return {'status': 'stale', 'operation_id': operation_id, 'generation': row['generation']}
            confirmation_id = self._confirmation(row)
            run, result, execution = self._execution(row, confirmation_id)
            if run:
                cancelled = self.runs.cancel(run['run_id'], reason=reason)
                status = cancelled['status']
                if status == 'conflict':
                    status = (cancelled['run']['status'] if cancelled['error_code'] == 'already_terminal'
                              and cancelled['run']['status'] != 'succeeded' else 'too_late')
                if status == 'cancelled' and not row['cancelled']:
                    with self._connect() as db:
                        db.execute('UPDATE rpe_operations SET cancelled=1,cancel_reason=?,run_id=?, '
                                   'confirmation_id=?,generation=generation+1 WHERE operation_id=?',
                                   (reason, run['run_id'], confirmation_id, operation_id))
                view = self.show_task(operation_id, owner)
                return {**view, **cancelled, 'status': status,
                        'generation': view['generation']}
            if row['cancelled']:
                return self.show_task(operation_id, owner)
            # Serialize with ProposalStore.execute's admission transaction. A
            # consumed NULL claim may still launch a worker: report too_late.
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if confirmation_id:
                    execution = db.execute('SELECT result FROM rpe_proposal_executions '
                        'WHERE confirmation_id=?', (confirmation_id,)).fetchone()
                    if execution:
                        result = json.loads(execution['result']) if execution['result'] is not None else None
                        if result is None:
                            return {'status': 'too_late', 'operation_id': operation_id,
                                    'generation': row['generation'], 'reason': 'execution_admitted'}
                        return {'status': result['status'], 'operation_id': operation_id,
                                'generation': row['generation'], 'execution_result': result}
                    tombstone = {'status': 'cancelled', 'operation_id': operation_id,
                        'confirmation_id': confirmation_id, 'proposal_id': row['proposal_id'],
                        'event_id': 'review-' + confirmation_id, 'reason': reason,
                        'worker_cleanup': {'cleanup_complete': True, 'remaining_children': [], 'errors': []}}
                    db.execute('INSERT INTO rpe_proposal_executions VALUES(?,?,?)',
                        (confirmation_id, 'review-' + confirmation_id, canonical_json(tombstone)))
                db.execute('UPDATE rpe_operations SET cancelled=1,cancel_reason=?, '
                           'confirmation_id=?,generation=generation+1 WHERE operation_id=?',
                           (reason, confirmation_id, operation_id))
            return self.show_task(operation_id, owner)

    def reconcile_task(self, operation_id, owner):
        """Inspect receipts, owner leases and recovery guards; never execute work."""
        row = self._owned(operation_id, owner)
        if row is None:
            return self._forbidden()
        view = self._view(row)
        run = view['run']
        # Existing recovery uses the lease as authority for a stopped owner.
        # Reconcile only this operation; unrelated active work is never swept.
        if run and run['status'] in {'received', 'running'}:
            from .recovery import _stopped_owner
            try:
                with _stopped_owner(self.runs, run):
                    self.runs.finish(run['run_id'], 'interrupted',
                        error={'code': 'owner_process_exited',
                               'message': 'owner stopped before a verified terminal receipt'})
            except ValueError:
                pass
            view = self.show_task(operation_id, owner)
            run = view['run']
        result = view['execution_result'] or {}
        cleanup = result.get('worker_cleanup')
        if cleanup is not None:
            cleanup_state = 'complete' if cleanup.get('cleanup_complete') is True else 'pending'
        elif run is None and view['status'] in {'awaiting_confirmation', 'needs_context', 'unsupported',
                                              'confirmed', 'cancelled'}:
            cleanup_state = 'not_started'
        else:
            cleanup_state = 'unknown'
        allowed = []
        if run and run['status'] in {'failed', 'interrupted'}:
            from .recovery import _guards, _saved_plan, _stopped_owner
            try:
                with _stopped_owner(self.runs, run):
                    _guards(run, _saved_plan(run))
                with self._connect() as db:
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                        "AND name='recovery_attempts'").fetchone()
                    reserved = db.execute('SELECT 1 FROM recovery_attempts WHERE retry_of=?',
                        (run['run_id'],)).fetchone() if exists else None
                if reserved is None:
                    allowed = ['recover_run']
            except (ValueError, OSError, KeyError, TypeError):
                pass
        if (run and (run.get('artifact') or any(step['stage'] in {'publish', 'complete'}
                                               for step in run['steps']))
                and run['status'] != 'succeeded'):
            allowed = ['inspect_publication']
        return {**view, 'terminal': view['status'] in _TERMINAL,
                'terminal_state': view['status'] if view['status'] in _TERMINAL else None,
                'cleanup_state': cleanup_state, 'cleanup': copy.deepcopy(cleanup),
                'allowed_recovery_actions': allowed}

    def handoff_task(self, source_operation_id, source_owner, target_host_context, *,
                     authorized_sources=None, path_mapping=None):
        """Hand off an operation to a second host with fresh session/owner and zero inherited confirmation."""
        row = self._owned(source_operation_id, source_owner)
        if row is None:
            return self._forbidden()
        _bounded(target_host_context)
        if type(target_host_context) is not dict:
            raise ValueError('target_host_context must be an object')
        target_ctx = json.loads(canonical_json(target_host_context))
        for name in ('owner', 'session_id'):
            _identity(target_ctx.get(name), name)
        target_workspace = _absolute(target_ctx.get('workspace', ''))
        if not target_ctx.get('workspace') or not target_workspace.is_dir():
            raise ValueError('target host workspace must be an existing directory')
        target_ctx['workspace'] = str(target_workspace)

        reauthorized = []
        if authorized_sources is not None:
            _bounded(authorized_sources)
            if not isinstance(authorized_sources, list):
                raise ValueError('authorized_sources must be a list')
            for item in authorized_sources:
                if not isinstance(item, dict):
                    return self._forbidden()
                if item.get('revoked') is True or item.get('authorized') is False:
                    return self._forbidden()
                src_session = item.get('session_id')
                if src_session is not None and src_session != target_ctx['session_id']:
                    return self._forbidden()
                src_owner = item.get('owner')
                if src_owner is not None and src_owner != target_ctx['owner']:
                    return self._forbidden()
                reauthorized.append(item.get('source_id') or item.get('ref') or 'authorized-source')

        mapping = {}
        if path_mapping is not None:
            _bounded(path_mapping)
            if not isinstance(path_mapping, dict):
                raise ValueError('path_mapping must be an object')
            for src_path, dst_path in path_mapping.items():
                if not isinstance(src_path, str) or not isinstance(dst_path, str) or not src_path or not dst_path:
                    raise ValueError('path_mapping entries must be non-empty strings')
                mapping[src_path] = dst_path

        def _remap(value):
            if not mapping:
                return value
            if isinstance(value, str):
                out = value
                for src_p, dst_p in mapping.items():
                    out = out.replace(src_p, dst_p)
                return out
            if isinstance(value, list):
                return [_remap(v) for v in value]
            if isinstance(value, dict):
                return {k: _remap(v) for k, v in value.items()}
            return value

        source_ctx = json.loads(row['host_context'])
        source_view = self._view(row)
        raw_text = _remap(source_view['source'].get('raw') or source_view['source'].get('text') or '')
        draft = _remap(copy.deepcopy(source_view['draft']))
        target_ctx['handoff_lineage'] = {
            'handoff_from_operation_id': source_operation_id,
            'handoff_from_owner': source_owner,
            'handoff_from_host': source_ctx.get('host_id', 'unknown'),
            'handoff_from_session_id': source_ctx.get('session_id'),
            'source_proposal_id': row['proposal_id'],
            'reauthorized_sources': reauthorized,
            'prior_confirmation_invalidated': True,
        }
        created = self.start_task({'raw': raw_text, 'draft': draft}, target_ctx)
        if 'operation_id' in created:
            created['handoff_lineage'] = copy.deepcopy(target_ctx['handoff_lineage'])
        return created

    def execute_task(self, operation_id, owner, *, confirmation_id, timeout=60):
        """Execute a confirmed host operation through the existing ProposalStore authority."""
        row = self._owned(operation_id, owner)
        if row is None:
            return self._forbidden()
        view = self._view(row)
        if view['status'] == 'cancelled':
            return {'status': 'cancelled', 'operation_id': operation_id, 'generation': row['generation']}
        if not view['confirmation_id'] or view['confirmation_id'] != confirmation_id:
            return self._forbidden()
        ctx = view['host_context']
        result = self.proposals.execute(
            confirmation_id,
            session_id=ctx['session_id'],
            workspace=ctx['workspace'],
            timeout=timeout,
        )
        updated = self.reconcile_task(operation_id, owner)
        return {**updated, 'execution_result': result}
