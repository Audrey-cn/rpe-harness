"""Run untrusted CSV producers with an OS boundary, never an AST safety claim."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import resource
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from functools import lru_cache

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_CODE_BYTES = 64 * 1024
MAX_RESIDENT_BYTES = 128 * 1024 * 1024
RSS_POLL_SECONDS = 0.02


def resource_limits():
    return {'input_bytes': MAX_INPUT_BYTES, 'output_bytes': MAX_OUTPUT_BYTES,
            'code_bytes': MAX_CODE_BYTES, 'resident_bytes': MAX_RESIDENT_BYTES,
            'memory_enforcement': 'rss_monitor_soft', 'rss_poll_seconds': RSS_POLL_SECONDS,
            'memory_limitation': 'macOS rlimits do not cap memory; instantaneous allocation peaks are not guaranteed',
            'network': 'denied', 'fork': 'denied', 'open_files': 64}

_DRIVER = '''import json, sys
from pathlib import Path
packet = json.load(sys.stdin)
namespace = {'__name__': '__rpe_candidate__'}
exec(compile(Path(sys.argv[1]).read_text(encoding='utf-8'), '<candidate>', 'exec'), namespace)
producer = namespace.get('prepare')
if not callable(producer):
    raise ValueError('candidate must define prepare(plan, staging_path)')
producer(packet['plan'], packet['staging_path'])
'''


def _literal(path):
    return json.dumps(str(Path(path).resolve()))


def _profile(job, staging, log):
    # The candidate cannot read the Harness/oracle, use a network, fork, or change its input.
    reads = {Path('/usr'), Path('/System'), Path('/dev'), Path(sys.base_prefix).resolve(),
             Path(sys.prefix).resolve(), job}
    readable = ' '.join('(subpath ' + _literal(path) + ')' for path in sorted(reads))
    return ('(version 1)(deny default)'
            '(allow process-exec)(deny process-fork)(allow sysctl-read)'
            '(allow file-read* (literal "/") ' + readable + ')'
            '(deny file-read* (subpath ' + _literal(Path(__file__).resolve().parents[1]) + '))'
            '(allow file-write* (literal ' + _literal(staging) + ')'
            ' (literal ' + _literal(log) + '))'
            '(allow signal (target self))')


def _limits(timeout):
    resource.setrlimit(resource.RLIMIT_CPU, (max(1, math.ceil(timeout)), max(2, math.ceil(timeout) + 1)))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class _ProcTaskInfo(ctypes.Structure):
    # Darwin SDK sys/proc_info.h:124-143; memory fields are bytes, not KiB.
    _fields_ = [(name, ctypes.c_uint64) for name in (
        'pti_virtual_size', 'pti_resident_size', 'pti_total_user', 'pti_total_system',
        'pti_threads_user', 'pti_threads_system')] + [(name, ctypes.c_int32) for name in (
        'pti_policy', 'pti_faults', 'pti_pageins', 'pti_cow_faults', 'pti_messages_sent',
        'pti_messages_received', 'pti_syscalls_mach', 'pti_syscalls_unix', 'pti_csw',
        'pti_threadnum', 'pti_numrunning', 'pti_priority')]


@lru_cache(maxsize=1)
def _proc_pidinfo_function():
    # libproc.h:96; PROC_PIDTASKINFO is 4 in sys/proc_info.h:721.
    function = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True).proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


def _resident_bytes(pid):
    if sys.platform != 'darwin':
        raise OSError(errno.ENOSYS, 'candidate RSS observer requires Darwin libproc')
    information = _ProcTaskInfo()
    function = _proc_pidinfo_function()
    ctypes.set_errno(0)
    received = function(pid, 4, 0, ctypes.byref(information), ctypes.sizeof(information))
    error = ctypes.get_errno()
    if received == ctypes.sizeof(information):
        return int(information.pti_resident_size)
    if received == 0 and error == errno.ESRCH:
        return None
    raise OSError(error or errno.EIO, f'candidate RSS observation incomplete: {received} bytes')


def run_candidate(content: bytes, plan: dict, owned_root, *, timeout=3.0,
                  memory_limit_bytes=MAX_RESIDENT_BYTES) -> bytes:
    """Accept only a bounded, regular staging file from a no-network macOS child."""
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise RuntimeError('candidate filesystem/network sandbox is unavailable')
    if type(timeout) not in (float, int) or not 0 < timeout <= 30:
        raise ValueError('candidate timeout must be in (0,30] seconds')
    if type(memory_limit_bytes) is not int or not 0 < memory_limit_bytes <= MAX_RESIDENT_BYTES:
        raise ValueError('candidate resident-memory soft threshold must be in (0,128MiB]')
    if len(content) > MAX_CODE_BYTES:
        raise ValueError('candidate code exceeds the bounded size')
    source = Path(plan['input_path'])
    data = source.read_bytes()
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError('candidate input exceeds the bounded size')
    if hashlib.sha256(data).hexdigest() != plan['input_sha256']:
        raise ValueError('input changed after the execution plan')
    root = Path(owned_root).resolve()
    directory = root / 'evolution-sandboxes'
    if directory.is_symlink():
        raise ValueError('sandbox root must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='csv-', dir=directory) as name:
        job = Path(name).resolve()
        input_copy, code, driver = job / 'input.csv', job / 'candidate.py', job / 'driver.py'
        staging, log = job / 'staging.csv', job / 'child.log'
        input_copy.write_bytes(data)
        code.write_bytes(content)
        driver.write_text(_DRIVER, encoding='utf-8')
        for path in (input_copy, code, driver):
            path.chmod(0o400)
        child_plan = dict(plan, input_path=str(input_copy))
        packet = job / 'plan.json'
        packet.write_text(json.dumps({'plan': child_plan, 'staging_path': str(staging)}), encoding='utf-8')
        packet.chmod(0o400)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(job), 'TMPDIR': str(job),
               'LANG': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
        profile = _profile(job, staging, log)
        with packet.open('rb') as incoming, log.open('wb') as outgoing:
            proc = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile,
                                     sys.executable, '-I', '-B', str(driver), str(code)],
                                    stdin=incoming, stdout=outgoing, stderr=subprocess.STDOUT,
                                    cwd=job, env=env, start_new_session=True,
                                    preexec_fn=lambda: _limits(timeout))
            try:
                deadline = time.monotonic() + timeout
                while proc.poll() is None:
                    resident = _resident_bytes(proc.pid)
                    if resident is not None and resident > memory_limit_bytes:
                        raise RuntimeError('candidate RSS soft memory threshold exceeded')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('candidate timeout')
                    try:
                        proc.wait(timeout=min(RSS_POLL_SECONDS, remaining))
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        if proc.returncode != 0:
            detail = log.read_bytes()[:1000].decode('utf-8', errors='replace')
            raise RuntimeError(f'candidate exited {proc.returncode}: {detail}')
        if input_copy.read_bytes() != data:
            raise RuntimeError('candidate altered the frozen source')
        if staging.is_symlink() or not staging.is_file() or staging.stat().st_nlink != 1:
            raise RuntimeError('candidate did not create an owned regular staging file')
        if staging.stat().st_size > MAX_OUTPUT_BYTES:
            raise RuntimeError('candidate output exceeds the bounded size')
        return staging.read_bytes()
