"""Offline consistency checks of retained 1.14 records, never a runtime replay."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parent
REPO=next(p for p in ROOT.parents if (p/'src/rpe_harness').is_dir())
sys.path.insert(0,str(REPO/'src'))


def need(condition,message):
    if not condition:raise ValueError(message)


def read(name):return json.loads((ROOT/name).read_text())
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


EXTERNAL_ENTRIES=(
    ('entry','final-native','entry-ledger','external-final-native-entry-execute','candidate_sha256'),
    ('directed','directed-native','directed-entry-ledger','external-directed-native-entry-execute','actual_prompt_sha256'),
    ('corrected_directed','corrected-directed-native','corrected-directed-entry-ledger',
        'external-corrected-directed-native-entry-execute','actual_prompt_sha256'))


def verify_native_entry(stem,sources,prompt_field):
    """Check retained facts; a consistent failed run remains a failed proof."""
    from rpe_harness.experiments.review_v114_comparison import verify_budget_observation
    entry=read(stem+'-entry.json');events=read(stem+'-entry-events.json')
    need(entry['model_called'] is True,stem+': no actual model call')
    need(type(entry['verified']) is bool and type(entry['execution_gate']['verified']) is bool,
        stem+': invalid entry/gate predicate')
    need(entry['actual_model_source_module_sha256']==sources,stem+': source mismatch')
    candidate_raw=json.dumps(entry['candidate'],ensure_ascii=False,sort_keys=True,allow_nan=False,indent=2).encode()
    need(entry['candidate_sha256']==sha(candidate_raw),stem+': candidate changed')
    prompt_digest=entry[prompt_field]
    need(isinstance(prompt_digest,str) and re.fullmatch('[a-f0-9]{64}',prompt_digest)
        and entry['original_prompt_sha256']==prompt_digest,stem+': actual prompt changed')
    need(type(events) is list and events and all(type(e) is dict for e in events),stem+': events absent')
    fields=('session_id','task_id','turn_id','api_request_id')
    api=lambda e:tuple(e.get(k) for k in fields)
    tool=lambda e:(*api(e),e.get('tool_call_id'),e.get('tool_name'))
    requests=[e for e in events if e.get('kind')=='pre_api_request']
    outcomes=[e for e in events if e.get('kind') in ('post_api_request','api_request_error')]
    pres=[e for e in events if e.get('kind')=='pre_tool_call']
    posts=[e for e in events if e.get('kind')=='post_tool_call']
    errors=sum(e.get('kind')=='api_request_error' for e in outcomes)
    emitted=Counter((*api(e),c.get('tool_call_id'),c.get('tool_name'))
        for e in outcomes for c in e.get('model_tool_calls',[]))
    chain=entry['chain_diagnostics']
    for key,value in {'api_requests':len(requests),'api_outcomes':len(outcomes),'api_errors':errors,
            'tool_pre_calls':len(pres),'tool_post_calls':len(posts),'model_tool_calls':sum(emitted.values())}.items():
        need(chain[key]==value,stem+': retained chain count changed:'+key)
    if chain['verified']:
        request_ids=Counter(map(api,requests));pre_ids=Counter(map(tool,pres))
        need(request_ids and request_ids==Counter(map(api,outcomes)) and not errors
            and all(n==1 for n in request_ids.values()),stem+': API chain invalid')
        need(pre_ids and pre_ids==Counter(map(tool,posts))==emitted and all(n==1 for n in pre_ids.values())
            and all(all(isinstance(value,str) and value for value in tool(e)) for e in pres+posts),
            stem+': tool chain invalid')
        need(Counter((tool(e),e.get('arguments_sha256')) for e in pres)
            ==Counter((tool(e),e.get('arguments_sha256')) for e in posts),stem+': tool arguments changed')
        need(chain['stream_calls']==len(pres),stem+': retained stream count changed')
    budget=entry['request_budget_observation']
    budget_ok=verify_budget_observation(budget)
    budget_rows=[{'max_completion_tokens':e.get('request_max_completion_tokens'),
        'thinking_type':'disabled' if e.get('request_thinking_type')=='disabled' else
            'other' if e.get('request_thinking_type') is not None else None} for e in requests]
    need(budget['api_requests']==len(requests) and budget['requests']==budget_rows[:256],
        stem+': actual request budget observations changed')
    output=ROOT/(stem+'-output.json')
    if entry['output_sha256'] is None:
        need(not output.exists(),stem+': unrecorded output retained')
    else:
        need(sha(output.read_bytes())==entry['output_sha256'],stem+': output changed')
        if entry['business'].get('artifact_sha256') is not None:
            need(entry['business']['artifact_sha256']==entry['output_sha256'],stem+': business output binding changed')
    gate=entry['execution_gate']
    if gate['verified']:
        need(gate['receipt_authenticated'] is True and gate['kernel_exit_observed'] is True
            and type(gate['process_returncode']) is int and gate['process_returncode']==0,
            stem+': runtime authenticated exit unproven')
        receipts=[e for e in events if e.get('kind')=='native_helper_process_observation'
            and e.get('kernel_exit_observed') is True]
        need(len(receipts)==1,stem+': retained unique kernel receipt missing')
        receipt=receipts[0]
        need(receipt.get('verified') is True and type(receipt.get('process_returncode')) is int
            and receipt['process_returncode']==0 and receipt.get('observer_joined') is True
            and receipt.get('overlap') is False and receipt.get('process_observation_error')==''
            and isinstance(receipt.get('process_receipt_hmac_sha256'),str)
            and re.fullmatch('[a-f0-9]{64}',receipt['process_receipt_hmac_sha256']),
            stem+': retained runtime kernel facts changed')
        matching=[e for e in posts if tool(e)==tool(receipt)
            and e.get('arguments_sha256')==receipt.get('arguments_sha256')]
        need(len(matching)==1 and matching[0].get('status')=='succeeded'
            and type(matching[0].get('tool_exit_code')) is int and matching[0]['tool_exit_code']==0,
            stem+': kernel receipt tool binding changed')
        need(gate['helper_call_id']==receipt['tool_call_id']
            and gate['arguments_sha256']==receipt['arguments_sha256']
            and gate['output_sha256']==receipt['output_sha256']==entry['output_sha256'],
            stem+': kernel output binding changed')
        need(entry['business']['business_correct'] is True and entry['business']['product_oracle_passed'] is True
            and entry['input_preserved'] is True,stem+': gated business outcome incorrect')
    if entry['verified']:
        need(entry['completed'] is True and entry['timed_out'] is False and type(entry['exit_code']) is int
            and entry['exit_code']==0 and gate['verified'] is True and budget_ok and chain['verified'] is True,
            stem+': successful entry lacks complete host/budget/chain/gate')
    return {'verified':entry['verified'],'kernel_gate_verified':gate['verified'],
        'api_requests':len(requests),'tool_calls':len(pres),'entry':entry}


def verify(require_cleanup=False):
    from rpe_harness.experiments.review_v114_comparison import verify_v114_evidence,verify_budget_observation
    from rpe_harness.experiments.cost_ledger import summarize_phases,PhaseLedger
    from rpe_harness.experiments.agent_audit import audit_spec,_verdict
    delivery=read('delivery.json')
    need(delivery['schema_version']=='rpe.v114-delivery/v1','delivery schema')
    need(type(delivery['manifest']) is dict and {'evidence/preregistration.json',
        'evidence/formal-freeze.json','budget-amendment.json','all-observed-costs.json',
        'corrected-directed-native-entry.json','codex-audit-result.json',
        'validation/full-suite.xml','validation/package-final.xml'}<=set(delivery['manifest']),
        'required evidence missing from manifest')
    for name,digest in delivery['manifest'].items():
        path=(ROOT/name).resolve()
        need(path.is_relative_to(ROOT.resolve()),'manifest path escaped')
        need(sha(path.read_bytes())==digest,'changed report:'+name)
    evidence=verify_v114_evidence(ROOT/'evidence')
    need(evidence['verified'] is True,'portable evidence:'+str(evidence.get('errors')))
    freeze=read('evidence/formal-freeze.json')
    sources=freeze['environment']['source_sha256']
    need(len(sources)==91 and sources==delivery['source_sha256'],'delivery source freeze mismatch')
    for name,digest in sources.items():
        need(sha((REPO/'src/rpe_harness'/name).read_bytes())==digest,'final source changed:'+name)
    wheel=ROOT/delivery['wheel']['path']
    need(sha(wheel.read_bytes())==delivery['wheel']['sha256'],'wheel changed')
    with zipfile.ZipFile(wheel) as archive:
        names={name.removeprefix('rpe_harness/'):sha(archive.read(name)) for name in archive.namelist()
            if name.startswith('rpe_harness/') and name.endswith('.py')}
        need(names==sources,'wheel modules differ from actual model source freeze')
    tests={}
    for name,expected in delivery['tests'].items():
        xml=ET.parse(ROOT/'validation'/name).getroot()
        cases=list(xml.iter('testcase'))
        failures=sum(c.find('failure') is not None or c.find('error') is not None for c in cases)
        skipped=sum(c.find('skipped') is not None for c in cases)
        suites=[s for s in xml.iter('testsuite') if not s.findall('testsuite')]
        subtests=sum(int(s.get('tests',0)) for s in suites)-len(cases)
        observed={'passed':len(cases)-failures-skipped,'skipped':skipped,'subtests':subtests}
        need(failures==0 and observed==expected,'test counts changed:'+name)
        tests[name]=observed
    plan,summary=read('evidence/preregistration.json'),read('evidence/summary.json')
    phases=read('evidence/cost-phases.json')['records']
    amendment=read('budget-amendment.json')
    need(amendment['schema_version']=='rpe.v114-proof-budget-amendment/v1'
        and amendment['amended']=={'max_draft_sessions':10,'max_native_execution_sessions':5,
            'max_external_entry_sessions':3,'max_cli_processes':3}
        and amendment['paired_trials_replayed'] is False and amendment['old_failures_regraded'] is False
        and amendment['production_source_changed'] is False,'proof-only budget amendment changed')
    entries={};components=[('comparison',phases)]
    for name,stem,ledger,attempt,prompt_field in EXTERNAL_ENTRIES:
        checked=verify_native_entry(stem,sources,prompt_field)
        records=read(ledger.removesuffix('-ledger')+'-cost-phases.json')['records']
        need((ROOT/ledger).is_dir(),name+': ledger absent')
        need(records==PhaseLedger(ROOT/ledger).records(),name+': ledger hashes changed')
        need(len(records)==1 and records[0]['arm']=='native' and records[0]['phase']=='execute'
            and records[0]['attempt_id']==attempt and records[0]['usage']==checked['entry']['usage'],
            name+': ledger identity or usage changed')
        if name=='corrected_directed':
            need(amendment['additional_engineering_correction']['declared_at']<=records[0]['reserved_at'],
                'corrected proof was not declared before reservation')
        entries[name]={key:value for key,value in checked.items() if key!='entry'}
        components.append((name,records))
    all_phases=[{**r,'attempt_id':name+'.'+r['attempt_id']} for name,records in components for r in records]
    costs=read('all-observed-costs.json')
    need(summarize_phases(all_phases)==costs['hermes_all_observed'],'all Hermes phase costs changed')
    need({name:summarize_phases(records) for name,records in components}==costs['hermes_components'],
        'Hermes component costs changed')
    need(len([r for r in phases if r['phase']=='draft'])==10,'draft population not exactly ten')
    native_count=sum(r['phase']=='execute' and r['arm']=='native' for r in all_phases)
    need(native_count<=5 and len(entries)==3,'native execution budget changed')
    audits=[]
    for prefix in ('cli-probe','independent-audit'):
        result=read(prefix+'-result.json')
        claim=read(prefix+'-claim.json')
        bundle=read(prefix+'-bundle.json')
        spec=audit_spec(bundle,profile='structured-v2')
        need(result['profile']==claim['profile']=='structured-v2','CLI profile changed')
        for field in ('max_turns','max_output_tokens','cli_max_budget_usd','timeout_seconds','retry_env'):
            need(claim[field]==spec[field],'CLI requested limit changed:'+field)
        need(claim['schema_sha256']==sha(encoded(spec['schema'])),'CLI schema changed')
        need(result['input_sha256']==claim['input_sha256']==sha(encoded({'audit_contract':spec['system'],'bundle':bundle})),
            'CLI bundle binding changed')
        need(result['verified_bill_usd'] is None,'unverified CLI bill presented as known')
        need(result['status'] in ('completed','failed','timed_out'),'CLI terminal status invalid')
        terminal=result.get('terminal_observation')
        if result['status']=='completed':
            need(type(terminal) is dict and terminal['is_error'] is False and terminal['subtype']=='success'
                and terminal['num_turns']==result['num_turns'],'CLI success terminal changed')
            need(result['exit_code']==0 and _verdict(result['verdict'],{c['evidence_id'] for c in bundle['cases']},
                profile='structured-v2') is not None,'CLI completed without valid verdict')
        else:
            need(result['verdict'] is None,'failed CLI carried a verdict')
        audits.append(result)
    codex=read('codex-audit-result.json');codex_claim=read('codex-audit-claim.json')
    codex_bundle=read('codex-audit-bundle.json')
    need(codex_bundle==read('independent-audit-bundle.json'),'fallback changed audit evidence')
    need(codex['status']=='completed' and codex['exit_code']==0 and codex['num_turns']==1
        and codex['observed_tool_item_count']==0 and codex['auth_copy_deleted'] is True,'fallback completion changed')
    for name,field in (('codex-audit-bundle.json','bundle_sha256'),('codex-audit-schema.json','schema_sha256'),
            ('codex-audit-input.json','input_sha256')):
        need(sha((ROOT/name).read_bytes())==codex[field]==codex_claim[field],'fallback bytes binding changed:'+name)
    need(_verdict(codex['verdict'],{c['evidence_id'] for c in codex_bundle['cases']},
        profile='structured-v2') is not None,'fallback verdict invalid')
    raw=(ROOT/'codex-audit-events.jsonl').read_bytes()
    need(sha(raw)==codex['stdout_sha256'],'fallback stdout changed')
    events=[json.loads(line) for line in raw.splitlines() if line.strip()]
    from collections import Counter
    need(dict(Counter(e['type'] for e in events))==codex['event_types'],'fallback event counts changed')
    completed=[e for e in events if e['type']=='turn.completed']
    items=[e['item'] for e in events if e['type']=='item.completed']
    messages=[e for e in items if e.get('type')=='agent_message']
    need(len(completed)==len(messages)==1 and completed[0]['usage']==codex['usage']
        and json.loads(messages[0]['text'])==codex['verdict'],'fallback usage/verdict observation changed')
    need(all(e.get('type') in ('agent_message','error') for e in items),'fallback used an unreported tool')
    need(sha((ROOT/'codex-audit-last-message.json').read_bytes())==codex['last_message_sha256']
        and read('codex-audit-last-message.json')==codex['verdict'],'fallback final message changed')
    need(codex_claim['wrapper_retries']==0 and codex_claim['cli_process_limit']==1
        and codex_claim['timeout_seconds']==180 and codex['elapsed_seconds']<=185,'fallback limits changed')
    need(codex['process_cleanup']['active_owned_cli_process_count']==0
        and codex['process_cleanup']['auth_copy_exists'] is False,'fallback cleanup unproven')
    audits.append(codex)
    need(len(read('independent-audit-bundle.json')['cases'])==8,'final audit population changed')
    estimate=sum(r['cli_estimated_cost_usd'] for r in audits if r['cli_estimated_cost_usd'] is not None)
    need(estimate==costs['cli_estimated_cost_usd_sum'] and costs['verified_total_bill_usd'] is None,'cost or billing changed')
    need(type(costs['cli_processes']) is int and costs['cli_processes']==len(audits)==3,'CLI process budget changed')
    if require_cleanup:
        cleanup=read('cleanup.json');preserved=read('validation/preservation-after-cleanup.json')
        need(cleanup['complete'] is True and preserved['preservation_ok'] is True,'cleanup/preservation unproven')
        need(all(not Path(p).exists() for p in cleanup['deleted_paths']),'owned paths remain')
    return {'verified':True,'scope':'offline_record_consistency_not_runtime_replay',
        'source_modules':len(sources),'model_validated_final_entry':entries['corrected_directed']['verified'],
        'final_model_entry_verified':entries['corrected_directed']['verified'],'external_native_entries':entries,
        'formal_complete_chain':{arm:summary['by_arm'][arm]['complete_chain'] for arm in ('native','review')},
        'native_execution_sessions':native_count,'cli_processes':costs['cli_processes'],
        'final_cli_audit_completed':any(r['status']=='completed' for r in audits[1:]),'tests':tests,'cleanup_required':require_cleanup}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--require-cleanup',action='store_true')
    arguments=parser.parse_args()
    try:result=verify(arguments.require_cleanup)
    except (OSError,ValueError,KeyError,TypeError,AssertionError) as error:
        print(json.dumps({'verified':False,'error':str(error)},ensure_ascii=False));raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False,indent=2))
