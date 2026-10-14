"""JSON candidates need their own real parser, literal truths and OS boundary."""
import copy
import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from rpe_harness.evolution.store import EvolutionStore


def module():
    assert importlib.util.find_spec('rpe_harness.evolution.json_acceptance') is not None, (
        'JSON candidate acceptance must be implemented independently')
    return importlib.import_module('rpe_harness.evolution.json_acceptance')


def example():
    path = Path(__file__).resolve().parents[1] / 'examples/json-candidate/candidate.py'
    assert path.is_file(), 'a standalone JSON candidate is required'
    return path.read_bytes()


def scope(root):
    return EvolutionStore(root / 'state.sqlite', root)


def direct_plan(root, raw=b'{"x":1}', style='minify'):
    from rpe_harness.json_contract import JsonNormalizePlan, EXECUTOR_REVISION, VERIFIER_REVISION
    source = root / 'source.json'
    source.write_bytes(raw)
    return JsonNormalizePlan(capability='json.normalize', contract_version=1,
        executor_id='builtin/json-normalize', executor_revision=EXECUTOR_REVISION,
        verifier_revision=VERIFIER_REVISION, input_path=str(source),
        output_path=str(root / 'result.json'), input_format='json',
        input_sha256=hashlib.sha256(raw).hexdigest(), style=style,
        constraints=('preserve_order', 'preserve_original'), goal='manual candidate acceptance')


def test_literal_fixture_suite_covers_styles_root_types_and_strict_rejections():
    acceptance = module()
    cases = acceptance._FIXTURES
    assert 35 <= len(cases) <= 50
    assert {case[1] for case in cases} == {'train', 'heldout'}
    assert {case[3] for case in cases} == {'pretty', 'minify'}
    assert all(type(case[2]) is bytes for case in cases)
    assert all(type(case[4]) is bytes for case in cases if case[5] == 'valid')
    names = {case[0] for case in cases}
    assert {'number_lexemes', 'escaped_strings', 'duplicate_decoded_key',
            'invalid_utf8', 'unpaired_surrogate', 'number_over_budget',
            'tokens_over_budget', 'depth_over_budget', 'input_over_budget'} <= names
    from rpe_harness.json_verification import inspect_json_bytes
    at_limit = next(case for case in cases if case[0] == 'tokens_at_budget')
    assert inspect_json_bytes(at_limit[2])['token_count'] == 100000


@pytest.mark.parametrize('kind', ['json_executor', 'json_recipe'])
def test_full_real_suite_and_certificate_report_execution_truth(tmp_path, kind):
    acceptance = module()
    content = example() if kind == 'json_executor' else (
        b'{"capability":"json.normalize","input_format":"json","style":"pretty",'
        b'"constraints":["preserve_order","preserve_original"],'
        b'"procedure":["plan","prepare","independent_verify","exclusive_publish"]}')
    checks = acceptance.replay_acceptance(scope(tmp_path), content, kind)
    assert len(checks) == len(acceptance._FIXTURES)
    assert all(check['passed'] is True for check in checks), checks
    from rpe_harness.evolution.sandbox import resource_limits
    report = {'kind': kind, 'capability': 'json.normalize', 'checks': checks,
              'train_count': sum(case[1] == 'train' for case in acceptance._FIXTURES),
              'heldout_count': sum(case[1] == 'heldout' for case in acceptance._FIXTURES),
              'resource_limits': resource_limits('json.normalize')}
    assert acceptance.complete_certificate(report) is True
    invalid = next(check for check in checks if check['fixture'] == 'duplicate_decoded_key')
    assert invalid['rejection_observed'] is True
    assert invalid['candidate_invoked'] is (kind == 'json_executor')
    byte_reject = next(check for check in checks if check['fixture'] == 'input_over_budget')
    assert byte_reject['candidate_invoked'] is False
    assert byte_reject['mode'] == 'admission_reject'
    valid = next(check for check in checks if check['mode'] == 'valid')
    assert valid['oracle_passed'] is True
    for field in ('mode', 'candidate_invoked', 'rejection_observed', 'oracle_passed',
                  'manual_truth_sha256'):
        forged = copy.deepcopy(report)
        forged['checks'][0][field] = not valid[field] if type(valid[field]) is bool else 'forged'
        assert acceptance.complete_certificate(forged) is False
    forged = copy.deepcopy(report)
    forged['resource_limits']['open_files'] = 64.0
    assert acceptance.complete_certificate(forged) is False
    forged = copy.deepcopy(report)
    forged['kind'] = []
    assert acceptance.complete_certificate(forged) is False
    forged = copy.deepcopy(report)
    reject = next(check for check in forged['checks'] if check['mode'] == 'candidate_reject')
    reject.pop('manual_truth_sha256')
    assert acceptance.complete_certificate(forged) is False
    assert list((tmp_path / 'evolution-validation').iterdir()) == []
    sandboxes = tmp_path / 'evolution-sandboxes'
    assert not sandboxes.exists() or list(sandboxes.iterdir()) == []


def test_rejecting_everything_does_not_produce_a_valid_certificate(tmp_path):
    acceptance = module()
    checks = acceptance.replay_acceptance(scope(tmp_path),
        b'def prepare(plan, staging_path):\n    raise ValueError("reject all")\n', 'json_executor')
    assert checks[0]['mode'] == 'valid'
    assert checks[0]['candidate_invoked'] is True
    assert checks[0]['rejection_observed'] is True
    assert checks[0]['passed'] is False


def test_oracle_rejecting_an_invalid_input_output_is_not_candidate_parser_rejection(tmp_path):
    acceptance = module()
    fixture = next(case for case in acceptance._FIXTURES if case[0] == 'duplicate_decoded_key')
    check = acceptance._fixture_check(
        b'def prepare(plan, staging_path):\n    open(staging_path,"wb").write(b"null\\n")\n',
        'json_executor', fixture, tmp_path, 3.0)
    assert check['candidate_invoked'] is True
    assert check['rejection_observed'] is False
    assert check['oracle_passed'] is False
    assert check['passed'] is False


@pytest.mark.parametrize('code', [
    b'def prepare(plan, staging_path):\n    missing_name\n',
    b'def prepare(plan, staging_path):\n    raise RuntimeError("broken environment")\n',
    b'import time\ndef prepare(plan, staging_path):\n    time.sleep(10)\n',
])
def test_unknown_errors_and_timeout_cannot_masquerade_as_parser_rejections(tmp_path, code):
    acceptance = module()
    fixture = next(case for case in acceptance._FIXTURES if case[0] == 'duplicate_decoded_key')
    check = acceptance._fixture_check(code, 'json_executor', fixture, tmp_path, 0.25)
    assert check['passed'] is False
    assert check['rejection_observed'] is False


@pytest.mark.parametrize('exception', ['ValueError', 'UnicodeError'])
def test_only_explicit_parser_rejections_have_typed_sandbox_outcomes(tmp_path, exception):
    from rpe_harness.evolution import sandbox
    assert hasattr(sandbox, 'CandidateRejected'), 'explicit parser rejection needs its own type'
    plan = direct_plan(tmp_path, b'{bad')
    with pytest.raises(sandbox.CandidateRejected):
        sandbox.run_candidate(
            f'def prepare(plan, staging_path):\n    raise {exception}("invalid source")\n'.encode(),
            plan.to_dict(), tmp_path)
    assert list((tmp_path / 'evolution-sandboxes').iterdir()) == []


def test_writing_a_result_before_rejection_is_not_an_atomic_parser_rejection(tmp_path):
    from rpe_harness.evolution import sandbox
    assert hasattr(sandbox, 'CandidateRejected')
    plan = direct_plan(tmp_path, b'{bad')
    with pytest.raises(RuntimeError) as raised:
        sandbox.run_candidate(b'def prepare(plan, staging_path):\n'
            b'    open(staging_path,"wb").write(b"null\\n")\n'
            b'    raise ValueError("invalid")\n', plan.to_dict(), tmp_path)
    assert not isinstance(raised.value, sandbox.CandidateRejected)


def test_json_and_csv_read_budgets_are_enforced_during_source_read(tmp_path, monkeypatch):
    from rpe_harness.evolution import sandbox
    from rpe_harness import path_binding
    assert sandbox.resource_limits('json.normalize')['input_bytes'] == 1024 * 1024
    assert sandbox.resource_limits()['input_bytes'] == 16 * 1024 * 1024
    calls, real = [], path_binding.read_source
    def observed(path, *, max_bytes=None):
        calls.append(max_bytes)
        return real(path, max_bytes=max_bytes)
    monkeypatch.setattr(path_binding, 'read_source', observed)
    plan = direct_plan(tmp_path, b'"' + b'x' * (1024 * 1024) + b'"')
    with pytest.raises(ValueError, match='budget|size|MiB|bound'):
        sandbox.run_candidate(b'def prepare(plan, staging_path): pass', plan.to_dict(), tmp_path)
    assert calls == [1024 * 1024]
    assert not (tmp_path / 'evolution-sandboxes').exists()


def test_missing_platform_is_a_real_failure_before_any_false_rejection(tmp_path, monkeypatch):
    acceptance = module()
    from rpe_harness.evolution import sandbox
    monkeypatch.setattr(sandbox.sys, 'platform', 'linux')
    checks = acceptance.replay_acceptance(scope(tmp_path), example(), 'json_executor')
    assert checks[0]['passed'] is False
    assert checks[0]['candidate_invoked'] is False
    assert checks[0]['rejection_observed'] is False
    assert 'unavailable' in checks[0]['error']


@pytest.mark.parametrize('change', [
    {'capability': 'tabular.deduplicate'}, {'input_format': 'csv'}, {'style': 'auto'},
    {'constraints': ['weaken_oracle']}, {'procedure': ['prepare', 'publish']},
    {'remote_fetch': True}, {'style': []}, {'constraints': ''},
])
def test_json_recipe_cannot_change_trusted_procedure_or_cross_contract(change):
    acceptance = module()
    recipe = {'capability': 'json.normalize', 'input_format': 'json', 'style': 'pretty',
              'constraints': ['preserve_order', 'preserve_original'],
              'procedure': ['plan', 'prepare', 'independent_verify', 'exclusive_publish']}
    recipe.update(change)
    with pytest.raises(ValueError):
        acceptance.recipe_valid(recipe)


def test_revision_scope_is_json_specific_and_example_cannot_import_rpe():
    acceptance = module()
    revisions = acceptance.current_revisions()
    assert revisions['capability'] == 'json.normalize'
    assert revisions['contract_version'] == 1
    assert revisions['fixture_suite_sha256']
    assert revisions['acceptance_revision']
    assert revisions['lifecycle_revision']
    assert b'rpe_harness' not in example()


def test_json_certificate_names_loaded_authorities_even_if_files_change_before_json_import(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'src' / 'rpe_harness'
    copied = tmp_path / 'modules' / 'rpe_harness'
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns('__pycache__'))
    program = '''import hashlib,sys
from pathlib import Path
from rpe_harness.evolution import candidates,sandbox
from rpe_harness import json_contract
assert 'rpe_harness.evolution.json_acceptance' not in sys.modules
loaded={}
for name,module in [('lifecycle_revision',candidates),('contract_revision',json_contract),
                    ('sandbox_revision',sandbox)]:
    path=Path(module.__file__)
    raw=path.read_bytes()
    loaded[name]=hashlib.sha256(raw).hexdigest()
    path.write_bytes(raw+b'\\n# changed only after authority was already loaded\\n')
    assert loaded[name]!=hashlib.sha256(path.read_bytes()).hexdigest()
from rpe_harness.evolution import json_acceptance
revisions=json_acceptance.current_revisions()
assert all(revisions[name]==value for name,value in loaded.items())
'''
    environment = dict(os.environ, PYTHONPATH=str(copied.parent), PYTHONDONTWRITEBYTECODE='1')
    child = subprocess.run([sys.executable, '-B', '-c', program], cwd=tmp_path,
                           env=environment, capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
