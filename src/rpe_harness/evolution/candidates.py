"""Immutable candidates, independent acceptance, CAS activation and recovery."""
from __future__ import annotations

import csv
import fcntl
import io
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from ..storage import canonical_json
from .sandbox import resource_limits, run_candidate
from .store import EvolutionStore, digest

_LIFECYCLE_REVISION = digest(Path(__file__).read_bytes())

COMPONENTS = frozenset({'contract', 'discovery', 'recipe', 'executor', 'host', 'core', 'verifier'})
JSON_KINDS = frozenset({'json_executor', 'json_recipe'})
REGISTERED_PAIRS = frozenset({('executor', 'csv_executor'), ('recipe', 'recipe'),
                             ('executor', 'json_executor'), ('recipe', 'json_recipe')})

# Manual truths are authored independently of the producer; held-out cases are never proposer input.
_FIXTURES = (
    ('chinese', 'train', '订单号,金额\n001,10\n002,20\n001,99\n', ['订单号'],
     [['订单号', '金额'], ['001', '10'], ['002', '20']], {}),
    ('composite', 'train', 'group,id,value\nA,01,first\nB,01,second\nA,01,last\n', ['group', 'id'],
     [['group', 'id', 'value'], ['A', '01', 'first'], ['B', '01', 'second']], {}),
    ('semicolon', 'train', 'id;note\n1;\n1;later\n2;保留\n', ['id'],
     [['id', 'note'], ['1', ''], ['2', '保留']], {'delimiter': ';', 'encoding': 'utf-8'}),
    ('untrimmed_types', 'heldout', 'key,value\n01,zero\n1,integer\n1.0,decimal\n 01 ,space\n01,later\n', ['key'],
     [['key', 'value'], ['01', 'zero'], ['1', 'integer'], ['1.0', 'decimal'], [' 01 ', 'space']], {}),
    ('quoted_multiline', 'heldout', 'key,note\n"甲\n乙","first,record"\n"甲\n乙",later\n甲,other\n', ['key'],
     [['key', 'note'], ['甲\n乙', 'first,record'], ['甲', 'other']], {}),
    ('case_composite', 'heldout', '类别,编号,内容\nA,000,首条\na,000,小写\nA,0,不同编号\nA,000,重复\n', ['类别', '编号'],
     [['类别', '编号', '内容'], ['A', '000', '首条'], ['a', '000', '小写'], ['A', '0', '不同编号']], {}),
    ('zero_records', 'heldout', 'id,value\n', ['id'], [['id', 'value']], {}),
    ('missing_column', 'heldout', 'id,value\n1,a\n', ['missing'], None, {}),
    ('empty_key', 'heldout', 'id,value\n,a\n', ['id'], None, {}),
    ('duplicate_header', 'heldout', 'id,id\n1,a\n', ['id'], None, {}),
    ('unsupported_keep', 'heldout', 'id,value\n1,a\n', ['id'], None, {'keep': 'last'}),
    ('unknown_option', 'heldout', 'id,value\n1,a\n', ['id'], None, {'send_email': True}),
    ('no_overwrite', 'heldout', 'id,value\n1,a\n', ['id'], None, {'existing_output': True}),
)


def current_revisions(kind='csv_executor'):
    if kind in JSON_KINDS:
        from .json_acceptance import current_revisions as json_revisions
        return json_revisions()
    from .. import contracts
    return {'contract_version': contracts.CONTRACT_VERSION,
            'contract_revision': digest(Path(contracts.__file__).read_bytes()),
            'executor_revision': contracts.EXECUTOR_REVISION,
            'verifier_revision': contracts.VERIFIER_REVISION,
            'verifier_file_revision': digest(Path(contracts.__file__).with_name('verification.py').read_bytes()),
            'acceptance_revision': digest(Path(__file__).read_bytes()),
            'sandbox_revision': digest(Path(__file__).with_name('sandbox.py').read_bytes()),
            'fixture_suite_sha256': digest(_FIXTURES)}


def propose_candidate(db_path, owned_root, *, component, kind, content,
                      evidence_run_ids, base_version_id=None, provenance=None):
    if component not in COMPONENTS or not isinstance(kind, str) or not kind:
        raise ValueError('candidate must name a known component and a kind')
    if not isinstance(evidence_run_ids, list) or any(not isinstance(x, str) or not x for x in evidence_run_ids):
        raise ValueError('evidence_run_ids must be durable run identifiers')
    store = EvolutionStore(db_path, owned_root)
    if kind in {'recipe', 'json_recipe'}:
        if isinstance(content, str):
            content = json.loads(content)
        if not isinstance(content, dict):
            raise ValueError('recipe content must be a declarative object')
        raw, suffix = canonical_json(content).encode(), '.json'
    elif isinstance(content, str):
        raw, suffix = content.encode('utf-8'), '.py' if kind in {'csv_executor', 'json_executor'} else '.txt'
    elif isinstance(content, dict):
        raw, suffix = canonical_json(content).encode(), '.json'
    else:
        raise ValueError('candidate content must be text or a declarative object')
    if not raw or len(raw) > 65536:
        raise ValueError('candidate content must be nonempty and <=64KiB')
    artifact, content_hash = store.write_artifact(raw, suffix)
    current, generation = store.pointer(component, kind)
    base = base_version_id if base_version_id is not None else current
    payload = {'evidence_run_ids': sorted(set(evidence_run_ids)), 'owned_root': str(store.owned_root),
               'requirements': ['independent_manual_fixtures', 'source_preservation', 'no_overwrite',
                                'same_contract_and_verifier', 'owned_scope', 'durable_rollback'],
               'budget': {'max_fixture_cases': len(_fixtures(kind)), 'timeout_per_case_s': 3.0},
               'rollback': {'strategy': 'restore_previous_pointer_or_registered_builtin'},
               'revisions': current_revisions(kind)}
    if provenance is not None:
        if not isinstance(provenance, dict) or len(canonical_json(provenance).encode()) > 65536:
            raise ValueError('candidate provenance must be a bounded object')
        payload['provenance'] = provenance
    fingerprint = digest({'component': component, 'kind': kind, 'content_hash': content_hash,
                          'base': base, 'base_generation': generation, **payload})
    identifier = fingerprint
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO evolution_proposals '
                   '(proposal_id,fingerprint,component,kind,content_hash,artifact_path,base_version_id,'
                   'base_generation,payload_json,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (identifier, fingerprint, component, kind, content_hash, artifact, base, generation,
                    canonical_json(payload), 'proposed', time.time()))
    return store.get_proposal(identifier)


def _evidence(store, proposal):
    from .cycle import record_verified_feedback, inspect_verified_feedback
    feedback = inspect_verified_feedback if store._read_only else record_verified_feedback
    evidence = {}
    for run_id in proposal['evidence_run_ids']:
        item = feedback(run_id, store.path)
        if item is None or item.get('plan_sha256') is None:
            raise ValueError('candidate requires durable verified plan-bound evidence')
        expected = (('json.normalize', 'json') if proposal['kind'] in JSON_KINDS
                    else ('tabular.deduplicate', 'csv'))
        if (proposal['component'], proposal['kind']) in REGISTERED_PAIRS:
            plan = item.get('plan') or {}
            if (plan.get('capability'), plan.get('input_format')) != expected:
                raise ValueError('candidate evidence must match its registered capability and input contract')
        evidence[run_id] = digest(item)
    if not evidence:
        raise ValueError('candidate has no verified evidence')
    return evidence


def _proposal_identity(store, proposal):
    """Freeze every proposal input, including the presence of source provenance."""
    with store.connect() as db:
        row = db.execute('SELECT * FROM evolution_proposals WHERE proposal_id=?',
                         (proposal['proposal_id'],)).fetchone()
    if row is None:
        raise ValueError('candidate proposal identity is missing')
    payload = json.loads(row['payload_json'])
    expected = digest({'component': row['component'], 'kind': row['kind'],
                       'content_hash': row['content_hash'], 'base': row['base_version_id'],
                       'base_generation': row['base_generation'], **payload})
    fields = ('proposal_id', 'fingerprint', 'component', 'kind', 'content_hash',
              'base_version_id', 'base_generation')
    if (expected != row['fingerprint'] or expected != row['proposal_id']
            or any(proposal.get(key) != row[key] for key in fields)
            or any(proposal.get(key) != value for key, value in payload.items())):
        raise ValueError('candidate proposal fingerprint or source provenance changed')


def _preflight(plan):
    from ..path_binding import current_binding, read_source
    if (plan.capability, plan.input_format) not in {('tabular.deduplicate', 'csv'), ('json.normalize', 'json')}:
        raise ValueError('code candidate is not registered for this contract')
    source = Path(plan.input_path)
    binding = current_binding()
    if binding is not None:
        binding.require_paths(source, plan.output_path)
    else:
        if os.path.lexists(plan.output_path):
            raise ValueError('output already exists; overwriting is forbidden')
        if source.is_symlink() or not source.is_file():
            raise ValueError('source changed after the execution plan')
    budget = resource_limits(plan.capability)['input_bytes']
    if digest(read_source(source, max_bytes=budget)) != plan.input_sha256:
        raise ValueError('source changed after the execution plan')


def _prepare_code(plan, content, root, *, timeout=3.0):
    if plan.capability == 'json.normalize' and plan.input_format == 'json':
        from .json_acceptance import prepare_code
        return prepare_code(plan, content, root, timeout=timeout)
    from ..execution import _new_staging, cleanup_staging
    from ..path_binding import read_source
    _preflight(plan)
    data = run_candidate(content, plan.to_dict(), root, timeout=timeout)
    prepared = plan.verification_parameters()
    staging = _new_staging(Path(plan.input_path), Path(plan.output_path), plan.input_sha256)
    prepared['staging_path'] = str(staging)
    try:
        with staging.open('wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        original = list(csv.reader(io.StringIO(read_source(plan.input_path,
            max_bytes=resource_limits()['input_bytes']).decode(plan.encoding), newline=''),
                                   delimiter=plan.delimiter, strict=True))
        produced = list(csv.reader(io.StringIO(data.decode(plan.encoding), newline=''),
                                   delimiter=plan.delimiter, strict=True))
        prepared.update(input_rows=len(original) - 1, output_rows=len(produced) - 1,
                        removed_rows=len(original) - len(produced))
        return prepared
    except BaseException:
        cleanup_staging(prepared)
        raise


def _recipe_valid(recipe, kind='recipe'):
    if kind == 'json_recipe':
        from .json_acceptance import recipe_valid
        return recipe_valid(recipe)
    allowed = {'capability', 'input_format', 'key_columns', 'constraints', 'keep', 'encoding',
               'delimiter', 'sheet', 'procedure'}
    if (not isinstance(recipe, dict) or set(recipe) - allowed
            or recipe.get('capability') != 'tabular.deduplicate'
            or recipe.get('input_format') != 'csv' or recipe.get('keep') != 'first'
            or recipe.get('procedure') != ['plan', 'prepare', 'independent_verify', 'exclusive_publish']):
        raise ValueError('recipe cannot replace the registered execution procedure')


def _fixture_check(content, kind, fixture, root, timeout):
    from ..contracts import ExecutionPlan
    from ..execution import cleanup_staging, prepare_deduplication
    from ..verification import verify_deduplication
    name, split, source_text, keys, expected, supplied = fixture
    directory = root / name
    directory.mkdir()
    options = dict(supplied)
    source, output = directory / 'source.csv', directory / 'result.csv'
    source.write_text(source_text, encoding=options.get('encoding', 'utf-8-sig'), newline='')
    before = source.read_bytes()
    if options.pop('existing_output', False):
        output.write_text('preserve existing output')
    existing = output.read_bytes() if output.exists() else None
    prepared, rejected, error = None, False, None
    try:
        request = {'schema_version': 2, 'event_id': name, 'goal': 'fixed manual acceptance task',
                   'capability': 'tabular.deduplicate', 'constraints': ['preserve_original', 'preserve_order'],
                   'execution': {'input_path': str(source), 'output_path': str(output),
                                 'key_columns': keys, **options}}
        plan = ExecutionPlan.from_request(request)
        if kind == 'csv_executor':
            prepared = _prepare_code(plan, content, root.parent, timeout=timeout)
        else:
            prepared = prepare_deduplication(**plan.execution_kwargs())
        report = verify_deduplication(prepared, plan)
        rejected = report['passed'] is not True
        rows = list(csv.reader(io.StringIO(Path(prepared['staging_path']).read_text(encoding=plan.encoding), newline=''),
                               delimiter=plan.delimiter, strict=True))
        correct = expected is not None and rows == expected and report['passed'] is True
    except (ValueError, RuntimeError, OSError, UnicodeError, csv.Error) as exc:
        rejected, correct, error = True, False, str(exc)[:1200]
    finally:
        if prepared is not None:
            cleanup_staging(prepared)
    source_safe = source.read_bytes() == before
    output_safe = (output.read_bytes() if output.exists() else None) == existing
    passed = source_safe and output_safe and (correct if expected is not None else rejected)
    return {'fixture': name, 'split': split, 'passed': passed,
            'source_preserved': source_safe, 'output_preserved': output_safe, 'error': error}


def validate_candidate(proposal_id, db_path, owned_root=None, *, timeout=3.0):
    if type(timeout) not in {int, float} or not 0 < timeout <= 3:
        raise ValueError('candidate validation timeout must stay within the 3-second per-case budget')
    store = EvolutionStore(db_path, owned_root)
    proposal = store.get_proposal(proposal_id)
    if (proposal['component'], proposal['kind']) not in REGISTERED_PAIRS:
        report = {'passed': False, 'reason': 'requires_external_trusted_acceptance_suite',
                  'component': proposal['component'], 'proposal_id': proposal_id}
        with store.connect() as db:
            db.execute("UPDATE evolution_proposals SET state='needs_external_validation' WHERE proposal_id=?",
                       (proposal_id,))
        return report
    report = {'passed': False, 'proposal_id': proposal_id, 'content_hash': proposal['content_hash'],
              'revisions': current_revisions(proposal['kind']),
              'resource_limits': (resource_limits('json.normalize')
                                  if proposal['kind'] in JSON_KINDS else resource_limits()),
              'checks': [], 'train_count': 0, 'heldout_count': 0}
    if proposal['kind'] in JSON_KINDS:
        report.update(kind=proposal['kind'], capability='json.normalize')
    try:
        _proposal_identity(store, proposal)
        content = store.read_artifact(proposal)
        if proposal.get('provenance') is not None:
            from ..assets.lifecycle import verify_proposal_source
            verify_proposal_source(proposal, store)
            report['provenance_sha256'] = digest(proposal['provenance'])
        report['evidence_hashes'] = _evidence(store, proposal)
        if proposal['revisions'] != current_revisions(proposal['kind']):
            raise ValueError('proposal contract/verifier/acceptance revision is stale')
        if proposal['kind'] in {'recipe', 'json_recipe'}:
            _recipe_valid(json.loads(content), proposal['kind'])
        report['checks'] = _replay_acceptance(store, content, proposal['kind'], timeout)
        for check in report['checks']:
            report[check['split'] + '_count'] += 1
        report['passed'] = _complete_certificate(report, proposal['kind'])
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        report['error'] = str(error)[:1200]
    state = 'validated' if report['passed'] else 'quarantine'
    certificate_hash = digest(report)
    version_id = digest({'proposal_id': proposal_id, 'certificate_hash': certificate_hash}) if report['passed'] else None
    with store.connect() as db:
        db.execute('UPDATE evolution_proposals SET state=?,validation_json=?,validation_hash=?,version_id=? WHERE proposal_id=?',
                   (state, canonical_json(report), certificate_hash, version_id, proposal_id))
        if version_id:
            db.execute('INSERT OR IGNORE INTO evolution_versions VALUES(?,?,?,?,?)',
                       (version_id, proposal_id, 'validated', None, time.time()))
    return report


def _replay_acceptance(store, content, kind, timeout=3.0):
    if kind in JSON_KINDS:
        from .json_acceptance import replay_acceptance
        return replay_acceptance(store, content, kind, timeout=timeout)
    directory = store.owned_path(store.owned_root / 'evolution-validation')
    directory.mkdir(exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix='fixtures-', dir=directory) as name:
        for fixture in _FIXTURES:
            check = _fixture_check(content, kind, fixture, Path(name), timeout)
            checks.append(check)
            if check['passed'] is not True:
                break
    return checks


def _fixtures(kind):
    if kind in JSON_KINDS:
        from .json_acceptance import _FIXTURES as json_fixtures
        return json_fixtures
    return _FIXTURES


def _complete_certificate(report, kind='csv_executor'):
    if kind in JSON_KINDS:
        from .json_acceptance import complete_certificate
        return isinstance(report, dict) and report.get('kind') == kind and complete_certificate(report)
    if not isinstance(report, dict):
        return False
    checks = report.get('checks')
    return (isinstance(checks, list) and len(checks) == len(_FIXTURES)
            and all(isinstance(item, dict) for item in checks)
            and [(item.get('fixture'), item.get('split')) for item in checks]
                == [(fixture[0], fixture[1]) for fixture in _FIXTURES]
            and all(item.get('passed') is True and item.get('source_preserved') is True
                    and item.get('output_preserved') is True for item in checks)
            and report.get('train_count') == sum(fixture[1] == 'train' for fixture in _FIXTURES)
            and report.get('heldout_count') == sum(fixture[1] == 'heldout' for fixture in _FIXTURES)
            and report.get('resource_limits') == resource_limits())


def _acceptance_predicates(checks):
    # Traceback paths stay in the hashed certificate, but random sandbox paths
    # do not change the meaning of independently repeated acceptance checks.
    fields = ('fixture', 'split', 'passed', 'source_preserved', 'output_preserved',
              'mode', 'candidate_invoked', 'rejection_observed', 'oracle_passed', 'manual_truth_sha256')
    return [tuple(check.get(field) for field in fields) for check in checks]


def _certificate(store, proposal, *, fresh_evidence=False):
    _proposal_identity(store, proposal)
    report = proposal['validation']
    if ((proposal['component'], proposal['kind']) not in REGISTERED_PAIRS
            or not report or not _complete_certificate(report, proposal['kind'])
            or report.get('passed') is not True or digest(report) != proposal['validation_hash']
            or report.get('content_hash') != proposal['content_hash']
            or report.get('revisions') != current_revisions(proposal['kind'])
            or proposal.get('version_id') != digest({'proposal_id': proposal['proposal_id'],
                                                   'certificate_hash': proposal['validation_hash']})):
        raise ValueError('candidate validation certificate is missing, stale or corrupt')
    if (report.get('provenance_sha256') is not None) != (proposal.get('provenance') is not None):
        raise ValueError('candidate source provenance was removed or added after validation')
    if proposal.get('provenance') is not None:
        from ..assets.lifecycle import verify_proposal_source
        if report.get('provenance_sha256') != digest(proposal['provenance']):
            raise ValueError('candidate source certificate is missing or corrupt')
        verify_proposal_source(proposal, store)
    if fresh_evidence and report['evidence_hashes'] != _evidence(store, proposal):
        raise ValueError('candidate verified evidence changed')
    return store.read_artifact(proposal)


def apply_candidate(proposal_id, db_path, owned_root=None, *, _checkpoint=None):
    store = EvolutionStore(db_path, owned_root)
    proposal = store.get_proposal(proposal_id)
    if proposal['state'] != 'validated' or not proposal['version_id']:
        raise ValueError('candidate must pass independent validation before activation')
    checked_content = _certificate(store, proposal, fresh_evidence=True)
    replay = _replay_acceptance(store, checked_content, proposal['kind'])
    if _acceptance_predicates(replay) != _acceptance_predicates(proposal['validation']['checks']):
        raise ValueError('activation fixture replay differs from validation certificate')
    slot = proposal['component'] + ':' + proposal['kind']
    before = store.pointer(proposal['component'], proposal['kind'])
    if before != (proposal['base_version_id'], proposal['base_generation']):
        raise ValueError('candidate base version is stale; compare-and-swap failed')
    identifier = uuid.uuid4().hex
    directory = store.owned_path(store.owned_root / 'evolution-activation-leases')
    directory.mkdir(exist_ok=True)
    lease_path = directory / identifier
    lease = lease_path.open('a+b')
    fcntl.flock(lease, fcntl.LOCK_EX)
    snapshot = {'version_id': before[0], 'generation': before[1],
                'target_version_id': proposal['version_id'], 'certificate_hash': proposal['validation_hash']}
    try:
        with store.connect() as db:
            db.execute('INSERT INTO evolution_activations VALUES(?,?,?,?,?,?,?)',
                       (identifier, slot, proposal['version_id'], canonical_json(snapshot), 'prepared',
                        str(lease_path), time.time()))
        if _checkpoint:
            _checkpoint('intent_persisted')
        # Recheck after the durable intent: code/certificate changes cannot land between check and switch.
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current_proposal = store._proposal(db.execute('SELECT * FROM evolution_proposals WHERE proposal_id=?',
                                                           (proposal_id,)).fetchone())
            # A single transaction binds the exact certificate metadata to the
            # CAS switch; no second connection can observe a different proposal.
            if (current_proposal['state'] != 'validated'
                    or current_proposal['version_id'] != proposal['version_id']
                    or current_proposal['validation_hash'] != proposal['validation_hash']
                    or current_proposal['validation'] != proposal['validation']
                    or current_proposal['content_hash'] != digest(checked_content)
                    or current_proposal['base_version_id'] != before[0]
                    or current_proposal['base_generation'] != before[1]):
                raise ValueError('candidate certificate metadata changed before atomic activation')
            _certificate(store, current_proposal)
            row = db.execute('SELECT version_id,generation FROM evolution_active WHERE slot=?', (slot,)).fetchone()
            current = (row['version_id'], row['generation']) if row else (None, 0)
            if current != before:
                raise ValueError('candidate base changed; compare-and-swap failed')
            db.execute('INSERT INTO evolution_active VALUES(?,?,?) ON CONFLICT(slot) DO UPDATE '
                       'SET version_id=excluded.version_id,generation=excluded.generation',
                       (slot, proposal['version_id'], before[1] + 1))
            if before[0]:
                db.execute("UPDATE evolution_versions SET state='dormant' WHERE version_id=?", (before[0],))
            db.execute("UPDATE evolution_versions SET state='active' WHERE version_id=?", (proposal['version_id'],))
            db.execute("UPDATE evolution_proposals SET state='applied' WHERE proposal_id=?", (proposal_id,))
            db.execute("UPDATE evolution_activations SET state='applied' WHERE activation_id=?", (identifier,))
        if _checkpoint:
            _checkpoint('activated')
    except BaseException:
        with store.connect() as db:
            db.execute("UPDATE evolution_activations SET state='aborted' WHERE activation_id=? AND state='prepared'", (identifier,))
        raise
    finally:
        lease.close()
        lease_path.unlink(missing_ok=True)
    return {'applied': True, 'activation_id': identifier, 'version_id': proposal['version_id'],
            'previous_version_id': before[0], 'component': proposal['component'], 'kind': proposal['kind'],
            'artifact_path': proposal['artifact_path'], 'content_hash': proposal['content_hash']}


def quarantine_candidate(version_id, db_path, reason):
    store = EvolutionStore(db_path)
    inspection = EvolutionStore.read_only(db_path)
    restored, recovery_reason = None, 'inactive_version_quarantined'
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        version = db.execute('SELECT p.component,p.kind,p.proposal_id FROM evolution_versions v '
                             'JOIN evolution_proposals p ON p.proposal_id=v.proposal_id WHERE v.version_id=?',
                             (version_id,)).fetchone()
        if version is None:
            raise KeyError(version_id)
        slot = version['component'] + ':' + version['kind']
        current = db.execute('SELECT version_id,generation FROM evolution_active WHERE slot=?',
                             (slot,)).fetchone()
        if current is not None and current['version_id'] == version_id:
            recovery_reason = 'restore_registered_builtin'
            activation = db.execute("SELECT snapshot_json FROM evolution_activations WHERE slot=? "
                "AND version_id=? AND state='applied' ORDER BY created_at DESC LIMIT 1",
                (slot, version_id)).fetchone()
            try:
                previous_id = json.loads(activation['snapshot_json'])['version_id'] if activation else None
                if previous_id is not None:
                    previous = inspection.get_version(previous_id)
                    if (previous_id == version_id
                            or (previous['component'], previous['kind']) != (version['component'], version['kind'])
                            or previous['state'] not in {'validated', 'active', 'dormant', 'rolled_back'}):
                        raise ValueError('previous version is quarantined or belongs to another slot')
                    # Read-only feedback avoids maintenance writes while this transaction
                    # holds the CAS lock; the exact original evidence is still re-read.
                    _certificate(inspection, previous, fresh_evidence=True)
                    restored = previous_id
                    recovery_reason = 'restore_previous_validated_version'
            except (ValueError, OSError, RuntimeError, KeyError, TypeError):
                recovery_reason = 'previous_version_invalid_restore_registered_builtin'
            snapshot = {'version_id': version_id, 'generation': current['generation'],
                        'target_version_id': restored, 'reason': recovery_reason}
            db.execute('INSERT INTO evolution_activations VALUES(?,?,?,?,?,?,?)',
                (uuid.uuid4().hex, slot, restored or 'builtin', canonical_json(snapshot),
                 'rolled_back', None, time.time()))
            changed = db.execute('UPDATE evolution_active SET version_id=?,generation=generation+1 '
                'WHERE slot=? AND version_id=? AND generation=?',
                (restored, slot, version_id, current['generation'])).rowcount
            if changed != 1:
                raise ValueError('quarantine base changed; compare-and-swap failed')
            if restored:
                db.execute("UPDATE evolution_versions SET state='active' WHERE version_id=?", (restored,))
        db.execute("UPDATE evolution_versions SET state='quarantine',reason=? WHERE version_id=?", (str(reason)[:1200], version_id))
        db.execute("UPDATE evolution_proposals SET state='quarantine' WHERE proposal_id=?", (version['proposal_id'],))
    return {'quarantined': True, 'version_id': version_id, 'reason': str(reason)[:1200],
            'restored_version_id': restored, 'recovery_reason': recovery_reason}


def prepare_candidate(plan, db_path):
    if (plan.capability, plan.input_format) == ('json.normalize', 'json'):
        executor_kind, recipe_kind = 'json_executor', 'json_recipe'
    elif (plan.capability, plan.input_format) == ('tabular.deduplicate', 'csv'):
        executor_kind, recipe_kind = 'csv_executor', 'recipe'
    else:
        return None
    store = EvolutionStore(db_path)
    version_id = store.pointer('executor', executor_kind)[0]
    recipe_id = store.pointer('recipe', recipe_kind)[0]
    selected_id = version_id or recipe_id
    if selected_id is None:
        return None
    _preflight(plan)
    try:
        selected = store.get_version(selected_id)
        version = selected if version_id else None
        recipe = selected if not version_id else None
        expected_pair = ('executor', executor_kind) if version else ('recipe', recipe_kind)
        if (selected['component'], selected['kind']) != expected_pair or selected['state'] != 'active':
            raise ValueError('production candidate does not belong to its active slot')
        content = _certificate(store, selected)
        if plan.contract_version != selected['validation']['revisions']['contract_version']:
            raise ValueError('production contract revision differs from candidate validation')
        if version:
            prepared = _prepare_code(plan, content, store.owned_root)
            prepared['candidate_version_id'] = version['version_id']
            prepared['candidate_code_sha256'] = version['content_hash']
        else:
            declared = json.loads(content)
            _recipe_valid(declared, recipe_kind)
            actual = plan.verification_parameters()
            fields = ('style', 'constraints') if recipe_kind == 'json_recipe' else (
                'capability', 'input_format', 'key_columns', 'constraints', 'sheet', 'keep', 'encoding', 'delimiter')
            if any(declared.get(key) != actual.get(key) for key in fields
                   if key not in {'capability', 'input_format'}):
                return None
            from ..tasks import prepare_plan
            prepared = prepare_plan(plan)
        if recipe and not version:
            prepared['recipe_version_id'] = recipe['version_id']
            prepared['recipe_content_sha256'] = recipe['content_hash']
        return prepared
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        quarantine_candidate(selected_id, db_path, str(error))
        return None


def rollback(version_id, db_path):
    store = EvolutionStore(db_path)
    inspection = EvolutionStore.read_only(db_path)
    version = store.get_version(version_id)
    slot = version['component'] + ':' + version['kind']
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT version_id,generation FROM evolution_active WHERE slot=?', (slot,)).fetchone()
        if current is None or current['version_id'] != version_id:
            raise ValueError('rollback base is no longer active')
        record = db.execute("SELECT snapshot_json FROM evolution_activations WHERE slot=? "
            "AND version_id=? AND state='applied' ORDER BY created_at DESC LIMIT 1", (slot, version_id)).fetchone()
        if record is None:
            raise ValueError('version has no applied activation snapshot')
        target = json.loads(record['snapshot_json'])['version_id']
        reason = 'restore_previous_validated_version' if target else 'restore_registered_builtin'
        if target:
            try:
                previous = inspection.get_version(target)
                if (target == version_id or (previous['component'], previous['kind']) !=
                        (version['component'], version['kind'])
                        or previous['state'] not in {'validated', 'active', 'dormant', 'rolled_back'}):
                    raise ValueError('previous version is quarantined or belongs to another slot')
                _certificate(inspection, previous, fresh_evidence=True)
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
                # Already under the CAS lock. A nested public quarantine would
                # open a second write transaction and could race another slot.
                invalid = db.execute('SELECT p.proposal_id,p.component,p.kind FROM evolution_versions v '
                    'JOIN evolution_proposals p ON p.proposal_id=v.proposal_id WHERE v.version_id=?',
                    (target,)).fetchone()
                if invalid is not None and (invalid['component'], invalid['kind']) == (version['component'], version['kind']):
                    db.execute("UPDATE evolution_versions SET state='quarantine',reason=? WHERE version_id=?",
                               ('rollback target is invalid: ' + str(error)[:1000], target))
                    db.execute("UPDATE evolution_proposals SET state='quarantine' WHERE proposal_id=?",
                               (invalid['proposal_id'],))
                target = None
                reason = 'previous_version_invalid_restore_registered_builtin'
        snapshot = {'version_id': version_id, 'generation': current['generation'],
                    'target_version_id': target, 'reason': reason}
        db.execute('INSERT INTO evolution_activations VALUES(?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, slot, target or 'builtin', canonical_json(snapshot), 'rolled_back', None, time.time()))
        db.execute('UPDATE evolution_active SET version_id=?,generation=generation+1 WHERE slot=?', (target, slot))
        db.execute("UPDATE evolution_versions SET state='rolled_back' WHERE version_id=?", (version_id,))
        if target:
            db.execute("UPDATE evolution_versions SET state='active' WHERE version_id=?", (target,))
    return {'rolled_back': True, 'version_id': version_id, 'restored_version_id': target, 'reason': reason}
