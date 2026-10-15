"""A vanished argv candidate supplies no proof; live probe failures stay fatal."""
import subprocess
import sys

import pytest

from rpe_harness.integrations import hermes_native_process as native


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin process metadata ABI')
def test_argv_probe_classifies_a_reaped_real_process_as_disappeared(tmp_path):
    process = subprocess.Popen([sys.executable, '-I', '-c', 'pass'], cwd=tmp_path,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert process.wait(timeout=5) == 0
    with pytest.raises(ProcessLookupError):
        native._DarwinProcesses().arguments(process.pid)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin process metadata ABI')
def test_argv_probe_failure_for_a_live_real_process_remains_an_error(tmp_path, monkeypatch):
    process = subprocess.Popen([sys.executable, '-I', '-c', 'import sys;sys.stdin.buffer.read(1)'],
        cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        probe = native._DarwinProcesses()
        assert probe.info(process.pid)['pid'] == process.pid
        # Inject only syscall failure. Process existence still comes from the
        # actual kernel, and must prevent classifying this as a harmless exit.
        monkeypatch.setattr(probe.libc, 'sysctl', lambda *_args: -1)
        with pytest.raises(OSError) as error:
            probe.arguments(process.pid)
        assert not isinstance(error.value, ProcessLookupError)
        assert process.poll() is None
    finally:
        process.communicate(input=b'\n', timeout=5)
    assert process.returncode == 0
