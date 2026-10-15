"""Opt-in native IO telemetry. No tool registration, execution or controller imports."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import threading

_IDS = ('session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id', 'tool_name')
_TOOLS = frozenset({'terminal', 'write_file', 'patch'})
_LIMIT = 65536


def evaluate_artifact_receipt(receipt, config, key):
    """Authenticate one actual IO transition and its current bytes, without an oracle.

    This is also usable at the trusted post-tool boundary to stop a completed
    native session. It does not establish business correctness or full API links.
    """
    verdict = {'verified': False, 'receipt_authenticated': False, 'errors': []}
    try:
        from .hermes_native_process import _digest, file_snapshot, receipt_signature
        if (type(config) is not dict or set(config) != {'workspace', 'artifact_name'}
                or config != native_evidence_config(config['workspace'], {'artifact_name': config['artifact_name']})):
            raise ValueError('native_artifact_configuration_changed')
        if type(receipt) is not dict or receipt.get('kind') != 'native_artifact_observation':
            raise ValueError('native_artifact_receipt_missing')
        signature = receipt.get('artifact_receipt_hmac_sha256')
        body = {name: value for name, value in receipt.items() if name not in {
            'kind', 'received_at', 'source_event_id', 'ingest_event_id', 'artifact_receipt_hmac_sha256'}}
        if (type(key) is not bytes or len(key) != 32 or not isinstance(signature, str)
                or not hmac.compare_digest(signature, receipt_signature(body, key))):
            raise ValueError('native_artifact_receipt_authentication_failed')
        verdict['receipt_authenticated'] = True
        if (receipt.get('artifact_config_sha256') != _digest(config)
                or receipt.get('status') != 'created' or receipt.get('verified') is not True
                or receipt.get('overlap') is not False or receipt.get('tool_name') not in _TOOLS
                or receipt.get('tool_status') not in {'ok', 'succeeded'}
                or any(not isinstance(receipt.get(name), str) or not receipt[name] for name in _IDS)
                or not isinstance(receipt.get('arguments_sha256'), str)
                or not re.fullmatch('[a-f0-9]{64}', receipt['arguments_sha256'])):
            raise ValueError('native_artifact_receipt_configuration_or_call_mismatch')
        if receipt['tool_name'] == 'terminal' and (type(receipt.get('tool_exit_code')) is not int
                or receipt['tool_exit_code'] != 0):
            raise ValueError('native_artifact_terminal_not_completed')
        target = Path(config['workspace'])/config['artifact_name']
        output = file_snapshot(target)
        if (output.get('state') != 'regular' or output.get('sha256') != receipt.get('content_sha256')
                or output.get('size') != receipt.get('bytes_written')
                or _digest(output.get('identity')) != receipt.get('output_identity_sha256')
                or hashlib.sha256(str(target).encode()).hexdigest() != receipt.get('target_path_sha256')):
            raise ValueError('native_artifact_final_output_changed')
        verdict.update(verified=True, output_sha256=output['sha256'], bytes_written=output['size'])
    except (OSError, ValueError, TypeError, KeyError) as error:
        verdict['errors'] = [str(error) if isinstance(error, ValueError) else type(error).__name__]
    return verdict


def _snapshot(path):
    """Never follow links; a receipt observes bounded regular bytes only."""
    path = Path(path)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        return {'state': 'invalid'}
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return {'state': 'absent'}
    except OSError:
        return {'state': 'invalid'}
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _LIMIT:
            return {'state': 'invalid'}
        data = b''
        while len(data) <= _LIMIT:
            chunk = os.read(fd, min(8192, _LIMIT + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        after = os.fstat(fd)
        def identity(value):
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if len(data) > _LIMIT or identity(before) != identity(after):
            return {'state': 'invalid'}
        return {'state': 'regular', 'sha256': hashlib.sha256(data).hexdigest(),
                'size': len(data), 'identity': identity(after)}
    except OSError:
        return {'state': 'invalid'}
    finally:
        os.close(fd)


def native_evidence_config(workspace, options, snapshot=None):
    """Caller chooses a fixed filename/skill ID; paths come from owned materialization."""
    if type(options) is dict and 'artifact_name' in options:
        if set(options) != {'artifact_name'} or options['artifact_name'] not in {'output.csv', 'output.json'}:
            raise ValueError('native business evidence observes only a fixed output filename')
        return {'workspace': str(Path(workspace).resolve(strict=True)), **options}
    if type(options) is not dict or set(options) - {'draft_name', 'cold_skill_id'}:
        raise ValueError('invalid native evidence options')
    if options.get('draft_name') != 'draft.json':
        raise ValueError('native evidence observes only draft.json')
    config = {'workspace': str(Path(workspace).resolve(strict=True)), 'draft_name': 'draft.json'}
    if options.get('cold_skill_id'):
        if type(snapshot) is not dict:
            raise ValueError('cold native evidence requires a materialized snapshot')
        matches = [row for row in snapshot['entries'] if row['skill_id'] == options['cold_skill_id']]
        if len(matches) != 1 or not matches[0].get('archive_path'):
            raise ValueError('native evidence skill is not a unique cold copy')
        row = matches[0]
        root, name = Path(snapshot['materialized_path']), row['native_load_name']
        if (not root.is_absolute() or root.name != 'skills' or not re.fullmatch(r'[\w.-]{1,160}', name)
                or Path(row['archive_path']) != root/'.archive'/name
                or Path(row['native_skill_path']) != root/name/'SKILL.md'):
            raise ValueError('native cold paths escaped their materialized snapshot')
        config['cold'] = {'skill_id': row['skill_id'], 'snapshot_id': snapshot['snapshot_id'],
            'name': row['native_load_name'], 'archive_path': row['archive_path'],
            'restored_path': str(Path(row['native_skill_path']).parent),
            'skill_sha256': row['skill_sha256'], 'script_sha256': row['script_sha256']}
    return config


class NativeEvidenceObserver:
    """Hash before/after IO at real tool hooks; overlapping calls are never attributed."""
    def __init__(self, config):
        artifact = type(config) is dict and 'artifact_name' in config
        if (type(config) is not dict or (set(config) != {'workspace', 'artifact_name'} if artifact
                else set(config) - {'workspace', 'draft_name', 'cold'})):
            raise ValueError('invalid native evidence configuration')
        self.workspace = Path(config['workspace'])
        if (not self.workspace.is_absolute() or not self.workspace.is_dir()
                or self.workspace.resolve() != self.workspace
                or (config['artifact_name'] not in {'output.csv', 'output.json'} if artifact
                    else config.get('draft_name') != 'draft.json')):
            raise ValueError('native evidence requires an owned workspace and fixed draft filename')
        self.artifact = artifact
        self.config = dict(config)
        self.target = self.workspace/(config['artifact_name'] if artifact else 'draft.json')
        self.cold = config.get('cold')
        if self.cold:
            expected = {'skill_id', 'snapshot_id', 'name', 'archive_path', 'restored_path',
                        'skill_sha256', 'script_sha256'}
            if type(self.cold) is not dict or set(self.cold) != expected:
                raise ValueError('invalid native cold evidence configuration')
            archive, restored = Path(self.cold['archive_path']), Path(self.cold['restored_path'])
            if (not archive.is_absolute() or not restored.is_absolute()
                    or archive.parent != restored.parent/'.archive'
                    or archive.name != restored.name or restored.name != self.cold['name']
                    or restored.parent.name != 'skills'
                    or any(not re.fullmatch('[a-f0-9]{64}', self.cold[key])
                           for key in ('skill_sha256', 'script_sha256'))):
                raise ValueError('invalid native cold snapshot paths or identities')
        self.pending, self.lock = {}, threading.RLock()

    def _target_snapshot(self):
        if self.artifact:
            from .hermes_native_process import file_snapshot
            return file_snapshot(self.target)
        return _snapshot(self.target)

    def _cold_snapshot(self):
        if not self.cold:
            return None
        return {place: {name: _snapshot(Path(self.cold[place+'_path'])/relative)
                       for name, relative in [('skill', 'SKILL.md'), ('helper', 'scripts/xlsx_read.py')]}
                for place in ('archive', 'restored')}

    def before(self, payload):
        if payload.get('tool_name') not in _TOOLS:
            return
        identity = tuple(payload.get(key) for key in _IDS)
        if any(not isinstance(value, str) or not value for value in identity):
            return
        with self.lock:
            overlap = bool(self.pending)
            for item in self.pending.values():
                item['overlap'] = True
            if self.artifact and identity in self.pending:
                return
            self.pending[identity] = {'draft': self._target_snapshot(),
                                      'cold': self._cold_snapshot(), 'overlap': overlap}

    def after(self, payload):
        identity = tuple(payload.get(key) for key in _IDS)
        with self.lock:
            before = self.pending.pop(identity, None)
            if before is None:
                return []
            after = self._target_snapshot()
            base = {key: payload[key] for key in _IDS}
            base['arguments_sha256'] = payload.get('arguments_sha256')
            try:
                outcome = json.loads(payload.get('result', ''))
            except (ValueError, TypeError):
                outcome = None
            successful = (payload.get('status') in {'ok', 'succeeded'}
                          and type(outcome) is dict and not outcome.get('error') and not before['overlap']
                          and (payload['tool_name'] != 'terminal' or
                               type(outcome.get('exit_code')) is int and outcome['exit_code'] == 0))
            if self.artifact and type(outcome) is dict:
                successful = successful and outcome.get('success') is not False and outcome.get('verified') is not False
            rows = []
            if before['draft'] != after or after['state'] == 'invalid':
                status = ('created' if before['draft']['state'] == 'absent' and after['state'] == 'regular'
                          else 'invalid' if after['state'] == 'invalid' else 'modified')
                receipt = {**base, 'kind': 'native_artifact_observation', 'status': status,
                    'verified': bool(successful and after['state'] == 'regular'),
                    'content_sha256': after.get('sha256'), 'bytes_written': after.get('size'),
                    'target_path_sha256': hashlib.sha256(str(self.target).encode()).hexdigest()}
                if self.artifact:
                    from .hermes_native_process import _digest, sign_native_receipt
                    receipt.update(artifact_config_sha256=_digest(self.config),
                        overlap=before['overlap'], tool_status=payload.get('status'),
                        output_identity_sha256=_digest(after['identity']) if 'identity' in after else None,
                        artifact_binding='new_file_during_single_native_tool_call')
                    if payload['tool_name'] == 'terminal':
                        receipt['tool_exit_code'] = outcome.get('exit_code') if type(outcome) is dict else None
                    signature = sign_native_receipt(receipt, 'artifact_receipt_hmac_sha256')
                    if signature is None:
                        receipt['verified'] = False
                    else:
                        receipt['artifact_receipt_hmac_sha256'] = signature
                rows.append(receipt)
            cold_after = self._cold_snapshot()
            if self.cold and before['cold'] != cold_after:
                valid = all(before['cold']['archive'][name].get('sha256') == self.cold[key]
                    and before['cold']['restored'][name]['state'] == 'absent'
                    and cold_after['archive'][name]['state'] == 'absent'
                    and cold_after['restored'][name].get('sha256') == self.cold[key]
                    for name, key in [('skill', 'skill_sha256'), ('helper', 'script_sha256')])
                rows.append({**base, 'kind': 'native_cold_observation', 'status': 'restored' if valid else 'changed',
                    'verified': bool(successful and valid), **{key: self.cold[key] for key in
                        ('skill_id', 'snapshot_id', 'skill_sha256', 'script_sha256')}})
            return rows
