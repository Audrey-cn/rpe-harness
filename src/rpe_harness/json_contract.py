"""Exact immutable json.normalize/v1 authority, independent of discovery metadata."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .path_binding import current_binding, read_source
from .json_execution import EXECUTOR_REVISION
from .json_verification import VERIFIER_REVISION

CONTRACT_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
CAPABILITY = 'json.normalize'
CONTRACT_VERSION = 1
EXECUTOR_ID = 'builtin/json-normalize'
CONSTRAINTS = frozenset({'preserve_original', 'preserve_order'})
MAX_INPUT_BYTES = 1024 * 1024
_METADATA = frozenset({'goal', 'source_ref', 'session_id'})
_REQUEST_FIELDS = frozenset({'schema_version', 'event_id', 'capability', 'contract_version',
                            'goal', 'constraints', 'execution', 'source_ref', 'session_id',
                            'task_type', 'input_format'})


# These identify the trusted implementations loaded by this interpreter. Editing
# source while another interpreter is running cannot silently change its plans.


def current_revisions():
    return {'executor_revision': EXECUTOR_REVISION, 'verifier_revision': VERIFIER_REVISION}


def validate_constraints(value):
    if (not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value)
            or set(value) - CONSTRAINTS):
        raise ValueError('unsupported JSON constraints')
    return tuple(sorted(set(value)))


def validate_paths(source, output):
    for value in (source, output):
        if (not isinstance(value, str) or not value or '\x00' in value
                or not Path(value).is_absolute() or '..' in Path(value).parts):
            raise ValueError('JSON paths must be absolute without traversal or NUL')
        if Path(value).suffix.lower() != '.json':
            raise ValueError('JSON input and output must have .json extensions')
    source, output = Path(source), Path(output)
    if source == output:
        raise ValueError('JSON input and output must differ')
    binding = current_binding()
    if binding is not None:
        binding.require_paths(source, output)
        return source, output
    # Inspect the submitted chain before resolve could erase symlink evidence.
    for path in (source, output.parent):
        cursor = Path(path.anchor)
        for part in path.parts[1:]:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError('JSON paths cannot contain symlinks')
    if not source.is_file():
        raise ValueError('JSON input must be an existing regular file')
    if not output.parent.is_dir() or os.path.lexists(output):
        raise ValueError('JSON output must be a new path in an existing directory')
    return source.resolve(strict=True), output.parent.resolve(strict=True) / output.name


@dataclass(frozen=True)
class JsonNormalizePlan:
    capability: str
    contract_version: int
    executor_id: str
    executor_revision: str
    verifier_revision: str
    input_path: str
    output_path: str
    input_format: str
    input_sha256: str
    style: str
    constraints: tuple[str, ...]
    goal: str
    source_ref: str | None = None
    session_id: str | None = None

    def __post_init__(self):
        if (self.capability != CAPABILITY or type(self.contract_version) is not int
                or self.contract_version != CONTRACT_VERSION or self.executor_id != EXECUTOR_ID
                or self.input_format != 'json'):
            raise ValueError('JSON capability contract is not registered')
        for name in ('executor_revision', 'verifier_revision', 'input_sha256'):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
                raise ValueError(name + ' must be a lowercase SHA256')
        for name in ('input_path', 'output_path'):
            value = getattr(self, name)
            if (not isinstance(value, str) or not Path(value).is_absolute() or '\x00' in value
                    or '..' in Path(value).parts or Path(value).suffix.lower() != '.json'):
                raise ValueError(name + ' must be an absolute .json path without traversal')
        if self.input_path == self.output_path:
            raise ValueError('JSON input and output must differ')
        if not isinstance(self.style, str) or self.style not in {'pretty', 'minify'}:
            raise ValueError('JSON style must be pretty or minify')
        if (not isinstance(self.constraints, tuple)
                or validate_constraints(self.constraints) != self.constraints):
            raise ValueError('JSON constraints must be canonical and immutable')
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError('JSON goal must be nonempty')
        for name in ('source_ref', 'session_id'):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(name + ' must be a nonempty string or null')

    def to_dict(self):
        result = asdict(self)
        result['constraints'] = list(self.constraints)
        return result

    def semantic_dict(self):
        return {key: value for key, value in self.to_dict().items() if key not in _METADATA}

    @property
    def sha256(self):
        raw = json.dumps(self.semantic_dict(), sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def execution_kwargs(self):
        return {'input_path': Path(self.input_path), 'output_path': Path(self.output_path),
                'input_sha256': self.input_sha256, 'style': self.style,
                'constraints': list(self.constraints)}

    def verification_parameters(self):
        return {'input_path': self.input_path, 'output_path': self.output_path,
                'input_sha256': self.input_sha256, 'format': self.input_format,
                'style': self.style, 'constraints': list(self.constraints)}

    def matches_prepared(self, prepared):
        return all(prepared.get(key) == value for key, value in self.verification_parameters().items())

    @classmethod
    def from_dict(cls, saved):
        if not isinstance(saved, dict) or set(saved) != {item.name for item in fields(cls)}:
            raise ValueError('persisted JSON plan fields are incomplete or unknown')
        values = dict(saved)
        if not isinstance(values['constraints'], (list, tuple)):
            raise ValueError('persisted JSON constraints must be a sequence')
        values['constraints'] = tuple(values['constraints'])
        return cls(**values)

    @classmethod
    def from_request(cls, payload):
        if (not isinstance(payload, dict) or type(payload.get('schema_version')) is not int
                or payload['schema_version'] != 2 or set(payload) - _REQUEST_FIELDS):
            raise ValueError('JSON requires an exact schema_version=2 request')
        if not isinstance(payload.get('event_id'), str) or not payload['event_id'].strip():
            raise ValueError('JSON event_id is required')
        params = payload.get('execution')
        if not isinstance(params, dict) or set(params) != {'input_path', 'output_path', 'style'}:
            raise ValueError('JSON execution requires only input_path, output_path and style')
        if not isinstance(payload.get('constraints', []), list):
            raise ValueError('JSON constraints must be a list of strings')
        constraints = validate_constraints(payload.get('constraints', []))
        if 'task_type' in payload and not isinstance(payload['task_type'], str):
            raise ValueError('JSON task_type metadata must be a string')
        if 'input_format' in payload and payload['input_format'] != 'json':
            raise ValueError('JSON input_format must match .json input')
        source, output = validate_paths(params['input_path'], params['output_path'])
        raw = read_source(source, max_bytes=MAX_INPUT_BYTES)
        plan = cls(capability=payload.get('capability'),
                   contract_version=payload.get('contract_version', CONTRACT_VERSION),
                   executor_id=EXECUTOR_ID, executor_revision=EXECUTOR_REVISION,
                   verifier_revision=VERIFIER_REVISION, input_path=str(source), output_path=str(output),
                   input_format='json', input_sha256=hashlib.sha256(raw).hexdigest(),
                   style=params['style'], constraints=constraints, goal=payload.get('goal'),
                   source_ref=payload.get('source_ref', payload['event_id']),
                   session_id=payload.get('session_id'))
        from .json_verification import inspect_json_bytes
        inspect_json_bytes(raw)
        return plan
