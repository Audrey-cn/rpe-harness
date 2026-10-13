from pathlib import Path
import json,hashlib,zipfile,sys
root=Path("/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T/rpe-v12-development-20261014-fp1l7bvp")
repo=Path("/Users/audrey/项目/rpe-harness")
sys.path.insert(0,str(repo/"src"))
from rpe_harness.integrations.hermes_session import _read_host_settings
settings=_read_host_settings(Path("/Users/audrey/.hermes/hermes-agent"))
secrets=[settings["api_key"].encode()]
# CLI authorization was consumed read-only; scan its currently configured token
# values in memory without retaining or printing their contents.
auth=Path.home()/".codex/auth.json"
if auth.is_file():
    def visit(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if isinstance(item,str) and len(item)>=20 and ("token" in key.lower() or "key" in key.lower()):
                    secrets.append(item.encode())
                else:visit(item)
        elif isinstance(value,list):
            for item in value:visit(item)
    visit(json.loads(auth.read_text()))
files=[]
for base in [repo/"reports/v1.2.0",repo/"src/rpe_harness",repo/"tests",repo/"integrations/hermes",repo/"docs/superpowers"]:
    files.extend(p for p in base.rglob("*") if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts)
files += [repo/"README.md",repo/"pyproject.toml"]
hits=[];members=0
for path in files:
    if path.suffix==".whl":
        with zipfile.ZipFile(path) as archive:
            entries=[(name,archive.read(name)) for name in archive.namelist() if not name.endswith("/")]
    else:entries=[("",path.read_bytes())]
    for member,data in entries:
        members+=1
        if any(secret in data for secret in secrets):
            hits.append({"path":str(path.relative_to(repo)),"member":member})
report={"files_checked":len(files),"members_checked":members,"credential_values_checked":len(secrets),"credential_contents_retained":False,"exact_value_matches":hits,"passed":not hits}
(repo/"reports/v1.2.0/evidence/credential-exact-scan.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False))
assert not hits
