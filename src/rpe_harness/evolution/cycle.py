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
            existing = db.execute('SELECT payload_hash FROM evolution_feedback WHERE run_id=?',
                                  (run_id,)).fetchone()
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
    fields = ('capability', 'input_format', 'key_columns', 'constraints', 'sheet',
              'keep', 'encoding', 'delimiter')
    return {**{key: plan[key] for key in fields},
            'procedure': ['plan', 'prepare', 'independent_verify', 'exclusive_publish']}


def _diagnoses(store, runs, feedback):
    """Use persisted stage/type facts rather than infer causes from keywords."""
    hashes = {item['run_id']: digest(item) for item in feedback}
    problems = []
    for run in runs:
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
        'executor': 'Propose a CSV producer variant and check exact row membership, order and types.',
        'host': 'Add a host admission/error regression from persisted event types and receipt hashes.',
        'core': 'Review failed stages and retry links; require a separately owned trusted suite for a core change.',
        'verifier': 'Add independent adversarial fixtures for verification failures; preserve existing oracle approval.',
    }
    related = {'contract': {'validate'}, 'discovery': {'route'}, 'executor': {'execute'},
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


def cycle(db_path, owned_root, *, auto_apply=True, proposer=None):
    """Persist diagnoses, consolidate verified recipes, and gate all activation.

    proposer(context) returns component/kind/content/evidence_run_ids dictionaries.
    It receives verified history; it receives neither held-out fixtures nor oracle answers.
    """
    from .candidates import (apply_candidate, current_revisions, propose_candidate,
                             quarantine_candidate, validate_candidate)
    store = EvolutionStore(db_path, owned_root)
    runs_store = RunStore(db_path)
    with store.connect() as db:
        identifiers = [row[0] for row in db.execute('SELECT run_id FROM runs ORDER BY started_at')]
    runs = [runs_store.get(run_id) for run_id in identifiers]
    feedback = [item for run_id in identifiers
                if (item := record_verified_feedback(run_id, db_path)) is not None]
    valid_ids = {item['run_id'] for item in feedback}
    quarantined = []
    for experience in store.list_experiences():
        if not set(experience['evidence_run_ids']).issubset(valid_ids):
            store.set_experience_state(experience['experience_id'], 'quarantine')
            quarantined.append({'experience_id': experience['experience_id'],
                                'reason': 'supporting durable artifact is no longer verified'})
    for component, kind in [('executor', 'csv_executor'), ('recipe', 'recipe')]:
        identifier = store.pointer(component, kind)[0]
        if identifier is None:
            continue
        try:
            active = store.get_version(identifier)
            supported = bool(active['evidence_run_ids']) and set(active['evidence_run_ids']).issubset(valid_ids)
        except (ValueError, KeyError):
            supported = False
        if not supported:
            quarantined.append(quarantine_candidate(identifier, db_path,
                                                     'supporting durable evidence is unavailable or corrupt'))
    generated = _diagnoses(store, runs, feedback)
    # The unattended default always produces durable, evidence-bound architecture
    # diagnoses, including components that need their own future trusted suite.
    grouped = {}
    for item in feedback:
        if item['plan']['input_format'] != 'csv':
            continue
        recipe = _recipe(item)
        key = digest(recipe)
        grouped.setdefault(key, {'recipe': recipe, 'run_ids': []})['run_ids'].append(item['run_id'])
    for key, group in grouped.items():
        lookup = 'tabular.deduplicate:csv:' + key
        store.add_experience(lookup, {'recipe': group['recipe'], 'evidence_run_ids': group['run_ids']})
        generated.append({'component': 'recipe', 'kind': 'recipe', 'content': group['recipe'],
                          'evidence_run_ids': group['run_ids']})
    if proposer is not None:
        proposed = proposer({'verified_feedback': feedback})
        if not isinstance(proposed, list) or any(not isinstance(item, dict) for item in proposed):
            raise ValueError('proposer must return a list of candidate objects')
        generated.extend(proposed)
    proposals, validated, applied, rejected = [], [], [], []
    for candidate in generated:
        active = store.get_active(candidate['component'], candidate['kind'])
        if (active and active['validation']['revisions'] == current_revisions()
                and active['content_hash'] == digest(canonical_json(candidate['content']).encode()
                                                      if isinstance(candidate['content'], dict) else candidate['content'].encode())
                and active['evidence_run_ids'] == sorted(set(candidate['evidence_run_ids']))):
            proposals.append(active['proposal_id'])
            continue
        proposal = propose_candidate(db_path, owned_root, **candidate)
        proposals.append(proposal['proposal_id'])
        if proposal['state'] in {'applied', 'quarantine'}:
            continue
        report = validate_candidate(proposal['proposal_id'], db_path)
        if report['passed'] is not True:
            rejected.append({'proposal_id': proposal['proposal_id'], 'validation': report})
            continue
        validated.append(proposal['proposal_id'])
        if auto_apply:
            try:
                applied.append(apply_candidate(proposal['proposal_id'], db_path))
            except ValueError as error:
                rejected.append({'proposal_id': proposal['proposal_id'], 'reason': str(error)})
    return {'verified_feedback_count': len(feedback), 'proposal_count': len(proposals),
            'proposal_ids': proposals, 'validated': validated, 'applied': applied,
            'rejected': rejected, 'quarantined': quarantined, 'owned_root': str(store.owned_root)}
