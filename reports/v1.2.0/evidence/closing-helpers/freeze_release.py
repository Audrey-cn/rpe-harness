from pathlib import Path
import hashlib,json,zipfile,shutil,xml.etree.ElementTree as ET
root=Path("/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T/rpe-v12-development-20261014-fp1l7bvp");repo=Path('/Users/audrey/项目/rpe-harness')
wheel=root/'wheel-release/rpe_harness-1.2.0-py3-none-any.whl'
z=zipfile.ZipFile(wheel)
files={str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((repo/'src/rpe_harness').rglob('*.py'))}
mismatches=[rel for rel in files if (repo/rel).read_bytes()!=z.read(rel.removeprefix('src/'))]
assert not mismatches
old=json.loads((root/'frozen-source-manifest.json').read_text())
changed=[rel for rel in files if files[rel]!=old['files'].get(rel)]
assert set(changed)=={'src/rpe_harness/integrations/hermes.py','src/rpe_harness/integrations/hermes_session.py','src/rpe_harness/integrations/hermes_broker.py','src/rpe_harness/evolution/sandbox.py','src/rpe_harness/evolution/cycle.py'},changed
report={'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'files':files,'wheel_source_mismatches':mismatches,'changed_from_36_session_snapshot':changed,'reason':'strict admission closure; ephemeral authenticated external executor to avoid macOS nested sandbox_apply; Darwin libproc RSS observation; factual parameter errors and persisted error_code feedback'}
(root/'frozen-source-manifest-release.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
dest=repo/'reports/v1.2.0'
shutil.copyfile(wheel,dest/'artifacts'/wheel.name)
shutil.copyfile(root/'frozen-source-manifest-release.json',dest/'evidence/frozen-source-manifest.json')
shutil.copyfile(root/'pytest-final-release.xml',dest/'evidence/pytest-final.xml')
testroot=ET.parse(root/'pytest-final-release.xml').getroot()
assert all(int(x.attrib.get(k,0))==0 for x in testroot.iter('testsuite') for k in ('failures','errors','skipped'))
summary={'command':'RPE_HERMES_SOURCE=/Users/audrey/.hermes/hermes-agent PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 owned/venv/bin/python -B -m pytest -q -p no:cacheprovider --basetemp owned/pytest-final-error-feedback --junitxml owned/pytest-final-release.xml','terminal_result':next(line.strip() for line in reversed((root/'pytest-final-release-terminal.txt').read_text().splitlines()) if 'passed' in line and 'subtests' in line),'junit_suites':[x.attrib for x in testroot.iter('testsuite')],'raw_terminal_log_retained':False,'version':'1.2.0','wheel_sha256':report['wheel_sha256']}
(dest/'evidence/pytest-final-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
protocol=json.loads((dest/'evidence/guarded-release-supplement-protocol.json').read_text())
protocol['source_and_wheel_sha256']=report['wheel_sha256'];protocol['frozen_manifest']='frozen-source-manifest.json'
protocol['production_change']='Close actual none-policy and same-turn terminal admission before execution; retain original-call read-only replay. Run the fixed Harness CLI outside the preserved Hermes sandbox through a session-owned authenticated, receipt/hash-bound loopback task endpoint with bounded child/socket cleanup; candidates retain their own single sandbox and native libproc RSS supervision.'
protocol['startup_failure_preserved']='final-supplement-startup-failure.json'
protocol['amendment_before_retry']='Read-only launcher runtime fix, ephemeral authenticated external executor to avoid macOS nested sandbox_apply, native Darwin RSS observation and fresh owned fixture state; six-case and same-session budgets/acceptance unchanged. Original pre-model failure, nested-sandbox RED and post-broker 977 four-of-six host outcomes plus same-source two-of-two diagnostics are preserved. Factual parameter error reporting and host error_code diagnoses are added without assuming the unknown original XLSX cause.'
(dest/'evidence/guarded-release-supplement-protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2))
print(json.dumps({'wheel_sha256':report['wheel_sha256'],'source_count':len(files),'changed':changed,'byte_matches':not mismatches,'full_regression':summary['terminal_result']}))

