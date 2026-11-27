"""Exact capability dispatch over one durable execution and publication pipeline."""
from .contract_sdk import DEFAULT_REGISTRY, PluginExecutionPlan
from .contracts import ExecutionPlan


def plan_from_request(payload):
    if isinstance(payload, dict) and payload.get('capability') == 'xlsx.inspect':
        from .skill_inspection import SkillInspectionPlan
        return SkillInspectionPlan.from_request(payload)
    if isinstance(payload, dict) and payload.get('capability') == 'json.normalize':
        from .json_contract import JsonNormalizePlan
        return JsonNormalizePlan.from_request(payload)
    if isinstance(payload, dict) and isinstance(payload.get('capability'), str):
        if DEFAULT_REGISTRY.has(payload['capability']):
            return DEFAULT_REGISTRY.plan_from_request(payload)
    return ExecutionPlan.from_request(payload)


def plan_from_dict(value):
    if isinstance(value, dict) and value.get('capability') == 'xlsx.inspect':
        from .skill_inspection import SkillInspectionPlan
        return SkillInspectionPlan.from_dict(value)
    if isinstance(value, dict) and value.get('capability') == 'json.normalize':
        from .json_contract import JsonNormalizePlan
        return JsonNormalizePlan.from_dict(value)
    if isinstance(value, dict) and isinstance(value.get('capability'), str):
        if DEFAULT_REGISTRY.has(value['capability']):
            return PluginExecutionPlan.from_dict(value)
    return ExecutionPlan.from_dict(value)


def prepare_plan(plan):
    if plan.capability == 'xlsx.inspect':
        from .skill_inspection import prepare_skill_inspection
        return prepare_skill_inspection(**plan.execution_kwargs())
    if plan.capability == 'json.normalize':
        from .json_execution import prepare_json_normalization
        return prepare_json_normalization(**plan.execution_kwargs())
    if isinstance(plan, PluginExecutionPlan):
        return DEFAULT_REGISTRY.prepare(plan)
    from .execution import prepare_deduplication
    return prepare_deduplication(**plan.execution_kwargs())


def verify_plan(prepared, plan):
    if plan.capability == 'xlsx.inspect':
        from .skill_verification import verify_skill_inspection
        return verify_skill_inspection(prepared, plan=plan)
    if plan.capability == 'json.normalize':
        from .json_verification import verify_json_normalization
        return verify_json_normalization(prepared, plan=plan)
    if isinstance(plan, PluginExecutionPlan):
        return DEFAULT_REGISTRY.verify(prepared, plan)
    from .verification import verify_deduplication
    return verify_deduplication(prepared, plan=plan)


def artifact_metrics(verification):
    if 'input_rows' in verification and 'output_rows' in verification and 'removed_rows' in verification:
        return {k: verification[k] for k in ('input_rows', 'output_rows', 'removed_rows')}
    if 'input_bytes' in verification and 'output_bytes' in verification and 'root_type' in verification:
        return {k: verification[k] for k in ('input_bytes', 'output_bytes', 'root_type')}
    return {k: verification[k] for k in ('input_bytes', 'output_bytes') if k in verification}


def current_plan_revisions(plan):
    if plan.capability == 'xlsx.inspect':
        from .skill_inspection import current_revisions
        return current_revisions()
    if plan.capability == 'json.normalize':
        from .json_contract import current_revisions
        return current_revisions()
    if isinstance(plan, PluginExecutionPlan):
        plugin = DEFAULT_REGISTRY.get(plan.capability)
        if plugin is None:
            raise ValueError(f'unregistered contract capability: {plan.capability}')
        return {'executor_revision': plugin.executor_revision, 'verifier_revision': plugin.verifier_revision}
    from .contracts import EXECUTOR_REVISION, VERIFIER_REVISION
    return {'executor_revision': EXECUTOR_REVISION, 'verifier_revision': VERIFIER_REVISION}


def contract_from_plan(plan):
    if plan.capability == 'xlsx.inspect':
        from .skill_inspection import contract_from_plan as inspection_contract
        return inspection_contract(plan)
    if plan.capability == 'json.normalize':
        execution = {'input_path': plan.input_path, 'output_path': plan.output_path, 'style': plan.style}
    elif isinstance(plan, PluginExecutionPlan):
        execution = {'input_path': plan.input_path, 'output_path': plan.output_path, **plan.options}
    else:
        execution = {'input_path': plan.input_path, 'output_path': plan.output_path,
            'key_columns': list(plan.key_columns), 'sheet': plan.sheet, 'keep': plan.keep,
            'encoding': plan.encoding, 'delimiter': plan.delimiter}
    return {'capability': plan.capability, 'contract_version': plan.contract_version,
            'constraints': list(plan.constraints), 'execution': execution}
