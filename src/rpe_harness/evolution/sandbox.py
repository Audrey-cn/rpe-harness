"""Run bounded untrusted producers with an OS boundary, never an AST safety claim."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
try:
    import resource
except ImportError:  # Windows
    resource = None
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
            'memory_enforcement': 'windows_job_hard_limit' if sys.platform == 'win32' else 'rss_monitor_soft',
            'rss_poll_seconds': RSS_POLL_SECONDS,
            'memory_limitation': (
                'Windows Job Object enforces hard process memory and active-process=1 limits'
                if sys.platform == 'win32'
                else 'macOS rlimits do not cap memory; instantaneous allocation peaks are not guaranteed'
            ),
            'network': 'host_acl' if sys.platform == 'win32' else 'denied',
            'fork': 'denied', 'open_files': 64}


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
    return json.dumps(str(Path(path).resolve()), ensure_ascii=False)


def _profile(job, staging, log):
    # The candidate cannot read the Harness/oracle, use a network, fork, or change its input.
    exe = Path(sys.executable)
    reads = {Path('/usr'), Path('/System'), Path('/dev'), Path(sys.base_prefix).resolve(),
             Path(sys.prefix).resolve(), exe.parent.resolve(), exe.resolve().parent.parent, job}
    readable = ' '.join('(subpath ' + _literal(path) + ')' for path in sorted(reads))
    return ('(version 1)(deny default)'
            '(allow process-exec)(deny process-fork)(allow sysctl-read)'
            '(allow file-read-metadata)'
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


# ---------------------------------------------------------------------------
# Windows native Win32 Job Object containment (pure stdlib ctypes)
# ---------------------------------------------------------------------------

class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('ReadOperationCount', ctypes.c_uint64),
        ('WriteOperationCount', ctypes.c_uint64),
        ('OtherOperationCount', ctypes.c_uint64),
        ('ReadTransferCount', ctypes.c_uint64),
        ('WriteTransferCount', ctypes.c_uint64),
        ('OtherTransferCount', ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', ctypes.c_uint32),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', _IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


def _win32_create_job(memory_limit_bytes: int):
    """Create a Win32 Job Object with hard memory cap, active-process=1, and kill-on-close."""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), 'CreateJobObjectW failed')
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    )
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = memory_limit_bytes
    if not kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(err, 'SetInformationJobObject failed')
    return kernel32, job


def _win32_resume_process(proc):
    """Resume only after Job assignment; Popen closes the primary thread handle."""
    function = ctypes.WinDLL('ntdll').NtResumeProcess
    function.argtypes = [ctypes.c_void_p]
    function.restype = ctypes.c_long
    if function(int(proc._handle)) != 0:
        raise CandidateExecutionError('NtResumeProcess failed')


@lru_cache(maxsize=8)
def _bwrap_pid_namespace_supported(bwrap: str) -> bool:
    """Probe optional PID namespaces without weakening filesystem/network isolation.

    Some kernels/AppArmor profiles deny PID namespaces, even when other bwrap
    namespaces work. The fallback remains bwrap-contained and is declared below.
    """
    try:
        result = subprocess.run(
            [bwrap, '--unshare-net', '--unshare-pid', '--die-with-parent', '--ro-bind', '/', '/',
             '--proc', '/proc', '/bin/true'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def sandbox_backend() -> str:
    """Detect available OS containment backend without faking isolation."""
    if sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file():
        return 'macos-sandbox-exec'
    if sys.platform.startswith('linux'):
        import shutil
        if shutil.which('bwrap'):
            return 'linux-bwrap'
    if sys.platform == 'win32':
        return 'windows-job-object'
    return 'unavailable'


def sandbox_capabilities() -> dict[str, object]:
    """Honest capability declaration for the detected OS containment backend."""
    backend = sandbox_backend()
    if backend == 'macos-sandbox-exec':
        return {
            'backend': backend,
            'process_tree': 'deny_fork_and_killpg',
            'filesystem': 'seatbelt_profile',
            'network': 'denied',
            'memory': 'rss_monitor_soft',
        }
    if backend == 'linux-bwrap':
        import shutil
        bwrap = shutil.which('bwrap')
        pid_namespace = bool(bwrap and _bwrap_pid_namespace_supported(bwrap))
        return {
            'backend': backend,
            'process_tree': ('pid_namespace_die_with_parent_and_killpg' if pid_namespace
                             else 'killpg_only_no_pid_namespace'),
            'pid_namespace': pid_namespace,
            'process_tree_limitation': (
                'PID namespace teardown contains descendants' if pid_namespace else
                'Host kernel/AppArmor or bwrap lacks PID namespaces; descendants that change '
                'process groups can escape killpg cleanup; bwrap filesystem/network limits remain'
            ),
            'filesystem': 'ro_bind_namespace',
            'network': 'unshare_net_denied',
            'memory': 'proc_tree_statm_rss_soft',
            'memory_limitation': 'Observed descendant RSS is summed; process-exit races and allocation peaks remain soft limits',
        }
    if backend == 'windows-job-object':
        return {
            'backend': backend,
            'process_tree': 'job_active_process_1_kill_on_close',
            'filesystem': 'host_acl_only',
            'network': 'host_acl_only',
            'memory': 'job_object_hard_process_limit',
        }
    return {
        'backend': 'unavailable',
        'process_tree': 'none',
        'filesystem': 'none',
        'network': 'none',
        'memory': 'none',
    }


def _bwrap_command(job: Path, driver: Path, code: Path) -> list[str]:
    import shutil
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise RuntimeError('candidate filesystem/network sandbox is unavailable')
    cmd = [bwrap, '--unshare-net', '--die-with-parent', '--proc', '/proc', '--dev', '/dev']
    if _bwrap_pid_namespace_supported(bwrap):
        cmd.insert(1, '--unshare-pid')
    for ro in ('/usr', '/lib', '/lib64', '/bin', '/etc/alternatives', sys.base_prefix, sys.prefix):
        p = Path(ro)
        if p.exists():
            cmd.extend(['--ro-bind', str(p), str(p)])
    cmd.extend(['--bind', str(job), str(job), '--chdir', str(job),
                sys.executable, '-I', '-B', str(driver), str(code)])
    return cmd


def _linux_resident_bytes(pid: int) -> int | None:
    try:
        statm = Path(f'/proc/{pid}/statm').read_text(encoding='ascii').split()
        return int(statm[1]) * os.sysconf('SC_PAGE_SIZE')
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
        return None


def _linux_child_pids(pid: int) -> list[int]:
    # Children can be forked by any thread, not just the thread-group leader.
    children = set()
    try:
        tasks = list(Path(f'/proc/{pid}/task').iterdir())
    except (FileNotFoundError, ProcessLookupError):
        return []
    for task in tasks:
        try:
            children.update(int(value) for value in (task / 'children').read_text(encoding='ascii').split())
        except (FileNotFoundError, ProcessLookupError):
            continue
    return sorted(children)


def _linux_process_tree_resident_bytes(pid: int) -> int | None:
    """Observe the outer bwrap monitor, its init, and the actual candidate tree."""
    total, observed = 0, False
    pending, visited = [pid], set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        resident = _linux_resident_bytes(current)
        if resident is not None:
            total += resident
            observed = True
        pending.extend(_linux_child_pids(current))
    return total if observed else None


def run_candidate(content: bytes, plan: dict, owned_root, *, timeout=3.0,
                  memory_limit_bytes=MAX_RESIDENT_BYTES) -> bytes:
    """Accept only a bounded, regular staging file from an OS-contained child."""
    backend = sandbox_backend()
    if backend == 'unavailable':
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
        if os.name != 'nt':
            for path in (input_copy, code, driver):
                path.chmod(0o400)
        child_plan = dict(plan, input_path=str(input_copy))
        packet = job / 'plan.json'
        packet.write_text(json.dumps({'plan': child_plan, 'staging_path': str(staging)}), encoding='utf-8')
        if os.name != 'nt':
            packet.chmod(0o400)
        if backend == 'windows-job-object':
            env = {
                'SYSTEMROOT': os.environ.get('SYSTEMROOT', r'C:\Windows'),
                'PATH': os.environ.get('PATH', ''),
                'TEMP': str(job),
                'TMP': str(job),
                'PYTHONUTF8': '1',
                'PYTHONDONTWRITEBYTECODE': '1',
            }
            # Windows venv python.exe may be a redirector that spawns the base
            # interpreter, which conflicts with the Job's active-process=1 cap.
            # The isolated driver only needs the stdlib. Use the interpreter
            # identified by this trusted runtime, never PATH or candidate input.
            child_executable = sys.executable
            base_executable = getattr(sys, '_base_executable', None)
            if (isinstance(base_executable, str) and Path(base_executable).is_absolute()
                    and Path(base_executable).is_file()):
                child_executable = base_executable
            child_argv = [child_executable, '-I', '-B', str(driver), str(code)]
            kernel32, job_handle = _win32_create_job(memory_limit_bytes)
            try:
                with packet.open('rb') as incoming, log.open('wb') as outgoing:
                    proc = subprocess.Popen(
                        child_argv,
                        stdin=incoming, stdout=outgoing, stderr=subprocess.STDOUT,
                        cwd=job, env=env,
                        # The candidate must not execute before containment is installed.
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004,  # CREATE_SUSPENDED
                    )
                    assigned = False
                    try:
                        assigned = bool(kernel32.AssignProcessToJobObject(job_handle, int(proc._handle)))
                        if not assigned:
                            raise CandidateExecutionError('AssignProcessToJobObject failed')
                        _win32_resume_process(proc)
                        try:
                            proc.wait(timeout=timeout)
                        except subprocess.TimeoutExpired as exc:
                            kernel32.TerminateJobObject(job_handle, 1)
                            raise CandidateExecutionError('candidate timeout') from exc
                    finally:
                        kernel32.TerminateJobObject(job_handle, 1)
                        # A failed assignment leaves the child outside the Job Object.
                        # Kill it directly and never let cleanup become an unbounded wait.
                        try:
                            if not assigned or proc.poll() is None:
                                proc.kill()
                        finally:
                            try:
                                proc.wait(timeout=3)
                            except subprocess.TimeoutExpired as exc:
                                raise CandidateExecutionError('candidate cleanup timeout') from exc
            finally:
                kernel32.CloseHandle(job_handle)
        else:
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(job), 'TMPDIR': str(job),
                   'LANG': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
            if backend == 'macos-sandbox-exec':
                profile = _profile(job, staging, log)
                child_argv = ['/usr/bin/sandbox-exec', '-p', profile,
                              str(Path(sys.executable).resolve()), '-I', '-B', str(driver), str(code)]
            else:
                child_argv = _bwrap_command(job, driver, code)
            with packet.open('rb') as incoming, log.open('wb') as outgoing:
                proc = subprocess.Popen(child_argv,
                                        stdin=incoming, stdout=outgoing, stderr=subprocess.STDOUT,
                                        cwd=job, env=env, start_new_session=True,
                                        preexec_fn=lambda: _limits(timeout))
                try:
                    deadline = time.monotonic() + timeout
                    while proc.poll() is None:
                        try:
                            resident = (_resident_bytes(proc.pid) if backend == 'macos-sandbox-exec'
                                        else _linux_process_tree_resident_bytes(proc.pid))
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
