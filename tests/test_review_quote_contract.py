"""Explicit quote addresses preserve exact sources without repairing model text."""
import copy
import json
import sqlite3

import pytest

from test_proposal import RAW, draft, proposed
from test_proposal_context import setup, propose
from test_hermes_review import review as review


def test_review_delimiter_schema_uses_public_character_contract(review):
    from rpe_harness.contracts import request_schema
    from rpe_harness.integrations.hermes_review import REVIEW_CONTRACT
    schema = review['sdk'].tools['rpe_propose']['schema']['parameters']['properties']
    delimiter = schema['execution']['properties']['delimiter']
    for key, value in request_schema()['properties']['execution']['properties']['delimiter'].items():
        assert delimiter[key] == value
    assert delimiter['examples'] == [',', '\t']
    assert 'never the words comma or TAB' in REVIEW_CONTRACT


@pytest.mark.parametrize('delimiter,accepted', [(',', True), ('\t', True),
    ('comma', False), ('TAB', False), ('\\t', False), ('\n', False), ('\x00', False)])
def test_delimiter_values_are_validated_without_rewriting_draft(tmp_path, delimiter, accepted):
    from rpe_harness.proposal import create_proposal
    from test_hermes_review import RAW as csv_raw, draft as csv_draft
    actual = delimiter if accepted else ','
    (tmp_path / 'orders.csv').write_text(actual.join(['id', 'value']) + '\na' + actual + '1\n')
    candidate = csv_draft()
    candidate['execution']['delimiter'] = delimiter
    result = create_proposal(csv_raw, candidate, db_path=tmp_path / 'state.sqlite',
        workspace=tmp_path, session_id='s', source_ref='current:user')
    assert (result['status'] == 'awaiting_confirmation') is accepted
    assert result['draft'] == candidate
    if accepted:
        assert result['machine_contract']['execution']['delimiter'] == delimiter
    assert not (tmp_path / 'out.csv').exists()


def test_request_anchors_are_exact_bounded_and_bound_to_whole_source():
    from rpe_harness.review_quotes import request_quote_anchors, resolve_quote_ref
    raw = '标题：😀\r\n\n保留逐字文本\t。\n'
    anchors = request_quote_anchors(raw)
    assert anchors[0]['quote'] == raw and anchors[0]['span'] == [0, len(raw)]
    assert [row['quote'] for row in anchors[1:]] == ['标题：😀', '保留逐字文本\t。']
    for row in anchors:
        assert raw[slice(*row['span'])] == row['quote']
        assert resolve_quote_ref(raw, row['quote_ref']) == row
        with pytest.raises(ValueError):
            resolve_quote_ref(raw + 'changed', row['quote_ref'])
    assert len(request_quote_anchors('x\n' * 1000)) <= 33


def test_addressed_request_is_rendered_without_rewriting_model_draft(tmp_path):
    from rpe_harness.review_quotes import request_quote_anchors
    from rpe_harness.proposal import ProposalStore
    candidate = draft()
    anchor = request_quote_anchors(RAW)[0]
    candidate['requirements'] = [{'text': '按完整当前请求起草', 'quote_ref': anchor['quote_ref'],
        'fields': ['/capability', '/constraints', *('/execution/' + key for key in candidate['execution'])]}]
    original = copy.deepcopy(candidate)
    result = proposed(tmp_path, candidate)
    assert result['status'] == 'awaiting_confirmation', result
    assert candidate == original == result['draft']
    assert 'quote' not in result['draft']['requirements'][0]
    assert result['requirements'][0]['quote'] == RAW
    assert result['requirements'][0]['span'] == [0, len(RAW)]
    assert anchor['quote_ref'] in result['summary']
    store = ProposalStore(tmp_path / 'state.sqlite')
    confirmation = store.confirm(result['proposal_id'], session_id='s',
        expected_proposal_sha256=result['proposal_sha256'], actor='human')
    executed = store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path)
    assert executed['status'] == 'succeeded'
    assert store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path) == executed


@pytest.mark.parametrize('fault', ['unknown', 'paraphrase', 'null', 'source_ref', 'raw_change'])
def test_quote_addresses_reject_unknown_stale_and_mismatched_claims(tmp_path, fault):
    from rpe_harness.review_quotes import request_quote_anchors
    candidate = draft()
    row = candidate['requirements'][0]
    row.pop('quote')
    row['quote_ref'] = request_quote_anchors(RAW)[0]['quote_ref']
    raw = RAW
    if fault == 'unknown':
        row['quote_ref'] = 'q:' + '0' * 64
    elif fault == 'paraphrase':
        row['quote'] = '请压缩 JSON 文件'
    elif fault == 'null':
        row['quote'] = None
    elif fault == 'source_ref':
        row['source_ref'] = {'source': 'invented', 'event_id': 'x', 'sha256': '0' * 64}
    else:
        raw += ' '
    result = proposed(tmp_path, candidate, raw=raw)
    assert result['status'] == 'needs_context'
    assert result['draft'] == candidate


def test_attachment_address_preserves_fact_gate_and_live_revalidation(tmp_path):
    from rpe_harness.proposal import ProposalStore
    path, context, candidate = setup(tmp_path)
    projected = context['model_sources'][0]
    row = candidate['requirements'][-1]
    row.pop('quote')
    row.pop('source_ref')
    row['quote_ref'] = projected['quote_refs']['key_columns']
    original = copy.deepcopy(candidate)
    result = propose(tmp_path, path, context, candidate)
    assert result['status'] == 'awaiting_confirmation', result
    assert result['draft'] == original and 'source_ref' not in original['requirements'][-1]
    rendered = result['requirements'][-1]
    assert rendered['quote'] == projected['quotes']['key_columns'] == '订单号'
    assert rendered['span'] == projected['quote_spans']['key_columns']
    assert rendered['source_ref'] == context['source_refs'][0]
    assert rendered['origin'] == 'context_source'
    candidate['execution']['key_columns'] = ['值']
    assert propose(tmp_path, path, context, candidate)['status'] == 'needs_context'
    store = ProposalStore(path)
    confirmation = store.confirm(result['proposal_id'], session_id='s',
        expected_proposal_sha256=result['proposal_sha256'], actor='human')
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM context_sources')
    with pytest.raises(ValueError):
        store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path)
    assert not (tmp_path / 'out.csv').exists()


@pytest.mark.parametrize('fault', ['field', 'source_ref', 'session', 'ref', 'projection_ref'])
def test_attachment_address_never_bypasses_source_or_fact_checks(tmp_path, fault):
    path, context, candidate = setup(tmp_path)
    row = candidate['requirements'][-1]
    row.pop('quote')
    row['quote_ref'] = context['model_sources'][0]['quote_refs']['key_columns']
    if fault == 'field':
        row['fields'].append('/execution/output_path')
    elif fault == 'source_ref':
        row['source_ref']['sha256'] = '0' * 64
    elif fault == 'session':
        context['session_id'] = 'other'
    elif fault == 'ref':
        context['source_refs'][0]['sha256'] = '0' * 64
    else:
        context['model_sources'][0]['quote_refs']['key_columns'] = 'q:' + '0' * 64
    result = propose(tmp_path, path, context, candidate)
    assert result['status'] == 'needs_context'
    assert result['draft'] == candidate


def test_literal_paraphrase_remains_blocked_without_an_address(tmp_path):
    candidate = draft()
    candidate['requirements'][0]['quote'] = '请压缩 JSON 文件'
    result = proposed(tmp_path, candidate)
    assert result['status'] == 'needs_context'
    assert result['draft'] == candidate


def test_legacy_context_projection_remains_revalidatable(tmp_path):
    from rpe_harness.review_context import validate_context
    path, context, _ = setup(tmp_path)
    legacy = copy.deepcopy(context)
    for item in legacy['model_sources']:
        item.pop('quote_refs')
        item.pop('quote_spans')
    assert validate_context(path, legacy, 's') == legacy


def test_model_payload_publishes_exact_anchors_and_both_citation_forms(review):
    api = review['api']
    from rpe_harness.review_quotes import request_quote_anchors
    config = json.loads(review['config'].read_text())
    messages = api._messages(config, {'phase': 'propose'})
    assert messages[1] == {'role': 'user', 'content': config['raw']}
    assert json.dumps(request_quote_anchors(config['raw']), ensure_ascii=False, sort_keys=True) in messages[2]['content']
    item = review['sdk'].tools['rpe_propose']['schema']['parameters']['properties']['requirements']['items']
    assert item['required'] == ['text', 'fields']
    assert item['anyOf'] == [{'required': ['quote']}, {'required': ['quote_ref']}]
    assert item['properties']['quote_ref']['pattern'] == '^q:[a-f0-9]{64}$'
