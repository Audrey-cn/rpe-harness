#!/usr/bin/env python3
"""100-iteration fault-injection soak harness for RPE-Harness v2.1.0.

Exercises mixed workloads across built-in contracts, HostAPI approvals,
cross-host handoffs, mid-flight cancellations, stale digests, wrong-owner
probes, and oracle rejections while asserting zero leaked staging files,
zero source mutations, and intact SQLite ledgers.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from rpe_harness import __version__  # noqa: E402
from rpe_harness.contract_sdk import ContractPlugin, DEFAULT_REGISTRY  # noqa: E402
from rpe_harness.evolution.sandbox import sandbox_backend, sandbox_capabilities  # noqa: E402
from rpe_harness.host_api import HostAPI, TrustedHostAction  # noqa: E402
from rpe_harness.mcp_server import StdioMCPServer  # noqa: E402
from rpe_harness.runtime import HarnessRuntime  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_soak(iterations: int = 120) -> dict:
    started = time.perf_counter()
    counts: dict[str, int] = {
        'tabular_dedup_ok': 0,
        'tabular_filter_ok': 0,
        'json_normalize_ok': 0,
        'json_patch_ok': 0,
        'mcp_host_full_loop_ok': 0,
        'handoff_invalidation_ok': 0,
        'cancel_fence_ok': 0,
        'stale_digest_rejected': 0,
        'wrong_owner_forbidden': 0,
        'oracle_tamper_blocked': 0,
    }
    failures: list[dict] = []

    tamper_plugin = ContractPlugin(
        capability='soak.tamper',
        executor_id='plugin/soak-tamper',
        input_formats=('txt',),
        validate_options=lambda opts, raw: {},
        execute=lambda plan, raw: (b'TAMPERED', {}),
        verify=lambda plan, raw, staged: {'passed': staged == raw, 'checks': {'intact': staged == raw}},
    )
    DEFAULT_REGISTRY.register(tamper_plugin, replace=True)

    try:
        with tempfile.TemporaryDirectory(prefix='rpe-soak-') as tmp:
            workspace = Path(tmp).resolve()
            db_path = workspace / 'soak.sqlite'
            rt = HarnessRuntime(db_path=db_path)
            api = HostAPI(db_path)
            mcp = StdioMCPServer(db_path=db_path, owner='alice', session_id='soak-s1', workspace=workspace)

            csv_src = workspace / 'orders.csv'
            csv_src.write_text('id,status,code\n001,active,0042\n001,active,0042\n002,paused,0007\n003,active,0099\n', encoding='utf-8')
            json_src = workspace / 'cfg.json'
            json_src.write_text('{ "env" : "prod", "retries" : 3, "debug" : false }\n', encoding='utf-8')
            txt_src = workspace / 'note.txt'
            txt_src.write_text('immutable-payload\n', encoding='utf-8')

            src_hashes = {p.name: _sha256(p) for p in (csv_src, json_src, txt_src)}

            for i in range(iterations):
                mode = i % 10
                try:
                    if mode == 0:
                        out = workspace / f'dedup_{i}.csv'
                        r = rt.run({
                            'schema_version': 2, 'event_id': f'soak-dedup-{i}',
                            'capability': 'tabular.deduplicate', 'goal': 'dedup',
                            'constraints': ['preserve_original', 'preserve_order'],
                            'execution': {'input_path': str(csv_src), 'output_path': str(out), 'key_columns': ['id']},
                        })
                        assert r['status'] == 'succeeded' and out.exists()
                        counts['tabular_dedup_ok'] += 1
                    elif mode == 1:
                        out = workspace / f'filter_{i}.csv'
                        r = rt.run({
                            'schema_version': 2, 'event_id': f'soak-filter-{i}',
                            'capability': 'tabular.filter', 'goal': 'filter active',
                            'constraints': ['preserve_original', 'preserve_order'],
                            'execution': {
                                'input_path': str(csv_src), 'output_path': str(out),
                                'column': 'status', 'operator': 'equals', 'value': 'active',
                            },
                        })
                        assert r['status'] == 'succeeded' and '0042' in out.read_text(encoding='utf-8')
                        counts['tabular_filter_ok'] += 1
                    elif mode == 2:
                        out = workspace / f'norm_{i}.json'
                        r = rt.run({
                            'schema_version': 2, 'event_id': f'soak-norm-{i}',
                            'capability': 'json.normalize', 'goal': 'minify',
                            'constraints': ['preserve_original'],
                            'execution': {'input_path': str(json_src), 'output_path': str(out), 'style': 'minify'},
                        })
                        assert r['status'] == 'succeeded' and out.exists()
                        counts['json_normalize_ok'] += 1
                    elif mode == 3:
                        out = workspace / f'patch_{i}.json'
                        r = rt.run({
                            'schema_version': 2, 'event_id': f'soak-patch-{i}',
                            'capability': 'json.patch', 'goal': 'patch',
                            'constraints': ['preserve_original'],
                            'execution': {
                                'input_path': str(json_src), 'output_path': str(out),
                                'operations': [{'op': 'set', 'key': 'iter', 'value': i}],
                            },
                        })
                        assert r['status'] == 'succeeded' and json.loads(out.read_text(encoding='utf-8'))['iter'] == i
                        counts['json_patch_ok'] += 1
                    elif mode == 4:
                        out_name = f'mcp_out_{i}.json'
                        res = mcp.handle_message({
                            'jsonrpc': '2.0', 'id': i, 'method': 'tools/call',
                            'params': {
                                'name': 'rpe_start_task',
                                'arguments': {
                                    'task': f'Normalize cfg.json to {out_name}, minify and preserve all values.',
                                    'draft': {
                                        'capability': 'json.normalize', 'constraints': [],
                                        'execution': {'input_path': 'cfg.json', 'output_path': out_name, 'style': 'minify'},
                                        'requirements': [
                                            {'text': 'Normalize', 'quote': 'Normalize', 'fields': ['/capability']},
                                            {'text': 'Input', 'quote': 'cfg.json', 'fields': ['/execution/input_path']},
                                            {'text': 'Output', 'quote': out_name, 'fields': ['/execution/output_path']},
                                            {'text': 'Minify', 'quote': 'minify', 'fields': ['/execution/style']},
                                            {'text': 'No extra', 'quote': None, 'fields': ['/constraints']},
                                        ],
                                        'unresolved': [],
                                    },
                                },
                            },
                        })
                        op = res['result']['structuredContent']
                        conf = api.confirm_task(
                            op['operation_id'], op['digest'],
                            TrustedHostAction(owner='alice', operation_id=op['operation_id'],
                                              generation=op['generation'], digest=op['digest']),
                        )
                        done = api.execute_task(op['operation_id'], 'alice', confirmation_id=conf['confirmation_id'])
                        assert done['status'] == 'succeeded' and (workspace / out_name).exists()
                        counts['mcp_host_full_loop_ok'] += 1
                    elif mode == 5:
                        out_name = f'handoff_{i}.json'
                        op = api.start_task(
                            {
                                'raw': f'Normalize cfg.json to {out_name}, minify.',
                                'draft': {
                                    'capability': 'json.normalize', 'constraints': [],
                                    'execution': {'input_path': 'cfg.json', 'output_path': out_name, 'style': 'minify'},
                                    'requirements': [
                                        {'text': 'Normalize', 'quote': 'Normalize', 'fields': ['/capability']},
                                        {'text': 'Input', 'quote': 'cfg.json', 'fields': ['/execution/input_path']},
                                        {'text': 'Output', 'quote': out_name, 'fields': ['/execution/output_path']},
                                        {'text': 'Minify', 'quote': 'minify', 'fields': ['/execution/style']},
                                        {'text': 'No extra', 'quote': None, 'fields': ['/constraints']},
                                    ],
                                    'unresolved': [],
                                },
                            },
                            {'owner': 'alice', 'session_id': 'soak-s1', 'workspace': str(workspace), 'host_id': 'host-a'},
                        )
                        api.confirm_task(
                            op['operation_id'], op['digest'],
                            TrustedHostAction(owner='alice', operation_id=op['operation_id'],
                                              generation=op['generation'], digest=op['digest']),
                        )
                        ho = api.handoff_task(
                            op['operation_id'], 'alice',
                            {'owner': 'bob', 'session_id': 'soak-s2', 'workspace': str(workspace), 'host_id': 'host-b'},
                        )
                        assert ho['confirmation_id'] is None and ho['status'] == 'awaiting_confirmation'
                        counts['handoff_invalidation_ok'] += 1
                    elif mode == 6:
                        out_name = f'cancel_{i}.json'
                        op = api.start_task(
                            {
                                'raw': f'Normalize cfg.json to {out_name}, minify.',
                                'draft': {
                                    'capability': 'json.normalize', 'constraints': [],
                                    'execution': {'input_path': 'cfg.json', 'output_path': out_name, 'style': 'minify'},
                                    'requirements': [
                                        {'text': 'Normalize', 'quote': 'Normalize', 'fields': ['/capability']},
                                        {'text': 'Input', 'quote': 'cfg.json', 'fields': ['/execution/input_path']},
                                        {'text': 'Output', 'quote': out_name, 'fields': ['/execution/output_path']},
                                        {'text': 'Minify', 'quote': 'minify', 'fields': ['/execution/style']},
                                        {'text': 'No extra', 'quote': None, 'fields': ['/constraints']},
                                    ],
                                    'unresolved': [],
                                },
                            },
                            {'owner': 'alice', 'session_id': 'soak-s1', 'workspace': str(workspace)},
                        )
                        cancelled = api.cancel_task(
                            op['operation_id'], op['generation'], owner='alice', reason='soak-cancel',
                        )
                        assert cancelled['status'] == 'cancelled' and not (workspace / out_name).exists()
                        counts['cancel_fence_ok'] += 1
                    elif mode == 7:
                        out_name = f'stale_{i}.json'
                        op = api.start_task(
                            {'raw': f'Normalize cfg.json to {out_name}, minify.'},
                            {'owner': 'alice', 'session_id': 'soak-s1', 'workspace': str(workspace)},
                        )
                        stale = api.confirm_task(
                            op['operation_id'], '0' * 64,
                            TrustedHostAction(owner='alice', operation_id=op['operation_id'],
                                              generation=op['generation'], digest='0' * 64),
                        )
                        assert stale['status'] == 'stale'
                        counts['stale_digest_rejected'] += 1
                    elif mode == 8:
                        out_name = f'owner_{i}.json'
                        op = api.start_task(
                            {'raw': f'Normalize cfg.json to {out_name}, minify.'},
                            {'owner': 'alice', 'session_id': 'soak-s1', 'workspace': str(workspace)},
                        )
                        denied = api.show_task(op['operation_id'], 'eve')
                        assert denied['status'] == 'forbidden'
                        counts['wrong_owner_forbidden'] += 1
                    else:
                        out = workspace / f'tamper_{i}.txt'
                        r = rt.run({
                            'schema_version': 2, 'event_id': f'soak-tamper-{i}',
                            'capability': 'soak.tamper', 'goal': 'must block tampered output',
                            'execution': {'input_path': str(txt_src), 'output_path': str(out)},
                        })
                        assert r['status'] == 'failed' and not out.exists()
                        counts['oracle_tamper_blocked'] += 1
                except Exception as exc:
                    failures.append({'iteration': i, 'mode': mode, 'error': f'{type(exc).__name__}: {exc}'})

            # Post-soak invariants
            leaked_staging = [p.name for p in workspace.glob('.rpe-*')]
            sources_intact = all(_sha256(workspace / name) == digest for name, digest in src_hashes.items())
            with sqlite3.connect(db_path) as conn:
                integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
                active_leases = conn.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0]

            elapsed = round(time.perf_counter() - started, 3)
            return {
                'version': __version__,
                'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                'platform': platform.platform(),
                'python': sys.version.split()[0],
                'sandbox_backend': sandbox_backend(),
                'sandbox_capabilities': sandbox_capabilities(),
                'iterations': iterations,
                'elapsed_seconds': elapsed,
                'throughput_ops_per_sec': round(iterations / max(elapsed, 0.001), 2),
                'counts': counts,
                'failures': failures,
                'invariants': {
                    'zero_failures': len(failures) == 0,
                    'zero_leaked_staging_files': len(leaked_staging) == 0,
                    'leaked_staging_files': leaked_staging,
                    'all_sources_byte_intact': sources_intact,
                    'sqlite_integrity_check': integrity,
                    'zero_stuck_running_runs': active_leases == 0,
                },
                'passed': (
                    len(failures) == 0
                    and len(leaked_staging) == 0
                    and sources_intact
                    and integrity == 'ok'
                    and active_leases == 0
                ),
            }
    finally:
        DEFAULT_REGISTRY.unregister('soak.tamper')


def main() -> int:
    report = run_soak(120)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
