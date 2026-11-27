"""Human-reviewed prose proposals, separate from trusted execution authority.

The model may propose typed values and literal anchors. Anchors establish where
words occur, never that all natural-language meaning has been understood. Only a
trusted local application calls ``confirm`` after displaying the entire review.
This is an application boundary, not protection against arbitrary same-UID code.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata

from .file_lock import NOFOLLOW, NONBLOCK, lock_exclusive, unlock

from .capability_catalog import _canonical, _definitions, build_catalog
from .intent import (MAX_INTENT_BYTES, MinimalStateStore, _bounded, _identity,
                     _normal_execution, interpret_intent)
from .skill_catalog import _absolute
from .context import ContextStore, SourceRef
from .review_context import recover_context, validate_context
from .review_quotes import resolve_quote_ref, source_quote_anchors
from .tasks import contract_from_plan, plan_from_request

_SCHEMA = 'rpe.proposal/v1'
_CONFIRM_SCHEMA = 'rpe.confirmation/v1'
_DRAFT_FIELDS = {'capability', 'constraints', 'execution', 'requirements', 'unresolved'}
_MAX_DOCUMENT = 512 * 1024


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def terminal_text(value, *, multiline=False):
    """Keep text inspectable while making terminal controls and bidi inert."""
    return ''.join(character if (multiline and character == '\n') or
        unicodedata.category(character) not in {'Cc', 'Cf', 'Cs'} else
        ('\\u' + format(ord(character), '04x')) for character in str(value))


def _shown(value):
    return terminal_text(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _path_facts(request, workspace):
    """Bind reviewed directory objects, then let BoundExecution carry live FDs."""
    from .path_binding import _directory
    result = {}
    for name, path in [('workspace', workspace),
            ('input_parent', Path(request['execution']['input_path']).parent),
            ('output_parent', Path(request['execution']['output_path']).parent)]:
        if os.name == 'nt':
            target = Path(os.path.abspath(Path(path).expanduser()))
            cursor = Path(target.anchor)
            for part in target.parts[1:]:
                cursor /= part
                if cursor.is_symlink():
                    raise ValueError('binding requires paths without symlinks')
            current = os.lstat(target)
            if not stat.S_ISDIR(current.st_mode):
                raise ValueError('bound directory must be a directory')
            result[name] = [current.st_dev, current.st_ino]
            continue
        descriptor = _directory(path)
        try:
            current = os.fstat(descriptor)
            result[name] = [current.st_dev, current.st_ino]
        finally:
            os.close(descriptor)
    return result


def _rows(raw, draft, sources=None):
    errors, missing, rows = [], [], []
    if type(draft) is not dict or set(draft) != _DRAFT_FIELDS:
        return rows, ['draft_fields'], ['draft must contain exactly capability, constraints, execution, requirements, unresolved']
    execution = draft['execution']
    pointers = {'/capability', '/constraints'}
    if type(execution) is dict:
        pointers |= {'/execution/' + name for name in execution}
    else:
        missing.append('execution')
    supplied = draft['requirements']
    if type(supplied) is not list or not 1 <= len(supplied) <= 32:
        return rows, [*missing, 'requirements'], ['requirements must contain 1 to 32 rows']
    covered = set()
    source_anchors = [anchor for content in (sources or {}).values()
        for anchor in source_quote_anchors(
            {key: content[key] for key in ('source', 'event_id', 'sha256')},
            content['payload']['text'], content['facts']).values()]
    for index, row in enumerate(supplied):
        if (type(row) is not dict or not {'text', 'fields'} <= set(row) or
                set(row) - {'text', 'quote', 'quote_ref', 'fields', 'source_ref'} or
                not ({'quote', 'quote_ref'} & set(row)) or
                not isinstance(row.get('text'), str) or not row['text'].strip() or
                type(row.get('fields')) is not list or not row['fields'] or
                any(not isinstance(field, str) or field not in pointers for field in row['fields']) or
                len(set(row['fields'])) != len(row['fields'])):
            missing.append('requirements.' + str(index))
            errors.append('requirement text and mapped current typed fields are required')
            continue
        resolved, addressed_span = copy.deepcopy(row), None
        if 'quote_ref' in row:
            try:
                anchor = resolve_quote_ref(raw, row['quote_ref'], source_anchors)
                if ('quote' in row and row['quote'] != anchor['quote'] or
                        'source_ref' in row and row['source_ref'] != anchor.get('source_ref')):
                    raise ValueError('quote or source_ref differs from the explicitly selected quote_ref')
                resolved['quote'], addressed_span = anchor['quote'], anchor['span']
                if 'source_ref' in anchor:
                    resolved['source_ref'] = anchor['source_ref']
            except ValueError as error:
                missing.append('requirements.' + str(index) + '.quote_ref')
                errors.append(str(error))
                continue
        quote = resolved['quote']
        quoted_source, origin = raw, 'source_quote' if quote is not None else 'proposed_default'
        if 'source_ref' in resolved:
            try:
                ref = SourceRef.from_dict(resolved['source_ref']).to_dict()
                source = (sources or {})[_canonical(ref)]
                quoted_source, origin = source['payload']['text'], 'context_source'
                for field in row['fields']:
                    name = field.removeprefix('/execution/') if field.startswith('/execution/') else field[1:]
                    if field == '/constraints':
                        value = draft['constraints']
                    elif field.startswith('/execution/') and type(execution) is dict:
                        value = execution.get(name)
                    else:
                        raise ValueError('context facts cannot authorize a capability')
                    fact = source['facts'].get(name)
                    same = (set(value) == set(fact) if field == '/constraints'
                            and type(value) is list and type(fact) is list
                            and all(type(item) is str for item in value) else value == fact)
                    if name not in source['facts'] or not same:
                        raise ValueError('context citation field is missing or conflicts with its explicit fact')
                if quote is None:
                    raise ValueError('context citation requires a literal quote')
            except (ValueError, KeyError, TypeError) as error:
                missing.append('requirements.' + str(index) + '.source_ref')
                errors.append(str(error) if isinstance(error, ValueError) else 'context citation is not an authorized source')
                continue
        if quote is not None and (not isinstance(quote, str) or not quote or quote not in quoted_source):
            missing.append('requirements.' + str(index) + '.quote')
            errors.append('source quotes must be nonempty literal substrings')
            continue
        start = quoted_source.find(quote) if quote is not None else None
        rows.append({'requirement_id': 'r' + str(index + 1).zfill(4),
            **resolved, 'span': addressed_span if addressed_span is not None else
                [start, start + len(quote)] if quote is not None else None,
            'origin': origin})
        covered.update(row['fields'])
    missing += sorted(pointers - covered)
    unresolved = draft['unresolved']
    if (type(unresolved) is not list or any(not isinstance(value, str) or not value.strip()
                                          for value in unresolved)):
        missing.append('unresolved')
        errors.append('unresolved must be an explicit list of nonempty strings')
    elif unresolved:
        missing.append('unresolved')
        errors += ['unresolved clause: ' + value for value in unresolved]
    return rows, sorted(set(missing)), errors


def render_review(document):
    """Render the complete source, all mappings/defaults, and executable values."""
    lines = ['任务要求审阅', '状态：' + terminal_text(document['status']),
        '原始请求（完整保留；控制字符以转义形式显示）：']
    lines.extend('  | ' + line for line in terminal_text(document['source']['raw'], multiline=True).split('\n'))
    lines.append('逐项要求与来源：')
    for row in document['requirements']:
        lines.append(row['requirement_id'] + ' ' + terminal_text(row['text']) +
            ' | 字段：' + _shown(row['fields']) + ' | ' + row['origin'] +
            ' | 引用：' + _shown(row['quote']) + ' | 字符范围：' + _shown(row['span']) +
            (' | 引用地址：' + _shown(row['quote_ref']) if 'quote_ref' in row else '') +
            (' | 来源：' + _shown(row['source_ref']) if 'source_ref' in row else ''))
    if document.get('review_context') is not None:
        lines.append('显式附件（仅必要事实与短引用；来源本身不授予执行权限）：')
        lines.append(_shown(document['review_context']['model_sources']))
        lines.append('字段出处：' + _shown(document.get('field_provenance', {})))
    if document.get('skill_selection') is not None:
        lines.append('技能选择（确认前重新核验原始来源）：' + _shown(document['skill_selection']))
    lines.append('标准化执行合同（所有值均属于确认范围）：')
    lines.append(_shown(document['machine_contract']))
    defaults = {}
    draft = document['draft']
    if type(draft) is dict and type(draft.get('execution')) is dict:
        defaults = {key: value for key, value in document['machine_contract'].get('execution', {}).items()
                    if key not in draft['execution']}
    lines.append('执行默认值（同样需要确认）：' + _shown(defaults))
    if 'parameter_evidence' in document:
        lines.append('原文字面参数核验（不证明要求没有遗漏）：' + _shown(document['parameter_evidence']))
    lines.append('待澄清：' + _shown(draft.get('unresolved', []) if type(draft) is dict else []))
    lines.append('缺少字段：' + _shown(document['missing']))
    lines.append('问题：' + _shown(document['errors']))
    lines.extend(['来源引用只证明原文位置，不证明自然语言语义完整。请对照完整原文检查遗漏、误解和默认值。',
        '确认表示信任的审阅者接受以上完整执行范围；模型输出、引用覆盖和草稿状态本身均不授予执行权限。'])
    return '\n'.join(lines)


def create_proposal(raw, draft, *, db_path, workspace, session_id, source_ref,
                    catalog_root=None, skill_snapshot_id=None, review_context=None,
                    skill_selection=None, value_evidence=False):
    """Validate one model draft without granting confirmation or writing output."""
    try:
        _identity(session_id, 'session_id')
        _identity(source_ref, 'source_ref')
        if not isinstance(raw, str) or not raw.strip() or len(raw.encode('utf-8')) > MAX_INTENT_BYTES:
            raise ValueError('source must be nonempty UTF-8 text of at most 64 KiB')
        _bounded(draft)
        draft = copy.deepcopy(draft)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        return {'status': 'needs_context', 'missing': ['bounded_source_or_draft'], 'errors': [str(error)]}
    if type(value_evidence) is not bool:
        raise ValueError('value_evidence must be a boolean')
    root = _absolute(workspace)
    if not root.is_dir():
        raise ValueError('workspace must be an existing directory')
    source_contents, context_errors = {}, []
    bound_context, bound_selection, recovery, provenance = None, None, None, {}
    if review_context is not None:
        try:
            bound_context = validate_context(db_path, review_context, session_id)
            contexts = ContextStore(db_path)
            source_contents = {_canonical(ref): contexts.resolve(ref, session_id=session_id)
                               for ref in bound_context['source_refs']}
        except (ValueError, TypeError, KeyError, OSError) as error:
            context_errors.append('invalid review context: ' + str(error))
    if skill_selection is not None:
        try:
            from .integrations.review_skills import validate_selection
            bound_selection = validate_selection(db_path, catalog_root, skill_selection)
        except (ValueError, TypeError, KeyError, OSError) as error:
            context_errors.append('invalid skill selection: ' + str(error))
    rows, missing, errors = _rows(raw, draft, source_contents)
    parameter_evidence = None
    if value_evidence:
        from .review_values import validate_parameter_witnesses
        parameter_evidence = validate_parameter_witnesses(raw, draft, source_contents)
        missing += parameter_evidence['missing']
        errors += parameter_evidence['errors']
    if context_errors:
        missing.append('verified_review_context_or_skill')
        errors += context_errors
    catalog = build_catalog(db_path=db_path if catalog_root is not None else None,
        catalog_root=catalog_root, skill_snapshot_id=skill_snapshot_id)
    status, result, machine = 'needs_context', None, {}
    if type(draft) is dict and set(draft) == _DRAFT_FIELDS:
        definition = next((item for item in _definitions() if item['capability'] == draft['capability']), None)
        if definition is None:
            status = 'unsupported'
            errors.append('capability is not registered')
        elif type(draft['execution']) is dict:
            unknown = set(draft['execution']) - set(definition['execution_fields'])
            missing += ['execution.' + name for name in sorted(unknown)]
            missing += ['execution.' + name for name in definition['required_execution']
                        if name not in draft['execution']]
            execution = draft['execution']
            constraints = draft['constraints']
            selection_unsupported = False
            if bound_selection is not None:
                selection_unsupported = (
                    bound_selection['kind'] == 'selected' and (
                        bound_selection['executable'] is not True or
                        draft['capability'] not in bound_selection['capabilities']) or
                    bound_selection['kind'] == 'none' and draft['capability'] == 'xlsx.inspect')
            if bound_context is not None and type(constraints) is list and all(
                    type(value) is str for value in constraints):
                try:
                    recovery = recover_context(db_path, bound_context, session_id,
                                               draft['capability'], execution, constraints)
                    provenance = {'/execution/' + name: refs for name, refs in recovery['provenance'].items()}
                    for row in rows:
                        if row['origin'] == 'context_source':
                            for field in row['fields']:
                                refs = provenance.setdefault(field, [])
                                if row['source_ref'] not in refs:
                                    refs.append(row['source_ref'])
                    if recovery['status'] != 'ready':
                        missing += ['execution.' + name for name in recovery['missing']]
                        missing += ['context.' + name for name in recovery['conflicts']]
                        missing.append('verified_context_recovery')
                        errors.append('context recovery is ' + recovery['status'] + ': ' + _shown(recovery['issues']))
                    # An explicit source may reveal an omission, but it does not
                    # rewrite the model's draft or its requirement mappings.
                    missing += ['execution.' + name for name in recovery['facts'] if name not in execution]
                except (ValueError, TypeError, KeyError, OSError) as error:
                    missing.append('verified_context_recovery')
                    errors.append(str(error))
            unsupported = (type(constraints) is list and
                all(isinstance(value, str) for value in constraints) and
                bool(set(constraints) - set(definition['constraints'])))
            unsupported |= (execution.get('keep', 'first') != 'first' or
                'style' in execution and execution['style'] not in ('minify', 'pretty') or
                'mode' in execution and execution['mode'] not in ('sheets', 'rows', 'formulas'))
            if selection_unsupported:
                status = 'unsupported'
                errors.append('discovery_only or incompatible skill selection cannot authorize this capability')
            elif unsupported:
                status = 'unsupported'
                errors.append('constraint or option is not supported')
            elif not missing:
                extracted = {'goal': 'Execute ' + draft['capability'],
                    **{key: copy.deepcopy(draft[key]) for key in ('capability', 'constraints', 'execution')}}
                result = interpret_intent(extracted, catalog=catalog, workspace=root,
                    session_id=session_id, source_ref=source_ref)
                if result['status'] == 'ready':
                    status = 'awaiting_confirmation'
                    machine = contract_from_plan(plan_from_request(result['request']))
                else:
                    status = 'unsupported' if result['status'] == 'unsupported' else 'needs_context'
                    missing += result.get('missing', []) or ['valid_execution_contract']
                    errors += result.get('errors', [])
    minimal_id = None
    if status == 'awaiting_confirmation':
        minimal_id = MinimalStateStore(db_path).capture(result, session_id=session_id,
                                                       source_ref=source_ref)['state_id']
    document = {'schema_version': _SCHEMA, 'status': status,
        'source': {'raw': raw, 'source_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                   'session_id': session_id, 'source_ref': source_ref},
        'draft': draft, 'requirements': rows, 'workspace': str(root),
        'catalog_sha256': result['catalog_sha256'] if result and result['status'] == 'ready' else catalog.snapshot_id,
        'catalog_source': copy.deepcopy(catalog.source),
        'input_sha256': result['input_sha256'] if result and result['status'] == 'ready' else None,
        'path_facts': _path_facts(result['request'], root) if status == 'awaiting_confirmation' else None,
        'minimal_state_id': minimal_id, 'machine_contract': machine,
        'review_context': bound_context, 'context_recovery': recovery,
        'field_provenance': provenance, 'skill_selection': bound_selection,
        'missing': sorted(set(missing)), 'errors': errors}
    if parameter_evidence is not None:
        document['parameter_evidence'] = parameter_evidence
    document['summary'] = render_review(document)
    if len(_canonical(document)) > _MAX_DOCUMENT:
        return {'status': 'needs_context', 'missing': ['bounded_review'], 'errors': ['review exceeds 512 KiB']}
    return ProposalStore(db_path)._save(document)


class ProposalStore:
    """Immutable draft and approval records; a separate CAS consumes execution."""
    def __init__(self, db_path):
        self.db_path = _absolute(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS rpe_proposals (proposal_id TEXT PRIMARY KEY, document BLOB NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS rpe_confirmations (confirmation_id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL, document BLOB NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS rpe_proposal_executions (confirmation_id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, result BLOB)')

    def _connect(self):
        _absolute(self.db_path)
        return sqlite3.connect(self.db_path, timeout=10)

    def _save(self, document):
        raw = _canonical(document)
        proposal_id = hashlib.sha256(raw).hexdigest()
        with self._connect() as db:
            db.execute('INSERT OR IGNORE INTO rpe_proposals VALUES (?,?)', (proposal_id, raw))
            saved = bytes(db.execute('SELECT document FROM rpe_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()[0])
        if saved != raw:
            raise ValueError('proposal integrity collision')
        return {**copy.deepcopy(document), 'proposal_id': proposal_id, 'proposal_sha256': proposal_id}

    def _get(self, identity, *, confirmation=False):
        if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity):
            raise ValueError('proposal or confirmation identity must be SHA256')
        table, key = ('rpe_confirmations', 'confirmation_id') if confirmation else ('rpe_proposals', 'proposal_id')
        with self._connect() as db:
            row = db.execute('SELECT document FROM ' + table + ' WHERE ' + key + '=?', (identity,)).fetchone()
        if row is None:
            raise ValueError('confirmation not found' if confirmation else 'proposal not found')
        raw = row[0].encode() if isinstance(row[0], str) else bytes(row[0])
        if len(raw) > _MAX_DOCUMENT or hashlib.sha256(raw).hexdigest() != identity:
            raise ValueError('confirmation integrity failed' if confirmation else 'proposal integrity failed')
        document = json.loads(raw)
        if type(document) is not dict or document.get('schema_version') != (_CONFIRM_SCHEMA if confirmation else _SCHEMA):
            raise ValueError('proposal or confirmation integrity failed')
        return document

    def show(self, proposal_id, *, session_id):
        """Integrity-only inspection does not probe or execute business files."""
        document = self._get(proposal_id)
        if document['source']['session_id'] != session_id:
            raise ValueError('proposal belongs to another session')
        if (document['source']['source_sha256'] != hashlib.sha256(document['source']['raw'].encode()).hexdigest()
                or document['summary'] != render_review(document)):
            raise ValueError('proposal source or summary integrity failed')
        return {**document, 'proposal_id': proposal_id, 'proposal_sha256': proposal_id}

    def _minimal(self, proposal, *, session_id, catalog_sha256):
        if proposal['status'] != 'awaiting_confirmation' or not proposal['minimal_state_id']:
            raise ValueError('proposal has unresolved, unsupported or missing requirements')
        source_contents = {}
        if proposal.get('review_context') is not None:
            bundle = validate_context(self.db_path, proposal['review_context'], session_id)
            contexts = ContextStore(self.db_path)
            source_contents = {_canonical(ref): contexts.resolve(ref, session_id=session_id)
                               for ref in bundle['source_refs']}
            historical = proposal.get('context_recovery')
            if type(historical) is not dict or historical.get('status') != 'ready':
                raise ValueError('review context recovery is not ready')
            fresh = ContextStore(self.db_path).revalidate_recovery(historical['recovery_id'], session_id=session_id)
            if (fresh.get('status') != 'ready' or
                    {key: value for key, value in fresh.items() if key != 'recovery_id'} !=
                    {key: value for key, value in historical.items() if key != 'recovery_id'}):
                raise ValueError('review context source freshness or recovery changed')
        rows, missing, errors = _rows(proposal['source']['raw'], proposal['draft'], source_contents)
        if 'parameter_evidence' in proposal:
            from .review_values import validate_parameter_witnesses
            evidence = validate_parameter_witnesses(proposal['source']['raw'], proposal['draft'], source_contents)
            if evidence != proposal['parameter_evidence'] or evidence['missing'] or evidence['errors']:
                raise ValueError('proposal literal parameter evidence differs or is incomplete')
        if rows != proposal['requirements'] or missing or errors:
            raise ValueError('proposal requirement quote addresses or source mappings differ')
        if proposal.get('skill_selection') is not None:
            from .integrations.review_skills import validate_selection
            source = proposal['catalog_source']
            validate_selection(self.db_path, source['catalog_root'] if source else None,
                               proposal['skill_selection'])
        document = MinimalStateStore(self.db_path).resolve(proposal['minimal_state_id'],
            session_id=session_id, catalog_sha256=catalog_sha256)
        request = document['request']
        if _path_facts(request, Path(proposal['workspace'])) != proposal['path_facts']:
            raise ValueError('reviewed workspace or input/output directory path changed')
        expected, _ = _normal_execution(proposal['draft']['execution'], Path(proposal['workspace']))
        actual = copy.deepcopy(request['execution'])
        if request['capability'] == 'xlsx.inspect':
            actual.pop('skill_binding', None)
        if (expected != actual or request['capability'] != proposal['draft']['capability'] or
                request['constraints'] != proposal['draft']['constraints'] or
                request['goal'] != 'Execute ' + request['capability'] or
                request['source_ref'] != proposal['source']['source_ref'] or
                document['workspace'] != proposal['workspace'] or
                document['input_sha256'] != proposal['input_sha256'] or
                document['catalog_source'] != proposal['catalog_source'] or
                contract_from_plan(plan_from_request(request)) != proposal['machine_contract']):
            raise ValueError('proposal and minimal contract integrity differ')
        return document

    def _catalog(self, proposal):
        source = proposal['catalog_source']
        return build_catalog(db_path=source['db_path'] if source else None,
            catalog_root=source['catalog_root'] if source else None,
            skill_snapshot_id=source['skill_snapshot_id'] if source else None)

    def confirm(self, proposal_id, *, session_id, expected_proposal_sha256, actor):
        """Trusted application action; deliberately absent from model tool APIs.

        ``actor`` labels the real caller (for example ``human-cli:audrey`` or
        ``simulated_fixture_reviewer``); this label is evidence, not authentication.
        A trusted embedding must authenticate its own user before calling here.
        """
        _identity(actor, 'actor')
        proposal = self.show(proposal_id, session_id=session_id)
        if expected_proposal_sha256 != proposal_id:
            raise ValueError('expected proposal digest differs from the displayed review')
        self._minimal(proposal, session_id=session_id, catalog_sha256=self._catalog(proposal).snapshot_id)
        document = {'schema_version': _CONFIRM_SCHEMA, 'status': 'confirmed',
            'proposal_id': proposal_id, 'proposal_sha256': proposal_id,
            'session_id': session_id, 'actor': actor}
        raw, confirmation_id = _canonical(document), _sha(document)
        with self._connect() as db:
            db.execute('INSERT OR IGNORE INTO rpe_confirmations VALUES (?,?,?)', (confirmation_id, proposal_id, raw))
            saved = db.execute('SELECT confirmation_id,document FROM rpe_confirmations WHERE proposal_id=?', (proposal_id,)).fetchone()
        if saved[0] != confirmation_id or bytes(saved[1]) != raw:
            raise ValueError('proposal already confirmed by another actor or confirmation integrity failed')
        return {**document, 'confirmation_id': confirmation_id}

    def resolve(self, confirmation_id, *, session_id, catalog_sha256):
        confirmation = self._get(confirmation_id, confirmation=True)
        if confirmation['session_id'] != session_id:
            raise ValueError('confirmation belongs to another session')
        proposal = self.show(confirmation['proposal_id'], session_id=session_id)
        if confirmation['proposal_sha256'] != proposal['proposal_sha256']:
            raise ValueError('confirmation proposal integrity failed')
        document = self._minimal(proposal, session_id=session_id, catalog_sha256=catalog_sha256)
        return {**document, 'confirmation_id': confirmation_id, 'proposal_id': proposal['proposal_id'],
            'proposal_sha256': proposal['proposal_sha256'], 'source': copy.deepcopy(proposal['source']),
            'draft': copy.deepcopy(proposal['draft']), 'requirements': copy.deepcopy(proposal['requirements'])}

    def execute(self, confirmation_id, *, session_id, workspace, catalog_root=None,
                skill_snapshot_id=None, timeout=120, python=None):
        """Consume one immutable approval, then start one bounded fixed CLI worker."""
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError('execution timeout must be between 0 and 120 seconds')
        confirmation = self._get(confirmation_id, confirmation=True)
        proposal = self.show(confirmation['proposal_id'], session_id=session_id)
        if confirmation['session_id'] != session_id or _absolute(workspace) != Path(proposal['workspace']):
            raise ValueError('execution session or workspace differs from confirmation')
        source = proposal['catalog_source']
        if catalog_root is not None and (source is None or _absolute(catalog_root) != Path(source['catalog_root'])):
            raise ValueError('execution catalog differs from proposal')
        if skill_snapshot_id is not None and (source is None or skill_snapshot_id != source['skill_snapshot_id']):
            raise ValueError('execution skill snapshot differs from proposal')
        event_id = 'review-' + confirmation_id
        def prior_result(db):
            previous = db.execute('SELECT event_id,result FROM rpe_proposal_executions WHERE confirmation_id=?',
                                  (confirmation_id,)).fetchone()
            if previous is None:
                return None
            if previous[0] != event_id:
                raise ValueError('execution claim integrity failed')
            return json.loads(previous[1]) if previous[1] is not None else {
                'status': 'running', 'event_id': event_id, 'confirmation_id': confirmation_id,
                'proposal_id': proposal['proposal_id']}

        # Source revalidation persists fresh evidence through its own connection;
        # it must precede the write transaction that claims this execution.
        with self._connect() as db:
            previous = prior_result(db)
        if previous is not None:
            return previous
        try:
            document = self.resolve(confirmation_id, session_id=session_id,
                catalog_sha256=self._catalog(proposal).snapshot_id)
            payload = copy.deepcopy(document['request'])
            payload['event_id'] = event_id
            plan_from_request(payload)
        except (ValueError, OSError):
            # Another caller may have claimed/published while this caller was
            # validating. Its durable claim remains the authoritative result.
            with self._connect() as db:
                previous = prior_result(db)
            if previous is not None:
                return previous
            raise
        # Serialized admission precedes process creation. An interrupted claim
        # stays closed; it is never silently replayed into a second publication.
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = prior_result(db)
            if previous is not None:
                return previous
            db.execute('INSERT INTO rpe_proposal_executions VALUES (?,?,NULL)', (confirmation_id, event_id))
        try:
            result = _execute_worker(payload, self.db_path, Path(proposal['workspace']),
                proposal['input_sha256'], proposal['path_facts'], timeout=timeout, python=python,
                revalidate=lambda: self.resolve(confirmation_id, session_id=session_id,
                    catalog_sha256=self._catalog(proposal).snapshot_id))
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            # A caught parent failure has a durable terminal outcome. Only an
            # actual parent crash may leave the consumed claim without a result.
            result = {'status': 'failed', 'error': {'code': 'harness_parent_error', 'type': type(error).__name__},
                'worker_cleanup': {'cleanup_complete': False, 'remaining_children': [],
                                   'errors': [type(error).__name__]}}
        result.update(confirmation_id=confirmation_id, proposal_id=proposal['proposal_id'])
        with self._connect() as db:
            db.execute('UPDATE rpe_proposal_executions SET result=? WHERE confirmation_id=? AND result IS NULL',
                       (_canonical(result), confirmation_id))
        return result


def _worker_lock(database, deadline):
    """Bound per-database local worker resources through serialization."""
    descriptor = os.open(str(database) + '.review-worker.lock',
        os.O_RDWR | os.O_CREAT | NOFOLLOW | NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('worker lock must be a regular file')
        while True:
            try:
                lock_exclusive(descriptor, blocking=False)
                return descriptor
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('review worker lock timed out')
                time.sleep(min(0.01, remaining))
    except BaseException:
        os.close(descriptor)
        raise


def _execute_worker(payload, database, workspace, input_sha256, path_facts, *, timeout, python, revalidate):
    """Use the broker's fixed CLI protocol, descriptor binding and child cleanup."""
    from .integrations.hermes_session import (_process_table, _capture_descendants,
        _stop_descendants, _remaining_descendants)
    from .path_binding import BoundExecution
    # A normal virtualenv interpreter is itself a symlink. It is fixed by this
    # trusted caller, never selected by a model or the proposal contract.
    interpreter = Path(python or sys.executable)
    owned, process, identity, binding, lock_fd = {}, None, None, None, None
    deadline = time.monotonic() + timeout
    cleanup_errors, result = [], {'status': 'failed', 'error': {'code': 'harness_launch_error'}}
    with tempfile.TemporaryDirectory(prefix='rpe-review-', dir=database.parent) as temporary:
        try:
            lock_fd = _worker_lock(database, deadline)
            # A wait may outlive an input edit or catalog revocation. Approval
            # is revalidated after the wait, immediately before binding/launch.
            revalidate()
            if not interpreter.is_absolute() or '..' in interpreter.parts or not interpreter.is_file():
                raise ValueError('fixed worker interpreter must be an absolute Python executable')
            binding = BoundExecution.capture(payload, workspace, temporary, input_sha256)
            output_directory = os.fstat(binding.output_fd)
            if (_path_facts(payload, workspace) != path_facts or
                    [output_directory.st_dev, output_directory.st_ino] != path_facts['output_parent']):
                raise ValueError('reviewed directory path changed before descriptor binding')
            if time.monotonic() >= deadline:
                raise TimeoutError('review worker deadline elapsed before launch')
            environment = {key: value for key, value in os.environ.items()
                if key in {'HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE'}}
            environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                PYTHONDONTWRITEBYTECODE='1', TMPDIR=temporary, TMP=temporary, TEMP=temporary)
            environment.update(binding.child_environment())
            process = subprocess.Popen([str(interpreter), '-B', '-m', 'rpe_harness',
                'events', 'submit', '--event', '-', '--db', str(database)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=temporary, env=environment, pass_fds=binding.pass_fds,
                start_new_session=True)
            identity = _process_table().get(process.pid, (None, None))[1]
            incoming = json.dumps(payload, ensure_ascii=False)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result = {'status': 'failed', 'error': {'code': 'harness_timeout'}}
                    break
                if identity:
                    _capture_descendants(process.pid, identity, owned)
                try:
                    stdout, _ = process.communicate(input=incoming, timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    incoming = None
                    continue
                parsed = json.loads(stdout)
                if (type(parsed) is not dict or parsed.get('status') not in
                        {'succeeded', 'rejected', 'failed', 'interrupted', 'cancelled', 'received', 'running', 'error'} or
                        parsed['status'] == 'succeeded' and process.returncode != 0 or
                        parsed['status'] == 'cancelled' and process.returncode != 1):
                    raise ValueError('invalid fixed CLI worker response')
                result = parsed
                break
        except TimeoutError:
            result = {'status': 'failed', 'error': {'code': 'harness_timeout'}}
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            result = {'status': 'failed', 'error': {'code': 'harness_worker_error', 'type': type(error).__name__}}
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        os.kill(process.pid, signal.SIGSTOP)
                    except OSError:
                        pass
                try:
                    if identity:
                        _capture_descendants(process.pid, identity, owned)
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    cleanup_errors.append(type(error).__name__)
                try:
                    _stop_descendants(owned)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    cleanup_errors.append(type(error).__name__)
            if binding is not None:
                binding.close()
            if lock_fd is not None:
                unlock(lock_fd)
                os.close(lock_fd)
    remaining = _remaining_descendants(owned)
    result['worker_cleanup'] = {'remaining_children': remaining, 'errors': cleanup_errors,
        'cleanup_complete': not remaining and not cleanup_errors}
    return result
