"""Opted-in process-local review finish through installed Hermes's native seam."""
from __future__ import annotations

from functools import wraps
from dataclasses import asdict
import importlib
import inspect
import json
import os


def install_review_response_adapter():
    """Use the native response-intake verdict before any tool can dispatch."""
    if os.environ.get('RPE_HERMES_ENTRY_MODE') != 'review':
        return
    loop = importlib.import_module('agent.conversation_loop')
    _install_review_response_check_adapter(loop)
    original = loop.normalize_model_response
    if getattr(original, '_rpe_review_response_adapter', False):
        return
    required = {'response', 'api_request_id', 'effective_task_id'}
    if not required <= set(inspect.signature(original).parameters):
        raise RuntimeError('installed Hermes response-intake seam differs')

    @wraps(original)
    def intake(agent, **context):
        from .hermes_review import ReviewIngressBlocked, recover_review_response
        from .review_diagnostics import append_response, response_summary
        native = {'session_id': getattr(agent, 'session_id', None),
            'turn_id': getattr(agent, '_current_turn_id', None),
            'task_id': context.get('effective_task_id'), 'api_request_id': context.get('api_request_id')}
        try:
            verdict = original(agent, **context)
        except BaseException as error:
            append_response(response_summary(native, None, stage='normalization',
                failure_code='review_native_normalization_failed'))
            if isinstance(error, Exception):
                raise ReviewIngressBlocked('review native normalization failed', code='review_native_normalization_failed') from None
            raise
        try:
            response = asdict(verdict.assistant_message)
        except BaseException as error:
            append_response(response_summary(native, None, native_action=getattr(verdict, 'action', None),
                stage='serialization', failure_code='review_response_serialization_failed'))
            if isinstance(error, Exception):
                raise ReviewIngressBlocked('review response serialization failed', code='review_response_serialization_failed') from None
            raise
        try:
            retry = recover_review_response(agent, effective_task_id=context['effective_task_id'],
                api_request_id=context['api_request_id'], response=response,
                intake_action=verdict.action)
        except (TypeError, ValueError, AttributeError) as error:
            raise ReviewIngressBlocked('review protocol response serialization failed') from error
        if retry:
            verdict.action = 'continue'
        return verdict

    intake._rpe_review_response_adapter = True
    loop.normalize_model_response = intake


def _install_review_response_check_adapter(loop):
    """Abort incomplete replies before Hermes can arm a length/fallback restart."""
    original = loop.check_api_response
    if getattr(original, '_rpe_review_response_check_adapter', False):
        return
    if not {'response', 'api_request_id', 'effective_task_id'} <= set(inspect.signature(original).parameters):
        raise RuntimeError('installed Hermes response-check seam differs')

    @wraps(original)
    def check(agent, **context):
        from .hermes_review import ReviewIngressBlocked, ReviewWorkflowStore
        from .review_diagnostics import append_response, response_summary
        from .hermes import HostEventStore
        native = {'session_id': agent.session_id, 'turn_id': context.get('turn_id'),
            'task_id': context.get('effective_task_id'), 'api_request_id': context.get('api_request_id')}
        code = 'review_native_response_before_intake_blocked'
        finish, summary, phase, expected = None, {}, None, None
        try:
            # This is the host's actual finish derivation, before its own retry paths.
            derive = importlib.import_module('agent.turn_response_check')._derive_finish_reason
            finish = derive(agent, context['response'], context['messages'])
            if finish in ('stop', 'tool_calls'):
                return original(agent, **context)
            normalized = agent._get_transport().normalize_response(context['response'])
            summary = {'finish_reason': finish, 'tool_calls': normalized.tool_calls or [],
                       'provider_data': normalized.provider_data}
            store = HostEventStore(os.environ['RPE_HERMES_DB'])
            state = ReviewWorkflowStore(store.path).get({**native,
                'entry_key': os.environ['RPE_HERMES_ENTRY_KEY']}, create=False)
            planned = state.get('policies', {}).get(native['api_request_id'], {})
            phase, expected = planned.get('phase'), planned.get('choice')
            outcomes = [event for event in store.list(native['session_id']) if
                event['kind'] in ('post_api_request', 'api_request_error') and
                all(event.get(key) == value for key, value in native.items())]
            if not outcomes:
                # An adapter-owned rejection outcome, not a provider transport
                # error or a forged post/intake verdict. Count the known usage once.
                usage = agent._usage_summary_for_api_request_hook(context['response'])
                store.record('api_request_error', {**native, 'finish_reason': finish,
                    'error_type': 'RPEReviewResponseCheckBlocked', 'error_code': code, 'usage': usage})
        except ReviewIngressBlocked:
            raise
        except Exception:
            # Unknown response/check/telemetry errors cannot enable native retries.
            pass
        append_response(response_summary(native, summary or {'finish_reason': finish}, phase=phase,
            expected_tool=expected, stage='response_check', failure_code=code))
        raise ReviewIngressBlocked('review native response blocked before intake', code=code)

    check._rpe_review_response_check_adapter = True
    loop.check_api_response = check


def install_review_finish_adapter():
    """Wrap the binding read by run_tool_round; never edit the installed host."""
    if os.environ.get('RPE_HERMES_ENTRY_MODE') != 'review':
        return
    rounds = importlib.import_module('agent.turn_tool_round')
    preflight = importlib.import_module('agent.turn_preflight')
    original = rounds.compress_after_tool_results
    if getattr(original, '_rpe_review_finish_adapter', False):
        return
    fields = ('messages', 'active_system_prompt', 'conversation_history',
              'compression_attempts', 'current_turn_user_idx')
    if not set((*fields, 'effective_task_id')) <= set(inspect.signature(original).parameters):
        raise RuntimeError('installed Hermes post-tool end-turn seam differs')

    @wraps(original)
    def finish_or_compress(agent, **context):
        from .hermes_review import ReviewIngressBlocked, finish_review_turn
        terminal = finish_review_turn(agent, effective_task_id=context['effective_task_id'],
            messages=context['messages'])
        if terminal is None:
            return original(agent, **context)
        identity = terminal.get('proposal_id') if type(terminal) is dict else None
        if (type(terminal) is not dict
                or terminal.get('status') not in ('awaiting_confirmation', 'needs_context', 'unsupported')
                or not isinstance(identity, str) or len(identity) != 64
                or any(char not in '0123456789abcdef' for char in identity)
                or terminal.get('proposal_sha256') != identity):
            raise ReviewIngressBlocked('review terminal status is not a durable proposal')
        return preflight.PostToolCompressionVerdict(end_turn=True,
            **{name: context[name] for name in fields},
            final_response=json.dumps(terminal, ensure_ascii=False, allow_nan=False,
                                      sort_keys=True, separators=(',', ':')),
            turn_exit_reason='rpe_review_proposal_recorded')

    finish_or_compress._rpe_review_finish_adapter = True
    rounds.compress_after_tool_results = finish_or_compress
