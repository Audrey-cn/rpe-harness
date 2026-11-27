import json
import hashlib

from rpe_harness.capability import SkillEntry
from rpe_harness.integrations.hermes_session import prepare_home
from rpe_harness.server import HarnessHandler
from rpe_harness.evolution.sandbox import run_candidate


def test_registered_json_skill_is_displayed_as_executable():
    skill = SkillEntry('builtin/json-normalize', 'JSON', 'Whitespace normalization')
    assert HarnessHandler._skill(None, skill)['executable'] is True


def test_skill_mode_prepares_native_skill_tools_without_daily_configuration(tmp_path):
    home = prepare_home(tmp_path / 'session', tmp_path, tmp_path / 'runs.sqlite',
                        tool_enabled=True, entry_mode='skills',
                        settings={'provider': 'xiaomi', 'model': 'fixture-model',
                                  'base_url': 'https://example.test/v1', 'api_key': 'fixture-key'})
    config = json.loads((home / 'config.yaml').read_text())
    assert config['platform_toolsets']['cli'] == ['rpe_harness', 'skills']
    assert config['skills']['external_dirs'] == []
    assert config['curator']['enabled'] is False
    assert config['memory']['memory_enabled'] is False
    assert config['tools']['tool_search']['enabled'] == 'off'


def test_unicode_owned_candidate_paths_execute_without_granting_extra_writes(tmp_path):
    owned = tmp_path / '中文候选'
    owned.mkdir()
    source = owned / '源.csv'
    source.write_bytes(b'id,value\n001,first\n')
    code = b'''from pathlib import Path
def prepare(plan, staging_path):
    source = Path(plan['input_path'])
    try:
        (source.parent / 'unauthorized.txt').write_text('should fail')
    except PermissionError:
        pass
    else:
        raise RuntimeError('sandbox allowed an ungranted write')
    Path(staging_path).write_bytes(source.read_bytes())
'''
    result = run_candidate(code, {'capability':'tabular.deduplicate',
                                  'input_format':'csv','input_path':str(source),
                                  'input_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}, owned)
    assert result == b'id,value\n001,first\n'
    assert source.read_bytes() == b'id,value\n001,first\n'


def test_materialized_skill_bytes_are_immutable_in_actual_host_sandbox(tmp_path):
    import subprocess
    import sys
    from rpe_harness.integrations.hermes_session import sandbox_profile
    skills=tmp_path/'skills'
    skills.mkdir()
    script=skills/'helper.py'
    script.write_text('original')
    config=tmp_path/'skill-config.json'
    config.write_text('original-config')
    profile=tmp_path/'sandbox.sb'
    profile.write_text(sandbox_profile([tmp_path],tmp_path/'private-home',
                       protected_write_roots=[skills,config]))
    probe='''from pathlib import Path
import sys
for name in sys.argv[1:]:
 p=Path(name)
 for action in (lambda:p.write_text('changed'),lambda:p.chmod(0o600),lambda:p.unlink()):
  try:action()
  except PermissionError:pass
  else:raise RuntimeError('mutable skill snapshot')
Path(sys.argv[1]).parent.parent.joinpath('allowed.txt').write_text('allowed')
'''
    result=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),sys.executable,'-B','-c',probe,
          str(script),str(config)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert script.read_text()=='original'
    assert config.read_text()=='original-config'
    assert (tmp_path/'allowed.txt').read_text()=='allowed'


def test_immutable_snapshot_ancestors_cannot_be_renamed(tmp_path):
    import subprocess
    import sys
    from rpe_harness.integrations.hermes_session import sandbox_profile
    home=tmp_path/'home'
    skills=home/'skills'
    skills.mkdir(parents=True)
    helper=skills/'helper.py'
    helper.write_text('original')
    profile=tmp_path/'sandbox.sb'
    profile.write_text(sandbox_profile([tmp_path],tmp_path/'private',protected_write_roots=[skills]))
    code='''from pathlib import Path
import sys
p=Path(sys.argv[1]);destination=p.parent/'home-moved'
try:p.rename(destination)
except PermissionError:pass
else:raise RuntimeError('ancestor rename bypass')
(p/'history.txt').write_text('allowed')
'''
    result=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),sys.executable,'-B','-c',code,str(home)],
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert helper.read_text()=='original'
    assert (home/'history.txt').read_text()=='allowed'
