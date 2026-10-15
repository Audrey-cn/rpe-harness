"""Opted-in process-local review finish through installed Hermes's native seam."""
from __future__ import annotations

from functools import wraps
import importlib
import inspect
import json
import os


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
