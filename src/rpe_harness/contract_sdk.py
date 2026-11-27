"""Declarative Contract Plugin SDK (rpe.contract/v1) and built-in developer contracts.

Third-party packages can expose custom verified contracts via Python entry points:
    [project.entry-points."rpe_harness.contracts"]
    my_contract = "my_pkg.contracts:PLUGIN"

Every plugin provides deterministic execution into a staged file and an independent
oracle verifier. Publication still goes through RPE's atomic no-overwrite pipeline.
"""
from __future__ import annotations

import codecs
import copy
import csv
import hashlib
import inspect
import io
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, fields
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

from .path_binding import current_binding, read_source
from .storage import canonical_json

ENTRY_POINT_GROUP = 'rpe_harness.contracts'
MAX_PLUGIN_INPUT_BYTES = 4 * 1024 * 1024
_METADATA = frozenset({'goal', 'source_ref', 'session_id'})
_REQUEST_FIELDS = frozenset({
    'schema_version', 'event_id', 'capability', 'contract_version',
    'goal', 'constraints', 'execution', 'source_ref', 'session_id',
    'task_type', 'input_format',
})
_DEFAULT_CONSTRAINTS = frozenset({'preserve_original', 'preserve_order'})


def _callable_revision(fn: Callable[..., Any], fallback_label: str) -> str:
    try:
        src = inspect.getsource(fn).encode('utf-8')
    except (OSError, TypeError):
        src = f'{fn.__module__}.{getattr(fn, "__qualname__", fallback_label)}'.encode('utf-8')
    return hashlib.sha256(src).hexdigest()


def _validate_plugin_paths(
    source_raw: Any,
    output_raw: Any,
    allowed_formats: tuple[str, ...],
    output_formats: tuple[str, ...] | None = None,
) -> tuple[Path, Path, str]:
    for value in (source_raw, output_raw):
        if (not isinstance(value, str) or not value or '\x00' in value
                or not Path(value).is_absolute() or '..' in Path(value).parts):
            raise ValueError('contract paths must be absolute without traversal or NUL')
    source, output = Path(source_raw), Path(output_raw)
    fmt = source.suffix.lower().lstrip('.')
    out_fmt = output.suffix.lower().lstrip('.')
    if fmt not in allowed_formats:
        raise ValueError(f'input must match allowed formats: {list(allowed_formats)}')
    expected_out = output_formats if output_formats is not None else (fmt,)
    if out_fmt not in expected_out:
        raise ValueError(f'output must match allowed formats: {list(expected_out)}')
    if source == output:
        raise ValueError('input and output must differ')
    binding = current_binding()
    if binding is not None:
        binding.require_paths(source, output)
        return source, output, fmt
    for path in (source, output.parent):
        cursor = Path(path.anchor)
        for part in path.parts[1:]:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError('contract paths cannot contain symlinks')
    if not source.is_file():
        raise ValueError('contract input must be an existing regular file')
    if not output.parent.is_dir() or os.path.lexists(output):
        raise ValueError('contract output must be a new path in an existing directory')
    return source.resolve(strict=True), output.parent.resolve(strict=True) / output.name, fmt


@dataclass(frozen=True)
class ContractPlugin:
    """Declarative specification of a verifiable deterministic contract."""

    capability: str
    executor_id: str
    input_formats: tuple[str, ...]
    validate_options: Callable[[dict[str, Any], bytes], dict[str, Any]]
    execute: Callable[['PluginExecutionPlan', bytes], tuple[bytes, dict[str, Any]]]
    verify: Callable[['PluginExecutionPlan', bytes, bytes], dict[str, Any]]
    output_formats: tuple[str, ...] | None = None
    contract_version: int = 1
    constraints: frozenset[str] = _DEFAULT_CONSTRAINTS
    description: str = ''
    executor_revision: str = ''
    verifier_revision: str = ''

    def __post_init__(self) -> None:
        if not isinstance(self.capability, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{2,63}', self.capability):
            raise ValueError('capability must be a lowercase dotted identifier')
        if not isinstance(self.executor_id, str) or not self.executor_id.strip():
            raise ValueError('executor_id must be a nonempty string')
        if not self.input_formats or any(not isinstance(f, str) or not f for f in self.input_formats):
            raise ValueError('input_formats must be a nonempty tuple of extensions')
        if type(self.contract_version) is not int or self.contract_version < 1:
            raise ValueError('contract_version must be a positive integer')
        if not self.executor_revision:
            object.__setattr__(self, 'executor_revision', _callable_revision(self.execute, self.executor_id))
        if not self.verifier_revision:
            object.__setattr__(self, 'verifier_revision', _callable_revision(self.verify, self.executor_id + ':verify'))

    def metadata(self) -> dict[str, Any]:
        return {
            'capability': self.capability,
            'contract_version': self.contract_version,
            'executor_id': self.executor_id,
            'input_formats': list(self.input_formats),
            'constraints': sorted(self.constraints),
            'description': self.description,
        }


@dataclass(frozen=True)
class PluginExecutionPlan:
    """Immutable, hash-anchored execution plan for any ContractPlugin."""

    capability: str
    contract_version: int
    executor_id: str
    executor_revision: str
    verifier_revision: str
    input_path: str
    output_path: str
    input_format: str
    input_sha256: str
    options_json: str
    constraints: tuple[str, ...]
    goal: str
    source_ref: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        for name in ('executor_revision', 'verifier_revision', 'input_sha256'):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
                raise ValueError(f'{name} must be a lowercase SHA256')
        for name in ('input_path', 'output_path'):
            value = getattr(self, name)
            if (not isinstance(value, str) or not Path(value).is_absolute()
                    or '\x00' in value or '..' in Path(value).parts):
                raise ValueError(f'{name} must be an absolute path without traversal')
        if self.input_path == self.output_path:
            raise ValueError('input and output must differ')
        if not isinstance(self.options_json, str):
            raise ValueError('options_json must be canonical JSON text')
        parsed = json.loads(self.options_json)
        if not isinstance(parsed, dict):
            raise ValueError('options_json must decode to a JSON object')
        if not isinstance(self.constraints, tuple) or any(not isinstance(c, str) for c in self.constraints):
            raise ValueError('constraints must be a tuple of strings')
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError('goal must be nonempty')
        for name in ('source_ref', 'session_id'):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f'{name} must be a nonempty string or null')

    @property
    def options(self) -> dict[str, Any]:
        return json.loads(self.options_json)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result['constraints'] = list(self.constraints)
        return result

    def semantic_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.to_dict().items() if k not in _METADATA}

    @property
    def sha256(self) -> str:
        raw = json.dumps(self.semantic_dict(), sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def verification_parameters(self) -> dict[str, Any]:
        return {
            'capability': self.capability,
            'input_path': self.input_path,
            'output_path': self.output_path,
            'input_sha256': self.input_sha256,
            'format': self.input_format,
            'options_json': self.options_json,
            'constraints': list(self.constraints),
        }

    def matches_prepared(self, prepared: dict[str, Any]) -> bool:
        return all(prepared.get(k) == v for k, v in self.verification_parameters().items())

    @classmethod
    def from_dict(cls, saved: dict[str, Any]) -> 'PluginExecutionPlan':
        if not isinstance(saved, dict) or set(saved) != {item.name for item in fields(cls)}:
            raise ValueError('persisted plugin plan fields are incomplete or unknown')
        values = dict(saved)
        if not isinstance(values['constraints'], (list, tuple)):
            raise ValueError('persisted plugin constraints must be a sequence')
        values['constraints'] = tuple(values['constraints'])
        return cls(**values)


class ContractRegistry:
    """Thread-safe registry for built-in and entry-point ContractPlugins."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plugins: dict[str, ContractPlugin] = {}
        self._entry_points_loaded = False
        self.load_errors: list[dict[str, str]] = []

    def register(self, plugin: ContractPlugin, *, replace: bool = False) -> None:
        if not isinstance(plugin, ContractPlugin):
            raise TypeError('plugin must be a ContractPlugin instance')
        with self._lock:
            if not replace and plugin.capability in self._plugins:
                raise ValueError(f'contract capability already registered: {plugin.capability}')
            self._plugins[plugin.capability] = plugin

    def unregister(self, capability: str) -> None:
        with self._lock:
            self._plugins.pop(capability, None)

    def load_entry_points(self) -> list[str]:
        """Discover installed plugins under the rpe_harness.contracts entry-point group."""
        loaded: list[str] = []
        with self._lock:
            if self._entry_points_loaded:
                return loaded
            self._entry_points_loaded = True
            try:
                eps = entry_points(group=ENTRY_POINT_GROUP)
            except Exception as exc:
                self.load_errors.append({'entry_point': ENTRY_POINT_GROUP, 'error': str(exc)})
                return loaded
            for ep in eps:
                try:
                    candidate = ep.load()
                    plugin = candidate() if callable(candidate) else candidate
                    if not isinstance(plugin, ContractPlugin):
                        raise TypeError('entry point did not return a ContractPlugin')
                    if plugin.capability not in self._plugins:
                        self._plugins[plugin.capability] = plugin
                        loaded.append(plugin.capability)
                except Exception as exc:
                    self.load_errors.append({'entry_point': ep.name, 'error': f'{type(exc).__name__}: {exc}'})
        return loaded

    def get(self, capability: str) -> ContractPlugin | None:
        self.load_entry_points()
        with self._lock:
            return self._plugins.get(capability)

    def has(self, capability: str) -> bool:
        return self.get(capability) is not None

    def list_plugins(self) -> list[ContractPlugin]:
        self.load_entry_points()
        with self._lock:
            return [self._plugins[k] for k in sorted(self._plugins)]

    def describe(self) -> list[dict[str, Any]]:
        return [plugin.metadata() for plugin in self.list_plugins()]

    def plan_from_request(self, payload: dict[str, Any]) -> PluginExecutionPlan:
        if (not isinstance(payload, dict) or type(payload.get('schema_version')) is not int
                or payload['schema_version'] != 2 or set(payload) - _REQUEST_FIELDS):
            raise ValueError('plugin contract requires an exact schema_version=2 request')
        if not isinstance(payload.get('event_id'), str) or not payload['event_id'].strip():
            raise ValueError('event_id is required')
        capability = payload.get('capability')
        plugin = self.get(capability) if isinstance(capability, str) else None
        if plugin is None:
            raise ValueError(f'unregistered contract capability: {capability}')
        version = payload.get('contract_version', plugin.contract_version)
        if type(version) is not int or version != plugin.contract_version:
            raise ValueError('contract_version does not match registered plugin')
        params = payload.get('execution')
        if not isinstance(params, dict) or 'input_path' not in params or 'output_path' not in params:
            raise ValueError('execution must include input_path and output_path')
        constraints_raw = payload.get('constraints', [])
        if (not isinstance(constraints_raw, list)
                or any(not isinstance(c, str) for c in constraints_raw)
                or set(constraints_raw) - plugin.constraints):
            raise ValueError('unsupported constraints for plugin contract')
        constraints = tuple(sorted(set(constraints_raw)))
        source, output, fmt = _validate_plugin_paths(
            params['input_path'], params['output_path'], plugin.input_formats, plugin.output_formats
        )
        if 'input_format' in payload and payload['input_format'] != fmt:
            raise ValueError('input_format must match input file extension')
        raw = read_source(source, max_bytes=MAX_PLUGIN_INPUT_BYTES)
        extra_opts = {k: v for k, v in params.items() if k not in {'input_path', 'output_path'}}
        validated_opts = plugin.validate_options(extra_opts, raw)
        if not isinstance(validated_opts, dict):
            raise ValueError('plugin validate_options must return a dict')
        return PluginExecutionPlan(
            capability=plugin.capability,
            contract_version=plugin.contract_version,
            executor_id=plugin.executor_id,
            executor_revision=plugin.executor_revision,
            verifier_revision=plugin.verifier_revision,
            input_path=str(source),
            output_path=str(output),
            input_format=fmt,
            input_sha256=hashlib.sha256(raw).hexdigest(),
            options_json=canonical_json(validated_opts),
            constraints=constraints,
            goal=payload.get('goal', ''),
            source_ref=payload.get('source_ref', payload['event_id']),
            session_id=payload.get('session_id'),
        )

    def prepare(self, plan: PluginExecutionPlan) -> dict[str, Any]:
        from .execution import register_staging_bytes
        plugin = self.get(plan.capability)
        if plugin is None:
            raise ValueError(f'unregistered contract capability: {plan.capability}')
        source, output = Path(plan.input_path), Path(plan.output_path)
        raw = read_source(source, max_bytes=MAX_PLUGIN_INPUT_BYTES)
        if hashlib.sha256(raw).hexdigest() != plan.input_sha256:
            raise ValueError('input changed after plan creation')
        staged_bytes, metrics = plugin.execute(plan, raw)
        if not isinstance(staged_bytes, bytes):
            raise ValueError('plugin execute must return (bytes, metrics)')
        staging = register_staging_bytes(source, output, plan.input_sha256, staged_bytes)
        prepared = {
            **plan.verification_parameters(),
            'staging_path': str(staging),
            'output_sha256': hashlib.sha256(staged_bytes).hexdigest(),
            'metrics': dict(metrics or {}),
        }
        return prepared

    def verify(self, prepared: dict[str, Any], plan: PluginExecutionPlan) -> dict[str, Any]:
        plugin = self.get(plan.capability)
        if plugin is None:
            raise ValueError(f'unregistered contract capability: {plan.capability}')
        if not plan.matches_prepared(prepared):
            raise ValueError('prepared artifact does not match frozen execution plan')
        source = Path(plan.input_path)
        staging = Path(prepared['staging_path'])
        raw_input = read_source(source, max_bytes=MAX_PLUGIN_INPUT_BYTES)
        if hashlib.sha256(raw_input).hexdigest() != plan.input_sha256:
            raise ValueError('input changed before verification')
        staged_bytes = staging.read_bytes()
        output_sha256 = hashlib.sha256(staged_bytes).hexdigest()
        verdict = dict(plugin.verify(plan, raw_input, staged_bytes))
        passed = bool(verdict.get('passed') is True)
        result = {
            **verdict,
            'passed': passed,
            'plan_sha256': plan.sha256,
            'input_sha256': plan.input_sha256,
            'output_sha256': output_sha256,
            'input_bytes': len(raw_input),
            'output_bytes': len(staged_bytes),
        }
        if passed:
            prepared['_verified_plan_sha256'] = plan.sha256
            prepared['_verified_output_sha256'] = output_sha256
        return result


# ---------------------------------------------------------------------------
# Built-in Contract 1: tabular.filter (deterministic CSV row filtering)
# ---------------------------------------------------------------------------

_FILTER_OPERATORS = frozenset({'equals', 'not_equals', 'in', 'not_empty'})


def _parse_csv(raw: bytes, encoding: str, delimiter: str) -> list[list[str]]:
    try:
        rows = list(csv.reader(io.StringIO(raw.decode(encoding), newline=''),
                               delimiter=delimiter, strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise ValueError('invalid csv input') from exc
    if not rows or any(len(r) != len(rows[0]) for r in rows[1:]):
        raise ValueError('invalid csv shape')
    header = rows[0]
    if not header or any(not c.strip() for c in header) or len(set(header)) != len(header):
        raise ValueError('csv header must contain unique nonempty column names')
    return rows


def _filter_validate(options: dict[str, Any], raw: bytes) -> dict[str, Any]:
    allowed = {'column', 'operator', 'value', 'values', 'encoding', 'delimiter'}
    unknown = set(options) - allowed
    if unknown:
        raise ValueError(f'unsupported tabular.filter options: {sorted(unknown)}')
    column = options.get('column')
    operator = options.get('operator')
    if not isinstance(column, str) or not column.strip():
        raise ValueError('column must be a nonempty string')
    if operator not in _FILTER_OPERATORS:
        raise ValueError(f'operator must be one of {sorted(_FILTER_OPERATORS)}')
    encoding = options.get('encoding', 'utf-8')
    delimiter = options.get('delimiter', ',')
    if not isinstance(encoding, str):
        raise ValueError('encoding must be a string')
    try:
        codecs.lookup(encoding)
    except LookupError as exc:
        raise ValueError('unsupported encoding') from exc
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in '\r\n\x00':
        raise ValueError('delimiter must be a single non-newline character')
    rows = _parse_csv(raw, encoding, delimiter)
    if column not in rows[0]:
        raise ValueError(f'filter column not found in header: {column}')
    normalized: dict[str, Any] = {
        'column': column,
        'operator': operator,
        'encoding': encoding,
        'delimiter': delimiter,
    }
    if operator in {'equals', 'not_equals'}:
        if 'value' not in options or not isinstance(options['value'], str):
            raise ValueError(f'operator {operator} requires a string value')
        normalized['value'] = options['value']
    elif operator == 'in':
        vals = options.get('values')
        if not isinstance(vals, list) or not vals or any(not isinstance(v, str) for v in vals):
            raise ValueError('operator in requires a nonempty list of strings in values')
        normalized['values'] = list(vals)
    return normalized


def _matches_filter(cell: str, opts: dict[str, Any]) -> bool:
    op = opts['operator']
    if op == 'equals':
        return cell == opts['value']
    if op == 'not_equals':
        return cell != opts['value']
    if op == 'in':
        return cell in opts['values']
    if op == 'not_empty':
        return bool(cell.strip())
    raise ValueError(f'unsupported operator: {op}')


def _filter_execute(plan: PluginExecutionPlan, raw: bytes) -> tuple[bytes, dict[str, Any]]:
    opts = plan.options
    rows = _parse_csv(raw, opts['encoding'], opts['delimiter'])
    header = rows[0]
    col_idx = header.index(opts['column'])
    kept = [header] + [row for row in rows[1:] if _matches_filter(row[col_idx], opts)]
    buf = io.StringIO(newline='')
    writer = csv.writer(buf, delimiter=opts['delimiter'], lineterminator='\n')
    writer.writerows(kept)
    out_bytes = buf.getvalue().encode(opts['encoding'])
    input_rows = len(rows) - 1
    output_rows = len(kept) - 1
    return out_bytes, {
        'input_rows': input_rows,
        'output_rows': output_rows,
        'removed_rows': input_rows - output_rows,
    }


def _filter_verify(plan: PluginExecutionPlan, raw_input: bytes, staged_bytes: bytes) -> dict[str, Any]:
    opts = plan.options
    src_rows = _parse_csv(raw_input, opts['encoding'], opts['delimiter'])
    out_rows = _parse_csv(staged_bytes, opts['encoding'], opts['delimiter'])
    col_idx = src_rows[0].index(opts['column'])
    expected = [src_rows[0]] + [row for row in src_rows[1:] if _matches_filter(row[col_idx], opts)]
    header_match = out_rows[0] == src_rows[0]
    rows_exact = out_rows == expected
    checks = {
        'header_preserved': header_match,
        'rows_match_predicate_and_order': rows_exact,
        'leading_zeros_preserved': rows_exact,
    }
    return {
        'passed': all(checks.values()),
        'checks': checks,
        'input_rows': len(src_rows) - 1,
        'output_rows': len(out_rows) - 1,
        'removed_rows': (len(src_rows) - 1) - (len(out_rows) - 1),
    }


TABULAR_FILTER_PLUGIN = ContractPlugin(
    capability='tabular.filter',
    executor_id='builtin/tabular-filter',
    input_formats=('csv',),
    validate_options=_filter_validate,
    execute=_filter_execute,
    verify=_filter_verify,
    description='Deterministic CSV row filtering preserving header, leading zeros, and row order',
)


# ---------------------------------------------------------------------------
# Built-in Contract 2: json.patch (deterministic JSON object patching)
# ---------------------------------------------------------------------------

def _strict_equal(a: Any, b: Any) -> bool:
    """Deep equality that distinguishes bool from int (True != 1)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return set(a.keys()) == set(b.keys()) and all(_strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def _apply_json_ops(doc: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(doc)
    for op in operations:
        parts = op['path']
        cursor = result
        for seg in parts[:-1]:
            if not isinstance(cursor, dict):
                raise ValueError(f'non-object encountered along path: {".".join(parts)}')
            if seg not in cursor:
                if op['op'] == 'set':
                    cursor[seg] = {}
                else:
                    raise ValueError(f'delete target parent missing: {".".join(parts)}')
            cursor = cursor[seg]
        if not isinstance(cursor, dict):
            raise ValueError(f'target parent is not an object: {".".join(parts)}')
        leaf = parts[-1]
        if op['op'] == 'set':
            cursor[leaf] = copy.deepcopy(op['value'])
        elif op['op'] == 'delete':
            if leaf not in cursor:
                raise ValueError(f'delete target key missing: {".".join(parts)}')
            del cursor[leaf]
        else:
            raise ValueError(f'unsupported json.patch op: {op["op"]}')
    return result


def _patch_validate(options: dict[str, Any], raw: bytes) -> dict[str, Any]:
    allowed = {'operations', 'indent'}
    unknown = set(options) - allowed
    if unknown:
        raise ValueError(f'unsupported json.patch options: {sorted(unknown)}')
    try:
        doc = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid json input') from exc
    if not isinstance(doc, dict):
        raise ValueError('json.patch requires a top-level JSON object')
    ops_raw = options.get('operations')
    if not isinstance(ops_raw, list) or not ops_raw or len(ops_raw) > 64:
        raise ValueError('operations must be a nonempty list of at most 64 patch entries')
    normalized_ops: list[dict[str, Any]] = []
    for item in ops_raw:
        if not isinstance(item, dict) or item.get('op') not in {'set', 'delete'}:
            raise ValueError('each operation must be an object with op set or delete')
        key = item.get('key')
        if not isinstance(key, str) or not key.strip():
            raise ValueError('each operation requires a nonempty string key')
        parts = key.split('.')
        if any(not p for p in parts):
            raise ValueError(f'invalid dotted key path: {key}')
        entry: dict[str, Any] = {'op': item['op'], 'key': key, 'path': parts}
        if item['op'] == 'set':
            if 'value' not in item:
                raise ValueError('set operation requires value')
            # Ensure value is strict JSON without NaN/Infinity
            json.dumps(item['value'], allow_nan=False)
            entry['value'] = item['value']
        elif 'value' in item:
            raise ValueError('delete operation must not include value')
        normalized_ops.append(entry)
    indent = options.get('indent', 2)
    if indent is not None and (type(indent) is not int or not 0 <= indent <= 8):
        raise ValueError('indent must be null or an integer between 0 and 8')
    # Dry-run validation against input so missing delete targets fail at admission
    _apply_json_ops(doc, normalized_ops)
    return {'operations': normalized_ops, 'indent': indent}


def _patch_execute(plan: PluginExecutionPlan, raw: bytes) -> tuple[bytes, dict[str, Any]]:
    opts = plan.options
    doc = json.loads(raw.decode('utf-8'))
    patched = _apply_json_ops(doc, opts['operations'])
    indent = opts['indent']
    if indent is None:
        text = json.dumps(patched, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
    else:
        text = json.dumps(patched, ensure_ascii=False, allow_nan=False, indent=indent) + '\n'
    return text.encode('utf-8'), {'root_type': 'object', 'operations_applied': len(opts['operations'])}


def _patch_verify(plan: PluginExecutionPlan, raw_input: bytes, staged_bytes: bytes) -> dict[str, Any]:
    opts = plan.options
    src_doc = json.loads(raw_input.decode('utf-8'))
    out_doc = json.loads(staged_bytes.decode('utf-8'))
    expected = _apply_json_ops(src_doc, opts['operations'])
    exact = _strict_equal(out_doc, expected)
    checks = {
        'output_is_valid_json_object': isinstance(out_doc, dict),
        'patch_matches_oracle': exact,
    }
    return {
        'passed': all(checks.values()),
        'checks': checks,
        'root_type': type(out_doc).__name__,
        'operations_applied': len(opts['operations']),
    }


JSON_PATCH_PLUGIN = ContractPlugin(
    capability='json.patch',
    executor_id='builtin/json-patch',
    input_formats=('json',),
    validate_options=_patch_validate,
    execute=_patch_execute,
    verify=_patch_verify,
    description='Deterministic JSON object set/delete patching with strict-type oracle verification',
)


def _create_default_registry() -> ContractRegistry:
    reg = ContractRegistry()
    reg.register(TABULAR_FILTER_PLUGIN)
    reg.register(JSON_PATCH_PLUGIN)
    from .dev_contracts import ENV_PATCH_PLUGIN, SQLITE_EXPORT_PLUGIN
    reg.register(ENV_PATCH_PLUGIN)
    reg.register(SQLITE_EXPORT_PLUGIN)
    return reg


DEFAULT_REGISTRY = _create_default_registry()
