"""Exact, content-bound quote addresses; selection never interprets business facts."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .context import SourceRef


def _anchor(text, start, end, source):
    quote, span = text[start:end], [start, end]
    body = {'source': source, 'quote': quote, 'span': span}
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    anchor = {'quote_ref': 'q:' + digest, 'quote': quote, 'span': span}
    if source['kind'] == 'context_source':
        anchor['source_ref'] = copy.deepcopy(source['source_ref'])
    return anchor


def request_quote_anchors(raw):
    """Publish the full current request and at most 32 exact nonempty lines."""
    if type(raw) is not str or not raw.strip() or len(raw.encode()) > 65536:
        raise ValueError('quote anchor request must be nonempty text of at most 64 KiB')
    source = {'kind': 'current_request', 'sha256': hashlib.sha256(raw.encode()).hexdigest()}
    anchors, offset = [_anchor(raw, 0, len(raw), source)], 0
    for line in raw.splitlines(keepends=True):
        literal = line.rstrip('\r\n')
        if literal.strip() and (offset != 0 or len(literal) != len(raw)):
            anchors.append(_anchor(raw, offset, offset + len(literal), source))
        offset += len(line)
        if len(anchors) == 33:
            break
    return anchors


def source_quote_anchors(source_ref, text, facts):
    """Address only the existing short excerpts containing explicit source facts."""
    ref = SourceRef.from_dict(source_ref).to_dict()
    source, anchors = {'kind': 'context_source', 'source_ref': ref}, {}
    for field, value in facts.items():
        parts = value if type(value) is list else [value]
        positions = [(text.find(part), len(part)) for part in parts if type(part) is str]
        if not positions or any(start < 0 for start, _ in positions):
            continue
        first = min(start for start, _ in positions)
        last = max(start + size for start, size in positions)
        if 0 < last - first <= 256:
            anchors[field] = _anchor(text, first, last, source)
    return anchors


def resolve_quote_ref(raw, quote_ref, source_anchors=()):
    """Resolve an exact published address; never search for similar model text."""
    if type(quote_ref) is not str or not re.fullmatch('q:[a-f0-9]{64}', quote_ref):
        raise ValueError('quote_ref must be an exact published quote address')
    matches = [row for row in [*request_quote_anchors(raw), *source_anchors]
               if row['quote_ref'] == quote_ref]
    if not matches or any(row != matches[0] for row in matches):
        raise ValueError('quote_ref is unknown or differs from the current verified sources')
    return copy.deepcopy(matches[0])
