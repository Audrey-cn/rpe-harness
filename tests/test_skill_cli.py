"""CLI contracts against real parsers, catalogs, and durable context sources."""
import argparse
import importlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3

import pytest


@pytest.fixture
def api():
    assert importlib.util.find_spec('rpe_harness.skill_cli') is not None, 'skill CLI is absent'
    return importlib.import_module('rpe_harness.skill_cli')


@pytest.fixture
def parser(api):
    parser = argparse.ArgumentParser()
    api.register_skills_and_context(parser.add_subparsers(dest='command', required=True))
    return parser


@pytest.fixture
def skills_args(tmp_path):
    return ['--db', str(tmp_path / 'skills.sqlite'), '--owned-root', str(tmp_path / 'owned')]


def invoke(api, parser, argv):
    result = api.dispatch_skills_and_context(parser.parse_args(argv))
    assert type(result) is dict
    json.dumps(result, allow_nan=False)
    return result


def jsonfile(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding='utf-8')
    return str(path)


def synchronize(api, parser, tmp_path, skills_args):
    root = tmp_path / 'source'
    shutil.copytree(Path(__file__).parent / 'fixtures/skills/productivity/xlsx',
                    root / 'productivity/xlsx')
    return invoke(api, parser, ['skills', 'sync', *skills_args, '--skill-root', str(root)])


def capture(api, parser, tmp_path):
    db = str(tmp_path / 'context.sqlite')
    result = invoke(api, parser, ['context', 'capture', '--db', db, '--source', 'hermes',
        '--event-id', 'event-1', '--session-id', 'session-a',
        '--payload', jsonfile(tmp_path, 'payload.json', {'text': 'original untrusted text'}),
        '--facts', jsonfile(tmp_path, 'facts.json', {'key_columns': ['customer_id']})])
    assert set(result) == {'source', 'event_id', 'sha256'}
    assert result['source'] == 'hermes'
    return db, result


def recovery(api, parser, tmp_path):
    db, ref = capture(api, parser, tmp_path)
    result = invoke(api, parser, ['context', 'recover', '--db', db,
        '--capability', 'tabular.deduplicate', '--session-id', 'session-a',
        '--request', jsonfile(tmp_path, 'request.json', {
            'facts': {'input_path': 'in.csv', 'output_path': 'out.csv'}, 'source_refs': [ref]})])
    return db, result


def test_skill_sync_query_lookup_trust_materialize(api, parser, tmp_path, skills_args, capsys):
    synced = synchronize(api, parser, tmp_path, skills_args)
    assert synced['added'] == ['productivity/xlsx']
    snapshot = synced['snapshot_id']
    query = invoke(api, parser, ['skills', 'query', *skills_args, '--query', 'xlsx',
        '--input-format', 'xlsx', '--capability', 'xlsx.inspect', '--limit', '1',
        '--snapshot-id', snapshot])
    assert query['kind'] == 'suggestion_only'
    assert query['candidates'][0]['adapter_status'] == 'untrusted'
    binding = invoke(api, parser, ['skills', 'trust', 'productivity/xlsx', *skills_args,
        '--snapshot-id', snapshot])
    assert binding['snapshot_id'] == snapshot
    lookup = invoke(api, parser, ['skills', 'lookup', 'productivity/xlsx', *skills_args])
    assert lookup['executable'] is True
    assert lookup['skill_binding'] == binding
    target = tmp_path / 'host/skills'
    exported = invoke(api, parser, ['skills', 'materialize', *skills_args,
        '--destination', str(target), '--snapshot-id', snapshot])
    assert exported['materialized_path'] == str(target)
    assert (target / 'productivity/xlsx/SKILL.md').read_bytes() == (
        tmp_path / 'source/productivity/xlsx/SKILL.md').read_bytes()
    assert capsys.readouterr().out == ''


def test_cold_skills_are_included_by_default_and_can_be_excluded(api, parser, tmp_path, skills_args):
    synchronize(api, parser, tmp_path, skills_args)
    result = invoke(api, parser, ['skills', 'temperature', 'productivity/xlsx', *skills_args,
        '--temperature', 'cold'])
    assert result['temperature'] == 'cold'
    query = ['skills', 'query', *skills_args, '--query', 'xlsx']
    assert invoke(api, parser, query)['candidates'][0]['temperature'] == 'cold'
    assert invoke(api, parser, [*query, '--exclude-cold'])['candidates'] == []
    invoke(api, parser, ['skills', 'temperature', 'productivity/xlsx', *skills_args,
        '--temperature', 'warm'])
    assert invoke(api, parser, [*query, '--exclude-cold'])['candidates'][0]['temperature'] == 'warm'


@pytest.mark.parametrize('action,options', [
    ('sync', ['--skill-root', 'source']), ('query', ['--query', 'xlsx']),
    ('trust', ['productivity/xlsx']), ('lookup', ['productivity/xlsx']),
    ('materialize', ['--destination', 'destination']),
    ('temperature', ['productivity/xlsx', '--temperature', 'cold']),
])
@pytest.mark.parametrize('missing', ['--db', '--owned-root'])
def test_every_skill_action_requires_explicit_paths(parser, tmp_path, action, options, missing):
    paths = {'--db': str(tmp_path / 'skills.sqlite'), '--owned-root': str(tmp_path / 'owned')}
    argv = ['skills', action, *options]
    for flag, value in paths.items():
        if flag != missing:
            argv.extend([flag, value])
    with pytest.raises(SystemExit) as error:
        parser.parse_args(argv)
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('argv', [
    ['context', 'capture', '--source', 'hermes', '--event-id', 'e', '--session-id', 's',
     '--payload', 'p.json', '--facts', 'f.json'],
    ['context', 'recover', '--capability', 'xlsx.inspect', '--session-id', 's', '--request', 'r.json'],
    ['context', 'show', 'recovery'], ['context', 'revalidate', 'recovery', '--session-id', 's'],
])
def test_context_actions_require_explicit_database(parser, argv):
    with pytest.raises(SystemExit) as error:
        parser.parse_args(argv)
    assert error.value.code == 2


def test_context_capture_recover_show_revalidate(api, parser, tmp_path, capsys):
    db, result = recovery(api, parser, tmp_path)
    assert result['status'] == 'ready'
    assert result['facts'] == {'input_path': 'in.csv', 'output_path': 'out.csv',
                               'key_columns': ['customer_id']}
    assert result['recovered_fields'] == ['key_columns']
    history = invoke(api, parser, ['context', 'show', result['recovery_id'], '--db', db])
    assert history['historical'] is True
    assert history['recovery_id'] == result['recovery_id']
    from rpe_harness.context import ContextStore
    assert 'historical' not in ContextStore(db).get_recovery(result['recovery_id'])
    with sqlite3.connect(db) as connection:
        connection.execute('UPDATE context_sources SET facts_json=?',
                           (json.dumps({'key_columns': ['email']}),))
    refreshed = invoke(api, parser, ['context', 'revalidate', result['recovery_id'],
        '--db', db, '--session-id', 'session-a'])
    assert refreshed['recovery_id'] != result['recovery_id']
    assert refreshed['status'] == 'rejected'
    assert 'key_columns' not in refreshed['facts']
    assert 'hash_mismatch' in {issue['code'] for issue in refreshed['issues']}
    with pytest.raises(ValueError, match='session'):
        invoke(api, parser, ['context', 'revalidate', result['recovery_id'],
            '--db', db, '--session-id', 'session-b'])
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('capability,missing', [('tabular.deduplicate', 'key_columns'),
    ('json.normalize', 'style'), ('xlsx.inspect', 'mode')])
def test_cli_uses_fixed_capability_requirements(api, parser, tmp_path, capability, missing):
    result = invoke(api, parser, ['context', 'recover', '--db', str(tmp_path / 'db.sqlite'),
        '--capability', capability, '--session-id', 'session-a', '--request',
        jsonfile(tmp_path, 'request.json', {'facts': {'input_path': 'in', 'output_path': 'out'},
                                         'source_refs': []})])
    assert result['status'] == 'needs_context'
    assert result['missing'] == [missing]


def test_cli_refuses_request_schema_overrides(api, parser, tmp_path):
    request = jsonfile(tmp_path, 'request.json', {'facts': {}, 'source_refs': [],
                                               'requirements': []})
    with pytest.raises(ValueError, match='facts.*source_refs'):
        invoke(api, parser, ['context', 'recover', '--db', str(tmp_path / 'db.sqlite'),
            '--capability', 'tabular.deduplicate', '--session-id', 's', '--request', request])


def test_capture_can_read_one_stdin_document(api, parser, tmp_path, monkeypatch):
    monkeypatch.setattr('sys.stdin', io.StringIO('{"text":"stdin"}'))
    result = invoke(api, parser, ['context', 'capture', '--db', str(tmp_path / 'db.sqlite'),
        '--source', 'hermes', '--event-id', 'e', '--session-id', 's', '--payload', '-',
        '--facts', jsonfile(tmp_path, 'facts.json', {})])
    from rpe_harness.context import ContextStore
    assert ContextStore(tmp_path / 'db.sqlite').resolve(result, session_id='s')['payload'] == {'text': 'stdin'}


def test_two_stdin_capture_documents_are_refused_before_read(api, parser, tmp_path, monkeypatch):
    stream = io.StringIO('{}')
    monkeypatch.setattr('sys.stdin', stream)
    with pytest.raises(ValueError, match='stdin'):
        invoke(api, parser, ['context', 'capture', '--db', str(tmp_path / 'db.sqlite'),
            '--source', 'hermes', '--event-id', 'e', '--session-id', 's', '--payload', '-', '--facts', '-'])
    assert stream.tell() == 0


@pytest.mark.parametrize('source', ['stdin', 'file'])
def test_json_input_has_one_mib_byte_limit(api, parser, tmp_path, monkeypatch, source):
    raw = '{"text":"' + '中' * 400_000 + '"}'
    if source == 'stdin':
        monkeypatch.setattr('sys.stdin', io.StringIO(raw))
        path = '-'
    else:
        path = str(tmp_path / 'request.json')
        Path(path).write_text(raw)
    with pytest.raises(ValueError, match='1 MiB'):
        invoke(api, parser, ['context', 'recover', '--db', str(tmp_path / 'db.sqlite'),
            '--capability', 'json.normalize', '--session-id', 's', '--request', path])


@pytest.mark.parametrize('value', [[], 'text', None, {'facts': {'x': float('nan')}, 'source_refs': []}])
def test_json_input_refuses_nonobjects_and_nonfinite_numbers(api, parser, tmp_path, value):
    with pytest.raises(ValueError):
        invoke(api, parser, ['context', 'recover', '--db', str(tmp_path / 'db.sqlite'),
            '--capability', 'json.normalize', '--session-id', 's', '--request',
            jsonfile(tmp_path, 'bad.json', value)])


def test_capture_preserves_store_byte_budgets(api, parser, tmp_path):
    with pytest.raises(ValueError, match='budget'):
        invoke(api, parser, ['context', 'capture', '--db', str(tmp_path / 'db.sqlite'),
            '--source', 'hermes', '--event-id', 'e', '--session-id', 's',
            '--payload', jsonfile(tmp_path, 'payload.json', {}),
            '--facts', jsonfile(tmp_path, 'facts.json', {'text': 'x' * 17000})])
    with sqlite3.connect(tmp_path / 'db.sqlite') as connection:
        assert connection.execute('SELECT count(*) FROM context_sources').fetchone()[0] == 0


def test_capture_preserves_source_byte_budget(api, parser, tmp_path):
    with pytest.raises(ValueError, match='budget'):
        invoke(api, parser, ['context', 'capture', '--db', str(tmp_path / 'db.sqlite'),
            '--source', 'hermes', '--event-id', 'e', '--session-id', 's',
            '--payload', jsonfile(tmp_path, 'payload.json', {'text': 'x' * (256 * 1024)}),
            '--facts', jsonfile(tmp_path, 'facts.json', {})])
    with sqlite3.connect(tmp_path / 'db.sqlite') as connection:
        assert connection.execute('SELECT count(*) FROM context_sources').fetchone()[0] == 0


@pytest.mark.parametrize('extra', [0, 1])
def test_binary_stdin_accepts_exactly_one_mib_and_refuses_more(api, parser, tmp_path, monkeypatch, extra):
    request = b'{"facts":{"input_path":"in","output_path":"out"},"source_refs":[]}'
    raw = request + b' ' * (1024 * 1024 - len(request) + extra)
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8'))
    argv = ['context', 'recover', '--db', str(tmp_path / 'db.sqlite'),
            '--capability', 'json.normalize', '--session-id', 's', '--request', '-']
    if extra:
        with pytest.raises(ValueError, match='1 MiB'):
            invoke(api, parser, argv)
    else:
        result = invoke(api, parser, argv)
        assert result['status'] == 'needs_context'
        assert result['missing'] == ['style']


def test_bounded_query_limits_still_apply(api, parser, tmp_path, skills_args):
    synchronize(api, parser, tmp_path, skills_args)
    with pytest.raises(ValueError, match='bounded query'):
        invoke(api, parser, ['skills', 'query', *skills_args, '--query', 'xlsx', '--limit', '0'])


def test_more_than_eight_source_refs_are_rejected(api, parser, tmp_path):
    db, ref = capture(api, parser, tmp_path)
    result = invoke(api, parser, ['context', 'recover', '--db', db,
        '--capability', 'tabular.deduplicate', '--session-id', 'session-a', '--request',
        jsonfile(tmp_path, 'request.json', {'facts': {}, 'source_refs': [ref] * 9})])
    assert result['status'] == 'rejected'
    assert result['issues'] == [{'code': 'limit_exceeded'}]


@pytest.mark.parametrize('argv', [
    ['skills', 'eval'], ['context', 'eval'],
    ['context', 'recover', '--db', 'db', '--capability', 'shell.execute', '--session-id', 's', '--request', '-'],
    ['skills', 'temperature', 'id', '--db', 'db', '--owned-root', 'root', '--temperature', 'hot'],
])
def test_unsupported_operations_and_values_are_rejected(parser, argv):
    with pytest.raises(SystemExit) as error:
        parser.parse_args(argv)
    assert error.value.code == 2
