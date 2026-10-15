#!/usr/bin/env python3
"""Offline record consistency; no replay, provider access or reconstruction of deleted keys."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.dont_write_bytecode = True
REPORT = Path(__file__).resolve().parent
WORKTREE = REPORT.parents[1]
sys.path.insert(0, str(WORKTREE/'src'))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    from rpe_harness.experiments.review_v115_comparison import verify_v115_evidence
    from rpe_harness.experiments.address_comparison import _read
    from rpe_harness.experiments.agent_audit import _verdict
    errors = []
    proof = json.loads((REPORT/'validation/wheel-proof.json').read_bytes())
    frozen = _read(REPORT/'formal-freeze.json')
    if proof['source_modules'] != frozen['environment']['source_sha256']:
        errors.append('model_source_differs_from_wheel_source')
    source = WORKTREE/'src/rpe_harness'
    if {str(p.relative_to(source)): sha(p.read_bytes()) for p in source.rglob('*.py')} != proof['source_modules']:
        errors.append('current_source_differs_from_model')
    wheel = REPORT/'release/rpe_harness-1.15.0-py3-none-any.whl'
    if sha(wheel.read_bytes()) != proof['wheel_sha256']:
        errors.append('wheel_hash_changed')
    with zipfile.ZipFile(wheel) as archive:
        if any(sha(archive.read('rpe_harness/'+name)) != digest for name, digest in proof['source_modules'].items()):
            errors.append('wheel_modules_changed')
    portable = verify_v115_evidence(REPORT/'evidence')
    if portable.get('verified') is not True:
        errors.append('portable_evidence_inconsistent')
    cli = json.loads((REPORT/'codex-audit-result.json').read_bytes())
    bundle = json.loads((REPORT/'codex-audit-bundle.json').read_bytes())
    if cli['status'] == 'completed':
        _verdict(cli['verdict'], {case['evidence_id'] for case in bundle['cases']}, profile='structured-v2')
        if cli['exit_code'] != 0 or cli['observed_tool_item_count'] != 0 or cli['num_turns'] != 1:
            errors.append('CLI_success_claim_differs')
    manifest = json.loads((REPORT/'manifest.json').read_bytes())
    for name, digest in manifest['files'].items():
        if sha((REPORT/name).read_bytes()) != digest:
            errors.append('retained_file_changed:'+name)
    cleanup = json.loads((REPORT/'cleanup.json').read_bytes())
    preservation = cleanup.get('preservation', {})
    if cleanup.get('owned_removed') is not True or not preservation.get('owned_cleanup_and_test_isolation_verified'):
        errors.append('cleanup_or_test_isolation_incomplete')
    result = {'verified': not errors, 'errors': errors, 'source_modules': len(proof['source_modules']),
        'portable_records': portable, 'CLI_status': cli['status'], 'model_replayed': False,
        'deleted_receipt_keys_reauthenticated': False, 'verified_billing': None}
    result['strict_daily_preservation'] = preservation.get('preserved')
    result['daily_skill_drift_observed'] = preservation.get('daily_skill_drift_observed')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
