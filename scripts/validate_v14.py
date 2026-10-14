#!/usr/bin/env python3
"""Installed 1.4 acceptance; test identities, DBs and subprocesses stay owned.

Uses local OpenSSL only to generate temporary test keys. Does not call a model,
change local trust/configuration, activate receiver assets or publish remotely.
"""
import argparse
import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from validate_fusion import test_identity


def validate(root, candidate, report_path):
    import rpe_harness
    from rpe_harness.assets.identity import canonical_json
    from rpe_harness.evolution import propose_candidate, validate_candidate, apply_candidate, EvolutionStore
    from rpe_harness.experiments.reuse_sequence import run_sequence
    from rpe_harness.runtime import HarnessRuntime
    from rpe_harness.recovery import recover_run
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'version': rpe_harness.__version__,
              'module_path': rpe_harness.__file__, 'checks': [], 'commands': [],
              'published': False, 'global_changes_by_runner': False, 'exceptions': []}
    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(root), TMP=str(root), TEMP=str(root))
    environment.pop('PYTHONPATH', None)
    def call(arguments, expected_exit=0, *, module='rpe_harness'):
        result = subprocess.run([sys.executable, '-B', '-m', module, *map(str, arguments)],
            env=environment, capture_output=True, text=True, timeout=120)
        row = {'module': module, 'arguments': list(map(str, arguments)), 'returncode': result.returncode}
        report['commands'].append(row)
        save()
        if result.returncode != expected_exit:
            raise RuntimeError('unexpected acceptance CLI status')
        return json.loads(result.stdout)
    try:
        assert rpe_harness.__version__ == '1.4.0' and '/site-packages/' in rpe_harness.__file__
        sequence = run_sequence(root / 'sequence', candidate_path=candidate)
        assert sequence['passed']
        report['reuse_sequence'] = sequence
        save()
        # Prepare a separate healthy version, retained temporarily for real-host reuse.
        state = root / 'state'
        state.mkdir()
        db = state / 'runs.sqlite'
        source, output = state / 'orders.csv', state / 'seed.csv'
        source.write_bytes(b'id,value\n001,first\n001,later\n002,last\n')
        seed = HarnessRuntime(db_path=db).run({'schema_version': 2, 'event_id': 'installed-registry-seed',
            'goal': 'keep first row by id', 'capability': 'tabular.deduplicate',
            'execution': {'input_path': str(source), 'output_path': str(output), 'key_columns': ['id']}})
        assert seed['status'] == 'succeeded'
        with output.open(encoding='utf-8-sig', newline='') as stream:
            assert list(csv.reader(stream)) == [['id', 'value'], ['001', 'first'], ['002', 'last']]
        proposal = propose_candidate(db, state, component='executor', kind='csv_executor',
            content=candidate.read_text(), evidence_run_ids=[seed['run_id']])
        certificate = validate_candidate(proposal['proposal_id'], db)
        assert certificate['passed'] and len(certificate['checks']) == 13
        active = apply_candidate(proposal['proposal_id'], db)
        report['host_candidate'] = {'db_path': str(db), 'version_id': active['version_id'],
            'seed_run_id': seed['run_id'], 'content_sha256': active['content_hash'],
            'independent_fixture_checks': len(certificate['checks'])}
        identities = []
        for role in ('creator', 'registry'):
            directory = root / role
            directory.mkdir(mode=0o700)
            identity = test_identity(directory)
            path = directory / 'test-identity.json'
            path.write_bytes(canonical_json(identity))
            path.chmod(0o600)
            identities.append((identity, path))
        policy = {'schema_version': 'rpe.trust-policy/v1', 'trusted_keys': [
            {'key_id': identity['public_key_id'], 'owner': 'acceptance-test-only-' + role,
             'roles': [role], 'scopes': [scope], 'public_key': identity['public_key'], 'revoked': False}
            for (identity, _), role, scope in zip(identities, ('creator', 'registry'),
                                                 ('capability-package', 'registry-index'))]}
        keyring = root / 'public-test-trust.json'
        keyring.write_bytes(canonical_json(policy))
        bundle = root / 'release-bundle'
        pointer = EvolutionStore(db).pointer('executor', 'csv_executor')
        built = call(['assets', 'registry-build', '--db', db, '--version-id', active['version_id'],
            '--destination', bundle, '--creator-identity', identities[0][1],
            '--registry-identity', identities[1][1], '--keyring', keyring])
        assert built['built'] and not built['published'] and not built['activated']
        integrity = call(['--bundle', bundle, '--keyring', keyring, '--mode', 'integrity',
            '--report', root / 'integrity.json'], module='rpe_harness.assets.prepublish')
        assert integrity['passed'] and integrity['independent_acceptance'] == 'not_run'
        accepted = call(['--bundle', bundle, '--keyring', keyring, '--mode', 'acceptance',
            '--owned-root', root, '--report', root / 'acceptance.json'], module='rpe_harness.assets.prepublish')
        assert accepted['passed'] and accepted['independent_acceptance'] == 'passed'
        assert accepted['published'] is accepted['activated'] is False
        assert EvolutionStore(db).pointer('executor', 'csv_executor') == pointer
        # A fresh receiving policy can revoke the registry signer without changing the bundle.
        revoked = copy.deepcopy(policy)
        revoked['trusted_keys'][1]['revoked'] = True
        revoked_path = root / 'revoked-test-trust.json'
        revoked_path.write_bytes(canonical_json(revoked))
        denied = call(['--bundle', bundle, '--keyring', revoked_path], 1,
                      module='rpe_harness.assets.prepublish')
        assert denied['passed'] is False and denied['published'] is denied['activated'] is False
        tampered = root / 'tampered-bundle'
        shutil.copytree(bundle, tampered)
        entry = built['entries'][0]
        code = tampered / entry['package_path'] / 'candidate.py'
        code.chmod(0o600)
        code.write_bytes(code.read_bytes() + b'\n# owned tamper test\n')
        rejected = call(['--bundle', tampered, '--keyring', keyring], 1,
                        module='rpe_harness.assets.prepublish')
        assert rejected['passed'] is False
        report['registry'] = {'built': built, 'integrity': integrity, 'acceptance': accepted,
                              'revocation_rejected': denied, 'tamper_rejected': rejected}
        report['checks'].append({'check': 'separate_registry_and_creator_signers', 'passed': True})
        save()
        # Actual stopped interpreter, fresh runtime and explicit linked recovery.
        recovery_root = root / 'recovery'
        recovery_root.mkdir()
        json_source, json_output = recovery_root / 'input.json', recovery_root / 'out.json'
        json_source.write_bytes(b' { "number": 9007199254740993, "zero": -0 } ')
        request = {'schema_version': 2, 'event_id': 'installed-json-crash', 'goal': 'minify JSON',
            'capability': 'json.normalize', 'execution': {'input_path': str(json_source),
                'output_path': str(json_output), 'style': 'minify'}}
        request_file = recovery_root / 'request.json'
        request_file.write_text(json.dumps(request))
        recovery_db = recovery_root / 'runs.sqlite'
        program = ('import json,os,sys\nfrom rpe_harness.runtime import HarnessRuntime\n'
            'runtime=HarnessRuntime(db_path=sys.argv[2])\noriginal=runtime.store.step\n'
            'def crash(run_id,stage,*a,**k):\n original(run_id,stage,*a,**k)\n'
            " if stage=='execute': os._exit(77)\n"
            'runtime.store.step=crash\nruntime.run(json.load(open(sys.argv[1])))\n')
        process = subprocess.run([sys.executable, '-B', '-c', program, str(request_file), str(recovery_db)],
            env=environment, capture_output=True, text=True, timeout=30)
        assert process.returncode == 77
        runtime = HarnessRuntime(db_path=recovery_db)
        old = runtime.store.list()[0]
        assert old['status'] == 'interrupted' and not json_output.exists()
        restored = recover_run(runtime, old['run_id'])
        assert restored['status'] == 'succeeded'
        assert json_output.read_bytes() == b'{"number":9007199254740993,"zero":-0}\n'
        assert recover_run(runtime, old['run_id'])['run_id'] == restored['run_id']
        report['checks'].append({'check': 'installed_json_crash_recovery', 'passed': True,
            'crashed_exit_code': 77, 'original_run_id': old['run_id'], 'recovery_run_id': restored['run_id'],
            'plan_sha256': restored['artifact']['plan_sha256'], 'idempotent': True})
        report['passed'] = True
    except Exception as error:
        report['exceptions'].append({'type': type(error).__name__})
    finally:
        save()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owned-root', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = validate(args.owned_root, args.candidate, args.report)
    print(json.dumps({k: result[k] for k in ('passed', 'version', 'module_path', 'exceptions')}))
    raise SystemExit(0 if result['passed'] else 1)
