"""Opted-in transport limits on the installed Hermes request builder.

The host still owns its client, streaming, retries, cancellation and turn loop.
Provider timeouts are socket I/O limits; the launcher's wall-clock deadline is
independent. This adapter never changes messages, tools or execution authority.
"""
from __future__ import annotations

from functools import wraps
import hashlib
import importlib
import json
import math
import os
import re
import stat


SCHEMA = 'rpe.hermes-request-budget/v1'
_FIELDS = frozenset({'schema_version', 'provider', 'model', 'max_completion_tokens',
    'thinking_type', 'request_timeout_seconds', 'stale_timeout_seconds'})
_CAP_FIELDS = ('max_tokens', 'max_completion_tokens', 'max_output_tokens')


class RequestBudgetBlocked(BaseException):
    """Stop this isolated host; ordinary middleware errors may be fail-open."""


def validate_request_budget_profile(profile):
    """Validate explicit limits; no prompt, tools, credentials or extra carriers."""
    if (type(profile) is not dict or set(profile) != _FIELDS
            or profile.get('schema_version') != SCHEMA or profile.get('provider') != 'xiaomi'
            or profile.get('thinking_type') != 'disabled'):
        raise ValueError('invalid Hermes request budget profile')
    model = profile.get('model')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,255}', model):
        raise ValueError('invalid Hermes request budget model')
    cap = profile.get('max_completion_tokens')
    if type(cap) is not int or not 1 <= cap <= 2400:
        raise ValueError('invalid Hermes request output limit')
    for name in ('request_timeout_seconds', 'stale_timeout_seconds'):
        value = profile.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 90:
            raise ValueError('invalid Hermes request I/O timeout')
    return dict(profile)


def _unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate request budget field')
        value[key] = item
    return value


def _load_profile(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16384:
            raise ValueError('request budget must be a bounded regular file')
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('request budget file grew beyond its bound')
    return validate_request_budget_profile(json.loads(raw, object_pairs_hook=_unique_fields))


def install_request_budget_adapter():
    """Wrap Hermes's builder in this opted-in process; never replace its SDK."""
    path = os.environ.get('RPE_HERMES_REQUEST_BUDGET_CONFIG')
    if not path:
        return
    try:
        profile = _load_profile(path)
        digest = hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(',', ':'),
            allow_nan=False).encode()).hexdigest()
        host = importlib.import_module('run_agent')
        timeouts = importlib.import_module('hermes_cli.timeouts')
        original = host.AIAgent._build_api_kwargs
        previous = getattr(original, '_rpe_request_budget_sha256', None)
        if previous:
            if previous != digest:
                raise ValueError('Hermes request budget is already frozen differently')
            return
        if not callable(original):
            raise ValueError('installed Hermes request builder differs')
    except (OSError, ValueError, TypeError, AttributeError, ImportError) as exc:
        raise RequestBudgetBlocked('Hermes request budget installation blocked') from exc

    @wraps(original)
    def bounded(agent, *args, **kwargs):
        if (agent.provider != profile['provider'] or agent.model != profile['model']
                or agent.api_mode != 'chat_completions'
                or timeouts.get_provider_request_timeout(agent.provider, agent.model)
                    != profile['request_timeout_seconds']
                or timeouts.get_provider_stale_timeout(agent.provider, agent.model)
                    != profile['stale_timeout_seconds']):
            raise RequestBudgetBlocked('Hermes runtime differs from the frozen request budget')
        request = original(agent, *args, **kwargs)
        if type(request) is not dict or request.get('model') != profile['model']:
            raise RequestBudgetBlocked('Hermes request builder returned an unexpected route')
        extra = request.get('extra_body', {})
        if type(extra) is not dict:
            raise RequestBudgetBlocked('Hermes request extra body is not a mapping')
        # Hermes length recovery may add an ephemeral max_tokens. Retain exactly
        # one output-cap carrier after the native builder and before middleware.
        request, extra = dict(request), dict(extra)
        for field in _CAP_FIELDS:
            request.pop(field, None)
            extra.pop(field, None)
        request['max_completion_tokens'] = profile['max_completion_tokens']
        extra['thinking'] = {'type': profile['thinking_type']}
        request['extra_body'] = extra
        request['timeout'] = profile['request_timeout_seconds']
        return request

    bounded._rpe_request_budget_sha256 = digest
    host.AIAgent._build_api_kwargs = bounded
