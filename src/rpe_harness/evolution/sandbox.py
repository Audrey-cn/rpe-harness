"""Run bounded untrusted producers with an OS boundary, never an AST safety claim."""
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

SANDBOX_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MAX_INPUT_BYTES = 16 * 1024 * 1024
JSON_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_CODE_BYTES = 64 * 1024
MAX_RESIDENT_BYTES = 128 * 1024 * 1024
RSS_POLL_SECONDS = 0.02


def resource_limits(capability=None):
    if capability not in {None, 'tabular.deduplicate', 'json.normalize'}:
        raise ValueError('candidate resource capability is not registered')
    return {'input_bytes': JSON_INPUT_BYTES if capability == 'json.normalize' else MAX_INPUT_BYTES,
            'output_bytes': MAX_OUTPUT_BYTES,
            'code_bytes': MAX_CODE_BYTES, 'resident_bytes': MAX_RESIDENT_BYTES,
            'memory_enforcement': 'rss_monitor_soft', 'rss_poll_seconds': RSS_POLL_SECONDS,
            'memory_limitation': 'macOS rlimits do not cap memory; instantaneous allocation peaks are not guaranteed',
            'network': 'denied', 'fork': 'denied', 'open_files': 64}

class CandidateExecutionError(RuntimeError):
    """A real child ran, but failed; resource failures are not parser rejections."""


class CandidateRejected(CandidateExecutionError):
    """The producer explicitly refused input without leaving an output artifact."""


_DRIVER = '''import json, sys
from pathlib import Path
packet = json.load(sys.stdin)
namespace = {'__name__': '__rpe_candidate__'}
exec(compile(Path(sys.argv[1]).read_text(encoding='utf-8'), '<candidate>', 'exec'), namespace)
producer = namespace.get('prepare')
if not callable(producer):
    raise ValueError('candidate must define prepare(plan, staging_path)')
try:
    producer(packet['plan'], packet['staging_path'])
except (ValueError, UnicodeError) as error:
    print('RPE_CANDIDATE_REJECTED:' + json.dumps({'type': type(error).__name__,
          'message': str(error)[:1000]}, ensure_ascii=True), flush=True)
    sys.exit(65)
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
    from ..path_binding import read_source
    capability = plan.get('capability')
    limits = resource_limits(capability)
    is_json = capability == 'json.normalize'
    if is_json and plan.get('input_format') != 'json':
        raise ValueError('JSON candidate requires the JSON input format')
    data = read_source(source, max_bytes=limits['input_bytes'])
    if hashlib.sha256(data).hexdigest() != plan['input_sha256']:
        raise ValueError('input changed after the execution plan')
    root = Path(owned_root).resolve()
    directory = root / 'evolution-sandboxes'
    if directory.is_symlink():
        raise ValueError('sandbox root must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True)
    suffix = 'json' if is_json else 'csv'
    with tempfile.TemporaryDirectory(prefix=suffix + '-', dir=directory) as name:
        job = Path(name).resolve()
        input_copy, code, driver = job / ('input.' + suffix), job / 'candidate.py', job / 'driver.py'
        staging, log = job / ('staging.' + suffix), job / 'child.log'
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
                    try:
                        resident = _resident_bytes(proc.pid)
                    except OSError as error:
                        raise CandidateExecutionError('candidate RSS observation failed') from error
                    if resident is not None and resident > memory_limit_bytes:
                        raise CandidateExecutionError('candidate RSS soft memory threshold exceeded')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CandidateExecutionError('candidate timeout')
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
        if input_copy.read_bytes() != data:
            raise CandidateExecutionError('candidate altered the frozen source')
        if proc.returncode != 0:
            # Bounded log inspection never reads an arbitrarily large diagnostic.
            with log.open('rb') as handle:
                handle.seek(max(0, os.fstat(handle.fileno()).st_size - 4096))
                detail = handle.read(4096).decode('utf-8', errors='replace')
            lines = detail.splitlines()
            marker = 'RPE_CANDIDATE_REJECTED:'
            if (proc.returncode == 65 and lines and lines[-1].startswith(marker)
                    and not os.path.lexists(staging)):
                try:
                    receipt = json.loads(lines[-1][len(marker):])
                except ValueError:
                    receipt = None
                if (type(receipt) is dict and set(receipt) == {'type', 'message'}
                        and receipt['type'] in {'ValueError', 'UnicodeError', 'UnicodeDecodeError',
                                                'UnicodeEncodeError', 'UnicodeTranslateError',
                                                'JSONDecodeError'}
                        and type(receipt['message']) is str):
                    raise CandidateRejected(f'candidate rejected input in {job}: {receipt["type"]}: {receipt["message"]}')
            raise CandidateExecutionError(f'candidate exited {proc.returncode}: {detail[:1000]}')
        if staging.is_symlink() or not staging.is_file() or staging.stat().st_nlink != 1:
            raise CandidateExecutionError('candidate did not create an owned regular staging file')
        if staging.stat().st_size > MAX_OUTPUT_BYTES:
            raise CandidateExecutionError('candidate output exceeds the bounded size')
        return staging.read_bytes()
