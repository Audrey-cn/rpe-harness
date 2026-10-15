"""The audit launcher is exercised through local executable processes only."""
import hashlib
import importlib
import json
import stat
import sys
import time
from pathlib import Path

import pytest

_BUNDLE = {'cases': [{'evidence_id': 'case-1'}]}


@pytest.fixture
def audit(tmp_path, monkeypatch):
    try:
        module = importlib.import_module('rpe_harness.experiments.agent_audit')
    except ModuleNotFoundError:
        pytest.fail('independent audit launcher is not implemented')
    settings = tmp_path/'settings.json'
    settings.write_text(json.dumps({'env': {
        'ANTHROPIC_AUTH_TOKEN': 'test-auth-private-value',
        'ANTHROPIC_BASE_URL': 'https://test.invalid',
    }}))
    monkeypatch.setattr(module, '_SETTINGS_PATH', settings)
    owner = tmp_path/'owner'
    owner.mkdir()
    return module, owner


def cli(tmp_path, body):
    executable = tmp_path/'fake-claude'
    executable.write_text('#!'+sys.executable+'\n'+body)
    executable.chmod(0o700)
    return executable


def result(**changes):
    value = {'type': 'result', 'subtype': 'success', 'is_error': False,
        'structured_output': {'cases': [{'evidence_id': 'case-1',
            'faithful': True, 'artifact_correct': None, 'reason': 'source matches'}],
            'unsupported_claims': []},
        'usage': {'input_tokens': 73, 'output_tokens': 19},
        'modelUsage': {'mimo-v2.6-flash': {'inputTokens': 73, 'outputTokens': 19}},
        'num_turns': 1, 'total_cost_usd': 0.001}
    value.update(changes)
    return value


def test_fake_cli_receives_isolated_request_and_actual_receipt(audit, tmp_path):
    module, owner = audit
    executable = cli(tmp_path, """import json, os, pathlib, sys
payload = sys.stdin.read()
pathlib.Path('received.json').write_text(json.dumps({
 'args':sys.argv[1:], 'env':{k:os.environ.get(k) for k in
 ['CLAUDE_CONFIG_DIR','XDG_CACHE_HOME','TMPDIR','CLAUDE_CODE_MAX_OUTPUT_TOKENS']},
 'payload':payload, 'cwd':os.getcwd()}))
print("""+repr(json.dumps(result()))+")\n")
    record = module.run_agent_audit({'cases': [{'evidence_id': 'case-1'}]},
                                    owner, executable=executable)
    attempt = owner/'agent-audit'
    seen = json.loads((attempt/'received.json').read_text())
    args = seen['args']
    for flag in ('-p', '--safe-mode', '--restricted', '--strict-mcp-config',
                 '--no-session-persistence', '--disable-slash-commands'):
        assert flag in args
    assert args[args.index('--tools')+1] == ''
    assert args[args.index('--setting-sources')+1] == ''
    assert args[args.index('--settings')+1] == str(module._SETTINGS_PATH)
    assert json.loads(args[args.index('--mcp-config')+1]) == {'mcpServers': {}}
    assert args[args.index('--max-turns')+1] == '1'
    for key in ('CLAUDE_CONFIG_DIR', 'XDG_CACHE_HOME', 'TMPDIR'):
        assert Path(seen['env'][key]).is_relative_to(attempt)
        assert Path(seen['env'][key]).is_dir()
    assert seen['env']['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] == '1200'
    assert json.loads(seen['payload'])['bundle']['cases'][0]['evidence_id'] == 'case-1'
    assert record['status'] == 'completed'
    assert record['verdict']['cases'][0]['artifact_correct'] is None
    assert record['usage'] == {'input_tokens': 73, 'output_tokens': 19}
    assert record['modelUsage']['mimo-v2.6-flash']['outputTokens'] == 19
    assert record['num_turns'] == 1
    assert record['cli_estimated_cost_usd'] == 0.001
    assert record['verified_bill_usd'] is None
    assert record['input_sha256'] == hashlib.sha256((attempt/'input.json').read_bytes()).hexdigest()
    assert record['stdout_sha256'] == hashlib.sha256((attempt/'raw.stdout').read_bytes()).hexdigest()
    assert record == json.loads((attempt/'record.json').read_text())
    for path in (attempt, attempt/'raw.stdout', attempt/'raw.stderr', attempt/'input.json'):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_failed_attempt_keeps_usage_redacts_public_data_and_cannot_retry(audit, tmp_path, monkeypatch):
    module, owner = audit
    monkeypatch.setenv('SOME_API_TOKEN', 'environment-private-value')
    response = result(subtype='error_during_execution', is_error=True,
        structured_output={'cases': [{'evidence_id': 'case-1', 'faithful': False,
            'artifact_correct': False, 'reason': 'test-auth-private-value environment-private-value https://u:pw@test.invalid/a?token=url-private-value'}],
            'unsupported_claims': ['Bearer other-private-value']})
    executable = cli(tmp_path, "import pathlib,sys\npathlib.Path('invoked').write_text('once')\nprint("+
        repr(json.dumps(response))+")\nprint('test-auth-private-value',file=sys.stderr)\nsys.exit(3)\n")
    record = module.run_agent_audit(_BUNDLE, owner, executable=executable)
    assert record['status'] == 'failed'
    assert record['exit_code'] == 3
    assert record['cli_estimated_cost_usd'] == 0.001
    public = json.dumps(record)
    for value in ('test-auth-private-value', 'environment-private-value',
                  'url-private-value', 'other-private-value', 'u:pw@'):
        assert value not in public
    assert 'test-auth-private-value' in (owner/'agent-audit'/'raw.stdout').read_text()
    with pytest.raises(FileExistsError):
        module.run_agent_audit(_BUNDLE, owner, executable=executable)
    assert (owner/'agent-audit'/'invoked').read_text() == 'once'


def test_successful_verdict_and_model_receipt_cannot_publish_secrets(audit, tmp_path, monkeypatch):
    module, owner = audit
    monkeypatch.setenv('SOME_API_TOKEN', 'environment-private-value')
    response = result(structured_output={'cases': [{'evidence_id': 'case-1',
        'faithful': True, 'artifact_correct': False,
        'reason': 'test-auth-private-value environment-private-value '
            'https://u:pw@test.invalid/a?api_key=url-private-value'}],
        'unsupported_claims': ['Bearer other-private-value']},
        modelUsage={'test-auth-private-value': {'outputTokens': 19}})
    executable = cli(tmp_path, 'print('+repr(json.dumps(response))+')\n')
    record = module.run_agent_audit(_BUNDLE, owner, executable=executable)
    assert record['status'] == 'completed'
    public = json.dumps(record)
    for secret in ('test-auth-private-value', 'environment-private-value',
                   'url-private-value', 'other-private-value', 'u:pw@'):
        assert secret not in public
    assert record['verdict']['cases'][0]['artifact_correct'] is False


def test_settings_cannot_override_the_owned_runtime_directories(audit, tmp_path):
    module, owner = audit
    module._SETTINGS_PATH.write_text(json.dumps({'env': {
        'CLAUDE_CONFIG_DIR': str(tmp_path/'daily-state'),
    }}))
    with pytest.raises(ValueError):
        module.run_agent_audit(_BUNDLE, owner,
                              executable=tmp_path/'not-installed')
    assert not (owner/'agent-audit').exists()


def test_timeout_kills_the_owned_process_group(audit, tmp_path):
    module, owner = audit
    executable = cli(tmp_path, """import pathlib,signal,subprocess,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
child = subprocess.Popen([sys.executable,'-c',
 "import pathlib,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(1.2);pathlib.Path('leaked-child').write_text('leak')"])
pathlib.Path('child.pid').write_text(str(child.pid))
while True: time.sleep(1)
""")
    started = time.monotonic()
    record = module.run_agent_audit(_BUNDLE, owner, timeout=1,
                                    executable=executable)
    assert record['status'] == 'timed_out'
    assert record['error_code'] == 'timeout'
    assert time.monotonic()-started < 3
    assert record['usage'] is None
    assert (owner/'agent-audit'/'child.pid').is_file()
    time.sleep(0.9)
    assert not (owner/'agent-audit'/'leaked-child').exists()


@pytest.mark.parametrize('output', ['not JSON', json.dumps(result(structured_output=None)),
    json.dumps(result(structured_output={'cases': [{'evidence_id': 'case-1',
        'faithful': 'yes', 'artifact_correct': None, 'reason': 'bad type'}],
        'unsupported_claims': []}))])
def test_invalid_verdict_is_never_accepted(audit, tmp_path, output):
    module, owner = audit
    executable = cli(tmp_path, 'print('+repr(output)+')\n')
    record = module.run_agent_audit(_BUNDLE, owner, executable=executable)
    assert record['status'] == 'invalid_output'
    assert record['verdict'] is None


def test_unavailable_executable_still_consumes_the_claim(audit, tmp_path):
    module, owner = audit
    record = module.run_agent_audit(_BUNDLE, owner,
                                    executable=tmp_path/'not-installed')
    assert record['status'] == 'failed'
    assert record['error_code'] == 'cli_unavailable'
    with pytest.raises(FileExistsError):
        module.run_agent_audit(_BUNDLE, owner, executable=tmp_path/'not-installed')


def test_oversized_input_and_symlink_owner_fail_before_launch(audit, tmp_path):
    module, owner = audit
    with pytest.raises(ValueError):
        module.run_agent_audit({**_BUNDLE, 'data': 'x'*(130*1024)}, owner,
                              executable=tmp_path/'not-installed')
    assert not (owner/'agent-audit').exists()
    alias = tmp_path/'alias'
    alias.symlink_to(owner, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        module.run_agent_audit(_BUNDLE, alias,
                              executable=tmp_path/'not-installed')
    assert not (owner/'agent-audit').exists()


@pytest.mark.parametrize('ids', [['case-1'], ['case-1', 'case-2', 'extra'],
    ['case-1', 'case-1'], ['wrong-1', 'wrong-2']])
def test_incomplete_or_wrong_evidence_population_retains_receipt(audit, tmp_path, ids):
    module, owner = audit
    response = result(structured_output={'cases': [{'evidence_id': evidence_id,
        'faithful': True, 'artifact_correct': None, 'reason': 'claimed match'}
        for evidence_id in ids], 'unsupported_claims': []})
    executable = cli(tmp_path, 'print('+repr(json.dumps(response))+')\n')
    record = module.run_agent_audit({'cases': [{'evidence_id': 'case-1'},
        {'evidence_id': 'case-2'}]}, owner, executable=executable)
    assert record['status'] == 'invalid_output'
    assert record['verdict'] is None
    assert record['cli_estimated_cost_usd'] == 0.001
    assert record['usage']['input_tokens'] == 73
