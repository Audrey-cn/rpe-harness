import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from rpe_harness.evolution import EvolutionStore, cycle
from rpe_harness.runtime import HarnessRuntime
from rpe_harness.storage import RunStore


def snapshot(database):
    with RunStore.read_only(database).connect() as db:
        return '\n'.join(db.iterdump())


def json_run(root, name='seed', style='minify'):
    source = root / (name + '.json')
    source.write_bytes(b' { "id": 1.2300, "text": "\\u4e2d" } ')
    result = HarnessRuntime(db_path=root / 'runs.sqlite').run({
        'schema_version': 2, 'event_id': name, 'goal': 'fixed literal JSON truth',
        'capability': 'json.normalize', 'execution': {'input_path': str(source),
        'output_path': str(root / (name + '-output.json')), 'style': style}})
    assert result['status'] == 'succeeded'
    return root / 'runs.sqlite', result


def test_default_json_recipe_and_dormant_experience_are_real_and_origin_is_explicit(tmp_path):
    database, seed = json_run(tmp_path)
    result = cycle(database, tmp_path)
    store = EvolutionStore(database)
    active = store.get_active('recipe', 'json_recipe')
    assert active is not None and active['validation']['passed'] is True
    assert any(item['origin'] == 'default_verified_recipe' for item in result['proposal_origins'])
    experience = next(item for item in store.list_experiences()
                      if item['lookup_key'].startswith('json.normalize:json:'))
    assert seed['run_id'] in experience['evidence_run_ids']
    store.set_experience_state(experience['experience_id'], 'dormant')
    assert EvolutionStore(database).lookup_experiences(experience['lookup_key'])[0]['state'] == 'dormant'


def test_json_recipe_keeps_same_pointer_for_new_evidence_and_second_style(tmp_path):
    database, _ = json_run(tmp_path)
    cycle(database, tmp_path)
    before = EvolutionStore(database).pointer('recipe', 'json_recipe')
    json_run(tmp_path, 'fresh-same-style')
    json_run(tmp_path, 'fresh-pretty', 'pretty')
    first = cycle(database, tmp_path)
    second = cycle(database, tmp_path)
    assert EvolutionStore(database).pointer('recipe', 'json_recipe') == before
    assert not [item for item in first['applied'] + second['applied'] if item['kind'] == 'json_recipe']
    assert len([item for item in EvolutionStore(database).list_experiences()
                if item['lookup_key'].startswith('json.normalize:json:')]) == 2


def test_successful_fallback_is_still_a_structured_failure_diagnosis(tmp_path):
    database, seed = json_run(tmp_path)
    runtime = HarnessRuntime(db_path=database)
    fact = {'plan_sha256': seed['artifact']['plan_sha256'],
            'actual_executor': {'kind': 'candidate', 'candidate_version_id': 'owned-bad-version'},
            'verification': {'passed': False, 'checks': {'tokens_preserved': False}},
            'quarantine': {'quarantined': True, 'version_id': 'owned-bad-version'}}
    runtime.store.step(seed['run_id'], 'candidate_fallback', fact)
    cycle(database, tmp_path, auto_apply=False)
    proposal = next(item for item in EvolutionStore(database).list_proposals()
                    if item['component'] == 'executor' and item['kind'] == 'diagnostic')
    content = json.loads(open(proposal['artifact_path']).read())
    problem = next(item for item in content['problems'] if item.get('kind') == 'candidate_fallback')
    assert problem['candidate_version_id'] == 'owned-bad-version'
    assert problem['run_id'] == seed['run_id'] and problem['verification_passed'] is False
    assert problem['evidence_sha256']


@pytest.mark.parametrize('field,value', [('max_proposals', 0), ('max_proposals', 33),
    ('max_proposals', True), ('max_history_runs', 0), ('max_history_runs', 101),
    ('max_history_runs', True)])
def test_cycle_budget_cannot_be_expanded_or_disabled(tmp_path, field, value):
    database, _ = json_run(tmp_path)
    before = snapshot(database)
    with pytest.raises(ValueError, match='budget'):
        cycle(database, tmp_path, **{field: value})
    assert snapshot(database) == before


def test_history_over_budget_refuses_before_any_maintenance_write(tmp_path):
    database, _ = json_run(tmp_path)
    json_run(tmp_path, 'second')
    before = snapshot(database)
    with pytest.raises(ValueError, match='history.*budget'):
        cycle(database, tmp_path, max_history_runs=1)
    assert snapshot(database) == before


def test_generated_proposal_over_budget_refuses_before_maintenance_write(tmp_path):
    database, _ = json_run(tmp_path)
    before = snapshot(database)
    with pytest.raises(ValueError, match='proposal.*budget'):
        cycle(database, tmp_path, max_proposals=1)
    assert snapshot(database) == before


def test_external_proposer_gets_verified_feedback_not_fixture_answers_and_is_labeled(tmp_path):
    database, seed = json_run(tmp_path)
    def proposer(context):
        assert set(context) == {'verified_feedback'}
        assert context['verified_feedback'][0]['run_id'] == seed['run_id']
        return [{'component': 'core', 'kind': 'source_patch', 'content': 'untrusted patch',
                 'evidence_run_ids': [seed['run_id']]}]
    report = cycle(database, tmp_path, auto_apply=False, proposer=proposer)
    assert any(item['origin'] == 'external_injected_code' for item in report['proposal_origins'])
    assert EvolutionStore(database).get_active('core', 'source_patch') is None


def test_external_proposal_count_overflow_is_rejected_before_writes(tmp_path):
    database, seed = json_run(tmp_path)
    before = snapshot(database)
    def proposer(context):
        return [{'component': 'core', 'kind': 'source_patch', 'content': str(index),
                 'evidence_run_ids': [seed['run_id']]} for index in range(33)]
    with pytest.raises(ValueError, match='proposal.*budget'):
        cycle(database, tmp_path, proposer=proposer)
    assert snapshot(database) == before


def test_cli_persisted_code_is_consumed_and_identical_new_proposal_is_not_reactivated(tmp_path):
    database, seed = json_run(tmp_path)
    candidate = Path(__file__).resolve().parents[1] / 'examples/json-candidate/candidate.py'
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(tmp_path))
    def cli(*arguments):
        result = subprocess.run([sys.executable, '-B', '-m', 'rpe_harness', 'evolution',
            *arguments, '--db', str(database), '--owned-root', str(tmp_path)], env=environment,
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)
    def propose(run_id):
        return cli('propose', '--component', 'executor', '--kind', 'json_executor',
                   '--candidate', str(candidate), '--evidence-run-id', run_id)
    first = propose(seed['run_id'])
    applied = cli('cycle')
    assert any(item['proposal_id'] == first['proposal_id'] and item['origin'] == 'external_persisted_proposal'
               for item in applied['proposal_origins'])
    store = EvolutionStore(database)
    before = store.pointer('executor', 'json_executor')
    _, later = json_run(tmp_path, 'fresh-evidence')
    redundant = propose(later['run_id'])
    repeated = cli('cycle')
    assert store.pointer('executor', 'json_executor') == before
    assert not [item for item in repeated['applied'] if item['kind'] == 'json_executor']
    assert store.get_proposal(redundant['proposal_id'])['state'] == 'superseded'
