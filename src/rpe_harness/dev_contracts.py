"""Developer-focused deterministic contracts built on rpe.contract/v1.

1. `env.patch`: Safe `.env` key set/delete preserving comments, blank lines,
   unrelated keys, and original ordering — verified by an independent oracle.
2. `sqlite.query_export`: Strictly read-only SQLite `SELECT` export to CSV
   with `mode=ro` + `PRAGMA query_only = ON` + independent oracle re-query.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from pathlib import Path
from typing import Any

from .contract_sdk import ContractPlugin, PluginExecutionPlan

# ---------------------------------------------------------------------------
# Contract A: env.patch (.env -> .env)
# ---------------------------------------------------------------------------

_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_ENV_LINE_RE = re.compile(r'^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$')


def _format_env_value(value: str) -> str:
    if any(ch in value for ch in ('\n', '\r', '\x00')):
        raise ValueError('env values cannot contain newlines or NUL bytes')
    if value == '' or any(ch in value for ch in (' ', '\t', '#', '"', "'")):
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _parse_env_lines(raw: bytes) -> list[dict[str, Any]]:
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('.env input must be valid UTF-8') from exc
    entries: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for idx, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            entries.append({'kind': 'raw', 'line': line})
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            raise ValueError(f'malformed .env line {idx + 1}')
        prefix, key, eq, val = match.groups()
        if key in seen_keys:
            raise ValueError(f'duplicate .env key in input: {key}')
        seen_keys.add(key)
        entries.append({'kind': 'kv', 'prefix': prefix, 'key': key, 'eq': eq, 'value': val, 'line': line})
    return entries


def _apply_env_ops(entries: list[dict[str, Any]], operations: list[dict[str, Any]]) -> str:
    items = [dict(e) for e in entries]
    for op in operations:
        key = op['key']
        existing_idx = next((i for i, e in enumerate(items) if e['kind'] == 'kv' and e['key'] == key), None)
        if op['op'] == 'set':
            formatted = _format_env_value(op['value'])
            if existing_idx is not None:
                e = items[existing_idx]
                e['value'] = formatted
                e['line'] = f"{e['prefix']}{key}{e['eq']}{formatted}"
            else:
                items.append({'kind': 'kv', 'prefix': '', 'key': key, 'eq': '=', 'value': formatted,
                              'line': f'{key}={formatted}'})
        elif op['op'] == 'delete':
            if existing_idx is None:
                raise ValueError(f'delete target .env key missing: {key}')
            items.pop(existing_idx)
        else:
            raise ValueError(f'unsupported env.patch op: {op["op"]}')
    return '\n'.join(e['line'] for e in items) + '\n'


def _env_validate(options: dict[str, Any], raw: bytes) -> dict[str, Any]:
    unknown = set(options) - {'operations'}
    if unknown:
        raise ValueError(f'unsupported env.patch options: {sorted(unknown)}')
    ops_raw = options.get('operations')
    if not isinstance(ops_raw, list) or not ops_raw or len(ops_raw) > 64:
        raise ValueError('operations must be a nonempty list of at most 64 entries')
    entries = _parse_env_lines(raw)
    normalized: list[dict[str, Any]] = []
    for item in ops_raw:
        if not isinstance(item, dict) or item.get('op') not in {'set', 'delete'}:
            raise ValueError('each env.patch operation requires op set or delete')
        key = item.get('key')
        if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
            raise ValueError(f'invalid env key: {key}')
        entry: dict[str, Any] = {'op': item['op'], 'key': key}
        if item['op'] == 'set':
            if 'value' not in item or not isinstance(item['value'], str):
                raise ValueError('set operation requires string value')
            _format_env_value(item['value'])
            entry['value'] = item['value']
        elif 'value' in item:
            raise ValueError('delete operation must not include value')
        normalized.append(entry)
    # Dry-run to reject missing delete targets at admission
    _apply_env_ops(entries, normalized)
    return {'operations': normalized}


def _env_execute(plan: PluginExecutionPlan, raw: bytes) -> tuple[bytes, dict[str, Any]]:
    entries = _parse_env_lines(raw)
    out_text = _apply_env_ops(entries, plan.options['operations'])
    return out_text.encode('utf-8'), {'operations_applied': len(plan.options['operations'])}


def _env_verify(plan: PluginExecutionPlan, raw_input: bytes, staged_bytes: bytes) -> dict[str, Any]:
    src_entries = _parse_env_lines(raw_input)
    out_entries = _parse_env_lines(staged_bytes)
    expected_bytes = _apply_env_ops(src_entries, plan.options['operations']).encode('utf-8')
    src_comments = [e['line'] for e in src_entries if e['kind'] == 'raw']
    out_comments = [e['line'] for e in out_entries if e['kind'] == 'raw']
    checks = {
        'comments_and_blanks_preserved': src_comments == out_comments,
        'exact_match_with_oracle': staged_bytes == expected_bytes,
    }
    return {
        'passed': all(checks.values()),
        'checks': checks,
        'operations_applied': len(plan.options['operations']),
    }


ENV_PATCH_PLUGIN = ContractPlugin(
    capability='env.patch',
    executor_id='builtin/env-patch',
    input_formats=('env',),
    validate_options=_env_validate,
    execute=_env_execute,
    verify=_env_verify,
    description='Deterministic .env key set/delete preserving comments, blank lines, and ordering',
)


# ---------------------------------------------------------------------------
# Contract B: sqlite.query_export (.sqlite / .db -> .csv)
# ---------------------------------------------------------------------------

_FORBIDDEN_SQL_RE = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|BEGIN|COMMIT|ROLLBACK)\b',
    re.IGNORECASE,
)


def _validate_readonly_sql(query: Any) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query must be a nonempty SQL SELECT string')
    cleaned = query.strip().rstrip(';').strip()
    if ';' in cleaned:
        raise ValueError('multiple SQL statements are forbidden')
    if not re.match(r'^(SELECT|WITH)\b', cleaned, re.IGNORECASE):
        raise ValueError('only read-only SELECT / WITH queries are allowed')
    if _FORBIDDEN_SQL_RE.search(cleaned):
        raise ValueError('write or schema-modifying SQL keywords are forbidden')
    return cleaned


def _run_readonly_query(db_path: Path, query: str, max_rows: int) -> tuple[list[str], list[list[str]]]:
    uri = f'file:{db_path.as_posix()}?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.execute('PRAGMA query_only = ON')
        cur = conn.execute(query)
        if not cur.description:
            raise ValueError('query returned no columns')
        header = [col[0] for col in cur.description]
        rows_raw = cur.fetchmany(max_rows + 1)
        if len(rows_raw) > max_rows:
            raise ValueError(f'query result exceeds max_rows limit ({max_rows})')
        rows = [['' if cell is None else str(cell) for cell in row] for row in rows_raw]
        return header, rows
    finally:
        conn.close()


def _sqlite_validate(options: dict[str, Any], raw: bytes) -> dict[str, Any]:
    unknown = set(options) - {'query', 'max_rows', 'delimiter'}
    if unknown:
        raise ValueError(f'unsupported sqlite.query_export options: {sorted(unknown)}')
    if not raw.startswith(b'SQLite format 3\x00'):
        raise ValueError('input file is not a valid SQLite 3 database')
    query = _validate_readonly_sql(options.get('query'))
    max_rows = options.get('max_rows', 10000)
    if type(max_rows) is not int or not 1 <= max_rows <= 100000:
        raise ValueError('max_rows must be an integer between 1 and 100000')
    delimiter = options.get('delimiter', ',')
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in '\r\n\x00':
        raise ValueError('delimiter must be a single non-newline character')
    return {'query': query, 'max_rows': max_rows, 'delimiter': delimiter}


def _sqlite_execute(plan: PluginExecutionPlan, raw: bytes) -> tuple[bytes, dict[str, Any]]:
    opts = plan.options
    header, rows = _run_readonly_query(Path(plan.input_path), opts['query'], opts['max_rows'])
    buf = io.StringIO(newline='')
    writer = csv.writer(buf, delimiter=opts['delimiter'], lineterminator='\n')
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode('utf-8'), {'input_rows': len(rows), 'output_rows': len(rows), 'removed_rows': 0}


def _sqlite_verify(plan: PluginExecutionPlan, raw_input: bytes, staged_bytes: bytes) -> dict[str, Any]:
    opts = plan.options
    expected_header, expected_rows = _run_readonly_query(Path(plan.input_path), opts['query'], opts['max_rows'])
    actual_rows = list(csv.reader(io.StringIO(staged_bytes.decode('utf-8'), newline=''),
                                  delimiter=opts['delimiter'], strict=True))
    exact = bool(actual_rows) and actual_rows[0] == expected_header and actual_rows[1:] == expected_rows
    checks = {
        'readonly_oracle_matches_csv': exact,
        'header_matches': bool(actual_rows) and actual_rows[0] == expected_header,
    }
    return {
        'passed': all(checks.values()),
        'checks': checks,
        'input_rows': len(expected_rows),
        'output_rows': max(0, len(actual_rows) - 1),
        'removed_rows': 0,
    }


SQLITE_EXPORT_PLUGIN = ContractPlugin(
    capability='sqlite.query_export',
    executor_id='builtin/sqlite-query-export',
    input_formats=('sqlite', 'db'),
    output_formats=('csv',),
    validate_options=_sqlite_validate,
    execute=_sqlite_execute,
    verify=_sqlite_verify,
    description='Strictly read-only SQLite SELECT export to CSV with independent oracle re-query',
)
