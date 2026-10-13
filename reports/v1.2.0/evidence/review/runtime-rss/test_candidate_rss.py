"""Darwin RSS observations must work inside an enclosing macOS sandbox."""
import ctypes
import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rpe_harness.evolution import sandbox


def test_rss_observer_reads_real_self_inside_outer_sandbox(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'src'
    program = (f'import sys;sys.path.insert(0,{str(source)!r});'
               'import os,json;from rpe_harness.evolution.sandbox import _resident_bytes;'
               'print(json.dumps({"resident_bytes":_resident_bytes(os.getpid())}))')
    result = subprocess.run(['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)',
                             sys.executable, '-I', '-B', '-c', program],
                            cwd=tmp_path, capture_output=True, text=True, timeout=5,
                            env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path),
                                 'TMPDIR': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stderr
    resident = json.loads(result.stdout)['resident_bytes']
    assert isinstance(resident, int) and resident > 0


def test_rss_observer_returns_none_for_actual_reaped_child(tmp_path):
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'pass'], cwd=tmp_path)
    assert child.wait(timeout=5) == 0
    assert sandbox._resident_bytes(child.pid) is None


@pytest.mark.parametrize('returned,code', [(0, errno.EPERM), (0, 0), (8, 0), (8, errno.ESRCH)])
def test_rss_unobservable_or_partial_result_fails_closed(monkeypatch, returned, code):
    # Only inject otherwise hard-to-force libproc failures. The outer-sandbox and
    # process-exit cases above call the real OS observer.
    def response(pid, flavor, arg, buffer, size):
        ctypes.set_errno(code)
        return returned

    monkeypatch.setattr(sandbox, '_proc_pidinfo_function', lambda: response, raising=False)
    with pytest.raises(OSError):
        sandbox._resident_bytes(os.getpid())
