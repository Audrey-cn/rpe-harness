#!/usr/bin/env python3
"""Installed-package local fusion acceptance. Test keys/data stay in owned-root.

Requires OpenSSL for a temporary test identity. Product signing/verification uses
the standard library; no real creator key, model call or public release is used.
"""
import argparse
import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def test_identity(root):
    private = root / 'test-private.pem'
    subprocess.run(['openssl', 'genrsa', '-out', str(private), '2048'],
                   check=True, capture_output=True, timeout=30)
    text = subprocess.run(['openssl', 'rsa', '-in', str(private), '-text', '-noout'],
                          check=True, capture_output=True, text=True, timeout=10).stdout
    def integer(name):
        section = re.search(rf'{name}:\n((?:\s+[0-9a-f:]+\n)+)', text).group(1)
        value = int(re.sub(r'\s|:', '', section), 16)
        raw = value.to_bytes((value.bit_length() + 7) // 8, 'big')
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    public = {'key_type': 'progenitor-rsa-pkcs1-sha256-v1', 'n': integer('modulus'), 'e': 65537}
    fingerprint = hashlib.sha256(canonical(public)).hexdigest()
    return {'key_type': public['key_type'], 'node_id': fingerprint,
            'public_key': public, 'public_key_id': fingerprint,
            'private_key': {'d': integer('privateExponent')}}


def validate(root, example, registry):
    import rpe_harness
    from rpe_harness.assets.identity import sign_envelope
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    checks, commands = [], []
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(root),
                       TMP=str(root), TEMP=str(root))
    environment.pop('PYTHONPATH', None)
    state = root / 'state'
    state.mkdir()
    db = state / 'runs.sqlite'
    def call(*arguments):
        command = [sys.executable, '-B', '-m', 'rpe_harness', *map(str, arguments)]
        process = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
        commands.append({'arguments': list(map(str, arguments)), 'returncode': process.returncode})
        if process.returncode:
            raise RuntimeError(process.stdout[:1000] + process.stderr[:1000])
        return json.loads(process.stdout)
    rows = call('assets', 'registry-list', '--registry', registry)['assets']
    assert len(rows) == 6 and len({row['content_sha256'] for row in rows}) == 5
    for row in rows:
        imported = call('assets', 'registry-import', '--registry', registry, '--name', row['name'],
                        '--owned-root', state, '--db', db)
        assert imported['state'] == 'unverified' and not imported['compatible'] and not imported['executable']
    checks.append({'check': 'legacy_registry', 'names': 6, 'contents': 5, 'executable': False})
    package = root / 'signed-package'
    shutil.copytree(example, package)
    identity = test_identity(root)
    manifest = json.loads((package / 'manifest.json').read_text())
    (package / 'signature.json').write_bytes(canonical(sign_envelope(manifest, identity,
                                                                  scope='capability-package')))
    identity_path = root / 'test-identity.json'
    identity_path.write_bytes(canonical(identity))
    keyring = root / 'test-trust.json'
    keyring.write_bytes(canonical({'schema_version': 'rpe.trust-policy/v1', 'trusted_keys': [{
        'key_id': identity['public_key_id'], 'owner': 'acceptance-test-only', 'roles': ['creator'],
        'scopes': ['capability-package'], 'public_key': identity['public_key'], 'revoked': False}]}))
    inspected = call('assets', 'inspect', '--package', package, '--keyring', keyring)
    assert inspected['source_verification']['passed'] is True
    asset = call('assets', 'import', '--package', package, '--owned-root', state, '--db', db)
    assert asset['state'] == 'unverified'
    workspace = root / 'workspace'
    workspace.mkdir()
    source = workspace / 'orders.csv'
    source.write_text('订单号,金额\n001,10\n002,20\n001,99\n', encoding='utf-8-sig')
    original = source.read_bytes()
    def run(name):
        request = {'schema_version': 2, 'event_id': name, 'goal': '按订单号保留首条',
            'capability': 'tabular.deduplicate', 'constraints': ['preserve_original', 'preserve_order'],
            'execution': {'input_path': str(source), 'output_path': str(workspace / name),
                          'key_columns': ['订单号']}}
        request_file = workspace / (name + '.json')
        request_file.write_bytes(canonical(request))
        result = call('run', '--request', request_file, '--db', db)
        assert result['status'] == 'succeeded' and source.read_bytes() == original
        with (workspace / name).open(encoding='utf-8-sig', newline='') as file:
            assert list(csv.reader(file)) == [['订单号', '金额'], ['001', '10'], ['002', '20']]
        return result
    seed = run('seed.csv')
    proposal = call('assets', 'propose', asset['asset_id'], '--keyring', keyring,
        '--evidence-run-id', seed['run_id'], '--db', db)
    validation = call('evolution', 'validate', proposal['proposal_id'], '--db', db)
    assert validation['passed'] and len(validation['checks']) == 13
    active = call('evolution', 'apply', proposal['proposal_id'], '--db', db)
    actual = run('candidate.csv')['artifact']['actual_executor']
    assert actual['candidate_version_id'] == active['version_id']
    assert actual['code_sha256'] == manifest['content_sha256']
    exported = call('assets', 'export', active['version_id'], '--db', db,
        '--destination', root / 'exported', '--identity', identity_path)
    exported_text = (root / 'exported' / 'manifest.json').read_text()
    assert str(root) not in exported_text and seed['run_id'] not in exported_text
    assert '订单号' not in exported_text and 'goal' not in exported_text
    imported_export = call('assets', 'import', '--package', root / 'exported', '--owned-root', state, '--db', db)
    assert imported_export['content_sha256'] == manifest['content_sha256']
    assert imported_export['state'] == 'unverified' and exported['signed'] is True
    rolled_back = call('evolution', 'rollback', active['version_id'], '--db', db)
    assert rolled_back['rolled_back'] is True
    assert run('fallback.csv')['artifact']['actual_executor']['kind'] == 'builtin'
    checks.append({'check': 'signed_package_lifecycle', 'asset_id': asset['asset_id'],
                   'proposal_id': proposal['proposal_id'], 'version_id': active['version_id'],
                   'content_sha256': actual['code_sha256'], 'fixtures': 13,
                   'actual_candidate_call': True, 'export_reimport': True, 'rollback_to_builtin': True,
                   'seed_run_id': seed['run_id']})
    return {'passed': True, 'version': rpe_harness.__version__,
            'module_path': rpe_harness.__file__, 'checks': checks, 'commands': commands}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owned-root', type=Path, required=True, help='new empty acceptance directory')
    parser.add_argument('--example', type=Path, required=True)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    arguments = parser.parse_args()
    report = validate(arguments.owned_root, arguments.example, arguments.registry)
    arguments.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'version': report['version'],
                      'module_path': report['module_path']}))
