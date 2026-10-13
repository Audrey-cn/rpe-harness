"""Shared local execution runtime. External skill metadata never grants execution."""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from .bridge import EventToStateConverter
from .capability import CapabilityRouter, SkillEntry
from .causal_graph import CausalGraph
from .mcsi import CausalState, TaskType
from .storage import RunStore

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
        import json
        with self.lock:
            graph_path = self.data_dir / 'causal_graph.json' if self.data_dir else None
            graph = CausalGraph.load(graph_path) if graph_path and graph_path.exists() else CausalGraph()
            index_path = self.data_dir / 'skill_index.json' if self.data_dir else None
            router = CapabilityRouter(index_path, graph=graph)
            annotations = self.data_dir / 'skill_annotations.json' if self.data_dir else None
            if annotations and annotations.exists():
                for sid, ann in json.loads(annotations.read_text()).get('annotations', {}).items():
                    if sid not in router.skills:
                        continue
                    skill = router.skills[sid]
                    for name in ['input_formats', 'priority', 'tags', 'capabilities', 'preconditions']:
                        if name in ann:
                            setattr(skill, name, ann[name])
                    if 'task_types' in ann:
                        skill.task_types = [TaskType.parse(t) for t in ann['task_types']]
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
            self.router, self.graph = router, graph
            self.graph_issues = graph.validate_skill_ids(set(router.skills))
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
                'graph_issues': self.graph_issues, 'usage': self.store.usage()}

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

    def run(self, payload: dict, source: str = 'local') -> dict:
        from .execution import prepare_deduplication, publish_output, cleanup_staging
        from .verification import verify_deduplication
        if not isinstance(payload, dict):
            raise ValueError('request must be a JSON object')
        event_id = payload.get('event_id')
        if not isinstance(event_id, str) or not event_id.strip():
            event_id = 'invalid-' + uuid.uuid4().hex
        record, fresh = self.store.claim_event(source, event_id, payload)
        if not fresh:
            return record
        run_id, prepared = record['run_id'], None
        stage, verification = 'validate', None
        try:
            state, params = self._validate_request(payload)
            self.store.mark_running(run_id, state.to_dict())
            stage = 'route'
            with self.lock:
                result = self.router.route(state)
            if result.fallback_to_llm or not result.candidates:
                raise ValueError('route requires escalation: ' + result.reason)
            skill_id = result.candidates[0].skill_id
            if skill_id != BUILTIN_IDS[state.input_format]:
                raise ValueError('selected metadata skill has no registered executor')
            self.store.step(run_id, stage, {'reason': result.reason, 'confidence': result.confidence}, skill_id=skill_id)
            stage = 'execute'
            self.store.step(run_id, stage, attempted=True)
            prepared = prepare_deduplication(**params, constraints=state.constraints)
            stage = 'verify'
            self.store.step(run_id, stage)
            verification = verify_deduplication(prepared)
            if verification.get('passed') is not True:
                raise RuntimeError('independent artifact verification failed')
            stage = 'publish'
            self.store.step(run_id, stage, {'verification': verification,
                            'output_path': str(params['output_path']),
                            'publication_confirmed': False})
            publish_output(prepared)
            artifact = {'path': str(params['output_path']), 'sha256': verification['output_sha256'],
                        'format': state.input_format, 'published': True,
                        'input_rows': prepared['input_rows'], 'output_rows': prepared['output_rows'],
                        'removed_rows': prepared['removed_rows']}
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
