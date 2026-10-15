#!/usr/bin/env python3
"""Export observed evidence and prepare a blinded CLI bundle, without model calls."""
import csv
import hashlib
import io
import json
from pathlib import Path
import random
import shutil
import sys

sys.dont_write_bytecode = True
WORKTREE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKTREE/'src'))
REPORT = WORKTREE/'reports/v1.15.0'
OWNED = Path(json.loads((REPORT/'validation/owned.json').read_bytes())['root'])


def write(path, value):
    if path.exists():
        if json.loads(path.read_bytes()) != value:
            raise ValueError('saved evidence differs')
        return
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def main():
    from rpe_harness.experiments import review_v111_comparison as shared
    from rpe_harness.experiments.review_v115_comparison import verify_v115_evidence
    from rpe_harness.review_quotes import request_quote_anchors
    plan = shared._load(OWNED/'comparison-runtime')
    summary = shared.summarize_v111_comparison(OWNED/'comparison-runtime')
    if not (REPORT/'evidence').exists():
        shared.export_v111_evidence(OWNED/'comparison-runtime', REPORT/'evidence')
    verification = verify_v115_evidence(REPORT/'evidence')
    write(REPORT/'validation/portable-verification.json', verification)
    shutil.copy2(OWNED/'comparison-runtime/preregistration.json', REPORT/'preregistration.json')
    shutil.copy2(OWNED/'comparison-runtime/formal-freeze.json', REPORT/'formal-freeze.json')
    if not (REPORT/'observations').exists():
        shutil.copytree(OWNED/'observations-final', REPORT/'observations')
    records = list(summary['records'])
    random.SystemRandom().shuffle(records)
    bundle = {'schema_version': 'rpe.blind-formal-audit/v1', 'cases': [],
        'instructions': ['Use only supplied task, caller sources, candidate and actual file contents.',
          'Interpretation is separate from execution/protocol acceptance. Missing evidence is unknown.',
          'Do not infer verified billing, human approval or general stable superiority.']}
    mapping = {}
    for index, record in enumerate(records, 1):
        trial = next(t for t in plan['sessions'] if t['trial_id'] == record['trial_id'])
        case = next(c for c in plan['cases'] if c['case_id'] == trial['case_id'])
        workspace = Path(trial['workspace'])
        evidence_id = f'A{index:02}'
        mapping[evidence_id] = {'trial_id': trial['trial_id'], 'arm': trial['arm'], 'case_id': trial['case_id']}
        def artifact(name):
            path = workspace/name
            if not path.is_file():
                return None
            raw = path.read_bytes()
            row = {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}
            if path.suffix == '.xlsx':
                from openpyxl import load_workbook
                book = load_workbook(path, read_only=True, data_only=False)
                row['sheets'] = {sheet.title: list(sheet.values) for sheet in book}
                book.close()
            else:
                row['text'] = raw.decode('utf-8')
                if path.suffix == '.csv':
                    row['rows'] = list(csv.reader(io.StringIO(row['text'].removeprefix('\ufeff')),
                        delimiter=case['parameters']['delimiter']))
                    row['utf8_bom_present'] = raw.startswith(b'\xef\xbb\xbf')
            return row
        bundle['cases'].append({'evidence_id': evidence_id, 'raw_request': case['task'],
            'request_quote_anchors': request_quote_anchors(case['task']),
            'caller_supplied_sources': case['sources'], 'candidate_proof': record.get('candidate_proof'),
            'source_bindings': record.get('source_bindings', []),
            'original_invalid_candidate': record.get('original_invalid_candidate'),
            'parameter_sources': record.get('parameter_sources', []),
            'supplied_input': artifact(case['input_name']), 'actual_output': artifact(case['output_name']),
            'execution_observations': {key: record.get(key) for key in ('draft_invocation_verified',
                'approved', 'execution_verified', 'no_business_output', 'source_preserved',
                'host_cleanup_complete', 'error_code')}})
    write(OWNED/'audit-blind-bundle.json', bundle)
    write(REPORT/'independent-audit-mapping.json', mapping)
    write(REPORT/'independent-audit-bundle.json', bundle)
    print(json.dumps(verification, ensure_ascii=False))


if __name__ == '__main__':
    main()
