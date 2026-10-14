import json

from rpe_harness.cli import main
from test_asset_lifecycle import next_task, package, signed_asset
from test_asset_trust import signing_key, trusted_identity


def invoke(capsys, argv):
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_assets_cli_inspect_import_show_list_and_invalid_hash(tmp_path, capsys):
    root = package(tmp_path / 'package')
    state = tmp_path / 'state'
    db = state / 'runs.sqlite'
    code, result = invoke(capsys, ['assets', 'inspect', '--package', str(root)])
    assert code == 0 and result['compatible'] is True and result['executable'] is False
    code, asset = invoke(capsys, ['assets', 'import', '--package', str(root),
        '--db', str(db), '--owned-root', str(state)])
    assert code == 0 and asset['state'] == 'unverified'
    code, shown = invoke(capsys, ['assets', 'show', asset['asset_id'], '--db', str(db)])
    assert code == 0 and shown['local_validation'] == 'unverified'
    code, listing = invoke(capsys, ['assets', 'list', '--db', str(db)])
    assert code == 0 and listing['assets'][0]['asset_id'] == asset['asset_id']
    (root / 'candidate.py').write_text('changed')
    code, result = invoke(capsys, ['assets', 'inspect', '--package', str(root)])
    assert code == 2 and result['status'] == 'error'


def test_assets_cli_adoption_uses_evolution_and_exports_signed_package(tmp_path, capsys, trusted_identity):
    db, run, asset, keyring = signed_asset(tmp_path, trusted_identity)
    code, proposal = invoke(capsys, ['assets', 'propose', asset['asset_id'],
        '--db', str(db), '--keyring', str(keyring), '--evidence-run-id', run['run_id']])
    assert code == 0 and proposal['state'] == 'proposed'
    code, checked = invoke(capsys, ['evolution', 'validate', proposal['proposal_id'], '--db', str(db)])
    assert code == 0 and checked['passed'] is True
    code, active = invoke(capsys, ['evolution', 'apply', proposal['proposal_id'], '--db', str(db)])
    assert code == 0 and active['applied'] is True
    assert next_task(tmp_path, db)['artifact']['actual_executor']['candidate_version_id'] == active['version_id']
    identity = tmp_path / 'identity.json'
    identity.write_text(json.dumps(trusted_identity))
    code, exported = invoke(capsys, ['assets', 'export', active['version_id'], '--db', str(db),
        '--destination', str(tmp_path / 'export'), '--identity', str(identity)])
    assert code == 0 and exported['signed'] is True
