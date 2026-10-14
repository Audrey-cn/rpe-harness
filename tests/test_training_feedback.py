"""Training export binds real runs; raw candidates execute under the actual boundary."""
import copy
import hashlib
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from rpe_harness.runtime import HarnessRuntime


def module():
    name = 'rpe_harness.evolution.training_feedback'
    assert importlib.util.find_spec(name) is not None, 'training feedback API is missing'
    return importlib.import_module(name)


def training_run(root, text='id,value\n01,first\n01,last\n02,other\n', *,
                 index=0, keys=None, encoding='utf-8', delimiter=',', source_bytes=None):
    root.mkdir(parents=True, exist_ok=True)
    source = root / f'input-{index}.csv'
    source.write_bytes(text.encode(encoding) if source_bytes is None else source_bytes)
    database = root / 'runs.sqlite'
    result = HarnessRuntime(db_path=database).run({'schema_version': 2,
        'event_id': f'training-{index}', 'capability': 'tabular.deduplicate',
        'goal': 'PRIVATE_TASK_BODY_WITH_CREDENTIAL', 'input_format': 'csv',
        'constraints': ['preserve_original', 'preserve_order'],
        'execution': {'input_path': str(source), 'output_path': str(root / f'output-{index}.csv'),
                      'key_columns': keys or ['id'], 'encoding': encoding, 'delimiter': delimiter}})
    assert result['status'] == 'succeeded', result
    return database, result['run_id']


def packet_for(root, **options):
    database, run_id = training_run(root, **options)
    return database, module().build_training_packet(database, [run_id], authorize_data_export=True)


def resign(packet):
    fields = {key: value for key, value in packet.items() if key != 'packet_sha256'}
    data = json.dumps(fields, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode()
    packet['packet_sha256'] = hashlib.sha256(data).hexdigest()
    return packet


def test_export_requires_explicit_boolean_authorization(tmp_path):
    api = module()
    for value in (False, None, 1, 'yes'):
        with pytest.raises(ValueError, match='authoriz'):
            api.build_training_packet(tmp_path / 'absent.sqlite', ['unknown'],
                                      authorize_data_export=value)
    assert not (tmp_path / 'absent.sqlite').exists()


def test_export_has_literal_truth_and_only_public_bound_fields(tmp_path):
    database, run_id = training_run(tmp_path)
    before = database.read_bytes()
    packet = module().build_training_packet(database, [run_id], authorize_data_export=True)
    assert packet['schema_version'] == 'rpe.training-feedback/v1'
    assert packet['kind'] == 'csv_executor'
    assert packet['capability'] == 'tabular.deduplicate'
    example = packet['examples'][0]
    assert example['expected_rows'] == [['id', 'value'], ['01', 'first'], ['02', 'other']]
    assert example['parameters'] == {'key_columns': ['id'], 'encoding': 'utf-8',
                                     'delimiter': ',', 'keep': 'first'}
    assert set(example) == {'run_id', 'source_text', 'expected_rows', 'parameters',
                            'input_sha256', 'output_sha256', 'plan_sha256', 'verification_sha256'}
    exported = json.dumps(packet)
    assert str(tmp_path) not in exported and 'PRIVATE_TASK_BODY' not in exported
    assert module().validate_training_packet(database, packet) == packet
    assert database.read_bytes() == before


@pytest.mark.parametrize('target', ['input', 'output', 'verification', 'plan', 'unverified'])
def test_export_rejects_mutated_or_unverified_source_anchors(tmp_path, target):
    database, run_id = training_run(tmp_path)
    api = module()
    packet = api.build_training_packet(database, [run_id], authorize_data_export=True)
    if target in ('input', 'output'):
        (tmp_path / f'{target}-0.csv').write_text('corrupt')
    else:
        with sqlite3.connect(database) as connection:
            if target == 'unverified':
                connection.execute("UPDATE runs SET status='failed', verification_ok=0 WHERE run_id=?", (run_id,))
            elif target == 'verification':
                row = connection.execute('SELECT verification_json FROM runs WHERE run_id=?', (run_id,)).fetchone()
                report = json.loads(row[0]); report['output_rows'] = 999
                connection.execute('UPDATE runs SET verification_json=? WHERE run_id=?', (json.dumps(report), run_id))
            else:
                connection.execute("UPDATE steps SET evidence_json='{}' WHERE run_id=? AND stage='plan'", (run_id,))
    with pytest.raises(ValueError):
        api.validate_training_packet(database, packet)


@pytest.mark.parametrize('mutation', ['source', 'truth', 'hash', 'unknown', 'parameter', 'run'])
def test_rehashed_spoofed_packet_cannot_replace_trusted_export(tmp_path, mutation):
    database, packet = packet_for(tmp_path)
    example = packet['examples'][0]
    if mutation == 'source': example['source_text'] = 'id,value\nX,fake\n'
    elif mutation == 'truth': example['expected_rows'][1][1] = 'fake'
    elif mutation == 'hash': example['verification_sha256'] = '0' * 64
    elif mutation == 'unknown': example['private_path'] = '/private/auth'
    elif mutation == 'parameter': example['parameters']['keep'] = 'last'
    else: packet['evidence_run_ids'] = ['not-a-real-run']; example['run_id'] = 'not-a-real-run'
    with pytest.raises(ValueError):
        module().validate_training_packet(database, resign(packet))


@pytest.mark.parametrize('ids', [[], ['missing'], ['same', 'same'], ['x'] * 9, 'not-a-list'])
def test_export_rejects_missing_duplicate_or_overbudget_run_ids(tmp_path, ids):
    with pytest.raises(ValueError):
        module().build_training_packet(tmp_path / 'absent.sqlite', ids, authorize_data_export=True)


def test_export_enforces_input_and_total_packet_budgets(tmp_path):
    database, run_id = training_run(tmp_path, 'id,value\n1,' + 'x' * 16384 + '\n')
    with pytest.raises(ValueError, match='budget|bounded|size'):
        module().build_training_packet(database, [run_id], authorize_data_export=True)
    root = tmp_path / 'total'
    ids = [training_run(root, 'id,value\n1,' + 'x' * 6000 + '\n', index=i)[1] for i in range(6)]
    with pytest.raises(ValueError, match='budget|bounded|size'):
        module().build_training_packet(root / 'runs.sqlite', ids, authorize_data_export=True)


def test_export_enforces_raw_output_budget_independently_of_input(tmp_path):
    text = 'id\n' + ''.join(f'{index}\n' for index in range(3000))
    database, run_id = training_run(tmp_path, text)
    assert (tmp_path / 'input-0.csv').stat().st_size < 16384
    assert (tmp_path / 'output-0.csv').stat().st_size > 16384
    with pytest.raises(ValueError, match='budget|bounded|size'):
        module().build_training_packet(database, [run_id], authorize_data_export=True)


def test_eight_real_runs_are_accepted_without_silent_truncation(tmp_path):
    ids = [training_run(tmp_path, index=index)[1] for index in range(8)]
    packet = module().build_training_packet(tmp_path / 'runs.sqlite', ids, authorize_data_export=True)
    assert packet['evidence_run_ids'] == ids
    assert [example['run_id'] for example in packet['examples']] == ids


@pytest.mark.parametrize('encoding,raw', [
    ('utf-8-sig', b'id,value\n1,first\n1,last\n'),
    ('utf-8-sig', b'\xef\xbb\xbfid,value\n1,first\n1,last\n'),
    ('utf-16', 'id,value\n1,first\n1,last\n'.encode('utf-16')),
    ('utf-16', b'\xfe\xff' + 'id,value\n1,first\n1,last\n'.encode('utf-16-be')),
])
def test_original_bytes_survive_consumed_encoding_signatures(tmp_path, encoding, raw):
    # Defaults accept UTF-8 with or without BOM; encoding=utf-8-sig alone does
    # not prove that the original bytes contained a signature.
    database, run_id = training_run(tmp_path, encoding=encoding, source_bytes=raw)
    packet = module().build_training_packet(database, [run_id], authorize_data_export=True)
    assert packet['examples'][0]['source_text'].startswith('id,value')
    assert module().validate_training_packet(database, packet) == packet


GOOD_CODE = '''import csv
def prepare(plan, staging_path):
    with open(plan['input_path'], encoding=plan['encoding'], newline='') as source:
        rows = list(csv.reader(source, delimiter=plan['delimiter'], strict=True))
    positions = [rows[0].index(name) for name in plan['key_columns']]
    result, seen = [rows[0]], set()
    for row in rows[1:]:
        key = tuple(row[position] for position in positions)
        if key not in seen:
            result.append(row)
            seen.add(key)
    with open(staging_path, 'w', encoding=plan['encoding'], newline='') as target:
        csv.writer(target, delimiter=plan['delimiter']).writerows(result)
'''

SANDBOX = pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
                             reason='real candidate OS sandbox unavailable')


@SANDBOX
def test_probe_runs_raw_candidate_on_composite_multiline_exact_values(tmp_path):
    text = 'a;b;value\n01;X;"first\nline"\n01;X;later\n01;x;lower\n1;X;numeric\n'
    _, packet = packet_for(tmp_path, text=text, keys=['a', 'b'], delimiter=';', encoding='utf-8-sig')
    assert packet['examples'][0]['expected_rows'] == [
        ['a', 'b', 'value'], ['01', 'X', 'first\nline'], ['01', 'x', 'lower'], ['1', 'X', 'numeric']]
    result = module().probe_training_candidate(GOOD_CODE, packet, tmp_path)
    assert result['candidate_sha256'] == hashlib.sha256(GOOD_CODE.encode()).hexdigest()
    assert result['packet_sha256'] == packet['packet_sha256']
    outcome = result['outcomes'][0]
    assert outcome['candidate_invoked'] and outcome['source_preserved'] and outcome['correct']
    assert not outcome['rejection_observed'] and not outcome['timed_out']
    assert outcome['rule_failures'] == []


@SANDBOX
@pytest.mark.parametrize('code,rule,rejected,timed_out', [
    ("def prepare(plan, staging_path):\n    raise ValueError('PRIVATE_ERROR')\n", 'candidate_rejected', True, False),
    ("def prepare(plan, staging_path):\n    while True: pass\n", 'candidate_timeout', False, True),
    ("def prepare(plan, staging_path):\n    open(staging_path, 'w').write('id,value\\n01,last\\n')\n", 'exact_first_records', False, False),
    ("def prepare(plan, staging_path):\n    1 / 0\n", 'candidate_execution', False, False),
    ("def prepare(plan, staging_path):\n    open(plan['input_path'], 'w').write('corrupt')\n", 'candidate_execution', False, False),
])
def test_probe_retains_actual_failures_without_private_or_qualification_material(tmp_path, code, rule, rejected, timed_out):
    _, packet = packet_for(tmp_path)
    before = (tmp_path / 'input-0.csv').read_bytes()
    result = module().probe_training_candidate(code, packet, tmp_path)
    outcome = result['outcomes'][0]
    assert outcome['candidate_invoked'] and outcome['source_preserved'] and not outcome['correct']
    assert outcome['rejection_observed'] is rejected and outcome['timed_out'] is timed_out
    assert rule in outcome['rule_failures']
    assert (tmp_path / 'input-0.csv').read_bytes() == before
    saved = json.dumps(result)
    assert str(tmp_path) not in saved and 'PRIVATE_ERROR' not in saved
    assert 'heldout' not in saved and 'fixture_suite' not in saved


def test_probe_rejects_bad_hash_budget_and_unknown_fields_before_execution(tmp_path):
    _, packet = packet_for(tmp_path)
    variants = []
    changed = copy.deepcopy(packet); changed['examples'][0]['source_text'] += 'tamper'; variants.append(changed)
    changed = copy.deepcopy(packet); changed['examples'][0]['source_text'] = 'x' * 16385; variants.append(resign(changed))
    changed = copy.deepcopy(packet); changed['credential'] = 'PRIVATE'; variants.append(resign(changed))
    for invalid in variants:
        with pytest.raises(ValueError):
            module().probe_training_candidate(GOOD_CODE, invalid, tmp_path / 'not-created')
    with pytest.raises(ValueError):
        module().probe_training_candidate('x' * 65537, packet, tmp_path / 'not-created')
    assert not (tmp_path / 'not-created').exists()
