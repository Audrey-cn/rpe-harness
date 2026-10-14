"""Post-formal regressions: the real registered snapshot is also needed on rejection."""
import json
from pathlib import Path
import shutil
import sqlite3
import sys

import pytest

from rpe_harness.capability_catalog import build_catalog
from rpe_harness.integrations import hermes, hermes_address
from rpe_harness.integrations.hermes_broker import execution_broker
from rpe_harness.skill_catalog import SkillCatalog
from test_hermes_address import SDK, dispatch, extracted, final_result, gate


@pytest.fixture
def registered(tmp_path, monkeypatch, request):
    case = getattr(request, 'param', 'unsupported')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'orders.csv').write_text('id,value\na,1\na,2\nb,3\n')
    db = tmp_path / 'trial.sqlite'
    catalog_root = tmp_path / 'catalog'
    source = tmp_path / 'skills-source'
    fixture = Path(sys.modules['test_hermes_address'].__file__).parent / 'fixtures/skills/productivity/xlsx'
    shutil.copytree(fixture, source / 'productivity/xlsx')
    skills = SkillCatalog(db, catalog_root)
    current = skills.sync(source)['snapshot_id']
    skills.trust_adapter('productivity/xlsx', snapshot_id=current)
    selection = None if case == 'implicit' else current
    catalog = build_catalog(db, catalog_root, selection)
    store = hermes.HostEventStore(db)
    extension = hermes_address.address_extension(db, workspace, catalog_root=catalog_root,
        snapshot_id=selection, source_ref='registered-public-prompt',
        source_facts={'execution': {'key_columns': ['value']}} if case == 'conflict' else None)
    config = tmp_path / 'address.json'
    config.write_text(json.dumps({'catalog_sha256': catalog.snapshot_id, 'entries': catalog.entries()}))
    monkeypatch.setenv('RPE_HERMES_ADDRESS_CONFIG', str(config))
    monkeypatch.setenv('RPE_HERMES_ENTRY_KEY', 'registered-entry')
    monkeypatch.delenv('RPE_HERMES_REQUIRED_REQUEST_PATH', raising=False)
    monkeypatch.delenv('RPE_HERMES_INPUT_SHA256', raising=False)
    sdk = SDK()
    hermes_address.register_address_workflow(sdk, store=store, workspace=workspace,
        python=sys.executable, error_path=tmp_path / 'observer-errors')
    with execution_broker(db_path=db, workspace=workspace, python=sys.executable, extension=extension) as broker:
        for key, value in broker.items():
            monkeypatch.setenv(key, value)
        yield {'sdk': sdk, 'store': store, 'db': db, 'workspace': workspace, 'api': hermes_address,
            'config': config, 'catalog_root': catalog_root, 'counter': 0, 'session': 'registered-session',
            'snapshot_id': selection, 'skills': skills, 'source': source, 'case': case}
    assert broker.cleanup['cleanup_complete'] is True


def understand_rejection(env):
    args = extracted()
    if env['case'] == 'needs_context':
        del args['execution']['key_columns']
    elif env['case'] == 'rejected':
        args['unknown'] = 'must not be ignored'
    elif env['case'] != 'conflict':
        args['constraints'] = ['preserve_original', '未知必需限制']
    return dispatch(env, 'rpe_understand', args)


@pytest.mark.parametrize('registered', ['unsupported', 'needs_context', 'conflict', 'rejected', 'implicit'], indirect=True)
def test_registered_catalog_rejection_keeps_exact_initial_snapshot_selection(registered):
    env = registered
    result = understand_rejection(env)
    expected = 'unsupported' if env['case'] == 'implicit' else env['case']
    assert result['status'] == expected
    with sqlite3.connect(env['db']) as db:
        state = json.loads(db.execute('SELECT state_json FROM rpe_skill_workflows').fetchone()[0])
    assert 'snapshot_id' in state
    assert state['snapshot_id'] == env['snapshot_id']
    assert state['catalog_root'] == str(env['catalog_root'])
    if expected == 'rejected':
        # Invalid extraction may be corrected at most three times.
        understand_rejection(env)
        understand_rejection(env)
    verdict = gate(env)
    assert verdict['status'] == 'accepted_rejection', verdict
    assert verdict['rejection_verified'] is True
    assert verdict['rejection_reason'] == expected
    assert verdict['harness_verified'] is False
    assert not (env['workspace'] / 'out.csv').exists()
    assert not any(event['kind'] == 'rpe_task_received' for event in env['store'].list())


@pytest.mark.parametrize('mutation', ['pointer', 'trust'])
def test_rejection_catalog_anchor_does_not_keep_revoked_authority(registered, mutation):
    env = registered
    assert understand_rejection(env)['status'] == 'unsupported'
    host_result = final_result(env)
    before = env['api'].evaluate_address_session(host_result, env['db'], env['workspace'], catalog_root=env['catalog_root'])
    assert before['status'] == 'accepted_rejection', before
    if mutation == 'trust':
        with sqlite3.connect(env['db']) as db:
            db.execute('DELETE FROM rpe_skill_trust')
    else:
        (env['source'] / 'unrelated').mkdir()
        (env['source'] / 'unrelated/SKILL.md').write_text('---\nname: unrelated\ndescription: added\n---\nAdded metadata\n')
        env['skills'].sync(env['source'])
    after = env['api'].evaluate_address_session(host_result, env['db'], env['workspace'], catalog_root=env['catalog_root'])
    assert after['status'] == 'unverified'
    assert after['errors'] == ['capability_catalog_changed']
    assert not after.get('rejection_verified', False)


def test_understanding_schema_enumerates_only_trusted_catalog_contract_flags(registered):
    env = registered
    params = env['sdk'].tools['rpe_understand']['schema']['parameters']['properties']
    assert params['constraints']['items']['enum'] == ['preserve_formulas', 'preserve_order', 'preserve_original']
    assert 'enum' not in params['capability']  # Unknown requested capabilities must still reach unsupported.
    value = dispatch(env, 'rpe_understand', extracted(capability='pdf.summarize'))
    assert value['status'] == 'unsupported'
    assert not (env['workspace'] / 'out.csv').exists()


def test_union_flag_still_requires_capability_specific_parent_authorization(registered):
    env = registered
    (env['workspace'] / 'raw.json').write_text('{"x":1}')
    value = dispatch(env, 'rpe_understand', extracted(capability='json.normalize',
        constraints=['preserve_formulas'], execution={
            'input_path': 'raw.json', 'output_path': 'out.json', 'style': 'pretty'}))
    assert value['status'] == 'unsupported'
    verdict = gate(env)
    assert verdict['rejection_verified'] is True, verdict
    assert not (env['workspace'] / 'out.json').exists()
