"""A small evidence-driven maintenance cycle, with an optional external proposer."""
from __future__ import annotations

import json
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


def _diagnoses(store, runs, feedback):
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
    if store is not None:
        with store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'hermes_host_events' in tables:
                rows = db.execute('SELECT ingest_event_id,kind,metadata_json FROM hermes_host_events ORDER BY id').fetchall()
                for row in rows:
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
    """Persist diagnoses, consolidate verified recipes, and gate all activation.

    proposer(context) returns component/kind/content/evidence_run_ids dictionaries.
    It receives verified history; it receives neither held-out fixtures nor oracle answers.
    """
    from .candidates import (_certificate, apply_candidate, current_revisions, propose_candidate,
                             quarantine_candidate, validate_candidate)
    for label, value, ceiling in [('proposal', max_proposals, 32), ('history', max_history_runs, 100)]:
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(label + ' budget must be a positive integer <= ' + str(ceiling))
    # Refuse an oversized batch before schema creation, activation recovery,
    # feedback persistence or any quarantine. Never silently drop old evidence.
    runs_store = RunStore.read_only(db_path) if Path(db_path).exists() else None
    if runs_store is not None:
        with runs_store.connect() as db:
            table = db.execute("SELECT name FROM sqlite_master WHERE name='runs'").fetchone()
            identifiers = [row[0] for row in db.execute(
                'SELECT run_id FROM runs ORDER BY started_at,run_id LIMIT ?', (max_history_runs + 1,))] if table else []
        if len(identifiers) > max_history_runs:
            raise ValueError('history exceeds the fixed maintenance budget')
    else:
        identifiers = []
    runs = [runs_store.get(run_id) for run_id in identifiers]
    feedback = [item for run_id in identifiers
                if (item := inspect_verified_feedback(run_id, db_path)) is not None]
    generated = _diagnoses(runs_store, runs, feedback)
    origins = ['default_diagnostic'] * len(generated)
    grouped = {}
    for item in feedback:
        plan = item['plan']
        if (plan['capability'], plan['input_format']) not in {
                ('tabular.deduplicate', 'csv'), ('json.normalize', 'json')}:
            continue
        recipe = _recipe(item)
        key = digest(recipe)
        grouped.setdefault(key, {'recipe': recipe, 'run_ids': []})['run_ids'].append(item['run_id'])
    for key in sorted(grouped):
        group = grouped[key]
        kind = 'json_recipe' if group['recipe']['capability'] == 'json.normalize' else 'recipe'
        generated.append({'component': 'recipe', 'kind': kind, 'content': group['recipe'],
                          'evidence_run_ids': group['run_ids']})
        origins.append('default_verified_recipe')
    if proposer is not None:
        proposed = proposer({'verified_feedback': feedback})
        if not isinstance(proposed, list) or any(not isinstance(item, dict) for item in proposed):
            raise ValueError('proposer must return a list of candidate objects')
        generated.extend(proposed)
        origins.extend(['external_injected_code'] * len(proposed))
    pending = []
    if runs_store is not None:
        with runs_store.connect() as db:
            table = db.execute("SELECT name FROM sqlite_master WHERE name='evolution_proposals'").fetchone()
            if table:
                pending = [row[0] for row in db.execute(
                    "SELECT proposal_id FROM evolution_proposals WHERE component='executor' "
                    "AND kind='json_executor' AND state IN ('proposed','validated') "
                    'ORDER BY created_at,proposal_id LIMIT ?', (max_proposals + 1,))]
    if len(generated) + len(pending) > max_proposals:
        raise ValueError('proposal count exceeds the fixed maintenance budget')
    # Only a bounded, successfully inspected batch may begin maintenance writes.
    runs_store = RunStore(db_path)
    store = EvolutionStore(db_path, owned_root)
    for item in feedback:
        store.add_feedback(item)
    valid_ids = {item['run_id'] for item in feedback}
    quarantined = []
    for experience in store.list_experiences():
        if not set(experience['evidence_run_ids']).issubset(valid_ids):
            store.set_experience_state(experience['experience_id'], 'quarantine')
            quarantined.append({'experience_id': experience['experience_id'],
                                'reason': 'supporting durable artifact is no longer verified'})
    for component, kind in [('executor', 'csv_executor'), ('recipe', 'recipe'),
                            ('executor', 'json_executor'), ('recipe', 'json_recipe')]:
        identifier = store.pointer(component, kind)[0]
        if identifier is None:
            continue
        try:
            active = store.get_version(identifier)
            supported = bool(active['evidence_run_ids']) and set(active['evidence_run_ids']).issubset(valid_ids)
            if supported:
                _certificate(store, active, fresh_evidence=True)
        except (ValueError, OSError, RuntimeError, KeyError):
            supported = False
        if not supported:
            quarantined.append(quarantine_candidate(identifier, db_path,
                                                     'supporting durable evidence is unavailable or corrupt'))
    for key, group in grouped.items():
        recipe = group['recipe']
        lookup = recipe['capability'] + ':' + recipe['input_format'] + ':' + key
        store.add_experience(lookup, {'recipe': group['recipe'], 'evidence_run_ids': group['run_ids']})
    proposals, validated, applied, rejected, proposal_origins = [], [], [], [], []
    for candidate, origin in zip(generated, origins):
        active = store.get_active(candidate['component'], candidate['kind'])
        if (active and active['validation']['revisions'] == current_revisions(candidate['kind'])
                and active['content_hash'] == digest(canonical_json(candidate['content']).encode()
                                                      if isinstance(candidate['content'], dict) else candidate['content'].encode())):
            _certificate(store, active, fresh_evidence=True)
            proposals.append(active['proposal_id'])
            proposal_origins.append({'proposal_id': active['proposal_id'], 'origin': origin,
                                     'reused_active': True})
            continue
        proposal = propose_candidate(db_path, owned_root, **candidate)
        proposals.append(proposal['proposal_id'])
        proposal_origins.append({'proposal_id': proposal['proposal_id'], 'origin': origin,
                                 'reused_active': False})
        if proposal['state'] in {'applied', 'quarantine'}:
            continue
        report = validate_candidate(proposal['proposal_id'], db_path)
        if report['passed'] is not True:
            rejected.append({'proposal_id': proposal['proposal_id'], 'validation': report})
            continue
        validated.append(proposal['proposal_id'])
        # One light recipe slot serves one declared style. Preserve an existing
        # healthy recipe instead of toggling it as new styles/evidence arrive;
        # other groups keep their own validated proposals and lookup records.
        keep_json_slot = (candidate['kind'] == 'json_recipe'
                          and store.get_active('recipe', 'json_recipe') is not None)
        if auto_apply and not keep_json_slot:
            try:
                applied.append(apply_candidate(proposal['proposal_id'], db_path))
            except ValueError as error:
                rejected.append({'proposal_id': proposal['proposal_id'], 'reason': str(error)})
    # Independent CLI Agents may persist immutable code proposals first. A later
    # bounded cycle consumes exactly those IDs, without inventing a new base or
    # admitting unsupported code kinds. Other-style recipes stay validated.
    for proposal_id in pending:
        if proposal_id in proposals:
            continue
        proposal = store.get_proposal(proposal_id)
        proposals.append(proposal_id)
        proposal_origins.append({'proposal_id': proposal_id,
                                 'origin': 'external_persisted_proposal', 'reused_active': False})
        if proposal['state'] not in {'proposed', 'validated'}:
            continue
        report = validate_candidate(proposal_id, db_path)
        if report.get('passed') is not True:
            rejected.append({'proposal_id': proposal_id, 'validation': report})
            continue
        validated.append(proposal_id)
        current = store.get_active('executor', 'json_executor')
        pending_proposal = store.get_proposal(proposal_id)
        if (current and current['content_hash'] == pending_proposal['content_hash']
                and current['validation']['revisions'] == current_revisions('json_executor')):
            before = store.pointer('executor', 'json_executor')
            _certificate(store, current, fresh_evidence=True)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT version_id,generation FROM evolution_active WHERE slot='executor:json_executor'").fetchone()
                pointer = (row['version_id'], row['generation']) if row else (None, 0)
                if pointer != before or pointer[0] != current['version_id']:
                    rejected.append({'proposal_id': proposal_id,
                                     'reason': 'active pointer changed; retry pending proposal',
                                     'retry_required': True})
                    continue
                fresh = store._proposal(db.execute('SELECT * FROM evolution_proposals WHERE proposal_id=?',
                                                   (proposal_id,)).fetchone())
                if fresh['state'] != 'validated' or fresh['validation_hash'] != pending_proposal['validation_hash']:
                    rejected.append({'proposal_id': proposal_id,
                                     'reason': 'pending certificate changed; retry pending proposal',
                                     'retry_required': True})
                    continue
                _certificate(store, fresh)
                db.execute("UPDATE evolution_proposals SET state='superseded' WHERE proposal_id=?",
                           (proposal_id,))
            proposal_origins[-1].update(reused_active=True, reused_version_id=current['version_id'],
                                       state='superseded')
            continue
        if auto_apply:
            try:
                applied.append(apply_candidate(proposal_id, db_path))
            except ValueError as error:
                rejected.append({'proposal_id': proposal_id, 'reason': str(error)})
    return {'verified_feedback_count': len(feedback), 'proposal_count': len(proposals),
            'proposal_ids': proposals, 'validated': validated, 'applied': applied,
            'proposal_origins': proposal_origins, 'rejected': rejected, 'quarantined': quarantined,
            'budget': {'max_proposals': max_proposals, 'max_history_runs': max_history_runs},
            'owned_root': str(store.owned_root)}
