import os, subprocess, sys
from pathlib import Path
from rpe_harness.integrations.hermes_session import sandbox_profile
root = Path('/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T/rpe-v12-development-20261014-fp1l7bvp/review/independent')
workspace = root/'control'; workspace.mkdir(exist_ok=True)
profile=sandbox_profile([workspace],Path.home()/'.hermes',native_import_root=Path('/Users/audrey/项目/rpe-harness/src'))
program = '''import sys
sys.path.insert(0,'/Users/audrey/项目/rpe-harness/src')
from rpe_harness.integrations.hermes import _metadata
print(_metadata({'request': {'body': {'tool_choice':'auto','tools':[]}}}))
'''
p = subprocess.run(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-B','-c',program],capture_output=True,text=True,cwd=workspace)
print('exit',p.returncode)
print(p.stdout)
print(p.stderr[-2000:])
