"""Regressions for all 17 findings in the v2.3.0 red-team audit."""

import argparse
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from http.client import HTTPResponse
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest

from rpe_harness import __version__, contract_sdk, dev_contracts, inbox
from rpe_harness.capability_catalog import build_catalog
from rpe_harness.contract_sdk import (
    DEFAULT_REGISTRY,
    ContractPlugin,
    ContractRegistry,
    PluginExecutionPlan,
)
from rpe_harness.evolution import sandbox
from rpe_harness.host_api import HostAPI, TrustedHostAction
from rpe_harness.intent import interpret_intent
from rpe_harness.mcp_server import StdioMCPServer, dispatch_mcp, register_mcp
from rpe_harness.proposal import create_proposal
from rpe_harness.review_context import prepare_context, recover_context
from rpe_harness.runtime import HarnessRuntime

_V231_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "bad",
    [
        "{broken",
        "[" * 10000 + "0" + "]" * 10000,
        '{"number":' + "9" * 10000 + "}",
        " " * (1024 * 1024 + 1),
        '{"text":"' + "x" * (1024 * 1024) + '"}',
        '{"text":"' + "界" * (400 * 1024) + '"}',
    ],
    ids=["syntax", "nesting", "integer", "whitespace", "ascii-size", "utf8-size"],
)
def test_v231_mcp_malformed_or_oversized_input_recovers(tmp_path, bad):
    server = StdioMCPServer(db_path=tmp_path / "runs.sqlite", workspace=tmp_path)
    ping = json.dumps({"jsonrpc": "2.0", "id": 42, "method": "ping"})
    output = io.StringIO()
    assert server.serve(io.StringIO(bad + "\n" + ping + "\n"), output) == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == -32700
    assert responses[0]["id"] is None
    assert responses[1] == {"jsonrpc": "2.0", "id": 42, "result": {}}
    assert len(responses) == 2


def test_v231_mcp_reads_only_bounded_chunks(tmp_path):
    class BoundedInput(io.StringIO):
        def __iter__(self):
            pytest.fail("unbounded iteration over stdin")

        def readline(self, size=-1):
            assert 0 < size <= 1024 * 1024 + 1
            return super().readline(size)

    source = BoundedInput(
        "x" * (3 * 1024 * 1024) + "\n" + '{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
    )
    output = io.StringIO()
    StdioMCPServer(db_path=tmp_path / "runs.sqlite").serve(source, output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [r.get("error", {}).get("code") for r in responses] == [-32700, None]
    assert responses[1]["id"] == 7


def test_v231_mcp_lone_surrogate_does_not_crash_utf8_output(tmp_path):
    server = StdioMCPServer(db_path=tmp_path / "runs.sqlite")
    wire = io.BytesIO()
    output = io.TextIOWrapper(wire, encoding="utf-8")
    source = io.StringIO(
        '{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )
    assert server.serve(source, output) == 0
    responses = [json.loads(line) for line in wire.getvalue().decode("utf-8").splitlines()]
    assert responses[0]["id"] == "\ud800"
    assert responses[1]["id"] == 2


def test_v231_mcp_catalog_startup_flags(tmp_path, monkeypatch):
    parser = argparse.ArgumentParser()
    register_mcp(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "mcp",
            "--db",
            str(tmp_path / "runs.sqlite"),
            "--catalog-root",
            str(tmp_path / "catalog"),
            "--skill-snapshot-id",
            "a" * 64,
        ]
    )
    captured = {}

    def serve(server):
        captured.update(catalog_root=server.catalog_root, snapshot=server.skill_snapshot_id)
        return 0

    monkeypatch.setattr(StdioMCPServer, "serve", serve)
    assert dispatch_mcp(args) == 0
    assert Path(captured["catalog_root"]) == tmp_path / "catalog"
    assert captured["snapshot"] == "a" * 64


def test_v231_mcp_trusted_catalog_activates_xlsx_inspect(tmp_path):
    from openpyxl import Workbook

    from rpe_harness.skill_catalog import SkillCatalog

    source = tmp_path / "skills"
    shutil.copytree(
        _V231_ROOT / "tests/fixtures/skills/productivity/xlsx", source / "productivity/xlsx"
    )
    db = tmp_path / "runs.sqlite"
    catalog_root = tmp_path / "catalog"
    skills = SkillCatalog(db, catalog_root)
    snapshot = skills.sync(source)["snapshot_id"]
    skills.trust_adapter("productivity/xlsx", snapshot)
    book = Workbook()
    book.save(tmp_path / "input.xlsx")
    book.close()
    draft = {
        "capability": "xlsx.inspect",
        "constraints": ["preserve_original"],
        "execution": {"input_path": "input.xlsx", "output_path": "out.json", "mode": "sheets"},
        "requirements": [
            {
                "text": "Review explicit contract",
                "quote": "Review",
                "fields": [
                    "/capability",
                    "/constraints",
                    "/execution/input_path",
                    "/execution/output_path",
                    "/execution/mode",
                ],
            }
        ],
        "unresolved": [],
    }
    server = StdioMCPServer(
        db_path=db, workspace=tmp_path, catalog_root=catalog_root, skill_snapshot_id=snapshot
    )
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "rpe_start_task", "arguments": {"task": "Review", "draft": draft}},
        }
    )
    operation = response["result"]["structuredContent"]
    assert operation["status"] == "awaiting_confirmation", operation
    assert operation["draft"]["capability"] == "xlsx.inspect"
    assert operation["proposal"]["catalog_source"]["skill_snapshot_id"] == snapshot
    assert not (tmp_path / "out.json").exists()
    spoof = server._call_tool(
        "rpe_start_task",
        {"task": "Review", "catalog_root": "/untrusted", "skill_snapshot_id": "b" * 64},
    )
    assert spoof["isError"] is True


def _v231_failed_operation(tmp_path, monkeypatch):
    from rpe_harness import json_execution
    from rpe_harness.runtime import HarnessRuntime

    (tmp_path / "input.json").write_text('{ "x": 1 }', encoding="utf-8")
    api = HostAPI(tmp_path / "runs.sqlite")
    draft = {
        "capability": "json.normalize",
        "constraints": [],
        "execution": {"input_path": "input.json", "output_path": "out.json", "style": "minify"},
        "requirements": [
            {
                "text": "Review explicit contract",
                "quote": "Review",
                "fields": [
                    "/capability",
                    "/constraints",
                    "/execution/input_path",
                    "/execution/output_path",
                    "/execution/style",
                ],
            }
        ],
        "unresolved": [],
    }
    operation = api.start_task(
        {"task": "Review", "draft": draft},
        {"owner": "alice", "session_id": "s", "workspace": str(tmp_path)},
    )
    assert operation["status"] == "awaiting_confirmation", operation
    receipt = api.confirm_task(
        operation["operation_id"],
        operation["digest"],
        TrustedHostAction(
            "alice", operation["operation_id"], operation["generation"], operation["digest"]
        ),
    )
    assert receipt["status"] == "confirmed"
    proposal = api.proposals.show(operation["proposal_id"], session_id="s")
    resolved = api.proposals.resolve(
        receipt["confirmation_id"], session_id="s", catalog_sha256=proposal["catalog_sha256"]
    )
    request = resolved["request"]
    request["event_id"] = receipt["event_id"]
    runtime = HarnessRuntime(db_path=api.db_path)

    def fail(**kwargs):
        raise RuntimeError("recoverable execution failure")

    with monkeypatch.context() as patch:
        patch.setattr(json_execution, "prepare_json_normalization", fail)
        failed = runtime.run(request)
    assert failed["status"] == "failed"
    assert api.show_task(operation["operation_id"], "alice")["run_id"] == failed["run_id"]
    return api, runtime, operation, failed, fail


def test_v231_recovered_retry_chain_visible_via_host_and_mcp(tmp_path, monkeypatch):
    from rpe_harness import json_execution
    from rpe_harness.recovery import recover_run

    api, runtime, operation, original, fail = _v231_failed_operation(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(json_execution, "prepare_json_normalization", fail)
        second = recover_run(runtime, original["run_id"])
    assert second["status"] == "failed"
    recovered = recover_run(runtime, second["run_id"])
    assert recovered["status"] == "succeeded"
    api = HostAPI(api.db_path)
    for read in (api.show_task, api.reconcile_task):
        view = read(operation["operation_id"], "alice")
        assert view["status"] == "succeeded"
        assert view["run_id"] == recovered["run_id"]
        assert view["execution_result"]["status"] == "succeeded"
        assert json.loads(Path(view["artifact"]["path"]).read_text()) == {"x": 1}
    assert api.reconcile_task(operation["operation_id"], "alice")["allowed_recovery_actions"] == []
    server = StdioMCPServer(db_path=api.db_path, owner="alice", workspace=tmp_path)
    for tool in ("rpe_show_task", "rpe_reconcile_task"):
        view = server._call_tool(tool, {"operation_id": operation["operation_id"]})[
            "structuredContent"
        ]
        assert view["status"] == "succeeded"
        assert view["run_id"] == recovered["run_id"]


def test_v231_retry_cycle_rejected_without_hanging(tmp_path, monkeypatch):
    api, runtime, operation, original, _ = _v231_failed_operation(tmp_path, monkeypatch)
    runtime.store.step(original["run_id"], "plan", {"retry_of": original["run_id"]})
    with pytest.raises(ValueError, match="cycle|cyclic"):
        api.show_task(operation["operation_id"], "alice")


@pytest.mark.parametrize("unfinished_link", [False, True])
def test_v231_retry_rejected_before_plan_step_remains_visible(
    tmp_path, monkeypatch, unfinished_link
):
    from rpe_harness.recovery import recover_run

    api, runtime, operation, original, _ = _v231_failed_operation(tmp_path, monkeypatch)
    run = runtime.run

    def mutate_after_guard(request, **kwargs):
        (tmp_path / "input.json").write_text('{"changed": true}')
        return run(request, **kwargs)

    monkeypatch.setattr(runtime, "run", mutate_after_guard)
    retry = recover_run(runtime, original["run_id"])
    assert retry["status"] == "rejected"
    assert not any(step["stage"] == "plan" for step in retry["steps"])
    if unfinished_link:
        # Simulate owner exit after run admission, before finalizing the retry link.
        with runtime.store.connect() as db:
            db.execute(
                "UPDATE recovery_attempts SET retry_run_id=NULL WHERE retry_of=?",
                (original["run_id"],),
            )
    reopened = HostAPI(api.db_path)
    for inspect in (reopened.show_task, reopened.reconcile_task):
        view = inspect(operation["operation_id"], "alice")
        assert view["run_id"] == retry["run_id"]
        assert view["status"] == "rejected"
        assert view["execution_result"]["status"] == "rejected"


def test_v231_versions_and_release_gates():
    import tomllib

    assert __version__ == "2.3.1"
    project = tomllib.loads((_V231_ROOT / "pyproject.toml").read_text())
    assert project["project"]["version"] == __version__
    assert (
        json.loads((_V231_ROOT / "integrations/dsh/package.json").read_text())["version"]
        == __version__
    )
    workflow = (_V231_ROOT / ".github/workflows/release.yml").read_text()
    release = workflow.index("- name: Create GitHub Release")
    assert workflow.index("ruff check src tests") < release
    assert workflow.index("pytest -p no:cacheprovider") < release
    assert workflow.index("GITHUB_REF_NAME") < release
    # Execute the exact version guard embedded in the release workflow.
    guard = workflow.split("python - <<'PY'", 1)[1].split("\n          PY", 1)[0]
    import textwrap

    guard = textwrap.dedent(guard).strip()
    for tag, succeeds in [("v2.3.1", True), ("v2.3.0", False), ("main", False)]:
        result = subprocess.run(
            [sys.executable, "-c", guard],
            cwd=_V231_ROOT,
            env={**os.environ, "GITHUB_REF_NAME": tag},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert (result.returncode == 0) is succeeds, result.stderr
    ci = (_V231_ROOT / ".github/workflows/ci.yml").read_text()
    assert ci.count("tests/test_v231_hardening.py") == 3


def _v231_dev_plan(plugin, source, raw, options):
    return PluginExecutionPlan(
        capability=plugin.capability,
        contract_version=plugin.contract_version,
        executor_id=plugin.executor_id,
        executor_revision=plugin.executor_revision,
        verifier_revision=plugin.verifier_revision,
        input_path=str(source),
        output_path=str(source.parent / "v231-result.csv"),
        input_format=source.suffix[1:],
        input_sha256=hashlib.sha256(raw).hexdigest(),
        options_json=json.dumps(plugin.validate_options(options, raw), sort_keys=True),
        constraints=(),
        goal="Exercise developer contract hardening",
    )


def _v231_dev_database(source):
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.execute("INSERT INTO items VALUES ('validated')")
    return source.read_bytes()


def test_v231_sqlite_execute_uses_validated_bytes_after_source_changes(tmp_path):
    source = tmp_path / "input.sqlite"
    raw = _v231_dev_database(source)
    plan = _v231_dev_plan(
        dev_contracts.SQLITE_EXPORT_PLUGIN, source, raw, {"query": "SELECT value FROM items"}
    )
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE items SET value = 'replacement'")
    exported, _ = dev_contracts._sqlite_execute(plan, raw)
    assert exported == b"value\nvalidated\n"


def test_v231_sqlite_verify_uses_validated_bytes_after_source_changes(tmp_path):
    source = tmp_path / "input.sqlite"
    raw = _v231_dev_database(source)
    plan = _v231_dev_plan(
        dev_contracts.SQLITE_EXPORT_PLUGIN, source, raw, {"query": "SELECT value FROM items"}
    )
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE items SET value = 'replacement'")
    assert dev_contracts._sqlite_verify(plan, raw, b"value\nvalidated\n")["passed"]
    assert not dev_contracts._sqlite_verify(plan, raw, b"value\nreplacement\n")["passed"]


def test_v231_sqlite_uses_private_readonly_encoded_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "input.sqlite"
    raw = _v231_dev_database(source)
    plan = _v231_dev_plan(
        dev_contracts.SQLITE_EXPORT_PLUGIN, source, raw, {"query": "SELECT value FROM items"}
    )
    scratch = tmp_path / (
        "snapshot # % directory" if os.name == "nt" else "snapshot ? # % directory"
    )
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    original_connect = sqlite3.connect
    opened = []

    def checked_connect(database, *args, **kwargs):
        parsed = urlsplit(str(database))
        snapshot = Path(url2pathname(parsed.path))
        assert snapshot != source, "query reopened the mutable original database"
        assert snapshot.is_relative_to(scratch)
        assert snapshot.read_bytes() == raw
        assert snapshot.stat().st_mode & 0o222 == 0
        if os.name != "nt":
            assert snapshot.parent.stat().st_mode & 0o077 == 0
        assert parsed.query == "mode=ro"
        assert not parsed.fragment
        opened.append(snapshot)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", checked_connect)
    exported, _ = dev_contracts._sqlite_execute(plan, raw)
    assert exported == b"value\nvalidated\n"
    assert dev_contracts._sqlite_verify(plan, raw, exported)["passed"]
    assert len(opened) == 2
    assert all(not path.exists() for path in opened)


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (b"KEY=old # useful note\n", b"KEY=new # useful note\n"),
        (b'export KEY = "old # literal"  # useful note\n', b"export KEY = new  # useful note\n"),
        (b"KEY='old # literal'\t# useful note\n", b"KEY=new\t# useful note\n"),
        (b"KEY='it\\'s # literal' # useful note\n", b"KEY=new # useful note\n"),
        (b"KEY=value#literal\n", b"KEY=new\n"),
        (b"KEY= # empty value note\n", b"KEY=new # empty value note\n"),
    ],
)
def test_v231_env_update_preserves_inline_comments(tmp_path, original, expected):
    plan = _v231_dev_plan(
        dev_contracts.ENV_PATCH_PLUGIN,
        tmp_path / "input.env",
        original,
        {"operations": [{"op": "set", "key": "KEY", "value": "new"}]},
    )
    output, _ = dev_contracts._env_execute(plan, original)
    assert output == expected
    assert dev_contracts._env_verify(plan, original, output)["passed"]


def test_v231_env_verifier_independently_rejects_comment_loss(tmp_path, monkeypatch):
    raw = b"KEY=old # annotation\n"
    plan = _v231_dev_plan(
        dev_contracts.ENV_PATCH_PLUGIN,
        tmp_path / "input.env",
        raw,
        {"operations": [{"op": "set", "key": "KEY", "value": "new"}]},
    )
    monkeypatch.setattr(dev_contracts, "_apply_env_ops", lambda *args: "KEY=new\n")
    broken_output, _ = dev_contracts._env_execute(plan, raw)
    verdict = dev_contracts._env_verify(plan, raw, broken_output)
    assert not verdict["passed"]
    assert not verdict["checks"]["comments_and_blanks_preserved"]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 'delete'",
        'SELECT 1 AS "update"',
        "SELECT 'a;b'",
        "SELECT 'it''s an update;'",
        'SELECT 1 AS "a""delete;"',
        "-- delete; ignored\nSELECT 1",
        "/* DROP TABLE items; */ SELECT 1",
        "SELECT 1 /* update; ignored */",
        "SELECT 1; -- trailing delete;",
        "WITH value AS (SELECT 1) SELECT * FROM value",
    ],
)
def test_v231_sqlite_allows_keywords_and_semicolons_only_inside_noncode(query):
    accepted = dev_contracts._validate_readonly_sql(query)
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute(accepted).fetchall()


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM items",
        "UPDATE items SET value = 1",
        "DROP TABLE items",
        "SELECT 1; SELECT 2",
        "SELECT 1;;",
        "SELECT 1; 'hidden second statement'",
        'SELECT 1; "hidden second statement"',
        "SELECT 'unterminated",
        "WITH value AS (SELECT 1) DELETE FROM items",
        "SELECT 'safe'; /* ignored */ DELETE FROM items",
        "/* harmless */ UPDATE items SET value = 1",
    ],
)
def test_v231_sqlite_rejects_writes_and_multiple_statements(query):
    with pytest.raises(ValueError):
        dev_contracts._validate_readonly_sql(query)


def _v231_plugin_spec(capability="acme.v231-review"):
    return ContractPlugin(
        capability=capability,
        executor_id="acme/v231-review",
        input_formats=("txt",),
        validate_options=lambda options, raw: dict(options),
        execute=lambda plan, raw: (raw.upper(), {}),
        verify=lambda plan, raw, staged: {"passed": staged == raw.upper()},
    )


@pytest.mark.parametrize("capability", ["tabular.deduplicate", "json.normalize", "xlsx.inspect"])
@pytest.mark.parametrize("replace", [False, True])
def test_v231_registry_rejects_reserved_builtin_capabilities(capability, replace):
    registry = ContractRegistry()
    with pytest.raises(ValueError, match="reserved"):
        registry.register(_v231_plugin_spec(capability), replace=replace)


@pytest.mark.parametrize("capability", ["tabular.deduplicate", "json.normalize", "xlsx.inspect"])
def test_v231_entrypoint_cannot_claim_reserved_builtin(capability, monkeypatch):
    class ReservedEntryPoint:
        name = "reserved-test"

        def load(self):
            return _v231_plugin_spec(capability)

    monkeypatch.setattr(contract_sdk, "entry_points", lambda **kwargs: [ReservedEntryPoint()])
    registry = ContractRegistry()
    assert registry.load_entry_points() == []
    assert registry.get(capability) is None
    assert registry.load_errors and "reserved" in registry.load_errors[0]["error"]


@pytest.mark.parametrize(
    "source_mode,draft_mode",
    [("uppercase", "uppercase"), ("uppercase", "lowercase"), ("uppercase", None)],
)
def test_v231_plugin_review_context_returns_structured_proposal(tmp_path, source_mode, draft_mode):
    plugin = _v231_plugin_spec()
    DEFAULT_REGISTRY.register(plugin)
    try:
        source = tmp_path / "input.txt"
        source.write_text("hello\n")
        execution = {"input_path": str(source), "output_path": str(tmp_path / "out.txt")}
        if draft_mode is not None:
            execution["mode"] = draft_mode
        path = tmp_path / "review.sqlite"
        context = prepare_context(
            path,
            [
                {
                    "source": "explicit",
                    "event_id": "paths",
                    "payload": {"text": "Review input.txt and out.txt with uppercase mode."},
                    "facts": {
                        "input_path": str(source),
                        "output_path": execution["output_path"],
                        "mode": source_mode,
                    },
                }
            ],
            "v231-plugin-session",
        )
        draft = {
            "capability": plugin.capability,
            "constraints": [],
            "execution": execution,
            "requirements": [
                {
                    "text": "Review explicit values",
                    "quote": None,
                    "fields": [
                        "/capability",
                        "/constraints",
                        *("/execution/" + key for key in execution),
                    ],
                }
            ],
            "unresolved": [],
        }
        result = create_proposal(
            "Review this explicit transformation.",
            draft,
            db_path=path,
            workspace=tmp_path,
            session_id="v231-plugin-session",
            source_ref="current:user",
            review_context=context,
        )
        if draft_mode == source_mode:
            assert result["status"] == "awaiting_confirmation", result
            assert result["context_recovery"]["status"] == "ready"
            assert result["context_recovery"]["provenance"]["input_path"] == context["source_refs"]
        else:
            assert result["status"] == "needs_context", result
            assert "verified_context_recovery" in result["missing"]
            assert any("execution.mode" in error for error in result["errors"])
        assert not (tmp_path / "out.txt").exists()
    finally:
        DEFAULT_REGISTRY.unregister(plugin.capability)


def test_v231_unknown_review_context_capability_is_validation_error(tmp_path):
    path = tmp_path / "review.sqlite"
    context = prepare_context(path, [], "v231-plugin-session")
    with pytest.raises(ValueError, match="unsupported context capability"):
        recover_context(path, context, "v231-plugin-session", "acme.not-registered", {}, [])


def test_v231_cross_format_plugin_plan_and_artifact_report_output_format(tmp_path):
    source = tmp_path / "input.sqlite"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE orders (code TEXT)")
        database.execute("INSERT INTO orders VALUES (?)", ("0042",))
    output = tmp_path / "out.csv"
    result = HarnessRuntime(db_path=tmp_path / "runs.sqlite").run(
        {
            "schema_version": 2,
            "event_id": "v231-cross-format",
            "capability": "sqlite.query_export",
            "goal": "Export order codes",
            "execution": {
                "input_path": str(source),
                "output_path": str(output),
                "query": "SELECT code FROM orders",
            },
        }
    )
    assert result["status"] == "succeeded", result
    assert output.read_text() == "code\n0042\n"
    plan = result["steps"][0]["evidence"]["plan"]
    assert plan["input_format"] == "sqlite"
    assert plan["output_format"] == "csv"
    assert result["artifact"]["format"] == "csv"
    assert PluginExecutionPlan.from_dict(plan).output_format == "csv"


def test_v231_same_format_plugin_plan_defaults_output_format_and_roundtrips(tmp_path):
    plugin = _v231_plugin_spec()
    registry = ContractRegistry()
    registry.register(plugin)
    source = tmp_path / "input.txt"
    source.write_text("hello")
    plan = registry.plan_from_request(
        {
            "schema_version": 2,
            "event_id": "v231-same-format",
            "capability": plugin.capability,
            "goal": "Uppercase text",
            "execution": {"input_path": str(source), "output_path": str(tmp_path / "out.txt")},
        }
    )
    assert plan.output_format == "txt"
    assert PluginExecutionPlan.from_dict(plan.to_dict()).output_format == "txt"
    legacy = plan.to_dict()
    legacy.pop("output_format")
    assert PluginExecutionPlan.from_dict(legacy).output_format == "txt"


@pytest.mark.parametrize(
    "input_name,output_name",
    [
        (".env", ".env.patched"),
        (".env.production", ".env.reviewed"),
        ("app.env", "app.patched.env"),
    ],
)
@pytest.mark.parametrize("entry", ["runtime", "intent", "proposal"])
def test_v231_env_names_are_supported_through_all_entry_paths(
    tmp_path, input_name, output_name, entry
):
    source, output = tmp_path / input_name, tmp_path / output_name
    source.write_text("# retain\nPORT=3000\n")
    execution = {
        "input_path": str(source),
        "output_path": str(output),
        "operations": [{"op": "set", "key": "PORT", "value": "4000"}],
    }
    if entry == "runtime":
        result = HarnessRuntime(db_path=tmp_path / "runs.sqlite").run(
            {
                "schema_version": 2,
                "event_id": "v231-env-path",
                "capability": "env.patch",
                "goal": "Set PORT to 4000",
                "execution": execution,
            }
        )
        assert result["status"] == "succeeded", result
        assert result["artifact"]["format"] == "env"
        assert output.read_text() == "# retain\nPORT=4000\n"
    elif entry == "intent":
        result = interpret_intent(
            {
                "goal": "Set PORT to 4000",
                "capability": "env.patch",
                "constraints": [],
                "execution": execution,
            },
            catalog=build_catalog(),
            workspace=tmp_path,
            session_id="v231-env-session",
            source_ref="current:user",
        )
        assert result["status"] == "ready", result
        assert not output.exists()
    else:
        draft = {
            "capability": "env.patch",
            "constraints": [],
            "execution": execution,
            "requirements": [
                {
                    "text": "Review explicit values",
                    "quote": None,
                    "fields": [
                        "/capability",
                        "/constraints",
                        *("/execution/" + key for key in execution),
                    ],
                }
            ],
            "unresolved": [],
        }
        result = create_proposal(
            "Set PORT to 4000.",
            draft,
            db_path=tmp_path / "review.sqlite",
            workspace=tmp_path,
            session_id="v231-env-session",
            source_ref="current:user",
        )
        assert result["status"] == "awaiting_confirmation", result
        assert not output.exists()
    assert source.read_text() == "# retain\nPORT=3000\n"


@pytest.mark.parametrize(
    "capability", ["tabular.filter", "json.patch", "env.patch", "sqlite.query_export"]
)
def test_v231_bundled_plugin_review_accepts_explicit_context(tmp_path, capability):
    if capability == "tabular.filter":
        source_name, output_name = "input.csv", "out.csv"
        (tmp_path / source_name).write_text("code,status\n0042,active\n")
        options = {"column": "status", "operator": "equals", "value": "active"}
    elif capability == "json.patch":
        source_name, output_name = "input.json", "out.json"
        (tmp_path / source_name).write_text('{"port":3000}\n')
        options = {"operations": [{"op": "set", "key": "port", "value": 4000}]}
    elif capability == "env.patch":
        source_name, output_name = ".env.production", ".env"
        (tmp_path / source_name).write_text("PORT=3000\n")
        options = {"operations": [{"op": "set", "key": "PORT", "value": "4000"}]}
    else:
        source_name, output_name = "input.sqlite", "out.csv"
        with sqlite3.connect(tmp_path / source_name) as database:
            database.execute("CREATE TABLE orders (code TEXT)")
        options = {"query": "SELECT code FROM orders"}
    execution = {"input_path": source_name, "output_path": output_name, **options}
    path = tmp_path / "review.sqlite"
    context = prepare_context(
        path,
        [
            {
                "source": "attachment",
                "event_id": "notes",
                "payload": {"text": "Review the explicit draft values."},
                "facts": {},
            }
        ],
        "v231-bundled-session",
    )
    draft = {
        "capability": capability,
        "constraints": [],
        "execution": execution,
        "requirements": [
            {
                "text": "Review explicit values",
                "quote": None,
                "fields": [
                    "/capability",
                    "/constraints",
                    *("/execution/" + key for key in execution),
                ],
            }
        ],
        "unresolved": [],
    }
    proposal = create_proposal(
        "Review the explicit draft values.",
        draft,
        db_path=path,
        workspace=tmp_path,
        session_id="v231-bundled-session",
        source_ref="current:user",
        review_context=context,
    )
    assert proposal["status"] == "awaiting_confirmation", proposal
    assert proposal["context_recovery"]["status"] == "ready"
    assert not (tmp_path / output_name).exists()


def _v231_seed_inbox(tmp_path, owner="alice", output="out.json"):
    source = tmp_path / "input.json"
    source.write_text('{"a": 1}', encoding="utf-8")
    api = HostAPI(tmp_path / "inbox.sqlite")
    op = api.start_task(
        {
            "task": f"Normalize input.json to {output}, minify and preserve all values.",
            "draft": {
                "capability": "json.normalize",
                "constraints": [],
                "execution": {"input_path": "input.json", "output_path": output, "style": "minify"},
                "requirements": [
                    {"text": "Normalize", "quote": "Normalize", "fields": ["/capability"]},
                    {"text": "Input", "quote": "input.json", "fields": ["/execution/input_path"]},
                    {"text": "Output", "quote": output, "fields": ["/execution/output_path"]},
                    {"text": "Minify", "quote": "minify", "fields": ["/execution/style"]},
                    {"text": "No extra", "quote": None, "fields": ["/constraints"]},
                ],
                "unresolved": [],
            },
        },
        {"owner": owner, "session_id": "v231", "workspace": str(tmp_path)},
    )
    assert op["status"] == "awaiting_confirmation"
    return api, op


@pytest.fixture
def v231_inbox_server(tmp_path):
    api, op = _v231_seed_inbox(tmp_path)
    yield (
        SimpleNamespace(
            server_address=("127.0.0.1", 18401),
            server_port=18401,
            csrf_token="v231-token",
            api=api,
            owner_filter=None,
        ),
        op,
    )


class _V231MemorySocket:
    def __init__(self, incoming):
        self.incoming = BytesIO(incoming)
        self.outgoing = BytesIO()

    def makefile(self, *args, **kwargs):
        return self.incoming

    def sendall(self, data):
        self.outgoing.write(data)


def _v231_inbox_request(server, method, path="/", headers=None, body=None):
    headers = dict(headers or {})
    host = server.server_address[0]
    headers.setdefault("Host", f"{'[' + host + ']' if ':' in host else host}:{server.server_port}")
    raw_body = (body or "").encode()
    if body is not None:
        headers.setdefault("Content-Length", str(len(raw_body)))
    request = f"{method} {path} HTTP/1.1\r\n"
    request += "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
    connection = _V231MemorySocket(request.encode() + raw_body)
    inbox.InboxHandler(connection, ("127.0.0.1", 40000), server)
    response = HTTPResponse(_V231MemorySocket(connection.outgoing.getvalue()))
    response.begin()
    return response.status, dict(response.getheaders()), response.read()


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "attacker.example:18401",
        "127.0.0.1:1",
        "localhost:1",
        "127.0.0.1",
        "user@127.0.0.1:1",
    ],
)
def test_v231_inbox_rejects_untrusted_host(v231_inbox_server, method, host):
    server, _ = v231_inbox_server
    status, _, _ = _v231_inbox_request(
        server, method, headers={"Host": host, "X-RPE-CSRF": server.csrf_token}
    )
    assert status == 403


@pytest.mark.parametrize(
    "origin",
    [
        "http://attacker.example",
        "null",
        "http://127.0.0.1:1",
        "https://127.0.0.1:{port}",
        "http://localhost:{port}",
    ],
)
def test_v231_inbox_rejects_cross_origin_post(v231_inbox_server, origin):
    server, _ = v231_inbox_server
    status, _, _ = _v231_inbox_request(
        server,
        "POST",
        "/api/cancel",
        headers={"Origin": origin.format(port=server.server_port), "X-RPE-CSRF": server.csrf_token},
        body="{}",
    )
    assert status == 403


@pytest.mark.parametrize(
    "fetch_site", ["cross-site", "cross-site ", "same-origin, cross-site", "unknown"]
)
def test_v231_inbox_rejects_cross_site_fetch(v231_inbox_server, fetch_site):
    server, _ = v231_inbox_server
    status, _, _ = _v231_inbox_request(
        server,
        "POST",
        "/api/cancel",
        headers={
            "Origin": f"http://127.0.0.1:{server.server_port}",
            "Sec-Fetch-Site": fetch_site,
            "X-RPE-CSRF": server.csrf_token,
        },
        body="{}",
    )
    assert status == 403


@pytest.mark.parametrize("path", ["/", "/api/pending", "/missing"])
def test_v231_inbox_all_responses_block_embedding(v231_inbox_server, path):
    server, _ = v231_inbox_server
    _, headers, _ = _v231_inbox_request(server, "GET", path)
    assert "frame-ancestors 'none'" in headers.get("Content-Security-Policy", "")
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("X-Content-Type-Options") == "nosniff"


def test_v231_inbox_same_origin_post_and_recent_confirmed(v231_inbox_server):
    server, op = v231_inbox_server
    action = TrustedHostAction(
        owner="alice",
        operation_id=op["operation_id"],
        generation=op["generation"],
        digest=op["digest"],
        actor="v231",
        action="confirm",
    )
    confirmed = server.api.confirm_task(op["operation_id"], op["digest"], action)
    assert confirmed["status"] == "confirmed"
    status, _, raw = _v231_inbox_request(server, "GET", "/api/pending")
    assert status == 200
    data = json.loads(raw)
    assert data["pending"] == []
    assert data["recent"][0]["operation_id"] == op["operation_id"]
    assert data["recent"][0]["status"] == "confirmed"
    status, _, raw = _v231_inbox_request(
        server,
        "POST",
        "/api/cancel",
        headers={
            "Origin": f"http://127.0.0.1:{server.server_port}",
            "X-RPE-CSRF": server.csrf_token,
        },
        body=json.dumps(
            {
                "operation_id": op["operation_id"],
                "owner": "alice",
                "generation": confirmed["generation"],
            }
        ),
    )
    assert status == 200
    assert json.loads(raw)["status"] == "cancelled"


def test_v231_inbox_ipv6_binding_and_cli_url(tmp_path, monkeypatch, capsys):
    if not socket.has_ipv6:
        pytest.skip("IPv6 unavailable")
    api = HostAPI(tmp_path / "v6.sqlite")

    def fake_bind(server):
        server.server_port = 18401
        server.server_name = "::1"

    monkeypatch.setattr(inbox.InboxHTTPServer, "server_bind", fake_bind)
    monkeypatch.setattr(inbox.InboxHTTPServer, "server_activate", lambda server: None)
    server = inbox.InboxHTTPServer(("::1", 18401), api)
    try:
        assert server.address_family == socket.AF_INET6
        status, _, _ = _v231_inbox_request(server, "GET")
        assert status == 200
    finally:
        server.server_close()

    class V231FakeV6Server:
        server_port = 18401

        def __init__(self, *args, **kwargs):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(inbox, "InboxHTTPServer", V231FakeV6Server)

    class V231Args:
        db = str(tmp_path / "cli.sqlite")
        web, owner, host, port = True, None, "::1", 18401

    assert inbox.dispatch_inbox(V231Args()) == 0
    assert "http://[::1]:18401/" in capsys.readouterr().out


def test_v231_inbox_javascript_reports_http_failure_and_recent_status(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable for inbox browser-script regression")
    page = inbox._render_html("token", tmp_path / "db")
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    program = r"""
const assert = require('node:assert/strict');
const elements = new Map();
function element(id) { if (!elements.has(id)) elements.set(id, {
  style: {}, textContent: '', innerHTML: '', children: [],
  appendChild(child) { this.children.push(child); },
  selectors: {}, querySelector(selector) { return this.selectors[selector] ||= {textContent: '', onclick: null}; }
}); return elements.get(id); }
global.document = {getElementById: element, createElement: () => ({...element('template'), children: [], selectors: {}})};
global.setInterval = () => {};
global.setTimeout = () => {};
let mode = 'read';
global.fetch = async () => mode === 'fail'
  ? {ok: false, status: 409, json: async () => ({error: 'approval generation is stale'})}
  : {ok: true, status: 200, json: async () => ({pending: [], recent: [
      {operation_id: 'op-failed', owner: 'alice', generation: 1, digest: 'abc', status: 'failed',
       summary: 'check', error: {message: 'worker failed'}}]})};
"""
    # Evaluate the shipped script and execute the actual action function against a failed HTTP response.
    program += (
        script
        + r"""
(async () => {
  await refresh();
  assert.match(JSON.stringify([...elements.values()]), /op-failed/);
  assert.match(JSON.stringify([...elements.values()]), /failed/);
  mode = 'fail';
  await act('/api/approve', {});
  assert.match(element('toast').textContent, /approval generation is stale/);
  assert.doesNotMatch(element('toast').textContent, /^done/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )
    result = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_v231_bwrap_uses_pid_namespace_when_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_bwrap_pid_namespace_supported", lambda _: True, raising=False)
    command = sandbox._bwrap_command(tmp_path, tmp_path / "driver.py", tmp_path / "candidate.py")
    assert "--unshare-pid" in command


def test_v231_bwrap_declares_pid_namespace_fallback(monkeypatch):
    monkeypatch.setattr(sandbox, "sandbox_backend", lambda: "linux-bwrap")
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_bwrap_pid_namespace_supported", lambda _: False, raising=False)
    caps = sandbox.sandbox_capabilities()
    assert caps["pid_namespace"] is False
    assert "no_pid_namespace" in caps["process_tree"]
    assert caps["memory"] == "proc_tree_statm_rss_soft"


def test_v231_linux_rss_counts_candidate_descendants(monkeypatch):
    observed = {100: 4096, 101: 8192, 102: 16384}
    monkeypatch.setattr(sandbox, "_linux_resident_bytes", lambda pid: observed.get(pid))
    monkeypatch.setattr(
        sandbox,
        "_linux_child_pids",
        lambda pid: {100: [101], 101: [102]}.get(pid, []),
        raising=False,
    )
    observer = getattr(sandbox, "_linux_process_tree_resident_bytes", sandbox._linux_resident_bytes)
    assert observer(100) == sum(observed.values())


@pytest.mark.parametrize("initial_returncode", [None, 0])
def test_v231_windows_job_assignment_failure_kills_and_bounds_wait(
    tmp_path, monkeypatch, initial_returncode
):
    source = tmp_path / "input.json"
    source.write_bytes(b"{}")
    plan = {
        "capability": "json.normalize",
        "input_format": "json",
        "input_path": str(source),
        "input_sha256": hashlib.sha256(b"{}").hexdigest(),
    }
    calls = []

    class V231UnassignedProcess:
        _handle = 123
        returncode = initial_returncode

        def poll(self):
            return self.returncode

        def kill(self):
            calls.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            assert timeout is not None, "unassigned child cleanup must have a deadline"
            return self.returncode

    child = V231UnassignedProcess()

    class V231Kernel:
        def AssignProcessToJobObject(self, *args):
            return False

        def TerminateJobObject(self, *args):
            calls.append("terminate-job")

        def CloseHandle(self, *args):
            calls.append("close-job")

    def spawn(*args, **kwargs):
        calls.append(("flags", kwargs["creationflags"]))
        return child

    monkeypatch.setattr(sandbox, "sandbox_backend", lambda: "windows-job-object")
    monkeypatch.setattr(sandbox, "_win32_create_job", lambda _: (V231Kernel(), 321))
    monkeypatch.setattr(sandbox.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(sandbox.subprocess, "Popen", spawn)
    with pytest.raises(sandbox.CandidateExecutionError, match="AssignProcessToJobObject"):
        sandbox.run_candidate(b"def prepare(plan, staging_path): pass", plan, tmp_path)
    assert "kill" in calls
    assert ("wait", 3) in calls
    assert next(item[1] for item in calls if isinstance(item, tuple) and item[0] == "flags") & 0x4


def test_v231_inbox_recent_failed_receipt_and_owner_filter(tmp_path):
    api, op = _v231_seed_inbox(tmp_path)
    _, other = _v231_seed_inbox(tmp_path, owner="bob", output="other.json")
    for item, owner in [(op, "alice"), (other, "bob")]:
        action = TrustedHostAction(
            owner=owner,
            operation_id=item["operation_id"],
            generation=item["generation"],
            digest=item["digest"],
            actor="v231",
            action="confirm",
        )
        confirmed = api.confirm_task(item["operation_id"], item["digest"], action)
        assert confirmed["status"] == "confirmed"
        with api._connect() as connection:
            connection.execute(
                "INSERT INTO rpe_proposal_executions(confirmation_id,event_id,result) VALUES(?,?,?)",
                (
                    confirmed["confirmation_id"],
                    confirmed["event_id"],
                    json.dumps(
                        {
                            "status": "failed",
                            "error": {"code": "worker_failed", "message": "failed visibly"},
                        }
                    ),
                ),
            )
    server = SimpleNamespace(
        server_address=("127.0.0.1", 18401),
        server_port=18401,
        csrf_token="token",
        api=api,
        owner_filter="alice",
    )
    status, _, raw = _v231_inbox_request(server, "GET", "/api/pending")
    assert status == 200
    data = json.loads(raw)
    assert [item["operation_id"] for item in data["recent"]] == [op["operation_id"]]
    assert data["recent"][0]["status"] == "failed"
    assert data["recent"][0]["error"]["message"] == "failed visibly"


@pytest.mark.parametrize(
    "method,path,headers",
    [
        ("POST", "/api/cancel", {}),
        ("GET", "/", {"Host": "attacker.example"}),
        ("OPTIONS", "/", {}),
    ],
)
def test_v231_inbox_error_responses_block_embedding(v231_inbox_server, method, path, headers):
    server, _ = v231_inbox_server
    status, response_headers, _ = _v231_inbox_request(server, method, path, headers)
    assert status >= 400
    assert response_headers["X-Frame-Options"] == "DENY"
    assert response_headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response_headers["Content-Security-Policy"]


@pytest.mark.parametrize("reason", ["apparmor", "timeout"])
def test_v231_bwrap_pid_namespace_probe_falls_back_safely(tmp_path, monkeypatch, reason):
    probe = sandbox._bwrap_pid_namespace_supported
    probe.cache_clear()

    def denied(*args, **kwargs):
        if reason == "apparmor":
            raise PermissionError("AppArmor blocks PID namespace")
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(sandbox.subprocess, "run", denied)
    monkeypatch.setattr(shutil, "which", lambda _: "/v231/bwrap-" + reason)
    assert probe("/v231/bwrap-" + reason) is False
    command = sandbox._bwrap_command(tmp_path, tmp_path / "driver.py", tmp_path / "candidate.py")
    assert "--unshare-pid" not in command
    assert "--unshare-net" in command
    assert "--die-with-parent" in command
    assert "--ro-bind" in command
    probe.cache_clear()


def test_v231_windows_resume_follows_assignment(tmp_path, monkeypatch):
    source = tmp_path / "input.json"
    source.write_bytes(b"{}")
    plan = {
        "capability": "json.normalize",
        "input_format": "json",
        "input_path": str(source),
        "input_sha256": hashlib.sha256(b"{}").hexdigest(),
    }
    calls = []

    class V231AssignedProcess:
        _handle, returncode = 123, None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            assert calls[:2] == ["assign", "resume"]
            assert timeout is not None
            self.returncode = 0
            return 0

    child = V231AssignedProcess()

    class V231Kernel:
        def AssignProcessToJobObject(self, *args):
            calls.append("assign")
            return True

        def TerminateJobObject(self, *args):
            calls.append("terminate")

        def CloseHandle(self, *args):
            calls.append("close")

    def spawn(*args, **kwargs):
        packet = json.load(kwargs["stdin"])
        from pathlib import Path as V231Path

        V231Path(packet["staging_path"]).write_bytes(b"{}")
        assert kwargs["creationflags"] & 0x4
        return child

    monkeypatch.setattr(sandbox, "sandbox_backend", lambda: "windows-job-object")
    monkeypatch.setattr(sandbox, "_win32_create_job", lambda _: (V231Kernel(), 321))
    monkeypatch.setattr(sandbox, "_win32_resume_process", lambda proc: calls.append("resume"))
    monkeypatch.setattr(sandbox.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(sandbox.subprocess, "Popen", spawn)
    assert sandbox.run_candidate(b"def prepare(plan, staging_path): pass", plan, tmp_path) == b"{}"
    assert calls[-1] == "close"


def test_v231_bwrap_probe_tests_pid_and_network_combination(monkeypatch):
    sandbox._bwrap_pid_namespace_supported.cache_clear()

    def run(command, **kwargs):
        assert "--unshare-pid" in command
        assert "--unshare-net" in command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(sandbox.subprocess, "run", run)
    assert sandbox._bwrap_pid_namespace_supported("/v231/combined-bwrap") is True
    sandbox._bwrap_pid_namespace_supported.cache_clear()


@pytest.mark.parametrize(
    "raw_headers",
    [
        "",
        "Host: 127.0.0.1:18401\r\nHost: attacker.example\r\n",
    ],
)
def test_v231_inbox_rejects_missing_or_duplicate_host(v231_inbox_server, raw_headers):
    server, _ = v231_inbox_server
    connection = _V231MemorySocket(("GET / HTTP/1.1\r\n" + raw_headers + "\r\n").encode())
    inbox.InboxHandler(connection, ("127.0.0.1", 40000), server)
    response = HTTPResponse(_V231MemorySocket(connection.outgoing.getvalue()))
    response.begin()
    assert response.status == 403
    assert response.getheader("X-Frame-Options") == "DENY"


@pytest.mark.parametrize("host", ["127.0.0.1:18401", "localhost:18401", "[::1]:18401"])
def test_v231_inbox_accepts_loopback_aliases_at_bound_port(v231_inbox_server, host):
    server, _ = v231_inbox_server
    from urllib.parse import urlsplit as v231_urlsplit

    server.bind_host = v231_urlsplit("//" + host).hostname
    assert _v231_inbox_request(server, "GET", headers={"Host": host})[0] == 200


def test_v231_inbox_rejects_duplicate_fetch_metadata(v231_inbox_server):
    server, _ = v231_inbox_server
    request = (
        "POST /api/cancel HTTP/1.1\r\nHost: 127.0.0.1:18401\r\n"
        "Sec-Fetch-Site: same-origin\r\nSec-Fetch-Site: cross-site\r\n"
        "X-RPE-CSRF: v231-token\r\nContent-Length: 2\r\n\r\n{}"
    )
    connection = _V231MemorySocket(request.encode())
    inbox.InboxHandler(connection, ("127.0.0.1", 40000), server)
    response = HTTPResponse(_V231MemorySocket(connection.outgoing.getvalue()))
    response.begin()
    assert response.status == 403


@pytest.mark.parametrize("status", [0, -1])
def test_v231_windows_resume_uses_native_handle_and_checks_status(monkeypatch, status):
    observed = []

    class V231Resume:
        def __call__(self, handle):
            observed.append(handle)
            return status

    class V231Ntdll:
        NtResumeProcess = V231Resume()

    monkeypatch.setattr(sandbox.ctypes, "WinDLL", lambda _: V231Ntdll(), raising=False)
    if status:
        with pytest.raises(sandbox.CandidateExecutionError, match="NtResumeProcess"):
            sandbox._win32_resume_process(SimpleNamespace(_handle=0x123456789))
    else:
        sandbox._win32_resume_process(SimpleNamespace(_handle=0x123456789))
    assert observed == [0x123456789]


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("host", ["localhost:18401", "[::1]:18401"])
def test_v231_inbox_rejects_other_loopback_bind_hosts(v231_inbox_server, method, host):
    server, _ = v231_inbox_server
    status, _, _ = _v231_inbox_request(
        server,
        method,
        headers={"Host": host, "X-RPE-CSRF": server.csrf_token},
        body="{}" if method == "POST" else None,
    )
    assert status == 403


def _v231_legacy_plugin_receipt(tmp_path, monkeypatch):
    from rpe_harness import runtime as runtime_module

    source = tmp_path / "legacy.csv"
    source.write_text("code,status\n0042,active\n0007,inactive\n", encoding="utf-8")
    runtime = HarnessRuntime(db_path=tmp_path / "legacy.sqlite")
    request = {
        "schema_version": 2,
        "event_id": "legacy-plugin-receipt",
        "capability": "tabular.filter",
        "goal": "Export active codes",
        "execution": {
            "input_path": str(source),
            "output_path": str(tmp_path / "out.csv"),
            "column": "status",
            "operator": "equals",
            "value": "active",
        },
    }

    def stopped(plan):
        raise RuntimeError("simulated stopped legacy execution")

    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, "prepare_plan", stopped)
        failed = runtime.run(request)
    assert failed["status"] == "failed", failed
    evidence = next(step["evidence"] for step in failed["steps"] if step["stage"] == "plan")
    legacy = dict(evidence["plan"])
    legacy.pop("output_format")
    # Reproduce the pre-2.3.1 digest from its persisted wire fields.
    semantic = {
        key: value
        for key, value in legacy.items()
        if key not in {"goal", "source_ref", "session_id"}
    }
    digest = hashlib.sha256(
        json.dumps(
            semantic, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    with runtime.store.connect() as database:
        database.execute(
            "UPDATE steps SET evidence_json=? WHERE run_id=? AND stage='plan'",
            (json.dumps({"plan": legacy, "plan_sha256": digest}), failed["run_id"]),
        )
    return runtime, runtime.store.get(failed["run_id"]), digest


@pytest.mark.parametrize("operation", ["load", "recover"])
def test_v231_legacy_plugin_plan_digest_allows_saved_plan_and_recovery(
    tmp_path, monkeypatch, operation
):
    from rpe_harness.recovery import _saved_plan, recover_run

    runtime, original, digest = _v231_legacy_plugin_receipt(tmp_path, monkeypatch)
    if operation == "load":
        restored = _saved_plan(original)
        assert restored.sha256 == digest
        assert restored.output_format == "csv"
    else:
        recovered = recover_run(runtime, original["run_id"])
        assert recovered["status"] == "succeeded", recovered
        assert recovered["artifact"]["plan_sha256"] == digest
        assert recovered["artifact"]["format"] == "csv"
        assert (tmp_path / "out.csv").read_text(encoding="utf-8") == "code,status\n0042,active\n"


def test_v231_legacy_cross_format_plan_infers_output_format(tmp_path):
    source = tmp_path / "legacy.sqlite"
    _v231_dev_database(source)
    plan = DEFAULT_REGISTRY.plan_from_request(
        {
            "schema_version": 2,
            "event_id": "legacy-export",
            "capability": "sqlite.query_export",
            "goal": "Export values",
            "execution": {
                "input_path": str(source),
                "output_path": str(tmp_path / "out.csv"),
                "query": "SELECT value FROM items",
            },
        }
    )
    legacy = plan.to_dict()
    legacy.pop("output_format")
    restored = PluginExecutionPlan.from_dict(legacy)
    assert restored.input_format == "sqlite"
    assert restored.output_format == "csv"
    assert restored.sha256 == plan.sha256


@pytest.mark.parametrize("output_format", ["json", None, ""])
def test_v231_persisted_plugin_output_format_cannot_mislabel_artifact(
    tmp_path, monkeypatch, output_format
):
    _, original, _ = _v231_legacy_plugin_receipt(tmp_path, monkeypatch)
    legacy = next(step["evidence"]["plan"] for step in original["steps"] if step["stage"] == "plan")
    with pytest.raises(ValueError, match="output_format"):
        PluginExecutionPlan.from_dict({**legacy, "output_format": output_format})


@pytest.mark.parametrize("base_kind", ["available", "missing", "empty", "relative", "directory"])
def test_v231_windows_candidate_launches_trusted_base_interpreter(tmp_path, monkeypatch, base_kind):
    source = tmp_path / "input.json"
    source.write_bytes(b"{}")
    plan = {
        "capability": "json.normalize",
        "input_format": "json",
        "input_path": str(source),
        "input_sha256": hashlib.sha256(b"{}").hexdigest(),
    }
    launcher = tmp_path / "venv" / "Scripts" / "python.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"venv redirector placeholder")
    base = tmp_path / "base-python.exe"
    base.write_bytes(b"base interpreter placeholder")
    values = {
        "available": str(base),
        "missing": str(tmp_path / "missing.exe"),
        "empty": "",
        "relative": "base-python.exe",
        "directory": str(tmp_path),
    }
    monkeypatch.setattr(sandbox.sys, "executable", str(launcher))
    monkeypatch.setattr(sandbox.sys, "_base_executable", values[base_kind], raising=False)
    calls, child_packet = [], {}

    class V231ExtraProcess:
        _handle, returncode = 123, None

        def poll(self):
            return self.returncode

        def kill(self):
            calls.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            assert calls[:2] == ["assign", "resume"]
            assert timeout is not None
            self.returncode = 0
            return 0

    process = V231ExtraProcess()

    class V231ExtraKernel:
        def AssignProcessToJobObject(self, *args):
            calls.append("assign")
            return True

        def TerminateJobObject(self, *args):
            calls.append("terminate")

        def CloseHandle(self, *args):
            calls.append("close")

    def spawn(argv, **kwargs):
        expected = str(base) if base_kind == "available" else str(launcher)
        assert argv[0] == expected
        assert argv[1:3] == ["-I", "-B"]
        assert kwargs["creationflags"] == 0x200 | 0x4
        child_packet.update(json.load(kwargs["stdin"]))
        return process

    def resume(proc):
        assert proc is process
        assert calls == ["assign"]
        calls.append("resume")
        Path(child_packet["staging_path"]).write_bytes(b"{}")

    def job(limit):
        assert limit == sandbox.MAX_RESIDENT_BYTES
        return V231ExtraKernel(), 321

    monkeypatch.setattr(sandbox, "sandbox_backend", lambda: "windows-job-object")
    monkeypatch.setattr(sandbox, "_win32_create_job", job)
    monkeypatch.setattr(sandbox, "_win32_resume_process", resume)
    monkeypatch.setattr(sandbox.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(sandbox.subprocess, "Popen", spawn)
    assert sandbox.run_candidate(b"def prepare(plan, staging_path): pass", plan, tmp_path) == b"{}"
    assert calls == ["assign", "resume", "terminate", "close"]


@pytest.mark.parametrize(
    "raw_headers",
    [
        "Host: attacker.example:18401\r\n",
        "",
        "Host: 127.0.0.1:18401\r\nHost: 127.0.0.1:18401\r\n",
        "Host: 127.0.0.1:18401\r\nOrigin: http://127.0.0.1:18401\r\n"
        "Origin: http://attacker.example\r\n",
        "Host: 127.0.0.1:18401\r\nSec-Fetch-Site: cross-site\r\n",
    ],
    ids=[
        "invalid-host",
        "missing-host",
        "duplicate-host",
        "duplicate-origin",
        "cross-site-no-origin",
    ],
)
def test_v231_rejected_post_preserves_state_and_all_security_headers(
    v231_inbox_server, raw_headers
):
    server, operation = v231_inbox_server
    request = (
        "POST /api/cancel HTTP/1.1\r\n"
        + raw_headers
        + "X-RPE-CSRF: v231-token\r\nContent-Length: 2\r\n\r\n{}"
    )
    connection = _V231MemorySocket(request.encode("ascii"))
    inbox.InboxHandler(connection, ("127.0.0.1", 40000), server)
    response = HTTPResponse(_V231MemorySocket(connection.outgoing.getvalue()))
    response.begin()
    assert response.status == 403
    assert response.getheader("Content-Security-Policy") == "frame-ancestors 'none'"
    assert response.getheader("X-Frame-Options") == "DENY"
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Cache-Control") == "no-store"
    assert (
        server.api.show_task(operation["operation_id"], "alice")["status"]
        == "awaiting_confirmation"
    )
