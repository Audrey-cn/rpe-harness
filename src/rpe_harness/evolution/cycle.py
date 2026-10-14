"""A small evidence-driven maintenance cycle, with an optional external proposer."""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..tasks import plan_from_dict, verify_plan
from ..storage import RunStore, canonical_json
from .store import EvolutionStore, digest


def record_verified_feedback(run_id, db_path):
    """Accept durable, plan-bound published results, never model summaries."""
    store = EvolutionStore(db_path)
    feedback = _inspect_feedback(run_id, RunStore(db_path))
    return store.add_feedback(feedback) if feedback is not None else None


def inspect_verified_feedback(run_id, db_path):
    """Freshly verify the same evidence without caching feedback or recovering owners."""
    runs = RunStore.read_only(db_path)
    feedback = _inspect_feedback(run_id, runs)
    if feedback is not None:
        with runs.connect() as db:
            table = db.execute("SELECT name FROM sqlite_master WHERE name='evolution_feedback'").fetchone()
            existing = db.execute('SELECT payload_hash FROM evolution_feedback WHERE run_id=?',
                                  (run_id,)).fetchone() if table else None
        if existing and existing['payload_hash'] != digest(feedback):
            raise ValueError('durable verified feedback changed')
    return feedback


def _inspect_feedback(run_id, runs):
    try:
        run = runs.get(run_id)
    except KeyError:
        return None
    if (run['status'] != 'succeeded' or run['verification_ok'] != 1
            or not run['verification'] or run['verification'].get('passed') is not True
            or not run['artifact'] or run['artifact'].get('published') is not True):
        return None
    anchors = [step['evidence'] for step in run['steps'] if step['stage'] == 'plan']
    if len(anchors) != 1:
        return None
    try:
        plan = plan_from_dict(anchors[0]['plan'])
        artifact, verification = run['artifact'], run['verification']
        if (anchors[0]['plan_sha256'] != plan.sha256
                or artifact.get('plan_sha256') != plan.sha256
                or verification.get('plan_sha256') != plan.sha256
                or artifact.get('path') != plan.output_path
                or artifact.get('input_sha256') != plan.input_sha256
                or verification.get('input_sha256') != plan.input_sha256):
            return None
        source, output = Path(plan.input_path), Path(plan.output_path)
        if (source.is_symlink() or output.is_symlink() or not source.is_file() or not output.is_file()
                or artifact.get('sha256') != verification.get('output_sha256')):
            return None
        if plan.capability != 'json.normalize' and (
                digest(source.read_bytes()) != plan.input_sha256
                or digest(output.read_bytes()) != artifact.get('sha256')):
            return None
        # Recheck the actual published bytes using the trusted oracle, independently
        # of both the stored producer counters and its natural-language report.
        prepared = {**plan.verification_parameters(), 'staging_path': plan.output_path}
        replay = verify_plan(prepared, plan)
        if replay.get('passed') is not True or replay['output_sha256'] != artifact['sha256']:
            return None
    except (ValueError, OSError, KeyError, TypeError):
        return None
    feedback = {'run_id': run_id, 'verified': True, 'plan': plan.to_dict(),
                'plan_sha256': plan.sha256, 'input_sha256': plan.input_sha256,
                'output_sha256': artifact['sha256'], 'verification_sha256': digest(verification),
                'artifact_sha256': digest(artifact), 'source': run['source'],
                'executor_id': plan.executor_id}
    return feedback


def _recipe(feedback):
    plan = feedback['plan']
    fields = (('capability', 'input_format', 'style', 'constraints')
              if plan['capability'] == 'json.normalize' else
              ('capability', 'input_format', 'key_columns', 'constraints', 'sheet',
               'keep', 'encoding', 'delimiter'))
    return {**{key: plan[key] for key in fields},
            'procedure': ['plan', 'prepare', 'independent_verify', 'exclusive_publish']}


def _diagnoses(store, runs, feedback, host_rows=None):
    """Use persisted stage/type facts rather than infer causes from keywords."""
    hashes = {item['run_id']: digest(item) for item in feedback}
    problems = []
    for run in runs:
        for step in run['steps']:
            if step['stage'] != 'candidate_fallback':
                continue
            evidence = step['evidence']
            verification = evidence.get('verification') or {}
            actual = evidence.get('actual_executor') or {}
            fact = {'run_id': run['run_id'], 'status': run['status'], 'stage': 'verify',
                    'kind': 'candidate_fallback', 'code': 'candidate_verification_failed',
                    'error_type': verification.get('error'),
                    'plan_sha256': evidence.get('plan_sha256'),
                    'candidate_version_id': actual.get('candidate_version_id'),
                    'verification_passed': verification.get('passed'),
                    'evidence_sha256': digest(evidence)}
            problems.append(fact)
            hashes[run['run_id']] = digest({'fallbacks': [s['evidence'] for s in run['steps']
                                           if s['stage'] == 'candidate_fallback']})
        if run['status'] not in {'failed', 'rejected', 'interrupted'}:
            continue
        error = run['error'] or {}
        anchored = next((step['evidence'] for step in run['steps'] if step['stage'] == 'plan'), {})
        fact = {'run_id': run['run_id'], 'status': run['status'], 'stage': run['stage'],
                'code': error.get('code'), 'error_type': error.get('type'),
                'retry_of': anchored.get('retry_of'), 'plan_sha256': anchored.get('plan_sha256')}
        hashes[run['run_id']] = digest({'fact': fact, 'error': error, 'steps': run['steps']})
        problems.append(fact)
    host = []
    if host_rows is None:
        host_rows = []
        if store is not None:
            with store.connect() as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='hermes_host_events'").fetchone():
                    host_rows = db.execute('SELECT ingest_event_id,kind,metadata_json FROM hermes_host_events ORDER BY id').fetchall()
    for row in host_rows:
        metadata = json.loads(row['metadata_json'])
        if (metadata.get('error_code') or metadata.get('error_type') or metadata.get('error')
                or row['kind'] in {'error', 'tool_error', 'gate_blocked'}):
            host_error = metadata.get('error') if isinstance(metadata.get('error'), dict) else {}
            item = {'host_event_id': row['ingest_event_id'], 'kind': row['kind'],
                    'error_type': metadata.get('error_type') or host_error.get('type'),
                    'code': metadata.get('error_code') or host_error.get('code'),
                    'metadata_sha256': digest(metadata)}
            host.append(item)
            hashes['host:' + row['ingest_event_id']] = digest(item)
    expected = {
        'contract': 'Add a regression case for rejected parameters; preserve exact registered authority.',
        'discovery': 'Keep metadata discovery separate from executable authority; retain dormant lookup.',
        'recipe': 'Consolidate verified plan parameters into a replayable declarative procedure.',
        'executor': 'Propose a registered capability producer variant; independently check its exact contract.',
        'host': 'Add a host admission/error regression from persisted event types and receipt hashes.',
        'core': 'Review failed stages and retry links; require a separately owned trusted suite for a core change.',
        'verifier': 'Add independent adversarial fixtures for verification failures; preserve existing oracle approval.',
    }
    related = {'contract': {'validate'}, 'discovery': {'route'}, 'executor': {'execute', 'verify'},
               'verifier': {'verify'}, 'recipe': {'execute', 'verify'},
               'core': {'validate', 'route', 'execute', 'verify', 'publish', 'complete'}}
    return [{'component': component, 'kind': 'diagnostic',
             'content': {'problem': 'persisted_failures_require_regression' if problems or host else
                         ('verified_history_requires_independent_upgrade_test' if feedback else
                          'no_plan_bound_verified_runs_yet'),
                         'problems': [item for item in problems if item['stage'] in related.get(component, set())],
                         'host_errors': host if component in {'host', 'core'} else [],
                         'evidence_hashes': hashes, 'expected_change': change,
                         'activation_gate': 'independent_acceptance_scope_budget_and_rollback'},
             'evidence_run_ids': sorted({*hashes} - {key for key in hashes if key.startswith('host:')})}
            for component, change in expected.items()]


def cycle(db_path, owned_root, *, auto_apply=True, proposer=None,
          max_proposals=32, max_history_runs=100):
    """Resume bounded maintenance; immutable certificates retain complete fresh checks.

    proposer receives only {'verified_feedback': current fresh feedback}. Distinct
    external overflow is refused before any database or candidate artifact write.
    History and background rechecks have separate per-cycle budgets, not a lifetime cap.
    """
    from .maintenance import maintenance_lease
    for label, value, ceiling in [('proposal', max_proposals, 32), ('history', max_history_runs, 100)]:
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(label + ' budget must be a positive integer <= ' + str(ceiling))
    with maintenance_lease(db_path, owned_root) as owner:
        if not owner:
            return {'verified_feedback_count': 0, 'proposal_count': 0, 'proposal_ids': [],
                    'validated': [], 'applied': [], 'proposal_origins': [], 'rejected': [],
                    'quarantined': [], 'budget': {'max_proposals': max_proposals,
                    'max_history_runs': max_history_runs}, 'owned_root': str(Path(owned_root).resolve()),
                    'maintenance': {'state': 'busy', 'cursor': {}, 'inspected_run_ids': [],
                    'remaining_run_count': None, 'pending_proposal_count': None,
                    'has_more': True, 'busy': True}}
        return _cycle_owned(db_path, owned_root, auto_apply, proposer, max_proposals, max_history_runs)


def _cycle_owned(db_path, owned_root, auto_apply, proposer, max_proposals, max_history_runs):
    from .candidates import (_certificate, apply_candidate, current_revisions, propose_candidate,
                             quarantine_candidate, validate_candidate)
    from .maintenance import discover, enqueue, preview, summary, update_work
    runs_store = RunStore.read_only(db_path) if Path(db_path).exists() else None
    batch = preview(runs_store, max_history_runs)
    identifiers = list(dict.fromkeys([*batch['work'],
                                     *[item['subject_id'] for item in batch['recheck']]]))
    runs = [runs_store.get(identifier) for identifier in identifiers]
    cache = {}

    def fresh(identifier):
        if identifier not in cache:
            try:
                cache[identifier] = inspect_verified_feedback(identifier, db_path)
            except (ValueError, OSError, KeyError):
                cache[identifier] = None
        return cache[identifier]

    feedback = [item for identifier in identifiers if (item := fresh(identifier)) is not None]
    external = []
    if proposer is not None:
        proposed = proposer({'verified_feedback': feedback})
        if not isinstance(proposed, list) or any(not isinstance(item, dict) for item in proposed):
            raise ValueError('proposer must return a list of candidate objects')
        # Canonicalize reference order before counting otherwise identical objects.
        unique = {}
        for item in proposed:
            item = dict(item)
            if isinstance(item.get('evidence_run_ids'), list):
                item['evidence_run_ids'] = sorted(set(item['evidence_run_ids']))
            unique.setdefault(digest(item), item)
        external = list(unique.values())
        if len(external) > max_proposals:
            raise ValueError('proposal count exceeds the fixed maintenance budget')
    # Only external preflight succeeded. Discovery and queue insertion are atomic;
    # no cursor is committed without a durable record for every discovered item.
    RunStore(db_path)
    store = EvolutionStore(db_path, owned_root)
    discover(store, batch)
    for item in feedback:
        store.add_feedback(item)
    quarantined = []
    # Independently re-read all original evidence for the four active slots.
    # These safety checks are not weakened by the history discovery budget.
    for component, kind in [('executor', 'csv_executor'), ('recipe', 'recipe'),
                            ('executor', 'json_executor'), ('recipe', 'json_recipe')]:
        identifier = store.pointer(component, kind)[0]
        if identifier is None:
            continue
        try:
            _certificate(store, store.get_version(identifier), fresh_evidence=True)
        except (ValueError, OSError, RuntimeError, KeyError):
            quarantined.append(quarantine_candidate(identifier, db_path,
                'supporting durable evidence or validation revision is unavailable, stale or corrupt'))
    # Experiences rotate separately; complete original references are verified,
    # never compared against membership in this turn's new history batch.
    with store.connect() as db:
        row = db.execute("SELECT cursor FROM evolution_maintenance_state WHERE name='experiences'").fetchone()
        position = row[0] if row else 0
        experiences = db.execute('SELECT rowid,* FROM evolution_experiences WHERE rowid>? '
                                 'ORDER BY rowid LIMIT ?', (position, max_history_runs)).fetchall()
        if not experiences:
            experiences = db.execute('SELECT rowid,* FROM evolution_experiences ORDER BY rowid LIMIT ?',
                                     (max_history_runs,)).fetchall()
    for row in experiences:
        experience = {**dict(row), **json.loads(row['payload_json'])}
        if experience['state'] == 'quarantine':
            continue
        if not experience['evidence_run_ids'] or any(fresh(identifier) is None
                                                    for identifier in experience['evidence_run_ids']):
            store.set_experience_state(experience['experience_id'], 'quarantine')
            quarantined.append({'experience_id': experience['experience_id'],
                                'reason': 'supporting durable artifact is no longer verified'})
    with store.connect() as db:
        if experiences:
            db.execute("INSERT INTO evolution_maintenance_state VALUES('experiences',?) "
                       'ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor', (experiences[-1]['rowid'],))
        host_rows = [json.loads(row[0]) for row in db.execute(
            "SELECT payload_json FROM evolution_maintenance_queue WHERE kind='host' AND state='pending' "
            'ORDER BY sequence LIMIT ?', (max_history_runs,))]
    # Diagnose occurrences independently so the semantic identity cannot depend
    # on which unrelated failures happened to share a batch.
    generated = []
    for run in runs:
        item = cache.get(run['run_id'])
        generated.extend(_diagnoses(runs_store, [run], [item] if item else [], []))
    for row in host_rows:
        generated.extend(_diagnoses(runs_store, [], [], [row]))
    if not runs and not host_rows:
        generated = _diagnoses(runs_store, [], [], [])
    grouped = {}
    for item in feedback:
        plan = item['plan']
        if (plan['capability'], plan['input_format']) not in {
                ('tabular.deduplicate', 'csv'), ('json.normalize', 'json')}:
            continue
        recipe = _recipe(item)
        group = grouped.setdefault(digest(recipe), {'recipe': recipe, 'run_ids': []})
        group['run_ids'].append(item['run_id'])
    for item in generated:
        hashes = item['content']['evidence_hashes']
        references = {identifier: {'sha256': value,
            'problems': [fact for fact in item['content']['problems'] if fact['run_id'] == identifier],
            'host_errors': [fact for fact in item['content']['host_errors']
                            if 'host:' + fact['host_event_id'] == identifier]}
                      for identifier, value in hashes.items()}
        enqueue(store, item, 'default_diagnostic', references)
    for key, group in sorted(grouped.items()):
        recipe = group['recipe']
        lookup = recipe['capability'] + ':' + recipe['input_format'] + ':' + key
        # A recipe's immutable first anchor stays small. Every later successful
        # occurrence remains inspectable in the evidence ledger and feedback table.
        anchor = group['run_ids'][:1]
        store.add_experience(lookup, {'recipe': recipe, 'evidence_run_ids': anchor})
        enqueue(store, {'component': 'recipe', 'kind': 'json_recipe'
                        if recipe['capability'] == 'json.normalize' else 'recipe',
                        'content': recipe, 'evidence_run_ids': anchor}, 'default_verified_recipe',
                {identifier: {'sha256': digest(cache[identifier])} for identifier in group['run_ids']})
    for item in external:
        enqueue(store, item, 'external_injected_code',
                {identifier: {'sha256': digest(value)} for identifier in item.get('evidence_run_ids', [])
                 if (value := fresh(identifier)) is not None})
    # Do not mark inspected work complete until all resulting proposals/evidence
    # are durably queued. A crash at any earlier point can replay this same work.
    for run in runs:
        if run['run_id'] in batch['work']:
            update_work(store, 'run:' + run['run_id'],
                        'waiting' if run['status'] in {'received', 'running'} else 'done')
    for row in host_rows:
        update_work(store, 'host:' + row['ingest_event_id'], 'done')
    with store.connect() as db:
        db.execute("INSERT INTO evolution_maintenance_state VALUES('recheck',?) "
                   'ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor', (batch['recheck_cursor'],))
        persisted = db.execute("SELECT proposal_id FROM evolution_proposals p WHERE component='executor' "
            "AND kind IN ('csv_executor','json_executor') AND state IN ('proposed','validated') "
            "AND NOT EXISTS (SELECT 1 FROM evolution_maintenance_queue q WHERE q.kind='candidate' "
            "AND json_extract(q.payload_json,'$.proposal_id')=p.proposal_id) "
            'ORDER BY created_at,proposal_id LIMIT 100').fetchall()
        for row in persisted:
            key = 'persisted:' + row['proposal_id']
            payload = canonical_json({'proposal_id': row['proposal_id'], 'origin': 'external_persisted_proposal'})
            now = time.time()
            db.execute('INSERT OR IGNORE INTO evolution_maintenance_queue '
                '(work_key,kind,subject_id,payload_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                (key, 'candidate', row['proposal_id'], payload, 'pending', now, now))
        if auto_apply:
            # A previous no-auto-apply cycle leaves a validated immutable candidate
            # ready for explicit later activation; do not invent a new proposal.
            for row in db.execute("SELECT work_key,payload_json FROM evolution_maintenance_queue "
                "WHERE kind='candidate' AND state='done' "
                "AND json_extract(payload_json,'$.deferred_apply')=1 ORDER BY sequence LIMIT ?",
                (max_proposals,)).fetchall():
                payload = json.loads(row['payload_json'])
                if payload.get('deferred_apply'):
                    db.execute("UPDATE evolution_maintenance_queue SET state='pending' WHERE work_key=?",
                               (row['work_key'],))
        work = [dict(row) for row in db.execute("SELECT * FROM evolution_maintenance_queue "
            "WHERE kind='candidate' AND state='pending' ORDER BY sequence LIMIT ?", (max_proposals,))]
    proposals, validated, applied, rejected, origins = [], [], [], [], []
    for row in work:
        payload = json.loads(row['payload_json'])
        origin = payload['origin']
        candidate = payload.get('candidate')
        proposal = store.get_proposal(payload['proposal_id']) if payload.get('proposal_id') else None
        if proposal is None:
            active = store.get_active(candidate['component'], candidate['kind'])
            raw = canonical_json(candidate['content']).encode() if isinstance(candidate['content'], dict) else candidate['content'].encode()
            if (active and active['validation']['revisions'] == current_revisions(candidate['kind'])
                    and active['content_hash'] == digest(raw)):
                _certificate(store, active, fresh_evidence=True)
                proposal = active
            else:
                proposal = propose_candidate(db_path, owned_root, **candidate)
            payload['proposal_id'] = proposal['proposal_id']
            update_work(store, row['work_key'], 'pending', payload)
        proposal_id = proposal['proposal_id']
        if proposal['state'] not in {'proposed', 'validated'}:
            # Crash replay or healthy active reuse needs no new candidate work.
            update_work(store, row['work_key'], 'done', payload)
            continue
        proposals.append(proposal_id)
        origins.append({'proposal_id': proposal_id, 'origin': origin, 'reused_active': False})
        report = validate_candidate(proposal_id, db_path)
        if report.get('passed') is not True:
            rejected.append({'proposal_id': proposal_id, 'validation': report})
            update_work(store, row['work_key'], 'done', payload)
            continue
        validated.append(proposal_id)
        proposal = store.get_proposal(proposal_id)
        current = store.get_active(proposal['component'], proposal['kind'])
        if (proposal['kind'] in {'csv_executor', 'json_executor'} and current
                and current['content_hash'] == proposal['content_hash']
                and current['validation']['revisions'] == current_revisions(proposal['kind'])):
            before = store.pointer(proposal['component'], proposal['kind'])
            _certificate(store, current, fresh_evidence=True)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                pointer = db.execute('SELECT version_id,generation FROM evolution_active WHERE slot=?',
                                     (proposal['component'] + ':' + proposal['kind'],)).fetchone()
                fresh_proposal = store._proposal(db.execute('SELECT * FROM evolution_proposals WHERE proposal_id=?',
                                                            (proposal_id,)).fetchone())
                if (not pointer or (pointer[0], pointer[1]) != before or fresh_proposal['state'] != 'validated'
                        or fresh_proposal['validation_hash'] != proposal['validation_hash']):
                    rejected.append({'proposal_id': proposal_id, 'retry_required': True,
                                     'reason': 'active pointer or certificate changed; retry pending proposal'})
                    continue
                _certificate(store, fresh_proposal)
                db.execute("UPDATE evolution_proposals SET state='superseded' WHERE proposal_id=?", (proposal_id,))
            origins[-1].update(reused_active=True, reused_version_id=current['version_id'], state='superseded')
        elif auto_apply and not (proposal['kind'] == 'json_recipe' and current is not None):
            try:
                applied.append(apply_candidate(proposal_id, db_path))
            except ValueError as error:
                rejected.append({'proposal_id': proposal_id, 'reason': str(error)})
        payload['deferred_apply'] = not auto_apply
        update_work(store, row['work_key'], 'done', payload)
    maintenance = summary(store, batch['work'], auto_apply=auto_apply)
    maintenance.update(discovered_run_count=len(batch['new']), scanned_host_count=len(batch['host']),
                       rechecked_run_ids=[row['subject_id'] for row in batch['recheck']])
    return {'verified_feedback_count': len(feedback), 'proposal_count': len(proposals),
            'proposal_ids': proposals, 'validated': validated, 'applied': applied,
            'proposal_origins': origins, 'rejected': rejected, 'quarantined': quarantined,
            'budget': {'max_proposals': max_proposals, 'max_history_runs': max_history_runs},
            'owned_root': str(store.owned_root), 'maintenance': maintenance}
