"""Readable prose -> review -> explicit local confirmation -> bounded execution."""
from __future__ import annotations

import getpass
from pathlib import Path
import sys
import tempfile

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
    draft.add_argument('--max-turns', type=int, default=3)
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
        with tempfile.TemporaryDirectory(prefix='rpe-review-host-', dir=database.parent) as owned:
            result = run_hermes_review(raw, args.workspace, database, owner_root=Path(owned),
                timeout=args.timeout, hermes_root=args.hermes_root, catalog_root=args.catalog_root,
                skill_snapshot_id=args.skill_snapshot_id, max_turns=args.max_turns)
        proposal = result.get('proposal')
        verdict = result.get('proposal_verification', {})
        if type(proposal) is not dict or verdict.get('passed') is not True:
            return {'status': 'needs_context', 'missing': ['verified_model_proposal'],
                'errors': verdict.get('errors', ['Hermes did not return a verified proposal']),
                'proposal_verification': verdict,
                'summary': '尚未形成可核验的要求草稿；没有确认或执行业务任务。\n' + terminal_text(str(verdict))}
        return {**proposal, 'summary': _summary(proposal), 'proposal_verification': verdict}
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
