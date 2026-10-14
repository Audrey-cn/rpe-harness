"""Lossless JSON whitespace normalization; the independent oracle owns approval.

The producer validates its own token grammar and never converts number tokens to
Python numeric values. Raw string spellings, including escapes, are retained.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .execution import _new_staging, cleanup_staging
from .path_binding import read_source

EXECUTOR_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 64
MAX_TOKENS = 100000
MAX_NUMBER_CHARS = 256
_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')


@dataclass(frozen=True)
class _Token:
    kind: str
    raw: str
    value: str | None = None


def _tokenize(raw: bytes) -> list[_Token]:
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('JSON input exceeds 1 MiB')
    try:
        text = raw.decode('utf-8', errors='strict')
    except UnicodeError as error:
        raise ValueError('JSON input must be UTF-8') from error
    if text.startswith('\ufeff'):
        raise ValueError('JSON BOM is not supported')
    tokens, offset = [], 0
    while offset < len(text):
        start, char = offset, text[offset]
        if char in ' \t\r\n':
            offset += 1
            continue
        if len(tokens) >= MAX_TOKENS:
            raise ValueError('JSON exceeds its token budget')
        if char in '{}[],:':
            tokens.append(_Token(char, char))
            offset += 1
        elif char == '"':
            try:
                value, offset = json.decoder.scanstring(text, offset + 1, True)
            except (ValueError, UnicodeError) as error:
                raise ValueError('invalid JSON string') from error
            if any(0xD800 <= ord(item) <= 0xDFFF for item in value):
                raise ValueError('JSON strings cannot contain unpaired surrogates')
            tokens.append(_Token('string', text[start:offset], value))
        elif char in '-0123456789':
            found = _NUMBER.match(text, offset)
            if found is None:
                raise ValueError('invalid JSON number')
            offset = found.end()
            number = text[start:offset]
            if len(number) > MAX_NUMBER_CHARS:
                raise ValueError('JSON number exceeds its token budget')
            tokens.append(_Token('number', number))
        else:
            word = next((word for word in ('true', 'false', 'null')
                         if text.startswith(word, offset)), None)
            if word is None:
                raise ValueError('invalid JSON token')
            offset += len(word)
            tokens.append(_Token('null' if word == 'null' else 'boolean', word))
    return tokens


def _validate(tokens: list[_Token]) -> str:
    """Validate a complete value with bounded recursive descent and decoded keys."""
    def expect(index, kind):
        if index >= len(tokens) or tokens[index].kind != kind:
            raise ValueError('invalid JSON grammar')
        return index + 1

    def value(index, depth):
        if index >= len(tokens):
            raise ValueError('JSON value is missing')
        token = tokens[index]
        if token.kind in {'string', 'number', 'boolean', 'null'}:
            return index + 1, token.kind
        if token.kind not in {'{', '['}:
            raise ValueError('invalid JSON value')
        if depth >= MAX_DEPTH:
            raise ValueError('JSON nesting exceeds 64 containers')
        closing, root_type = ('}', 'object') if token.kind == '{' else (']', 'array')
        index += 1
        if index < len(tokens) and tokens[index].kind == closing:
            return index + 1, root_type
        keys = set()
        while True:
            if token.kind == '{':
                key_index = index
                index = expect(index, 'string')
                key = tokens[key_index].value
                if key in keys:
                    raise ValueError('duplicate decoded JSON object key')
                keys.add(key)
                index = expect(index, ':')
            index, _ = value(index, depth + 1)
            if index < len(tokens) and tokens[index].kind == closing:
                return index + 1, root_type
            index = expect(index, ',')

    ending, root_type = value(0, 0)
    if ending != len(tokens):
        raise ValueError('JSON has trailing content')
    return root_type


def _format(tokens: list[_Token], style: str) -> bytes:
    chunks, size, depth = [], 0, 0

    def append(text):
        nonlocal size
        data = text.encode('utf-8')
        size += len(data)
        if size > MAX_OUTPUT_BYTES:
            raise ValueError('JSON output exceeds 16 MiB')
        chunks.append(data)

    for index, token in enumerate(tokens):
        previous = tokens[index - 1].kind if index else None
        if style == 'pretty' and index:
            if token.kind in {'}', ']'} and previous not in {'{', '['}:
                append('\n' + '  ' * (depth - 1))
            elif previous in {'{', '['} and token.kind not in {'}', ']'}:
                append('\n' + '  ' * depth)
            elif previous == ',':
                append('\n' + '  ' * depth)
            elif previous == ':':
                append(' ')
        append(token.raw)
        if token.kind in {'{', '['}:
            depth += 1
        elif token.kind in {'}', ']'}:
            depth -= 1
    append('\n')
    return b''.join(chunks)


def prepare_json_normalization(input_path: Path, output_path: Path, *, style: str,
                               input_sha256: str | None = None,
                               constraints: list[str] | None = None) -> dict[str, Any]:
    """Write an owned staging inode; publication always requires oracle approval."""
    from .json_contract import validate_paths, validate_constraints
    if not isinstance(style, str) or style not in {'pretty', 'minify'}:
        raise ValueError('JSON style must be pretty or minify')
    source, output = validate_paths(str(input_path), str(output_path))
    constraints = validate_constraints([] if constraints is None else constraints)
    raw = read_source(source, max_bytes=MAX_INPUT_BYTES)
    source_hash = hashlib.sha256(raw).hexdigest()
    if input_sha256 is not None and source_hash != input_sha256:
        raise ValueError('JSON input differs from the immutable plan')
    tokens = _tokenize(raw)
    _validate(tokens)
    artifact = _format(tokens, style)
    prepared = {'input_path': str(source), 'output_path': str(output),
                'input_sha256': source_hash, 'format': 'json', 'style': style,
                'constraints': list(constraints)}
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
