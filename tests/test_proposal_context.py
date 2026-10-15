"""Context is reviewable evidence, never a repair or an execution authorization."""
import copy
import importlib
import importlib.util
import json
import sqlite3
from pathlib import Path
import shutil

import pytest


RAW = '请把 input.csv 去重，另存 out.csv，业务键沿用附件中的明确决定。'


def setup(tmp_path, facts=None):
    assert importlib.util.find_spec('rpe_harness.review_context'), 'review context bridge is absent'
    bridge = importlib.import_module('rpe_harness.review_context')
    (tmp_path / 'input.csv').write_text('订单号,值\na,1\na,2\nb,3\n')
    path = tmp_path / 'state.sqlite'
    context = bridge.prepare_context(path, [{'source': 'explicit', 'event_id': 'decision-1',
        'payload': {'text': '业务键为订单号。'},
        'facts': {'key_columns': ['订单号']} if facts is None else facts}], 's')
    draft = {'capability': 'tabular.deduplicate', 'constraints': [],
        'execution': {'input_path': 'input.csv', 'output_path': 'out.csv', 'key_columns': ['订单号']},
        'requirements': [
            {'text': '去重', 'quote': '去重', 'fields': ['/capability', '/constraints']},
            {'text': '读取文件', 'quote': 'input.csv', 'fields': ['/execution/input_path']},
            {'text': '另存文件', 'quote': 'out.csv', 'fields': ['/execution/output_path']},
            {'text': '附件业务键', 'quote': '订单号', 'fields': ['/execution/key_columns'],
             'source_ref': copy.deepcopy(context['source_refs'][0])}], 'unresolved': []}
    return path, context, draft


def propose(tmp_path, path, context, draft):
    from rpe_harness.proposal import create_proposal
    return create_proposal(RAW, draft, db_path=path, workspace=tmp_path, session_id='s',
                           source_ref='current:user', review_context=context)


def test_source_quote_is_bound_and_reviewed_without_full_history_or_raw_rewrite(tmp_path):
    from rpe_harness.intent import MinimalStateStore
    from rpe_harness.proposal import ProposalStore
    path, context, draft = setup(tmp_path)
    proposal = propose(tmp_path, path, context, draft)
    assert proposal['status'] == 'awaiting_confirmation', proposal
    assert proposal['source']['raw'] == RAW and proposal['draft'] == draft
    assert proposal['requirements'][-1]['origin'] == 'context_source'
    assert proposal['field_provenance']['/execution/key_columns'] == context['source_refs']
    assert 'context_source' in proposal['summary'] and '订单号' in proposal['summary']
    assert 'payload' not in json.dumps(MinimalStateStore(path).show(proposal['minimal_state_id'], session_id='s'))
    confirmation = ProposalStore(path).confirm(proposal['proposal_id'], session_id='s',
        expected_proposal_sha256=proposal['proposal_sha256'], actor='human')
    assert confirmation['status'] == 'confirmed' and not (tmp_path / 'out.csv').exists()


def test_verified_source_proposal_executes_and_replays_one_durable_result(tmp_path):
    from rpe_harness.proposal import ProposalStore
    path, context, draft = setup(tmp_path)
    proposal = propose(tmp_path, path, context, draft)
    store = ProposalStore(path)
    confirmation = store.confirm(proposal['proposal_id'], session_id='s',
        expected_proposal_sha256=proposal['proposal_sha256'], actor='human')
    result = store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path)
    assert result['status'] == 'succeeded', result
    assert (tmp_path / 'out.csv').read_text(encoding='utf-8-sig') == '订单号,值\na,1\nb,3\n'
    assert store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path) == result
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['unknown_ref', 'quote', 'field', 'conflict', 'session', 'extra'])
def test_untrusted_or_conflicting_context_drafts_remain_nonconfirmable(tmp_path, fault):
    from rpe_harness.proposal import ProposalStore
    path, context, draft = setup(tmp_path)
    if fault == 'unknown_ref':
        draft['requirements'][-1]['source_ref']['sha256'] = '0' * 64
    elif fault == 'quote':
        draft['requirements'][-1]['quote'] = '另存'
    elif fault == 'field':
        draft['requirements'][-1]['fields'] = ['/execution/output_path', '/execution/key_columns']
    elif fault == 'conflict':
        draft['execution']['key_columns'] = ['值']
    elif fault == 'session':
        context['session_id'] = 'other'
    else:
        draft['requirements'][-1]['facts'] = {'key_columns': ['订单号']}
    proposal = propose(tmp_path, path, context, draft)
    assert proposal['status'] != 'awaiting_confirmation'
    if 'proposal_id' in proposal:
        with pytest.raises(ValueError):
            ProposalStore(path).confirm(proposal['proposal_id'], session_id='s',
                expected_proposal_sha256=proposal['proposal_sha256'], actor='human')


def test_missing_business_key_is_not_filled_from_prose_or_repaired_in_draft(tmp_path):
    path, context, draft = setup(tmp_path, facts={})
    draft['execution'].pop('key_columns')
    draft['requirements'].pop()
    proposal = propose(tmp_path, path, context, draft)
    assert proposal['status'] == 'needs_context'
    assert 'execution.key_columns' in proposal['missing']
    assert 'key_columns' not in proposal['draft']['execution']


def test_explicit_source_does_not_repair_a_model_omission(tmp_path):
    path, context, draft = setup(tmp_path)
    draft['execution'].pop('key_columns')
    draft['requirements'].pop()
    proposal = propose(tmp_path, path, context, draft)
    assert proposal['status'] == 'needs_context'
    assert 'key_columns' not in proposal['draft']['execution']


def test_source_constraint_conflict_is_not_silently_dropped(tmp_path):
    path, context, draft = setup(tmp_path, facts={'key_columns': ['订单号'], 'constraints': ['preserve_order']})
    proposal = propose(tmp_path, path, context, draft)
    assert proposal['status'] == 'needs_context'
    assert 'constraints' in str(proposal['errors'])


@pytest.mark.parametrize('current', [None, ','])
def test_explicit_optional_source_fact_cannot_be_omitted_or_overridden(tmp_path, current):
    path, context, draft = setup(tmp_path, facts={'key_columns': ['订单号'], 'delimiter': '\t'})
    # Facts may be explicitly supplied by the host even when the prose has no
    # literal spelling for them. The projection must not invent a quote.
    assert 'delimiter' not in context['model_sources'][0]['quotes']
    if current is not None:
        draft['execution']['delimiter'] = current
        draft['requirements'][0]['fields'].append('/execution/delimiter')
    proposal = propose(tmp_path, path, context, draft)
    assert proposal['status'] == 'needs_context'
    assert 'execution.delimiter' in str(proposal['errors'])


def test_source_constraint_membership_does_not_depend_on_flag_order(tmp_path):
    from rpe_harness.review_context import prepare_context
    path, _, draft = setup(tmp_path)
    context = prepare_context(path, [{'source': 'flags', 'event_id': 'flags',
        'payload': {'text': '保留原文件和顺序'},
        'facts': {'constraints': ['preserve_original', 'preserve_order']}}], 's')
    draft['constraints'] = ['preserve_order', 'preserve_original']
    draft['requirements'][0]['fields'] = ['/capability']
    draft['requirements'].append({'text': '保留原文件及顺序', 'quote': '原文件和顺序',
        'source_ref': context['source_refs'][0], 'fields': ['/constraints']})
    draft['requirements'][-2].pop('source_ref')
    draft['requirements'][-2]['quote'] = None
    proposal = propose(tmp_path, path, context, draft)
    assert proposal['status'] == 'awaiting_confirmation', proposal


def test_discovery_only_selected_skill_cannot_authorize_a_registered_capability(tmp_path):
    from rpe_harness.proposal import create_proposal
    from rpe_harness.skill_catalog import SkillCatalog
    from rpe_harness.integrations.review_skills import selection_for
    path, context, draft = setup(tmp_path)
    skill_root = tmp_path / 'skills'
    skill = skill_root / 'notes'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: notes\ndescription: tabular helper\n---\nRead prose only.')
    catalog_root = tmp_path / 'catalog'
    catalog = SkillCatalog(path, catalog_root)
    snapshot = catalog.sync(skill_root)['snapshot_id']
    selection = selection_for(catalog.lookup('notes', snapshot))
    proposal = create_proposal(RAW, draft, db_path=path, workspace=tmp_path, session_id='s',
        source_ref='current:user', review_context=context, catalog_root=catalog_root,
        skill_snapshot_id=snapshot, skill_selection=selection)
    assert proposal['status'] == 'unsupported'
    assert proposal['minimal_state_id'] is None
    assert not (tmp_path / 'out.csv').exists()


def test_invalid_execution_with_context_capability_citation_is_a_blocked_draft(tmp_path):
    path, context, draft = setup(tmp_path)
    draft['execution'] = []
    draft['requirements'] = [{'text': '不能引用附件授予能力', 'quote': '订单号',
        'source_ref': context['source_refs'][0], 'fields': ['/capability']}]
    proposal = propose(tmp_path, path, context, draft)
    assert proposal['status'] == 'needs_context'


def test_two_explicit_sources_disagreeing_on_key_cannot_be_confirmed(tmp_path):
    from rpe_harness.review_context import prepare_context
    path, _, draft = setup(tmp_path)
    context = prepare_context(path, [
        {'source': 'explicit', 'event_id': 'first', 'payload': {'text': '业务键为订单号'},
         'facts': {'key_columns': ['订单号']}},
        {'source': 'explicit', 'event_id': 'second', 'payload': {'text': '业务键为值'},
         'facts': {'key_columns': ['值']}}], 's')
    draft['requirements'][-1]['source_ref'] = context['source_refs'][0]
    proposal = propose(tmp_path, path, context, draft)
    assert proposal['status'] == 'needs_context'
    assert proposal['context_recovery']['status'] == 'conflict'
    assert 'key_columns' in proposal['context_recovery']['conflicts']


@pytest.mark.parametrize('stage', ['confirm', 'resolve', 'execute'])
def test_selected_canonical_source_is_rechecked_when_using_proposal(tmp_path, stage):
    from rpe_harness.proposal import create_proposal, ProposalStore
    from rpe_harness.skill_catalog import SkillCatalog
    from rpe_harness.integrations.review_skills import selection_for
    path, context, draft = setup(tmp_path)
    source = tmp_path / 'source-skills'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx',
                    source / 'productivity/xlsx')
    catalog_root = tmp_path / 'catalog'
    catalog = SkillCatalog(path, catalog_root)
    snapshot = catalog.sync(source)['snapshot_id']
    catalog.trust_adapter('productivity/xlsx', snapshot)
    selection = selection_for(catalog.lookup('productivity/xlsx', snapshot))
    proposal = create_proposal(RAW, draft, db_path=path, workspace=tmp_path, session_id='s',
        source_ref='current:user', review_context=context, catalog_root=catalog_root,
        skill_snapshot_id=snapshot, skill_selection=selection)
    assert proposal['status'] == 'awaiting_confirmation', proposal
    assert proposal['skill_selection']['source_path'] == str(source / 'productivity/xlsx/SKILL.md')
    store = ProposalStore(path)
    confirmation = None
    if stage != 'confirm':
        confirmation = store.confirm(proposal['proposal_id'], session_id='s',
            expected_proposal_sha256=proposal['proposal_sha256'], actor='human')
    (source / 'productivity/xlsx/SKILL.md').write_text('changed canonical skill')
    with pytest.raises(ValueError):
        if stage == 'confirm':
            store.confirm(proposal['proposal_id'], session_id='s',
                expected_proposal_sha256=proposal['proposal_sha256'], actor='human')
        elif stage == 'resolve':
            # Passing the original catalog hash must not skip canonical source
            # checks merely because the input itself is a built-in CSV task.
            store.resolve(confirmation['confirmation_id'], session_id='s', catalog_sha256=proposal['catalog_sha256'])
        else:
            store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path)
    assert not (tmp_path / 'out.csv').exists()


@pytest.mark.parametrize('stage', ['confirm', 'resolve', 'execute'])
@pytest.mark.parametrize('fault', ['delete', 'hash', 'session', 'future'])
def test_source_is_revalidated_before_accepting_or_using_authority(tmp_path, stage, fault):
    from rpe_harness.proposal import ProposalStore
    path, context, draft = setup(tmp_path)
    proposal = propose(tmp_path, path, context, draft)
    assert proposal['status'] == 'awaiting_confirmation', proposal
    store = ProposalStore(path)
    confirmation = None
    if stage != 'confirm':
        confirmation = store.confirm(proposal['proposal_id'], session_id='s',
            expected_proposal_sha256=proposal['proposal_sha256'], actor='human')
    with sqlite3.connect(path) as db:
        if fault == 'delete':
            db.execute('DELETE FROM context_sources')
        elif fault == 'hash':
            db.execute('UPDATE context_sources SET payload_json=?', ('{"text":"replacement"}',))
        elif fault == 'session':
            db.execute("UPDATE context_sources SET session_id='other'")
        else:
            # Even consistently rehashed future sources must not become fresh evidence.
            from rpe_harness.context import _digest, _json
            row = db.execute('SELECT source,event_id,session_id,payload_json,facts_json FROM context_sources').fetchone()
            content = {'source': row[0], 'event_id': row[1], 'session_id': row[2],
                'payload': json.loads(row[3]), 'facts': json.loads(row[4]), 'received_at': 1e20}
            db.execute('UPDATE context_sources SET received_at=?,payload_hash=?',
                (1e20, _digest(_json(content, 262144))))
    with pytest.raises(ValueError):
        if stage == 'confirm':
            store.confirm(proposal['proposal_id'], session_id='s',
                expected_proposal_sha256=proposal['proposal_sha256'], actor='human')
        elif stage == 'resolve':
            store.resolve(confirmation['confirmation_id'], session_id='s', catalog_sha256=proposal['catalog_sha256'])
        else:
            store.execute(confirmation['confirmation_id'], session_id='s', workspace=tmp_path)
    assert not (tmp_path / 'out.csv').exists()
