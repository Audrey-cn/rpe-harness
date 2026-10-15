"""One predeclared public-entry acceptance; never retries a model."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from rpe_harness.experiments.address_comparison import _environment
from rpe_harness.skill_catalog import SkillCatalog, XLSX_SKILL_ID

ROOT = Path(__file__).resolve().parent
OWNED = ROOT.parent
WORK = ROOT / 'workspace'
DB = ROOT / 'state.sqlite'
PROTOCOL = json.loads((ROOT / 'protocol.json').read_text())
HOST = Path('/Users/audrey/.hermes/hermes-agent')


def save(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def cli(operation, *arguments):
    command = [sys.executable, '-B', '-m', 'rpe_harness', 'review', operation,
               *map(str, arguments), '--db', str(DB), '--json']
    completed = subprocess.run(command, capture_output=True, text=True,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(ROOT)},
        timeout=PROTOCOL['timeout_seconds'] + 45)
    result = json.loads(completed.stdout)
    return {**result, 'cli_exit_code': completed.returncode}


def run():
    with (ROOT / 'attempt.claimed').open('x') as stream:
        stream.write(str(time.time()))
    started = time.monotonic()
    catalog = SkillCatalog(DB, ROOT / 'catalog')
    snapshot = catalog.sync('/Users/audrey/.hermes/skills')['snapshot_id']
    catalog.trust_adapter(XLSX_SKILL_ID, snapshot)
    catalog.set_temperature(XLSX_SKILL_ID, 'cold')
    save('source-freeze.json', _environment(sys.executable, HOST))
    draft = cli('draft', '--source', WORK / 'prompt.txt', '--workspace', WORK,
        '--context-sources', WORK / 'sources.json', '--catalog-root', ROOT / 'catalog',
        '--skill-snapshot-id', snapshot, '--owner-root', ROOT / 'sessions',
        '--read-isolation-root', OWNED, '--deny-read-root', ROOT / 'protocol.json',
        '--hermes-root', HOST, '--timeout', PROTOCOL['timeout_seconds'],
        '--max-turns', PROTOCOL['max_turns'])
    save('draft.json', draft)
    result = {'schema_version': 'rpe.v111-postfix-result/v1', 'verified': False,
        'model_sessions': 1, 'human_confirmation_verified': False,
        'reviewer_kind': PROTOCOL['reviewer_kind'], 'formal_scores_unchanged': True,
        'usage': draft.get('host_observation', {}).get('usage'), 'errors': []}
    try:
        gate = draft['proposal_verification']
        assert gate.get('verified') is True and gate.get('proposal_verified') is True, 'native_proposal_unverified'
        assert draft['status'] == 'awaiting_confirmation', 'proposal_not_confirmable'
        assert gate.get('native_discovery_calls') == gate.get('native_selection_calls') == 1, 'selection_missing'
        assert gate.get('native_skill_view_calls') == gate.get('cold_skill_restores') == 1, 'cold_native_load_missing'
        candidate = draft['draft']
        assert candidate['capability'] == 'xlsx.inspect', 'wrong_capability'
        assert candidate['execution'] == {'input_path': 'input.xlsx', 'output_path': 'output.json',
            'mode': 'rows', 'sheet': '复核'}, 'wrong_business_values'
        assert set(candidate['constraints']) <= {'preserve_original'}, 'unsupported_constraints'
        assert candidate['unresolved'] == [], 'unresolved_business_requirement'
        assert draft['context_recovery']['status'] == 'ready', 'context_not_ready'
        assert {'/execution/mode', '/execution/sheet'} <= set(draft['field_provenance']), 'source_provenance_missing'
        assert draft['skill_selection']['skill_id'] == XLSX_SKILL_ID, 'wrong_skill_identity'
        assert draft['skill_selection']['snapshot_id'] == snapshot, 'skill_snapshot_changed'
        assert not (WORK / 'output.json').exists(), 'premature_business_output'
        shown = cli('show', draft['proposal_id'])
        assert shown['proposal_sha256'] == draft['proposal_sha256'], 'review_digest_changed'
        save('show.json', shown)
        confirmation = cli('confirm', draft['proposal_id'], '--accept-digest', draft['proposal_sha256'],
                           '--actor', PROTOCOL['reviewer_kind'])
        assert confirmation['status'] == 'confirmed', 'confirmation_failed'
        save('confirmation.json', confirmation)
        execution = cli('run', confirmation['confirmation_id'], '--workspace', WORK)
        save('execution.json', execution)
        assert execution['status'] == 'succeeded', 'execution_failed'
        assert execution.get('worker_cleanup', {}).get('cleanup_complete') is True, 'worker_cleanup_incomplete'
        from rpe_harness.integrations.hermes_gate import read_run
        durable = read_run(DB, execution['run_id'])
        receipts = [step['evidence']['helper_execution'] for step in durable['steps']
                    if step['stage'] == 'execute' and 'helper_execution' in step.get('evidence', {})]
        assert len(receipts) == 1 and receipts[0]['helper_pid'] > 1, 'actual_helper_process_missing'
        assert receipts[0]['script_sha256'] == draft['skill_selection']['script_sha256'], 'helper_digest_changed'
        save('helper-execution.json', receipts[0])
        artifact = json.loads((WORK / 'output.json').read_text())
        assert json.dumps(artifact, sort_keys=True) == json.dumps(PROTOCOL['expected'], sort_keys=True), 'business_oracle_failed'
        original = hashlib.sha256((WORK / 'input.xlsx').read_bytes()).hexdigest()
        assert original == PROTOCOL['input_sha256'], 'input_changed'
        replay = cli('run', confirmation['confirmation_id'], '--workspace', WORK)
        assert replay == execution, 'durable_replay_changed'
        result.update(verified=True, proposal_id=draft['proposal_id'],
            source_restore_verified=True, cold_skill_restore_verified=True,
            original_helper_artifact_verified=True, deterministic_replay_verified=True,
            input_sha256=original, artifact_sha256=hashlib.sha256((WORK / 'output.json').read_bytes()).hexdigest())
    except (AssertionError, KeyError, ValueError, OSError) as error:
        result['errors'].append(str(error) if isinstance(error, AssertionError) else type(error).__name__)
    result['elapsed_seconds'] = time.monotonic() - started
    save('result.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    run()
