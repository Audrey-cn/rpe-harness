"""Provider usage telemetry keeps missing cache fields distinct from reported zero."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


BOOTSTRAP_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/rpe_harness/integrations/hermes_bootstrap.py"
)


def bootstrap():
    assert BOOTSTRAP_PATH.is_file(), "the isolated Hermes bootstrap is missing"
    spec = importlib.util.spec_from_file_location("tested_hermes_bootstrap", BOOTSTRAP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reported_zero_cache_tokens_are_retained_with_their_source_fields():
    usage = {
        "prompt_tokens": 31,
        "completion_tokens": 7,
        "total_tokens": 38,
        "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": None},
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 31,
        "authorization": "must-not-be-captured",
    }
    raw, present = bootstrap().extract_provider_usage(usage)
    assert raw == {
        "prompt_tokens": 31,
        "completion_tokens": 7,
        "total_tokens": 38,
        "prompt_tokens_details": {"cached_tokens": 0},
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 31,
    }
    assert present == [
        "completion_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "prompt_tokens",
        "prompt_tokens_details.cached_tokens",
        "total_tokens",
    ]


def test_sdk_default_zero_is_not_treated_as_a_provider_report():
    details = SimpleNamespace(cached_tokens=0, model_fields_set=set())
    usage = SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=3,
        total_tokens=14,
        prompt_tokens_details=details,
        cache_read_input_tokens=0,
        model_fields_set={
            "prompt_tokens", "completion_tokens", "total_tokens", "prompt_tokens_details"
        },
    )
    raw, present = bootstrap().extract_provider_usage(usage)
    assert raw == {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
    assert "cache_read_input_tokens" not in present
    assert "prompt_tokens_details.cached_tokens" not in present

    details.model_fields_set.add("cached_tokens")
    raw, present = bootstrap().extract_provider_usage(usage)
    assert raw["prompt_tokens_details"] == {"cached_tokens": 0}
    assert "prompt_tokens_details.cached_tokens" in present


def test_nested_cache_usage_is_numeric_allowlisted_and_json_safe():
    raw, present = bootstrap().extract_provider_usage(
        {
            "input_tokens": 100,
            "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 80, "secret": "hidden"},
            "output_tokens_details": {"reasoning_tokens": 2},
            "cache_creation": {"ephemeral_5m_input_tokens": 20},
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": False,
            "total_tokens": float("nan"),
            "prompt_cache_hit_tokens": float("inf"),
            "prompt_cache_miss_tokens": "0",
            "unknown_tokens": 999,
            "full_response": {"content": "hidden"},
        }
    )
    assert raw == {
        "input_tokens": 100,
        "output_tokens": 5,
        "input_tokens_details": {"cached_tokens": 80},
        "output_tokens_details": {"reasoning_tokens": 2},
        "cache_creation": {"ephemeral_5m_input_tokens": 20},
    }
    assert present == [
        "cache_creation.ephemeral_5m_input_tokens",
        "input_tokens",
        "input_tokens_details.cached_tokens",
        "output_tokens",
        "output_tokens_details.reasoning_tokens",
    ]
    json.dumps(raw, allow_nan=False)


def test_augmented_summary_preserves_accounting_and_restricts_response_identity():
    original = {"input_tokens": 11, "output_tokens": 3, "cache_read_tokens": 0}
    response = SimpleNamespace(
        id="chatcmpl-test",
        model="mimo-v2.6-pro",
        provider="xiaomi",
        usage={"prompt_tokens": 11, "completion_tokens": 3},
        choices=[{"message": {"content": "private-content"}}],
        api_key="private-key",
    )
    result = bootstrap().augment_usage_summary(original, response)
    assert {key: result[key] for key in original} == original
    assert original == {"input_tokens": 11, "output_tokens": 3, "cache_read_tokens": 0}
    assert result["raw_provider_usage"] == {"prompt_tokens": 11, "completion_tokens": 3}
    assert result["present_fields"] == ["completion_tokens", "prompt_tokens"]
    assert result["response_identity"] == {
        "id": "chatcmpl-test", "model": "mimo-v2.6-pro", "provider": "xiaomi"
    }
    assert "private" not in json.dumps(result)


def test_missing_usage_retains_identity_without_fabricated_zero_usage():
    result = bootstrap().augment_usage_summary(
        None, SimpleNamespace(id="chatcmpl-no-usage", model="mimo-v2.6-pro", usage=None)
    )
    assert result == {
        "raw_provider_usage": {},
        "present_fields": [],
        "response_identity": {"id": "chatcmpl-no-usage", "model": "mimo-v2.6-pro"},
    }
    assert bootstrap().augment_usage_summary(None, None) is None


def test_runtime_patch_is_idempotent_and_leaves_original_accounting_active():
    class Hooks:
        def __init__(self):
            self.accounted = 0

        def _usage_summary_for_api_request_hook(self, response):
            self.accounted += response.usage["total_tokens"]
            return {"total_tokens": self.accounted, "cache_read_tokens": 0}

    module = bootstrap()
    module.install_usage_telemetry(Hooks)
    module.install_usage_telemetry(Hooks)
    instance = Hooks()
    result = instance._usage_summary_for_api_request_hook(
        SimpleNamespace(id="resp-one", usage={"total_tokens": 17})
    )
    assert instance.accounted == 17
    assert result["total_tokens"] == 17
    assert result["cache_read_tokens"] == 0
    assert result["raw_provider_usage"] == {"total_tokens": 17}
    assert result["present_fields"] == ["total_tokens"]


def test_file_bootstrap_dispatches_cli_after_patch_without_modifying_installation(tmp_path):
    install_root = tmp_path / "installed-hermes"
    agent_dir = install_root / "agent"
    cli_dir = install_root / "hermes_cli"
    agent_dir.mkdir(parents=True)
    cli_dir.mkdir()
    sources = {
        install_root / "hermes_bootstrap.py": "",
        agent_dir / "__init__.py": "",
        cli_dir / "__init__.py": "",
        agent_dir / "api_request_hooks.py": (
            "class ApiRequestHooksMixin:\n"
            "    def _usage_summary_for_api_request_hook(self, response):\n"
            "        return {'cache_read_tokens': 0, 'total_tokens': 9}\n"
        ),
        cli_dir / "main.py": (
            "import json, sys\n"
            "from types import SimpleNamespace\n"
            "from agent.api_request_hooks import ApiRequestHooksMixin\n"
            "def main():\n"
            "    response = SimpleNamespace(id='resp-cli', model='mimo-v2.6-pro',\n"
            "                               usage={'prompt_tokens': 9})\n"
            "    usage = ApiRequestHooksMixin()._usage_summary_for_api_request_hook(response)\n"
            "    print(json.dumps({'argv': sys.argv[1:], 'usage': usage}))\n"
            "    return 7\n"
        ),
    }
    for path, source in sources.items():
        path.write_text(source, encoding="utf-8")
    environment = dict(os.environ, RPE_HERMES_INSTALL_ROOT=str(install_root))
    completed = subprocess.run(
        [sys.executable, "-B", str(BOOTSTRAP_PATH), "chat", "--source", "rpe-v11"],
        env=environment, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 7, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["argv"] == ["chat", "--source", "rpe-v11"]
    assert payload["usage"] == {
        "cache_read_tokens": 0,
        "total_tokens": 9,
        "raw_provider_usage": {"prompt_tokens": 9},
        "present_fields": ["prompt_tokens"],
        "response_identity": {"id": "resp-cli", "model": "mimo-v2.6-pro"},
    }
    assert {path: path.read_text(encoding="utf-8") for path in sources} == sources
    assert not list(install_root.rglob("__pycache__"))


def test_spawn_registry_captures_detached_child_before_short_parent_returns(tmp_path):
    registry = tmp_path / "processes.jsonl"
    code = '''import importlib.util,json,os,pathlib,subprocess,sys,time
spec=importlib.util.spec_from_file_location('private_bootstrap',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
original_class=subprocess.Popen
module.install_process_telemetry();module.install_process_telemetry()
child=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(30)',
                        'never-log-command-secret'],start_new_session=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
rows=[json.loads(line) for line in pathlib.Path(os.environ['RPE_HERMES_PROCESS_PATH']).read_text().splitlines()]
assert subprocess.Popen is original_class and isinstance(child,original_class)
assert len(rows)==1 and rows[0]['pid']==child.pid
assert {'pid','parent_pid','lstart'} <= set(rows[0])
assert rows[0]['parent_pid']==os.getpid()
print(json.dumps(rows[0]),flush=True)
time.sleep(.1)
'''
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code, str(BOOTSTRAP_PATH)],
        env=dict(os.environ, RPE_HERMES_PROCESS_PATH=str(registry)),
        capture_output=True, text=True, timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    record = json.loads(completed.stdout)
    try:
        assert registry.stat().st_mode & 0o777 == 0o600
        assert "never-log" not in registry.read_text()
        identity = subprocess.run(
            ["/bin/ps", "-p", str(record["pid"]), "-o", "lstart="],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        identity = " ".join(identity)
        assert identity == record["lstart"]
        # The owner has already exited, yet its detached child is identifiable.
        assert record["parent_pid"] != record["pid"]
    finally:
        os.kill(record["pid"], 9)


def test_registry_write_failure_kills_spawned_child_without_leaking_error_details(tmp_path):
    code = '''import importlib.util,json,os,subprocess,sys
spec=importlib.util.spec_from_file_location('private_bootstrap',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
spawned=[]
original_init=subprocess.Popen.__init__
def track(self,*args,**kwargs):
    original_init(self,*args,**kwargs)
    if kwargs.get('start_new_session'):
        spawned.append(self)
subprocess.Popen.__init__=track
def failed_sync(fd):
    raise OSError('private-file-secret')
module.os.fsync=failed_sync
module.install_process_telemetry()
try:
    subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(30)',
                      'private-command-secret'],start_new_session=True,
                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
except RuntimeError as exc:
    print(json.dumps({'error':str(exc),'alive':spawned[0].poll() is None}))
else:
    spawned[0].kill();spawned[0].wait()
    raise AssertionError('telemetry failure was ignored')
'''
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code, str(BOOTSTRAP_PATH)],
        env=dict(os.environ, RPE_HERMES_PROCESS_PATH=str(tmp_path / "processes.jsonl")),
        capture_output=True, text=True, timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == {"error": "cannot persist isolated child process identity", "alive": False}
    assert "private-" not in completed.stdout + completed.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS Hermes sandbox")
def test_spawn_registry_works_under_the_actual_launcher_sandbox_profile(tmp_path):
    from rpe_harness.integrations.hermes_session import sandbox_profile

    profile = tmp_path / "hermes.sb"
    profile.write_text(sandbox_profile(
        [tmp_path], Path.home() / ".hermes", Path.home() / ".hermes/hermes-agent",
        read_roots=[Path(sys.executable).parent.parent, Path.home() / ".hermes/tools"],
        native_import_root=BOOTSTRAP_PATH.parents[2],
        owner_read_scope=(tmp_path.parent, [tmp_path]),
    ))
    registry = tmp_path / "processes.jsonl"
    code = '''import importlib.util,json,os,pathlib,subprocess,sys
spec=importlib.util.spec_from_file_location('private_bootstrap',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
module.install_process_telemetry()
child=subprocess.Popen(['/bin/sleep','2'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
    record=json.loads(pathlib.Path(os.environ['RPE_HERMES_PROCESS_PATH']).read_text())
    assert record['pid']==child.pid and child.poll() is None
    assert isinstance(record['start_sec'],int) and 0<=record['start_usec']<1000000
    print(json.dumps({'recorded':True,'identity':bool(record['lstart'])}))
finally:
    child.kill();child.wait()
'''
    completed = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(profile), sys.executable, "-B", "-c", code,
         str(BOOTSTRAP_PATH)],
        cwd=tmp_path, env=dict(os.environ, RPE_HERMES_PROCESS_PATH=str(registry)),
        capture_output=True, text=True, timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"recorded": True, "identity": True}
