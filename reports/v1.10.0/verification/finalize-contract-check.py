"""Export a completed selected smoke pair; preserve original comparison scores."""
from pathlib import Path
import json
import shutil

from rpe_harness.experiments.address_comparison import _read, _save, _sha
from rpe_harness.experiments.cost_ledger import summarize_phases
from rpe_harness.experiments.review_comparison import (
    export_review_evidence, summarize_review_comparison, verify_review_evidence,
)

OWNED = Path('/Users/audrey/项目/缓存/test/rpe-v110-37f23ae55b694cb3912f32f88684cfe2')
REPORT = Path('/Users/audrey/.codex/worktrees/rpe-v110/rpe-harness/reports/v1.10.0')
SOURCE = OWNED/'comparison-contract-check'
DESTINATION = REPORT/'contract-check'


def total_wall(records):
    values = [row.get('full_trial_elapsed_seconds') for row in records]
    return sum(values) if all(type(value) in {int,float} and value>=0 for value in values) else None


def main():
    if len(list((SOURCE/'records').glob('*.json')))!=2:
        raise SystemExit('diagnostic_pair_not_complete')
    plan = _read(SOURCE/'preregistration.json')
    if (plan.get('experiment_role')!='post_diagnostic_smoke'
        or plan.get('selected_case_ids')!=['json-lossless'] or plan['sessions']):
        raise SystemExit('not_selected_diagnostic_registration')
    summary = summarize_review_comparison(SOURCE)
    if summary['pilot_attempts']!=2 or any(row['status']=='incomplete' for row in summary['records']):
        raise SystemExit('diagnostic_pair_not_complete')
    if not summary.get('driver_records') or summary['driver_records'][-1]['status']!='completed':
        raise SystemExit('diagnostic_driver_not_complete')
    export_review_evidence(SOURCE, DESTINATION)
    verdict = verify_review_evidence(DESTINATION)
    _save(REPORT/'contract-check-offline-verification.json', verdict)
    if not verdict['verified']:
        raise SystemExit('diagnostic_offline_verification_failed')

    registrations = [('pilot-original','pilot-original'),
                     ('formal-frozen','public-evidence'),
                     ('post-diagnostic-smoke','contract-check')]
    sources, phases, summaries, references = [], [], [], []
    for namespace, directory in registrations:
        root = REPORT/directory
        observed = _read(root/'summary.json')
        rows = _read(root/'cost-phases.json')['records']
        summaries.append(observed)
        sources.append({'registration':namespace,'fingerprint':observed['fingerprint'],
            'source_file':directory+'/cost-phases.json',
            'source_file_sha256':_sha((root/'cost-phases.json').read_bytes())})
        phases += [{**row,'attempt_id':namespace+'--'+row['attempt_id']} for row in rows]
        references += [{'registration':namespace,'namespaced_attempt_id':namespace+'--'+row['attempt_id'],
            'original_attempt_id':row['attempt_id'],'original_record_sha256':row['record_sha256']} for row in rows]
    if sum(row['phase']=='draft' for row in phases)!=16:
        raise SystemExit('combined_diagnostic_draft_budget_mismatch')
    if sum(row['phase']=='execute' and row['arm']=='native' for row in phases)>8:
        raise SystemExit('combined_diagnostic_native_budget_exceeded')
    smoke_phases = [row for row in phases if row['attempt_id'].startswith('post-diagnostic-smoke--')]
    all_records = [row for observed in summaries for row in observed['records']]
    base = _read(REPORT/'overall-costs.json')
    original_whole = base['whole_costs']
    base.pop('sha256')
    base.update(source_registrations=sources, phase_references=references,
        pre_diagnostic_whole_costs=original_whole, whole_costs=summarize_phases(phases),
        post_diagnostic_smoke_costs=summarize_phases(smoke_phases),
        driver_elapsed_seconds_sum=sum(row['elapsed_seconds'] for observed in summaries
            for row in observed.get('driver_records',[])),
        driver_invocations=sum(len(observed.get('driver_records',[])) for observed in summaries),
        whole_full_trial_elapsed_seconds_sum=total_wall(all_records),
        draft_sessions_reserved=16,
        native_execution_sessions_reserved=sum(row['phase']=='execute' and row['arm']=='native' for row in phases),
        diagnostic_selection_policy=('JSON pair selected after the original negative comparison. '
            'Report independently; never combine it with formal quality denominators or rerate earlier outcomes.'),
        populations={'original_comparison':{'draft_budget':14,'native_execution_budget':7},
            'post_diagnostic_smoke':{'selected_case_ids':['json-lossless'],'draft_budget':2,
                'native_execution_budget':1,'role':'post_diagnostic_smoke'}})
    for arm in ('native','review'):
        arm_cost = base['by_arm'][arm]
        arm_cost['pre_diagnostic_whole_costs'] = arm_cost['whole_costs']
        arm_cost['whole_costs'] = summarize_phases(row for row in phases if row['arm']==arm)
        arm_cost['post_diagnostic_smoke_costs'] = summarize_phases(row for row in smoke_phases if row['arm']==arm)
        arm_cost['whole_full_trial_elapsed_seconds_sum'] = total_wall([row for row in all_records if row['arm']==arm])
    original_cost_path = REPORT/'overall-costs-original-pilot-and-formal.json'
    if original_cost_path.exists():
        raise SystemExit('original_cost_snapshot_already_exists')
    shutil.copyfile(REPORT/'overall-costs.json', original_cost_path)
    (REPORT/'overall-costs.json').unlink()
    _save(REPORT/'overall-costs.json', base)
    findings = {'schema_version':'rpe.post-diagnostic-findings/v1',
        'experiment_role':'post_diagnostic_smoke','selected_case_ids':['json-lossless'],
        'fingerprint':summary['fingerprint'],'offline_verified':verdict['verified'],
        'public_business_artifacts_verified':verdict['business_artifacts_verified'],
        'public_approved_proposals_verified':verdict['approved_proposals_verified'],
        'original_formal_outcomes_unchanged':True,
        'reviewer_kind':'simulated_fixture_reviewer','human_confirmation_verified':False,
        'population_limit':'Selected diagnostic pair after observing failures; no population-quality or savings estimate.',
        'records':[{key:row.get(key) for key in ('trial_id','arm','category','status','draft_faithful',
            'approved','business_correct','product_oracle_passed','accepted','comparable',
            'draft_invocation_verified','execution_verified','execution_kind','model_identity',
            'full_trial_elapsed_seconds')} for row in summary['records']],
        'post_diagnostic_smoke_costs':base['post_diagnostic_smoke_costs']}
    _save(REPORT/'post-diagnostic-findings.json', findings)
    print(json.dumps({'offline_verified':verdict['verified'],
        'selected_diagnostic_records':findings['records'],
        'whole_costs':base['whole_costs'],'post_diagnostic_costs':base['post_diagnostic_smoke_costs']},ensure_ascii=False))


if __name__=='__main__':
    main()
