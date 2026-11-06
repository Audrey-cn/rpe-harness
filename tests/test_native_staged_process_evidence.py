"""Same-call staged stdout evidence from real, provider-free subprocesses."""
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from rpe_harness.integrations import hermes_native_process as native


@pytest.fixture
def staged_case(tmp_path):
    workspace, skill = tmp_path/'workspace', tmp_path/'skills/xlsx'
    workspace.mkdir()
    (skill/'scripts').mkdir(parents=True)
    helper, source = skill/'scripts/xlsx_read.py', skill/'SKILL.md'
    helper.write_text('import sys,time\ntime.sleep(.15)\nprint("real helper output")\n'
                      'raise SystemExit(7 if "fail" in sys.argv else 0)\n')
    source.write_text('fixed original skill')
    (workspace/'input.xlsx').write_text('fixed original input')
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    config = {'workspace': str(workspace), 'helper_path': str(helper), 'skill_path': str(source),
        'helper_python': sys.executable, 'helper_python_sha256': digest(Path(sys.executable).resolve()),
        'skill_id': 'productivity/xlsx', 'snapshot_id': 'snapshot',
        'skill_sha256': digest(source), 'script_sha256': digest(helper),
        'input_name': 'input.xlsx', 'input_sha256': digest(workspace/'input.xlsx'),
        'output_name': 'output.json', 'stdout_name': 'helper-stdout.json',
        'parameters': {'mode': 'rows', 'sheet': '盘点'}}
    argv = [sys.executable, '-I', '-B', str(helper), 'input.xlsx', '--json', '--sheet', '盘点']
    return config, argv


def invoke(config, command):
    from rpe_harness.integrations.hermes_gate import digest_json
    args = {'command': command}
    before = {'session_id': 'staged-unit', 'task_id': 'task', 'turn_id': 'turn',
        'api_request_id': 'api', 'tool_call_id': 'execute', 'tool_name': 'terminal',
        'args': args, 'arguments_sha256': digest_json(args)}
    observer = native.NativeExecutionObserver(config)
    observer.before(before)
    child = subprocess.run(['/bin/bash', '-c', command], cwd=config['workspace'],
        env={key: value for key, value in os.environ.items() if not key.startswith(('PYTHON', 'DYLD_', 'LD_'))},
        capture_output=True, text=True, timeout=5)
    row, = observer.after({**before, 'status': 'succeeded' if child.returncode == 0 else 'failed',
        'tool_exit_code': child.returncode})
    return child, row


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
@pytest.mark.parametrize('copy', ['cp helper-stdout.json output.json',
    shlex.join([sys.executable, '-I', '-c',
        'import shutil;shutil.copyfile("helper-stdout.json","output.json")'])])
def test_staged_stdout_accepts_same_call_copy_without_parsing_copy_command(staged_case, copy, monkeypatch):
    config, argv = staged_case
    monkeypatch.setattr(native, '_SIGNING_KEY', b's'*32)
    child, row = invoke(config, shlex.join(argv)+' > helper-stdout.json; '+copy)
    assert child.returncode == 0
    assert row['verified'] is True, row
    assert row['kernel_exit_observed'] is True and row['process_returncode'] == 0
    assert row['artifact_binding'] == 'same_call_final_bytes_equal_observed_stdout_not_exclusive_writer'
    expected = hashlib.sha256(b'real helper output\n').hexdigest()
    assert row['stdout_sha256'] == row['output_sha256'] == expected
    assert row['stdout_size'] == row['output_size'] == 19
    workspace = Path(config['workspace'])
    assert row['stdout_path_sha256'] == hashlib.sha256(str(workspace/'helper-stdout.json').encode()).hexdigest()
    assert row['output_path_sha256'] == hashlib.sha256(str(workspace/'output.json').encode()).hexdigest()
    assert row['stdout_identity_sha256'] != row['output_identity_sha256']
    signature = row.pop('process_receipt_hmac_sha256')
    assert signature == native.receipt_signature({key: value for key, value in row.items() if key != 'kind'}, b's'*32)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
@pytest.mark.parametrize('fault', ['existing_stage', 'existing_final', 'wrong_stdout', 'stage_symlink',
    'final_symlink', 'final_hardlink', 'missing_copy', 'changed_final', 'deleted_stage', 'renamed_stage',
    'changed_stage_after_exit', 'replaced_stage_after_exit', 'failed_helper'])
def test_staged_stdout_rejects_missing_or_unsafe_source_to_final_binding(staged_case, fault):
    config, argv = staged_case
    workspace = Path(config['workspace'])
    stage, final = workspace/'helper-stdout.json', workspace/'output.json'
    stdout, suffix = 'helper-stdout.json', '; cp helper-stdout.json output.json'
    if fault == 'existing_stage':
        stage.write_text('old')
    elif fault == 'existing_final':
        final.write_text('old')
    elif fault == 'wrong_stdout':
        stdout = 'other.json'; suffix = '; cp other.json output.json'
    elif fault == 'stage_symlink':
        (workspace/'other.json').write_text(''); stage.symlink_to(workspace/'other.json')
    elif fault == 'final_symlink':
        (workspace/'other.json').write_text(''); final.symlink_to(workspace/'other.json')
    elif fault == 'final_hardlink':
        suffix = '; ln helper-stdout.json output.json'
    elif fault == 'missing_copy':
        suffix = ''
    elif fault == 'changed_final':
        suffix += '; printf wrong > output.json'
    elif fault == 'deleted_stage':
        suffix += '; rm helper-stdout.json'
    elif fault == 'renamed_stage':
        suffix = '; mv helper-stdout.json output.json'
    elif fault == 'changed_stage_after_exit':
        suffix = '; sleep .1; printf changed > helper-stdout.json; cp helper-stdout.json output.json'
    elif fault == 'replaced_stage_after_exit':
        suffix = '; sleep .1; cp helper-stdout.json other.json; mv other.json helper-stdout.json; cp helper-stdout.json output.json'
    elif fault == 'failed_helper':
        config['parameters']['sheet'] = argv[-1] = 'fail'
    _child, row = invoke(config, shlex.join(argv)+' > '+stdout+suffix)
    assert row['verified'] is False, row
    if fault == 'failed_helper':
        assert row['process_returncode'] == 7


@pytest.mark.parametrize('name', ['input.xlsx', 'output.json', '../outside.json', '/tmp/out.json',
    'sub/out.json', '', '.', '..', None, 2, 'bad\x00name'])
def test_staged_stdout_configuration_requires_distinct_fixed_workspace_filename(staged_case, name):
    config, _argv = staged_case
    config['stdout_name'] = name
    with pytest.raises(ValueError):
        native.NativeExecutionObserver(config)


def test_staged_stdout_option_materializes_without_changing_legacy_configuration(staged_case):
    config, _argv = staged_case
    options = {key: value for key, value in config.items()
        if key not in {'workspace', 'helper_path', 'skill_path', 'snapshot_id'}}
    snapshot = {'snapshot_id': 'snapshot', 'materialized_path': str(Path(config['skill_path']).parent.parent),
        'entries': [{'skill_id': config['skill_id'], 'skill_path': config['skill_path'],
            'skill_sha256': config['skill_sha256'], 'script_sha256': config['script_sha256']}]}
    assert native.native_execution_evidence_config(config['workspace'], options, snapshot) == config
    del options['stdout_name']
    expected = {key: value for key, value in config.items() if key != 'stdout_name'}
    assert native.native_execution_evidence_config(config['workspace'], options, snapshot) == expected


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
def test_legacy_configuration_keeps_direct_inode_binding_and_receipt_fields(staged_case):
    config, argv = staged_case
    del config['stdout_name']
    _child, row = invoke(config, shlex.join(argv)+' > output.json')
    assert row['verified'] is True, json.dumps(row, ensure_ascii=False)
    assert row['artifact_binding'] == 'final_bytes_on_observed_stdout_inode_not_exclusive_writer'
    assert not any(key.startswith('stdout_') for key in row)
    (Path(config['workspace'])/'output.json').unlink()
    _child, row = invoke(config, shlex.join(argv)+' > helper-stdout.json; cp helper-stdout.json output.json')
    assert row['verified'] is False, row


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin process metadata ABI')
@pytest.mark.parametrize('mode', ['exec-changed', 'exec-in-progress', 'live-unchanged'])
def test_argv_probe_failure_distinguishes_exec_from_current_image(staged_case, monkeypatch, mode):
    config, argv = staged_case
    del config['stdout_name']
    release = Path(config['workspace'])/'release-launcher'
    exec_changed = mode != 'live-unchanged'
    launch = ('os.execv(shell[0], shell)' if exec_changed else
              'raise SystemExit(subprocess.call(shell))')
    launcher = ('import os,subprocess,sys,time\n'
                'while not os.path.exists(sys.argv[1]): time.sleep(.002)\n'
                'shell = ["/bin/bash", "-c", sys.argv[2]]\n'+launch)
    script = 'sleep .1; '+shlex.join(argv)+' > output.json; :'
    original = native._DarwinProcesses.arguments
    original_info = native._DarwinProcesses.info
    failed = []
    stale = {}
    replayed = []
    frozen = None

    def info(probe, pid):
        nonlocal frozen
        if pid in stale:
            replayed.append(pid)
            return stale.pop(pid)
        if replayed and pid == replayed[-1]:
            frozen = None
        return original_info(probe, pid)

    def arguments(probe, pid):
        nonlocal frozen
        values, environment = original(probe, pid)
        if values[1:4] != ['-I', '-c', launcher]:
            return values, environment
        before = original_info(probe, pid)
        release.touch()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not exec_changed or probe.info(pid)['executable'] == '/bin/bash':
                failed.append(pid)
                if mode == 'exec-in-progress':
                    # Replay one stale image read after the real exec. Pause only
                    # the observer clock so scheduler jitter cannot exhaust the window.
                    stale[pid] = before
                    frozen = time.monotonic()
                # Inject the syscall failure; process identity and helper exit are real.
                raise OSError(errno.EIO, 'native process arguments unavailable')
            time.sleep(.002)
        return values, environment

    monkeypatch.setattr(native._DarwinProcesses, 'arguments', arguments)
    monkeypatch.setattr(native._DarwinProcesses, 'info', info)
    if mode == 'exec-in-progress':
        monkeypatch.setattr(native, 'time', SimpleNamespace(
            monotonic=lambda: time.monotonic() if frozen is None else frozen))
    child, row = invoke(config, 'exec '+shlex.join([sys.executable, '-I', '-c', launcher, str(release), script]))
    assert failed and child.returncode == 0, child.stderr
    assert bool(replayed) is (mode == 'exec-in-progress')
    assert row['verified'] is exec_changed, row['process_observation_error']
    assert ('native_process_probe_failed' in row['process_observation_error']) is not exec_changed
    assert row['pid'] not in failed and row['process_returncode'] == 0


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
def test_staged_stdout_does_not_reuse_process_exit_across_terminal_calls(staged_case):
    config, argv = staged_case
    _child, helper = invoke(config, shlex.join(argv)+' > helper-stdout.json')
    assert helper['verified'] is False, helper
    _child, copy = invoke(config, 'cp helper-stdout.json output.json')
    assert copy['verified'] is False, copy
