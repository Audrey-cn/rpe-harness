"""Exact execution authority and immutable, independently verifiable plans.

Discovery metadata and host descriptions never register an executor. Plans bind
execution semantics; event/session IDs and natural descriptions remain metadata.
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from .mcsi import CausalState, TaskType
from .path_binding import current_binding, read_source

CAPABILITY = 'tabular.deduplicate'
CONTRACT_VERSION = 1
EXECUTORS = {'csv': 'builtin/csv-deduplicate', 'xlsx': 'builtin/xlsx-deduplicate'}
CONSTRAINTS = frozenset({'preserve_original', 'preserve_order', 'preserve_formulas'})
_OPTIONS = frozenset({'input_path', 'output_path', 'key_columns', 'sheet', 'keep', 'encoding', 'delimiter'})
_METADATA = frozenset({'goal', 'source_ref', 'session_id'})


def _revision(name: str) -> str:
    return hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()


# Loaded trusted code revisions are stable for this interpreter, including when
# another development process edits a source file. Candidates cannot alter them.
EXECUTOR_REVISION = _revision('execution.py')
VERIFIER_REVISION = _revision('verification.py')


def resolve_executor(capability: str, input_format: str, contract_version: int = 1) -> str:
    if capability != CAPABILITY or type(contract_version) is not int or contract_version != CONTRACT_VERSION:
        raise ValueError('capability contract is not registered')
    if input_format not in EXECUTORS:
        raise ValueError('input must be a supported csv or xlsx file')
    return EXECUTORS[input_format]


def _header(header: list[Any], columns: tuple[str, ...]) -> None:
    if (not header or any(not isinstance(item, str) or not item.strip() for item in header)
            or len(set(header)) != len(header)):
        raise ValueError('header must contain unique nonempty column names')
    if set(columns) - set(header):
        raise ValueError('key columns not found in input')


def _probe(data: bytes, fmt: str, columns: tuple[str, ...], sheet: str | None,
           encoding: str, delimiter: str) -> str | None:
    if fmt == 'csv':
        if sheet is not None:
            raise ValueError('sheet is only supported for xlsx')
        try:
            records = list(csv.reader(io.StringIO(data.decode(encoding), newline=''),
                                      delimiter=delimiter, strict=True))
        except (UnicodeError, csv.Error) as error:
            raise ValueError('invalid csv input') from error
        if not records or any(len(row) != len(records[0]) for row in records[1:]):
            raise ValueError('invalid csv input shape')
        _header(records[0], columns)
        return None
    from openpyxl import load_workbook
    try:
        workbook = load_workbook(io.BytesIO(data), data_only=False, keep_links=False)
    except (ValueError, KeyError, BadZipFile, OSError) as error:
        raise ValueError('invalid xlsx input') from error
    try:
        selected = sheet or workbook.active.title
        if selected not in workbook.sheetnames:
            raise ValueError('requested sheet does not exist')
        _header([cell.value for cell in workbook[selected][1]], columns)
        return selected
    finally:
        workbook.close()


@dataclass(frozen=True)
class ExecutionPlan:
    capability: str
    contract_version: int
    executor_id: str
    executor_revision: str
    verifier_revision: str
    input_path: str
    output_path: str
    input_format: str
    input_sha256: str
    key_columns: tuple[str, ...]
    constraints: tuple[str, ...]
    sheet: str | None
    keep: str
    encoding: str
    delimiter: str
    goal: str
    source_ref: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.executor_id != resolve_executor(self.capability, self.input_format, self.contract_version):
            raise ValueError('executor does not match registered capability contract')
        for name in ('executor_revision', 'verifier_revision', 'input_sha256'):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
                raise ValueError(f'{name} must be a SHA256')
        for name in ('input_path', 'output_path'):
            value = getattr(self, name)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError(f'{name} must be an absolute path')
        if self.input_path == self.output_path:
            raise ValueError('input and output must differ')
        if (not isinstance(self.key_columns, tuple) or not self.key_columns
                or any(not isinstance(item, str) or not item.strip() for item in self.key_columns)
                or len(set(self.key_columns)) != len(self.key_columns)):
            raise ValueError('key_columns must be unique nonempty immutable names')
        if (not isinstance(self.constraints, tuple)
                or any(not isinstance(item, str) for item in self.constraints)
                or set(self.constraints) - CONSTRAINTS):
            raise ValueError('unsupported immutable constraints')
        if self.input_format == 'csv' and 'preserve_formulas' in self.constraints:
            raise ValueError('preserve_formulas requires xlsx')
        if self.sheet is not None and (not isinstance(self.sheet, str) or not self.sheet):
            raise ValueError('sheet must be a nonempty string')
        if self.input_format == 'csv' and self.sheet is not None:
            raise ValueError('csv cannot select a sheet')
        if self.keep != 'first':
            raise ValueError('only keep=first is supported')
        if not isinstance(self.encoding, str):
            raise ValueError('encoding must be a string')
        try:
            codecs.lookup(self.encoding)
        except LookupError as error:
            raise ValueError('unsupported encoding') from error
        if not isinstance(self.delimiter, str) or len(self.delimiter) != 1 or self.delimiter in '\r\n\x00':
            raise ValueError('delimiter must be a single non-newline character')
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError('goal must be nonempty')
        for name in ('source_ref', 'session_id'):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f'{name} must be a nonempty string or null')

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result['key_columns'] = list(self.key_columns)
        result['constraints'] = list(self.constraints)
        return result

    def semantic_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.to_dict().items() if key not in _METADATA}

    @property
    def sha256(self) -> str:
        encoded = json.dumps(self.semantic_dict(), sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    def execution_kwargs(self) -> dict[str, Any]:
        return {'input_path': Path(self.input_path), 'output_path': Path(self.output_path),
                'key_columns': list(self.key_columns), 'constraints': list(self.constraints),
                'sheet': self.sheet, 'keep': self.keep, 'encoding': self.encoding,
                'delimiter': self.delimiter}

    def verification_parameters(self) -> dict[str, Any]:
        result = self.execution_kwargs()
        result.update(input_path=self.input_path, output_path=self.output_path,
                      input_sha256=self.input_sha256, format=self.input_format)
        return result

    def matches_prepared(self, prepared: dict[str, Any]) -> bool:
        return all(prepared.get(key) == value for key, value in self.verification_parameters().items())

    @classmethod
    def from_dict(cls, saved: dict[str, Any]) -> 'ExecutionPlan':
        """Reconstruct persisted semantics without reinterpreting current files."""
        if not isinstance(saved, dict) or set(saved) != {item.name for item in fields(cls)}:
            raise ValueError('persisted plan fields are incomplete or unknown')
        values = dict(saved)
        for name in ('key_columns', 'constraints'):
            if not isinstance(values[name], (list, tuple)):
                raise ValueError(f'{name} must be a sequence')
            values[name] = tuple(values[name])
        return cls(**values)

    @classmethod
    def from_request(cls, payload: dict[str, Any]) -> 'ExecutionPlan':
        if not isinstance(payload, dict) or type(payload.get('schema_version')) is not int:
            raise ValueError('request must have an integer schema_version')
        version = payload['schema_version']
        if version not in (1, 2):
            raise ValueError('schema_version must be 1 or 2')
        if not isinstance(payload.get('event_id'), str) or not payload['event_id'].strip():
            raise ValueError('event_id is required')
        params = payload.get('execution')
        if not isinstance(params, dict) or set(params) - _OPTIONS:
            raise ValueError('execution must have only registered parameters')
        if version == 1:
            state = payload.get('state')
            if not isinstance(state, dict):
                raise ValueError('state must be a JSON object')
            causal = CausalState.from_event(state)
            if causal.task_type not in (TaskType.DATA_PROCESSING, TaskType.FILE_OPERATION):
                raise ValueError('unsupported task type for deduplication')
            capability, goal = causal.required_capability, causal.goal
            constraints, format_hint = causal.constraints, causal.input_format
            source_ref, session_id = causal.source_ref, causal.session_id or payload.get('session_id')
        else:
            unknown = set(payload) - {'schema_version', 'event_id', 'capability', 'contract_version',
                                      'goal', 'constraints', 'execution', 'source_ref', 'session_id',
                                      'task_type', 'input_format'}
            if unknown:
                raise ValueError(f'unknown request fields: {sorted(unknown)}')
            capability, goal = payload.get('capability'), payload.get('goal')
            constraints, format_hint = payload.get('constraints', []), payload.get('input_format')
            source_ref, session_id = payload.get('source_ref', payload['event_id']), payload.get('session_id')
            if 'task_type' in payload and not isinstance(payload['task_type'], str):
                raise ValueError('task_type metadata must be a string')
        if not isinstance(constraints, list) or any(not isinstance(item, str) for item in constraints):
            raise ValueError('constraints must be a list of strings')
        columns = params.get('key_columns')
        if not isinstance(columns, list):
            raise ValueError('key_columns must be a list')
        source, target = params.get('input_path'), params.get('output_path')
        if any(not isinstance(path, str) or not Path(path).is_absolute() for path in (source, target)):
            raise ValueError('input_path and output_path must be absolute paths')
        source_path, target_path = Path(source), Path(target)
        binding = current_binding()
        if binding is not None:
            binding.require_paths(source_path, target_path)
        else:
            if not source_path.is_file() or source_path.is_symlink():
                raise ValueError('input must be an existing regular file, not a symlink')
            if not target_path.parent.is_dir() or os.path.lexists(target_path):
                raise ValueError('output must be a new path in an existing directory')
            source_path = source_path.resolve(strict=True)
            target_path = target_path.parent.resolve(strict=True) / target_path.name
        fmt = source_path.suffix.lower().lstrip('.')
        if target_path.suffix.lower() != '.' + fmt:
            raise ValueError('input/output extensions must match')
        if format_hint is not None and format_hint != fmt:
            raise ValueError('declared input format does not match probed input')
        contract_version = payload.get('contract_version', CONTRACT_VERSION)
        plan = cls(capability=capability, contract_version=contract_version,
                   executor_id=resolve_executor(capability, fmt, contract_version),
                   executor_revision=EXECUTOR_REVISION, verifier_revision=VERIFIER_REVISION,
                   input_path=str(source_path), output_path=str(target_path), input_format=fmt,
                   input_sha256=hashlib.sha256(read_source(source_path)).hexdigest(),
                   key_columns=tuple(columns), constraints=tuple(sorted(set(constraints))),
                   sheet=params.get('sheet'), keep=params.get('keep', 'first'),
                   encoding=params.get('encoding', 'utf-8-sig'), delimiter=params.get('delimiter', ','),
                   goal=goal, source_ref=source_ref, session_id=session_id)
        data = read_source(source_path)
        if hashlib.sha256(data).hexdigest() != plan.input_sha256:
            raise ValueError('input changed during contract probing')
        selected = _probe(data, fmt, plan.key_columns, plan.sheet, plan.encoding, plan.delimiter)
        if selected != plan.sheet:
            saved = plan.to_dict()
            saved['sheet'] = selected
            plan = cls.from_dict(saved)
        return plan


def request_schema() -> dict[str, Any]:
    """Model-independent v2 execution request schema."""
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['schema_version', 'event_id', 'capability', 'goal', 'execution'],
        'properties': {
            'schema_version': {'type': 'integer', 'const': 2},
            'event_id': {'type': 'string', 'minLength': 1},
            'capability': {'type': 'string', 'const': CAPABILITY},
            'contract_version': {'type': 'integer', 'const': CONTRACT_VERSION},
            'goal': {'type': 'string', 'minLength': 1},
            'source_ref': {'type': 'string', 'minLength': 1},
            'session_id': {'type': 'string', 'minLength': 1},
            'task_type': {'type': 'string', 'description': 'Advisory metadata only.'},
            'input_format': {'type': 'string', 'enum': ['csv', 'xlsx']},
            'constraints': {'type': 'array', 'items': {'type': 'string', 'enum': sorted(CONSTRAINTS)}},
            'execution': {
                'type': 'object', 'additionalProperties': False,
                'required': ['input_path', 'output_path', 'key_columns'],
                'properties': {
                    'input_path': {'type': 'string', 'minLength': 1},
                    'output_path': {'type': 'string', 'minLength': 1},
                    'key_columns': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                                    'items': {'type': 'string', 'minLength': 1}},
                    'sheet': {'type': 'string', 'minLength': 1},
                    'keep': {'type': 'string', 'enum': ['first']},
                    'encoding': {'type': 'string'}, 'delimiter': {'type': 'string', 'minLength': 1, 'maxLength': 1},
                },
            },
        },
    }
