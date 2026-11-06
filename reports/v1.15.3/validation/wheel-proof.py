#!/usr/bin/env python3
"""Match all wheel and installed Python files to the source checkout."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

import rpe_harness


def files(package):
    return {p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in package.rglob('*.py')}


wheel, installed, output = map(Path, sys.argv[1:])
source = Path(__file__).resolve().parents[3]/'src/rpe_harness'
package = installed.resolve()/'rpe_harness'
if Path(rpe_harness.__file__).resolve().parent != package:
    raise SystemExit('RPE imported outside the installed wheel')
with ZipFile(wheel) as archive:
    names = [name for name in archive.namelist() if name.startswith('rpe_harness/') and name.endswith('.py')]
    if any(count != 1 for count in Counter(names).values()):
        raise SystemExit('duplicate source member in wheel')
    payload = {name.removeprefix('rpe_harness/'): hashlib.sha256(archive.read(name)).hexdigest() for name in names}
if not payload or payload != files(package) or payload != files(source):
    raise SystemExit('wheel, install and source differ')
with output.open('x', encoding='utf-8') as stream:
    json.dump({'source_wheel_installed_python_files_equal': True, 'python_files': len(payload),
               'version': rpe_harness.__version__, 'wheel': wheel.name,
               'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}, stream, indent=2)
    stream.write('\n')
