"""Cross-platform OS containment backend tests (macOS seatbelt, Linux bwrap, Windows Job Object)."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from rpe_harness.evolution.sandbox import (
    CandidateExecutionError,
    _bwrap_command,
    run_candidate,
    sandbox_backend,
    sandbox_capabilities,
)


def test_sandbox_backend_and_honest_capabilities():
    backend = sandbox_backend()
    caps = sandbox_capabilities()
    assert caps['backend'] == backend
    if sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file():
        assert backend == 'macos-sandbox-exec'
        assert caps['network'] == 'denied'
    elif sys.platform == 'win32':
        assert backend == 'windows-job-object'
        assert caps['memory'] == 'job_object_hard_process_limit'
        assert caps['filesystem'] == 'host_acl_only'
    elif sys.platform.startswith('linux') and shutil.which('bwrap'):
        assert backend == 'linux-bwrap'
        assert caps['network'] == 'unshare_net_denied'


def test_bwrap_command_construction_when_available(tmp_path: Path):
    if not sys.platform.startswith('linux') or not shutil.which('bwrap'):
        pytest.skip('Linux bwrap only')
    cmd = _bwrap_command(tmp_path, tmp_path / 'driver.py', tmp_path / 'candidate.py')
    assert '--unshare-net' in cmd
    assert '--die-with-parent' in cmd
    assert '--bind' in cmd


def test_sandbox_runs_valid_candidate_and_enforces_timeout(tmp_path: Path):
    backend = sandbox_backend()
    if backend == 'unavailable':
        pytest.skip('No OS sandbox backend on this host')

    src = tmp_path / 'in.json'
    raw = b'{"a": 1, "b": [2, 3]}\n'
    src.write_bytes(raw)
    plan = {
        'capability': 'json.normalize',
        'input_path': str(src),
        'output_path': str(tmp_path / 'out.json'),
        'input_format': 'json',
        'input_sha256': hashlib.sha256(raw).hexdigest(),
        'style': 'minify',
    }
    good_code = (
        b"import json\n"
        b"from pathlib import Path\n"
        b"def prepare(plan, staging_path):\n"
        b"    doc = json.loads(Path(plan['input_path']).read_text(encoding='utf-8'))\n"
        b"    Path(staging_path).write_text(json.dumps(doc, separators=(',', ':')), encoding='utf-8')\n"
    )
    try:
        staged = run_candidate(good_code, plan, tmp_path, timeout=5.0)
    except CandidateExecutionError as exc:
        # GitHub-hosted Ubuntu 24.04 runners may restrict unprivileged userns via AppArmor.
        msg = str(exc).lower()
        if backend == 'linux-bwrap' and ('namespace' in msg or 'operation not permitted' in msg or 'uid map' in msg):
            pytest.skip(f'Host kernel/AppArmor restricts unprivileged bwrap userns: {exc}')
        raise
    assert json.loads(staged.decode('utf-8')) == {'a': 1, 'b': [2, 3]}

    # Timeout containment check
    hang_code = (
        b"import time\n"
        b"def prepare(plan, staging_path):\n"
        b"    time.sleep(10)\n"
    )
    with pytest.raises(CandidateExecutionError, match='timeout'):
        run_candidate(hang_code, plan, tmp_path, timeout=0.5)
