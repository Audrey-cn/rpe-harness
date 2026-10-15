"""Offline recorded-evidence checks; no provider, subprocess or state writes.

Run only after the retained 1.13 evidence has been assembled. Public checks
recompute recorded facts; they do not reconstruct cleaned private sessions.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
REPO = next((parent for parent in ROOT.parents if (parent / 'src/rpe_harness').is_dir()), None)
if REPO is not None:
    sys.path.insert(0, str(REPO / 'src'))
SHA = re.compile(r'[a-f0-9]{64}\Z')


def need(condition, message):
    if not condition:
        raise ValueError(message)


def local(name):
    need(isinstance(name, str) and name and not Path(name).is_absolute(), 'invalid report path')
    path = (ROOT / name).resolve()
    need(path.is_relative_to(ROOT.resolve()), 'report path escapes delivery: ' + name)
    return path


def read(name):
    return json.loads(local(name).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_manifest(manifest, required):
    need(type(manifest) is dict and bool(manifest), 'empty evidence manifest')
    need(set(required) <= set(manifest), 'required files absent from evidence manifest')
    for name, digest in manifest.items():
        need(isinstance(digest, str) and SHA.fullmatch(digest), 'invalid manifest SHA: ' + str(name))
        need(sha(local(name)) == digest, 'manifest mismatch: ' + name)
    return len(manifest)


def count(value):
    return type(value) is int and value >= 0


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def test_record(name, passed, skipped=0, subtests=0):
    need(all(count(value) for value in (passed, skipped, subtests)), 'invalid expected XML counts')
    root = ET.parse(local('validation/' + name)).getroot()
    need(root.tag in ('testsuite', 'testsuites'), 'unsupported test XML root: ' + name)
    cases = list(root.iter('testcase'))
    failures = sum(case.find('failure') is not None or case.find('error') is not None for case in cases)
    skip_count = sum(case.find('skipped') is not None for case in cases)
    suites = [suite for suite in root.iter('testsuite') if not suite.findall('testsuite')]
    need(bool(suites), 'test suites missing: ' + name)
    extra = sum(int(suite.get('tests', '0')) for suite in suites) - len(cases)
    need((len(cases) - failures - skip_count, skip_count, failures, extra) ==
         (passed, skipped, 0, subtests), 'test record differs: ' + name)
    need(all(int(suite.get(key, '0')) == 0 for suite in root.iter('testsuite')
             for key in ('failures', 'errors')), 'suite failures or errors: ' + name)
    return {'passed': passed, 'skipped': skipped, 'subtests_passed': subtests}


def audit_limits(audit, claim):
    usage = audit.get('usage')
    output = usage.get('output_tokens') if type(usage) is dict else None
    output = output if count(output) else None
    turns, elapsed, cost = audit.get('num_turns'), audit.get('elapsed_seconds'), audit.get('cli_estimated_cost_usd')
    return {'completed_sessions': int(audit.get('status') == 'completed'),
        'num_turns': turns if count(turns) else None, 'output_tokens': output,
        'requested_turn_limit_adhered': turns <= claim['max_turns'] if count(turns) else None,
        'aggregate_output_above_requested_figure': output > claim['max_output_tokens'] if output is not None else None,
        'time_limit_adhered': elapsed <= claim['timeout_seconds'] if number(elapsed) else None,
        'cli_estimated_dollar_limit_adhered': cost <= claim['cli_max_budget_usd'] if number(cost) else None,
        'provider_api_requests': None, 'per_request_output_limit_enforcement': None}


def summarize_batches(initial, supplement):
    """Namespace copied aggregate IDs; preserve every original phase and cost."""
    from rpe_harness.experiments.cost_ledger import summarize_phases
    return summarize_phases({**phase, 'attempt_id': prefix + phase['attempt_id']}
        for prefix, phases in (('initial.', initial), ('supplement.', supplement))
        for phase in phases)


def check_audit_terminal(audit, terminal):
    """Retain this delivery's sole failed CLI result without manufacturing a verdict."""
    need(audit['status'] == 'failed' and audit['error_code'] == 'cli_failed' and
         audit['exit_code'] == 1 and audit['verdict'] is None and
         type(terminal) is dict and terminal.get('type') == 'result' and
         terminal.get('is_error') is True and terminal.get('subtype') == 'error_max_turns' and
         terminal.get('stop_reason') == 'max_tokens' and count(terminal.get('num_turns')) and
         terminal['num_turns'] == audit['num_turns'] and
         type(terminal.get('errors')) is list and bool(terminal['errors']) and
         all(isinstance(error, str) and error for error in terminal['errors']),
         'sole audit failed terminal observation differs')
    return terminal


def check_population(plan, summary, phases):
    """Each retained batch must contain its original complete finite population."""
    cases, sessions = plan['cases'], plan['sessions']
    need(plan['profile'] == 'review-v113' and len(cases) == 4 and len(sessions) == 8 and
         plan['pilots'] == [] and plan['pilot_case'] is None,
         '1.13 requires four formal pairs and no pilot population')
    need(plan['max_draft_sessions'] == 8 and plan['max_native_execution_sessions'] == 3 and
         plan['max_task_attempts'] == 1, 'frozen 8/3 single-attempt budgets differ')
    need(sum(case['category'] == 'production' for case in cases) == 3 and
         sum(case['category'] == 'contract_probe' for case in cases) == 1, 'production/probe population differs')
    need(summary['formal_attempts'] == 8 and summary['pilot_attempts'] == 0 and
         len(summary['pairs']) == 4 and len(summary['records']) == 8, 'formal trial accounting incomplete')
    trials = {trial['trial_id']: trial for trial in sessions}
    need(len(trials) == 8 and set(trials) == {row['trial_id'] for row in summary['records']} and
         all(trial['phase'] == 'formal' for trial in sessions), 'formal records differ from registered trials')
    drafts = [phase for phase in phases if phase['phase'] == 'draft']
    native_runs = [phase for phase in phases if phase['phase'] == 'execute' and phase['arm'] == 'native']
    need(len(drafts) == 8 and len({phase['attempt_id'] for phase in drafts}) == 8 and
         all(not phase['attempt_id'].startswith('pilot-') for phase in phases), 'draft count or pilot phases differ')
    need(len(native_runs) == summary['native_execution_sessions'] <= 3 and
         len({phase['attempt_id'] for phase in native_runs}) == len(native_runs), 'native execution budget differs')
    case_index = {case['case_id']: case for case in cases}
    need(all(case_index[trials[phase['attempt_id'].removesuffix('-execute')]['case_id']]['category'] == 'production'
             for phase in phases if phase['phase'] == 'execute'), 'probe execution recorded')
    return native_runs


def verify(require_cleanup=False):
    need(REPO is not None, 'delivery must reside in the repository containing src/rpe_harness')
    from rpe_harness.experiments.review_v113_comparison import verify_v113_evidence
    from rpe_harness.experiments.agent_audit import _SYSTEM, _SCHEMA, _json, _verdict

    evidence = {name: verify_v113_evidence(local(name)) for name in ('evidence-initial', 'evidence')}
    for name, checked in evidence.items():
        need(checked['verified'] is True, name + ': ' + str(checked.get('errors')))
    need(not (ROOT / 'pilot-original').exists(), 'unexpected pilot archive')
    plan, summary = read('evidence/preregistration.json'), read('evidence/summary.json')
    phases = read('evidence/cost-phases.json')['records']
    initial_plan, initial_summary = read('evidence-initial/preregistration.json'), read('evidence-initial/summary.json')
    initial_phases = read('evidence-initial/cost-phases.json')['records']
    native_runs = check_population(plan, summary, phases)
    initial_runs = check_population(initial_plan, initial_summary, initial_phases)
    initial_sources = read('evidence-initial/formal-freeze.json')['environment']['source_sha256']
    amendment = read('budget-amendment.json')
    need(amendment['schema_version'] == 'rpe.v113-budget-amendment/v1' and
         isinstance(amendment['reason'], str) and amendment['reason'].strip() and
         amendment['initial_registration_sha256'] == initial_plan['controller_registration_sha256'] and
         amendment['final_registration_sha256'] == plan['controller_registration_sha256'],
         'budget amendment registration or reason differs')
    need(amendment['max_draft_sessions'] == 16 and amendment['max_native_execution_sessions'] == 6 and
         amendment['max_audit_cli_processes'] == 1 and amendment['initial_batch_preserved'] is True and
         amendment['original_budget_exceeded'] is True and
         amendment['comparison_kind'] == 'same_case_supplement_not_new_sample' and
         amendment['allowed_changed_modules'] == ['integrations/hermes_bootstrap.py'],
         'supplement budget or declared scope differs')
    final_drafts = [phase for phase in phases if phase['phase'] == 'draft']
    need(number(amendment['declared_at']) and
         amendment['declared_at'] <= min(phase['reserved_at'] for phase in final_drafts),
         'supplement budget was not declared before final model phases')
    # Container metadata may change when an XLSX fixture is rebuilt. Task text,
    # source facts, parameters and declared business rows must remain identical;
    # each batch's actual input SHA is independently checked by its verifier.
    business_cases = lambda document: [{key: value for key, value in case.items()
        if key not in {'source_path', 'input_sha256'}} for case in document['cases']]
    need(_json(business_cases(initial_plan)) == _json(business_cases(plan)) and
         all(initial_plan[field] == plan[field] for field in
             ('public_contract_sha256', 'execution_public_contract_sha256', 'skill_sha256', 'script_sha256')),
         'same-case supplement changed task, source facts, business truth or public contracts')
    need(len(initial_runs) + len(native_runs) <= amendment['max_native_execution_sessions'],
         'all-batch native execution budget exceeded')

    executed_sources = read('evidence/formal-freeze.json')['environment']['source_sha256']
    need(set(initial_sources) == set(executed_sources) and sorted(name for name in executed_sources
         if executed_sources[name] != initial_sources[name]) == amendment['allowed_changed_modules'],
         'supplement changed production source beyond bootstrap entry guard')
    delivery = read('delivery-source-freeze.json')
    need(delivery['schema_version'] == 'rpe.v113-delivery-source-freeze/v1' and
         delivery['executed_registration_sha256'] == plan['controller_registration_sha256'] and
         delivery['executed_source_commit'] == 'ad0561a33e778081005f0e192f0a3b2ec0a9d840' and
         isinstance(delivery['source_commit'], str) and
         re.fullmatch('[a-f0-9]{40}', delivery['source_commit']) and
         delivery['model_validation_of_final_entry_patch'] is False and
         delivery['allowed_changed_modules'] == ['integrations/hermes_bootstrap.py'],
         'delivery source or explicit post-model validation boundary differs')
    model_phases = [phase for phase in phases if phase.get('api_expected') is True and
                    phase['phase'] in ('draft', 'execute')]
    need(model_phases and all(number(phase.get('finished_at')) for phase in model_phases) and
         number(delivery['frozen_at']) and
         delivery['frozen_at'] >= max(phase['finished_at'] for phase in model_phases),
         'delivery engineering freeze was not recorded after final model phases')
    sources = delivery['source_sha256']
    need(type(sources) is dict and set(sources) == set(executed_sources) and
         sorted(name for name in sources if sources[name] != executed_sources[name]) ==
         delivery['allowed_changed_modules'], 'post-model delivery changed source beyond bootstrap entry')
    current = {str(path.relative_to(REPO / 'src/rpe_harness')): sha(path)
               for path in (REPO / 'src/rpe_harness').rglob('*.py')}
    need(bool(sources) and current == sources, 'current source differs from delivery freeze')
    proof = read('validation/final-wheel-proof.json')
    outside_checkout = proof.get('installed_outside_checkout') is True
    if isinstance(proof.get('installed_import_path'), str):
        installed = Path(proof['installed_import_path'])
        outside_checkout = (installed.is_absolute() and
            not installed.resolve().is_relative_to(REPO.resolve()) and
            proof.get('source_editable') is False and
            proof.get('source_wheel_installed_modules_equal') is True)
    need(proof['verified'] is True and proof['version'] == '1.13.0' and
         outside_checkout and
         proof.get('modules', proof.get('source_modules')) == sources and
         proof.get('modules_verified', len(sources)) == len(sources), 'wheel proof source or version mismatch')
    wheel_name = proof.get('wheel', proof.get('wheel_name'))
    need(isinstance(wheel_name, str) and Path(wheel_name).name == wheel_name, 'invalid wheel filename')
    wheel = REPO / 'dist' / wheel_name
    need(wheel.is_file() and sha(wheel) == proof.get('sha256', proof.get('wheel_sha256')), 'delivery wheel missing or SHA mismatch')
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.startswith('rpe_harness/') and name.endswith('.py')]
        need(len(names) == len(set(names)), 'duplicate wheel modules')
        modules = {name.removeprefix('rpe_harness/'): hashlib.sha256(archive.read(name)).hexdigest() for name in names}
        need(modules == sources, 'wheel module bytes differ from delivery freeze')

    bundle, claim, audit = read('agent-audit/bundle.json'), read('agent-audit/claim.json'), read('agent-audit/result.json')
    ids = [case['evidence_id'] for case in bundle['cases']]
    need(1 <= len(ids) <= 32 and all(isinstance(value, str) and value for value in ids) and
         len(set(ids)) == len(ids), 'invalid audit evidence IDs')
    input_sha = hashlib.sha256(_json({'audit_contract': _SYSTEM, 'bundle': bundle})).hexdigest()
    need(audit['input_sha256'] == claim['input_sha256'] == input_sha and
         claim['schema_sha256'] == hashlib.sha256(_json(_SCHEMA)).hexdigest(), 'audit input or schema SHA mismatch')
    budget = plan['independent_audit_budget']
    need(budget['cli_processes'] == 1 and budget['provider_api_limit_verified'] is False and
         claim['max_turns'] == budget['requested_max_turns'] == 1 and
         claim['max_output_tokens'] == budget['requested_max_output_tokens'] == 1200 and
         claim['timeout_seconds'] == budget['timeout'] == 180 and
         claim['cli_max_budget_usd'] == budget['cli_estimated_usd'] == .25,
         'independent audit requested limits differ')
    need(audit['host'] == claim['host'] == 'claude-code' and
         audit['requested_model'] == claim['requested_model'] and audit['verified_bill_usd'] is None,
         'audit route identity or unverified billing differs')
    need(audit['status'] in ('completed', 'failed', 'timed_out', 'invalid_output'), 'unknown audit status')
    completed = audit['status'] == 'completed'
    if completed:
        need(audit['exit_code'] == 0 and audit['error_code'] is None and
             _verdict(audit['verdict'], set(ids)) is not None, 'completed audit verdict or evidence IDs differ')
    else:
        need(audit['verdict'] is None, 'failed audit published a successful verdict')
    terminal = check_audit_terminal(audit, read('agent-audit/terminal-observation.json'))
    costs = read('all-observed-costs.json')
    for field, component in (('hermes_all_including_failed_and_pilot', 'costs'),
                             ('hermes_formal', 'formal_costs'), ('hermes_pilot', 'pilot_costs'),
                             ('hermes_shared', 'shared_costs')):
        need(costs[field] == summary[component], 'retained cost component differs: ' + field)
    need(costs['hermes_initial_failed_batch'] == initial_summary['costs'] and
         costs['hermes_all_batches'] == summarize_batches(initial_phases, phases),
         'initial failure costs or combined observed costs differ')
    need(costs['independent_agent'] == audit and costs['independent_agent_requested_limits'] == claim,
         'retained audit cost or requested limits differ')
    need(costs['whole_run_total_tokens'] is None and costs['whole_run_verified_bill_usd'] is None and
         costs['known_components_only'] is True, 'unknown all-run totals replaced with claimed totals')
    limits = costs['independent_agent_limit_observations']
    for key, expected in audit_limits(audit, claim).items():
        need(key in limits and _json(limits[key]) == _json(expected), 'audit observed limit differs: ' + key)

    declared = read('validation/test-records.json')['records']
    need(type(declared) is dict and {'source_full_suite', 'final_package', 'final_keyed_native_entry'} <= set(declared),
         'final XML declarations missing')
    records, required = {}, {'verify_delivery.py', 'evidence/preregistration.json', 'evidence/formal-freeze.json',
        'evidence/summary.json', 'evidence/cost-phases.json', 'validation/final-wheel-proof.json',
        'validation/test-records.json', 'agent-audit/bundle.json', 'agent-audit/claim.json',
        'agent-audit/result.json', 'agent-audit/terminal-observation.json', 'all-observed-costs.json',
        'budget-amendment.json', 'delivery-source-freeze.json',
        'evidence-initial/preregistration.json', 'evidence-initial/formal-freeze.json',
        'evidence-initial/summary.json', 'evidence-initial/cost-phases.json'}
    for label, record in declared.items():
        need(isinstance(label, str) and type(record) is dict and isinstance(record.get('file'), str), 'invalid XML declaration')
        records[label] = test_record(record['file'], record['passed'], record.get('skipped', 0), record.get('subtests_passed', 0))
        need(record['passed'] > 0, 'empty final verification: ' + label)
        if label == 'final_keyed_native_entry':
            need(record.get('skipped', 0) == 0, 'final keyed native entry was skipped')
        required.add('validation/' + record['file'])
    if require_cleanup:
        cleanup, preservation = read('cleanup.json'), read('validation/preservation-after-cleanup.json')
        baseline_name = 'validation/preservation-baseline.json'
        baseline = read(baseline_name)
        need(baseline['schema_version'] == 'rpe.post-cleanup-preservation-baseline/v1' and
             preservation['schema_version'] == 'rpe.post-cleanup-preservation/v1',
             'cleanup preservation schema differs')
        deleted, markers = baseline['deleted_paths'], baseline['owned_process_path_markers']
        need(type(deleted) is list and bool(deleted) and len(set(deleted)) == len(deleted) and
             type(markers) is list and bool(markers) and
             all(isinstance(name, str) and Path(name).is_absolute() and '..' not in Path(name).parts
                 for name in deleted + markers), 'invalid cleanup baseline paths')
        need(preservation['preservation_ok'] is True and preservation['owned_processes']['count'] == 0 and
             preservation['leaked_test_session_ID_count'] == 0 and
             preservation['baseline_sha256'] == sha(local(baseline_name)), 'post-cleanup preservation failed')
        removed = preservation['deleted_paths']
        need(removed['checked_count'] == len(deleted) and removed['all_absent'] is True and
             removed['still_present'] == [], 'post-cleanup deleted-path accounting differs')
        need(all(not Path(name).exists() and not Path(name).is_symlink() for name in deleted),
             'a baseline-owned deleted path still exists')
        for key, field in (('owned_root', 'owned_root'), ('worktree', 'worktree_path')):
            path = Path(cleanup[field])
            need(path.is_absolute() and cleanup[key + '_removed'] is True and
                 str(path) in deleted and any(path.is_relative_to(Path(marker)) for marker in markers) and
                 not path.exists() and not path.is_symlink(), 'cleanup incomplete or outside baseline: ' + key)
        required.update({'cleanup.json', 'validation/preservation-after-cleanup.json', baseline_name,
                         'validation/post-cleanup-preservation.py'})
    manifest_count = check_manifest(read('evidence-manifest.json')['sha256'], required)
    return {'verified': True, 'evidence': evidence, 'manifest_files_verified': manifest_count,
        'current_source_modules_verified': len(sources), 'wheel_bytes_verified': True,
        'executed_source_modules_recorded': len(executed_sources),
        'executed_source_commit': delivery['executed_source_commit'],
        'delivery_source_commit': delivery['source_commit'],
        'executed_to_delivery_changed_modules': delivery['allowed_changed_modules'],
        'model_validation_of_final_entry_patch': False,
        'formal_pairs': 4, 'formal_attempts': 8, 'pilot_attempts': 0,
        'native_execution_sessions': len(native_runs), 'all_batches_formal_attempts': 16,
        'all_batches_native_execution_sessions': len(initial_runs) + len(native_runs),
        'same_case_supplement': True, 'original_8_3_budget_met': False,
        'fixture_bytes_identical_between_batches': all(left['input_sha256'] == right['input_sha256']
            for left, right in zip(initial_plan['cases'], plan['cases'])), 'test_records': records,
        'independent_audit_completed': completed, 'independent_audit_status': audit['status'],
        'independent_audit_terminal_subtype': terminal['subtype'],
        'independent_audit_stop_reason': terminal['stop_reason'],
        'independent_agent_limit_observations': limits, 'cleanup_required': require_cleanup,
        'human_confirmation_verified': False,
        'runtime_proof': 'recorded_observations_not_independent_thirdparty_runtime_attestation',
        'comparison_ready': summary['comparison_ready']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-cleanup', action='store_true')
    args = parser.parse_args()
    try:
        result = verify(args.require_cleanup)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, UnicodeError, ImportError,
            ET.ParseError, zipfile.BadZipFile) as error:
        result = {'verified': False, 'error': str(error), 'error_type': type(error).__name__}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    raise SystemExit(0 if result['verified'] else 1)
