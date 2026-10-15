"""Offline recorded-evidence checks; no provider, subprocess or state writes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
REPO = next(parent for parent in ROOT.parents if (parent / 'src/rpe_harness').is_dir())
sys.path.insert(0, str(REPO / 'src'))


def need(condition, message):
    if not condition:
        raise ValueError(message)


def read(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_record(name, passed, skipped=0, subtests=0):
    root = ET.parse(ROOT / 'validation' / name).getroot()
    cases = list(root.iter('testcase'))
    failures = sum(case.find('failure') is not None or case.find('error') is not None for case in cases)
    skip_count = sum(case.find('skipped') is not None for case in cases)
    suites = [root] if root.tag == 'testsuite' else list(root.iter('testsuite'))
    extra = sum(int(suite.get('tests', '0')) for suite in suites) - len(cases)
    need((len(cases) - failures - skip_count, skip_count, failures, extra) ==
         (passed, skipped, 0, subtests), 'test record differs: ' + name)
    need(all(int(suite.get(key, '0')) == 0 for suite in suites for key in ('failures', 'errors')),
         'suite failures or errors: ' + name)
    return {'passed': passed, 'skipped': skipped, 'subtests_passed': subtests}


def verify(require_cleanup=False):
    from rpe_harness.experiments.review_v112_comparison import verify_v112_evidence
    from rpe_harness.experiments.agent_audit import _SYSTEM, _json, _verdict
    evidence = {}
    for name in ('evidence', 'pilot-original'):
        evidence[name] = verify_v112_evidence(ROOT / name)
        need(evidence[name]['verified'] is True, name + ': ' + str(evidence[name].get('errors')))
    manifest = read('evidence-manifest.json')['sha256']
    need(isinstance(manifest, dict) and bool(manifest), 'empty evidence manifest')
    for name, digest in manifest.items():
        path = (ROOT / name).resolve()
        need(path.is_relative_to(ROOT) and sha(path) == digest, 'manifest mismatch: ' + name)
    sources = read('evidence/formal-freeze.json')['environment']['source_sha256']
    current = {str(path.relative_to(REPO / 'src/rpe_harness')): sha(path)
               for path in (REPO / 'src/rpe_harness').rglob('*.py')}
    need(len(sources) == 85 and current == sources, 'current source differs from 85-module formal freeze')
    proof = read('validation/final-wheel-proof.json')
    need(proof['verified'] is True and proof.get('modules', proof.get('source_modules')) == sources, 'wheel proof source mismatch')
    wheel = REPO / 'dist' / proof.get('wheel', proof.get('wheel_name', ''))
    wheel_checked = wheel.is_file()
    need(wheel_checked, 'delivery wheel missing')
    if wheel_checked:
        need(sha(wheel) == proof.get('sha256', proof.get('wheel_sha256')), 'wheel SHA mismatch')
        with zipfile.ZipFile(wheel) as archive:
            modules = {name.removeprefix('rpe_harness/'): hashlib.sha256(archive.read(name)).hexdigest()
                       for name in archive.namelist() if name.startswith('rpe_harness/') and name.endswith('.py')}
        need(modules == sources, 'wheel module bytes differ from formal freeze')
    bundle, audit = read('agent-audit/bundle.json'), read('agent-audit/result.json')
    ids = [case['evidence_id'] for case in bundle['cases']]
    need(1 <= len(ids) <= 32 and len(set(ids)) == len(ids), 'invalid audit evidence IDs')
    input_sha = hashlib.sha256(_json({'audit_contract': _SYSTEM, 'bundle': bundle})).hexdigest()
    need(audit['input_sha256'] == input_sha, 'audit input SHA mismatch')
    claim = ROOT / 'agent-audit/claim.json'
    if claim.exists():
        need(read('agent-audit/claim.json')['input_sha256'] == input_sha, 'audit claim input SHA mismatch')
    completed = audit['status'] == 'completed'
    if completed:
        need(_verdict(audit['verdict'], set(ids)) is not None, 'completed audit verdict or evidence IDs differ')
    costs = read('all-observed-costs.json')
    summary = read('evidence/summary.json')
    need(costs['hermes_all_including_failed_and_pilot'] == summary['costs'] and
         costs['hermes_formal'] == summary['formal_costs'] and
         costs['hermes_pilot'] == summary['pilot_costs'] and
         costs['independent_agent'] == audit, 'retained cost components differ')
    need(costs['whole_run_total_tokens'] is None and costs['whole_run_verified_bill_usd'] is None,
         'unknown all-run tokens or billing replaced')
    limits = costs['independent_agent_limit_observations']
    need(limits['num_turns'] == audit['num_turns'] and
         limits['output_tokens'] == audit['usage']['output_tokens'] and
         limits['requested_turn_limit_adhered'] == (audit['num_turns'] <= read('agent-audit/claim.json')['max_turns']),
         'audit observed limits differ')
    records = {'source_full_suite': test_record('full-suite.xml', 1774, 6, 3),
               'post_pilot': test_record('post-pilot.xml', 71, 1),
               'final_package': test_record('final-package.xml', 100)}
    if require_cleanup:
        cleanup = read('cleanup.json')
        preservation = read('validation/preservation-after-cleanup.json')
        need(preservation['preservation_ok'] is True and preservation['owned_processes']['count'] == 0
             and preservation['leaked_test_session_ID_count'] == 0, 'post-cleanup preservation failed')
        for key in ('owned_root', 'worktree', 'cli_probe'):
            path = Path(cleanup['owned_root' if key == 'owned_root' else key + '_path'])
            need(path.is_absolute() and cleanup[key + '_removed'] is True and
                 not path.exists() and not path.is_symlink(), 'cleanup incomplete: ' + key)
    return {'verified': True, 'evidence': evidence, 'manifest_files_verified': len(manifest),
            'current_source_modules_verified': len(sources), 'wheel_bytes_verified': wheel_checked,
            'test_records': records, 'independent_audit_completed': completed,
            'independent_audit_status': audit['status'], 'independent_agent_limit_observations': limits,
            'cleanup_required': require_cleanup,
            'human_confirmation_verified': False,
            'runtime_proof': 'recorded_observations_not_independent_thirdparty_runtime_attestation',
            'comparison_ready': read('evidence/summary.json')['comparison_ready']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-cleanup', action='store_true')
    args = parser.parse_args()
    try:
        result = verify(args.require_cleanup)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, ET.ParseError, zipfile.BadZipFile) as error:
        result = {'verified': False, 'error': str(error), 'error_type': type(error).__name__}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    raise SystemExit(0 if result['verified'] else 1)
