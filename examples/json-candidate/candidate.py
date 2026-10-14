"""Standalone lexical JSON normalizer; useful code still needs external approval."""
import json
import re

_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')


def _tokens(raw):
    if len(raw) > 1024 * 1024:
        raise ValueError('input exceeds 1 MiB')
    text = raw.decode('utf-8')
    if text.startswith('\ufeff'):
        raise ValueError('BOM is unsupported')
    result, index = [], 0
    while index < len(text):
        start, char = index, text[index]
        if char in ' \t\r\n':
            index += 1
            continue
        if len(result) >= 100000:
            raise ValueError('token budget exceeded')
        if char in '{}[],:':
            result.append((char, char, None))
            index += 1
        elif char == '"':
            value, index = json.decoder.scanstring(text, index + 1, True)
            if any(0xD800 <= ord(item) <= 0xDFFF for item in value):
                raise ValueError('unpaired surrogate')
            result.append(('string', text[start:index], value))
        elif char in '-0123456789':
            found = _NUMBER.match(text, index)
            if found is None:
                raise ValueError('invalid number')
            index = found.end()
            if index - start > 256:
                raise ValueError('number budget exceeded')
            result.append(('number', text[start:index], None))
        else:
            literal = next((word for word in ('true', 'false', 'null')
                            if text.startswith(word, index)), None)
            if literal is None:
                raise ValueError('invalid token')
            result.append(('scalar', literal, None))
            index += len(literal)
    return result


def _validate(tokens):
    def expect(index, kind):
        if index >= len(tokens) or tokens[index][0] != kind:
            raise ValueError('invalid grammar')
        return index + 1

    def value(index, depth):
        if index >= len(tokens):
            raise ValueError('value is missing')
        kind = tokens[index][0]
        if kind in {'string', 'number', 'scalar'}:
            return index + 1
        if kind not in {'{', '['}:
            raise ValueError('invalid value')
        if depth >= 64:
            raise ValueError('nesting budget exceeded')
        close, index, keys = ('}' if kind == '{' else ']'), index + 1, set()
        if index < len(tokens) and tokens[index][0] == close:
            return index + 1
        while True:
            if kind == '{':
                key_index = index
                index = expect(index, 'string')
                key = tokens[key_index][2]
                if key in keys:
                    raise ValueError('duplicate decoded key')
                keys.add(key)
                index = expect(index, ':')
            index = value(index, depth + 1)
            if index < len(tokens) and tokens[index][0] == close:
                return index + 1
            index = expect(index, ',')

    if value(0, 0) != len(tokens):
        raise ValueError('trailing content')


def prepare(plan, staging_path):
    if plan.get('capability') != 'json.normalize' or plan.get('input_format') != 'json':
        raise ValueError('unsupported contract')
    style = plan.get('style')
    if style not in {'pretty', 'minify'}:
        raise ValueError('unsupported style')
    with open(plan['input_path'], 'rb') as handle:
        raw = handle.read(1024 * 1024 + 1)
    tokens = _tokens(raw)
    _validate(tokens)
    chunks, size, depth = [], 0, 0
    for index, (kind, raw_token, _) in enumerate(tokens):
        previous = tokens[index - 1][0] if index else None
        gap = ''
        if style == 'pretty' and index:
            if kind in {'}', ']'} and previous not in {'{', '['}:
                gap = '\n' + '  ' * (depth - 1)
            elif previous in {'{', '['} and kind not in {'}', ']'}:
                gap = '\n' + '  ' * depth
            elif previous == ',':
                gap = '\n' + '  ' * depth
            elif previous == ':':
                gap = ' '
        data = (gap + raw_token).encode('utf-8')
        size += len(data)
        if size + 1 > 16 * 1024 * 1024:
            raise ValueError('output budget exceeded')
        chunks.append(data)
        if kind in {'{', '['}:
            depth += 1
        elif kind in {'}', ']'}:
            depth -= 1
    with open(staging_path, 'wb') as handle:
        handle.write(b''.join(chunks) + b'\n')
