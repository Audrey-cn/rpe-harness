"""Native skill source proof with only standard-library dependencies.

The restricted Hermes process can load this module without access to workflow,
broker or catalog code.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

_SKILL_BYTES_LIMIT = 65536


def _read_owned_skill(path, root):
    """Walk only materialized directories by descriptor; never follow aliases."""
    path, root = Path(path), Path(root)
    parts = path.relative_to(root).parts
    if not path.is_absolute() or '..' in path.parts or len(parts) < 2 or path.name != 'SKILL.md':
        raise ValueError('invalid materialized skill path')
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open('/', directory_flags)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            info = os.fstat(file_descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > _SKILL_BYTES_LIMIT):
                raise ValueError('materialized skill is not a bounded regular file with one link')
            with os.fdopen(os.dup(file_descriptor), 'rb') as stream:
                raw = stream.read(_SKILL_BYTES_LIMIT + 1)
            current = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            if (info.st_dev != current.st_dev or info.st_ino != current.st_ino or current.st_nlink != 1
                    or info.st_mtime_ns != current.st_mtime_ns or info.st_ctime_ns != current.st_ctime_ns
                    or len(raw) != info.st_size or info.st_size != current.st_size):
                raise ValueError('materialized skill changed while reading')
            return raw
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _native_skill_result(raw):
    if isinstance(raw, str):
        if len(raw) > 1_048_576 or len(raw.encode()) > 1_048_576:
            raise ValueError('native skill result exceeds limit')
        return json.loads(raw)
    return raw


def _verified_native_skill_source(result, *, path, root, skill_sha256, name):
    """Bind either Hermes result schema to the exact owned bytes selected by the parent."""
    path, root = Path(path), Path(root)
    if (not isinstance(result, dict) or result.get('success') is not True
            or result.get('setup_needed') is not False or not isinstance(name, str)
            or not name or result.get('name') != name):
        raise ValueError('native skill is not the ready selected skill')
    expected = {'path': path.relative_to(root).as_posix(), 'skill_dir': str(path.parent)}
    if '_source_path' in result:
        if result['_source_path'] != str(path) or any(
                key in result and result[key] != value for key, value in expected.items()):
            raise ValueError('legacy native skill source differs')
    elif any(result.get(key) != value for key, value in expected.items()):
        raise ValueError('native skill path or directory differs')
    content = result.get('content')
    if not isinstance(content, str) or len(content) > _SKILL_BYTES_LIMIT:
        raise ValueError('invalid native skill content')
    owned = _read_owned_skill(path, root)
    if content.encode() != owned or hashlib.sha256(owned).hexdigest() != skill_sha256:
        raise ValueError('native skill bytes differ')
    return str(path)
