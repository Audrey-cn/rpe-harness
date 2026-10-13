from pathlib import Path
import json,hashlib,sqlite3,subprocess
root=Path("/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T/rpe-v12-development-20261014-fp1l7bvp");repo=Path('/Users/audrey/项目/rpe-harness')
initial=json.loads((root/'initial-state.json').read_text())
daily=Path.home()/'.hermes'
checks={}
for name,old in initial['daily_files'].items():
 p=daily/name
 new={'exists':p.is_file(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None}
 checks[name]={'initial':old,'current':new,'unchanged':old==new}
hermes=Path('/Users/audrey/.hermes/hermes-agent')
head=subprocess.run(['git','rev-parse','HEAD'],cwd=hermes,text=True,capture_output=True,check=True).stdout.strip()
status=subprocess.run(['git','status','--porcelain'],cwd=hermes,text=True,capture_output=True,check=True).stdout
# Read only metadata from daily database. Never read user message bodies.
session_ids=set()
for p in list((repo/'reports/v1.2.0/evidence').rglob('*.json')):
 try:x=json.loads(p.read_text())
 except ValueError:continue
 def collect(item):
  if isinstance(item,dict):
   sid=item.get('session_id')
   if isinstance(sid,str) and sid:session_ids.add(sid)
   for value in item.values():collect(value)
  elif isinstance(item,list):
   for value in item:collect(value)
 collect(x)
found={}
db_path=daily/'state.db'
if db_path.is_file():
 with sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True) as db:
  tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  for table,column in [('sessions','id'),('messages','session_id')]:
   if table not in tables:continue
   columns={row[1] for row in db.execute('PRAGMA table_info('+table+')')}
   if column not in columns:continue
   ids=sorted(session_ids)
   if ids:
    rows=db.execute('SELECT DISTINCT '+column+' FROM '+table+' WHERE '+column+' IN ('+','.join('?' for _ in ids)+')',ids).fetchall()
    found[table]=[row[0] for row in rows]
report={'phase':'final read-only observation before owned cleanup','daily_files':checks,'hermes_head':head,'head_unchanged':head==initial['hermes_head'],'git_status':status,'git_status_unchanged':status==initial['hermes_git_status'],'known_test_session_ids_checked':len(session_ids),'known_test_sessions_in_daily_database':found,'daily_database_opened_read_only':True,'restoration_or_deletion_performed':False}
(root/'final-daily-observation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'unchanged_file_flags':{name:x['unchanged'] for name,x in checks.items()},'head_unchanged':report['head_unchanged'],'git_status_unchanged':report['git_status_unchanged'],'session_ids_checked':len(session_ids),'our_sessions_found':found},ensure_ascii=False))

