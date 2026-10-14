"""Exact xlsx.inspect/v1 plan and fixed-argv execution of the original XLSX helper."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from zipfile import BadZipFile

from .execution import _new_staging, cleanup_staging
from .path_binding import current_binding, read_source
from .skill_catalog import (_absolute, _canonical, _read, validate_skill_binding,
                            BINDING_FIELDS, XLSX_SKILL_ID, XLSX_SKILL_SHA256, XLSX_SCRIPT_SHA256)
from .xlsx_resource import preflight_xlsx, RESOURCE_REVISION

CAPABILITY = 'xlsx.inspect'
CONTRACT_VERSION = 1
EXECUTOR_ID = 'builtin/xlsx-inspect'
EXECUTOR_REVISION = hashlib.sha256(Path(__file__).read_bytes() + RESOURCE_REVISION.encode()).hexdigest()
CONTRACT_REVISION = EXECUTOR_REVISION
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 2048
MAX_SHEET_GRID = 100000
MAX_WORKBOOK_GRID = 200000
MODES = {'rows': '--json', 'sheets': '--sheets', 'formulas': '--formulas'}


def current_revisions():
    from .skill_verification import VERIFIER_REVISION
    return {'executor_revision': EXECUTOR_REVISION, 'verifier_revision': VERIFIER_REVISION}


def inspect_xlsx_bytes(raw):
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('workbook exceeds 16 MiB')
    try:
        preflight_xlsx(raw)
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(raw), data_only=False, keep_links=False)
        try:
            grid = [sheet.max_row * sheet.max_column for sheet in book.worksheets]
            if any(cells > MAX_SHEET_GRID for cells in grid) or sum(grid) > MAX_WORKBOOK_GRID:
                raise ValueError('workbook grid exceeds 100000 cells/sheet or 200000 cells/workbook')
            return book.sheetnames, book.active.title
        finally:
            book.close()
    except (BadZipFile, KeyError, OSError, TypeError) as error:
        raise ValueError('invalid XLSX workbook') from error


def validate_paths(source, output):
    if not isinstance(source, str) or not isinstance(output, str):
        raise ValueError('inspection paths must be strings')
    if not Path(source).is_absolute() or not Path(output).is_absolute():
        raise ValueError('inspection paths must be absolute')
    if Path(source).suffix.lower() != '.xlsx' or Path(output).suffix.lower() != '.json':
        raise ValueError('inspection requires .xlsx input and .json output')
    binding = current_binding()
    if binding is not None:
        binding.require_paths(Path(source), Path(output))
        return Path(source), Path(output)
    source, output = _absolute(source), _absolute(output)
    if not source.is_file() or not output.parent.is_dir() or os.path.lexists(output):
        raise ValueError('input must exist and output must be a new path in an existing directory')
    return source, output


@dataclass(frozen=True)
class SkillInspectionPlan:
    capability: str
    contract_version: int
    executor_id: str
    executor_revision: str
    verifier_revision: str
    input_path: str
    output_path: str
    input_format: str
    input_sha256: str
    mode: str
    sheet: str | None
    skill_binding: tuple[tuple[str, str], ...]
    constraints: tuple[str, ...]
    goal: str
    source_ref: str | None = None
    session_id: str | None = None

    def __post_init__(self):
        if (self.capability != CAPABILITY or type(self.contract_version) is not int
                or self.contract_version != 1 or self.executor_id != EXECUTOR_ID
                or self.input_format != 'xlsx'):
            raise ValueError('inspection capability contract is not registered')
        for name in ('executor_revision', 'verifier_revision', 'input_sha256'):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError(name + ' must be SHA256')
        for name, suffix in (('input_path', '.xlsx'), ('output_path', '.json')):
            value = getattr(self, name)
            if (not isinstance(value, str) or not Path(value).is_absolute() or '\x00' in value
                    or '..' in Path(value).parts or Path(value).suffix.lower() != suffix):
                raise ValueError('invalid inspection path')
        if not isinstance(self.mode, str) or self.mode not in MODES:
            raise ValueError('inspection mode must be rows, sheets or formulas')
        if self.sheet is not None and (not isinstance(self.sheet, str) or not self.sheet.strip()):
            raise ValueError('sheet must be a nonempty name or null')
        if self.mode == 'sheets' and self.sheet is not None:
            raise ValueError('sheets inventory does not accept a selected sheet')
        if (type(self.skill_binding) is not tuple or any(type(pair) is not tuple or len(pair) != 2
                for pair in self.skill_binding) or len(dict(self.skill_binding)) != len(self.skill_binding)):
            raise ValueError('skill binding must be immutable and unique')
        validate_skill_binding(dict(self.skill_binding))
        if (type(self.constraints) is not tuple or any(not isinstance(v, str) for v in self.constraints)
                or set(self.constraints) - {'preserve_original'}
                or tuple(sorted(set(self.constraints))) != self.constraints):
            raise ValueError('inspection only supports preserve_original')
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError('inspection goal is required')
        for name in ('source_ref', 'session_id'):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(name + ' must be a nonempty string or null')

    def to_dict(self):
        result = asdict(self)
        result['skill_binding'] = dict(self.skill_binding)
        result['constraints'] = list(self.constraints)
        return result

    def semantic_dict(self):
        return {key: value for key, value in self.to_dict().items() if key not in {'goal', 'source_ref', 'session_id'}}

    @property
    def sha256(self):
        return hashlib.sha256(_canonical(self.semantic_dict())).hexdigest()

    def execution_kwargs(self):
        return {'input_path': Path(self.input_path), 'output_path': Path(self.output_path),
                'input_sha256': self.input_sha256, 'mode': self.mode, 'sheet': self.sheet,
                'skill_binding': dict(self.skill_binding), 'constraints': list(self.constraints)}

    def verification_parameters(self):
        return {'input_path': self.input_path, 'output_path': self.output_path, 'input_sha256': self.input_sha256,
                'format': self.input_format, 'mode': self.mode, 'sheet': self.sheet,
                'skill_binding': dict(self.skill_binding), 'constraints': list(self.constraints)}

    def matches_prepared(self, prepared):
        return all(prepared.get(key) == value for key, value in self.verification_parameters().items())

    @classmethod
    def from_dict(cls, saved):
        if type(saved) is not dict or set(saved) != {field.name for field in fields(cls)}:
            raise ValueError('persisted inspection plan is incomplete or has unknown fields')
        values = dict(saved)
        if type(values['skill_binding']) is not dict or not isinstance(values['constraints'], (list, tuple)):
            raise ValueError('persisted inspection binding or constraints invalid')
        values['skill_binding'] = tuple(sorted(values['skill_binding'].items()))
        values['constraints'] = tuple(values['constraints'])
        return cls(**values)

    @classmethod
    def from_request(cls, payload):
        allowed = {'schema_version', 'event_id', 'capability', 'contract_version', 'goal', 'constraints',
                   'execution', 'source_ref', 'session_id', 'task_type', 'input_format'}
        if type(payload) is not dict or set(payload) - allowed or type(payload.get('schema_version')) is not int or payload['schema_version'] != 2:
            raise ValueError('inspection requires exact schema_version=2 request')
        if not isinstance(payload.get('event_id'), str) or not payload['event_id'].strip():
            raise ValueError('inspection event_id is required')
        params = payload.get('execution')
        if (type(params) is not dict or set(params) - {'input_path', 'output_path', 'mode', 'sheet', 'skill_binding'}
                or not {'input_path', 'output_path', 'mode', 'skill_binding'} <= set(params)):
            raise ValueError('inspection execution parameters are incomplete or unknown')
        if type(params['skill_binding']) is not dict:
            raise ValueError('inspection requires explicit skill binding')
        if payload.get('input_format', 'xlsx') != 'xlsx':
            raise ValueError('inspection input format must be xlsx')
        normalized_contract(payload)
        constraints = payload.get('constraints', [])
        if type(constraints) is not list or any(not isinstance(v, str) for v in constraints):
            raise ValueError('inspection constraints must be strings')
        source, output = validate_paths(params['input_path'], params['output_path'])
        raw = read_source(source, max_bytes=MAX_INPUT_BYTES)
        names, _ = inspect_xlsx_bytes(raw)
        sheet = params.get('sheet')
        if params['mode'] in {'rows', 'formulas'}:
            if sheet is not None and sheet not in names:
                raise ValueError('selected sheet does not exist')
        return cls(capability=payload.get('capability'), contract_version=payload.get('contract_version', 1),
                   executor_id=EXECUTOR_ID, **current_revisions(), input_path=str(source), output_path=str(output),
                   input_format='xlsx', input_sha256=hashlib.sha256(raw).hexdigest(), mode=params['mode'],
                   sheet=sheet, skill_binding=tuple(sorted(params['skill_binding'].items())),
                   constraints=tuple(sorted(set(constraints))), goal=payload.get('goal'),
                   source_ref=payload.get('source_ref', payload['event_id']), session_id=payload.get('session_id'))


def contract_from_plan(plan):
    return {'capability': plan.capability, 'contract_version': plan.contract_version,
            'constraints': list(plan.constraints), 'execution': {'input_path': plan.input_path,
            'output_path': plan.output_path, 'mode': plan.mode, 'sheet': plan.sheet,
            'skill_binding': dict(plan.skill_binding)}}


def normalized_contract(payload):
    """Canonical business identity; safe after publication, without any filesystem reads."""
    allowed = {'schema_version', 'event_id', 'capability', 'contract_version', 'goal', 'constraints',
               'execution', 'source_ref', 'session_id', 'task_type', 'input_format'}
    if (type(payload) is not dict or set(payload) - allowed
            or payload.get('capability') != CAPABILITY
            or type(payload.get('contract_version', 1)) is not int
            or payload.get('contract_version', 1) != 1
            or payload.get('input_format', 'xlsx') != 'xlsx'):
        raise ValueError('inspection contract shape or capability is invalid')
    params = payload.get('execution')
    if (type(params) is not dict or set(params) - {'input_path', 'output_path', 'mode', 'sheet', 'skill_binding'}
            or not {'input_path', 'output_path', 'mode', 'skill_binding'} <= set(params)):
        raise ValueError('inspection execution shape is invalid')
    mode, sheet = params['mode'], params.get('sheet')
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('inspection mode is invalid')
    if sheet is not None and (not isinstance(sheet, str) or not sheet.strip() or mode == 'sheets'):
        raise ValueError('inspection sheet is invalid')
    for name, suffix in (('input_path', '.xlsx'), ('output_path', '.json')):
        path = params[name]
        if (not isinstance(path, str) or not Path(path).is_absolute() or '\x00' in path
                or '..' in Path(path).parts or Path(path).suffix.lower() != suffix):
            raise ValueError('inspection contract path is invalid')
    binding = params['skill_binding']
    if type(binding) is not dict or set(binding) != BINDING_FIELDS:
        raise ValueError('inspection binding shape is invalid')
    if (binding['skill_id'] != XLSX_SKILL_ID or binding['skill_sha256'] != XLSX_SKILL_SHA256
            or binding['script_sha256'] != XLSX_SCRIPT_SHA256
            or any(not isinstance(value, str) for value in binding.values())
            or len(binding['snapshot_id']) != 64
            or any(c not in '0123456789abcdef' for c in binding['snapshot_id'])
            or not Path(binding['script_path']).is_absolute() or '..' in Path(binding['script_path']).parts
            or '\x00' in binding['script_path']):
        raise ValueError('inspection binding identity is invalid')
    constraints = payload.get('constraints', [])
    if (type(constraints) is not list or any(not isinstance(v, str) for v in constraints)
            or set(constraints) - {'preserve_original'}):
        raise ValueError('inspection constraints are invalid')
    return {'capability': CAPABILITY, 'contract_version': 1, 'constraints': sorted(set(constraints)),
            'execution': {'input_path': params['input_path'], 'output_path': params['output_path'],
                          'mode': mode, 'sheet': sheet, 'skill_binding': dict(binding)}}


def _run_helper(raw, helper, binding, mode, sheet):
    if (type(helper) is not bytes or len(helper) > 65536
            or hashlib.sha256(helper).hexdigest() != binding['script_sha256']):
        raise ValueError('helper bytes changed before the fixed native invocation')
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise RuntimeError('native helper filesystem/network sandbox unavailable')
    from .evolution.sandbox import (_limits, _profile, _resident_bytes, MAX_RESIDENT_BYTES,
                                    RSS_POLL_SECONDS)
    owned_root = Path(binding['script_path']).parents[6]
    _absolute(owned_root)
    directory = owned_root / 'skill-sandboxes'
    _absolute(directory)
    directory.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='inspect-', dir=directory) as temporary:
        job = Path(temporary)
        source, script = job / 'input.xlsx', job / 'xlsx_read.py'
        source.write_bytes(raw)
        script.write_bytes(helper)
        source.chmod(0o400)
        script.chmod(0o400)
        stdout, stderr = job / 'stdout.json', job / 'stderr.log'
        argv = ['/usr/bin/sandbox-exec', '-p', _profile(job, stdout, stderr),
                sys.executable, '-I', '-B', str(script), str(source), MODES[mode]]
        if sheet is not None:
            argv.extend(['--sheet', sheet])
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(job), 'TMPDIR': str(job),
               'LANG': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
        started = time.monotonic()
        with stdout.open('wb') as outgoing, stderr.open('wb') as error_log:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=outgoing, stderr=error_log,
                    cwd=job, env=env, start_new_session=True, preexec_fn=lambda: _limits(5))
            try:
                while process.poll() is None:
                    resident = _resident_bytes(process.pid)
                    if resident is not None and resident > MAX_RESIDENT_BYTES:
                        raise RuntimeError('native helper RSS soft threshold exceeded')
                    remaining = 5 - (time.monotonic() - started)
                    if remaining <= 0:
                        raise RuntimeError('native helper exceeded 5-second budget')
                    try:
                        process.wait(timeout=min(remaining, RSS_POLL_SECONDS))
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if process.returncode != 0:
            raise RuntimeError('native helper failed: ' + _read(stderr, MAX_OUTPUT_BYTES)[-1000:].decode('utf-8', errors='replace'))
        if _read(source, MAX_INPUT_BYTES) != raw or _read(script) != helper:
            raise RuntimeError('native helper changed its frozen input or script')
        return _read(stdout, MAX_OUTPUT_BYTES), {'helper_pid': process.pid,
                    'duration_ms': (time.monotonic() - started) * 1000,
                    'script_sha256': binding['script_sha256'], 'argv_mode': MODES[mode],
                    'network': 'denied', 'fork': 'denied', 'memory_enforcement': 'rss_monitor_soft'}


def prepare_skill_inspection(input_path, output_path, *, mode, skill_binding, sheet=None,
                             input_sha256=None, constraints=None):
    source, output = validate_paths(str(input_path), str(output_path))
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('invalid inspection mode')
    if constraints is not None and (type(constraints) is not list or set(constraints) - {'preserve_original'}):
        raise ValueError('invalid inspection constraints')
    if sheet is not None and (not isinstance(sheet, str) or not sheet.strip() or mode == 'sheets'):
        raise ValueError('invalid inspection sheet')
    binding = dict(validate_skill_binding(skill_binding))
    raw = read_source(source, max_bytes=MAX_INPUT_BYTES)
    source_hash = hashlib.sha256(raw).hexdigest()
    if input_sha256 is not None and source_hash != input_sha256:
        raise ValueError('inspection input differs from immutable plan')
    names, _ = inspect_xlsx_bytes(raw)
    if sheet is not None and sheet not in names:
        raise ValueError('selected sheet does not exist')
    helper = _read(binding['script_path'])
    artifact, receipt = _run_helper(raw, helper, binding, mode, sheet)
    prepared = {'input_path': str(source), 'output_path': str(output), 'input_sha256': source_hash,
                'format': 'xlsx', 'mode': mode, 'sheet': sheet, 'skill_binding': binding,
                'constraints': sorted(set(constraints or [])), 'helper_execution': receipt}
    try:
        staging = _new_staging(source, output, source_hash)
        prepared['staging_path'] = str(staging)
        with staging.open('wb') as handle:
            handle.write(artifact)
            handle.flush()
            os.fsync(handle.fileno())
        return prepared
    except BaseException:
        cleanup_staging(prepared)
        raise
