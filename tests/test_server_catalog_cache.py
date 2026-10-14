"""HTTP reuse must avoid skill sweeps while detecting revoked directory authority."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import sqlite3
import threading

from rpe_harness.runtime import HarnessRuntime
from rpe_harness.server import create_server
from rpe_harness.skill_catalog import SkillCatalog
from test_server import call


@contextmanager
def live_server(tmp_path, *, trusted=False):
    db = tmp_path / 'runtime.sqlite'
    source = tmp_path / 'source'
    if trusted:
        shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx', source / 'productivity/xlsx')
    else:
        (source / 'unrelated').mkdir(parents=True)
        (source / 'unrelated/SKILL.md').write_text('---\nname: unrelated\ndescription: unrelated initial text\n---\nOriginal metadata\n')
    skills = SkillCatalog(db, tmp_path / 'catalog')
    snapshot = skills.sync(source)['snapshot_id']
    if trusted:
        skills.trust_adapter('productivity/xlsx', snapshot_id=snapshot)
    runtime = HarnessRuntime(db_path=db)
    server = create_server(runtime, port=0, catalog_root=tmp_path / 'catalog')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, skills, source
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def prepare_json(server, tmp_path, *, session='s'):
    source = tmp_path / 'input.json'
    source.write_text('[1,2]')
    return call(server.server_address, '/intent/prepare', {'workspace': str(tmp_path),
        'session_id': session, 'source_ref': 'public-user-task', 'extracted': {
            'goal': 'Normalize JSON', 'capability': 'json.normalize', 'constraints': ['preserve_original'],
            'execution': {'input_path': str(source), 'output_path': str(tmp_path / 'out.json'), 'style': 'pretty'}}})


def test_repeated_http_addresses_and_intent_prepare_reuse_one_catalog_scan(tmp_path, monkeypatch):
    original = SkillCatalog.lookup
    enumerated = []
    def lookup(self, skill_id, snapshot_id=None):
        enumerated.append(skill_id)
        return original(self, skill_id, snapshot_id)
    monkeypatch.setattr(SkillCatalog, 'lookup', lookup)
    with live_server(tmp_path) as (server, _, _):
        address = server.server_address
        assert call(address, '/capabilities/resolve', {'capability': 'json.normalize'})[1]['status'] == 'ready'
        initial = len(enumerated)
        assert initial == 1
        for _ in range(3):
            assert call(address, '/capabilities/resolve', {'capability': 'tabular.deduplicate'})[1]['status'] == 'ready'
            assert len(call(address, '/capabilities')[1]['entries']) == 4
        status, handoff = prepare_json(server, tmp_path)
        assert status == 200 and handoff['status'] == 'ready'
        assert len(enumerated) == initial
        assert not (tmp_path / 'out.json').exists()


def test_cached_http_directory_stays_stale_until_explicit_reload(tmp_path):
    with live_server(tmp_path) as (server, skills, source):
        address = server.server_address
        initial = call(address, '/capabilities')[1]['catalog_sha256']
        (source / 'unrelated/SKILL.md').write_text('---\nname: unrelated\ndescription: changed metadata\n---\nChanged metadata\n')
        skills.sync(source)
        status, result = call(address, '/capabilities/resolve', {'capability': 'json.normalize'})
        assert status == 200 and result['status'] == 'unsupported'
        assert result['catalog_sha256'] != initial
        assert all(not row['executable'] for row in call(address, '/capabilities')[1]['entries'])
        assert prepare_json(server, tmp_path)[1]['status'] == 'unsupported'
        assert call(address, '/reload', {})[0] == 200
        assert call(address, '/capabilities/resolve', {'capability': 'json.normalize'})[1]['status'] == 'ready'
        assert prepare_json(server, tmp_path, session='after-reload')[0] == 200


def test_cached_http_directory_detects_trust_revocation_without_reload(tmp_path):
    with live_server(tmp_path, trusted=True) as (server, skills, _):
        address = server.server_address
        assert call(address, '/capabilities/resolve', {'capability': 'xlsx.inspect'})[1]['status'] == 'ready'
        with sqlite3.connect(skills.db_path) as db:
            db.execute('DELETE FROM rpe_skill_trust')
        rejected = call(address, '/capabilities/resolve', {'capability': 'xlsx.inspect'})[1]
        assert rejected['status'] == 'unsupported'
        # Every cached entry becomes stale until explicit reload, even unrelated builtins.
        assert call(address, '/capabilities/resolve', {'capability': 'json.normalize'})[1]['status'] == 'unsupported'
        assert call(address, '/reload', {})[0] == 200
        assert call(address, '/capabilities/resolve', {'capability': 'json.normalize'})[1]['status'] == 'ready'
        assert call(address, '/capabilities/resolve', {'capability': 'xlsx.inspect'})[1]['status'] == 'unsupported'
