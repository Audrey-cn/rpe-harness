import json

import pytest

from rpe_harness.cli import main


def test_registry_validation_cli_fails_closed_without_bundle_or_default_db(tmp_path, capsys, monkeypatch):
    state = tmp_path / 'default-state'
    monkeypatch.setenv('RPE_STATE_DIR', str(state))
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'schema_version': 'rpe.trust-policy/v1', 'trusted_keys': []}))
    try:
        code = main(['assets', 'registry-validate', '--bundle', str(tmp_path / 'missing'),
                     '--keyring', str(policy)])
    except SystemExit:
        pytest.fail('registry validation CLI is not registered')
    result = json.loads(capsys.readouterr().out)
    assert code != 0 and result['passed'] is False
    assert result['published'] is False and result['activated'] is False
    assert not state.exists()
