#!/usr/bin/env python3
"""Run exactly one blind audit after all eight formal trials are durable.

Prepared offline. Running this file invokes the configured model once; it has
no repair, retry, pilot, or provider-probing path.
"""
import csv
import copy
import hashlib
import io
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True
OWNED = Path('/Users/audrey/项目/缓存/test/rpe-v113-dccde01f9c6a405298e2b9d06f306f96')
COMPARISON = OWNED/'comparison-final'
WORKTREE = Path('/Users/audrey/.codex/worktrees/rpe-v113/rpe-harness')
sys.path.insert(0, str(WORKTREE/'src'))

from rpe_harness.experiments.agent_audit import run_agent_audit
from rpe_harness.experiments.cost_ledger import PhaseLedger
from rpe_harness.path_binding import read_regular_file
from rpe_harness.proposal import ProposalStore
from rpe_harness.review_quotes import request_quote_anchors


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def save_once(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


def raw_owned(path):
    path = Path(path)
    if not path.is_absolute() or not path.resolve().is_relative_to(COMPARISON.resolve()):
        raise ValueError('audit evidence path escapes comparison ownership')
    return read_regular_file(path, max_bytes=1024*1024)


def read_json(path):
    return json.loads(raw_owned(path))


def strip_hash_auxiliary(value):
    value = copy.deepcopy(value)
    if isinstance(value, dict):
        value.pop('unresolved_sha256', None)
        for row in value.get('requirements', []):
            if isinstance(row, dict):
                row.pop('text_sha256', None)
    return value


def decoded_artifact(path):
    """Decode actual files, using no expected rows or registered parameters."""
    path = Path(path)
    if not path.exists():
        return None
    raw = raw_owned(path)
    if path.suffix == '.xlsx':
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
        try:
            return {'format': 'xlsx', 'sheets': {
                sheet.title: [list(row) for row in sheet.iter_rows(values_only=True)]
                for sheet in book.worksheets}}
        finally:
            book.close()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        return {'format': 'undecodable', 'text': None,
                'byte_count': len(raw), 'decode_error': 'UnicodeError'}
    if path.suffix == '.json':
        try:
            value = json.loads(text)
        except ValueError:
            return {'format': 'json', 'text': text, 'json': None,
                    'decode_error': 'invalid_json'}
        return {'format': 'json', 'text': text, 'json': value}
    delimiter = '\t' if '\t' in text.partition('\n')[0] else ','
    return {'format': 'csv', 'text': text, 'delimiter_observed': delimiter,
            'rows': list(csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True))}


def ensure_no_oracle_keys(value):
    if isinstance(value, dict):
        if {'expected_rows', 'oracle_plan', 'gold'} & {key.casefold() for key in value}:
            raise ValueError('forbidden oracle key in independent evidence')
        for item in value.values():
            ensure_no_oracle_keys(item)
    elif isinstance(value, list):
        for item in value:
            ensure_no_oracle_keys(item)


def prepare_bundle():
    plan = read_json(COMPARISON/'preregistration.json')
    if plan.get('profile') != 'review-v113' or plan.get('root') != str(COMPARISON):
        raise ValueError('not the owned 1.12 comparison')
    trials = plan['sessions']
    if (len(trials) != 8 or any(t.get('phase') != 'formal' for t in trials)
            or Counter(t['arm'] for t in trials) != {'native': 4, 'review': 4}
            or len({t['trial_id'] for t in trials}) != 8):
        raise ValueError('exactly eight registered formal trials required')
    phases = PhaseLedger(COMPARISON/'ledger').records()
    completed = {'completed', 'failed', 'timed_out', 'refused'}
    records = {}
    for trial in trials:
        identity = trial['trial_id']
        record = read_json(COMPARISON/'records'/(identity+'.json'))
        if (record.get('trial_id') != identity or record.get('phase') != 'formal'
                or record.get('arm') != trial['arm']
                or record.get('status') not in {'completed', 'failed'}
                or 'full_trial_elapsed_seconds' not in record):
            raise ValueError('formal record is not final')
        owned_phases = [p for p in phases if p['attempt_id'].startswith(identity+'-')]
        names = {p['phase'] for p in owned_phases}
        if not {'draft', 'verification'} <= names or any(p['status'] not in completed for p in owned_phases):
            raise ValueError('formal phase receipts are not all terminal')
        records[identity] = record
    # Fixed, label-free order; this mapping is kept outside the submitted bundle.
    ordered = sorted(trials, key=lambda t: hashlib.sha256(
        ('v113-independent-audit:'+t['trial_id']).encode()).digest())
    mapping = {f'A{index:02d}': {'trial_id': t['trial_id'], 'case_id': t['case_id'],
        'arm': t['arm'], 'record_path': str(COMPARISON/'records'/(t['trial_id']+'.json')),
        'workspace': t['workspace']} for index, t in enumerate(ordered, 1)}
    cases_by_id = {case['case_id']: case for case in plan['cases']}
    cases = []
    for evidence_id, trial in zip(mapping, ordered):
        case = cases_by_id[trial['case_id']]
        record, workspace = records[trial['trial_id']], Path(trial['workspace'])
        input_path, original_path = workspace/case['input_name'], Path(case['source_path'])
        try:
            input_bytes = raw_owned(input_path)
        except FileNotFoundError:
            input_bytes = None
        original_bytes = raw_owned(original_path)
        item = {'evidence_id': evidence_id, 'raw_request': case['task'],
            'caller_sources': read_json(workspace/'sources.json'),
            'request_quote_anchors': request_quote_anchors(case['task']),
            'resolved_requirements': None, 'model_sources': [], 'source_bindings': [],
            'supplied_input': decoded_artifact(original_path),
            'candidate_proof': strip_hash_auxiliary(record.get('candidate_proof')),
            'actual_output': decoded_artifact(workspace/case['output_name']),
            'execution_observations': {key: record.get(key) for key in (
                'draft_invocation_verified', 'approved', 'execution_verified',
                'host_cleanup_complete', 'no_business_output', 'error_code',
                'verification_error_code')}}
        if trial['arm'] == 'review':
            proposal_id = (record.get('draft_gate') or {}).get('proposal_id')
            if proposal_id:
                db_path = Path(trial['db'])
                if not db_path.resolve().is_relative_to(COMPARISON.resolve()):
                    raise ValueError('proposal database escapes comparison ownership')
                # _get is the current integrity-checked document reader. Neither
                # show/confirm nor any execution or semantic oracle is invoked.
                document = ProposalStore(db_path)._get(proposal_id)
                item['resolved_requirements'] = document['requirements']
                item['model_sources'] = (document.get('review_context') or {}).get('model_sources', [])
        else:
            item['source_bindings'] = copy.deepcopy(record.get('source_bindings', []))
        item['execution_observations']['actual_input_bytes_preserved'] = input_bytes == original_bytes
        item['execution_observations']['actual_input_file_present'] = input_bytes is not None
        item['execution_observations']['actual_output_file_present'] = (workspace/case['output_name']).exists()
        if input_bytes != original_bytes:
            item['input_after'] = decoded_artifact(input_path)
        # Preserve original quote_ref and SourceRef identities so the supplied
        # quote/span records can actually dereference the candidate's citations.
        cases.append(item)
    bundle = {'schema_version': 'rpe.blind-formal-audit/v1',
        'instructions': [
            'Audit these eight independent evidence IDs without guessing withheld strategy labels.',
            'Use only raw request, caller sources, actual file contents, candidate proof, and observed execution facts.',
            'Insufficient evidence is not proven semantic error. If candidate evidence is missing or insufficient, reason MUST begin unknown; faithful=false then means unproven, not contradicted.',
            'Set artifact_correct=null when actual artifact correctness cannot be established. Missing output alone does not establish wrong semantics.',
            'Return all eight IDs exactly once. Reasons MUST be English and at most 12 words each; unsupported_claims MUST contain at most 3 concise strings.',
            'Do not infer tool invocation, correct execution, human approval, or verified costs from a candidate claim alone.'
        ], 'cases': cases}
    ensure_no_oracle_keys(bundle)
    if len(encoded(bundle)) > 124*1024:
        raise ValueError('complete evidence exceeds finite input budget; do not truncate or call')
    return bundle, mapping


def main():
    if (OWNED/'agent-audit').exists():
        raise FileExistsError('independent attempt already claimed; no retry')
    bundle, mapping = prepare_bundle()
    save_once(OWNED/'audit-blind-mapping.json', mapping)
    save_once(OWNED/'audit-blind-bundle.json', bundle)
    # The one and only provider-facing invocation in this script.
    result = run_agent_audit(bundle, OWNED)
    save_once(OWNED/'independent-audit-result.json', result)
    print(json.dumps({'status': result['status'], 'error_code': result['error_code'],
        'case_count': len(bundle['cases']), 'cli_estimated_cost_usd': result['cli_estimated_cost_usd'],
        'verified_bill_usd': result['verified_bill_usd']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
