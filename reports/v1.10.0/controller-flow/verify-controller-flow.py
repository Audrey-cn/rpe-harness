#!/usr/bin/env python3
"""Portable, stdlib-only check of the separately labelled post-measurement flow."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = item
    return value


def document(path):
    value = json.loads(path.read_bytes(), object_pairs_hook=unique)
    saved = value.pop('sha256')
    if saved != sha(canonical(value)):
        raise ValueError('document_digest_changed')
    return value


def compact(raw):
    """Independent byte oracle: remove only structural ASCII JSON whitespace."""
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('artifact_over_budget')
    def invalid_constant(value):
        raise ValueError('non_json_constant')
    json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=unique,
               parse_float=Decimal, parse_int=Decimal, parse_constant=invalid_constant)
    result = bytearray()
    quoted = escaped = False
    for byte in raw:
        if quoted:
            result.append(byte)
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
            result.append(byte)
        elif byte not in b' \t\r\n':
            result.append(byte)
    if quoted or escaped:
        raise ValueError('unterminated_json_string')
    return bytes(result)


def verify(root):
    root = root.absolute()
    manifest = document(root / 'manifest.json')
    for name, expected in manifest['files'].items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('manifest_path_escape')
        path = root / relative
        if path.is_symlink() or not path.is_file() or sha(path.read_bytes()) != expected:
            raise ValueError('public_file_changed')
    receipt = document(root / 'receipt.json')
    proposal = document(root / 'proposal-public.json')
    confirmation = document(root / 'confirmation.json')
    durable = document(root / 'durable-proof.json')
    oracle = document(root / 'oracle.json')
    assert receipt['role'] == 'post_measurement_controller_flow'
    assert receipt['primary_scores_changed'] is False and receipt['human_confirmation'] is False
    assert receipt['new_Hermes_model_calls'] == receipt['controller_api_requests'] == 0
    assert receipt['Codex_development_review_tokens'] is None
    assert receipt['Codex_development_review_billing'] is None
    assert receipt['primary_preservation']['before'] == receipt['primary_preservation']['after']
    assert receipt['workspace_added_files'] == ['output.json']
    assert all(receipt['checks'].values())
    assert proposal['proposal_id'] == proposal['proposal_sha256'] == receipt['proposal_id']
    assert proposal['source']['source_sha256'] == sha(proposal['source']['raw'].encode())
    assert proposal['status'] == 'awaiting_confirmation' and proposal['unresolved_sha256'] == []
    assert proposal['candidate'] == {'capability': 'json.normalize', 'constraints': ['preserve_order'],
        'execution': {'input_path': 'input.json', 'output_path': 'output.json', 'style': 'minify'}}
    required = {'/capability', '/constraints', *('/execution/' + k for k in proposal['candidate']['execution'])}
    covered = set()
    for row in proposal['requirements']:
        quote = row['quote']
        if quote is None:
            assert row['span'] is None and row['origin'] == 'proposed_default'
        else:
            start = proposal['source']['raw'].find(quote)
            assert start >= 0 and row['span'] == [start, start + len(quote)] and row['origin'] == 'source_quote'
        assert row['fields'] and set(row['fields']) <= required
        covered.update(row['fields'])
    assert covered == required
    expected_confirmation = {'schema_version': 'rpe.confirmation/v1', 'status': 'confirmed',
        'proposal_id': proposal['proposal_id'], 'proposal_sha256': proposal['proposal_sha256'],
        'session_id': proposal['source']['session_id'], 'actor': 'simulated_semantic_review'}
    assert {k: v for k, v in confirmation.items() if k != 'confirmation_id'} == expected_confirmation
    assert confirmation['confirmation_id'] == sha(canonical(expected_confirmation)) == receipt['confirmation_id']
    assert durable['confirmation_id'] == confirmation['confirmation_id']
    assert durable['run_id'] == receipt['run_id'] and durable['status'] == 'succeeded'
    assert durable['event_id'] == 'review-' + confirmation['confirmation_id']
    assert durable['public_contract'] == proposal['machine_contract_public']
    assert durable['original_contract_sha256'] == durable['durable_contract_sha256']
    assert durable['worker_cleanup']['cleanup_complete'] is True
    assert durable['replay_same_result'] is True and durable['new_run_count'] == 1
    source, output = ((root / name).read_bytes() for name in ('input.json', 'output.json'))
    assert sha(source) == oracle['input_sha256'] == receipt['input_sha256']
    assert sha(output) == oracle['output_sha256'] == receipt['output_sha256']
    assert output == compact(source) + b'\n' == oracle['public_fixture_expected_text'].encode()
    assert oracle['published_independent_oracle']['passed'] is True
    assert all(oracle['published_independent_oracle']['checks'].values())
    return {'verified': True, 'role': receipt['role'], 'new_Hermes_model_calls': 0,
        'controller_api_requests': 0, 'human_confirmation': False,
        'runtime_attestation': 'recorded_runtime_checks_only', 'run_id': receipt['run_id']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    started = time.monotonic()
    try:
        result = verify(args.root)
    except (OSError, ValueError, TypeError, KeyError, AssertionError, UnicodeError) as error:
        result = {'verified': False, 'error_code': type(error).__name__}
    result['offline_verification_elapsed_seconds'] = time.monotonic() - started
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
