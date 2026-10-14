#!/usr/bin/env python3
"""Run only public local fixtures through the installed wheel; no host/model calls."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from zipfile import ZipFile


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--rehearsal', action='store_true', help='never count this as final release evidence')
    args = parser.parse_args()
    root, source, wheel = args.root.absolute(), args.source.absolute(), args.wheel.absolute()
    if not re.fullmatch('[a-zA-Z0-9_-]{1,80}', args.label):
        raise ValueError('label must be a simple unique path component')
    if Path.cwd() != root:
        raise ValueError('smoke must start with CWD at the registered owned-root')
    installenv = root / 'installenv'
    if not Path(sys.executable).absolute().is_relative_to(installenv):
        raise ValueError('execute this script with ROOT/installenv/bin/python')
    work = root / 'wheel-smoke' / args.label
    work.mkdir(parents=True, exist_ok=False)
    trace = []
    report = {'schema_version': 'rpe.installed-wheel-smoke/v1', 'label': args.label,
              'final_evidence': not args.rehearsal, 'passed': False, 'checks': {},
              'cwd': str(root), 'work_root': str(work), 'python': sys.executable,
              'wheel': str(wheel), 'wheel_sha256': digest(wheel.read_bytes())}
    started = time.monotonic()
    environment = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP'):
        environment.pop(key, None)
    environment.update(PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(work / 'tmp'))
    (work / 'tmp').mkdir()
    cli = installenv / 'bin/rpe-harness'

    def check(name, condition):
        report['checks'][name] = bool(condition)
        if not condition:
            raise AssertionError(name)

    def command(name, arguments, *, code, status=None):
        argv = [str(cli), *map(str, arguments)]
        result = subprocess.run(argv, cwd=root, env=environment, text=True, capture_output=True, timeout=30)
        item = {'name': name, 'argv': argv, 'returncode': result.returncode,
                'stdout': result.stdout, 'stderr': result.stderr}
        trace.append(item)
        check(name + '_exit_code', result.returncode == code)
        data = json.loads(result.stdout)
        if status is not None:
            check(name + '_status', data.get('status') == status)
        save(work / (name + '.json'), data)
        return data

    try:
        import rpe_harness
        from rpe_harness.intent import MinimalStateStore
        from rpe_harness.tasks import plan_from_request
        from rpe_harness.json_verification import verify_json_normalization

        installed_root = Path(rpe_harness.__file__).parent
        check('import_is_installed_package', installed_root.is_relative_to(installenv))
        check('installed_version_1_8_0', importlib.metadata.version('rpe-harness') == '1.8.0')
        report['installed_package_root'] = str(installed_root)
        with ZipFile(wheel) as archive:
            wheel_files = {name: archive.read(name) for name in archive.namelist()
                           if name.startswith('rpe_harness/') and name.endswith('.py')}
        source_root = source / 'src'
        source_files = {path.relative_to(source_root).as_posix(): path.read_bytes()
                        for path in (source_root / 'rpe_harness').rglob('*.py')}
        installed_files = {'rpe_harness/' + path.relative_to(installed_root).as_posix(): path.read_bytes()
                           for path in installed_root.rglob('*.py')}
        all_names = sorted(set(wheel_files) | set(source_files) | set(installed_files))
        hashes = [{'path': name, 'wheel_sha256': digest(wheel_files[name]) if name in wheel_files else None,
                   'source_sha256': digest(source_files[name]) if name in source_files else None,
                   'installed_sha256': digest(installed_files[name]) if name in installed_files else None,
                   'matches': name in wheel_files and name in source_files and name in installed_files
                   and wheel_files[name] == source_files[name] == installed_files[name]} for name in all_names]
        save(work / 'package-python-hashes.json', hashes)
        report['python_files_checked'] = len(hashes)
        report['release_integrity_passed'] = bool(hashes) and all(row['matches'] for row in hashes)
        report['checks']['wheel_installed_source_python_match'] = report['release_integrity_passed']
        if not args.rehearsal:
            check('release_integrity', report['release_integrity_passed'])

        source_file, output_file = work / 'input.json', work / 'output.json'
        original = b' { "z" : [1, 2], "text" : "hello", "nil" : null } \n'
        expected = b'{"z":[1,2],"text":"hello","nil":null}\n'
        source_file.write_bytes(original)
        extracted = {'goal': 'Minify this public JSON file while preserving the input',
                     'capability': 'json.normalize', 'constraints': ['preserve_original'],
                     'execution': {'input_path': str(source_file), 'output_path': str(output_file),
                                   'style': 'minify'}}
        extraction_file = work / 'extracted.json'
        save(extraction_file, extracted)
        db = work / 'state.sqlite'
        session, source_ref = 'installed-smoke-session', 'public-smoke:' + digest(original)
        catalog = command('capabilities', ['capabilities', 'resolve', '--capability', 'json.normalize',
                                          '--input-format', 'json', '--constraint', 'preserve_original'],
                          code=0, status='ready')
        prepare = ['intent', 'prepare', '--db', db, '--workspace', work, '--session-id', session,
                   '--source-ref', source_ref, '--extracted', extraction_file]
        handoff = command('prepare', prepare, code=0, status='ready')
        check('handoff_exact_five_fields', set(handoff) == {
            'state_id', 'capability', 'constraints', 'catalog_sha256', 'status'})
        check('handoff_catalog_matches', handoff['catalog_sha256'] == catalog['catalog_sha256'])
        check('prepare_does_not_execute', not output_file.exists())
        shown = command('show', ['intent', 'show', handoff['state_id'], '--db', db,
                                 '--session-id', session], code=0, status='ready')
        check('show_preserves_handoff', shown == handoff)
        command('cross_session', ['intent', 'show', handoff['state_id'], '--db', db,
                                  '--session-id', 'different-session'], code=2, status='error')

        anchored = MinimalStateStore(db).resolve(handoff['state_id'], session_id=session,
                                                 catalog_sha256=handoff['catalog_sha256'])
        request = anchored['request']
        check('anchored_request_has_exact_extracted_execution', request['execution'] == extracted['execution'])
        check('anchored_request_is_v2', request['schema_version'] == 2)
        request_file = work / 'anchored-request.json'
        save(request_file, request)
        plan = plan_from_request(request)
        outcome = command('run', ['run', '--request', request_file, '--db', work / 'runs.sqlite'],
                          code=0, status='succeeded')
        check('runtime_independent_oracle_passed', outcome['verification']['passed'] is True)
        check('artifact_exact_whitespace_and_tokens', output_file.read_bytes() == expected)
        check('input_preserved', source_file.read_bytes() == original)
        prepared = plan.verification_parameters()
        prepared['staging_path'] = str(output_file)
        independent = verify_json_normalization(prepared, plan=plan)
        save(work / 'independent-oracle.json', independent)
        check('published_artifact_independently_reopened', independent['passed'] is True)
        bad_output = work / 'bad-output.json'
        bad_output.write_bytes(b'{"z":[99],"text":"hello","nil":null}\n')
        prepared['staging_path'] = str(bad_output)
        tamper = verify_json_normalization(prepared, plan=plan)
        save(work / 'tampered-artifact-oracle.json', tamper)
        check('oracle_rejects_changed_business_tokens', tamper['passed'] is False
              and tamper['checks'].get('raw_tokens_preserved') is False)
        again = MinimalStateStore(db).resolve(handoff['state_id'], session_id=session,
                                              catalog_sha256=handoff['catalog_sha256'])
        check('handoff_replay_keeps_anchored_parameters', again['request'] == request)
        replay = command('run_replay', ['run', '--request', request_file, '--db', work / 'runs.sqlite'],
                         code=0, status='succeeded')
        check('task_idempotency', replay['run_id'] == outcome['run_id'])

        missing = copy.deepcopy(extracted)
        missing['execution'].pop('style')
        save(work / 'missing.json', missing)
        refused = command('missing', [*prepare[:-1], work / 'missing.json'], code=3, status='needs_context')
        check('missing_style_is_explicit', 'execution.style' in refused['missing'] and 'state_id' not in refused)
        bad = copy.deepcopy(extracted)
        bad['unexpected'] = 'must not be ignored'
        save(work / 'bad.json', bad)
        refused = command('bad', [*prepare[:-1], work / 'bad.json'], code=2, status='rejected')
        check('unknown_field_never_handed_off', 'state_id' not in refused)
        command('unsupported', ['capabilities', 'resolve', '--capability', 'pdf.summarize'],
                code=2, status='unsupported')
        check('final_input_preserved', source_file.read_bytes() == original)
        report['source_sha256'] = digest(original)
        report['output_sha256'] = digest(output_file.read_bytes())
        report['run_id'] = outcome['run_id']
        report['functional_passed'] = all(value for name, value in report['checks'].items()
                                          if name != 'wheel_installed_source_python_match')
        report['passed'] = report['functional_passed'] and (args.rehearsal or report['release_integrity_passed'])
    except Exception as error:
        report['error'] = {'type': type(error).__name__, 'message': str(error)}
    finally:
        report['wall_seconds'] = round(time.monotonic() - started, 6)
        save(work / 'command-trace.json', trace)
        save(work / 'result.json', report)
        print(json.dumps({'passed': report['passed'], 'final_evidence': report['final_evidence'],
                          'report': str(work / 'result.json'), 'checks': len(report['checks']),
                          'error': report.get('error')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
