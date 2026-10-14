"""Explicit, bounded Hermes generation. Only later independent acceptance can approve code."""
from __future__ import annotations

import os
import pwd
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from ..integrations.hermes_session import run_hermes_session
from ..path_binding import _directory, read_regular_file
from ..storage import RunStore, canonical_json
from .candidates import propose_candidate
from .cycle import inspect_verified_feedback
from .store import digest


_CAPABILITIES = {'json_executor': 'json.normalize', 'csv_executor': 'tabular.deduplicate'}
_ABI = {
    'json_executor': {
        'capability': 'json.normalize', 'input_format': 'json',
        'plan_fields': ['capability', 'input_format', 'input_path', 'style'],
        'styles': ['minify', 'pretty'],
        'rules': ['Use strict UTF-8 without BOM; reject invalid JSON and duplicate decoded object keys.',
                  'Preserve the exact raw string and number token spelling and their order.',
                  'minify removes whitespace between tokens; pretty uses two-space indentation, '
                  'line breaks after container openings and commas, and a space after each colon.',
                  'An empty container stays on one line. Both styles end with exactly one LF.',
                  'Reject nonfinite values, unpaired surrogates, and unsupported styles.'],
        'limits': {'input_bytes': 1048576, 'output_bytes': 16777216, 'depth': 64,
                   'tokens': 100000, 'number_characters': 256},
    },
    'csv_executor': {
        'capability': 'tabular.deduplicate', 'input_format': 'csv',
        'plan_fields': ['capability', 'input_format', 'input_path', 'key_columns',
                        'encoding', 'delimiter', 'keep'],
        'keep': 'first',
        'rules': ['Keep the first record for each tuple of exact key-column string values.',
                  'Preserve headers, raw cell values, retained record order, encoding and delimiter.',
                  'Do not trim, coerce numbers, or fold case. Handle quoted commas and multiline cells.',
                  'Reject malformed rows, duplicate headers, missing key columns, empty key values, '
                  'and unsupported keep policies.'],
        'limits': {'input_bytes': 16777216, 'output_bytes': 16777216},
    },
}
_USAGE_FIELDS = frozenset({'api_requests', 'missing_observations', 'requests_with_usage',
    'api_errors', 'coverage_complete', 'known_total_tokens', 'prompt_tokens', 'output_tokens',
    'input_tokens', 'total_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens'})


def _deny_read_roots():
    import_root = Path(__file__).resolve().parents[2]
    current = import_root.parent if import_root.name == 'src' else import_root / 'rpe_harness'
    # The OS account home remains stable when test/host HOME is isolated.
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    primary = account_home / '项目' / 'rpe-harness'
    return list(dict.fromkeys([current, primary, account_home / '.codex' / 'worktrees']))


def _feedback(db_path, capability):
    if not db_path.exists():
        return []
    runs = RunStore.read_only(db_path)
    input_format = 'json' if capability == 'json.normalize' else 'csv'
    with runs.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone():
            return []
        identifiers = [row[0] for row in db.execute(
            "SELECT DISTINCT r.run_id,r.event_fk FROM runs r JOIN steps s ON s.run_id=r.run_id "
            "WHERE r.status='succeeded' AND r.verification_ok=1 AND s.stage='plan' "
            "AND json_extract(s.evidence_json,'$.plan.capability')=? "
            "AND json_extract(s.evidence_json,'$.plan.input_format')=? "
            'ORDER BY r.event_fk DESC LIMIT 100', (capability, input_format))]
    result = []
    for identifier in identifiers:
        try:
            item = inspect_verified_feedback(identifier, db_path)
        except (ValueError, OSError, KeyError):
            continue
        if item is None or (item['plan']['capability'], item['plan']['input_format']) != (capability, input_format):
            continue
        run = runs.get(identifier)
        verification = run['verification']
        metrics = {key: verification[key] for key in
                   ('input_rows', 'output_rows', 'removed_rows', 'token_count')
                   if type(verification.get(key)) is int and verification[key] >= 0}
        public = {'verified': True, 'input_sha256': item['input_sha256'],
                  'output_sha256': item['output_sha256'], 'plan_sha256': item['plan_sha256'],
                  'verification_sha256': item['verification_sha256'], 'metrics': metrics}
        if capability == 'json.normalize':
            public['style'] = item['plan']['style']
        else:
            public['key_column_count'] = len(item['plan']['key_columns'])
        result.append((item, public))
        if len(result) == 8:
            break
    return result


def _usage(value):
    if not isinstance(value, dict):
        return None
    # Missing cost fields remain unknown; error messages and provider payloads
    # never enter the compact durable generation report.
    return {key: item for key, item in value.items() if key in _USAGE_FIELDS
            and (item is None or type(item) in {int, bool})}


def _identity(value):
    if not isinstance(value, dict):
        return None
    result = {key: value[key][:256] for key in ('provider', 'requested_model')
              if isinstance(value.get(key), str)}
    result['response_models'] = [item[:256] for item in value.get('response_models', [])[:16]
                                 if isinstance(item, str)]
    return result


def _cleanup(value):
    if not isinstance(value, dict):
        return None
    keys = {'cleanup_complete', 'external_executor_cleanup_complete', 'tracked_descendants',
            'signaled_descendants', 'external_executor_tracked_children'}
    result = {key: item for key, item in value.items() if key in keys and type(item) in {bool, int}}
    for key in ('remaining_descendants', 'external_executor_remaining_children'):
        items = value.get(key)
        result[key] = [item for item in items if type(item) is int][:64] if isinstance(items, list) else None
    errors = value.get('errors')
    result['errors'] = [{key: item[key][:128] for key in ('stage', 'error_type')
                         if isinstance(item.get(key), str)}
                        for item in errors[:32] if isinstance(item, dict)] if isinstance(errors, list) else None
    return result


def _host_error(result):
    facts = []
    for event in result.get('events', []):
        if not isinstance(event, dict) or event.get('kind') != 'api_request_error':
            continue
        item = {key: event[key][:128] for key in ('error_code', 'error_type')
                if isinstance(event.get(key), str)}
        if type(event.get('status_code')) is int:
            item['status_code'] = event['status_code']
        facts.append(item)
    return {'code': 'generation_timed_out' if result.get('timed_out') else 'generation_failed',
            'exit_code': result.get('exit_code'), 'api_errors': facts[:32],
            'observer_errors': result.get('observer_errors')}


def _write_report(root, report):
    directory = root / 'evidence' / 'worker'
    for path in (root / 'evidence', directory):
        if path.is_symlink():
            raise ValueError('worker report directory must not be a symlink')
        path.mkdir(exist_ok=True)
    fd = _directory(directory)
    name = report['worker_id'] + '.json'
    report['report_path'] = str(directory / name)
    try:
        destination = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=fd)
        with os.fdopen(destination, 'wb') as stream:
            stream.write(canonical_json(report).encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(fd)
    finally:
        os.close(fd)


def run_proposal_worker(db_path, owned_root, *, kind='json_executor', timeout=120, hermes_root=None):
    """Generate one untrusted proposal from at most eight fresh real feedback records.

    The worker can emit existing registered producer code; it cannot validate,
    activate, change models, or give model tools the real feedback database.
    """
    if not isinstance(kind, str) or kind not in _CAPABILITIES:
        raise ValueError('worker kind must be csv_executor or json_executor')
    if type(timeout) is not int or not 1 <= timeout <= 180:
        raise ValueError('worker timeout must be an integer between 1 and 180 seconds')
    root = Path(os.path.abspath(Path(owned_root).expanduser()))
    database = Path(os.path.abspath(Path(db_path).expanduser()))
    if root.is_symlink() or database.is_symlink() or not database.is_relative_to(root):
        raise ValueError('worker database must be inside its owned_root without symlinks')
    if not root.exists():
        parent = _directory(root.parent)
        os.close(parent)
        root.mkdir(mode=0o700)
    descriptor = _directory(root)
    os.close(descriptor)
    if database.exists():
        with RunStore.read_only(database).connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='evolution_meta'").fetchone():
                saved = db.execute("SELECT value FROM evolution_meta WHERE key='owned_root'").fetchone()
                if saved is not None and Path(saved[0]) != root:
                    raise ValueError('worker database already belongs to another owned_root')
    started = time.monotonic()
    report = {'worker_id': uuid.uuid4().hex, 'kind': kind, 'timeout_seconds': timeout,
              'completed': False, 'proposal_id': None, 'candidate_sha256': None,
              'evidence_run_ids': [], 'session_id': None, 'usage': None, 'model_identity': None,
              'elapsed_seconds': None, 'process_cleanup': None, 'error': None,
              'workspace_cleaned': True}
    feedback = _feedback(database, _CAPABILITIES[kind])
    if not feedback:
        report['error'] = {'code': 'no_verified_feedback'}
    else:
        report['evidence_run_ids'] = [item['run_id'] for item, _ in feedback]
        packet = {'goal': 'Implement the registered producer ABI from verified public feedback.',
                  'kind': kind, 'contract': _ABI[kind],
                  'verified_feedback': [public for _, public in feedback]}
        report['feedback_summary_sha256'] = digest(packet)
        worker_root = Path(tempfile.mkdtemp(prefix='proposal-worker-', dir=root))
        workspace = worker_root / 'workspace'
        content = None
        report['workspace_cleaned'] = False
        try:
            workspace.mkdir(mode=0o700)
            (workspace / 'feedback.json').write_text(canonical_json(packet), encoding='utf-8')
            prompt = ('Read feedback.json in the current workspace. Write a standalone Python '
                'producer to candidate.py, defining prepare(plan, staging_path). Use only Python '
                'standard-library imports; do not import rpe_harness or read other project files. '
                'The function reads plan["input_path"] and writes only staging_path, leaving the '
                'source unchanged. Reject invalid or unsupported input by raising ValueError '
                'before creating staging_path. Do not overwrite an existing staging file. '
                'Follow the public contract and bounded limits exactly. Generate code only; '
                'this task does not authorize approval or activation.')
            result = run_hermes_session(prompt, workspace, worker_root / 'host.sqlite',
                tool_enabled=False, owner_root=worker_root, timeout=timeout,
                hermes_root=hermes_root, entry_mode='optional',
                read_isolation_root=root, deny_read_roots=_deny_read_roots())
            report.update(session_id=result.get('session_id'), usage=_usage(result.get('usage')),
                          model_identity=_identity(result.get('model_identity')),
                          process_cleanup=_cleanup(result.get('process_cleanup')))
            cleanup = result.get('process_cleanup') or {}
            if (cleanup.get('cleanup_complete') is not True or cleanup.get('remaining_descendants') != []
                    or cleanup.get('external_executor_cleanup_complete') is not True
                    or cleanup.get('external_executor_remaining_children') != [] or cleanup.get('errors') != []):
                report['error'] = {'code': 'process_cleanup_incomplete'}
            elif result.get('completed') is not True or result.get('timed_out') or result.get('exit_code') != 0:
                report['error'] = _host_error(result)
            else:
                try:
                    raw = read_regular_file(workspace / 'candidate.py', max_bytes=65536)
                    if not raw:
                        raise ValueError('empty candidate')
                    report['candidate_sha256'] = digest(raw)
                    content = raw.decode('utf-8')
                except (ValueError, OSError, UnicodeError) as error:
                    report['error'] = {'code': 'invalid_candidate', 'type': type(error).__name__}
        except Exception as error:
            report['error'] = {'code': 'launcher_failed', 'type': type(error).__name__,
                               'message_sha256': digest(str(error).encode())}
        finally:
            try:
                shutil.rmtree(worker_root)
                report['workspace_cleaned'] = not worker_root.exists()
            except OSError as error:
                report['error'] = {'code': 'workspace_cleanup_failed', 'type': type(error).__name__}
        if content is not None and report['error'] is None and report['workspace_cleaned']:
            # Long-running generation cannot silently change its source evidence.
            for item, _ in feedback:
                try:
                    unchanged = inspect_verified_feedback(item['run_id'], database)
                except (ValueError, OSError, KeyError):
                    unchanged = None
                if unchanged is None or digest(unchanged) != digest(item):
                    report['error'] = {'code': 'verified_feedback_changed'}
                    break
            if report['error'] is None:
                try:
                    proposal = propose_candidate(database, root, component='executor', kind=kind,
                        content=content, evidence_run_ids=report['evidence_run_ids'])
                    report.update(completed=True, proposal_id=proposal['proposal_id'])
                except (ValueError, OSError, KeyError) as error:
                    report['error'] = {'code': 'proposal_failed', 'type': type(error).__name__}
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    _write_report(root, report)
    return report
