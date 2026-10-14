"""Directory-object authority for the trusted broker's fixed CLI child.

Logical request paths remain the plan/receipt identity. Open descriptors carry
filesystem authority across exec; no candidate receives these descriptors.
The broker database parent (including private staging) is trusted. This is not
an OS boundary against an arbitrary same-UID process moving bound directories.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import stat

_ENV = 'RPE_BROKER_PATH_BINDING'
_CURRENT = ContextVar('rpe_path_binding', default=None)


def current_binding():
    return _CURRENT.get()


def _identity(fd):
    value = os.fstat(fd)
    return value.st_dev, value.st_ino


def _read_descriptor(fd, max_bytes=None):
    if max_bytes is not None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError('source read budget must be a positive integer')
        if os.fstat(fd).st_size > max_bytes:
            raise ValueError('source exceeds its read budget')
    chunks, offset = [], 0
    while True:
        amount = min(1024 * 1024, max_bytes + 1 - offset) if max_bytes is not None else 1024 * 1024
        data = os.pread(fd, amount, offset)
        if not data:
            return b''.join(chunks)
        if max_bytes is not None and offset + len(data) > max_bytes:
            raise ValueError('source exceeds its read budget')
        chunks.append(data)
        offset += len(data)


def source_digest_nofollow(path, max_bytes=None):
    """Broker receipt probing must not read through a raced symlink either."""
    parent = _directory(Path(path).parent)
    try:
        fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('bound input must be a regular file')
            return hashlib.sha256(_read_descriptor(fd, max_bytes)).hexdigest()
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _directory(path):
    """Walk without following any symlink, retaining authority at each step."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('binding requires absolute paths without traversal')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _descendant(root_fd, parts):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class BoundExecution:
    def __init__(self, source, output, source_fd, output_fd, staging_root, expected_sha):
        self.source, self.output = Path(source), Path(output)
        self.source_fd, self.output_fd = source_fd, output_fd
        self.staging_root = Path(staging_root)
        self.expected_sha = expected_sha

    @classmethod
    def capture(cls, payload, workspace, staging_root, expected_sha):
        if (not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY')
                or not hasattr(os, 'pread') or os.open not in os.supports_dir_fd
                or os.link not in os.supports_dir_fd):
            raise ValueError('broker directory-object binding is unavailable on this platform')
        source, output = (Path(payload['execution'][key]) for key in ('input_path', 'output_path'))
        root = Path(workspace)
        relative = [path.relative_to(root) for path in (source, output)]
        if any('..' in path.parts or not path.name for path in relative):
            raise ValueError('broker paths must be inside its workspace')
        descriptors = []
        try:
            root_fd = _directory(root)
            descriptors.append(root_fd)
            source_parent = _descendant(root_fd, relative[0].parts[:-1])
            descriptors.append(source_parent)
            source_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=source_parent)
            descriptors.append(source_fd)
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise ValueError('bound input must be a regular file')
            output_fd = _descendant(root_fd, relative[1].parts[:-1])
            descriptors.append(output_fd)
            stage_fd = _directory(staging_root)
            descriptors.append(stage_fd)
            if os.fstat(stage_fd).st_dev != os.fstat(output_fd).st_dev:
                raise ValueError('broker requires staging and output on the same filesystem')
            result = cls(source, output, source_fd, output_fd, staging_root, expected_sha)
            result.check_paths()
            result.read_source(max_bytes=1048576 if payload.get('capability') == 'json.normalize' else None)
            descriptors.remove(source_fd)
            descriptors.remove(output_fd)
            return result
        finally:
            for fd in descriptors:
                os.close(fd)

    def check_paths(self):
        """Reject stale logical paths; descriptor authority still protects the race after this check."""
        opened = []
        try:
            parent = _directory(self.source.parent)
            opened.append(parent)
            source = os.open(self.source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
            opened.append(source)
            output = _directory(self.output.parent)
            opened.append(output)
            if _identity(source) != _identity(self.source_fd) or _identity(output) != _identity(self.output_fd):
                raise ValueError('broker bound input or output directory was replaced')
        except OSError as error:
            raise ValueError('broker bound path is no longer available without symlinks') from error
        finally:
            for fd in opened:
                os.close(fd)

    def require_paths(self, source, output):
        if Path(source) != self.source or Path(output) != self.output:
            raise ValueError('execution paths differ from broker binding')
        self.check_paths()
        if self.output_exists():
            raise ValueError('output already exists; overwriting is forbidden')

    def output_exists(self):
        try:
            os.stat(self.output.name, dir_fd=self.output_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def read_source(self, max_bytes=None):
        data = _read_descriptor(self.source_fd, max_bytes)
        if hashlib.sha256(data).hexdigest() != self.expected_sha:
            raise ValueError('bound input differs from the durable receipt')
        return data

    def publish(self, staging):
        self.check_paths()
        # Staging lives in the trusted DB parent. Only the destination uses the
        # mutable workspace, and linkat resolves its basename against our fd.
        os.link(staging, self.output.name, dst_dir_fd=self.output_fd, follow_symlinks=False)
        os.fsync(self.output_fd)
        self.check_paths()

    @property
    def pass_fds(self):
        return self.source_fd, self.output_fd

    def child_environment(self):
        return {_ENV: json.dumps({'source': str(self.source), 'output': str(self.output),
            'source_fd': self.source_fd, 'output_fd': self.output_fd,
            'staging_root': str(self.staging_root), 'expected_sha': self.expected_sha})}

    def close(self):
        for fd in self.pass_fds:
            os.close(fd)

    @contextmanager
    def activate(self):
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)


@contextmanager
def inherited_binding():
    encoded = os.environ.pop(_ENV, None)
    if encoded is None:
        yield
        return
    binding = BoundExecution(**json.loads(encoded))
    try:
        if (not stat.S_ISREG(os.fstat(binding.source_fd).st_mode)
                or not stat.S_ISDIR(os.fstat(binding.output_fd).st_mode)):
            raise ValueError('invalid inherited broker descriptors')
        # Do not check names here: runtime must persist a plan rejection if the
        # workspace changed between broker validation and child startup.
        with binding.activate():
            yield
    finally:
        binding.close()


def read_regular_file(path, *, max_bytes):
    """Bound a nofollow file read without interpreting it as the broker source."""
    parent = _directory(Path(path).parent)
    try:
        fd = os.open(Path(path).name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('file must be regular')
            return _read_descriptor(fd, max_bytes)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def read_source(path, max_bytes=None):
    binding = current_binding()
    if binding is None:
        if max_bytes is None:
            return Path(path).read_bytes()
        return read_regular_file(path, max_bytes=max_bytes)
    if Path(path) != binding.source:
        raise ValueError('source differs from broker binding')
    return binding.read_source(max_bytes)
