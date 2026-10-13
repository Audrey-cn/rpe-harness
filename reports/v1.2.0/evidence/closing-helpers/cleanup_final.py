from pathlib import Path
import os,json,hashlib,shutil,stat,subprocess,datetime
root=Path("/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T/rpe-v12-development-20261014-fp1l7bvp")
repo=Path("/Users/audrey/项目/rpe-harness")
assert root.name=="rpe-v12-development-20261014-fp1l7bvp" and root.parent==Path("/private/var/folders/6f/1dz7k1mx6mlbk4vwbrg2n00c0000gn/T")
assert not root.is_symlink() and root.is_dir()
initial=json.loads((root/"initial-state.json").read_text());assert initial["owned_root"]==str(root)
def process_rows():
    out=subprocess.run(["/bin/ps","-axo","pid=,ppid=,command="],capture_output=True,text=True,check=True)
    result={}
    for line in out.stdout.splitlines():
        fields=line.strip().split(None,2)
        if len(fields)==3 and fields[0].isdigit() and fields[1].isdigit():result[int(fields[0])]=(int(fields[1]),fields[2])
    return result
def owned_processes():
    rows=process_rows();exclude={os.getpid()};cursor=os.getpid()
    while cursor in rows and rows[cursor][0] not in exclude:
        cursor=rows[cursor][0];exclude.add(cursor)
    return [{"pid":pid,"command_sha256":hashlib.sha256(cmd.encode()).hexdigest()} for pid,(parent,cmd) in rows.items() if pid not in exclude and str(root) in cmd]
remaining=owned_processes()
assert not remaining, {"owned_processes_still_running":remaining}
security=json.loads((repo/"reports/v1.2.0/evidence/credential-exact-scan.json").read_text());assert security["passed"]
daily=json.loads((root/"final-daily-observation.json").read_text());assert all(not rows for rows in daily["known_test_sessions_in_daily_database"].values())
# Only one known external, ephemeral CLI generation session belongs to the tests.
# The desktop project conversation and all other user history are excluded.
cli_uuid="01a13720-c6ca-7621-b808-8710d54e34a0"
history=Path.home()/".codex/sessions";found=[]
if history.is_dir():
    for path in history.rglob("*"):
        if path.is_file() and not path.is_symlink() and cli_uuid in path.name:
            found.append(str(path.relative_to(history)));path.unlink()
alias=Path.home()/".codex/tmp/arg0"
aliases=[{"name":p.name,"modified_epoch":p.stat().st_mtime} for p in alias.iterdir()] if alias.is_dir() else []
# No aliases were registered as owned by this run; preserve all global aliases.
regular_bytes=0;file_count=0;symlinks=0
for directory,dirs,files in os.walk(root,followlinks=False):
    for name in dirs+files:
        p=Path(directory)/name;s=p.lstat()
        if stat.S_ISLNK(s.st_mode):symlinks+=1
        elif stat.S_ISREG(s.st_mode):regular_bytes+=s.st_size;file_count+=1
# Only task-timestamped cache files in this checkout; preserve older/shared caches.
started_epoch=datetime.datetime.fromisoformat(initial["started_utc"].replace("Z","+00:00")).timestamp()
project_cache_files=[]
cache_directories=[repo/".pytest_cache",*(repo/"src").rglob("__pycache__"),*(repo/"tests").rglob("__pycache__")]
for cache_dir in cache_directories:
    if cache_dir.is_symlink() or not cache_dir.is_dir():continue
    for path in cache_dir.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.stat().st_mtime>=started_epoch:
            project_cache_files.append(str(path.relative_to(repo)));path.unlink()
    for directory in sorted([p for p in cache_dir.rglob("*") if p.is_dir() and not p.is_symlink()],key=lambda p:len(p.parts),reverse=True)+[cache_dir]:
        try:directory.rmdir()
        except OSError:pass
shutil.rmtree(root)
assert not root.exists()
remaining_after=owned_processes();assert not remaining_after
report={"completed_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),"owned_root":str(root),"owned_directory_deleted":True,"regular_files_removed":file_count,"regular_bytes_removed":regular_bytes,"symlinks_removed_without_following":symlinks,"owned_processes_before_delete":remaining,"owned_processes_after_delete":remaining_after,"external_cli_session_uuid":cli_uuid,"external_cli_history_files_removed":found,"global_arg0_aliases_preserved":aliases,"global_alias_deletion_performed":False,"project_task_timestamped_cache_files_removed":project_cache_files,"daily_observation":daily,"credential_values_in_retained_artifacts_absent":True,"preserved_scope":["daily Hermes configuration, authentication and state","all unrelated user history","shared runtime/cache installations","existing gateways and services","concurrent daily-source/config/auth/state changes"],"scope_limit":"No initial machine-wide process snapshot; verifies absence of owned test processes, not identity of every global service.","local_branch":"codex/rpe-harness-v1.2","local_release_tag":"v1.2.0"}
(repo/"reports/v1.2.0/evidence/cleanup.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({"owned_directory_deleted":True,"files_removed":file_count,"bytes_removed":regular_bytes,"remaining_owned_processes":len(remaining_after),"our_sessions_in_daily_history":daily["known_test_sessions_in_daily_database"],"concurrent_global_changes_preserved":True}))
