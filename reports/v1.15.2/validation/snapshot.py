from pathlib import Path
import hashlib,json,os,site,sys
import rpe_harness
from rpe_harness.experiments.address_comparison import _environment
cfg=Path(sys.prefix)/'pyvenv.cfg'
assert 'include-system-site-packages = false' in cfg.read_text()
assert site.ENABLE_USER_SITE is False and Path(os.environ['PYTHONPATH']).is_absolute()
value={'fingerprint':_environment(sys.executable,Path.home()/'.hermes/hermes-agent'),
       'absolute_pythonpath':os.environ['PYTHONPATH'],
       'import_file':str(Path(rpe_harness.__file__).resolve()),
       'venv_config_sha256':hashlib.sha256(cfg.read_bytes()).hexdigest(),
       'user_site_enabled':site.ENABLE_USER_SITE}
with Path(sys.argv[1]).open('x') as stream:
    json.dump(value,stream,ensure_ascii=False,indent=2)
    stream.write('\n')
