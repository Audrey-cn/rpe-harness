"""The proposal worker uses real feedback and persists code without approving it."""
import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from rpe_harness.evolution import EvolutionStore
from test_evolution import verified_run
from test_json_cycle import json_run


def module():
    return importlib.import_module('rpe_harness.evolution.worker')


def session_result(**changes):
    return {'completed': True, 'exit_code': 0, 'timed_out': False,
            'session_id': 'real-boundary-session', 'usage': None,
            'model_identity': {'provider': 'local-test-provider', 'requested_model': 'unchanged-model',
                               'response_models': ['reported-model']},
            'elapsed_seconds': 1.25, 'events': [],
            'final_response': 'PRIVATE_PROSE_MUST_NOT_BE_SAVED',
            'process_cleanup': {'cleanup_complete': True, 'remaining_descendants': [],
                                'external_executor_cleanup_complete': True,
                                'external_executor_remaining_children': [], 'errors': []}, **changes}


def test_no_fresh_feedback_skips_model_and_keeps_failure_report(tmp_path, monkeypatch):
    worker = module()
    def forbidden(*args, **kwargs):
        pytest.fail('model called without verified feedback')
    monkeypatch.setattr(worker, 'run_hermes_session', forbidden)
    report = worker.run_proposal_worker(tmp_path / 'runs.sqlite', tmp_path)
    assert report['proposal_id'] is None
    assert report['error']['code'] == 'no_verified_feedback'
    assert report['usage'] is None
    assert Path(report['report_path']).is_file()
    assert not list(tmp_path.glob('proposal-worker-*'))


def test_csv_worker_does_not_treat_verified_xlsx_runs_as_csv_evidence(tmp_path, monkeypatch):
    from openpyxl import Workbook
    from rpe_harness.runtime import HarnessRuntime
    book = Workbook()
    sheet = book.active
    sheet.title = 'Orders'
    sheet.append(['id', 'value'])
    sheet.append(['1', 'first'])
    sheet.append(['1', 'later'])
    source = tmp_path / 'orders.xlsx'
    book.save(source)
    database = tmp_path / 'runs.sqlite'
    run = HarnessRuntime(db_path=database).run({'schema_version': 1, 'event_id': 'xlsx',
        'state': {'task_type': 'data.processing', 'goal': 'deduplicate', 'input_format': 'xlsx',
                  'required_capability': 'tabular.deduplicate',
                  'constraints': ['preserve_original', 'preserve_order']},
        'execution': {'input_path': str(source), 'output_path': str(tmp_path / 'output.xlsx'),
                      'key_columns': ['id'], 'sheet': 'Orders'}})
    assert run['status'] == 'succeeded'
    worker = module()
    def forbidden(*args, **kwargs):
        pytest.fail('CSV proposer received XLSX evidence')
    monkeypatch.setattr(worker, 'run_hermes_session', forbidden)
    report = worker.run_proposal_worker(database, tmp_path, kind='csv_executor')
    assert report['error']['code'] == 'no_verified_feedback'
    assert report['proposal_id'] is None


@pytest.mark.parametrize('kind', ['json_executor', 'csv_executor'])
def test_generation_persists_immutable_proposal_and_cleans_owned_home(tmp_path, monkeypatch, kind):
    worker = module()
    database, seed = json_run(tmp_path) if kind == 'json_executor' else verified_run(tmp_path)
    directories = []
    code = 'def prepare(plan, staging_path):\n    raise ValueError("untrusted generation")\n'
    def generate(prompt, workspace, host_db, **options):
        workspace, owner = Path(workspace), Path(options['owner_root'])
        directories.append(owner)
        assert workspace.is_relative_to(owner) and owner.is_relative_to(tmp_path)
        assert options['tool_enabled'] is False and options['entry_mode'] == 'optional'
        assert Path(host_db).is_relative_to(owner) and Path(host_db) != database
        assert str(tmp_path / 'orders.csv') not in prompt
        assert str(tmp_path / 'seed.json') not in prompt
        assert 'heldout' not in prompt and 'fixture_suite' not in prompt
        (owner / 'isolated-home').mkdir()
        (owner / 'isolated-home' / 'credential-copy').write_text('PRIVATE_AUTH')
        (workspace / 'candidate.py').write_text(code)
        return session_result()
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path, kind=kind)
    assert report['completed'] is True and report['workspace_cleaned'] is True
    assert report['session_id'] == 'real-boundary-session'
    assert report['usage'] is None
    assert report['model_identity']['requested_model'] == 'unchanged-model'
    proposal = EvolutionStore(database).get_proposal(report['proposal_id'])
    assert proposal['state'] == 'proposed' and proposal['validation'] is None
    assert proposal['component'] == 'executor' and proposal['kind'] == kind
    assert proposal['evidence_run_ids'] == [seed['run_id']]
    assert Path(proposal['artifact_path']).read_text() == code
    assert report['candidate_sha256'] == proposal['content_hash']
    assert EvolutionStore(database).get_active('executor', kind) is None
    assert not any(path.exists() for path in directories)
    saved = Path(report['report_path']).read_text()
    assert 'PRIVATE_PROSE' not in saved and 'PRIVATE_AUTH' not in saved


@pytest.mark.parametrize('failure', ['timeout', 'model_error', 'cleanup', 'cleanup_missing',
                                    'missing', 'symlink', 'oversize'])
def test_failed_generation_never_proposes_and_keeps_unknown_cost(tmp_path, monkeypatch, failure):
    worker = module()
    database, _ = json_run(tmp_path)
    def generate(prompt, workspace, host_db, **options):
        candidate = Path(workspace) / 'candidate.py'
        candidate.write_text('def prepare(plan, staging_path): pass\n')
        result = session_result()
        if failure == 'timeout':
            result.update(completed=False, timed_out=True, exit_code=-9)
        elif failure == 'model_error':
            result.update(completed=False, exit_code=1,
                events=[{'kind': 'api_request_error', 'error_type': 'ServiceUnavailable',
                         'status_code': 503, 'message': 'PRIVATE_ERROR_TEXT'}])
        elif failure == 'cleanup':
            result['process_cleanup']['cleanup_complete'] = False
        elif failure == 'cleanup_missing':
            result['process_cleanup'] = {'cleanup_complete': True}
        elif failure == 'missing':
            candidate.unlink()
        elif failure == 'symlink':
            candidate.unlink()
            candidate.symlink_to(tmp_path / 'seed.json')
        else:
            candidate.write_bytes(b'x' * 65537)
        return result
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path)
    assert report['completed'] is False and report['proposal_id'] is None
    assert report['usage'] is None and report['workspace_cleaned'] is True
    assert report['error']
    assert EvolutionStore(database).list_proposals() == []
    assert not list(tmp_path.glob('proposal-worker-*'))
    assert 'PRIVATE_ERROR_TEXT' not in Path(report['report_path']).read_text()


def test_source_changed_during_model_generation_cannot_back_a_proposal(tmp_path, monkeypatch):
    worker = module()
    database, _ = json_run(tmp_path)
    def generate(prompt, workspace, host_db, **options):
        (Path(workspace) / 'candidate.py').write_text('def prepare(plan, staging_path): pass\n')
        (tmp_path / 'seed-output.json').write_bytes(b'corrupt')
        return session_result()
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path)
    assert report['error']['code'] == 'verified_feedback_changed'
    assert report['proposal_id'] is None


def test_launcher_exception_cleans_partial_home_and_saves_only_error_hash(tmp_path, monkeypatch):
    worker = module()
    database, _ = json_run(tmp_path)
    def generate(prompt, workspace, host_db, **options):
        (Path(options['owner_root']) / 'partial-credential').write_text('PRIVATE_SECRET')
        raise RuntimeError('PRIVATE_EXCEPTION_WITH_CREDENTIAL')
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path)
    assert report['error']['code'] == 'launcher_failed'
    assert report['error']['type'] == 'RuntimeError'
    assert report['workspace_cleaned'] is True and report['usage'] is None
    assert not list(tmp_path.glob('proposal-worker-*'))
    assert 'PRIVATE_' not in Path(report['report_path']).read_text()


def test_worker_public_feedback_is_bounded_and_contains_no_plan_paths(tmp_path, monkeypatch):
    worker = module()
    database, _ = json_run(tmp_path)
    for index in range(9):
        json_run(tmp_path, str(index))
    def generate(prompt, workspace, host_db, **options):
        packet = json.loads((Path(workspace) / 'feedback.json').read_text())
        assert len(packet['verified_feedback']) == 8
        encoded = json.dumps(packet['verified_feedback'])
        assert 'input_path' not in encoded and 'output_path' not in encoded
        assert str(tmp_path) not in encoded
        assert all(item['verified'] is True and item['output_sha256'] for item in packet['verified_feedback'])
        (Path(workspace) / 'candidate.py').write_text('def prepare(plan, staging_path): pass\n')
        return session_result(usage={'coverage_complete': False, 'known_total_tokens': 27,
                                    'total_tokens': None, 'cache_read_tokens': None})
    monkeypatch.setattr(worker, 'run_hermes_session', generate)
    report = worker.run_proposal_worker(database, tmp_path)
    assert len(report['evidence_run_ids']) == 8
    assert report['usage']['known_total_tokens'] == 27
    assert report['usage']['total_tokens'] is None


@pytest.mark.parametrize('options', [{'kind': 'core'}, {'kind': 'recipe'}, {'timeout': True},
    {'timeout': 0}, {'timeout': 181}, {'timeout': 1.5}])
def test_invalid_worker_scope_is_rejected_before_db_or_workspace_creation(tmp_path, options):
    with pytest.raises(ValueError):
        module().run_proposal_worker(tmp_path / 'runs.sqlite', tmp_path, **options)
    assert not (tmp_path / 'runs.sqlite').exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(), reason='macOS real sandbox boundary')
def test_real_worker_sandbox_denies_feedback_and_heldout_while_public_workspace_is_usable(tmp_path, monkeypatch):
    from rpe_harness.integrations import hermes, hermes_session as launcher
    worker = module()
    owned = tmp_path / 'worker-owned'
    owned.mkdir()
    database, _ = json_run(owned)
    secret = owned / 'private-marker.txt'
    secret.write_text('private feedback marker')
    baseline = owned / 'baseline-copy'
    baseline.mkdir()
    heldout = baseline / 'heldout.py'
    heldout.write_text('private oracle marker')
    other_repo = tmp_path / 'separate-project'
    other_repo.mkdir()
    project_marker = other_repo / 'private-source.py'
    project_marker.write_text('private project marker')
    import_root = tmp_path / 'bootstrap-runtime' / 'lib' / 'python' / 'site-packages'
    package = import_root / 'rpe_harness'
    integrations = package / 'integrations'
    integrations.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (integrations / '__init__.py').write_text('')
    shutil.copyfile(hermes.__file__, integrations / 'hermes.py')
    program = '''import json,os,sys
from pathlib import Path
sys.path.insert(0,IMPORT_ROOT)
from rpe_harness.integrations.hermes import HostEventStore
reads=[]
for path in PRIVATE_PATHS:
    try:
        Path(path).read_bytes()
        reads.append(False)
    except PermissionError:
        reads.append(True)
workspace=Path(os.environ['RPE_HERMES_WORKSPACE'])
packet=json.loads((workspace/'feedback.json').read_text())
assert packet['verified_feedback'][0]['verified'] is True
(workspace/'candidate.py').write_text('def prepare(plan, staging_path): raise ValueError("probe only")\\n')
HostEventStore(os.environ['RPE_HERMES_DB']).record('on_session_end',{'session_id':'worker-sandbox-probe','completed':True})
print(json.dumps({'type':'result','session_id':'worker-sandbox-probe','text':json.dumps({'denied':reads,'public_read':True,'candidate_written':True})}))
'''
    private_paths = [str(secret), str(database), str(heldout), str(project_marker)]
    bootstrap = integrations / 'hermes_bootstrap.py'
    bootstrap.write_text('IMPORT_ROOT=' + repr(str(import_root)) + '\nPRIVATE_PATHS=' + repr(private_paths) + '\n' + program)
    monkeypatch.setattr(launcher, '__file__', str(integrations / 'hermes_session.py'))
    monkeypatch.setattr(launcher, '_installed_python', lambda root: Path(sys.executable).resolve())
    monkeypatch.setattr(launcher, '_read_host_settings', lambda *args: {
        'provider': 'xiaomi', 'model': 'no-model-probe', 'base_url': 'https://example.invalid',
        'api_key': 'isolated-no-model-test-key'})
    source_roots = getattr(worker, '_deny_read_roots', lambda: [])()
    monkeypatch.setattr(worker, '_deny_read_roots', lambda: [*source_roots, other_repo], raising=False)
    sessions = []
    def real_launch(*args, **kwargs):
        result = launcher.run_hermes_session(*args, **kwargs)
        sessions.append(result)
        return result
    monkeypatch.setattr(worker, 'run_hermes_session', real_launch)
    report = worker.run_proposal_worker(database, owned, hermes_root=tmp_path / 'fake-hermes', timeout=5)
    assert report['completed'] is True, report
    result = sessions[0]
    probe = json.loads(result['final_response'])
    assert probe['denied'] == [True, True, True, True]
    assert probe['public_read'] is True and probe['candidate_written'] is True
    assert result['usage']['api_requests'] == 0
    assert result['process_cleanup']['cleanup_complete'] is True
    assert not Path(result['session_root']).exists()
    assert report['workspace_cleaned'] is True
