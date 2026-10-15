"""Opt-in Darwin kernel exit telemetry, without execution or shell interpretation.

The observer only examines descendants of this Hermes process during a real
terminal call. Fast processes that finish before registration have no proof.
Only kernel process metadata supplies argv/environment validation. Stdout
markers and child-written receipts supply no proof. This is local runtime
evidence, not independent third-party attestation.
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import select
import stat
import struct
import sys
import threading
import time

from .hermes_native import _IDS

_MAX_BYTES = 32 * 1024 * 1024
_OPTION_KEYS = {'skill_id', 'helper_python', 'helper_python_sha256', 'skill_sha256',
                'script_sha256', 'input_name', 'input_sha256', 'output_name', 'parameters'}
_CONFIG_KEYS = _OPTION_KEYS | {'workspace', 'helper_path', 'skill_path', 'snapshot_id'}
_SIGNING_KEY = None


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _digest(value):
    return _sha(json.dumps(value, sort_keys=True, ensure_ascii=False,
                           separators=(',', ':'), allow_nan=False).encode())


def install_native_execution_signing_key():
    """Consume a controller pipe once, before model-visible child processes exist."""
    global _SIGNING_KEY
    descriptor = os.environ.pop('RPE_HERMES_NATIVE_EXECUTION_KEY_FD', None)
    if descriptor is None:
        return
    fd = int(descriptor)
    try:
        key = os.read(fd, 33)
    finally:
        os.close(fd)
    if len(key) != 32 or _SIGNING_KEY is not None:
        raise ValueError('invalid native execution authentication key')
    _SIGNING_KEY = key


def receipt_signature(row, key):
    """Canonical signed fields are exactly the primitive fields persisted by the host."""
    return hmac.new(key, _digest(row).encode(), hashlib.sha256).hexdigest()


def sign_native_receipt(row, signature_field):
    """Sign host-owned primitive metadata with the already consumed private key."""
    if _SIGNING_KEY is None:
        return None
    return receipt_signature({key: value for key, value in row.items()
                              if key not in {'kind', signature_field}}, _SIGNING_KEY)


def file_snapshot(path):
    """Reject links, special files and changes while hashing bounded bytes."""
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        return {'state': 'invalid'}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'state': 'absent'}
    except OSError:
        return {'state': 'invalid'}
    try:
        def identity(s):
            return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _MAX_BYTES:
            return {'state': 'invalid'}
        data = bytearray()
        while len(data) <= _MAX_BYTES:
            part = os.read(fd, min(65536, _MAX_BYTES + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        if (len(data) > _MAX_BYTES or identity(before) != identity(after)
                or identity(current) != identity(after)):
            return {'state': 'invalid'}
        return {'state': 'regular', 'sha256': _sha(data), 'size': len(data), 'identity': identity(after)}
    except OSError:
        return {'state': 'invalid'}
    finally:
        os.close(fd)


def native_execution_evidence_config(workspace, options, snapshot):
    """Resolve fixed original helper paths from the owned materialized skill copy."""
    if type(options) is not dict or set(options) != _OPTION_KEYS or type(snapshot) is not dict:
        raise ValueError('invalid native execution evidence options')
    rows = [row for row in snapshot['entries'] if row['skill_id'] == options['skill_id']]
    if len(rows) != 1:
        raise ValueError('native execution requires one materialized original skill')
    row = rows[0]
    source = Path(row.get('native_skill_path', row['skill_path']))
    root = Path(snapshot['materialized_path'])
    if (not source.is_absolute() or not source.is_relative_to(root) or source.name != 'SKILL.md'
            or options['skill_sha256'] != row['skill_sha256']
            or options['script_sha256'] != row['script_sha256']):
        raise ValueError('native execution evidence differs from skill snapshot')
    config = {**options, 'workspace': str(Path(workspace).resolve(strict=True)),
        'snapshot_id': snapshot['snapshot_id'], 'skill_path': str(source),
        'helper_path': str(source.parent/'scripts/xlsx_read.py')}
    _validate_config(config)
    return config


def _validate_config(config):
    if type(config) is not dict or set(config) != _CONFIG_KEYS:
        raise ValueError('invalid native execution evidence configuration')
    for key in ('workspace', 'helper_python', 'helper_path', 'skill_path'):
        path = Path(config[key])
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('native execution paths must be absolute')
    for key in ('input_name', 'output_name'):
        if not isinstance(config[key], str) or Path(config[key]).name != config[key] or config[key] in ('', '.', '..'):
            raise ValueError('native execution observes fixed workspace filenames')
    if config['input_name'] == config['output_name']:
        raise ValueError('native execution output must be new')
    for key in ('helper_python_sha256', 'script_sha256', 'skill_sha256', 'input_sha256'):
        if not isinstance(config[key], str) or not re.fullmatch('[a-f0-9]{64}', config[key]):
            raise ValueError('native execution requires frozen file hashes')
    params = config['parameters']
    if (type(params) is not dict or set(params) - {'mode', 'sheet'}
            or params.get('mode') not in {'rows', 'sheets', 'formulas'}
            or 'sheet' in params and params['sheet'] is not None and type(params['sheet']) is not str):
        raise ValueError('native execution requires fixed original helper parameters')
    helper, source = Path(config['helper_path']), Path(config['skill_path'])
    if source.name != 'SKILL.md' or helper != source.parent/'scripts/xlsx_read.py':
        raise ValueError('native execution must observe the original helper path')


class _DarwinProcesses:
    """Read fixed public Darwin libproc/sysctl ABI records; never run probes."""
    def __init__(self):
        self.lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        self.libc = ctypes.CDLL(None, use_errno=True)

    def descendants(self):
        found, parents = set(), [os.getpid()]
        while parents:
            parent = parents.pop()
            buf = (ctypes.c_int * 512)()
            count = self.lib.proc_listchildpids(parent, buf, ctypes.sizeof(buf))
            if count < 0 or count >= 512 or len(found) + count >= 512:
                raise OSError('native descendant observation budget exceeded')
            for pid in buf[:count]:
                if pid > 0 and pid not in found:
                    found.add(pid)
                    parents.append(pid)
        return found

    def info(self, pid):
        # SDK sys/proc_info.h: proc_bsdinfo (136 bytes), start timeval at 120.
        bsd = ctypes.create_string_buffer(136)
        if self.lib.proc_pidinfo(pid, 3, 0, bsd, len(bsd)) != len(bsd):
            raise ProcessLookupError('native process identity unavailable')
        actual, parent = struct.unpack_from('=II', bsd.raw, 12)
        start = struct.unpack_from('=QQ', bsd.raw, 120)
        if actual != pid or not start[0]:
            raise ProcessLookupError('native process identity changed')
        executable = ctypes.create_string_buffer(4096)
        if self.lib.proc_pidpath(pid, executable, len(executable)) <= 0:
            raise ProcessLookupError('native process executable unavailable')
        return {'pid': pid, 'parent_pid': parent, 'start_identity': list(start),
                'executable': os.fsdecode(executable.value)}

    def arguments(self, pid):
        # KERN_PROCARGS2: argc, executable NUL, padding NULs, argv, environ.
        buf = ctypes.create_string_buffer(1024 * 1024)
        size = ctypes.c_size_t(len(buf))
        mib = (ctypes.c_int * 3)(1, 49, pid)
        if self.libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
            raise OSError('native process arguments unavailable')
        raw = buf.raw[:size.value]
        argc = struct.unpack_from('=i', raw)[0]
        if not 1 <= argc <= 32:
            raise ValueError('native process argument budget exceeded')
        offset = raw.index(b'\0', 4) + 1
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        parts = raw[offset:].split(b'\0')
        argv = [os.fsdecode(part) for part in parts[:argc]]
        environment = {}
        for part in parts[argc:]:
            key, separator, value = part.partition(b'=')
            if separator:
                name = os.fsdecode(key)
                if name.startswith(('PYTHON', 'DYLD_', 'LD_')):
                    if name in environment:
                        raise ValueError('duplicate native startup environment')
                    environment[name] = os.fsdecode(value)
        return argv, environment

    def io_paths(self, pid):
        # Fixed 64-bit SDK ABI: vnode_info=152, vnode_info_path=1176,
        # proc_fileinfo=24. Both calls must return the full expected structure.
        cwd = ctypes.create_string_buffer(2352)
        out = ctypes.create_string_buffer(1200)
        if (self.lib.proc_pidinfo(pid, 9, 0, cwd, len(cwd)) != len(cwd)
                or self.lib.proc_pidfdinfo(pid, 1, 2, out, len(out)) != len(out)):
            raise OSError('native process regular stdout or cwd unavailable')
        device, mode, links, inode = struct.unpack_from('=IHHQ', out.raw, 24)
        if not stat.S_ISREG(mode) or links != 1:
            raise ValueError('native process stdout is not a single-link regular file')
        return (os.fsdecode(cwd.raw[152:1176].split(b'\0', 1)[0]),
                os.fsdecode(out.raw[176:1200].split(b'\0', 1)[0]), [device, inode])


def _directory_identity(path):
    path = Path(path)
    if path.is_symlink():
        return None
    try:
        value = path.stat()
        return [value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns,
                sorted(item.name for item in path.iterdir())]
    except OSError:
        return None


class _KernelCall:
    def __init__(self, config):
        self.config = config
        self.stop, self.ready = threading.Event(), threading.Event()
        self.rows, self.errors, self.overlap = [], [], False
        self.initial = self.snapshots()
        self.directories = self.directory_snapshots()
        self.output_before = file_snapshot(Path(config['workspace'])/config['output_name'])
        self.thread = threading.Thread(target=self.run, name='native-exit-observer')
        self.thread.start()
        if not self.ready.wait(1):
            self.errors.append('native_process_observer_not_ready')
            self.stop.set()

    def snapshots(self):
        c = self.config
        return {key: file_snapshot(path) for key, path in {
            'helper': c['helper_path'], 'skill': c['skill_path'],
            'input': str(Path(c['workspace'])/c['input_name']),
            # The frozen interpreter pathname can intentionally be a venv link.
            'interpreter': str(Path(c['helper_python']).resolve())}.items()}

    def directory_snapshots(self):
        root = Path(self.config['skill_path']).parent
        return [_directory_identity(root), _directory_identity(root/'scripts')]

    def fixed_bytes(self):
        c = self.config
        return (self.initial == self.snapshots() and self.directories == self.directory_snapshots()
                and self.directories[0] is not None and self.directories[1] is not None
                and self.directories[0][-1] == ['SKILL.md', 'scripts']
                and self.directories[1][-1] == ['xlsx_read.py']
                and all(self.initial[key].get('sha256') == c[expected] for key, expected in
                    [('helper', 'script_sha256'), ('skill', 'skill_sha256'),
                     ('input', 'input_sha256'), ('interpreter', 'helper_python_sha256')]))

    def matches(self, argv, environment):
        c = self.config
        if not argv or argv[0] != c['helper_python']:
            return False
        rest, flags = argv[1:], []
        while rest and rest[0] in {'-B', '-u', '-Bu', '-uB', '-I'}:
            flags.append(rest[0]); rest = rest[1:]
        mode = {'rows': '--json', 'sheets': '--sheets', 'formulas': '--formulas'}[c['parameters']['mode']]
        expected = [mode] + (['--sheet', c['parameters']['sheet']] if c['parameters'].get('sheet') is not None else [])
        if (len(rest) != len(expected) + 2 or rest[0] != c['helper_path']
                or rest[1] not in {c['input_name'], str(Path(c['workspace'])/c['input_name'])}
                or rest[2:] != expected):
            return False
        # No model-controlled startup import paths or dynamic library injection.
        harmless = {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1', 'PYTHONUTF8': '1'}
        if any(key not in harmless or value != harmless[key] for key, value in environment.items()):
            raise ValueError('native_process_unsafe_startup_environment')
        if '-I' not in flags:
            cfg = Path(c['helper_python']).parent.parent/'pyvenv.cfg'
            try:
                text = cfg.read_text()
            except OSError:
                text = ''
            if not re.search(r'(?im)^include-system-site-packages\s*=\s*false\s*$', text):
                raise ValueError('native_process_user_site_not_isolated')
        return True

    def run(self):
        watched, seen = {}, set()
        queue, frozen_fds = None, []
        try:
            if sys.platform != 'darwin' or not hasattr(select, 'kqueue'):
                raise ValueError('native_kernel_process_observer_unsupported')
            process = _DarwinProcesses()
            baseline = process.descendants()
            queue = select.kqueue()
            # Catch writes/renames even when a model restores identical bytes
            # and the original inode before the final hash check.
            for path in (self.config['helper_path'], self.config['skill_path'],
                         str(Path(self.config['workspace'])/self.config['input_name']),
                         str(Path(self.config['helper_python']).resolve())):
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                frozen_fds.append(fd)
                queue.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                    fflags=(select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_EXTEND
                            | select.KQ_NOTE_ATTRIB | select.KQ_NOTE_LINK | select.KQ_NOTE_RENAME
                            | select.KQ_NOTE_REVOKE))], 0, 0)
            self.ready.set()
            deadline = time.monotonic() + 60
            closing_deadline = None
            while time.monotonic() < deadline:
                wait = 0
                if self.stop.is_set():
                    if not watched:
                        break
                    # Native terminal can return before kqueue publishes an
                    # already registered grandchild's exit. Drain only those
                    # registrations with a bounded kernel wait before closing;
                    # a timeout still supplies no proof or inferred exit code.
                    if closing_deadline is None:
                        closing_deadline = min(deadline, time.monotonic() + 1)
                    wait = closing_deadline - time.monotonic()
                    if wait <= 0:
                        break
                for event in queue.control(None, 64, wait):
                    if event.filter == select.KQ_FILTER_VNODE:
                        # macOS can emit NOTE_ATTRIB when exec validates the
                        # interpreter's code signature without changing its
                        # persisted stat identity. Real chmod/touch changes
                        # ctime/mtime and is still rejected by fixed_bytes().
                        if event.fflags != select.KQ_NOTE_ATTRIB or not self.fixed_bytes():
                            self.errors.append('native_process_frozen_file_changed')
                        continue
                    if event.ident not in watched:
                        continue
                    row = watched.pop(event.ident)
                    row['process_returncode'] = os.waitstatus_to_exitcode(event.data)
                    row['kernel_exit_observed'] = bool(event.fflags & 0x04000000)
                    row['exit_output'] = file_snapshot(Path(self.config['workspace'])/self.config['output_name'])
                    self.rows.append(row)
                if self.stop.is_set():
                    continue
                for pid in process.descendants() - baseline - seen:
                    try:
                        info = process.info(pid)
                        if Path(info['executable']).resolve() != Path(self.config['helper_python']).resolve():
                            continue  # bash can subsequently exec Python under the same PID
                        argv, environment = process.arguments(pid)
                        if not self.matches(argv, environment):
                            continue
                        seen.add(pid)
                        cwd, stdout, stdout_identity = process.io_paths(pid)
                        if (cwd != self.config['workspace'] or stdout != str(Path(cwd)/self.config['output_name'])
                                or not self.fixed_bytes()):
                            raise ValueError('native_process_paths_or_frozen_bytes_changed')
                        out = file_snapshot(stdout)
                        if out.get('identity', [])[:2] != stdout_identity:
                            raise ValueError('native_process_stdout_identity_changed')
                        queue.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                            fflags=select.KQ_NOTE_EXIT | 0x04000000)], 0, 0)
                        if process.info(pid) != info:
                            raise ValueError('native_process_identity_changed')
                        watched[pid] = {key: info[key] for key in ('pid', 'parent_pid', 'start_identity')}
                        watched[pid].update(process_argv_sha256=_digest(argv),
                            startup_environment_sha256=_digest(environment), stdout_identity=stdout_identity)
                    except ProcessLookupError:
                        continue  # A missed/short process supplies no receipt.
                    except (OSError, ValueError, struct.error) as error:
                        seen.add(pid)
                        self.errors.append(str(error) if isinstance(error, ValueError) else 'native_process_probe_failed')
                self.stop.wait(.002)
            else:
                self.errors.append('native_process_observer_deadline')
            if watched:
                self.errors.append('native_process_exit_not_observed')
        except (OSError, ValueError, struct.error) as error:
            self.errors.append(str(error) if isinstance(error, ValueError) else 'native_kernel_observer_failed')
        finally:
            if queue is not None:
                queue.close()
            for fd in frozen_fds:
                os.close(fd)
            self.ready.set()

    def finish(self):
        self.stop.set()
        self.thread.join(2)
        if self.thread.is_alive():
            self.errors.append('native_process_observer_join_failed')
        output = file_snapshot(Path(self.config['workspace'])/self.config['output_name'])
        valid = (not self.errors and not self.overlap and len(self.rows) == 1
                 and self.output_before['state'] == 'absent' and self.fixed_bytes())
        if len(self.rows) != 1:
            self.errors.append('native_unique_helper_process_exit_missing')
        row = self.rows[0] if len(self.rows) == 1 else {}
        valid = bool(valid and row.get('kernel_exit_observed') is True and row.get('process_returncode') == 0
                     and output['state'] == 'regular' and row.get('exit_output') == output
                     and output.get('identity', [])[:2] == row.get('stdout_identity'))
        c = self.config
        return {'kind': 'native_helper_process_observation', 'verified': valid,
            'proof_kind': 'darwin_kernel_process_exit', 'errors': sorted(set(self.errors)),
            'process_config_sha256': _digest(c),
            **{key: c[key] for key in ('skill_id', 'snapshot_id', 'skill_sha256', 'script_sha256',
                                       'input_sha256', 'helper_python_sha256')},
            **{key: value for key, value in row.items() if key not in {'exit_output', 'stdout_identity', 'start_identity'}},
            'process_start_identity_sha256': _digest(row['start_identity']) if 'start_identity' in row else None,
            'output_sha256': output.get('sha256'), 'output_size': output.get('size'),
            'output_identity_sha256': _digest(output['identity']) if 'identity' in output else None,
            'output_path_sha256': _sha(str(Path(c['workspace'])/c['output_name']).encode()),
            'overlap': self.overlap, 'observer_joined': not self.thread.is_alive(),
            'artifact_binding': 'final_bytes_on_observed_stdout_inode_not_exclusive_writer'}


class NativeExecutionObserver:
    """One bounded kernel observer per immutable real terminal invocation."""
    def __init__(self, config):
        _validate_config(config)
        self.config = config
        self.pending, self.lock = {}, threading.RLock()

    def before(self, payload):
        if payload.get('tool_name') not in {'terminal', 'write_file', 'patch'}:
            return
        identity = tuple(payload.get(key) for key in _IDS)
        if any(type(value) is not str or not value for value in identity):
            return
        with self.lock:
            overlap = bool(self.pending)
            for call in self.pending.values():
                if call is not None:
                    call.overlap = True
            if identity in self.pending:
                return
            if payload['tool_name'] != 'terminal':
                self.pending[identity] = None
                return
            call = _KernelCall(self.config)
            call.overlap = overlap
            self.pending[identity] = call

    def after(self, payload):
        identity = tuple(payload.get(key) for key in _IDS)
        with self.lock:
            call = self.pending.pop(identity, None)
        if call is None:
            return []
        row = call.finish()
        row.update({key: payload[key] for key in _IDS})
        row['arguments_sha256'] = payload.get('arguments_sha256')
        row['tool_status'] = payload.get('status')
        row['process_observation_error'] = ','.join(row.pop('errors'))
        if _SIGNING_KEY is not None:
            row['process_receipt_hmac_sha256'] = receipt_signature(
                {key: value for key, value in row.items() if key != 'kind'}, _SIGNING_KEY)
        return [row]
