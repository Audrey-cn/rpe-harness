"""Bounded metadata-only review diagnostics. These records grant no authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

MAX_JOURNAL = 512 * 1024
MAX_ENVELOPE = 64 * 1024
TOOLS = {'rpe_discover', 'rpe_select', 'skill_view', 'rpe_propose', 'none'}
PHASES = {'discover', 'select', 'view', 'propose', 'none'}
CODES = {'review_protocol_native_API_identity_differs', 'review_protocol_native_response_not_dispatchable',
    'review_protocol_recovery_not_allowed', 'review_protocol_response_observation_missing',
    'review_protocol_response_observation_differs', 'review_protocol_response_invalid',
    'review_protocol_unexpected_error', 'review_unexpected_phase_repeat',
    'review_protocol_wrong_tool_or_identity', 'review_protocol_arguments_invalid', 'review_native_response_before_intake_blocked',
    'review_raw_arguments_invalid', 'review_argument_capture_failed', 'review_argument_admission_failed',
    'review_native_normalization_failed',
    'review_response_serialization_failed', 'review_plugin_unavailable',
    'api_content_filter', 'api_request_error', 'host_or_observation_incomplete',
    'terminal_session_mismatch', 'credential_in_host_response', 'postlaunch_diagnostics_failed',
    'review_unverified', 'diagnostic_persistence_failed', 'diagnostic_limit_exceeded', 'diagnostic_invalid', 'diagnostic_missing'}


def failure(code, *, domain='protocol', stage='response_guard'):
    return {'domain': domain, 'code': code if code in CODES else 'review_unverified',
            'stage': stage, 'retryable': False}


def _enum(value, allowed):
    return value if type(value) is str and value in allowed else None


def _identity(value):
    return value if type(value) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) else None


def response_summary(native, response, *, phase=None, expected_tool=None, native_action=None,
                     rpe_action='blocked', failure_code=None, stage='response_guard'):
    response = response if type(response) is dict else {}
    data = response.get('provider_data')
    calls = response.get('tool_calls')
    return {**{key: _identity(native.get(key)) for key in
                ('session_id', 'task_id', 'turn_id', 'api_request_id')},
        'stage': _enum(stage, {'normalization', 'serialization', 'response_guard', 'ingress', 'api', 'response_check', 'argument_capture', 'argument_admission', 'request_policy'}),
        'phase': _enum(phase, PHASES), 'expected_tool': _enum(expected_tool, TOOLS),
        'intake': 'not_reached' if stage in ('response_check', 'api') else 'reached' if stage == 'response_guard' else None,
        'native_action': _enum(native_action, {'fallthrough', 'continue', 'return'}),
        'finish_reason': _enum(response.get('finish_reason'),
                              {'stop', 'tool_calls', 'length', 'content_filter', 'incomplete'}),
        'refusal_field_present': ('refusal' in data) if type(data) is dict else
                                  False if data is None and 'provider_data' in response else None,
        'tool_call_count': len(calls) if type(calls) is list and len(calls) < 2**31 else None,
        'rpe_action': _enum(rpe_action, {'accepted', 'recovery', 'blocked'}),
        'failure_code': _enum(failure_code, CODES), 'diagnostic_only': True}


def _project(row):
    """Reproject even private journal data; arbitrary keys and strings never escape."""
    if type(row) is not dict:
        raise ValueError('invalid diagnostic row')
    count = row.get('tool_call_count')
    result = response_summary(row, {}, phase=row.get('phase'), expected_tool=row.get('expected_tool'),
        native_action=row.get('native_action'), rpe_action=row.get('rpe_action'),
        failure_code=row.get('failure_code'), stage=row.get('stage'))
    result['finish_reason'] = _enum(row.get('finish_reason'),
        {'stop', 'tool_calls', 'length', 'content_filter', 'incomplete'})
    result['refusal_field_present'] = row.get('refusal_field_present') if type(row.get('refusal_field_present')) is bool else None
    result['tool_call_count'] = count if type(count) is int and 0 <= count < 2**31 else None
    return result


def append_response(row):
    """Best effort private append with a hard byte cap; never change a gate decision."""
    path = os.environ.get('RPE_HERMES_REVIEW_DIAGNOSTICS')
    if not path:
        return
    descriptor = None
    try:
        import fcntl
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return
        os.fchmod(descriptor, 0o600)
        raw = (json.dumps(_project(row), ensure_ascii=True, separators=(',', ':')) + '\n').encode()
        marker = b'{"failure_code":"diagnostic_limit_exceeded"}\n'
        if metadata.st_size + len(raw) <= MAX_JOURNAL - len(marker):
            os.write(descriptor, raw)
        elif metadata.st_size + len(marker) <= MAX_JOURNAL:
            os.write(descriptor, marker)
        os.fsync(descriptor)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def collect_diagnostics(path, result=None, *, secret=''):
    result = result or {}
    reasons, rows = [], []
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('invalid diagnostics file')
        if metadata.st_size > MAX_JOURNAL:
            reasons.append('diagnostic_limit_exceeded')
        raw = os.read(descriptor, MAX_JOURNAL + 1)
        for line in raw.splitlines():
            if len(rows) >= 32:
                reasons.append('diagnostic_limit_exceeded')
                break
            if len(line) > 2048:
                reasons.append('diagnostic_invalid')
                continue
            try:
                value = json.loads(line)
                if type(value) is not dict:
                    raise ValueError('invalid row')
                if value.get('failure_code') == 'diagnostic_limit_exceeded':
                    reasons.append('diagnostic_limit_exceeded')
                    continue
                row = _project(value)
                if secret and secret in json.dumps(row):
                    reasons.append('diagnostic_invalid')
                    continue
                if row != value:
                    reasons.append('diagnostic_invalid')
                rows.append(row)
            except (ValueError, TypeError, UnicodeError):
                reasons.append('diagnostic_invalid')
    except FileNotFoundError:
        reasons.append('diagnostic_missing')
    except (OSError, ValueError, TypeError):
        reasons.append('diagnostic_invalid')
    finally:
        if descriptor is not None:
            os.close(descriptor)
    events = result.get('events') if type(result.get('events')) is list else []
    observed = {tuple(e.get(k) for k in ('session_id', 'task_id', 'turn_id', 'api_request_id'))
                for e in events if e.get('kind') == 'post_api_request'}
    recorded = {tuple(e.get(k) for k in ('session_id', 'task_id', 'turn_id', 'api_request_id')) for e in rows}
    if observed - recorded:
        reasons.append('diagnostic_missing')
    guarded = [row for row in rows if row.get('stage') == 'response_guard']
    guarded_ids = [tuple(e.get(k) for k in ('session_id', 'task_id', 'turn_id', 'api_request_id')) for e in guarded]
    if set(guarded_ids) - observed or len(set(guarded_ids)) != len(guarded_ids) or any(None in ids for ids in guarded_ids):
        reasons.append('diagnostic_invalid')
    for event in events:
        if event.get('kind') == 'api_request_error':
            if len(rows) >= 32:
                reasons.append('diagnostic_limit_exceeded')
                break
            adapter = (event.get('error_type') == 'RPEReviewResponseCheckBlocked' and
                       event.get('error_code') == 'review_native_response_before_intake_blocked')
            code = 'review_native_response_before_intake_blocked' if adapter else (
                'api_content_filter' if event.get('error_type') == 'ContentPolicyBlocked' else 'api_request_error')
            if adapter and any(row.get('api_request_id') == event.get('api_request_id') and
                    row.get('session_id') == event.get('session_id') and row.get('stage') == 'response_check' for row in rows):
                continue
            rows.append(response_summary(event, {'finish_reason': event.get('finish_reason')} if adapter else None,
                stage='response_check' if adapter else 'api', failure_code=code))
    if not rows:
        reasons.append('diagnostic_missing')
    failures = [failure(row['failure_code'], domain='model' if row['stage'] in ('api', 'normalization', 'response_check') or row['failure_code'] == 'review_protocol_native_response_not_dispatchable' else 'protocol',
                        stage=row['stage']) for row in rows if row.get('failure_code')]
    if result.get('failure'):
        item = result['failure']
        failures.append(failure(item.get('code'), domain=_enum(item.get('domain'), {'protocol','host','observation','model'}) or 'observation',
            stage=_enum(item.get('stage'), {'response_guard','finalization','session','normalization','serialization','api','ingress','response_check','argument_capture','argument_admission','request_policy'}) or 'finalization'))
    if (result.get('completed') is not True or result.get('observer_errors') or result.get('timed_out')
            or (result.get('process_cleanup') or {}).get('cleanup_complete') is not True):
        failures.append(failure('host_or_observation_incomplete', domain='host', stage='session'))
        reasons.append('host_or_observation_incomplete')
    if len(failures) > 32:
        reasons.append('diagnostic_limit_exceeded')
    envelope = {'schema_version': 1, 'diagnostic_only': True,
        'coverage': {'complete': not reasons, 'reasons': sorted(set(reasons))},
        'responses': rows, 'failures': failures[:32]}
    assert len(json.dumps(envelope).encode()) <= MAX_ENVELOPE
    return envelope


def persist_diagnostics(database, diagnostics):
    """Parent-owned atomic artifact beside the trusted DB, before temporary HOME deletion."""
    directory = Path(database).absolute().parent / 'review-diagnostics'
    directory.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = uuid.uuid4().hex + '.tmp'
    name = uuid.uuid4().hex + '.json'
    try:
        raw = json.dumps(diagnostics, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
        if len(raw) > MAX_ENVELOPE:
            raise ValueError('diagnostic envelope limit exceeded')
        output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        try:
            with os.fdopen(output, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
    finally:
        os.close(descriptor)
    return {'path': str(directory / name), 'sha256': hashlib.sha256(raw).hexdigest()}
