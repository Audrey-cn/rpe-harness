"""Opt-in native post-tool stop, bound to in-memory persisted IO receipts.

Stopping records a candidate or an executed artifact, never business acceptance.
No oracle, proposal controller, prompt mutation or extra model call is involved.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import inspect
import json
import os
import stat
import threading
from collections import Counter
from functools import wraps
from pathlib import Path

_IDS = ('session_id', 'task_id', 'turn_id', 'api_request_id')
_FINISHER = None


class NativeTurnFinisher:
    def __init__(self, evidence_config=None, execution_config=None):
        self.config, self.execution = evidence_config, execution_config
        self.rows, self.lock = [], threading.RLock()

    def record(self, kind, payload):
        # Called only after the real observer's durable write succeeds. Keeping
        # this private copy avoids treating a model-writable SQLite row as proof.
        with self.lock:
            self.rows.append({'kind': kind, **json.loads(json.dumps(payload))})

    def finish(self, agent, *, effective_task_id, messages):
        identity = (getattr(agent, 'session_id', None), effective_task_id,
            getattr(agent, '_current_turn_id', None), getattr(agent, '_current_api_request_id', None))
        if any(not isinstance(value, str) or not value for value in identity):
            return None
        tail = []
        for message in reversed(messages):
            if type(message) is not dict or message.get('role') != 'tool':
                break
            tail.append(message.get('tool_call_id'))
        if not tail or any(not isinstance(value, str) or not value for value in tail):
            return None
        with self.lock:
            rows = [row for row in self.rows if tuple(row.get(k) for k in _IDS) == identity]
        kinds = Counter(row['kind'] for row in rows)
        if (kinds['pre_api_request'] != 1 or kinds['post_api_request'] != 1
                or kinds['api_request_error']):
            return None
        api = next(row for row in rows if row['kind'] == 'post_api_request')
        calls = api.get('model_tool_calls', [])
        expected = Counter((row.get('tool_call_id'), row.get('tool_name')) for row in calls)
        before = [row for row in rows if row['kind'] == 'pre_tool_call']
        after = [row for row in rows if row['kind'] == 'post_tool_call']
        if (not expected or any(count != 1 for count in expected.values())
                or Counter((r.get('tool_call_id'), r.get('tool_name')) for r in before) != expected
                or Counter((r.get('tool_call_id'), r.get('tool_name')) for r in after) != expected
                or Counter(tail) != Counter(call[0] for call in expected)
                or any(row.get('status') not in ('ok', 'succeeded', 'loaded') for row in after)
                or any(row.get('tool_name') == 'terminal' and row.get('tool_exit_code') != 0 for row in after)):
            return None
        for post in after:
            pre = next(r for r in before if r['tool_call_id'] == post['tool_call_id'])
            if not pre.get('arguments_sha256') or pre.get('arguments_sha256') != post.get('arguments_sha256'):
                return None
        receipt_kind = 'native_helper_process_observation' if self.execution else 'native_artifact_observation'
        receipts = [row for row in rows if row['kind'] == receipt_kind and row.get('verified') is True]
        if len(receipts) != 1:
            return None
        receipt = receipts[0]
        post = next((row for row in after if row['tool_call_id'] == receipt.get('tool_call_id')
            and row['tool_name'] == receipt.get('tool_name')
            and row['arguments_sha256'] == receipt.get('arguments_sha256')), None)
        if post is None:
            return None
        if self.execution:
            from . import hermes_native_process as native
            output = native.file_snapshot(Path(self.execution['workspace'])/self.execution['output_name'])
            signature = receipt.get('process_receipt_hmac_sha256')
            body = {k:v for k,v in receipt.items() if k not in ('kind','process_receipt_hmac_sha256')}
            if (native._SIGNING_KEY is None or not isinstance(signature, str)
                    or not hmac.compare_digest(signature, native.receipt_signature(body, native._SIGNING_KEY))
                    or receipt.get('kernel_exit_observed') is not True or receipt.get('process_returncode') != 0
                    or receipt.get('observer_joined') is not True or receipt.get('overlap') is not False
                    or receipt.get('process_config_sha256') != native._digest(self.execution)
                    or receipt.get('process_observation_error')
                    or output.get('state') != 'regular' or output.get('sha256') != receipt.get('output_sha256')
                    or native._digest(output.get('identity')) != receipt.get('output_identity_sha256')):
                return None
            name, digest = self.execution['output_name'], output['sha256']
            if 'stdout_name' in self.execution:
                stdout_path = Path(self.execution['workspace'])/self.execution['stdout_name']
                stdout = native.file_snapshot(stdout_path)
                if (receipt.get('artifact_binding') != 'same_call_final_bytes_equal_observed_stdout_not_exclusive_writer'
                        or stdout.get('state') != 'regular'
                        or stdout.get('sha256') != receipt.get('stdout_sha256')
                        or stdout.get('size') != receipt.get('stdout_size')
                        or native._digest(stdout.get('identity')) != receipt.get('stdout_identity_sha256')
                        or hashlib.sha256(str(stdout_path).encode()).hexdigest() != receipt.get('stdout_path_sha256')
                        or stdout.get('sha256') != output.get('sha256')
                        or stdout.get('size') != output.get('size')):
                    return None
        elif self.config and 'artifact_name' in self.config:
            from . import hermes_native_process as native
            from .hermes_native import evaluate_artifact_receipt
            verdict = evaluate_artifact_receipt(receipt, self.config, native._SIGNING_KEY)
            if verdict['verified'] is not True:
                return None
            name, digest = self.config['artifact_name'], verdict['output_sha256']
        elif self.config:
            from .hermes_native import _snapshot
            path = Path(self.config['workspace'])/'draft.json'
            observed = _snapshot(path)
            if (receipt.get('status') != 'created' or observed.get('state') != 'regular'
                    or observed.get('sha256') != receipt.get('content_sha256')
                    or observed.get('size') != receipt.get('bytes_written')
                    or receipt.get('target_path_sha256') != hashlib.sha256(str(path).encode()).hexdigest()):
                return None
            try:
                # Syntax is the candidate boundary; business correctness is
                # assessed later, and never determines whether to keep sampling.
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
                    metadata = os.fstat(stream.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
                        return None
                    raw = stream.read(65537)
                if (len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != observed['sha256']
                        or type(json.loads(raw)) is not dict):
                    return None
            except (OSError, ValueError):
                return None
            name, digest = 'draft.json', observed['sha256']
        else:
            return None
        return {'status': 'native_artifact_recorded', 'artifact_name': name,
            'content_sha256': digest, 'accepted': False}


def install_native_finish_adapter():
    if os.environ.get('RPE_HERMES_NATIVE_FINISH_ENABLED') != '1':
        return
    if os.environ.get('RPE_HERMES_TOOL_ENABLED') != '0':
        raise RuntimeError('native finish requires native tools')
    rounds = importlib.import_module('agent.turn_tool_round')
    preflight = importlib.import_module('agent.turn_preflight')
    original = rounds.compress_after_tool_results
    if getattr(original, '_rpe_native_finish_adapter', False):
        return
    fields = ('messages','active_system_prompt','conversation_history','compression_attempts','current_turn_user_idx')
    if not {*fields,'effective_task_id'} <= set(inspect.signature(original).parameters):
        raise RuntimeError('installed Hermes native end-turn seam differs')

    @wraps(original)
    def finish_or_compress(agent, **context):
        terminal = None if _FINISHER is None else _FINISHER.finish(agent,
            effective_task_id=context['effective_task_id'], messages=context['messages'])
        if terminal is None:
            return original(agent, **context)
        return preflight.PostToolCompressionVerdict(end_turn=True,
            **{name:context[name] for name in fields},
            final_response=json.dumps(terminal,sort_keys=True,separators=(',',':')),
            turn_exit_reason='rpe_native_artifact_recorded')
    finish_or_compress._rpe_native_finish_adapter = True
    rounds.compress_after_tool_results = finish_or_compress
