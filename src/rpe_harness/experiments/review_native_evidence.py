"""Strict 1.12 native evidence helpers; legacy experiment scoring is unchanged."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
from collections import Counter

from ..integrations.hermes_native import evaluate_artifact_receipt
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


def evaluate_native_review_execution(result, plan, trial):
    """1.12-only original helper grammar with one exact, read-only diagnostic suffix.

    Parsing never rewrites model arguments or their hashes. The full original
    command stays bound to the durable native call and its actual exit status.
    """
    from .address_comparison import evaluate_native_address_session, _python_metadata
    legacy = evaluate_native_address_session(result, plan, trial)
    if legacy.get('verified') or legacy.get('errors') != ['native_original_helper_execution_missing']:
        return legacy
    verdict = {**legacy, 'verified': False, 'errors': ['native_exact_helper_diagnostic_invocation_missing']}
    try:
        from ..integrations.hermes_gate import digest_json
        posts, streams = _native_calls(result, trial['db'])
        case = next(case for case in plan['cases'] if case['case_id'] == trial['case_id'])
        if case['capability'] != 'xlsx.inspect' or case['expected_status'] != 'ready':
            return legacy
        workspace = Path(trial['workspace']).resolve()
        helper = Path(result['session_root'])/'home/skills/productivity/xlsx/scripts/xlsx_read.py'
        source = helper.parent.parent/'SKILL.md'
        if (_sha(read_regular_file(source, max_bytes=65536)) != plan['skill_sha256']
                or _sha(read_regular_file(helper, max_bytes=65536)) != plan['script_sha256']
                or _sha(read_regular_file(workspace/case['input_name'], max_bytes=32*1024*1024)) != trial['input_sha256']):
            raise ValueError('native_frozen_helper_or_input_changed')
        if _python_metadata(plan['helper_python']) != plan['environment']['helper_python_identity']:
            raise ValueError('native_frozen_interpreter_changed')
        loaded = [post for post in posts if post.get('tool_name') == 'skill_view' and post.get('status') == 'loaded'
            and post.get('skill_id') == XLSX_SKILL_ID and post.get('snapshot_id') == plan['skill_snapshot_id']
            and post.get('content_sha256') == plan['skill_sha256'] and post.get('skill_source_path') == str(source)]
        mode = {'rows': '--json', 'sheets': '--sheets', 'formulas': '--formulas'}[case['parameters']['mode']]
        mode_args = [mode]+(['--sheet', case['parameters']['sheet']] if case['parameters'].get('sheet') is not None else [])
        outputs = {case['output_name'], str(workspace/case['output_name'])}
        inputs = {case['input_name'], str(workspace/case['input_name'])}
        events = result['events']
        for stream in streams:
            args = stream.get('input')
            if stream.get('name') != 'terminal' or type(args) is not dict:
                continue
            command = args.get('command')
            if type(command) is not str or any(char in command for char in ('`', '\n', '\r')):
                continue
            lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|><')
            lexer.whitespace_split, lexer.commenters = True, ''
            tokens = list(lexer)
            if (len(tokens) < 7 or tokens[-7:-1] != ['&&', 'echo', 'EXIT=$?', '&&', 'wc', '-c']
                    or tokens[-1] not in outputs):
                continue
            core = tokens[:-7]
            if any('$' in token for token in core):
                continue
            if core[:1] == ['cd']:
                if len(core) < 4 or Path(core[1]).resolve() != workspace or core[2] != '&&':
                    continue
                core = core[3:]
            if core[:1] != [plan['helper_python']]:
                continue
            core = core[1:]
            flags = []
            while core and core[0] in {'-B', '-u', '-Bu', '-uB'} and len(flags) < 4:
                flags.append(core.pop(0))
            if (len(core) != len(mode_args)+4 or core[0] != str(helper) or core[1] not in inputs
                    or core[2:-2] != mode_args or core[-2] != '>' or core[-1] not in outputs):
                continue
            matching = [post for post in posts if post.get('tool_name') == 'terminal'
                and post.get('arguments_sha256') == digest_json(args)
                and ('tool_call_id' not in stream or post.get('tool_call_id') == stream['tool_call_id'])
                and post.get('status') == 'succeeded' and post.get('tool_exit_code') == 0]
            if len(matching) != 1:
                continue
            pre = [event for event in events if event.get('kind') == 'pre_tool_call' and _same_call(event, matching[0])]
            if len(pre) != 1 or not any(events.index(load) < events.index(pre[0]) for load in loaded):
                continue
            return {'verified': True, 'status': 'accepted', 'errors': [], 'proof_kind': 'native_exact_helper_readonly_diagnostic',
                'skill_id': XLSX_SKILL_ID, 'snapshot_id': plan['skill_snapshot_id'], 'script_sha256': plan['script_sha256'],
                'helper_call_id': matching[0]['tool_call_id'], 'arguments_sha256': digest_json(args),
                'python_startup_flags': flags, 'diagnostic_suffix': 'echo_exit_and_count_output'}
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        verdict['errors'] = [str(error) if isinstance(error, ValueError) else type(error).__name__]
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


def evaluate_native_review_execution_v113(result, plan, trial):
    """Require an authenticated actual helper exit, plus independent business truth.

    Only 1.13 XLSX execution uses this gate. A final artifact is bound to the
    observed helper's stdout inode and current bytes; this is not an exclusive
    writer claim. Historical 1.11/1.12 command scoring is unchanged.
    """
    from .address_comparison import (_python_metadata, evaluate_native_address_session,
                                     score_address_artifact)
    case = next(case for case in plan['cases'] if case['case_id'] == trial['case_id'])
    if case['capability'] != 'xlsx.inspect' or case['expected_status'] != 'ready':
        return evaluate_native_address_session(result, plan, trial)
    verdict = {'verified': False, 'status': 'unverified', 'errors': [],
               'proof_kind': 'authenticated_darwin_kernel_process_exit', 'receipt_authenticated': False,
               'artifact_binding': 'final_bytes_on_observed_stdout_inode_not_exclusive_writer'}
    try:
        import hmac
        from ..integrations.hermes_native_process import (_digest, _validate_config, file_snapshot,
                                                         receipt_signature)
        posts, _streams = _native_calls(result, trial['db'])
        session = Path(result['session_root'])
        if (not session.is_absolute() or session != session.resolve()
                or not session.is_relative_to(Path(plan['root'])/'sessions')):
            raise ValueError('native_process_session_not_owned')
        config = json.loads(read_regular_file(session/'native-execution-config.json', max_bytes=65536))
        _validate_config(config)
        expected = {'workspace': str(Path(trial['workspace']).resolve()), 'skill_id': XLSX_SKILL_ID,
            'snapshot_id': plan['skill_snapshot_id'], 'helper_python': plan['helper_python'],
            'helper_python_sha256': plan['environment']['helper_python_identity']['executable_sha256'],
            'skill_sha256': plan['skill_sha256'], 'script_sha256': plan['script_sha256'],
            'input_name': case['input_name'], 'input_sha256': trial['input_sha256'],
            'output_name': case['output_name'], 'parameters': case['parameters']}
        if any(not exact_json(config.get(key), value) for key, value in expected.items()):
            raise ValueError('native_process_configuration_changed')
        allowed_sources = [session/'home/skills/xlsx/SKILL.md',
                           session/'home/skills/productivity/xlsx/SKILL.md']
        source, helper = Path(config['skill_path']), Path(config['helper_path'])
        if source not in allowed_sources or helper != source.parent/'scripts/xlsx_read.py':
            raise ValueError('native_process_original_helper_path_changed')
        for path, key in [(source, 'skill_sha256'), (helper, 'script_sha256'),
                          (Path(config['workspace'])/case['input_name'], 'input_sha256')]:
            if file_snapshot(path).get('sha256') != config[key]:
                raise ValueError('native_process_frozen_helper_or_input_changed')
        if _python_metadata(plan['helper_python']) != plan['environment']['helper_python_identity']:
            raise ValueError('native_process_frozen_interpreter_changed')
        rows = [event for event in result['events'] if event.get('kind') == 'native_helper_process_observation'
                and event.get('kernel_exit_observed') is True]
        if len(rows) != 1 or rows[0].get('verified') is not True:
            raise ValueError('native_unique_successful_helper_process_exit_missing')
        receipt = rows[0]
        key = read_regular_file(session/'native-execution-auth.key', max_bytes=32)
        signed = {key: value for key, value in receipt.items() if key not in {
            'kind', 'received_at', 'source_event_id', 'ingest_event_id', 'process_receipt_hmac_sha256'}}
        signature = receipt.get('process_receipt_hmac_sha256')
        if (len(key) != 32 or not isinstance(signature, str)
                or not hmac.compare_digest(signature, receipt_signature(signed, key))):
            raise ValueError('native_process_receipt_authentication_failed')
        verdict['receipt_authenticated'] = True
        if (receipt.get('process_config_sha256') != _digest(config)
                or receipt.get('process_returncode') != 0 or receipt.get('observer_joined') is not True
                or receipt.get('overlap') is not False or receipt.get('process_observation_error') != ''):
            raise ValueError('native_process_receipt_configuration_or_exit_mismatch')
        matching = [post for post in posts if _same_call(post, receipt)]
        if len(matching) != 1 or matching[0].get('status') != 'succeeded' or matching[0].get('tool_exit_code') != 0:
            raise ValueError('native_process_receipt_call_mismatch')
        pre = [event for event in result['events'] if event.get('kind') == 'pre_tool_call'
               and _same_call(event, matching[0])]
        loaded = [post for post in posts if post.get('tool_name') == 'skill_view' and post.get('status') == 'loaded'
            and post.get('skill_id') == XLSX_SKILL_ID and post.get('snapshot_id') == plan['skill_snapshot_id']
            and post.get('content_sha256') == plan['skill_sha256'] and post.get('skill_source_path') == str(source)]
        if len(pre) != 1 or not any(result['events'].index(load) < result['events'].index(pre[0]) for load in loaded):
            raise ValueError('native_process_original_skill_not_loaded_before_helper')
        output_path = Path(config['workspace'])/case['output_name']
        output = file_snapshot(output_path)
        if (output.get('sha256') != receipt.get('output_sha256') or output.get('size') != receipt.get('output_size')
                or _digest(output.get('identity')) != receipt.get('output_identity_sha256')
                or _sha(str(output_path).encode()) != receipt.get('output_path_sha256')):
            raise ValueError('native_process_final_output_changed')
        business = score_address_artifact(plan, trial)
        if business.get('business_correct') is not True or business.get('product_oracle_passed') is not True:
            raise ValueError('native_process_final_output_business_incorrect')
        verdict.update(verified=True, status='accepted', process_returncode=0, kernel_exit_observed=True,
            helper_call_id=receipt['tool_call_id'], arguments_sha256=receipt['arguments_sha256'],
            output_sha256=receipt['output_sha256'], process_argv_sha256=receipt['process_argv_sha256'])
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        verdict['errors'].append(str(error) if isinstance(error, ValueError) else type(error).__name__)
    return verdict


def evaluate_native_review_execution_v114(result, plan, trial):
    """Keep the XLSX process gate; authenticate actual IO for other native outputs."""
    from .address_comparison import score_address_artifact
    from ..integrations.hermes_native import native_evidence_config
    case = next(case for case in plan['cases'] if case['case_id'] == trial['case_id'])
    if case['capability'] == 'xlsx.inspect':
        return evaluate_native_review_execution_v113(result, plan, trial)
    verdict = {'verified': False, 'status': 'unverified', 'errors': [],
        'proof_kind': 'authenticated_native_tool_io', 'receipt_authenticated': False,
        'artifact_binding': 'new_file_during_single_native_tool_call'}
    try:
        posts, _streams = _native_calls(result, trial['db'])
        if case['expected_status'] != 'ready':
            raise ValueError('native_business_artifact_contract_missing')
        session = Path(result['session_root'])
        if (not session.is_absolute() or session != session.resolve()
                or not session.is_relative_to(Path(plan['root'])/'sessions')):
            raise ValueError('native_artifact_session_not_owned')
        config = json.loads(read_regular_file(session/'native-evidence-config.json', max_bytes=65536))
        expected = native_evidence_config(trial['workspace'], {'artifact_name': case['output_name']})
        if not exact_json(config, expected):
            raise ValueError('native_artifact_configuration_changed')
        rows = [event for event in result['events'] if event.get('kind') == 'native_artifact_observation']
        if len(rows) != 1:
            raise ValueError('native_unique_new_artifact_receipt_missing')
        receipt = rows[0]
        key = read_regular_file(session/'native-execution-auth.key', max_bytes=32)
        checked = evaluate_artifact_receipt(receipt, config, key)
        verdict['receipt_authenticated'] = checked['receipt_authenticated']
        if not checked['verified']:
            raise ValueError(checked['errors'][0])
        matching = [post for post in posts if _same_call(post, receipt)]
        if len(matching) != 1 or matching[0].get('status') not in {'ok', 'succeeded'}:
            raise ValueError('native_artifact_receipt_call_mismatch')
        if receipt['tool_name'] == 'terminal' and (type(matching[0].get('tool_exit_code')) is not int
                or matching[0]['tool_exit_code'] != 0):
            raise ValueError('native_artifact_terminal_not_completed')
        business = score_address_artifact(plan, trial)
        if business.get('business_correct') is not True or business.get('product_oracle_passed') is not True:
            raise ValueError('native_artifact_final_output_business_incorrect')
        verdict.update(verified=True, status='accepted', output_sha256=checked['output_sha256'],
            artifact_call_id=receipt['tool_call_id'], arguments_sha256=receipt['arguments_sha256'])
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        verdict['errors'] = [str(error) if isinstance(error, ValueError) else type(error).__name__]
    return verdict


def native_chain_diagnostics(result, db_path):
    """Keep counts explaining a failed chain, never IDs, arguments or response text."""
    from ..integrations.hermes_gate import _persisted_events, digest_json
    events = result.get('events') or []
    rows = [event for event in events if type(event) is dict]
    fields = ('session_id', 'task_id', 'turn_id', 'api_request_id')
    def identity(event, tool=False):
        names = (*fields, 'tool_call_id', 'tool_name') if tool else fields
        return tuple(event.get(name) if isinstance(event.get(name), str) else None for name in names)
    requests = [event for event in rows if event.get('kind') == 'pre_api_request']
    outcomes = [event for event in rows if event.get('kind') in {'post_api_request', 'api_request_error'}]
    pres = [event for event in rows if event.get('kind') == 'pre_tool_call']
    posts = [event for event in rows if event.get('kind') == 'post_tool_call']
    request_ids, outcome_ids = Counter(map(identity, requests)), Counter(map(identity, outcomes))
    pre_ids, post_ids = (Counter(identity(event, True) for event in group) for group in (pres, posts))
    emitted = Counter((*identity(event), call.get('tool_call_id'), call.get('tool_name'))
        for event in outcomes for call in event.get('model_tool_calls', []) if type(call) is dict
        and all(isinstance(call.get(key), str) for key in ('tool_call_id', 'tool_name')))
    streams = result.get('stream_tool_calls') or []
    observed_signatures = Counter((event.get('tool_name'), event.get('arguments_sha256')) for event in pres
        if all(isinstance(event.get(key), str) for key in ('tool_name', 'arguments_sha256')))
    stream_signatures = Counter()
    for stream in streams:
        try:
            if type(stream) is dict and isinstance(stream.get('name'), str):
                stream_signatures[(stream['name'], digest_json(stream.get('input')))] += 1
        except (TypeError, ValueError):
            pass
    diagnostics = {'verified': False, 'error_code': None, 'durable_observations_match': False,
        'api_requests': len(requests), 'api_outcomes': len(outcomes),
        'api_errors': sum(event.get('kind') == 'api_request_error' for event in outcomes),
        'api_unmatched_requests': sum((request_ids-outcome_ids).values()),
        'api_unmatched_outcomes': sum((outcome_ids-request_ids).values()),
        'api_duplicate_requests': sum(count-1 for count in request_ids.values()),
        'api_duplicate_outcomes': sum(count-1 for count in outcome_ids.values()),
        'tool_pre_calls': len(pres), 'tool_post_calls': len(posts), 'model_tool_calls': sum(emitted.values()),
        'tool_missing_posts': sum((pre_ids-post_ids).values()),
        'tool_unmatched_posts': sum((post_ids-pre_ids).values()),
        'tool_unemitted_calls': sum((pre_ids-emitted).values()),
        'tool_unobserved_calls': sum((emitted-pre_ids).values()), 'stream_calls': len(streams),
        'stream_unmatched_calls': sum((observed_signatures-stream_signatures).values())
            + sum((stream_signatures-observed_signatures).values())}
    try:
        diagnostics['durable_observations_match'] = bool(events) and events == _persisted_events(db_path, result.get('session_id'))
        _native_calls(result, db_path)
        diagnostics['verified'] = True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as error:
        diagnostics['error_code'] = str(error) if isinstance(error, ValueError) and str(error).startswith('native_') else type(error).__name__
    return diagnostics
