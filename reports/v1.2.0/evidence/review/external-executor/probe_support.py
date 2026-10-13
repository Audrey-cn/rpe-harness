"""Owned subprocess observation for independent broker probes."""
from pathlib import Path
import subprocess
import sys
import time

from rpe_harness.integrations import hermes_session as session


def outer_run(profile,program,workspace,seconds=12):
    command=['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-B','-c',program]
    env={'PATH':'/usr/bin:/bin','HOME':str(workspace),'TMPDIR':str(workspace),
         'PYTHONDONTWRITEBYTECODE':'1','LANG':'en_US.UTF-8'}
    started=time.monotonic()
    process=subprocess.Popen(command,cwd=workspace,env=env,stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,text=True,start_new_session=True)
    identity=session._process_table().get(process.pid,(None,None))[1]
    descendants={}
    try:
        while process.poll() is None and time.monotonic()<started+seconds:
            if identity: session._capture_descendants(process.pid,identity,descendants)
            time.sleep(.05)
        if process.poll() is None:
            session._stop_group(process)
            raise AssertionError('outer probe exceeded bounded timeout')
        out,err=process.communicate(timeout=2)
        remaining=session._remaining_descendants(descendants)
        return {'exit_code':process.returncode,'stdout':out,'stderr':err,
                'elapsed_s':time.monotonic()-started,'observed_descendants':sorted(descendants),
                'cleanup_intervention_pids':list(remaining)}
    finally:
        if process.poll() is None: session._stop_group(process)
        session._stop_descendants(descendants)
        assert not session._remaining_descendants(descendants),'owned child cleanup failed'


def task(workspace,output='result-new.csv'):
    source=Path(workspace)/'new.csv'
    source.write_text('id,value\na,first\na,later\nb,second\n')
    return {'schema_version':2,'event_id':output,'capability':'tabular.deduplicate',
        'goal':'retain first','constraints':['preserve_original','preserve_order'],
        'execution':{'input_path':str(source),'output_path':str(Path(workspace)/output),
                      'key_columns':['id']}}
