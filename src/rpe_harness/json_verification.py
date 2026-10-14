"""Independent JSON oracle: grammar, raw tokens, exact whitespace and plan seals.

This module does not import the producer's parser, tokenizer or formatter. A
tagged stdlib decoder validates grammar; independent lexical spans preserve
string/number spelling; whitespace intervals check the requested style.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .path_binding import _directory, read_source

VERIFIER_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 64
MAX_TOKENS = 100000
MAX_NUMBER_CHARS = 256
_NUMBER_TOKEN = re.compile(r'-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?\Z')


class _Number(str):
    """A raw numeric token, distinguishable from decoded JSON strings."""


class _Object(list):
    """Ordered member pairs, distinguishable from JSON arrays."""


def _pairs(items):
    seen = set()
    for key, _ in items:
        if key in seen:
            raise ValueError('duplicate decoded JSON object key')
        seen.add(key)
    return _Object(items)


def _nonfinite(value):
    raise ValueError('nonfinite JSON tokens are forbidden')


def _lexical_spans(text):
    spans, index, depth = [], 0, 0
    string_decoder = json.JSONDecoder()
    while index < len(text):
        if text[index] in ' \t\r\n':
            index += 1
            continue
        if len(spans) == MAX_TOKENS:
            raise ValueError('JSON exceeds its token budget')
        start = index
        if text[index] == '"':
            _, index = string_decoder.raw_decode(text, index)
        elif text[index] in '{}[],:':
            char = text[index]
            index += 1
            if char in '{[':
                depth += 1
                if depth > MAX_DEPTH:
                    raise ValueError('JSON nesting exceeds 64 containers')
            elif char in '}]':
                depth -= 1
                if depth < 0:
                    raise ValueError('invalid JSON container closure')
        else:
            # A scalar ends at a structural delimiter or JSON whitespace.
            while index < len(text) and text[index] not in ' \t\r\n{}[],:"':
                index += 1
            token = text[start:index]
            if token not in {'true', 'false', 'null'}:
                if not _NUMBER_TOKEN.fullmatch(token):
                    raise ValueError('invalid JSON scalar token')
                if len(token) > MAX_NUMBER_CHARS:
                    raise ValueError('JSON number exceeds its token budget')
        spans.append((start, index, text[start:index]))
    return spans


def _inspect(raw, limit):
    if not isinstance(raw, bytes) or len(raw) > limit:
        raise ValueError('JSON bytes exceed their fixed budget')
    try:
        text = raw.decode('utf-8', errors='strict')
        if text.startswith('\ufeff'):
            raise ValueError('JSON BOM is not supported')
        spans = _lexical_spans(text)
        decoded = json.loads(text, object_pairs_hook=_pairs, parse_int=_Number,
                             parse_float=_Number, parse_constant=_nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('invalid strict UTF-8 JSON') from error
    pending = [decoded]
    while pending:
        item = pending.pop()
        if isinstance(item, _Object):
            for key, value in item:
                pending.extend((key, value))
        elif type(item) is list:
            pending.extend(item)
        elif type(item) is str and any(0xD800 <= ord(char) <= 0xDFFF for char in item):
            raise ValueError('JSON strings cannot contain unpaired surrogates')
    if isinstance(decoded, _Object):
        root_type = 'object'
    elif isinstance(decoded, _Number):
        root_type = 'number'
    elif type(decoded) is list:
        root_type = 'array'
    elif type(decoded) is str:
        root_type = 'string'
    elif type(decoded) is bool:
        root_type = 'boolean'
    else:
        root_type = 'null'
    return text, spans, root_type


def inspect_json_bytes(raw: bytes) -> dict[str, Any]:
    """Strict bounded admission probe; no file writes or producer imports."""
    _, spans, root_type = _inspect(raw, MAX_INPUT_BYTES)
    return {'root_type': root_type, 'token_count': len(spans)}


def _style_matches(text, spans, style):
    if style not in {'pretty', 'minify'}:
        return False
    previous_end, previous_token, depth = 0, None, 0
    for start, end, token in spans:
        expected_gap = ''
        if style == 'pretty' and previous_token is not None:
            if token in {'}', ']'} and previous_token not in {'{', '['}:
                expected_gap = '\n' + '  ' * (depth - 1)
            elif previous_token in {'{', '['} and token not in {'}', ']'}:
                expected_gap = '\n' + '  ' * depth
            elif previous_token == ',':
                expected_gap = '\n' + '  ' * depth
            elif previous_token == ':':
                expected_gap = ' '
        if text[previous_end:start] != expected_gap:
            return False
        if token in {'{', '['}:
            depth += 1
        elif token in {'}', ']'}:
            depth -= 1
        previous_end, previous_token = end, token
    return bool(spans) and text[previous_end:] == '\n'


def _read_artifact(path):
    # Staging belongs to the producer, so treat it as untrusted and bound the read.
    path = Path(path)
    parent_fd = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_OUTPUT_BYTES:
            raise ValueError('JSON artifact must be a bounded regular file')
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, MAX_OUTPUT_BYTES + 1 - size))
            if not chunk:
                return b''.join(chunks)
            size += len(chunk)
            if size > MAX_OUTPUT_BYTES:
                raise ValueError('JSON output exceeds 16 MiB')
            chunks.append(chunk)
    finally:
        os.close(fd)


def verify_json_normalization(prepared: dict[str, Any], plan: Any = None) -> dict[str, Any]:
    """Reopen bytes and seal only exact, independently checked plan-bound output."""
    prepared.pop('_verified_output_sha256', None)
    prepared.pop('_verified_plan_sha256', None)
    report = {'passed': False, 'checks': {}}
    if plan is not None:
        from .json_contract import EXECUTOR_REVISION, VERIFIER_REVISION
        report['plan_sha256'] = plan.sha256
        if plan.executor_revision != EXECUTOR_REVISION:
            report['checks']['executor_revision'] = False
            return report
        if plan.verifier_revision != VERIFIER_REVISION:
            report['checks']['verifier_revision'] = False
            return report
        if not plan.matches_prepared(prepared):
            report['checks']['request_bound'] = False
            return report
        report['checks']['request_bound'] = True
    parameters = plan.verification_parameters() if plan is not None else prepared
    try:
        if parameters.get('format') != 'json':
            raise ValueError('JSON verification requires the JSON format')
        source = read_source(parameters['input_path'], max_bytes=MAX_INPUT_BYTES)
        artifact = _read_artifact(prepared['staging_path'])
        report.update(input_sha256=hashlib.sha256(source).hexdigest(),
                      output_sha256=hashlib.sha256(artifact).hexdigest(),
                      input_bytes=len(source), output_bytes=len(artifact))
        report['checks']['source_unchanged'] = report['input_sha256'] == parameters['input_sha256']
        if not report['checks']['source_unchanged']:
            return report
        _, before, root_type = _inspect(source, MAX_INPUT_BYTES)
        text, after, output_type = _inspect(artifact, MAX_OUTPUT_BYTES)
        report['root_type'] = root_type
        report['checks'].update(strict_json=True, root_type_preserved=root_type == output_type,
            raw_tokens_preserved=[item[2] for item in before] == [item[2] for item in after],
            requested_style=_style_matches(text, after, parameters['style']))
    except (ValueError, OSError, KeyError, TypeError, UnicodeError, RecursionError) as error:
        report['checks']['artifact_readable'] = False
        report['error'] = str(error)
        return report
    report['passed'] = all(report['checks'].values())
    if report['passed']:
        prepared['_verified_output_sha256'] = report['output_sha256']
        if plan is not None:
            prepared['_verified_plan_sha256'] = plan.sha256
    return report
