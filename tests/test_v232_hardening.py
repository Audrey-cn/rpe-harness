"""Regressions for every v2.3.2 Round-2 audit finding and Windows LF fixtures."""

import hashlib
import http.client
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from rpe_harness import __version__, contract_sdk, dev_contracts, inbox, json_execution
from rpe_harness.contract_sdk import DEFAULT_REGISTRY, ContractPlugin
from rpe_harness.evolution import sandbox
from rpe_harness.host_api import HostAPI, TrustedHostAction
from rpe_harness.mcp_server import MAX_STDIN_LINE_BYTES, StdioMCPServer
from rpe_harness.recovery import recover_run
from rpe_harness.review_context import (
    prepare_context,
    recover_context,
    validate_context,
    validate_sources,
)
from rpe_harness.runtime import HarnessRuntime
from rpe_harness.server import HarnessHandler


def _v232_start_json(api, workspace, output, owner="alice"):
    source = workspace / "input.json"
    if not source.exists():
        source.write_bytes(b'{ "x": 1 }')
    execution = {"input_path": "input.json", "output_path": output, "style": "minify"}
    draft = {
        "capability": "json.normalize", "constraints": [], "execution": execution,
        "requirements": [{
            "text": "Review explicit contract", "quote": "Review",
            "fields": ["/capability", "/constraints", *("/execution/" + k for k in execution)],
        }],
        "unresolved": [],
    }
    result = api.start_task({"task": "Review", "draft": draft}, {
        "owner": owner, "session_id": "v232-session", "workspace": str(workspace),
    })
    assert result["status"] == "awaiting_confirmation", result
    return result


def _v232_confirm_json(api, operation):
    receipt = api.confirm_task(operation["operation_id"], operation["digest"], TrustedHostAction(
        operation["owner"], operation["operation_id"], operation["generation"], operation["digest"],
    ))
    assert receipt["status"] == "confirmed", receipt
    proposal = api.proposals.show(operation["proposal_id"], session_id="v232-session")
    request = api.proposals.resolve(receipt["confirmation_id"], session_id="v232-session",
                                    catalog_sha256=proposal["catalog_sha256"])["request"]
    request["event_id"] = receipt["event_id"]
    return request


def _v232_failed_json(workspace, monkeypatch):
    api = HostAPI(workspace / "runs.sqlite")
    operation = _v232_start_json(api, workspace, "failed.json")
    request = _v232_confirm_json(api, operation)
    runtime = HarnessRuntime(db_path=api.db_path)

    def fail(**kwargs):
        raise RuntimeError("recoverable v232 execution failure")

    with monkeypatch.context() as patch:
        patch.setattr(json_execution, "prepare_json_normalization", fail)
        failed = runtime.run(request)
    assert failed["status"] == "failed", failed
    return api, operation, runtime, failed


@pytest.mark.parametrize("facts", [
    {"query": "SELECT 1", "max_rows": 10},
    {"operations": [{"op": "set", "key": "MODE", "value": "dev"}]},
    {"column": "kind", "operator": "equals", "value": ""},
    {"column": "kind", "operator": "in", "values": ["a", "b"]},
])
def test_v232_registered_builtin_plugin_option_facts_roundtrip(tmp_path, facts):
    source = [{"source": "explicit", "event_id": "options", "payload": {
        "text": "Review SELECT 1 and kind with a and b in dev mode.",
    }, "facts": facts}]
    bundle = prepare_context(tmp_path / "context.sqlite", source, "session")
    assert bundle["model_sources"][0]["facts"] == facts


def test_v232_custom_plugin_option_facts_follow_live_registration(tmp_path):
    plugin = ContractPlugin(
        capability="acme.v232-options", executor_id="acme/v232-options", input_formats=("txt",),
        validate_options=lambda options, raw: dict(options),
        execute=lambda plan, raw: (raw, {}),
        verify=lambda plan, raw, staged: {"passed": staged == raw},
        option_keys=("custom_switch",),
    )
    source = [{"source": "explicit", "event_id": "custom", "payload": {
        "text": "Use input.txt and out.txt with the custom switch.",
    }, "facts": {"input_path": "input.txt", "output_path": "out.txt", "custom_switch": True}}]
    with pytest.raises(ValueError, match="unknown|unauthorized"):
        validate_sources(source)
    DEFAULT_REGISTRY.register(plugin)
    try:
        path = tmp_path / "context.sqlite"
        bundle = prepare_context(path, source, "session")
        execution = dict(source[0]["facts"])
        assert recover_context(path, bundle, "session", plugin.capability, execution, [])["status"] == "ready"
        execution["custom_switch"] = False
        with pytest.raises(ValueError, match="execution.custom_switch"):
            recover_context(path, bundle, "session", plugin.capability, execution, [])
        source[0]["facts"]["arbitrary_authority"] = "forbidden"
        with pytest.raises(ValueError, match="unknown|unauthorized"):
            validate_sources(source)
    finally:
        DEFAULT_REGISTRY.unregister(plugin.capability)
    source[0]["facts"].pop("arbitrary_authority", None)
    with pytest.raises(ValueError, match="unknown|unauthorized"):
        validate_sources(source)


@pytest.mark.parametrize("fact,draft", [
    (True, 1),
    ([{"op": "set", "path": "flag", "value": True}],
     [{"op": "set", "path": "flag", "value": 1}]),
    ({"threshold": 1}, {"threshold": 1.0}),
])
def test_v232_plugin_context_option_agreement_is_type_strict(tmp_path, fact, draft):
    plugin = ContractPlugin(
        capability="acme.v232-strict", executor_id="acme/v232-strict", input_formats=("txt",),
        validate_options=lambda options, raw: dict(options), execute=lambda plan, raw: (raw, {}),
        verify=lambda plan, raw, staged: {"passed": staged == raw}, option_keys=("strict_value",),
    )
    DEFAULT_REGISTRY.register(plugin)
    try:
        facts = {"input_path": "input.txt", "output_path": "out.txt", "strict_value": fact}
        path = tmp_path / "context.sqlite"
        bundle = prepare_context(path, [{"source": "explicit", "event_id": "typed", "payload": {
            "text": "Review input.txt and out.txt using the supplied option.",
        }, "facts": facts}], "session")
        assert recover_context(path, bundle, "session", plugin.capability, facts, [])["status"] == "ready"
        with pytest.raises(ValueError, match="execution.strict_value"):
            recover_context(path, bundle, "session", plugin.capability,
                            {**facts, "strict_value": draft}, [])
        tampered = json.loads(json.dumps(bundle))
        tampered["model_sources"][0]["facts"]["strict_value"] = draft
        with pytest.raises(ValueError, match="model projection differs"):
            validate_context(path, tampered, "session")
    finally:
        DEFAULT_REGISTRY.unregister(plugin.capability)


@pytest.mark.parametrize("owner", [None, "alice"])
def test_v232_recent_filters_pending_before_limit(tmp_path, monkeypatch, owner):
    api, failed_op, runtime, _ = _v232_failed_json(tmp_path, monkeypatch)
    confirmed = _v232_start_json(api, tmp_path, "confirmed.json")
    _v232_confirm_json(api, confirmed)
    # A host can stop after persisting the receipt but before saving its link.
    with api._connect() as db:
        db.execute("UPDATE rpe_operations SET confirmation_id=NULL WHERE operation_id=?",
                   (confirmed["operation_id"],))
    succeeded = _v232_start_json(api, tmp_path, "succeeded.json")
    result = runtime.run(_v232_confirm_json(api, succeeded))
    assert result["status"] == "succeeded", result
    for index in range(55):
        _v232_start_json(api, tmp_path, f"pending-{index}.json")
    other = _v232_start_json(api, tmp_path, "other.json", owner="bob")
    _v232_confirm_json(api, other)
    recent = inbox.list_recent(api, owner=owner)
    states = {item["operation_id"]: item["status"] for item in recent}
    assert states[failed_op["operation_id"]] == "failed"
    assert states[confirmed["operation_id"]] == "confirmed"
    assert states[succeeded["operation_id"]] == "succeeded"
    assert "awaiting_confirmation" not in states.values()
    assert (other["operation_id"] in states) is (owner is None)


def test_v232_recent_limits_nonpending_results_to_fifty(tmp_path):
    api = HostAPI(tmp_path / "runs.sqlite")
    operations = [_v232_start_json(api, tmp_path, f"cancel-{index}.json") for index in range(52)]
    for operation in operations:
        assert api.cancel_task(operation["operation_id"], operation["generation"], owner="alice")["status"] == "cancelled"
    recent = inbox.list_recent(api, owner="alice")
    assert [row["operation_id"] for row in recent] == [row["operation_id"] for row in reversed(operations[-50:])]


@pytest.mark.parametrize("tamper", [False, True])
def test_v232_pre_admission_retry_reservation_is_recoverable(tmp_path, monkeypatch, tamper):
    api, operation, runtime, failed = _v232_failed_json(tmp_path, monkeypatch)

    def before_admission(*args, **kwargs):
        raise RuntimeError("owner exited before retry admission")

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "run", before_admission)
        with pytest.raises(RuntimeError, match="before retry admission"):
            recover_run(runtime, failed["run_id"])
    if tamper:
        with runtime.store.connect() as db:
            db.execute("UPDATE recovery_attempts SET plan_sha256=?", ("0" * 64,))
    view = HostAPI(api.db_path).reconcile_task(operation["operation_id"], "alice")
    assert view["allowed_recovery_actions"] == ([] if tamper else ["recover_run"])
    if tamper:
        with pytest.raises(ValueError, match="reserved retry"):
            recover_run(runtime, failed["run_id"])
    else:
        result = recover_run(runtime, failed["run_id"])
        assert result["status"] == "succeeded", result
        assert api.reconcile_task(operation["operation_id"], "alice")["allowed_recovery_actions"] == []


def test_v232_windows_skill_fixture_is_written_as_lf(tmp_path, monkeypatch):
    import shutil

    from test_v231_hardening import test_v231_mcp_trusted_catalog_activates_xlsx_inspect

    from rpe_harness.skill_catalog import SkillCatalog

    copytree = shutil.copytree
    trust = SkillCatalog.trust_adapter

    def crlf_checkout(*args, **kwargs):
        result = copytree(*args, **kwargs)
        for path in Path(result).rglob("*"):
            if path.is_file():
                path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        return result

    def check_lf(catalog, *args, **kwargs):
        for path in (tmp_path / "skills").rglob("*"):
            if path.is_file():
                assert b"\r\n" not in path.read_bytes(), path
        return trust(catalog, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", crlf_checkout)
    monkeypatch.setattr(SkillCatalog, "trust_adapter", check_lf)
    test_v231_mcp_trusted_catalog_activates_xlsx_inspect(tmp_path)


def test_v232_versions_and_three_os_ci_gates():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    assert __version__ == "2.3.2"
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == __version__
    assert json.loads((root / "integrations/dsh/package.json").read_text())["version"] == __version__
    ci = (root / ".github/workflows/ci.yml").read_text()
    for job in ("linux-core", "macos-core", "windows-core"):
        section = ci.split("  " + job + ":", 1)[1].split("\n  windows-core:", 1)[0].split("\n  macos-core:", 1)[0]
        assert "tests/test_v232_hardening.py" in section
    assert ci.count("tests/test_v232_hardening.py") == 3


def _v232_sandbox_mock_child(monkeypatch, mutate, *, exit_code=0, after_close=None):
    child_packet = {}

    class V232SandboxProcess:
        _handle = 123
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            self.returncode = exit_code
            return exit_code

    class V232SandboxKernel:
        def AssignProcessToJobObject(self, *args):
            return True

        def TerminateJobObject(self, *args):
            return True

        def CloseHandle(self, *args):
            if after_close is not None:
                after_close(child_packet)
            return True

    def spawn(argv, **kwargs):
        child_packet.update(json.load(kwargs['stdin']))
        mutate(child_packet)
        return V232SandboxProcess()

    monkeypatch.setattr(sandbox, 'sandbox_backend', lambda: 'windows-job-object')
    monkeypatch.setattr(sandbox, '_win32_create_job', lambda _: (V232SandboxKernel(), 321))
    monkeypatch.setattr(sandbox, '_win32_resume_process', lambda _: None)
    monkeypatch.setattr(sandbox.subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, raising=False)
    monkeypatch.setattr(sandbox.subprocess, 'Popen', spawn)


@pytest.mark.parametrize('target', ['input', 'staging'])
@pytest.mark.parametrize('replacement', ['fifo', 'zero_symlink', 'directory', 'oversize'])
def test_v232_sandbox_rejects_unsafe_postflight_files(tmp_path, monkeypatch, target, replacement):
    from rpe_harness import path_binding

    if replacement == 'fifo' and not hasattr(os, 'mkfifo'):
        pytest.skip('FIFOs require POSIX')
    if replacement == 'zero_symlink' and not Path('/dev/zero').exists():
        pytest.skip('/dev/zero symlink replacement requires POSIX')
    source = tmp_path / 'source.json'
    source.write_bytes(b'{}')
    plan = {'capability': 'json.normalize', 'input_format': 'json',
            'input_path': str(source), 'input_sha256': hashlib.sha256(b'{}').hexdigest()}
    monkeypatch.setattr(sandbox, 'MAX_OUTPUT_BYTES', 16)

    def mutate(packet):
        stage = Path(packet['staging_path'])
        stage.write_bytes(b'{}')
        changed = Path(packet['plan']['input_path']) if target == 'input' else stage
        changed.chmod(0o600)
        changed.unlink()
        if replacement == 'fifo':
            os.mkfifo(changed)
        elif replacement == 'zero_symlink':
            changed.symlink_to('/dev/zero')
        elif replacement == 'directory':
            changed.mkdir()
        else:
            changed.write_bytes(b'x' * 32)

    _v232_sandbox_mock_child(monkeypatch, mutate)
    raw_read = Path.read_bytes
    regular_read = path_binding.read_regular_file
    reading_regular = False

    def guard_regular_read(path, *, max_bytes):
        nonlocal reading_regular
        reading_regular = True
        try:
            return regular_read(path, max_bytes=max_bytes)
        finally:
            reading_regular = False

    def guard_raw_read(path):
        # An unsafe implementation must fail promptly instead of hanging this test
        # on the FIFO or allocating indefinitely while following /dev/zero.
        if (not reading_regular and path.name in {'input.json', 'staging.json'}
                and 'evolution-sandboxes' in path.parts):
            pytest.fail('postflight must use bounded nofollow regular-file reads')
        return raw_read(path)

    monkeypatch.setattr(path_binding, 'read_regular_file', guard_regular_read)
    monkeypatch.setattr(Path, 'read_bytes', guard_raw_read)
    with pytest.raises(sandbox.CandidateExecutionError):
        sandbox.run_candidate(b'def prepare(plan, staging_path): pass', plan, tmp_path)


def test_v232_sandbox_postflight_reads_are_bounded(tmp_path, monkeypatch):
    from rpe_harness import path_binding

    source = tmp_path / 'source.json'
    source.write_bytes(b'{}')
    plan = {'capability': 'json.normalize', 'input_format': 'json',
            'input_path': str(source), 'input_sha256': hashlib.sha256(b'{}').hexdigest()}
    _v232_sandbox_mock_child(
        monkeypatch, lambda packet: Path(packet['staging_path']).write_bytes(b'{"ok":true}'))
    original = path_binding.read_regular_file
    reads = []

    def read(path, *, max_bytes):
        reads.append((Path(path).name, max_bytes))
        return original(path, max_bytes=max_bytes)

    monkeypatch.setattr(path_binding, 'read_regular_file', read)
    assert sandbox.run_candidate(b'def prepare(plan, staging_path): pass', plan, tmp_path) == b'{"ok":true}'
    assert ('input.json', sandbox.JSON_INPUT_BYTES) in reads
    assert ('staging.json', sandbox.MAX_OUTPUT_BYTES) in reads


@pytest.mark.parametrize('target', ['input', 'staging'])
@pytest.mark.parametrize('replacement', ['fifo', 'zero_symlink', 'oversize'])
def test_v232_sandbox_rejects_replacement_between_check_and_read(
        tmp_path, monkeypatch, target, replacement):
    from rpe_harness import path_binding

    if replacement == 'fifo' and not hasattr(os, 'mkfifo'):
        pytest.skip('FIFOs require POSIX')
    if replacement == 'zero_symlink' and not Path('/dev/zero').exists():
        pytest.skip('/dev/zero symlink replacement requires POSIX')
    source = tmp_path / 'source.json'
    source.write_bytes(b'{}')
    plan = {'capability': 'json.normalize', 'input_format': 'json',
            'input_path': str(source), 'input_sha256': hashlib.sha256(b'{}').hexdigest()}
    monkeypatch.setattr(sandbox, 'JSON_INPUT_BYTES', 16)
    monkeypatch.setattr(sandbox, 'MAX_OUTPUT_BYTES', 16)
    _v232_sandbox_mock_child(
        monkeypatch, lambda packet: Path(packet['staging_path']).write_bytes(b'{}'))
    regular_read = path_binding.read_regular_file
    attempted = []

    def replace_before_read(path, *, max_bytes):
        changed = Path(path)
        if changed.name == target + '.json':
            attempted.append(changed.name)
            changed.chmod(0o600)
            changed.unlink()
            if replacement == 'fifo':
                os.mkfifo(changed)
            elif replacement == 'zero_symlink':
                changed.symlink_to('/dev/zero')
            else:
                changed.write_bytes(b'x' * 32)
        return regular_read(path, max_bytes=max_bytes)

    monkeypatch.setattr(path_binding, 'read_regular_file', replace_before_read)
    with pytest.raises(sandbox.CandidateExecutionError):
        sandbox.run_candidate(b'def prepare(plan, staging_path): pass', plan, tmp_path)
    assert attempted == [target + '.json']


@pytest.mark.parametrize('pid_namespace', [True, False])
def test_v232_bwrap_declares_pid_namespace_mode_and_ro_input(tmp_path, monkeypatch, pid_namespace):
    monkeypatch.setattr(shutil, 'which', lambda _: '/usr/bin/bwrap')
    monkeypatch.setattr(sandbox, 'sandbox_backend', lambda: 'linux-bwrap')
    monkeypatch.setattr(sandbox, '_bwrap_pid_namespace_supported', lambda _: pid_namespace)
    copied = tmp_path / 'input.json'
    copied.write_bytes(b'{}')
    command = sandbox._bwrap_command(tmp_path, tmp_path / 'driver.py', tmp_path / 'candidate.py', copied)
    assert ('--unshare-pid' in command) is pid_namespace
    job_mount = command.index('--bind')
    input_mount = next(i for i, arg in enumerate(command[:-2])
                       if arg == '--ro-bind' and command[i + 1:i + 3] == [str(copied)] * 2)
    assert input_mount > job_mount
    capabilities = sandbox.sandbox_capabilities()
    assert capabilities['pid_namespace'] is pid_namespace
    assert capabilities['candidate_execution_available'] is pid_namespace
    assert capabilities['pid_namespace_mode'] == (
        'unshare_pid' if pid_namespace else 'fallback_no_pid_namespace')
    if not pid_namespace:
        assert 'escape killpg' in capabilities['process_tree_limitation']
        assert 'setsid' in capabilities['candidate_execution_unavailable_reason']


def test_v232_bwrap_without_pid_namespace_refuses_before_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda _: '/usr/bin/bwrap')
    monkeypatch.setattr(sandbox, 'sandbox_backend', lambda: 'linux-bwrap')
    monkeypatch.setattr(sandbox, '_bwrap_pid_namespace_supported', lambda _: False)

    def spawn(*args, **kwargs):
        pytest.fail('untrusted candidate must not start without PID-namespace containment')

    monkeypatch.setattr(sandbox.subprocess, 'Popen', spawn)
    with pytest.raises(sandbox.CandidateExecutionError, match='PID namespace unavailable'):
        sandbox.run_candidate(b'def prepare(plan, staging_path): pass', {}, tmp_path)


@pytest.mark.parametrize('replacement', ['fifo', 'zero_symlink', 'directory', 'oversize'])
def test_v232_sandbox_rejects_unsafe_failure_diagnostics(tmp_path, monkeypatch, replacement):
    from rpe_harness import path_binding

    if replacement == 'fifo' and not hasattr(os, 'mkfifo'):
        pytest.skip('FIFOs require POSIX')
    if replacement == 'zero_symlink' and not Path('/dev/zero').exists():
        pytest.skip('/dev/zero symlink replacement requires POSIX')
    source = tmp_path / 'source.json'
    source.write_bytes(b'{}')
    plan = {'capability': 'json.normalize', 'input_format': 'json',
            'input_path': str(source), 'input_sha256': hashlib.sha256(b'{}').hexdigest()}
    monkeypatch.setattr(sandbox, 'MAX_OUTPUT_BYTES', 16)

    def replace_log(packet):
        # Run after the parent's inherited stdout handle closes so this mocked
        # postflight state is portable to Windows as well as POSIX.
        changed = Path(packet['plan']['input_path']).parent / 'child.log'
        changed.unlink()
        if replacement == 'fifo':
            os.mkfifo(changed)
        elif replacement == 'zero_symlink':
            changed.symlink_to('/dev/zero')
        elif replacement == 'directory':
            changed.mkdir()
        else:
            changed.write_bytes(b'x' * 32)

    _v232_sandbox_mock_child(
        monkeypatch, lambda _: None, exit_code=1, after_close=replace_log)
    original_open = Path.open
    original_read = path_binding.read_regular_file
    reading_regular = False
    log_reads = []

    def guarded_read(path, *, max_bytes):
        nonlocal reading_regular
        reading_regular = True
        if Path(path).name == 'child.log':
            log_reads.append(max_bytes)
        try:
            return original_read(path, max_bytes=max_bytes)
        finally:
            reading_regular = False

    def guarded_open(path, mode='r', *args, **kwargs):
        if path.name == 'child.log' and mode == 'rb' and not reading_regular:
            pytest.fail('failure diagnostics must use bounded nofollow regular-file reads')
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(path_binding, 'read_regular_file', guarded_read)
    monkeypatch.setattr(Path, 'open', guarded_open)
    with pytest.raises(sandbox.CandidateExecutionError, match='diagnostic'):
        sandbox.run_candidate(b'def prepare(plan, staging_path): pass', plan, tmp_path)
    assert log_reads == [sandbox.MAX_OUTPUT_BYTES]


def test_v232_sandbox_bounded_failure_diagnostic_keeps_tail(tmp_path, monkeypatch):
    source = tmp_path / 'source.json'
    source.write_bytes(b'{}')
    plan = {'capability': 'json.normalize', 'input_format': 'json',
            'input_path': str(source), 'input_sha256': hashlib.sha256(b'{}').hexdigest()}

    def write_log(packet):
        changed = Path(packet['plan']['input_path']).parent / 'child.log'
        changed.write_bytes(b'ignored prefix' * 1000 + b'\nRPE_CANDIDATE_REJECTED:'
                            + b'{"type":"ValueError","message":"bounded rejection"}\n')

    _v232_sandbox_mock_child(
        monkeypatch, lambda _: None, exit_code=65, after_close=write_log)
    with pytest.raises(sandbox.CandidateRejected, match='bounded rejection'):
        sandbox.run_candidate(b'def prepare(plan, staging_path): pass', plan, tmp_path)


class _V232HTTPTransport:
    def __init__(self, incoming):
        self.incoming = incoming
        self.outgoing = bytearray()

    def makefile(self, mode, buffering=-1):
        assert mode == 'rb'
        return io.BytesIO(self.incoming)

    def sendall(self, data):
        self.outgoing.extend(data)

    def settimeout(self, timeout):
        pass

    def shutdown(self, direction):
        pass


def _v232_http_request(method='GET', headers='', *, host='127.0.0.1:18400'):
    recorded = []
    runtime = SimpleNamespace(stats=lambda: {'ok': True}, record_usage=lambda *args: recorded.append(args))
    server = SimpleNamespace(runtime=runtime, request_timeout=2, server_port=18400)
    body = b'{"skill_id":"builtin/test","success":true}' if method == 'POST' else b''
    path = '/usage' if method == 'POST' else '/stats'
    fields = (f'Host: {host}\r\n' if host is not None else '') + headers
    if method == 'POST':
        fields += f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n'
    request = f'{method} {path} HTTP/1.1\r\n{fields}\r\n'.encode('ascii') + body
    transport = _V232HTTPTransport(request)
    HarnessHandler(transport, ('127.0.0.1', 12345), server)
    response = http.client.HTTPResponse(_V232HTTPTransport(bytes(transport.outgoing)))
    response.begin()
    result = response.status, dict(response.getheaders()), json.loads(response.read()), recorded
    response.close()
    return result


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize('host', [None, 'evil.example:18400', '127.0.0.1:18401',
                                 '127.0.0.1', 'evil.example@127.0.0.1:18400',
                                 '127.0.0.1:18400/evil', '127.0.0.1:18400,evil.example',
                                 '[::1]evil:18400', '[::1]:18400/'])
def test_v232_http_rejects_rebinding_hosts_before_dispatch(method, host):
    status, _, _, recorded = _v232_http_request(method, host=host)
    assert status == 403
    assert recorded == []


@pytest.mark.parametrize('headers', [
    'Host: evil.example:18400\r\n',
    'Origin: http://evil.example:18400\r\n',
    'Origin: http://127.0.0.1:18401\r\n',
    'Origin: http://localhost:18400\r\n',
    'Origin: https://127.0.0.1:18400\r\n',
    'Origin: null\r\n',
    'Origin: http://127.0.0.1:18400#\r\n',
    'Origin: http://[::1]evil:18400\r\n',
    'Origin: http://127.0.0.1:18400\r\nOrigin: http://evil.example:18400\r\n',
    'Sec-Fetch-Site: cross-site\r\n',
    'Sec-Fetch-Site: same-site\r\n',
    'Sec-Fetch-Site: same-origin\r\nSec-Fetch-Site: cross-site\r\n',
])
def test_v232_http_post_requires_same_origin(headers):
    status, _, _, recorded = _v232_http_request('POST', headers)
    assert status == 403
    assert recorded == []


@pytest.mark.parametrize('host', ['127.0.0.1:18400', 'localhost:18400', '[::1]:18400'])
@pytest.mark.parametrize('fetch_site', [None, 'same-origin', 'none'])
def test_v232_http_accepts_loopback_same_origin_and_native_requests(host, fetch_site):
    headers = f'Origin: http://{host}\r\n'
    if fetch_site is not None:
        headers += f'Sec-Fetch-Site: {fetch_site}\r\n'
    status, _, _, recorded = _v232_http_request('POST', headers, host=host)
    assert status == 200
    assert recorded == [('builtin/test', True)]
    assert _v232_http_request('POST', host=host)[0] == 200


@pytest.mark.parametrize('host,expected', [('127.0.0.1:18400', 200), ('evil.example:18400', 403)])
def test_v232_main_http_security_headers_on_success_and_errors(host, expected):
    status, headers, _, _ = _v232_http_request(host=host)
    assert status == expected
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert headers['X-Frame-Options'] == 'DENY'
    assert headers['Content-Security-Policy'] == "frame-ancestors 'none'"


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_v232_httpconnection_generated_host_remains_compatible(method):
    # Exercise the real client's Host generation without requiring socket.bind
    # permissions; the generated wire request still goes through the handler.
    wire = _V232HTTPTransport(b'')
    connection = http.client.HTTPConnection('127.0.0.1', 18400)
    connection.sock = wire
    payload = b'{"skill_id":"builtin/test","success":true}'
    connection.request(method, '/usage' if method == 'POST' else '/stats',
                       body=payload if method == 'POST' else None,
                       headers={'Content-Type': 'application/json'} if method == 'POST' else {})
    recorded = []
    runtime = SimpleNamespace(stats=lambda: {'ok': True},
                              record_usage=lambda *args: recorded.append(args))
    transport = _V232HTTPTransport(bytes(wire.outgoing))
    HarnessHandler(transport, ('127.0.0.1', 12345), SimpleNamespace(
        runtime=runtime, request_timeout=2, server_port=18400))
    response = http.client.HTTPResponse(_V232HTTPTransport(bytes(transport.outgoing)))
    response.begin()
    assert response.status == 200, response.read()
    assert recorded == ([('builtin/test', True)] if method == 'POST' else [])
    response.close()


@pytest.mark.parametrize('length,expected', [('nope', 400), ('-1', 400), ('1048577', 413)])
def test_v232_valid_host_preserves_http_body_admission_guards(length, expected):
    wire = (f'POST /route HTTP/1.0\r\nHost: 127.0.0.1:18400\r\n'
            f'Content-Length: {length}\r\n\r\n').encode()
    transport = _V232HTTPTransport(wire)
    HarnessHandler(transport, ('127.0.0.1', 12345), SimpleNamespace(
        runtime=None, request_timeout=2, server_port=18400))
    response = http.client.HTTPResponse(_V232HTTPTransport(bytes(transport.outgoing)))
    response.begin()
    assert response.status == expected, response.read()
    response.close()


@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('bad_line', [b'\xff\n', b'{"text":"\xc0\xaf"}\n',
                                      b'x' * (MAX_STDIN_LINE_BYTES + 20) + b'\xff\n'])
def test_v232_mcp_invalid_utf8_and_oversize_records_recover(tmp_path, wrapped, bad_line):
    payload = bad_line + b'{"jsonrpc":"2.0","id":232,"method":"ping"}\n'
    source = io.BytesIO(payload)
    if wrapped:
        source = io.TextIOWrapper(source, encoding='utf-8', errors='strict')
    output = io.StringIO()
    server = StdioMCPServer(db_path=tmp_path / 'mcp.sqlite')
    assert server.serve(source, output) == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]['id'] is None
    assert responses[0]['error']['code'] == -32700
    assert responses[1] == {'jsonrpc': '2.0', 'id': 232, 'result': {}}
    assert len(responses) == 2


def test_v232_mcp_prefers_bounded_binary_buffer(tmp_path):
    class BoundedBuffer(io.BytesIO):
        def readline(self, size=-1):
            assert 0 < size <= MAX_STDIN_LINE_BYTES + 1
            return super().readline(size)

    class BinaryStdin:
        buffer = BoundedBuffer(b'\xff\n' + b'x' * (3 * MAX_STDIN_LINE_BYTES) + b'\n'
                               b'{"jsonrpc":"2.0","id":233,"method":"ping"}\n')

        def readline(self, size=-1):
            pytest.fail('stdin text decoder must not process untrusted bytes')

    output = io.StringIO()
    assert StdioMCPServer(db_path=tmp_path / 'bounded.sqlite').serve(BinaryStdin(), output) == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [row.get('error', {}).get('code') for row in responses] == [-32700, -32700, None]
    assert responses[-1]['id'] == 233


def test_v232_mcp_cli_invalid_utf8_stdin_recovers(tmp_path):
    process = subprocess.run(
        [sys.executable, '-m', 'rpe_harness', 'mcp', '--db', str(tmp_path / 'cli.sqlite')],
        input=b'\xff\n{"jsonrpc":"2.0","id":234,"method":"ping"}\n',
        capture_output=True, timeout=10, check=False,
    )
    assert process.returncode == 0, process.stderr.decode('utf-8', errors='replace')
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert responses[0]['error']['code'] == -32700
    assert responses[1] == {'jsonrpc': '2.0', 'id': 234, 'result': {}}
    assert len(responses) == 2


def _v232_dev_plan(plugin, source, raw, options):
    return contract_sdk.PluginExecutionPlan(
        capability=plugin.capability,
        contract_version=plugin.contract_version,
        executor_id=plugin.executor_id,
        executor_revision=plugin.executor_revision,
        verifier_revision=plugin.verifier_revision,
        input_path=str(source),
        output_path=str(source.parent / "v232-dev-result.csv"),
        input_format=source.suffix[1:],
        input_sha256=hashlib.sha256(raw).hexdigest(),
        options_json=json.dumps(plugin.validate_options(options, raw), sort_keys=True),
        constraints=(),
        goal="Verify bounded developer contracts",
    )


def _v232_dev_database(source):
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.execute("INSERT INTO items VALUES ('safe')")
    return source.read_bytes()


def test_v232_sqlite_progress_handler_interrupts_recursive_aggregate(tmp_path, monkeypatch):
    raw = _v232_dev_database(tmp_path / "input.sqlite")
    monkeypatch.setattr(dev_contracts, "_SQLITE_MAX_VM_STEPS", 10000, raising=False)
    started = time.monotonic()
    with pytest.raises(ValueError, match="execution (deadline|budget)"):
        dev_contracts._run_readonly_query(
            raw,
            "WITH RECURSIVE numbers(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM numbers "
            "WHERE x < 1000000000) SELECT sum(x) FROM numbers",
            1,
        )
    assert time.monotonic() - started < 5
    assert dev_contracts._run_readonly_query(raw, "SELECT value FROM items", 1) == (
        ["value"], [["safe"]]
    )


def test_v232_sqlite_progress_handler_enforces_monotonic_deadline(tmp_path, monkeypatch):
    raw = _v232_dev_database(tmp_path / "input.sqlite")
    ticks = iter([0.0, 6.0])
    monkeypatch.setattr(dev_contracts.time, "monotonic", lambda: next(ticks, 6.0))
    with pytest.raises(ValueError, match="execution (deadline|budget)"):
        dev_contracts._run_readonly_query(
            raw,
            "WITH RECURSIVE numbers(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM numbers "
            "WHERE x < 1000000000) SELECT sum(x) FROM numbers",
            1,
        )


@pytest.mark.parametrize("original", [b"KEY=only\n", b"KEY=only"])
def test_v232_env_delete_last_key_is_empty_and_independently_verified(tmp_path, original):
    plan = _v232_dev_plan(
        dev_contracts.ENV_PATCH_PLUGIN, tmp_path / "input.env", original,
        {"operations": [{"op": "delete", "key": "KEY"}]},
    )
    output, _ = dev_contracts._env_execute(plan, original)
    assert output == b""
    assert dev_contracts._env_verify(plan, original, b"")["passed"]
    assert not dev_contracts._env_verify(plan, original, b"\n")["passed"]


@pytest.mark.parametrize(
    "original",
    [b"KEY=don't # annotation\n", b'KEY=an"unclosed # annotation\n',
     b"KEY=plain'quoted'\t# annotation\n"],
)
def test_v232_env_unquoted_embedded_quote_preserves_inline_comment(tmp_path, original):
    plan = _v232_dev_plan(
        dev_contracts.ENV_PATCH_PLUGIN, tmp_path / "input.env", original,
        {"operations": [{"op": "set", "key": "KEY", "value": "new"}]},
    )
    whitespace = b"\t" if b"\t#" in original else b" "
    expected = b"KEY=new" + whitespace + b"# annotation\n"
    output, _ = dev_contracts._env_execute(plan, original)
    assert output == expected
    assert dev_contracts._env_verify(plan, original, expected)["passed"]
    assert not dev_contracts._env_verify(plan, original, b"KEY=new\n")["passed"]


@pytest.mark.parametrize("first_module", ["dev_contracts", "contract_sdk"])
def test_v232_developer_contract_import_order_in_fresh_interpreter(first_module):
    source_root = str(Path(dev_contracts.__file__).resolve().parents[1])
    process = subprocess.run(
        [sys.executable, "-c", f"from rpe_harness import {first_module}; "
         "from rpe_harness import dev_contracts, contract_sdk; "
         "assert contract_sdk.DEFAULT_REGISTRY.get('env.patch') is dev_contracts.ENV_PATCH_PLUGIN; "
         "assert contract_sdk.DEFAULT_REGISTRY.get('sqlite.query_export') "
         "is dev_contracts.SQLITE_EXPORT_PLUGIN"],
        env={**os.environ, "PYTHONPATH": source_root},
        capture_output=True, text=True, timeout=10,
    )
    assert process.returncode == 0, process.stderr


@pytest.mark.parametrize(
    "query",
    ["SELECT 1 AS [delete]", "SELECT 1 AS [a;b]", "SELECT 1 AS `update`",
     "SELECT 1 AS `a;b`", "SELECT 1 AS `a``delete;b`",
     "SELECT 1 AS [/*delete;*/]", "SELECT 1 AS `--update;`"],
)
def test_v232_sqlite_masks_bracket_and_backtick_identifiers(query):
    admitted = dev_contracts._validate_readonly_sql(query)
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute(admitted).fetchall() == [(1,)]


@pytest.mark.parametrize(
    "query",
    ["SELECT 1 AS [safe]; DELETE FROM items", "SELECT 1 AS `safe`; SELECT 2",
     "SELECT 1; [hidden]", "SELECT 1; `hidden`", "SELECT 1 AS [unterminated",
     "SELECT 1 AS `unterminated", "UPDATE [delete] SET [a;b]=1",
     "WITH [safe] AS (SELECT 1) DELETE FROM items"],
)
def test_v232_sqlite_identifier_masker_still_blocks_writes_and_statements(query):
    with pytest.raises(ValueError):
        dev_contracts._validate_readonly_sql(query)


@pytest.mark.parametrize("keys", [("query", "query"), ("",), ("bad key",), ["query"], (1,)])
def test_v232_plugin_option_key_declarations_reject_invalid_keys(keys):
    with pytest.raises(ValueError, match="option_keys"):
        contract_sdk.ContractPlugin(
            capability="acme.v232-options", executor_id="acme/v232-options",
            input_formats=("txt",), validate_options=lambda opts, raw: opts,
            execute=lambda plan, raw: (raw, {}),
            verify=lambda plan, raw, staged: {"passed": raw == staged},
            option_keys=keys,
        )


def test_v232_builtin_contract_options_are_declared_in_metadata():
    expected = {
        "tabular.filter": {"column", "operator", "value", "values", "encoding", "delimiter"},
        "json.patch": {"operations", "indent"},
        "env.patch": {"operations"},
        "sqlite.query_export": {"query", "max_rows", "delimiter"},
    }
    for capability, keys in expected.items():
        plugin = contract_sdk.DEFAULT_REGISTRY.get(capability)
        assert set(plugin.option_keys) == keys
        assert set(plugin.metadata()["option_keys"]) == keys
