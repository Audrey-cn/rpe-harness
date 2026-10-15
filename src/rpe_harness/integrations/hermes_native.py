"""Opt-in native IO telemetry. No tool registration, execution or controller imports."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading

_IDS = ('session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id', 'tool_name')
_TOOLS = frozenset({'terminal', 'write_file', 'patch'})
_LIMIT = 65536


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
        if type(config) is not dict or set(config) - {'workspace', 'draft_name', 'cold'}:
            raise ValueError('invalid native evidence configuration')
        self.workspace = Path(config['workspace'])
        if (not self.workspace.is_absolute() or not self.workspace.is_dir()
                or self.workspace.resolve() != self.workspace or config.get('draft_name') != 'draft.json'):
            raise ValueError('native evidence requires an owned workspace and fixed draft filename')
        self.target = self.workspace/'draft.json'
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
            self.pending[identity] = {'draft': _snapshot(self.target),
                                      'cold': self._cold_snapshot(), 'overlap': overlap}

    def after(self, payload):
        identity = tuple(payload.get(key) for key in _IDS)
        with self.lock:
            before = self.pending.pop(identity, None)
            if before is None:
                return []
            after = _snapshot(self.target)
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
            rows = []
            if before['draft'] != after or after['state'] == 'invalid':
                status = ('created' if before['draft']['state'] == 'absent' and after['state'] == 'regular'
                          else 'invalid' if after['state'] == 'invalid' else 'modified')
                rows.append({**base, 'kind': 'native_artifact_observation', 'status': status,
                    'verified': bool(successful and after['state'] == 'regular'),
                    'content_sha256': after.get('sha256'), 'bytes_written': after.get('size'),
                    'target_path_sha256': hashlib.sha256(str(self.target).encode()).hexdigest()})
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
