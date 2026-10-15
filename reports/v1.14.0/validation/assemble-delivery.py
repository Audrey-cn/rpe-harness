"""Assemble only allowlisted sanitized reports from this owned finite run."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

sys.dont_write_bytecode=True
OWNED=Path('/Users/audrey/项目/缓存/test/rpe-v114-8581d6e12eab449aaa71bf8af01ce97b')
REPO=Path(__file__).resolve().parents[3]
OUT=REPO/'reports/v1.14.0'
sys.path.insert(0,str(REPO/'src'))


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2))
def load(path):return json.loads(path.read_text())
def sha(raw):return hashlib.sha256(raw).hexdigest()


EXTERNAL_ENTRIES=(('entry','final-native','entry-ledger'),
    ('directed','directed-native','directed-entry-ledger'),
    ('corrected_directed','corrected-directed-native','corrected-directed-entry-ledger'))


def main():
    from rpe_harness.experiments.cost_ledger import PhaseLedger,summarize_phases
    xmls={'baseline.xml':'baseline.xml','full-suite.xml':'full-suite.xml','frozen-entry.xml':'frozen-entry.xml',
        'cli-readonly/final-results.xml':'cli-profile.xml','native-evidence/finish.xml':'native-evidence.xml'}
    for source,target in xmls.items():shutil.copyfile(OWNED/source,OUT/'validation'/target)
    for source,target in (
        ('cli-probe-result.json','cli-probe-result.json'),
        ('cli-probe/agent-audit/claim.json','cli-probe-claim.json'),
        ('independent-audit-result.json','independent-audit-result.json'),
        ('agent-audit/claim.json','independent-audit-claim.json'),
        ('audit-blind-bundle.json','independent-audit-bundle.json'),
        ('audit-blind-mapping.json','independent-audit-mapping.json')):
        shutil.copyfile(OWNED/source,OUT/target)
    save(OUT/'cli-probe-bundle.json',load(OWNED/'cli-probe-preregistration.json')['bundle'])
    phases=load(OUT/'evidence/cost-phases.json')['records']
    components=[('comparison',phases)]
    entry_predicates={}
    for name,stem,ledger_name in EXTERNAL_ENTRIES:
        for suffix in ('-entry.json','-entry-events.json','-output.json'):
            shutil.copyfile(OWNED/(stem+suffix),OUT/(stem+suffix))
        ledger=OUT/ledger_name;ledger.mkdir(exist_ok=True)
        for path in (OWNED/ledger_name).glob('*.json'):shutil.copyfile(path,ledger/path.name)
        records=PhaseLedger(OWNED/ledger_name).records()
        save(OUT/(ledger_name.removesuffix('-ledger')+'-cost-phases.json'),{'records':records})
        components.append((name,records))
        entry_predicates[name]=load(OUT/(stem+'-entry.json'))['verified']
    combined=[{**r,'attempt_id':name+'.'+r['attempt_id']} for name,records in components for r in records]
    audits=[load(OUT/(prefix+'-result.json')) for prefix in ('cli-probe','independent-audit')]
    save(OUT/'all-observed-costs.json',{'schema_version':'rpe.v114-all-costs/v1',
        'hermes_all_observed':summarize_phases(combined),
        'hermes_components':{name:summarize_phases(records) for name,records in components},
        'hermes_phase_namespaces':[name+'.' for name,_ in components],
        'cli_processes':len(audits),'cli_estimated_cost_usd_sum':sum(
            r['cli_estimated_cost_usd'] for r in audits if r['cli_estimated_cost_usd'] is not None),
        'cli_unknown_cost_processes':sum(r['cli_estimated_cost_usd'] is None for r in audits),
        'cli_usage':[r.get('usage') for r in audits],
        'verified_total_bill_usd':None,'development_agent_cost_usd':None,
        'unknowns':['CLI/provider physical retries','verified billing','development agents and local compute'],
        'timing_policy':'phase sums include disjoint phase observations; never add overlapping driver/full-trial timers'})
    plan=load(OWNED/'comparison/preregistration.json');freeze=load(OWNED/'comparison/formal-freeze.json')
    native=load(OWNED/'comparison/records/pilot-pilot-cold-recheck-native.json')
    source_changed=[name for name,digest in plan['environment']['source_sha256'].items()
        if freeze['environment']['source_sha256'].get(name)!=digest]
    save(OUT/'engineering-pilot-origin.json',{'schema_version':'rpe.v114-pilot-origin/v1',
        'registration_sha256':plan['sha256'],'registration_environment':plan['environment'],
        'actual_native_host_prompt_sha256':native['actual_host_prompt_sha256'],
        'native_pilot_accepted':False,'native_pilot_replayed':False,
        'changed_before_formal_freeze':source_changed,'new_runtime_note_model_validated_only_by_formal_samples':True})
    tests={}
    for path in (OUT/'validation').glob('*.xml'):
        xml=ET.parse(path).getroot();cases=list(xml.iter('testcase'))
        failures=sum(c.find('failure') is not None or c.find('error') is not None for c in cases)
        skipped=sum(c.find('skipped') is not None for c in cases)
        suites=[s for s in xml.iter('testsuite') if not s.findall('testsuite')]
        if failures:raise ValueError('not a passing retained XML:'+path.name)
        tests[path.name]={'passed':len(cases)-skipped,'skipped':skipped,
            'subtests':sum(int(s.get('tests',0)) for s in suites)-len(cases)}
    save(OUT/'validation/test-records.json',tests)
    release=OUT/'release';release.mkdir(exist_ok=True)
    wheel=REPO/'dist/rpe_harness-1.14.0-py3-none-any.whl'
    shutil.copyfile(wheel,release/wheel.name)
    save(OUT/'delivery.json',{'schema_version':'rpe.v114-delivery/v1','source_commit':'5d8bb85',
        'source_sha256':freeze['environment']['source_sha256'],'tests':tests,
        'external_native_entry_verified':entry_predicates,
        'final_model_entry_verified':entry_predicates['corrected_directed'],
        'wheel':{'path':'release/'+wheel.name,'sha256':sha(wheel.read_bytes())},'manifest':{}})
    print(json.dumps({'assembled':True,'tests':tests,'source_modules':len(freeze['environment']['source_sha256']),
        'external_native_entry_verified':entry_predicates,
        'final_model_entry_verified':entry_predicates['corrected_directed'],
        'native_execution_sessions':sum(r['phase']=='execute' and r['arm']=='native' for r in combined)}))


if __name__=='__main__':main()
