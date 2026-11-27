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
import tempfile
import time
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


def _split_env_comment(value: str) -> tuple[str, str]:
    # Quotes embedded in an unquoted value (for example don't) are literal.
    # Only a leading quote protects whitespace-delimited '#' characters.
    quote: str | None = value[0] if value.startswith(('"', "'")) else None
    escaped = False
    for idx in range(1 if quote is not None else 0, len(value)):
        char = value[idx]
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
        elif char == '#' and (idx == 0 or value[idx - 1] in ' \t'):
            start = idx
            while start and value[start - 1] in ' \t':
                start -= 1
            return value[:start], value[start:]
    return value, ''


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
        val, comment = _split_env_comment(val)
        if comment.startswith('#') and not val:
            # Whitespace after '=' belongs to the comment when the value is empty.
            bare_eq = eq.rstrip(' \t')
            comment = eq[len(bare_eq):] + comment
            eq = bare_eq
        entries.append({'kind': 'kv', 'prefix': prefix, 'key': key, 'eq': eq,
                        'value': val, 'comment': comment, 'line': line})
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
                comment = e.get('comment', '')
                if comment.startswith('#'):
                    comment = ' ' + comment
                e['comment'] = comment
                e['line'] = f"{e['prefix']}{key}{e['eq']}{formatted}{comment}"
            else:
                items.append({'kind': 'kv', 'prefix': '', 'key': key, 'eq': '=', 'value': formatted,
                              'line': f'{key}={formatted}'})
        elif op['op'] == 'delete':
            if existing_idx is None:
                raise ValueError(f'delete target .env key missing: {key}')
            items.pop(existing_idx)
        else:
            raise ValueError(f'unsupported env.patch op: {op["op"]}')
    return '\n'.join(e['line'] for e in items) + ('\n' if items else '')


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
    # Keep an independent key/layout oracle rather than replaying the executor's
    # line-mutation helper: otherwise the same comment-loss bug passes both sides.
    expected = {e['key']: dict(e) for e in src_entries if e['kind'] == 'kv'}
    layout = [(e['kind'], e['key'] if e['kind'] == 'kv' else e['line']) for e in src_entries]
    for op in plan.options['operations']:
        key = op['key']
        if op['op'] == 'delete':
            expected.pop(key)
            layout.remove(('kv', key))
            continue
        if key not in expected:
            expected[key] = {'prefix': '', 'eq': '=', 'comment': ''}
            layout.append(('kv', key))
        entry = expected[key]
        comment = entry['comment']
        if comment.startswith('#'):
            comment = ' ' + comment
        entry['comment'] = comment
        entry['line'] = ''.join((entry['prefix'], key, entry['eq'],
                                 _format_env_value(op['value']), comment))
    expected_lines = [expected[value]['line'] if kind == 'kv' else value
                      for kind, value in layout]
    expected_bytes = ('\n'.join(expected_lines) + ('\n' if expected_lines else '')).encode('utf-8')
    src_comments = [e['line'] for e in src_entries if e['kind'] == 'raw']
    out_comments = [e['line'] for e in out_entries if e['kind'] == 'raw']
    out_inline = {e['key']: e['comment'] for e in out_entries if e['kind'] == 'kv'}
    expected_inline = {key: e['comment'] for key, e in expected.items()}
    checks = {
        'comments_and_blanks_preserved': (src_comments == out_comments
                                          and out_inline == expected_inline),
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
    option_keys=('operations',),
)


# ---------------------------------------------------------------------------
# Contract B: sqlite.query_export (.sqlite / .db -> .csv)
# ---------------------------------------------------------------------------

_FORBIDDEN_SQL_RE = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|BEGIN|COMMIT|ROLLBACK)\b',
    re.IGNORECASE,
)
_SQLITE_QUERY_TIMEOUT_SECONDS = 5.0
_SQLITE_PROGRESS_INTERVAL = 1000
_SQLITE_MAX_VM_STEPS = 50_000_000


def _sql_code_only(query: str) -> str:
    """Mask SQLite quoted tokens and comments without joining adjacent SQL words."""
    code: list[str] = []
    idx = 0
    while idx < len(query):
        if query.startswith('--', idx):
            end = query.find('\n', idx + 2)
            if end < 0:
                break
            code.append('\n')
            idx = end + 1
        elif query.startswith('/*', idx):
            end = query.find('*/', idx + 2)
            code.append(' ')
            # SQLite allows a block comment to extend through the end of input.
            idx = len(query) if end < 0 else end + 2
        elif query[idx] in {"'", '"', '`', '['}:
            opening = query[idx]
            quote = ']' if opening == '[' else opening
            idx += 1
            while idx < len(query):
                if query[idx] == quote:
                    if opening != '[' and idx + 1 < len(query) and query[idx + 1] == quote:
                        idx += 2
                        continue
                    idx += 1
                    break
                idx += 1
            else:
                raise ValueError('unterminated SQL quoted token')
            # Keep a token marker so quoted text after a statement terminator
            # cannot disappear and make an internal semicolon look trailing.
            code.append('?')
        else:
            code.append(query[idx])
            idx += 1
    return ''.join(code)


def _validate_readonly_sql(query: Any) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query must be a nonempty SQL SELECT string')
    cleaned = query.strip()
    code = _sql_code_only(cleaned).strip()
    if code.endswith(';'):
        code = code[:-1].rstrip()
    if ';' in code:
        raise ValueError('multiple SQL statements are forbidden')
    if not re.match(r'^(SELECT|WITH)\b', code, re.IGNORECASE):
        raise ValueError('only read-only SELECT / WITH queries are allowed')
    if _FORBIDDEN_SQL_RE.search(code):
        raise ValueError('write or schema-modifying SQL keywords are forbidden')
    return cleaned


def _run_readonly_query(raw: bytes, query: str, max_rows: int) -> tuple[list[str], list[list[str]]]:
    # Query exactly the bytes admitted by the SDK, never the mutable source path.
    # The private directory also keeps source-side WAL/journal files out of this snapshot.
    with tempfile.TemporaryDirectory(prefix='rpe-sqlite-') as scratch:
        db_path = Path(scratch) / 'snapshot.sqlite'
        db_path.write_bytes(raw)
        db_path.chmod(0o400)
        uri = f'{db_path.as_uri()}?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            # SQLite's connection timeout only bounds lock waits. An aggregate
            # can execute unbounded VM work before producing its first row.
            deadline = time.monotonic() + _SQLITE_QUERY_TIMEOUT_SECONDS
            steps = 0
            limit_reached = False

            def check_execution_budget() -> int:
                nonlocal steps, limit_reached
                steps += _SQLITE_PROGRESS_INTERVAL
                limit_reached = (steps >= _SQLITE_MAX_VM_STEPS
                                 or time.monotonic() >= deadline)
                return int(limit_reached)

            conn.set_progress_handler(check_execution_budget, _SQLITE_PROGRESS_INTERVAL)
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
        except sqlite3.OperationalError as exc:
            if limit_reached:
                raise ValueError('SQLite query exceeded execution budget') from exc
            raise
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
    header, rows = _run_readonly_query(raw, opts['query'], opts['max_rows'])
    buf = io.StringIO(newline='')
    writer = csv.writer(buf, delimiter=opts['delimiter'], lineterminator='\n')
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode('utf-8'), {'input_rows': len(rows), 'output_rows': len(rows), 'removed_rows': 0}


def _sqlite_verify(plan: PluginExecutionPlan, raw_input: bytes, staged_bytes: bytes) -> dict[str, Any]:
    opts = plan.options
    expected_header, expected_rows = _run_readonly_query(raw_input, opts['query'], opts['max_rows'])
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
    option_keys=('query', 'max_rows', 'delimiter'),
)
