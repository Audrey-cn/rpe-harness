"""Readable prose -> review -> explicit local confirmation -> bounded execution."""
from __future__ import annotations

import getpass
from pathlib import Path
import sys
import tempfile
from contextlib import nullcontext

from .proposal import ProposalStore, terminal_text
from .runtime import default_db_path
from .source_cli import _read_source


def register_review(commands):
    review = commands.add_parser('review', help='自然语言任务审阅、明确确认与执行')
    operations = review.add_subparsers(dest='review_command', required=True)
    draft = operations.add_parser('draft', help='用真实 Hermes 会话把原文整理为待审阅草稿')
    source = draft.add_mutually_exclusive_group(required=True)
    source.add_argument('--text', help='当前完整自然语言请求')
    source.add_argument('--source', help='UTF-8 原文文件，或 - 读取标准输入')
    draft.add_argument('--workspace', type=Path, default=Path.cwd())
    draft.add_argument('--hermes-root', type=Path)
    draft.add_argument('--timeout', type=float, default=180)
    draft.add_argument('--max-turns', type=int, default=8)
    draft.add_argument('--context-sources', type=Path, help='明确授权导入的来源附件文件；沿用宿主来源格式')
    draft.add_argument('--owner-root', type=Path, help='独立宿主状态目录；指定后由调用方负责清理')
    draft.add_argument('--read-isolation-root', type=Path, help='限制宿主读取本轮独立目录内其他会话')
    draft.add_argument('--deny-read-root', type=Path, action='append', default=[], help='额外禁止宿主读取的目录')
    show = operations.add_parser('show', help='显示完整来源、逐项要求、默认值和执行范围')
    show.add_argument('proposal_id')
    confirm = operations.add_parser('confirm', help='完整审阅后输入 确认；此动作不执行任务')
    confirm.add_argument('proposal_id')
    confirm.add_argument('--accept-digest', help='信任的外部自动化明确接受已审阅的完整 SHA256')
    confirm.add_argument('--actor', help='真实审阅者或信任的应用身份标签')
    run = operations.add_parser('run', help='执行已确认的不可变合同')
    run.add_argument('confirmation_id')
    run.add_argument('--workspace', type=Path)
    run.add_argument('--timeout', type=float, default=120)
    for operation in (draft, show, confirm, run):
        operation.add_argument('--db', type=Path, default=default_db_path())
        operation.add_argument('--json', action='store_true', help='输出机器可读的完整结果')
    for operation in (show, confirm, run):
        operation.add_argument('--session-id', help='可选；默认从不可变记录读取会话')
    for operation in (draft, run):
        operation.add_argument('--catalog-root', type=Path)
        operation.add_argument('--skill-snapshot-id')


def _summary(proposal):
    return (proposal['summary'] + '\n草稿 ID：' + proposal['proposal_id'] +
        '\n完整审阅 SHA256：' + proposal['proposal_sha256'])


def dispatch_review(args):
    if args.review_command == 'draft':
        # The import and host process are deferred until an actual draft request.
        from .integrations.hermes_review import run_hermes_review
        raw = args.text if args.text is not None else _read_source(args.source)
        database = Path(args.db).absolute()
        database.parent.mkdir(parents=True, exist_ok=True)
        host_scope = (nullcontext(str(args.owner_root.absolute())) if args.owner_root is not None else
                      tempfile.TemporaryDirectory(prefix='rpe-review-host-', dir=database.parent))
        with host_scope as owned:
            result = run_hermes_review(raw, args.workspace, database, owner_root=Path(owned),
                timeout=args.timeout, hermes_root=args.hermes_root, catalog_root=args.catalog_root,
                skill_snapshot_id=args.skill_snapshot_id, max_turns=args.max_turns,
                context_sources=_context_sources(args.context_sources),
                read_isolation_root=args.read_isolation_root, deny_read_roots=args.deny_read_root)
        proposal = result.get('proposal')
        verdict = result.get('proposal_verification', {})
        observation = {key: result[key] for key in ('session_id', 'usage', 'elapsed_seconds',
            'model_identity', 'process_cleanup', 'events', 'completed', 'timed_out',
            'exit_code', 'observer_errors', 'original_prompt_sha256', 'prompt_sha256',
            'catalog_snapshot_id', 'public_contract_sha256') if key in result}
        if (type(proposal) is not dict or verdict.get('verified') is not True
                or verdict.get('proposal_verified') is not True):
            return {'status': 'needs_context', 'missing': ['verified_model_proposal'],
                'errors': verdict.get('errors', ['Hermes did not return a verified proposal']),
                'proposal_verification': verdict,
                'host_observation': observation,
                'summary': '尚未形成可核验的要求草稿；没有确认或执行业务任务。\n' + terminal_text(str(verdict))}
        return {**proposal, 'summary': _summary(proposal), 'proposal_verification': verdict,
                'host_observation': observation}
    store = ProposalStore(args.db)
    if args.review_command == 'run':
        confirmation = store._get(args.confirmation_id, confirmation=True)
        session = args.session_id or confirmation['session_id']
        proposal = store.show(confirmation['proposal_id'], session_id=session)
        return store.execute(args.confirmation_id, session_id=session,
            workspace=args.workspace or Path(proposal['workspace']), catalog_root=args.catalog_root,
            skill_snapshot_id=args.skill_snapshot_id, timeout=args.timeout)
    document = store._get(args.proposal_id)
    session = args.session_id or document['source']['session_id']
    proposal = store.show(args.proposal_id, session_id=session)
    if args.review_command == 'show':
        return {**proposal, 'summary': _summary(proposal)}
    if args.accept_digest is None:
        if not sys.stdin.isatty():
            raise ValueError('confirmation requires an interactive terminal; trusted automation must use --accept-digest')
        print(_summary(proposal), flush=True)
        if input('核对完整原文与执行范围后，请输入 确认：').strip() != '确认':
            raise ValueError('未输入 确认；没有保存确认授权')
        accepted = proposal['proposal_sha256']
        actor = args.actor or 'human-cli:' + getpass.getuser()
        summary = ''
    else:
        accepted = args.accept_digest
        actor = args.actor or 'trusted-application:' + getpass.getuser()
        summary = _summary(proposal) + '\n'
    confirmation = store.confirm(args.proposal_id, session_id=session,
        expected_proposal_sha256=accepted, actor=actor)
    summary += ('已确认完整执行范围；任务尚未执行。\n确认 ID：' + confirmation['confirmation_id'] +
        '\n审阅者：' + terminal_text(actor))
    return {**confirmation, 'summary': summary}


def _context_sources(path):
    if path is None:
        return None
    import json
    value = json.loads(_read_source(str(path)))
    from .review_context import validate_sources
    return validate_sources(value)
