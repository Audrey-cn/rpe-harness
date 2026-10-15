"""Bounded local skill snapshots. Search is advisory; adapters are explicitly trusted."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

MAX_SKILLS = 2048
MAX_FILE = 65536
XLSX_SKILL_ID = 'productivity/xlsx'
XLSX_SKILL_SHA256 = 'b302d7ac997843a757ab3c11513ea012889ebdae32f842edb44faa9a4485f19b'
XLSX_SCRIPT_SHA256 = '5ab31b1b417efea11a8af7b15c6aa5f26d6d966effefaa19af604c1fa0ca0916'
BINDING_FIELDS = {'snapshot_id', 'skill_id', 'skill_sha256', 'script_sha256', 'script_path'}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _absolute(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    if '..' in Path(value).parts:
        raise ValueError('paths cannot contain traversal')
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('paths cannot contain symlinks')
    return path


def _read(path, limit=MAX_FILE):
    from .path_binding import _directory, _read_descriptor
    path = _absolute(path)
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('source must be a regular file')
        return _read_descriptor(fd, limit)
    finally:
        os.close(fd)


def _scalar(text):
    text = text.strip()
    if text.startswith('"'):
        try:
            return json.loads(text)
        except ValueError:
            # YAML quoted strings can contain escapes outside JSON's subset.
            return text[1:-1] if text.endswith('"') else text
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    return text


def _metadata(raw, fallback):
    """Read a conservative metadata subset, never interpret YAML tags or skill templates."""
    text = raw.decode('utf-8', errors='strict')
    if not text.startswith('---\n') or '\n---' not in text[4:]:
        raise ValueError('skill requires delimited frontmatter')
    header = text[4:].split('\n---', 1)[0]
    result = {'name': fallback, 'description': '', 'tags': []}
    lines = header.splitlines()
    for index, line in enumerate(lines):
        found = re.fullmatch(r'(name|description):\s*(.*)', line)
        if found:
            key, value = found.groups()
            if value in {'>', '|', '>-', '|-'}:
                following = []
                for following_line in lines[index + 1:]:
                    if following_line and not following_line.startswith((' ', '\t')):
                        break
                    following.append(following_line.strip())
                value = ' '.join(following)
            result[key] = _scalar(value)
        tag = re.fullmatch(r'\s+tags:\s*\[(.*)\]\s*', line)
        if tag:
            result['tags'] = [_scalar(value) for value in tag[1].split(',') if value.strip()]
    if (not isinstance(result['name'], str) or not re.fullmatch(r'[\w.-]{1,160}', result['name'])
            or not isinstance(result['description'], str)):
        raise ValueError('skill name or description is invalid')
    result['dynamic'] = bool(re.search(r'^deps:|!`|\$\{|\{\{', text, re.M))
    return result


def validate_skill_binding(binding):
    """Exact pinned implementation and immutable snapshot seal, independent of descriptions."""
    if type(binding) is not dict or set(binding) != BINDING_FIELDS:
        raise ValueError('skill binding fields are invalid')
    if (binding['skill_id'] != XLSX_SKILL_ID or binding['skill_sha256'] != XLSX_SKILL_SHA256
            or binding['script_sha256'] != XLSX_SCRIPT_SHA256
            or not isinstance(binding['snapshot_id'], str)
            or not re.fullmatch('[a-f0-9]{64}', binding['snapshot_id'])):
        raise ValueError('skill adapter is not explicitly registered')
    if (not isinstance(binding['script_path'], str) or not Path(binding['script_path']).is_absolute()
            or '..' in Path(binding['script_path']).parts or '\x00' in binding['script_path']):
        raise ValueError('snapshot script path must be absolute without traversal')
    path = _absolute(binding['script_path'])
    suffix = Path('skills/productivity/xlsx/scripts/xlsx_read.py')
    if len(path.parts) < 6 or Path(*path.parts[-5:]) != suffix:
        raise ValueError('script is not in a registered snapshot path')
    snapshot = path.parents[4]
    manifest = _read(snapshot / 'snapshot.json', 8 * 1024 * 1024)
    if snapshot.name != binding['snapshot_id'] or _sha(manifest) != binding['snapshot_id']:
        raise ValueError('snapshot seal differs from binding')
    document = json.loads(manifest)
    rows = [row for row in document['entries'] if row['skill_id'] == XLSX_SKILL_ID]
    if (len(rows) != 1 or rows[0]['skill_sha256'] != binding['skill_sha256']
            or rows[0].get('script_sha256') != binding['script_sha256']):
        raise ValueError('snapshot entry differs from binding')
    if (_sha(_read(path)) != binding['script_sha256']
            or _sha(_read(path.parent.parent / 'SKILL.md')) != binding['skill_sha256']):
        raise ValueError('snapshot skill or helper bytes changed')
    if (_sha(_read(rows[0]['source_path'])) != binding['skill_sha256']
            or _sha(_read(rows[0]['script_source_path'])) != binding['script_sha256']):
        raise ValueError('original skill or helper changed after synchronization')
    return binding


class SkillCatalog:
    def __init__(self, db_path, owned_root):
        self.db_path = _absolute(db_path)
        self.owned_root = _absolute(owned_root)
        self.owned_root.mkdir(parents=True, exist_ok=True)
        self.snapshots = self.owned_root / 'skill-snapshots'
        self.snapshots.mkdir(exist_ok=True)
        _absolute(self.snapshots)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS rpe_skill_snapshots (
                    snapshot_id TEXT PRIMARY KEY, document BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS rpe_skill_current (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), snapshot_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rpe_skill_temperature (
                    skill_id TEXT PRIMARY KEY, temperature TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rpe_skill_trust (
                    snapshot_id TEXT NOT NULL, skill_id TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id,skill_id));''')

    def _connect(self):
        _absolute(self.db_path)
        return sqlite3.connect(self.db_path, timeout=10)

    def _document(self, snapshot_id=None):
        with self._connect() as db:
            if snapshot_id is None:
                row = db.execute('SELECT snapshot_id FROM rpe_skill_current WHERE singleton=1').fetchone()
                if row is None:
                    return None, {'entries': []}
                snapshot_id = row[0]
            if not isinstance(snapshot_id, str) or not re.fullmatch('[a-f0-9]{64}', snapshot_id):
                raise ValueError('snapshot id must be SHA256')
            row = db.execute('SELECT document FROM rpe_skill_snapshots WHERE snapshot_id=?', (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError('snapshot not found')
        raw = bytes(row[0])
        if _sha(raw) != snapshot_id or _read(self.snapshots / snapshot_id / 'snapshot.json', 8 * 1024 * 1024) != raw:
            raise ValueError('snapshot integrity failed')
        return snapshot_id, json.loads(raw)

    def sync(self, skill_root):
        root = _absolute(skill_root)
        if not root.is_dir():
            raise ValueError('skill root must be an existing directory')
        old_id, old = self._document()
        entries, content = [], {}
        # Do not follow links. Retain skipped directory/file links as unavailable records.
        for directory, dirs, files in os.walk(root, followlinks=False):
            directory = Path(directory)
            for name in list(dirs):
                if (directory / name).is_symlink():
                    relative = (directory / name).relative_to(root).as_posix()
                    entries.append({'skill_id': relative, 'name': name, 'description': '', 'tags': [],
                                    'source_path': str(directory / name / 'SKILL.md'), 'status': 'symlink',
                                    'skill_sha256': None, 'load_name': None})
                    dirs.remove(name)
            if 'SKILL.md' not in files:
                continue
            path = directory / 'SKILL.md'
            relative = directory.relative_to(root).as_posix()
            if relative == '.':
                relative = directory.name
            entry = {'skill_id': relative, 'source_path': str(path), 'status': 'available',
                     'name': directory.name, 'description': '', 'tags': [], 'skill_sha256': None}
            if relative.startswith('.archive/'):
                entry['archived'] = True
            try:
                raw = _read(path)
                entry.update(_metadata(raw, directory.name), skill_sha256=_sha(raw))
                content[relative + '/SKILL.md'] = raw
                # Hermes restore_skill flattens archive paths. Recognize this
                # registered implementation by both pinned contents, never its name alone.
                if relative == XLSX_SKILL_ID or (entry['name'] == 'xlsx' and
                        (entry.get('archived') or relative == 'xlsx')):
                    helper = directory / 'scripts/xlsx_read.py'
                    try:
                        helper_raw = _read(helper)
                        entry.update(script_sha256=_sha(helper_raw), script_source_path=str(helper))
                        content[relative + '/scripts/xlsx_read.py'] = helper_raw
                    except (OSError, ValueError):
                        entry['helper_status'] = 'unavailable'
            except (OSError, ValueError, UnicodeError) as error:
                entry['status'] = 'symlink' if path.is_symlink() else 'unavailable'
                entry['issue'] = type(error).__name__
            entries.append(entry)
            # Support files cannot become extra skills or execute during discovery.
            dirs[:] = [name for name in dirs if name not in {'scripts', 'references', 'templates', 'assets', 'tests'}]
            if len(entries) > MAX_SKILLS:
                raise ValueError('skill snapshot exceeds 2048 skills')
        if len(entries) > MAX_SKILLS:
            raise ValueError('skill snapshot exceeds 2048 skills')
        relocated = [row for row in entries if row['skill_id'] != XLSX_SKILL_ID
            and row.get('status') == 'available' and row.get('skill_sha256') == XLSX_SKILL_SHA256
            and row.get('script_sha256') == XLSX_SCRIPT_SHA256
            and (row.get('archived') or row['skill_id'] == 'xlsx')]
        if len(relocated) == 1 and not any(row['skill_id'] == XLSX_SKILL_ID for row in entries):
            row = relocated[0]
            previous = row['skill_id']
            row['skill_id'] = XLSX_SKILL_ID
            for suffix in ('/SKILL.md', '/scripts/xlsx_read.py'):
                content[XLSX_SKILL_ID + suffix] = content.pop(previous + suffix)
        entries.sort(key=lambda row: row['skill_id'])
        names = {}
        for row in entries:
            for alias in {row['name'], Path(row['skill_id']).name, row['skill_id']}:
                names.setdefault(alias, []).append(row['skill_id'])
        for row in entries:
            row['load_name'] = (row['name'] if len(names[row['name']]) == 1 else
                                row['skill_id'] if len(names[row['skill_id']]) == 1 else None)
            row['resolution_status'] = 'unique' if len(names[row['name']]) == 1 else 'ambiguous'
            row['category'] = row['skill_id'].split('/')[0] if '/' in row['skill_id'] else None
        document = {'schema_version': 'rpe.skill-catalog/v1', 'source_root': str(root), 'entries': entries}
        raw = _canonical(document)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('skill metadata snapshot exceeds its byte budget')
        snapshot_id = _sha(raw)
        target = self.snapshots / snapshot_id
        if not target.exists():
            with tempfile.TemporaryDirectory(prefix='skill-sync-', dir=self.owned_root) as temporary:
                stage = Path(temporary) / snapshot_id
                stage.mkdir()
                (stage / 'snapshot.json').write_bytes(raw)
                for relative, data in content.items():
                    destination = stage / 'skills' / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    destination.chmod(0o400)
                try:
                    os.rename(stage, target)
                except FileExistsError:
                    pass
        if _read(target / 'snapshot.json', 8 * 1024 * 1024) != raw:
            raise ValueError('snapshot collision or corruption')
        with self._connect() as db:
            db.execute('INSERT OR IGNORE INTO rpe_skill_snapshots VALUES (?,?)', (snapshot_id, raw))
            db.execute('INSERT INTO rpe_skill_current VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET snapshot_id=excluded.snapshot_id', (snapshot_id,))
        before, after = {r['skill_id']: r for r in old['entries']}, {r['skill_id']: r for r in entries}
        return {'snapshot_id': snapshot_id, 'previous_snapshot_id': old_id,
                'added': sorted(after.keys() - before.keys()), 'removed': sorted(before.keys() - after.keys()),
                'changed': sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
                'total': len(entries), 'issues': [r for r in entries if r['status'] != 'available']}

    def lookup(self, skill_id, snapshot_id=None):
        snapshot_id, document = self._document(snapshot_id)
        rows = [r for r in document['entries'] if r['skill_id'] == skill_id]
        if len(rows) != 1:
            raise KeyError('skill not found')
        row = dict(rows[0], snapshot_id=snapshot_id)
        row['skill_path'] = str(self.snapshots / snapshot_id / 'skills' / skill_id / 'SKILL.md')
        row['adapter_status'] = 'no_adapter'
        row['temperature'] = 'cold' if row.get('archived') else 'warm'
        with self._connect() as db:
            temp = db.execute('SELECT temperature FROM rpe_skill_temperature WHERE skill_id=?', (skill_id,)).fetchone()
            trusted = db.execute('SELECT 1 FROM rpe_skill_trust WHERE snapshot_id=? AND skill_id=?', (snapshot_id, skill_id)).fetchone()
        if temp and not row.get('archived'):
            row['temperature'] = temp[0]
        if row['status'] == 'available':
            try:
                if _sha(_read(row['skill_path'])) != row['skill_sha256']:
                    raise ValueError('snapshot changed')
                if row.get('script_sha256'):
                    helper = Path(row['skill_path']).parent / 'scripts/xlsx_read.py'
                    if _sha(_read(helper)) != row['script_sha256']:
                        raise ValueError('snapshot helper changed')
            except (ValueError, OSError):
                row['status'] = 'corrupt'
            if row['status'] == 'available':
                try:
                    if _sha(_read(row['source_path'])) != row['skill_sha256']:
                        row['status'] = 'stale'
                    elif row.get('script_source_path') and _sha(_read(row['script_source_path'])) != row['script_sha256']:
                        row['status'] = 'stale'
                except (OSError, ValueError):
                    row['status'] = 'stale'
        if (skill_id == XLSX_SKILL_ID and row['skill_sha256'] == XLSX_SKILL_SHA256
                and row.get('script_sha256') == XLSX_SCRIPT_SHA256 and not row.get('dynamic')):
            row['adapter_status'] = 'trusted' if trusted and row['status'] == 'available' else 'untrusted'
            row['capabilities'] = ['xlsx.inspect', 'tabular.deduplicate']
            row['input_formats'] = ['xlsx', 'csv']
            if row['adapter_status'] == 'trusted':
                row['skill_binding'] = {'snapshot_id': snapshot_id, 'skill_id': skill_id,
                    'skill_sha256': row['skill_sha256'], 'script_sha256': row['script_sha256'],
                    'script_path': str(Path(row['skill_path']).parent / 'scripts/xlsx_read.py')}
        else:
            row['capabilities'], row['input_formats'] = [], []
        row['executable'] = row['adapter_status'] == 'trusted'
        return row

    def trust_adapter(self, skill_id, snapshot_id=None):
        row = self.lookup(skill_id, snapshot_id=snapshot_id)
        if row['status'] != 'available' or row['adapter_status'] not in {'untrusted', 'trusted'}:
            raise ValueError('no registered adapter for current skill bytes')
        with self._connect() as db:
            db.execute('INSERT OR IGNORE INTO rpe_skill_trust VALUES (?,?)', (row['snapshot_id'], skill_id))
        return self.lookup(skill_id, snapshot_id=row['snapshot_id'])['skill_binding']

    def set_temperature(self, skill_id, temperature):
        self.lookup(skill_id)
        if temperature not in {'warm', 'cold'}:
            raise ValueError('temperature must be warm or cold')
        with self._connect() as db:
            db.execute('INSERT INTO rpe_skill_temperature VALUES (?,?) ON CONFLICT(skill_id) DO UPDATE SET temperature=excluded.temperature', (skill_id, temperature))
        return self.lookup(skill_id)

    def query(self, q, input_format=None, capability=None, include_cold=True, limit=5, snapshot_id=None):
        if (not isinstance(q, str) or len(q.encode('utf-8')) > 4096 or type(limit) is not int
                or not 1 <= limit <= 10 or type(include_cold) is not bool
                or input_format not in {None, 'csv', 'xlsx', 'json', 'docx', 'pdf'}
                or capability is not None and not isinstance(capability, str)):
            raise ValueError('invalid bounded query')
        snapshot_id, document = self._document(snapshot_id)
        tokens = set(re.findall(r'[\w.-]+', q.casefold()))
        candidates = []
        for raw in document['entries']:
            if raw['status'] != 'available' or raw['load_name'] is None:
                continue
            registered = (raw['skill_id'] == XLSX_SKILL_ID and raw['skill_sha256'] == XLSX_SKILL_SHA256
                          and raw.get('script_sha256') == XLSX_SCRIPT_SHA256 and not raw.get('dynamic'))
            if capability and (not registered or capability not in {'xlsx.inspect', 'tabular.deduplicate'}):
                continue
            metadata = ' '.join([raw['skill_id'], raw['name'], raw['description'], *raw['tags']]).casefold()
            if input_format and input_format not in metadata:
                continue
            hits = sorted(token for token in tokens if token in metadata)
            exact = q.casefold() in {raw['skill_id'].casefold(), raw['load_name'].casefold()}
            if not hits and not exact and not capability:
                continue
            candidates.append((0 if exact else 1, -len(hits), raw['skill_id'], hits, exact))
        candidates.sort(key=lambda value: value[:3])
        result = []
        # Only matched rows re-read full source bytes, rather than N full snapshot reads.
        for _, _, skill_id, hits, exact in candidates:
            row = self.lookup(skill_id, snapshot_id=snapshot_id)
            if row['status'] != 'available' or row['temperature'] == 'cold' and not include_cold:
                continue
            row['matched_terms'] = hits
            row['match_kind'] = 'exact' if exact else 'adapter' if capability else 'metadata'
            result.append(row)
            if len(result) == limit:
                break
        return {'snapshot_id': snapshot_id, 'kind': 'suggestion_only',
                'candidates': result,
                'reason': 'matched' if result else 'no_adapter' if capability else 'no_match'}

    def materialize(self, destination, snapshot_id=None):
        snapshot_id, document = self._document(snapshot_id)
        if snapshot_id is None:
            raise ValueError('no snapshot to materialize')
        destination = _absolute(destination)
        if destination.exists():
            raise ValueError('materialization destination must be new')
        destination.parent.mkdir(parents=True, exist_ok=True)
        entries = []
        with tempfile.TemporaryDirectory(prefix='skill-home-', dir=destination.parent) as temporary:
            stage = Path(temporary) / 'skills'
            stage.mkdir()
            for source in document['entries']:
                row = self.lookup(source['skill_id'], snapshot_id=snapshot_id)
                if row['status'] in {'corrupt', 'stale'}:
                    raise ValueError('skill changed before materialization')
                if row['status'] != 'available' or row.get('dynamic'):
                    continue
                local = stage / row['skill_id']
                local.mkdir(parents=True)
                (local / 'SKILL.md').write_bytes(_read(row['skill_path']))
                (local / 'SKILL.md').chmod(0o400)
                result = dict(row, skill_path=str(destination / row['skill_id'] / 'SKILL.md'))
                if row.get('script_sha256'):
                    (local / 'scripts').mkdir()
                    helper = local / 'scripts/xlsx_read.py'
                    helper.write_bytes(_read(Path(row['skill_path']).parent / 'scripts/xlsx_read.py'))
                    helper.chmod(0o400)
                    result['script_path'] = str(destination / row['skill_id'] / 'scripts/xlsx_read.py')
                entries.append(result)
            os.rename(stage, destination)
        return {'snapshot_id': snapshot_id, 'materialized_path': str(destination), 'entries': entries}
