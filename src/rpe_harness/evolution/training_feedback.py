"""Authorized CSV training exports and real, bounded untrusted-code probes.

Packet hashes detect corruption; authenticity is established only by rebuilding
against the caller's trusted run database. No qualification fixtures live here.
"""
from __future__ import annotations

import codecs
import csv
import io
import os
import re
import tempfile
from pathlib import Path

from ..path_binding import _directory, read_regular_file
from ..storage import RunStore, canonical_json
from ..tasks import plan_from_dict
from .cycle import inspect_verified_feedback
from .sandbox import CandidateExecutionError, CandidateRejected, MAX_CODE_BYTES, run_candidate
from .store import digest

_SCHEMA = 'rpe.training-feedback/v1'
_EXAMPLE_BYTES = 16 * 1024
_PACKET_BYTES = 64 * 1024
_PARAMETERS = {'key_columns', 'encoding', 'delimiter', 'keep'}
_HASHES = {'input_sha256', 'output_sha256', 'plan_sha256', 'verification_sha256'}
_EXAMPLE_FIELDS = {'run_id', 'source_text', 'expected_rows', 'parameters'} | _HASHES
_PACKET_FIELDS = {'schema_version', 'kind', 'capability', 'evidence_run_ids', 'examples', 'packet_sha256'}


def _identifiers(run_ids):
    if (type(run_ids) is not list or not 1 <= len(run_ids) <= 8
            or any(type(value) is not str or not value or len(value) > 128
                   or '/' in value or '\\' in value for value in run_ids)
            or len(set(run_ids)) != len(run_ids)):
        raise ValueError('training requires one to eight unique bounded run IDs')
    return list(run_ids)


def _rows(data, parameters):
    return list(csv.reader(io.StringIO(data.decode(parameters['encoding']), newline=''),
                           delimiter=parameters['delimiter'], strict=True))


def _training_truth(rows, parameters):
    """Derive exact first-record truth without producer or oracle helpers."""
    if (not rows or not rows[0] or any(not name.strip() for name in rows[0])
            or len(set(rows[0])) != len(rows[0])
            or any(len(row) != len(rows[0]) for row in rows[1:])):
        raise ValueError('training CSV has invalid rows or headers')
    positions = [rows[0].index(name) for name in parameters['key_columns']]
    expected = [rows[0]]
    seen = set()
    for row in rows[1:]:
        key = tuple(row[position] for position in positions)
        if any(not value.strip() for value in key):
            raise ValueError('training CSV contains empty keys')
        if key not in seen:
            seen.add(key)
            expected.append(row)
    return expected


def _source_bytes(example):
    """Reconstruct decoded text, including the optional UTF-8 signature."""
    encoded = example['source_text'].encode(example['parameters']['encoding'])
    alternatives = [encoded]
    for marker in (codecs.BOM_UTF8, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE,
                   codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        if encoded.startswith(marker):
            alternatives.append(encoded[len(marker):])
    # UTF-16/32 text decoding consumes the byte-order marker. Restore either
    # byte order only when its bytes match the frozen original source digest.
    encoding = codecs.lookup(example['parameters']['encoding']).name
    if encoding in {'utf-16', 'utf-32'}:
        for suffix, marker in [('le', codecs.BOM_UTF16_LE if encoding == 'utf-16' else codecs.BOM_UTF32_LE),
                               ('be', codecs.BOM_UTF16_BE if encoding == 'utf-16' else codecs.BOM_UTF32_BE)]:
            alternatives.append(marker + example['source_text'].encode(encoding + '-' + suffix))
    for data in alternatives:
        if digest(data) == example['input_sha256']:
            if len(data) > _EXAMPLE_BYTES:
                raise ValueError('training input exceeds the bounded size budget')
            return data
    raise ValueError('training source text differs from its original-byte hash')


def _check_packet(packet):
    if type(packet) is not dict or set(packet) != _PACKET_FIELDS:
        raise ValueError('training packet fields are incomplete or unknown')
    try:
        size = len(canonical_json(packet).encode('utf-8'))
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError('training packet must be a serializable bounded object') from error
    if size > _PACKET_BYTES:
        raise ValueError('training packet exceeds the total size budget')
    if (packet['schema_version'], packet['kind'], packet['capability']) != (
            _SCHEMA, 'csv_executor', 'tabular.deduplicate'):
        raise ValueError('training packet contract is not registered')
    identifiers = _identifiers(packet['evidence_run_ids'])
    examples = packet['examples']
    if type(examples) is not list or len(examples) != len(identifiers):
        raise ValueError('training examples do not match evidence IDs')
    if packet['packet_sha256'] != digest({key: value for key, value in packet.items()
                                           if key != 'packet_sha256'}):
        raise ValueError('training packet hash differs')
    for identifier, example in zip(identifiers, examples):
        if type(example) is not dict or set(example) != _EXAMPLE_FIELDS or example['run_id'] != identifier:
            raise ValueError('training example fields or evidence binding differ')
        if any(type(example[key]) is not str or not re.fullmatch('[a-f0-9]{64}', example[key])
               for key in _HASHES):
            raise ValueError('training example requires SHA256 bindings')
        if type(example['source_text']) is not str:
            raise ValueError('training source must be text')
        if len(example['source_text'].encode('utf-8')) > _EXAMPLE_BYTES:
            raise ValueError('training input text exceeds the size budget')
        parameters = example['parameters']
        if type(parameters) is not dict or set(parameters) != _PARAMETERS:
            raise ValueError('training parameter fields are incomplete or unknown')
        columns = parameters['key_columns']
        if (type(columns) is not list or not columns
                or any(type(name) is not str or not name.strip() for name in columns)
                or len(set(columns)) != len(columns) or parameters['keep'] != 'first'
                or type(parameters['encoding']) is not str
                or type(parameters['delimiter']) is not str or len(parameters['delimiter']) != 1
                or parameters['delimiter'] in '\r\n\x00'):
            raise ValueError('training parameters are invalid')
        rows = example['expected_rows']
        if (type(rows) is not list or not rows or not rows[0]
                or any(type(row) is not list or len(row) != len(rows[0])
                       or any(type(cell) is not str for cell in row) for row in rows)):
            raise ValueError('training expected rows must be a rectangular string table')
        if len(canonical_json(rows).encode('utf-8')) > _EXAMPLE_BYTES:
            raise ValueError('training expected result exceeds the size budget')
        try:
            data = _source_bytes(example)
            truth = _training_truth(_rows(data, parameters), parameters)
        except (LookupError, UnicodeError, csv.Error) as error:
            raise ValueError('training CSV text or encoding is invalid') from error
        if truth != rows:
            raise ValueError('training expected rows differ from independent first-record truth')
    return identifiers


def build_training_packet(db_path, run_ids, *, authorize_data_export=False):
    """Export only explicitly selected, freshly verified CSV business data."""
    if authorize_data_export is not True:
        raise ValueError('training data export requires explicit authorization')
    identifiers = _identifiers(run_ids)
    try:
        runs = RunStore.read_only(db_path)
        examples = []
        for identifier in identifiers:
            run = runs.get(identifier)
            anchors = [step['evidence'] for step in run['steps'] if step['stage'] == 'plan']
            if len(anchors) != 1:
                raise ValueError('training requires verified plan-bound runs')
            plan = plan_from_dict(anchors[0]['plan'])
            if (plan.capability, plan.input_format) != ('tabular.deduplicate', 'csv'):
                raise ValueError('training requires verified CSV runs')
            # Bound reads before the oracle performs its fresh source checks.
            source = read_regular_file(plan.input_path, max_bytes=_EXAMPLE_BYTES)
            output = read_regular_file(plan.output_path, max_bytes=_EXAMPLE_BYTES)
            feedback = inspect_verified_feedback(identifier, db_path)
            if (feedback is None or feedback['plan_sha256'] != plan.sha256
                    or feedback['input_sha256'] != digest(source)
                    or feedback['output_sha256'] != digest(output)):
                raise ValueError('training requires fresh verified source and artifact anchors')
            parameters = {key: getattr(plan, key) for key in _PARAMETERS}
            parameters['key_columns'] = list(plan.key_columns)
            expected = _rows(output, parameters)
            if expected != _training_truth(_rows(source, parameters), parameters):
                raise ValueError('verified training output differs from independent truth')
            examples.append({'run_id': identifier, 'source_text': source.decode(plan.encoding),
                             'expected_rows': expected, 'parameters': parameters,
                             **{key: feedback[key] for key in _HASHES}})
    except (OSError, KeyError, TypeError, LookupError, UnicodeError, csv.Error) as error:
        raise ValueError('training source evidence is unavailable or invalid') from error
    packet = {'schema_version': _SCHEMA, 'kind': 'csv_executor',
              'capability': 'tabular.deduplicate', 'evidence_run_ids': identifiers, 'examples': examples}
    packet['packet_sha256'] = digest(packet)
    _check_packet(packet)
    return packet


def validate_training_packet(db_path, packet):
    """Return fresh trusted data; a self-signed hash is never authenticity."""
    identifiers = _check_packet(packet)
    rebuilt = build_training_packet(db_path, identifiers, authorize_data_export=True)
    if canonical_json(rebuilt) != canonical_json(packet):
        raise ValueError('training packet differs from freshly verified evidence')
    return rebuilt


def probe_training_candidate(content, packet, owned_root):
    """Execute original candidate bytes against training only; report public facts."""
    _check_packet(packet)
    if type(content) is str:
        content = content.encode('utf-8')
    if type(content) is not bytes or not content or len(content) > MAX_CODE_BYTES:
        raise ValueError('training candidate must be nonempty bounded code bytes or text')
    root = Path(owned_root)
    if root.is_symlink():
        raise ValueError('training probe root must be an owned directory without symlinks')
    root.mkdir(parents=True, exist_ok=True)
    descriptor = _directory(root)
    os.close(descriptor)
    outcomes = []
    with tempfile.TemporaryDirectory(prefix='training-probe-', dir=root) as name:
        directory = Path(name)
        for index, example in enumerate(packet['examples']):
            source = directory / f'input-{index}.csv'
            original = _source_bytes(example)
            source.write_bytes(original)
            plan = {'capability': 'tabular.deduplicate', 'input_format': 'csv',
                    'input_path': str(source), 'input_sha256': example['input_sha256'],
                    **example['parameters']}
            outcome = {'run_id': example['run_id'], 'candidate_invoked': False,
                       'source_preserved': True, 'correct': False, 'rejection_observed': False,
                       'timed_out': False, 'rule_failures': [], 'error_type': None}
            try:
                output = run_candidate(content, plan, directory)
                outcome['candidate_invoked'] = True
                if len(output) > _EXAMPLE_BYTES:
                    outcome['rule_failures'].append('output_budget')
                else:
                    try:
                        actual = _rows(output, example['parameters'])
                        if not actual or actual[0] != example['expected_rows'][0]:
                            outcome['rule_failures'].append('headers_preserved')
                        if actual != example['expected_rows']:
                            outcome['rule_failures'].append('exact_first_records')
                        outcome['correct'] = actual == example['expected_rows']
                    except (UnicodeError, csv.Error):
                        outcome['rule_failures'].append('readable_csv')
            except CandidateRejected:
                outcome.update(candidate_invoked=True, rejection_observed=True,
                               error_type='CandidateRejected', rule_failures=['candidate_rejected'])
            except CandidateExecutionError as error:
                timeout = str(error) == 'candidate timeout'
                outcome.update(candidate_invoked=True, timed_out=timeout,
                               error_type='CandidateExecutionError',
                               rule_failures=['candidate_timeout' if timeout else 'candidate_execution'])
            except RuntimeError:
                outcome.update(error_type='RuntimeError', rule_failures=['sandbox_unavailable'])
            finally:
                outcome['source_preserved'] = source.is_file() and not source.is_symlink() and source.read_bytes() == original
                if not outcome['source_preserved']:
                    outcome['correct'] = False
                    outcome['rule_failures'].append('source_preservation')
            outcomes.append(outcome)
    return {'candidate_sha256': digest(content), 'packet_sha256': packet['packet_sha256'],
            'outcomes': outcomes}
