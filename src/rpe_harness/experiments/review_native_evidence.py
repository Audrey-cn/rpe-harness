"""Strict 1.12 native evidence helpers; legacy experiment scoring is unchanged."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3

from ..path_binding import read_regular_file
from ..skill_catalog import XLSX_SKILL_ID
from .address_comparison import _native_calls
from .skill_comparison import exact_json


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def native_review_prompt(raw, model_sources=(), *, cold_skill=False, host_python=None, hermes_root=None):
    """Publish the same anchors as the review arm, with a visible raw/support boundary."""
    from ..review_quotes import request_quote_anchors
    support = {'original_request': raw, 'request_quote_anchors': request_quote_anchors(raw),
               'authorized_sources': list(model_sources)}
    if cold_skill:
        if (not isinstance(host_python, str) or not Path(host_python).is_absolute()
                or not isinstance(hermes_root, str) or not Path(hermes_root).is_absolute()):
            raise ValueError('cold native prompt requires the installed host Python and repository')
        support['skill_availability'] = {
            'home': 'current HERMES_HOME only; cold copies live in skills/.archive',
            'restore_command': shlex.join(['env', 'PYTHONPATH='+hermes_root, host_python,
                '-B', '-m', 'hermes_cli.main', 'curator', 'restore', 'xlsx']),
            'view': 'After successful native restore, call native skill_view(name="xlsx").',
            'authority': 'Availability metadata grants no additional business capability or dependency installation.'}
    return (raw + '\n\nThe preceding text is the complete original_request. The support_metadata below '
            'contains only authorized exact quote anchors, source attachments and runtime availability. '
            'Only original_request or the explicitly identified source quotes may be cited as requirements; '
            'availability instructions are not user-request quotes. Generate and save your own draft.json '
            'through your actual native tools; final response text does not publish a draft.\n'
            'support_metadata=' + json.dumps(support, ensure_ascii=False, sort_keys=True, allow_nan=False))


def native_draft_trace(raw, draft, model_sources=()):
    """Resolve published IDs without rewriting candidates or accepting support-text quotes."""
    from ..review_quotes import resolve_quote_ref
    from ..context import SourceRef
    try:
        execution = draft['execution']
        required = {'/capability', '/constraints', *('/execution/'+key for key in execution)}
        rows = draft.get('requirements')
        if type(rows) is not list or not 1 <= len(rows) <= 32:
            return False
        anchors = []
        for source in model_sources:
            for field, ref in source.get('quote_refs', {}).items():
                anchors.append({'quote_ref': ref, 'quote': source['quotes'][field],
                    'span': source['quote_spans'][field], 'source_ref': source['source_ref']})
        recovered = {'/execution/'+field for source in model_sources for field, value in source['facts'].items()
                     if field in execution and exact_json(execution[field], value)}
        if any('constraints' in source['facts'] and exact_json(source['facts']['constraints'], draft.get('constraints'))
               for source in model_sources):
            recovered.add('/constraints')
        covered, cited = set(), set()
        for original in rows:
            row = copy.deepcopy(original)
            if (type(row) is not dict or set(row)-{'text', 'quote', 'fields', 'source_ref', 'quote_ref'}
                    or type(row.get('text')) is not str or not row['text'].strip()
                    or type(row.get('fields')) is not list or not row['fields']
                    or any(type(field) is not str for field in row['fields'])
                    or len(set(row['fields'])) != len(row['fields']) or set(row['fields'])-required):
                return False
            if 'quote_ref' in row:
                anchor = resolve_quote_ref(raw, row['quote_ref'], anchors)
                for key in ('quote', 'source_ref'):
                    if key in row and row[key] != anchor.get(key):
                        return False
                row.update({key: anchor[key] for key in ('quote', 'source_ref') if key in anchor})
            if 'quote' not in row:
                return False
            if row.get('source_ref') is None:
                if row['quote'] is not None and (type(row['quote']) is not str or not row['quote'] or row['quote'] not in raw):
                    return False
            else:
                reference = SourceRef.from_dict(row['source_ref']).to_dict()
                matches = [source for source in model_sources if source['source_ref'] == reference]
                if len(matches) != 1 or type(row['quote']) is not str or not row['quote']:
                    return False
                source = matches[0]
                if not any(row['quote'] in quote for quote in source['quotes'].values()):
                    return False
                for pointer in row['fields']:
                    field = 'constraints' if pointer == '/constraints' else pointer.removeprefix('/execution/')
                    value = draft['constraints'] if field == 'constraints' else execution.get(field)
                    if field not in source['facts'] or not exact_json(source['facts'][field], value):
                        return False
                cited.update(row['fields'])
            covered.update(row['fields'])
        return covered == required and recovered <= cited
    except (ValueError, TypeError, KeyError):
        return False


def _same_call(left, right):
    return all(left.get(key) == right.get(key) for key in
               ('session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id', 'tool_name', 'arguments_sha256'))


def evaluate_native_review_draft(result, plan, trial):
    """Require a unique real IO transition bound to the durable native tool invocation."""
    verdict = {'verified': False, 'status': 'unverified', 'errors': [], 'proof_kind': 'native_io_receipt'}
    try:
        posts, _streams = _native_calls(result, trial['db'])
        rows = [event for event in result['events'] if event.get('kind') == 'native_artifact_observation']
        if len(rows) != 1 or rows[0].get('status') != 'created' or rows[0].get('verified') is not True:
            raise ValueError('native_unique_new_draft_receipt_missing')
        receipt = rows[0]
        matching = [post for post in posts if _same_call(post, receipt)]
        if len(matching) != 1 or matching[0].get('status') not in {'ok', 'succeeded'}:
            raise ValueError('native_draft_receipt_call_mismatch')
        if matching[0].get('tool_name') == 'terminal' and matching[0].get('tool_exit_code') != 0:
            raise ValueError('native_draft_terminal_not_completed')
        target = Path(trial['workspace']).resolve()/'draft.json'
        raw = read_regular_file(target, max_bytes=65536)
        if (receipt.get('target_path_sha256') != _sha(str(target).encode())
                or receipt.get('content_sha256') != _sha(raw) or receipt.get('bytes_written') != len(raw)):
            raise ValueError('native_draft_final_bytes_changed')
        json.loads(raw)
        verdict.update(verified=True, status='verified_native_draft', content_sha256=_sha(raw),
                       bytes_written=len(raw), arguments_sha256=receipt['arguments_sha256'])
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        verdict['errors'].append(str(error) if isinstance(error, ValueError) else type(error).__name__)
    return verdict


def native_cold_proof(result, plan, trial):
    """Prove a real exact Hermes CLI restore transition followed by native skill_view."""
    verdict = {'verified': False, 'restore_calls': 0, 'native_view_calls': 0, 'errors': []}
    try:
        posts, streams = _native_calls(result, trial['db'])
        receipts = [event for event in result['events'] if event.get('kind') == 'native_cold_observation']
        host_python_sha = plan['environment']['host_runtime']['selected_python']['executable_path_sha256']
        restored = []
        from ..integrations.hermes_gate import digest_json
        for stream in streams:
            if stream.get('name') != 'terminal' or type(stream.get('input')) is not dict:
                continue
            command = stream['input'].get('command')
            if type(command) is not str:
                continue
            tokens = shlex.split(command)
            if tokens[:1] == ['cd'] and len(tokens) >= 4 and tokens[2] == '&&':
                if Path(tokens[1]).resolve() != Path(trial['workspace']).resolve():
                    continue
                tokens = tokens[3:]
            if tokens[:2] == ['env', 'PYTHONPATH='+str(Path(plan['hermes_root']))]:
                tokens = tokens[2:]
            if not tokens or _sha(tokens[0].encode()) != host_python_sha:
                continue
            rest = tokens[1:]
            if rest[:1] == ['-B']:
                rest = rest[1:]
            if rest != ['-m', 'hermes_cli.main', 'curator', 'restore', 'xlsx']:
                continue
            matching = [post for post in posts if post.get('tool_name') == 'terminal'
                and post.get('arguments_sha256') == digest_json(stream['input'])
                and post.get('status') == 'succeeded' and post.get('tool_exit_code') == 0]
            if len(matching) == 1 and any(_same_call(row, matching[0]) and row.get('verified') is True
                and row.get('status') == 'restored' and row.get('skill_id') == XLSX_SKILL_ID
                and row.get('snapshot_id') == plan['skill_snapshot_id']
                and row.get('skill_sha256') == plan['skill_sha256']
                and row.get('script_sha256') == plan['script_sha256'] for row in receipts):
                restored.append(matching[0])
        views = [post for post in posts if post.get('tool_name') == 'skill_view' and post.get('status') == 'loaded'
            and post.get('skill_id') == XLSX_SKILL_ID and post.get('snapshot_id') == plan['skill_snapshot_id']
            and post.get('content_sha256') == plan['skill_sha256'] and post.get('skill_sha256') == plan['skill_sha256']]
        verdict.update(restore_calls=len(restored), native_view_calls=len(views))
        if len(receipts) != 1 or len(restored) != 1 or len(views) != 1:
            raise ValueError('native_unique_cold_restore_and_view_missing')
        events = result['events']
        if events.index(restored[0]) >= events.index(views[0]):
            raise ValueError('native_cold_view_precedes_restore')
        verdict['verified'] = True
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        verdict['errors'].append(str(error) if isinstance(error, ValueError) else type(error).__name__)
    return verdict
