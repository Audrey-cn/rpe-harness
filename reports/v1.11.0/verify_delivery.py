"""Offline delivery checks: no model, subprocess, installation or state writes."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / 'src'))


def read(name):
    return json.loads((ROOT / name).read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify():
    from rpe_harness.experiments.review_v111_comparison import verify_v111_evidence
    formal = verify_v111_evidence(ROOT / 'evidence')
    assert formal['verified'] is True, formal
    manifest = read('evidence-manifest.json')
    for name, digest in manifest['sha256'].items():
        assert sha(ROOT / name) == digest, name
    protocol = read('post-acceptance/protocol.json')
    result = read('post-acceptance/result.json')
    draft = read('post-acceptance/draft.json')
    shown = read('post-acceptance/show.json')
    gate = draft['proposal_verification']
    assert result['verified'] is True and result['model_sessions'] == protocol['max_model_sessions'] == 1
    assert result['formal_scores_unchanged'] is True
    assert result['human_confirmation_verified'] is False
    assert gate['verified'] is True and gate['proposal_verified'] is True
    for key in ('native_discovery_calls', 'native_selection_calls', 'native_skill_view_calls', 'cold_skill_restores'):
        assert gate[key] == 1, key
    assert draft['status'] == 'awaiting_confirmation'
    assert draft['proposal_id'] == result['proposal_id']
    assert shown['proposal_sha256'] == draft['proposal_sha256']
    assert draft['host_observation']['process_cleanup']['cleanup_complete'] is True
    assert draft['context_recovery']['status'] == 'ready'
    assert {'/execution/mode', '/execution/sheet'} <= set(draft['field_provenance'])
    assert draft['draft']['execution'] == {'input_path': 'input.xlsx', 'output_path': 'output.json', 'mode': 'rows', 'sheet': '复核'}
    assert sha(ROOT / 'post-acceptance/input.xlsx') == protocol['input_sha256'] == result['input_sha256']
    assert sha(ROOT / 'post-acceptance/output.json') == result['artifact_sha256']
    assert read('post-acceptance/output.json') == protocol['expected']
    from openpyxl import load_workbook
    book = load_workbook(ROOT / 'post-acceptance/input.xlsx', read_only=True, data_only=False)
    try:
        rows = [list(row) for row in book[protocol['expected']['sheet']].iter_rows(values_only=True)]
        assert rows == protocol['expected']['rows']
    finally:
        book.close()
    execution = read('post-acceptance/execution.json')
    assert execution['status'] == 'succeeded' and execution['verification_ok'] == 1 and execution['verification']['passed'] is True
    assert execution['worker_cleanup']['cleanup_complete'] is True
    assert execution == read('post-acceptance/replay.json')
    assert read('post-acceptance/durable-replay.json')['run_count'] == 1
    helper = read('post-acceptance/helper-execution.json')
    assert helper['helper_pid'] > 1
    assert helper['script_sha256'] == draft['skill_selection']['script_sha256'] == gate['selected_script_sha256']
    assert helper['network'] == helper['fork'] == 'denied'
    sources = read('post-acceptance/source-freeze.json')['source_sha256']
    for name, digest in sources.items():
        assert sha(REPO / 'src/rpe_harness' / name) == digest, name
    wheel_proof = read('validation/final-wheel-evidence.json')
    wheel = REPO / 'dist' / wheel_proof['wheel_name']
    wheel_checked = wheel.is_file()
    if wheel_checked:
        assert sha(wheel) == wheel_proof['wheel_sha256']
        with zipfile.ZipFile(wheel) as archive:
            for name, digest in sources.items():
                assert hashlib.sha256(archive.read('rpe_harness/' + name)).hexdigest() == digest
    costs = read('all-observed-costs.json')
    before = read('evidence/summary.json')['costs']
    assert costs['known_total_tokens'] == before['known_total_tokens'] + result['usage']['known_total_tokens'] == 276431
    assert costs['known_logical_api_requests'] == before['api_requests'] + result['usage']['api_requests'] == 36
    assert costs['missing_usage_observations'] == 4 and costs['total_tokens'] is None
    coverage = read('validation/test-coverage.json')
    assert coverage['remaining_source_failures'] == coverage['remaining_package_failures'] == 0
    return {'verified': True, 'formal_evidence': formal, 'postfix_business_artifact_verified': True,
            'current_source_modules_verified': len(sources), 'wheel_bytes_verified': wheel_checked,
            'source_testcases_verified': coverage['current_cases_verified'],
            'installed_testcases_verified': coverage['distinct_installed_cases_verified'],
            'human_confirmation_verified': False,
            'runtime_proof': 'recorded_observations_not_independent_attestation',
            'comparison_ready': read('evidence/summary.json')['comparison_ready']}


if __name__ == '__main__':
    print(json.dumps(verify(), ensure_ascii=False, sort_keys=True, indent=2))
