"""Exact capability dispatch over one durable execution and publication pipeline."""
from .contracts import ExecutionPlan


def plan_from_request(payload):
    if isinstance(payload, dict) and payload.get('capability') == 'xlsx.inspect':
        from .skill_inspection import SkillInspectionPlan
        return SkillInspectionPlan.from_request(payload)
    if isinstance(payload, dict) and payload.get('capability') == 'json.normalize':
        from .json_contract import JsonNormalizePlan
        return JsonNormalizePlan.from_request(payload)
    return ExecutionPlan.from_request(payload)


def plan_from_dict(value):
    if isinstance(value, dict) and value.get('capability') == 'xlsx.inspect':
        from .skill_inspection import SkillInspectionPlan
        return SkillInspectionPlan.from_dict(value)
    if isinstance(value, dict) and value.get('capability') == 'json.normalize':
        from .json_contract import JsonNormalizePlan
        return JsonNormalizePlan.from_dict(value)
    return ExecutionPlan.from_dict(value)


def prepare_plan(plan):
    if plan.capability == 'xlsx.inspect':
        from .skill_inspection import prepare_skill_inspection
        return prepare_skill_inspection(**plan.execution_kwargs())
    if plan.capability == 'json.normalize':
        from .json_execution import prepare_json_normalization
        return prepare_json_normalization(**plan.execution_kwargs())
    from .execution import prepare_deduplication
    return prepare_deduplication(**plan.execution_kwargs())


def verify_plan(prepared, plan):
    if plan.capability == 'xlsx.inspect':
        from .skill_verification import verify_skill_inspection
        return verify_skill_inspection(prepared, plan=plan)
    if plan.capability == 'json.normalize':
        from .json_verification import verify_json_normalization
        return verify_json_normalization(prepared, plan=plan)
    from .verification import verify_deduplication
    return verify_deduplication(prepared, plan=plan)


def artifact_metrics(verification):
    fields = ('input_bytes', 'output_bytes', 'root_type') if 'input_bytes' in verification else (
        'input_rows', 'output_rows', 'removed_rows')
    return {key: verification[key] for key in fields}


def current_plan_revisions(plan):
    if plan.capability == 'xlsx.inspect':
        from .skill_inspection import current_revisions
        return current_revisions()
    if plan.capability == 'json.normalize':
        from .json_contract import current_revisions
        return current_revisions()
    from .contracts import EXECUTOR_REVISION, VERIFIER_REVISION
    return {'executor_revision': EXECUTOR_REVISION, 'verifier_revision': VERIFIER_REVISION}


def contract_from_plan(plan):
    if plan.capability == 'xlsx.inspect':
        from .skill_inspection import contract_from_plan as inspection_contract
        return inspection_contract(plan)
    if plan.capability == 'json.normalize':
        execution = {'input_path': plan.input_path, 'output_path': plan.output_path, 'style': plan.style}
    else:
        execution = {'input_path': plan.input_path, 'output_path': plan.output_path,
            'key_columns': list(plan.key_columns), 'sheet': plan.sheet, 'keep': plan.keep,
            'encoding': plan.encoding, 'delimiter': plan.delimiter}
    return {'capability': plan.capability, 'contract_version': plan.contract_version,
            'constraints': list(plan.constraints), 'execution': execution}
