"""Literal parameter witnesses catch contradictory drafts without interpreting prose."""
import copy
import json

import pytest

from rpe_harness.review_quotes import request_quote_anchors, source_quote_anchors


def candidate(execution, rows, *, capability='tabular.deduplicate'):
    return {'capability': capability, 'constraints': [], 'execution': execution,
            'requirements': rows, 'unresolved': []}


def requirement(field, quote, **extra):
    return {'text': 'Retain this requested value.', 'fields': ['/execution/' + field],
            'quote': quote, **extra}


def check(raw, draft, sources=None):
    from rpe_harness.review_values import validate_parameter_witnesses
    return validate_parameter_witnesses(raw, draft, sources)


def test_v114_full_request_anchor_cannot_validate_utf8_sig_as_utf8():
    raw = ('按附件复合业务键去重，使用真正的TAB字符分隔、UTF-8编码另存 output.csv。')
    anchor = request_quote_anchors(raw)[0]
    draft = candidate({'encoding': 'utf-8-sig', 'delimiter': '\t'}, [{
        'text': '输出编码为 UTF-8。', 'fields': ['/execution/encoding'],
        'quote_ref': anchor['quote_ref']}])
    before = copy.deepcopy(draft)
    result = check(raw, draft)
    assert result['status'] == 'needs_context'
    assert '/execution/encoding' in result['missing']
    assert any('parameter_value_conflict' in error for error in result['errors'])
    witness = result['witnesses'][0]
    assert witness['canonical_value'] == 'utf-8-sig'
    assert witness['literal_values'] == ['utf-8']
    assert witness['origin'] == 'source_quote'
    assert draft == before


@pytest.mark.parametrize('literal,value', [('UTF-8', 'utf8'), ('UTF_8', 'UTF-8'),
                                          ('latin-1', 'iso8859-1')])
def test_encoding_aliases_validate_without_rewriting_candidate(literal, value):
    draft = candidate({'encoding': value}, [requirement('encoding', literal)])
    result = check('Use ' + literal + ' encoding.', draft)
    assert result['status'] == 'ready'
    assert result['witnesses'][0]['status'] == 'verified_literal'
    assert draft['execution']['encoding'] == value


@pytest.mark.parametrize('literal,value', [('TAB', '\t'), ('\t', '\t'),
    ('逗号', ','), ('comma', ','), (';', ';'), ('"\\t"', '\t')])
def test_delimiter_literal_names_and_characters_bind_to_real_character(literal, value):
    result = check(literal, candidate({'delimiter': value}, [requirement('delimiter', literal)]))
    assert result['status'] == 'ready'
    assert result['witnesses'][0]['literal_values'] == [value]


def test_tab_witness_rejects_default_comma():
    result = check('真正的TAB字符分隔', candidate({'delimiter': ','}, [
        requirement('delimiter', '真正的TAB字符分隔')]))
    assert result['status'] == 'needs_context'
    assert any('parameter_value_conflict' in error for error in result['errors'])


@pytest.mark.parametrize('field,value,quote,capability', [
    ('keep', 'first', 'keep=first', 'tabular.deduplicate'),
    ('mode', 'rows', 'mode=rows', 'xlsx.inspect'),
    ('style', 'pretty', 'style=pretty', 'json.normalize')])
def test_registered_enumerated_literals_use_same_value_gate(field, value, quote, capability):
    result = check(quote, candidate({field: value}, [requirement(field, quote)],
                                    capability=capability))
    assert result['status'] == 'ready'
    assert result['witnesses'][0]['status'] == 'verified_literal'


@pytest.mark.parametrize('field,value,quote,capability', [
    ('mode', 'sheets', 'mode=rows', 'xlsx.inspect'),
    ('style', 'minify', 'style=pretty', 'json.normalize')])
def test_registered_enum_mismatch_is_not_an_encoding_specific_rule(field, value, quote, capability):
    result = check(quote, candidate({field: value}, [requirement(field, quote)],
                                    capability=capability))
    assert result['status'] == 'needs_context'
    assert any('parameter_value_conflict' in error for error in result['errors'])


def test_free_prose_keep_requirement_is_explicitly_unverified():
    raw = '保留首条整行和原行顺序'
    result = check(raw, candidate({'keep': 'first'}, [requirement('keep', raw)]))
    assert result['status'] == 'ready'
    assert result['witnesses'][0]['status'] == 'unverified_prose'
    assert result['coverage_limits']


@pytest.mark.parametrize('field,value', [('encoding', 'utf-8'), ('delimiter', ',')])
def test_full_request_without_parameter_literal_is_not_value_evidence(field, value):
    raw = '请完成任务并保留输入文件。'
    result = check(raw, candidate({field: value}, [requirement(field, raw)]))
    assert result['status'] == 'needs_context'
    assert any('parameter_literal_missing' in error for error in result['errors'])


def test_proposed_default_is_reviewable_and_never_claims_no_explicit_request():
    raw = '请用 UTF-8 输出。'
    result = check(raw, candidate({'encoding': 'utf-8-sig'}, [requirement('encoding', None)]))
    assert result['status'] == 'ready'
    assert result['witnesses'][0]['origin'] == 'proposed_default'
    assert result['witnesses'][0]['status'] == 'requires_human_review'
    assert result['coverage_limits']


def test_conflicting_supported_literals_require_review_instead_of_picking_one():
    raw = '请在 UTF-8 与 latin-1 中选择。'
    result = check(raw, candidate({'encoding': 'utf-8'}, [requirement('encoding', raw)]))
    assert result['status'] == 'needs_context'
    assert any('parameter_literal_ambiguous' in error for error in result['errors'])


def test_codec_name_embedded_inside_one_oversized_token_is_not_a_literal():
    raw = 'x' * 128 + 'utf-8'
    result = check(raw, candidate({'encoding': 'utf-8'}, [requirement('encoding', raw)]))
    assert result['status'] == 'needs_context'
    assert any('parameter_literal_missing' in error for error in result['errors'])


def test_attachment_typed_fact_witness_reuses_exact_source_address():
    ref = {'source': 'explicit-choice', 'event_id': 'event-1', 'sha256': 'a' * 64}
    text, facts = '决定：encoding=UTF-8。', {'encoding': 'UTF-8'}
    anchor = source_quote_anchors(ref, text, facts)['encoding']
    source_key = json.dumps(ref, sort_keys=True, ensure_ascii=False,
                            separators=(',', ':')).encode()
    sources = {source_key: {**ref, 'payload': {'text': text}, 'facts': facts}}
    draft = candidate({'encoding': 'utf8'}, [{
        'text': '使用明确附件中的编码。', 'fields': ['/execution/encoding'],
        'quote_ref': anchor['quote_ref']}])
    result = check('按附件决定。', draft, sources)
    assert result['status'] == 'ready'
    assert result['witnesses'][0]['origin'] == 'context_source'
    assert result['witnesses'][0]['source_ref'] == ref


@pytest.mark.parametrize('fault', ['unknown_ref', 'invented_quote', 'incompatible_source_ref'])
def test_unknown_or_misaddressed_witness_does_not_become_value_proof(fault):
    raw = '使用 UTF-8'
    row = requirement('encoding', 'UTF-8')
    if fault == 'unknown_ref':
        row.pop('quote')
        row['quote_ref'] = 'q:' + '0' * 64
    elif fault == 'invented_quote':
        row['quote'] = 'latin-1'
    else:
        row['source_ref'] = {'source': 'invented', 'event_id': 'x', 'sha256': '0' * 64}
    result = check(raw, candidate({'encoding': 'utf-8'}, [row]))
    assert result['status'] == 'needs_context'
    assert any('parameter_witness_source_invalid' in error for error in result['errors'])


def test_unsupported_and_unmapped_fields_gain_no_accidental_semantic_claim():
    raw = 'utf-8 input.csv'
    result = check(raw, candidate({'input_path': 'other.csv', 'encoding': 'utf-8-sig'}, [
        requirement('input_path', raw)]))
    assert result['status'] == 'ready'
    assert result['witnesses'] == []
    assert result['coverage_limits']


def test_bounded_input_rejects_oversized_prose_and_malformed_rows():
    for raw, draft in [('a' * 65537, candidate({}, [])),
                       ('UTF-8', candidate({'encoding': 'utf-8'}, [None]))]:
        result = check(raw, draft)
        assert result['status'] == 'needs_context'
        assert result['errors']
