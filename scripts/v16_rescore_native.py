"""Read-only supplemental evidence: Hermes stream-v1 lacks tool-call IDs.

Original formal gates remain immutable. Artifact truth, receipt linkage, and
strict helper success are separate claims. Provider tokens are not billed cost.
"""
import argparse
from collections import Counter
import difflib
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3

IDS = ('session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def receipt_links(events, streams):
    """Match real API/model/pre/post IDs; ID-less streams match counted classes."""
    proof = {'passed': False, 'stream_has_call_ids': all(s.get('tool_call_id') for s in streams),
        'stream_identity_policy': 'ID-less stream entries match (name,input SHA) equivalence classes; '
                                  'no stream call ID is inferred or invented',
        'equivalence_classes': [], 'errors': []}
    try:
        pres = [e for e in events if e['kind'] == 'pre_tool_call']
        posts = [e for e in events if e['kind'] == 'post_tool_call']
        def identity(e):
            if any(not isinstance(e.get(key), str) or not e[key] for key in IDS):
                raise ValueError('real_call_identity_missing')
            return tuple(e[key] for key in IDS) + (e['tool_name'], e['arguments_sha256'])
        pre_ids, post_ids = Counter(map(identity, pres)), Counter(map(identity, posts))
        if not pres or pre_ids != post_ids or any(count != 1 for count in pre_ids.values()):
            raise ValueError('real_pre_post_identity_mismatch')
        api_pres = Counter(tuple(e.get(key) for key in IDS[:-1]) for e in events if e['kind'] == 'pre_api_request')
        api_outcomes = Counter(tuple(e.get(key) for key in IDS[:-1]) for e in events
                               if e['kind'] in {'post_api_request', 'api_request_error'})
        if (not api_pres or api_pres != api_outcomes or
                any(count != 1 or any(not key for key in identity) for identity, count in api_pres.items())):
            raise ValueError('actual_api_observations_incomplete')
        emitted = []
        for outcome in (e for e in events if e['kind'] == 'post_api_request'):
            api_key = tuple(outcome.get(key) for key in IDS[:-1])
            requests = [e for e in events if e['kind'] == 'pre_api_request' and
                        tuple(e.get(key) for key in IDS[:-1]) == api_key]
            if len(requests) != 1 or any(not value for value in api_key):
                raise ValueError('actual_api_request_link_missing')
            for call in outcome.get('model_tool_calls', []):
                emitted.append(api_key + (call.get('tool_call_id'), call.get('tool_name')))
        real = Counter(tuple(e[key] for key in IDS) + (e['tool_name'],) for e in pres)
        if real != Counter(emitted):
            raise ValueError('actual_model_call_identity_mismatch')
        classes = Counter((e['tool_name'], e['arguments_sha256']) for e in pres)
        streamed = Counter((s['name'], digest(s['input'])) for s in streams)
        if classes != streamed:
            raise ValueError('stream_arguments_multiset_mismatch')
        id_fields = ['tool_call_id' in stream for stream in streams]
        if any(id_fields):
            if not all(id_fields) or any(not isinstance(s['tool_call_id'], str) or not s['tool_call_id'] for s in streams):
                raise ValueError('stream_call_ids_partial_or_invalid')
            expected_ids = Counter((e['tool_name'], e['tool_call_id'], e['arguments_sha256']) for e in pres)
            actual_ids = Counter((s['name'], s['tool_call_id'], digest(s['input'])) for s in streams)
            if expected_ids != actual_ids or any(count != 1 for count in expected_ids.values()):
                raise ValueError('stream_actual_call_id_mismatch')
        proof.update(passed=True, equivalence_classes=[{'tool_name': name,
            'arguments_sha256': value, 'count': count} for (name, value), count in sorted(classes.items())])
    except (ValueError, KeyError, TypeError) as error:
        proof['errors'].append(str(error))
    return proof


def helper_command(command, *, helper_python, helper_path, workspace, mode, sheet):
    """Only direct helper execution; diagnostics suffix never proves helper success."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|><')
        lexer.whitespace_split, lexer.commenters = True, ''
        tokens = list(lexer)
        if tokens[:1] == ['cd']:
            if len(tokens) < 4 or Path(tokens[1]).resolve() != Path(workspace).resolve() or tokens[2] != '&&':
                return 'unproven'
            tokens = tokens[3:]
        suffix = []
        if ';' in tokens:
            boundary = tokens.index(';')
            tokens, suffix = tokens[:boundary], tokens[boundary + 1:]
        if any('$' in token or '`' in token or '\n' in token for token in tokens):
            return 'unproven'
        if tokens[:2] == [helper_python, '-B']:
            tokens.pop(1)
        flags = [{'rows': '--json', 'sheets': '--sheets', 'formulas': '--formulas'}[mode]]
        if sheet is not None:
            flags += ['--sheet', sheet]
        if (tokens[:2] != [helper_python, helper_path] or len(tokens) < 6 or
                tokens[2] not in {'input.xlsx', str(Path(workspace) / 'input.xlsx')} or
                tokens[3:-2] != flags or tokens[-2] != '>' or
                tokens[-1] not in {'output.json', str(Path(workspace) / 'output.json')}):
            return 'unproven'
        if not suffix:
            return 'strict'
        pieces, current = [], []
        for token in suffix + [';']:
            if token == ';':
                pieces.append(current)
                current = []
            else:
                current.append(token)
        for piece in pieces:
            if not piece or piece[0] not in {'echo', 'ls'}:
                return 'unproven'
            for token in piece[1:]:
                if token in {'exit=$?', 'input.xlsx', 'output.json'} or token.startswith('-'):
                    continue
                if any(value in token for value in ('$', '`', '>', '<', '&', '|', '\n')):
                    return 'unproven'
                if piece[0] == 'ls' and token not in {str(Path(workspace) / 'input.xlsx'), str(Path(workspace) / 'output.json')}:
                    return 'unproven'
        return 'invocation_seen_success_unknown'
    except (ValueError, KeyError, TypeError):
        return 'unproven'


def exact(actual, expected):
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return set(actual) == set(expected) and all(exact(actual[k], v) for k, v in expected.items())
    if type(expected) is list:
        return len(actual) == len(expected) and all(exact(a, b) for a, b in zip(actual, expected))
    return actual == expected


def durable_events(db, session):
    with sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True) as connection:
        rows = connection.execute('SELECT ingest_event_id,source_event_id,kind,received_at,metadata_json '
            'FROM hermes_host_events WHERE session_id=? ORDER BY id', (session,)).fetchall()
    return [{'ingest_event_id': row[0], 'source_event_id': row[1], 'kind': row[2],
             'received_at': row[3], **json.loads(row[4])} for row in rows]


def rescore(preregistration):
    plan = json.loads(Path(preregistration).read_text())
    records = Path(plan['owned_root']) / 'records'
    cases = {case['case_id']: case for case in plan['cases']}
    rows = []
    for trial in plan['sessions']:
        formal_path = records / (trial['trial_id'] + '.json')
        if not formal_path.exists():
            continue
        formal = json.loads(formal_path.read_text())
        raw_path = records / (trial['trial_id'] + '.raw.json')
        raw = json.loads(raw_path.read_text()) if raw_path.exists() else {}
        case = cases[trial['case_id']]
        output = Path(trial['workspace']) / 'output.json'
        try:
            artifact = output.is_file() and not output.is_symlink() and exact(json.loads(output.read_text()), case['expected'])
        except (OSError, ValueError):
            artifact = False
        row = {'trial_id': trial['trial_id'], 'arm': trial['arm'],
            'formal_status': formal.get('status'), 'formal_accepted': formal.get('accepted'),
            'formal_gate': formal.get('gate'), 'raw_completed': raw.get('completed'),
            'artifact_exact': artifact, 'usage': raw.get('usage') or formal.get('usage'),
            'elapsed_seconds': raw.get('elapsed_seconds', formal.get('elapsed_seconds')),
            'billed_cost': formal.get('billed_cost'), 'raw_sha256': sha(raw_path) if raw_path.exists() else None,
            'formal_record_sha256': sha(formal_path), 'supplemental_receipt_link': None,
            'original_helper_proof': 'not_applicable' if trial['arm'] == 'rpe' else 'unproven'}
        row['source_unchanged'] = sha(Path(trial['workspace']) / 'input.xlsx') == trial['input_sha256']
        if trial['arm'] == 'native' and raw:
            events = durable_events(plan['db'], raw.get('session_id'))
            link = receipt_links(events, raw.get('stream_tool_calls', []))
            if events != raw.get('events'):
                link.update(passed=False)
                link['errors'].append('raw_durable_events_differ')
            row['supplemental_receipt_link'] = link
            source = Path(raw['session_root']) / 'home/skills/productivity/xlsx/SKILL.md'
            helper = source.parent / 'scripts/xlsx_read.py'
            try:
                sealed = sha(source) == plan['skill_sha256'] and sha(helper) == plan['script_sha256']
            except OSError:
                sealed = False
            loads = [e for e in events if e['kind'] == 'post_tool_call' and e.get('tool_name') == 'skill_view' and
                e.get('status') == 'loaded' and e.get('skill_id') == 'productivity/xlsx' and
                e.get('snapshot_id') == plan['snapshot_id'] and e.get('content_sha256') == plan['skill_sha256'] and
                e.get('skill_source_path') == str(source)]
            if link['passed'] and sealed and loads:
                proofs = []
                for stream in raw.get('stream_tool_calls', []):
                    if stream.get('name') != 'terminal':
                        continue
                    posts = [e for e in events if e['kind'] == 'post_tool_call' and e.get('tool_name') == 'terminal' and
                        e.get('arguments_sha256') == digest(stream['input'])]
                    if not posts or any(e.get('status') != 'succeeded' for e in posts):
                        continue
                    if not all(any(events.index(load) < events.index(post) for load in loads) for post in posts):
                        continue
                    proofs.append(helper_command(stream['input'].get('command'),
                        helper_python=plan['helper_python'], helper_path=str(helper),
                        workspace=trial['workspace'], mode=case['mode'], sheet=case['sheet']))
                row['original_helper_proof'] = ('strict_success' if 'strict' in proofs else
                    'invocation_seen_success_unknown' if 'invocation_seen_success_unknown' in proofs else 'unproven')
        rows.append(row)
    arms = {}
    for arm in ('native', 'rpe'):
        subset = [row for row in rows if row['arm'] == arm]
        finalized = [row for row in subset if row['formal_status'] in {'completed', 'failed'}]
        usages = [row.get('usage') or {} for row in subset]
        known = [u.get('total_tokens') for u in usages if type(u.get('total_tokens')) is int]
        billed = [row['billed_cost'] for row in subset if type(row['billed_cost']) in (int, float)]
        arms[arm] = {'attempts_recorded': len(subset), 'finalized_sessions': len(finalized),
            'artifact_exact': sum(row['artifact_exact'] for row in finalized),
            'business_artifact_accuracy': sum(row['artifact_exact'] for row in finalized) / len(finalized) if finalized else None,
            'receipt_link_passed': sum((row['supplemental_receipt_link'] or {}).get('passed') is True for row in finalized),
            'helper_proof_counts': dict(Counter(row['original_helper_proof'] for row in finalized)),
            'observed_total_tokens': sum(known) if len(known) == len(subset) and subset else None,
            'known_observed_tokens': sum(u.get('known_total_tokens', u.get('total_tokens') or 0) for u in usages),
            'usage_coverage_complete': bool(subset) and all(u.get('coverage_complete') is True for u in usages),
            'billed_cost': sum(billed) if subset and len(billed) == len(subset) else None,
            'known_billed_cost': sum(billed) if billed else None,
            'api_requests': sum(u['api_requests'] for u in usages)
                if subset and all(type(u.get('api_requests')) is int for u in usages) else None,
            'elapsed_seconds': sum(row['elapsed_seconds'] for row in subset)
                if subset and all(type(row['elapsed_seconds']) in (int, float) for row in subset) else None}
    return {'fingerprint': plan['fingerprint'], 'supplemental_only': True,
        'supplemental_script_sha256': sha(__file__),
        'finding': 'Native formal stream-ID gate is incompatible with this Hermes SDK stream; '
                   'this adapter defect is not native artifact failure.',
        'scope': 'All recorded formal attempts including failures; pilots remain separate; running snapshot may be incomplete',
        'planned_sessions': len(plan['sessions']), 'recorded_sessions': len(rows),
        'cost_policy': 'Full observed API usage is retained. Tokens are not billed cost; unknown billing is null.',
        'arms': arms, 'records': rows}


def proposed_patch():
    path = Path(__file__).resolve().parents[1] / 'src/rpe_harness/experiments/skill_comparison.py'
    before = path.read_text()
    old = """            calls = [call for call in streams if call.get('name') == pre['tool_name'] and
                     call.get('tool_call_id') == pre['tool_call_id']]
            if (len(calls) != 1 or type(calls[0].get('input')) is not dict or
                    digest_json(calls[0]['input']) != pre.get('arguments_sha256')):
                raise ValueError('native_stream_arguments_link_missing')
"""
    new = """            # SDK stream-v1 has no call IDs: match counted equivalent argument classes.
            # API/model/pre/post IDs still come from the authoritative persisted observer.
            classes = Counter((e.get('tool_name'), e.get('arguments_sha256')) for e in events
                              if e['kind'] == 'pre_tool_call')
            streamed = Counter((call.get('name'), digest_json(call.get('input'))) for call in streams)
            if classes != streamed:
                raise ValueError('native_stream_arguments_multiset_mismatch')
            calls = [call for call in streams if call.get('name') == pre['tool_name'] and
                     digest_json(call.get('input')) == pre.get('arguments_sha256')]
            if not calls:
                raise ValueError('native_stream_arguments_link_missing')
"""
    if old not in before:
        return '# SDK stream matching fix is already applied in this source. Preserve the archived Candidate A patch.\n'
    identity_check = """        identity_fields = ('session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id')
        pres = [event for event in events if event['kind'] == 'pre_tool_call']
        posts = [event for event in events if event['kind'] == 'post_tool_call']
        def call_identity(event):
            if any(not isinstance(event.get(key), str) or not event[key] for key in identity_fields):
                raise ValueError('native_real_call_identity_missing')
            return tuple(event[key] for key in identity_fields) + (event['tool_name'],)
        expected_calls = Counter(call_identity(event) for event in pres)
        if (expected_calls != Counter(call_identity(event) for event in posts) or
                any(count != 1 for count in expected_calls.values())):
            raise ValueError('native_real_pre_post_identity_mismatch')
        emitted = []
        for outcome in (event for event in events if event['kind'] == 'post_api_request'):
            api_key = tuple(outcome.get(key) for key in identity_fields[:-1])
            requests = [event for event in events if event['kind'] == 'pre_api_request' and
                        tuple(event.get(key) for key in identity_fields[:-1]) == api_key]
            if len(requests) != 1 or any(not key for key in api_key):
                raise ValueError('native_actual_api_request_link_missing')
            emitted.extend(api_key + (call.get('tool_call_id'), call.get('tool_name'))
                           for call in outcome.get('model_tool_calls', []))
        if expected_calls != Counter(emitted):
            raise ValueError('native_actual_model_call_identity_mismatch')
"""
    after = before.replace(old, new).replace('import argparse\n', 'import argparse\nfrom collections import Counter\n', 1)
    after = after.replace("        streams = result.get('stream_tool_calls') or []\n",
        identity_check + "        streams = result.get('stream_tool_calls') or []\n", 1)
    return '# Proposed after formal run only; original strict helper shell rule remains unchanged.\n' + ''.join(
        difflib.unified_diff(before.splitlines(True), after.splitlines(True),
        fromfile='a/src/rpe_harness/experiments/skill_comparison.py',
        tofile='b/src/rpe_harness/experiments/skill_comparison.py'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preregistration', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--patch-output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = rescore(args.preregistration)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.patch_output.exists():
        args.patch_output.write_text(proposed_patch())
    print(json.dumps({'recorded_sessions': report['recorded_sessions'], 'arms': report['arms']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
