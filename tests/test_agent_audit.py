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


@pytest.mark.parametrize('profile', ['legacy', 'structured-v2'])
def test_timeout_kills_the_owned_process_group(audit, tmp_path, profile):
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
                                    executable=executable, profile=profile)
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


def test_legacy_audit_spec_preserves_historical_contract_and_claim(audit, tmp_path):
    module, owner = audit
    spec = module.audit_spec(_BUNDLE)
    assert spec['profile'] == 'legacy'
    assert hashlib.sha256(spec['system'].encode()).hexdigest() == (
        'b8cbb63f06f1a4cb70cf49dcfebe476ce4684155bbad14afcee09531880ab1c4')
    assert hashlib.sha256(module._json(spec['schema'])).hexdigest() == (
        'a6f2898f3073a38ecd3e3be48c0c3d45f802bb90baf8137b23169dd57ca5a37d')
    executable = cli(tmp_path, 'print('+repr(json.dumps(result()))+')\n')
    record = module.run_agent_audit(_BUNDLE, owner, executable=executable)
    claim = json.loads((owner/'agent-audit'/'claim.json').read_text())
    assert set(claim) == {'schema_version', 'input_sha256', 'claimed_at',
        'timeout_seconds', 'max_turns', 'max_output_tokens', 'cli_max_budget_usd',
        'requested_model', 'host', 'schema_sha256'}
    assert claim['max_turns'] == 1
    assert claim['max_output_tokens'] == 1200
    assert claim['cli_max_budget_usd'] == 0.25
    assert claim['timeout_seconds'] == 180
    assert record['input_sha256'] == (
        '418300d74259477b777ef3fe8c025d4cd50623470c44523c30093d00c9ec8b21')


def test_structured_profile_transports_and_claims_the_frozen_spec(audit, tmp_path):
    module, owner = audit
    bundle = {'cases': [{'evidence_id': 'A02'}, {'evidence_id': 'A01'}]}
    response = result(structured_output={'cases': [
        {'evidence_id': 'A01', 'faithful': True, 'artifact_correct': True,
         'reason': 'Actual rows match requested filtering'},
        {'evidence_id': 'A02', 'faithful': False, 'artifact_correct': None,
         'reason': 'unknown; missing candidate evidence'}], 'unsupported_claims': []})
    executable = cli(tmp_path, """import json, os, pathlib, sys
pathlib.Path('received.json').write_text(json.dumps({
 'args':sys.argv[1:], 'env':{k:os.environ.get(k) for k in
 ['HOME','CLAUDE_CONFIG_DIR','XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_CACHE_HOME',
  'TMPDIR','CLAUDE_CODE_MAX_OUTPUT_TOKENS','CLAUDE_CODE_MAX_RETRIES',
  'CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES','MAX_STRUCTURED_OUTPUT_RETRIES']},
 'payload':sys.stdin.read()}))
print("""+repr(json.dumps(response))+")\n")
    spec = module.audit_spec(bundle, profile='structured-v2')
    record = module.run_agent_audit(bundle, owner, executable=executable,
                                    profile='structured-v2')
    attempt = owner/'agent-audit'
    seen = json.loads((attempt/'received.json').read_text())
    args = seen['args']
    assert args[args.index('--max-turns')+1] == '4'
    assert args[args.index('--max-budget-usd')+1] == '0.4'
    assert args[args.index('--system-prompt')+1] == spec['system']
    assert json.loads(args[args.index('--json-schema')+1]) == spec['schema']
    assert json.loads(seen['payload'])['audit_contract'] == spec['system']
    for key in ('HOME', 'CLAUDE_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
                'XDG_CACHE_HOME', 'TMPDIR'):
        assert Path(seen['env'][key]).is_relative_to(attempt)
        assert Path(seen['env'][key]).is_dir()
    assert {key: seen['env'][key] for key in (
        'CLAUDE_CODE_MAX_OUTPUT_TOKENS', 'CLAUDE_CODE_MAX_RETRIES',
        'CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES', 'MAX_STRUCTURED_OUTPUT_RETRIES')
        } == {'CLAUDE_CODE_MAX_OUTPUT_TOKENS': '2400', 'CLAUDE_CODE_MAX_RETRIES': '0',
              'CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES': '0',
              'MAX_STRUCTURED_OUTPUT_RETRIES': '2'}
    schema = spec['schema']['properties']
    assert schema['cases']['minItems'] == schema['cases']['maxItems'] == 2
    assert schema['cases']['items']['properties']['evidence_id']['enum'] == ['A01', 'A02']
    assert schema['cases']['items']['properties']['reason']['maxLength'] == 160
    assert schema['unsupported_claims']['maxItems'] == 3
    assert schema['unsupported_claims']['items']['maxLength'] == 240
    claim = json.loads((attempt/'claim.json').read_text())
    assert claim['profile'] == record['profile'] == 'structured-v2'
    for key in ('max_turns', 'max_output_tokens', 'timeout_seconds', 'cli_max_budget_usd'):
        assert claim[key] == spec[key]
    assert claim['retry_env'] == spec['retry_env']
    assert claim['schema_sha256'] == hashlib.sha256(module._json(spec['schema'])).hexdigest()
    assert record['status'] == 'completed'
    assert record['verdict'] == response['structured_output']


def test_audit_specs_do_not_share_mutable_schemas(audit):
    module, _ = audit
    for profile in ('legacy', 'structured-v2'):
        spec = module.audit_spec(_BUNDLE, profile=profile)
        spec['schema']['properties']['cases']['maxItems'] = 0
        assert module.audit_spec(_BUNDLE, profile=profile)['schema']['properties'][
            'cases']['maxItems'] == (32 if profile == 'legacy' else 1)


@pytest.mark.parametrize('profile', ['', 'structured-v3', None, []])
def test_unknown_audit_profile_is_rejected_before_claim(audit, tmp_path, profile):
    module, owner = audit
    with pytest.raises(ValueError):
        module.run_agent_audit(_BUNDLE, owner, profile=profile,
                              executable=tmp_path/'not-installed')
    assert not (owner/'agent-audit').exists()


@pytest.mark.parametrize('timeout', [0, 181, float('nan'), float('inf'), True])
def test_structured_timeout_is_rejected_before_claim(audit, tmp_path, timeout):
    module, owner = audit
    with pytest.raises(ValueError):
        module.run_agent_audit(_BUNDLE, owner, profile='structured-v2',
                              timeout=timeout, executable=tmp_path/'not-installed')
    assert not (owner/'agent-audit').exists()


@pytest.mark.parametrize('change', [
    {'reason': 'x'*161}, {'reason': ' '},
    {'reason': 'one two three four five six seven eight nine ten eleven twelve thirteen'},
    {'unsupported_claims': ['x']*4}, {'unsupported_claims': ['x'*241]},
    {'evidence_id': 'extra'},
])
def test_structured_verdict_constraints_are_enforced_independently(audit, tmp_path, change):
    module, owner = audit
    response = result()
    if 'unsupported_claims' in change:
        response['structured_output'].update(change)
    else:
        response['structured_output']['cases'][0].update(change)
    executable = cli(tmp_path, 'print('+repr(json.dumps(response))+')\n')
    record = module.run_agent_audit(_BUNDLE, owner, profile='structured-v2',
                                    executable=executable)
    assert record['status'] == 'invalid_output'
    assert record['verdict'] is None
    assert record['usage']['output_tokens'] == 19
    assert record['cli_estimated_cost_usd'] == 0.001


def test_structured_failure_retains_safe_terminal_and_consumes_claim(audit, tmp_path):
    module, owner = audit
    response = result(subtype='error_max_turns', is_error=True, structured_output=None,
        stop_reason='max_tokens', num_turns=5, duration_ms=26000,
        duration_api_ms=25000, errors=['test-auth-private-value Bearer private-value'],
        total_cost_usd=0.19)
    executable = cli(tmp_path, 'import sys\nprint('+repr(json.dumps(response))+')\nsys.exit(1)\n')
    record = module.run_agent_audit(_BUNDLE, owner, profile='structured-v2',
                                    executable=executable)
    assert record['status'] == 'failed'
    assert record['error_code'] == 'cli_failed'
    assert record['verdict'] is None
    assert record['usage']['output_tokens'] == 19
    assert record['num_turns'] == 5
    assert record['cli_estimated_cost_usd'] == 0.19
    terminal = record['terminal_observation']
    assert terminal == {'type': 'result', 'subtype': 'error_max_turns',
        'is_error': True, 'stop_reason': 'max_tokens', 'num_turns': 5,
        'duration_ms': 26000, 'duration_api_ms': 25000,
        'errors': ['[REDACTED] Bearer [REDACTED]']}
    with pytest.raises(FileExistsError):
        module.run_agent_audit(_BUNDLE, owner, profile='structured-v2',
                               executable=executable)


def test_structured_terminal_does_not_publish_untrusted_numeric_types(audit, tmp_path):
    module, owner = audit
    response = result(num_turns=True, duration_ms=-1, duration_api_ms='12',
        total_cost_usd=float('nan'), errors='not a list')
    executable = cli(tmp_path, 'print('+repr(json.dumps(response))+')\n')
    record = module.run_agent_audit(_BUNDLE, owner, profile='structured-v2',
                                    executable=executable)
    assert record['num_turns'] is None
    assert record['cli_estimated_cost_usd'] is None
    assert record['terminal_observation']['num_turns'] is None
    assert record['terminal_observation']['duration_ms'] is None
    assert record['terminal_observation']['duration_api_ms'] is None
    assert record['terminal_observation']['errors'] == []


def test_structured_terminal_redacts_full_secrets_before_diagnostic_truncation(audit, tmp_path):
    module, owner = audit
    secret = 'private-auth-fragment-'*150
    module._SETTINGS_PATH.write_text(json.dumps({'env': {'ANTHROPIC_AUTH_TOKEN': secret}}))
    response = result(subtype='error_during_execution', is_error=True,
        structured_output=None, errors=[secret+' diagnostic suffix'])
    executable = cli(tmp_path, 'print('+repr(json.dumps(response))+')\n')
    record = module.run_agent_audit(_BUNDLE, owner, profile='structured-v2',
                                    executable=executable)
    assert record['terminal_observation']['errors'] == ['[REDACTED] diagnostic suffix']
    assert 'private-auth-fragment-' not in json.dumps(record)
