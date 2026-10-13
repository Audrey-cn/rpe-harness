"""Shared local execution runtime. External skill metadata never grants execution."""
from __future__ import annotations

import os
import json
import threading
import uuid
from pathlib import Path

from .bridge import EventToStateConverter
from .capability import CapabilityRouter, SkillEntry
from .causal_graph import CausalGraph
from .mcsi import CausalState, TaskType
from .storage import RunStore, canonical_json
from .contracts import ExecutionPlan

BUILTIN_IDS = {'csv': 'builtin/csv-deduplicate', 'xlsx': 'builtin/xlsx-deduplicate'}
SUPPORTED_CONSTRAINTS = {'preserve_original', 'preserve_order', 'preserve_formulas'}


def default_db_path() -> Path:
    return Path(os.environ.get('RPE_STATE_DIR', str(Path.home() / '.local/share/rpe-harness'))) / 'runs.sqlite'


class HarnessRuntime:
    def __init__(self, data_dir: Path | str | None = None, db_path: Path | str | None = None):
        self.data_dir = Path(data_dir).resolve() if data_dir else None
        self.store = RunStore(db_path or default_db_path())
        self.lock = threading.RLock()
        self.store.recover_interrupted()
        self.reload()

    def reload(self):
        with self.lock:
            metadata_issues = []
            graph_path = self.data_dir / 'causal_graph.json' if self.data_dir else None
            try:
                graph = CausalGraph.load(graph_path) if graph_path and graph_path.exists() else CausalGraph()
            except (ValueError, TypeError, AttributeError, OSError) as exc:
                graph = CausalGraph()
                metadata_issues.append({'component': 'causal_graph', 'error_type': type(exc).__name__})
            index_path = self.data_dir / 'skill_index.json' if self.data_dir else None
            annotations = self.data_dir / 'skill_annotations.json' if self.data_dir else None
            try:
                router = CapabilityRouter(index_path, graph=graph)
                if annotations and annotations.exists():
                    for sid, ann in json.loads(annotations.read_text()).get('annotations', {}).items():
                        if not isinstance(ann, dict):
                            raise ValueError('annotations must be objects')
                        if sid not in router.skills:
                            continue
                        skill = router.skills[sid]
                        for name in ['input_formats', 'tags', 'capabilities', 'preconditions']:
                            if name in ann:
                                if not isinstance(ann[name], list) or any(not isinstance(v, str) for v in ann[name]):
                                    raise ValueError('annotation lists must contain strings')
                                setattr(skill, name, ann[name])
                        if 'priority' in ann:
                            if type(ann['priority']) is not int or not 1 <= ann['priority'] <= 10:
                                raise ValueError('annotation priority must be 1 to 10')
                            skill.priority = ann['priority']
                        if 'task_types' in ann:
                            if not isinstance(ann['task_types'], list):
                                raise ValueError('annotation task_types must be a list')
                            skill.task_types = [TaskType.parse(t) for t in ann['task_types']]
                router.rebuild_rules()
            except (ValueError, TypeError, AttributeError, OSError) as exc:
                # Advisory corruption is visible and quarantined; it cannot
                # prevent loading trusted execution contracts.
                router = CapabilityRouter(graph=graph)
                metadata_issues.append({'component': 'skill_metadata', 'error_type': type(exc).__name__})
            for fmt, sid in BUILTIN_IDS.items():
                router.register_skill(SkillEntry(sid, f'{fmt.upper()} first-record deduplication',
                    '按明确关键列去重，保留首条、原输入和顺序',
                    task_types=[TaskType.DATA_PROCESSING, TaskType.FILE_OPERATION],
                    input_formats=[fmt], tags=['tabular.deduplicate'],
                    capabilities=['tabular.deduplicate'], priority=10,
                    preconditions=['file_exists', 'output_available']))
            for sid, item in self.store.skill_states().items():
                if sid in router.skills:
                    router.skills[sid].enabled = item['enabled']
                    router.skills[sid].is_dormant = item['is_dormant']
            try:
                graph_issues = graph.validate_skill_ids(set(router.skills))
            except (ValueError, TypeError, AttributeError) as exc:
                graph = CausalGraph()
                router.graph = graph
                graph_issues = []
                metadata_issues.append({'component': 'causal_graph', 'error_type': type(exc).__name__})
            self.router, self.graph = router, graph
            self.metadata_issues = metadata_issues
            self.graph_issues = graph_issues
            router.rebuild_rules()
            self._refresh_usage()

    def _refresh_usage(self):
        for sid, item in self.store.usage().items():
            if sid in self.router.skills:
                skill = self.router.skills[sid]
                skill.use_count = item['use_count']
                skill.last_used = item['last_used']
                if item['verified_success_rate'] is not None:
                    skill.success_rate = item['verified_success_rate']

    def route(self, body: dict):
        state = EventToStateConverter().convert(body)
        with self.lock:
            return state, self.router.route(state, top_k=body.get('top_k', 5))

    def record_usage(self, skill_id: str, success: bool):
        if skill_id not in self.router.skills or type(success) is not bool:
            raise ValueError('known skill_id and boolean success are required')
        with self.lock:
            self.store.report_usage(skill_id, success)
            self._refresh_usage()

    def set_skill_state(self, skill_id: str, *, enabled: bool | None = None,
                        dormant: bool | None = None):
        with self.lock:
            skill = self.router.skills.get(skill_id)
            if skill is None:
                raise ValueError('unknown skill_id')
            if any(v is not None and type(v) is not bool for v in [enabled, dormant]):
                raise ValueError('skill state must be boolean')
            self.store.set_skill_state(skill_id, skill.enabled if enabled is None else enabled,
                                       skill.is_dormant if dormant is None else dormant)
            self.reload()

    def stats(self):
        return {'router': self.router.get_stats(), 'graph': self.graph.get_stats(),
                'graph_issues': self.graph_issues, 'metadata_issues': self.metadata_issues,
                'usage': self.store.usage()}

    def _validate_request(self, payload: dict) -> tuple[CausalState, dict]:
        if type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
            raise ValueError('schema_version must be 1')
        if not isinstance(payload.get('event_id'), str) or not payload['event_id'].strip():
            raise ValueError('event_id is required')
        if not isinstance(payload.get('state'), dict) or not isinstance(payload.get('execution'), dict):
            raise ValueError('state and execution must be JSON objects')
        raw = dict(payload['state'])
        raw.setdefault('source_ref', payload['event_id'])
        raw.setdefault('session_id', payload.get('session_id'))
        state = CausalState.from_event(raw)
        state.validate()
        params = dict(payload['execution'])
        for name in ['input_path', 'output_path']:
            value = params.get(name)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError(f'{name} must be an absolute path')
            params[name] = Path(value)
        source, target = params['input_path'], params['output_path']
        if not source.is_file() or source.is_symlink():
            raise ValueError('input must be an existing regular file, not a symlink')
        fmt = source.suffix.lower().lstrip('.')
        if fmt not in BUILTIN_IDS or state.input_format != fmt or target.suffix.lower() != f'.{fmt}':
            raise ValueError('input/output extensions and state.input_format must match csv or xlsx')
        if source.resolve() == target.resolve() or os.path.lexists(target):
            raise ValueError('output must be a new path different from input')
        if not target.parent.is_dir():
            raise ValueError('output parent must exist')
        if state.task_type not in [TaskType.DATA_PROCESSING, TaskType.FILE_OPERATION]:
            raise ValueError('unsupported task type for deduplication')
        if state.required_capability != 'tabular.deduplicate':
            raise ValueError('required_capability must be tabular.deduplicate')
        if set(state.constraints) - SUPPORTED_CONSTRAINTS:
            raise ValueError('unsupported constraint')
        keys = params.get('key_columns')
        if not isinstance(keys, list) or not keys or any(not isinstance(k, str) or not k for k in keys) or len(set(keys)) != len(keys):
            raise ValueError('key_columns must be a nonempty list of unique column names')
        unknown = set(params) - {'input_path', 'output_path', 'key_columns', 'sheet', 'keep', 'encoding', 'delimiter'}
        if unknown:
            raise ValueError(f'unsupported execution options: {sorted(unknown)}')
        # File facts are probed here; user-supplied facts cannot bypass execution checks.
        state.facts = ['file_exists', 'output_available']
        return state, params

    def run(self, payload: dict, source: str = 'local', *,
            expected_plan: ExecutionPlan | None = None, retry_of: str | None = None) -> dict:
        from .execution import prepare_deduplication, publish_output, cleanup_staging
        from .verification import verify_deduplication
        from .evolution.candidates import prepare_candidate, quarantine_candidate
        if not isinstance(payload, dict):
            raise ValueError('request must be a JSON object')
        # Admission and execution consume one owned snapshot, even if a caller
        # changes its original nested dictionaries after the event is saved.
        payload = json.loads(canonical_json(payload))
        event_id = payload.get('event_id')
        if not isinstance(event_id, str) or not event_id.strip():
            event_id = 'invalid-' + uuid.uuid4().hex
        record, fresh = self.store.claim_event(source, event_id, payload)
        if not fresh:
            return record
        run_id, prepared = record['run_id'], None
        stage, verification = 'validate', None
        try:
            if payload.get('schema_version') == 1:
                state, _ = self._validate_request(payload)
                plan = ExecutionPlan.from_request(payload)
            else:
                plan = ExecutionPlan.from_request(payload)
                state = CausalState(task_type=TaskType.parse(payload.get('task_type')), goal=plan.goal,
                    required_capability=plan.capability, input_format=plan.input_format,
                    constraints=list(plan.constraints), facts=['file_exists', 'output_available'],
                    source_ref=plan.source_ref, session_id=plan.session_id)
            if expected_plan is not None and plan.sha256 != expected_plan.sha256:
                raise ValueError('request no longer matches the anchored original execution plan')
            self.store.mark_running(run_id, state.to_dict())
            evidence = {'plan': plan.to_dict(), 'plan_sha256': plan.sha256}
            if retry_of is not None:
                evidence['retry_of'] = retry_of
            self.store.step(run_id, 'plan', evidence)
            stage = 'route'
            skill_id = plan.executor_id
            availability = self.store.skill_states().get(skill_id)
            if availability and (not availability['enabled'] or availability['is_dormant']):
                raise ValueError('registered executor is disabled or dormant')
            self.store.step(run_id, stage, {'reason': 'registered_contract', 'plan_sha256': plan.sha256}, skill_id=skill_id)
            stage = 'execute'
            self.store.step(run_id, stage, {'plan_sha256': plan.sha256}, attempted=True)
            builtin = {'kind': 'builtin', 'executor_id': plan.executor_id,
                       'code_sha256': plan.executor_revision, 'plan_sha256': plan.sha256}
            actual = dict(builtin)
            prepared = prepare_candidate(plan, self.store.path)
            candidate_version = None
            if prepared is None:
                prepared = prepare_deduplication(**plan.execution_kwargs())
            elif prepared.get('candidate_version_id'):
                candidate_version = prepared['candidate_version_id']
                actual.update(kind='candidate', candidate_version_id=candidate_version,
                              code_sha256=prepared.get('candidate_code_sha256'))
            elif prepared.get('recipe_version_id'):
                candidate_version = prepared['recipe_version_id']
                actual.update(recipe_version_id=candidate_version,
                              recipe_content_sha256=prepared.get('recipe_content_sha256'))
            else:
                raise RuntimeError('candidate artifact has no registered version evidence')
            self.store.step(run_id, stage, {'actual_executor': actual, 'plan_sha256': plan.sha256})
            stage = 'verify'
            self.store.step(run_id, stage, {'plan_sha256': plan.sha256})
            try:
                verification = verify_deduplication(prepared, plan=plan)
            except (ValueError, TypeError, OSError, KeyError) as exc:
                if candidate_version is None:
                    raise
                verification = {'passed': False, 'plan_sha256': plan.sha256,
                                'checks': {'artifact_readable': False}, 'error': type(exc).__name__}
            if verification.get('passed') is not True and candidate_version is not None:
                # This branch precedes any publication intent. A candidate can
                # never weaken the oracle or change the original frozen plan.
                cleanup_staging(prepared)
                prepared = None
                quarantine = quarantine_candidate(candidate_version, self.store.path,
                                                   'independent production verification failed')
                self.store.step(run_id, 'candidate_fallback', {
                    'plan_sha256': plan.sha256, 'actual_executor': actual,
                    'verification': verification, 'quarantine': quarantine})
                actual = dict(builtin)
                stage = 'execute'
                self.store.step(run_id, stage, {'plan_sha256': plan.sha256,
                                              'actual_executor': actual, 'fallback': True})
                prepared = prepare_deduplication(**plan.execution_kwargs())
                stage = 'verify'
                self.store.step(run_id, stage, {'plan_sha256': plan.sha256, 'fallback': True})
                verification = verify_deduplication(prepared, plan=plan)
            if verification.get('passed') is not True:
                raise RuntimeError('independent artifact verification failed')
            stage = 'publish'
            self.store.step(run_id, stage, {'verification': verification,
                            'output_path': plan.output_path, 'plan_sha256': plan.sha256,
                            'publication_confirmed': False})
            publish_output(prepared, plan=plan)
            artifact = {'path': plan.output_path, 'sha256': verification['output_sha256'],
                        'format': plan.input_format, 'published': True, 'plan_sha256': plan.sha256,
                        'input_sha256': plan.input_sha256,
                        'actual_executor': actual,
                        'input_rows': verification['input_rows'], 'output_rows': verification['output_rows'],
                        'removed_rows': verification['removed_rows']}
            self.store.step(run_id, 'complete', artifact)
            answer = self.store.finish(run_id, 'succeeded', verification=verification, artifact=artifact)
        except Exception as exc:
            invalid = (isinstance(exc, ValueError) and stage in ['validate', 'route', 'execute']) or (isinstance(exc, TypeError) and stage in ['validate', 'route'])
            status = 'rejected' if invalid else 'failed'
            self.store.step(run_id, stage)
            answer = self.store.finish(run_id, status, verification=verification,
                error={'code': 'invalid_request' if status == 'rejected' else 'execution_failed',
                       'type': type(exc).__name__, 'message': str(exc)})
        finally:
            if prepared is not None:
                cleanup_staging(prepared)
        with self.lock:
            self._refresh_usage()
        return answer
