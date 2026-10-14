import argparse
import json
import os
import subprocess
import sys

import pytest


def parser():
    from rpe_harness.source_cli import register_source
    result = argparse.ArgumentParser()
    register_source(result.add_subparsers(dest='command', required=True))
    return result


def test_source_cli_prepares_and_shows_source_bound_handoff_without_execution(tmp_path):
    from rpe_harness.source_cli import dispatch_source
    (tmp_path / 'input.json').write_text('[1, 2]')
    request = tmp_path / 'source.json'
    request.write_text(json.dumps({'capability': 'json.normalize', 'constraints': [],
        'execution': {'input_path': 'input.json', 'output_path': 'out.json', 'style': 'minify'}}))
    db = tmp_path / 'state.sqlite'
    handoff = dispatch_source(parser().parse_args(['source', 'prepare', '--db', str(db),
        '--workspace', str(tmp_path), '--session-id', 's', '--source-ref', 'r', '--source', str(request)]))
    assert handoff['status'] == 'ready' and len(handoff['requirements_sha256']) == 64
    assert not (tmp_path / 'out.json').exists()
    assert dispatch_source(parser().parse_args(['source', 'show', handoff['state_id'],
        '--db', str(db), '--session-id', 's'])) == handoff


def test_source_cli_preserves_free_text_refusal_and_does_not_create_business_state(tmp_path):
    from rpe_harness.source_cli import dispatch_source
    request = tmp_path / 'request.txt'
    request.write_text('压缩JSON，并删除退款交易')
    result = dispatch_source(parser().parse_args(['source', 'prepare', '--db', str(tmp_path / 'state.sqlite'),
        '--workspace', str(tmp_path), '--session-id', 's', '--source-ref', 'r', '--source', str(request)]))
    assert result['status'] == 'needs_context'
    assert result['source']['raw'] == request.read_text()
    assert not (tmp_path / 'state.sqlite').exists()


def test_source_cli_has_no_independent_run_authorization():
    with pytest.raises(SystemExit):
        parser().parse_args(['source', 'run', 'invented-state'])


def test_source_file_read_refuses_fifo_without_waiting_for_a_writer(tmp_path):
    fifo = tmp_path / 'request.fifo'
    os.mkfifo(fifo)
    completed = subprocess.run([sys.executable, '-B', '-c',
        'from rpe_harness.source_cli import _read_source; import sys; _read_source(sys.argv[1])',
        str(fifo)], capture_output=True, text=True, timeout=2)
    assert completed.returncode != 0
    assert 'ValueError' in completed.stderr and 'regular' in completed.stderr
