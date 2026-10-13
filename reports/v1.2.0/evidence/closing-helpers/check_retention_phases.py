from pathlib import Path
import json,hashlib
base=Path("/Users/audrey/项目/rpe-harness/reports/v1.2.0")
ledger=json.loads((base/"evidence/initial-retention-manifest.json").read_text())
index={}
for path in base.rglob("*"):
    if path.is_file() and not path.is_symlink():
        index.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(),[]).append(str(path.relative_to(base)))
items=[]
for item in ledger:
    current=base/item["retained"]
    current_matches=current.is_file() and hashlib.sha256(current.read_bytes()).hexdigest()==item["sha256"]
    preserved=index.get(item["sha256"],[])
    items.append({"original_copy_destination":item["retained"],"original_copy_sha256":item["sha256"],"current_destination_matches_original":current_matches,"preserved_original_bytes_at":preserved})
    assert preserved,item["retained"]
report={"ledger":"initial-retention-manifest.json","interpretation":"The ledger records original copy events. Closing validation replaces the top-level final wheel, source manifest, test XML and installed evidence; historical bytes remain in explicit snapshot directories listed below. It is not a claim that every original destination still contains its old snapshot.","items":items,"all_original_bytes_preserved":True}
(base/"evidence/retention-phases.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({"ledger_items":len(items),"all_original_bytes_preserved":True,"destinations_updated":sum(not item["current_destination_matches_original"] for item in items)}))
