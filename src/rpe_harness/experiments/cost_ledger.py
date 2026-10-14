"""Immutable phase reservations and observed costs, including failed attempts.

This ledger does not estimate billing or infer missing model usage. Shared
cost allocation belongs to a comparison, not to the measured whole-run sum.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

from ..path_binding import _directory, read_regular_file
from ..storage import canonical_json

_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_USAGE = frozenset({'api_requests', 'api_errors', 'missing_observations',
    'requests_with_usage', 'coverage_complete', 'known_total_tokens',
    'total_tokens', 'prompt_tokens', 'output_tokens', 'input_tokens',
    'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens'})
_OUTCOME = frozenset({'status', 'elapsed_seconds', 'usage', 'model_identity',
    'artifact_correct', 'workflow_verified', 'candidate_reused', 'fallback',
    'candidate_sha256', 'version_id', 'error_code', 'observed_peak_rss_bytes'})
_STATUS = frozenset({'completed', 'failed', 'timed_out', 'refused'})


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError('ledger names must be bounded plain identifiers')
    return value


def _number(value):
    if type(value) not in {float, int} or not math.isfinite(value) or value < 0:
        raise ValueError('cost must be a finite nonnegative number')
    return value


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _usage(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - _USAGE:
        raise ValueError('unknown usage field')
    result = {}
    for key, item in value.items():
        if key == 'coverage_complete':
            if type(item) is not bool:
                raise ValueError('usage coverage must be boolean')
        elif item is not None and (type(item) is not int or item < 0):
            raise ValueError('usage counts must be nonnegative integers or null')
        result[key] = item
    total, known = result.get('total_tokens'), result.get('known_total_tokens')
    if total is not None and known is not None and total != known:
        raise ValueError('total and known token counts disagree')
    if result.get('coverage_complete') is True and (total is None or result.get('missing_observations', 0)):
        raise ValueError('complete usage requires a known total and no missing observation')
    return result


def _identity(value):
    if not isinstance(value, dict) or set(value) - {'provider', 'requested_model', 'response_models'}:
        raise ValueError('identity must contain only public model identifiers')
    result = {}
    for key in ('provider', 'requested_model'):
        if key in value:
            item = value[key]
            if not isinstance(item, str) or len(item) > 256 or any(ord(c) < 32 for c in item):
                raise ValueError('invalid model identifier')
            result[key] = item
    responses = value.get('response_models', [])
    if not isinstance(responses, list) or len(responses) > 16:
        raise ValueError('invalid response model identifiers')
    for item in responses:
        if not isinstance(item, str) or len(item) > 256 or any(ord(c) < 32 for c in item):
            raise ValueError('invalid response model identifier')
    result['response_models'] = responses
    return result


class PhaseLedger:
    def __init__(self, root):
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        if self.root.is_symlink():
            raise ValueError('ledger root must not be a symlink')
        if not self.root.exists():
            fd = _directory(self.root.parent)
            try:
                os.mkdir(self.root.name, mode=0o700, dir_fd=fd)
            finally:
                os.close(fd)
        fd = _directory(self.root)
        os.close(fd)

    def _write(self, name, value):
        document = {**value, 'record_sha256': _digest(value)}
        raw = canonical_json(document).encode()
        if len(raw) > 65536:
            raise ValueError('ledger record exceeds 64KiB')
        fd = _directory(self.root)
        try:
            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=fd)
            with os.fdopen(output, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(fd)
        finally:
            os.close(fd)
        return document

    def _read(self, name):
        value = json.loads(read_regular_file(self.root/name, max_bytes=65536))
        if not isinstance(value, dict):
            raise ValueError('invalid ledger record')
        saved = value.pop('record_sha256', None)
        if saved != _digest(value):
            raise ValueError('ledger record changed')
        return {**value, 'record_sha256': saved}

    def reserve(self, arm, phase, attempt_id, *, api_expected=True, scope='operational'):
        arm, phase, attempt_id = map(_name, (arm, phase, attempt_id))
        if type(api_expected) is not bool or scope not in {'operational', 'research'}:
            raise ValueError('invalid ledger scope or API declaration')
        return self._write(attempt_id+'.reservation.json', {
            'schema_version': 'rpe.phase-ledger/v1', 'arm': arm, 'phase': phase,
            'attempt_id': attempt_id, 'api_expected': api_expected, 'scope': scope,
            'reserved_at': time.time(), 'status': 'incomplete'})

    def finish(self, attempt_id, outcome):
        attempt_id = _name(attempt_id)
        reservation = self._read(attempt_id+'.reservation.json')
        if not isinstance(outcome, dict) or set(outcome)-_OUTCOME or outcome.get('status') not in _STATUS:
            raise ValueError('invalid or private outcome fields')
        result = {'status': outcome['status'], 'elapsed_seconds': _number(outcome.get('elapsed_seconds')),
                  'usage': _usage(outcome.get('usage'))}
        if not reservation['api_expected'] and result['usage'] and result['usage'].get('api_requests', 0):
            raise ValueError('unexpected API usage in a local-only phase')
        for key in ('artifact_correct', 'workflow_verified', 'candidate_reused', 'fallback'):
            if key in outcome:
                if type(outcome[key]) is not bool:
                    raise ValueError('outcome predicates must be boolean')
                result[key] = outcome[key]
        if 'model_identity' in outcome and outcome['model_identity'] is not None:
            result['model_identity'] = _identity(outcome['model_identity'])
        for key in ('candidate_sha256', 'version_id', 'error_code'):
            if key in outcome and outcome[key] is not None:
                result[key] = _name(outcome[key])
        if 'observed_peak_rss_bytes' in outcome:
            value = outcome['observed_peak_rss_bytes']
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('invalid observed RSS')
            result['observed_peak_rss_bytes'] = value
        return self._write(attempt_id+'.outcome.json', {
            'attempt_id': attempt_id, 'reservation_sha256': reservation['record_sha256'],
            'finished_at': time.time(), **result})

    def records(self):
        result = []
        for path in sorted(self.root.glob('*.reservation.json')):
            reservation = self._read(path.name)
            attempt_id = _name(reservation['attempt_id'])
            if path.name != attempt_id+'.reservation.json':
                raise ValueError('ledger reservation name mismatch')
            try:
                outcome = self._read(attempt_id+'.outcome.json')
            except FileNotFoundError:
                outcome = {'status': 'incomplete', 'elapsed_seconds': None, 'usage': None}
            else:
                if (outcome['attempt_id'] != attempt_id or
                        outcome['reservation_sha256'] != reservation['record_sha256']):
                    raise ValueError('ledger outcome binding mismatch')
            result.append({**reservation, **outcome})
        return result


def summarize_phases(records):
    """Add observed counts once per phase; unknowns and RSS remain distinct."""
    records = list(records)
    if len({r['attempt_id'] for r in records}) != len(records):
        raise ValueError('duplicate cost phase')
    token_sum = request_sum = elapsed_sum = 0
    token_complete = request_complete = elapsed_complete = True
    missing = 0
    detailed_fields = ('cache_read_tokens', 'cache_write_tokens', 'input_tokens',
                       'prompt_tokens', 'output_tokens', 'reasoning_tokens')
    detail_sums = {field: 0 for field in detailed_fields}
    detail_complete = {field: True for field in detailed_fields}
    peaks = []
    starts, ends = [], []
    for record in records:
        elapsed = record.get('elapsed_seconds')
        if elapsed is None:
            elapsed_complete = False
        else:
            elapsed_sum += _number(elapsed)
        if record.get('reserved_at') is not None:
            starts.append(record['reserved_at'])
        if record.get('finished_at') is not None:
            ends.append(record['finished_at'])
        peak = record.get('observed_peak_rss_bytes')
        if peak is not None:
            peaks.append(peak)
        if not record['api_expected']:
            continue
        value = record.get('usage') or {}
        tokens, requests = value.get('total_tokens'), value.get('api_requests')
        token_sum += value.get('known_total_tokens') or tokens or 0
        request_sum += requests or 0
        token_complete &= type(tokens) is int and value.get('coverage_complete') is True
        request_complete &= type(requests) is int
        missing += value.get('missing_observations') or (0 if value else 1)
        for field in detailed_fields:
            item = value.get(field)
            if type(item) is int:
                detail_sums[field] += item
            else:
                detail_complete[field] = False
    result = {'phase_count': len(records),
        'failed_phases': sum(r['status'] in {'failed', 'timed_out'} for r in records),
        'incomplete_phases': sum(r['status'] == 'incomplete' for r in records),
        'total_tokens': token_sum if token_complete else None, 'known_total_tokens': token_sum,
        'api_requests': request_sum if request_complete else None, 'known_api_requests': request_sum,
        'usage_complete': bool(token_complete), 'missing_observations': missing,
        'elapsed_seconds_sum': elapsed_sum if elapsed_complete else None,
        'known_elapsed_seconds_sum': elapsed_sum,
        'observed_makespan_seconds': max(ends)-min(starts) if starts and ends else None,
        'observed_peak_rss_bytes': max(peaks) if peaks else None,
        'rss_observation_coverage': len(peaks), 'billing_verified_cost': None}
    for field in detailed_fields:
        result[field] = detail_sums[field] if detail_complete[field] else None
        result['known_'+field] = detail_sums[field]
    return result
