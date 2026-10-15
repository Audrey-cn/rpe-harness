"""Kernel exit observations; subprocesses below are real and never call a provider."""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


def process_module():
    name = 'rpe_harness.integrations.hermes_native_process'
    assert importlib.util.find_spec(name) is not None, 'native kernel process observer is missing'
    return importlib.import_module(name)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def process_case(tmp_path):
    workspace, skill = tmp_path/'workspace', tmp_path/'skills/xlsx'
    workspace.mkdir()
    (skill/'scripts').mkdir(parents=True)
    helper = skill/'scripts/xlsx_read.py'
    helper.write_text('import json,sys,time\ntime.sleep(.15)\nprint(json.dumps({"real": True}))\nraise SystemExit(7 if "fail" in sys.argv else 0)\n')
    source = skill/'SKILL.md'
    source.write_text('fixed test helper')
    (workspace/'input.xlsx').write_text('fixed input')
    config = {'workspace': str(workspace), 'helper_path': str(helper), 'skill_path': str(source),
        'helper_python': sys.executable, 'helper_python_sha256': digest(Path(sys.executable).resolve()),
        'skill_id': 'productivity/xlsx', 'snapshot_id': 'snapshot',
        'skill_sha256': digest(source), 'script_sha256': digest(helper),
        'input_name': 'input.xlsx', 'input_sha256': digest(workspace/'input.xlsx'),
        'output_name': 'output.json', 'parameters': {'mode': 'rows', 'sheet': '盘点'}}
    argv = [sys.executable, '-B', str(helper), 'input.xlsx', '--json', '--sheet', '盘点']
    return config, argv


def payload(command, number='1'):
    from rpe_harness.integrations.hermes_gate import digest_json
    args = {'command': command}
    return {'session_id': 'unit', 'task_id': 'task', 'turn_id': 'turn', 'api_request_id': 'api',
        'tool_call_id': number, 'tool_name': 'terminal', 'args': args, 'arguments_sha256': digest_json(args)}


def execute(config, argv, *, suffix='', env=None, command=None):
    module = process_module()
    watch = module.NativeExecutionObserver(config)
    command = command or shlex.join(argv)+' > output.json'+suffix
    before = payload(command)
    watch.before(before)
    child = subprocess.run(['/bin/bash', '-c', command], cwd=config['workspace'],
        env=env or {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'DYLD_', 'LD_'))},
        capture_output=True, text=True, timeout=5)
    receipts = watch.after({**before, 'status': 'succeeded' if child.returncode == 0 else 'failed',
        'result': json.dumps({'exit_code': child.returncode}), 'tool_exit_code': child.returncode})
    return child, receipts


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
def test_kernel_exit_proves_real_helper_success_without_shell_suffix_parser(process_case):
    config, argv = process_case
    child, rows = execute(config, argv, suffix='; echo "exit=$?"; wc -c output.json')
    assert child.returncode == 0
    row, = rows
    assert row['verified'] is True, row
    assert row['process_returncode'] == 0
    assert row['output_sha256'] == hashlib.sha256(b'{"real": true}\n').hexdigest()
    assert row['arguments_sha256'] == payload(shlex.join(argv)+' > output.json; echo "exit=$?"; wc -c output.json')['arguments_sha256']


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
@pytest.mark.parametrize('suffix', ['', '; echo "exit=0"; wc -c output.json'])
def test_kernel_exit_rejects_failed_helper_even_when_shell_or_print_claims_success(process_case, suffix):
    config, argv = process_case
    # A different, explicitly frozen argument contract exercises actual exit7.
    config['parameters'] = {'mode': 'rows', 'sheet': 'fail'}
    argv[-1] = 'fail'
    child, rows = execute(config, argv, suffix=suffix)
    assert child.returncode == (0 if suffix else 7)
    row, = rows
    assert row['process_returncode'] == 7 and row['verified'] is False, row


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
@pytest.mark.parametrize('fault', ['wrong_helper', 'wrong_interpreter', 'wrong_args', 'bypass', 'startup_injection', 'output_symlink', 'input_symlink'])
def test_kernel_exit_does_not_attribute_bypass_wrong_identity_or_startup_code(process_case, fault):
    config, argv = process_case
    workspace = Path(config['workspace'])
    command = None
    if fault == 'wrong_helper':
        other = workspace/'other.py'; other.write_bytes(Path(config['helper_path']).read_bytes()); argv[2] = str(other)
    elif fault == 'wrong_interpreter':
        argv[0] = str(Path(sys.executable).resolve())
    elif fault == 'wrong_args':
        argv[-1] = 'other'
    elif fault == 'bypass':
        command = 'false && '+shlex.join(argv)+' > output.json; echo exit=0'
    elif fault == 'startup_injection':
        injected = workspace/'injected'; injected.mkdir()
        (injected/'sitecustomize.py').write_text('import os\nprint("forged")\nos._exit(0)\n')
        command = 'PYTHONPATH='+shlex.quote(str(injected))+' '+shlex.join(argv)+' > output.json; true'
    elif fault == 'output_symlink':
        (workspace/'elsewhere').write_text(''); (workspace/'output.json').symlink_to(workspace/'elsewhere')
    elif fault == 'input_symlink':
        (workspace/'input.xlsx').rename(workspace/'source.xlsx'); (workspace/'input.xlsx').symlink_to(workspace/'source.xlsx')
    child, rows = execute(config, argv, command=command)
    assert all(row['verified'] is False for row in rows), rows


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
def test_kernel_exit_binds_final_bytes_without_claiming_exclusive_writer(process_case):
    config, argv = process_case
    child, rows = execute(config, argv, suffix='; echo changed > output.json')
    assert child.returncode == 0 and rows[0]['process_returncode'] == 0, rows
    assert rows[0]['output_sha256'] == hashlib.sha256(b'changed\n').hexdigest()
    assert rows[0]['artifact_binding'] == 'final_bytes_on_observed_stdout_inode_not_exclusive_writer'


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
def test_kernel_exit_rejects_overlapping_native_calls(process_case):
    config, argv = process_case
    watch = process_module().NativeExecutionObserver(config)
    command = shlex.join(argv)+' > output.json'
    watch.before(payload(command, '1'))
    watch.before(payload('true', '2'))
    subprocess.run(['/bin/bash', '-c', command], cwd=config['workspace'],
        env={k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'DYLD_', 'LD_'))}, check=True)
    rows = watch.after({**payload(command), 'status': 'succeeded', 'result': '{"exit_code":0}'})
    watch.after({**payload('true', '2'), 'status': 'succeeded', 'result': '{"exit_code":0}'})
    assert not any(row['verified'] for row in rows), rows


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
@pytest.mark.parametrize('fault', ['input_hardlink', 'replace_output_inode', 'transient_input_rename'])
def test_kernel_receipt_rejects_aliases_inode_replacement_and_transient_source_changes(process_case, fault):
    config, argv = process_case
    workspace = Path(config['workspace'])
    suffix = ''
    command = None
    if fault == 'input_hardlink':
        os.link(workspace/'input.xlsx', workspace/'input-alias.xlsx')
    elif fault == 'replace_output_inode':
        suffix = '; cp output.json replacement; mv replacement output.json'
    elif fault == 'transient_input_rename':
        command = 'mv input.xlsx old.xlsx; printf altered > input.xlsx; '+shlex.join(argv)+' > output.json; rm input.xlsx; mv old.xlsx input.xlsx'
    _child, rows = execute(config, argv, suffix=suffix, command=command)
    assert not any(row['verified'] for row in rows), rows


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
def test_native_file_call_overlap_and_duplicate_pre_hook_do_not_leave_observer_threads(process_case):
    import threading
    config, argv = process_case
    watch = process_module().NativeExecutionObserver(config)
    command = shlex.join(argv)+' > output.json'
    before = payload(command)
    watch.before(before)
    watch.before(before)  # duplicate identity must not replace and orphan its watcher
    other = {**payload('write', '2'), 'tool_name': 'write_file'}
    watch.before(other)
    subprocess.run(['/bin/bash', '-c', command], cwd=config['workspace'],
        env={k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'DYLD_', 'LD_'))}, check=True)
    rows = watch.after({**before, 'status': 'succeeded'})
    assert watch.after(other) == []
    assert not any(row['verified'] for row in rows)
    assert not [thread for thread in threading.enumerate() if thread.name == 'native-exit-observer']


def test_unsupported_kernel_observer_fails_closed_without_affecting_other_native_tools(process_case, monkeypatch):
    module = process_module()
    monkeypatch.setattr(module.sys, 'platform', 'unsupported')
    config, argv = process_case
    watch = module.NativeExecutionObserver(config)
    before = payload(shlex.join(argv))
    watch.before(before)
    row, = watch.after({**before, 'status': 'succeeded'})
    assert row['verified'] is False and row['observer_joined'] is True
    assert 'native_kernel_process_observer_unsupported' in row['process_observation_error']


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel process receipt')
def test_observer_drains_registered_kernel_exit_before_joining_after_tool_returns(process_case, monkeypatch):
    """Delay real queued exit delivery until the closing bounded kernel wait.

    This deterministically reproduces scheduler timing where native terminal has
    returned while an already registered helper's exit notification is pending.
    No process event or exit status is fabricated by the scheduling adapter.
    """
    module = process_module()
    original_kqueue = module.select.kqueue
    class DeferredExitQueue:
        def __init__(self):
            self.actual = original_kqueue()
            self.deferred = []
        def control(self, changes, max_events, timeout=None):
            if timeout and self.deferred:
                rows, self.deferred = self.deferred[:max_events], self.deferred[max_events:]
                return rows
            rows = self.actual.control(changes, max_events, timeout)
            if timeout == 0 and max_events:
                self.deferred.extend(row for row in rows if row.filter == module.select.KQ_FILTER_PROC)
                return [row for row in rows if row.filter != module.select.KQ_FILTER_PROC]
            return rows
        def close(self): self.actual.close()
        def __getattr__(self, name): return getattr(self.actual, name)
    monkeypatch.setattr(module.select, 'kqueue', DeferredExitQueue)
    config, argv = process_case
    child, rows = execute(config, argv)
    assert child.returncode == 0
    row, = rows
    assert row.get('process_returncode') == 0 and row['verified'] is True, row
    assert row['observer_joined'] is True
