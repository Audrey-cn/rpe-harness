"""Zero-dependency local approval inbox (CLI + loopback Web UI) for RPE-Harness.

Closes the MCP human-in-the-loop last mile: models draft proposals via MCP
(`rpe_start_task`), and the human reviews and approves/cancels them out-of-band
from this trusted host process.
"""
from __future__ import annotations

import html
import json
import secrets
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .host_api import HostAPI, TrustedHostAction
from .runtime import default_db_path

_LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


def list_pending(api: HostAPI, *, owner: str | None = None) -> list[dict[str, Any]]:
    """Return all operations currently awaiting human confirmation."""
    with api._connect() as db:
        if owner is not None:
            rows = db.execute(
                'SELECT * FROM rpe_operations WHERE owner=? AND cancelled=0 AND confirmation_id IS NULL '
                'ORDER BY rowid DESC',
                (owner,),
            ).fetchall()
        else:
            rows = db.execute(
                'SELECT * FROM rpe_operations WHERE cancelled=0 AND confirmation_id IS NULL '
                'ORDER BY rowid DESC'
            ).fetchall()
    pending: list[dict[str, Any]] = []
    for row in rows:
        view = api._view(dict(row))
        if view.get('status') != 'awaiting_confirmation':
            continue
        proposal = view.get('proposal') or {}
        draft = view.get('draft') or {}
        execution = draft.get('execution') if isinstance(draft, dict) else {}
        pending.append({
            'operation_id': view['operation_id'],
            'owner': view['owner'],
            'generation': view['generation'],
            'digest': view['digest'],
            'status': view['status'],
            'capability': draft.get('capability') if isinstance(draft, dict) else None,
            'input_path': execution.get('input_path') if isinstance(execution, dict) else None,
            'output_path': execution.get('output_path') if isinstance(execution, dict) else None,
            'summary': proposal.get('summary') or (view.get('source') or {}).get('raw') or '',
            'host_context': view.get('host_context') or {},
        })
    return pending


def list_recent(api: HostAPI, *, owner: str | None = None) -> list[dict[str, Any]]:
    """Keep approved and terminal operations visible after they leave the pending list."""
    with api._connect() as db:
        # Filter pending work before the bounded history window. Confirmation
        # receipts also cover a crash before the operation linkage was repaired.
        query = ("SELECT o.* FROM rpe_operations o JOIN rpe_proposals p "
                 "ON p.proposal_id=o.proposal_id WHERE (o.cancelled=1 "
                 "OR o.confirmation_id IS NOT NULL OR EXISTS "
                 "(SELECT 1 FROM rpe_confirmations c WHERE c.proposal_id=o.proposal_id) "
                 "OR json_extract(CAST(p.document AS TEXT), '$.status') "
                 "<> 'awaiting_confirmation')")
        parameters = ()
        if owner is not None:
            query += ' AND o.owner=?'
            parameters = (owner,)
        rows = db.execute(query + ' ORDER BY o.rowid DESC LIMIT 50', parameters).fetchall()
    recent = []
    for row in rows:
        view = api._view(dict(row))
        if view['status'] == 'awaiting_confirmation':
            continue
        draft = view.get('draft') or {}
        result = view.get('run') or view.get('execution_result') or {}
        recent.append({
            'operation_id': view['operation_id'], 'owner': view['owner'],
            'generation': view['generation'], 'digest': view['digest'], 'status': view['status'],
            'capability': draft.get('capability'),
            'summary': (view.get('proposal') or {}).get('summary') or (view.get('source') or {}).get('raw') or '',
            'artifact': view.get('artifact'), 'error': result.get('error'),
        })
    return recent


def approve_and_execute(
    api: HostAPI,
    *,
    operation_id: str,
    owner: str,
    digest: str,
    generation: int,
    actor: str = 'local-inbox',
    timeout: int = 60,
) -> dict[str, Any]:
    """Host-side trusted confirmation followed by deterministic verified execution."""
    action = TrustedHostAction(
        owner=owner,
        operation_id=operation_id,
        generation=generation,
        digest=digest,
        actor=actor,
        action='confirm',
    )
    confirmed = api.confirm_task(operation_id, digest, action)
    if confirmed.get('status') != 'confirmed':
        return confirmed
    return api.execute_task(
        operation_id,
        owner,
        confirmation_id=confirmed['confirmation_id'],
        timeout=timeout,
    )


def reject_operation(
    api: HostAPI,
    *,
    operation_id: str,
    owner: str,
    generation: int,
    reason: str = 'rejected-in-inbox',
) -> dict[str, Any]:
    return api.cancel_task(operation_id, generation, owner=owner, reason=reason)


def _render_html(csrf_token: str, db_path: Path) -> str:
    escaped_db = html.escape(str(db_path))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>RPE-Harness Local Approval Inbox</title>
<style>
  :root {{ color-scheme: dark; --bg:#090d16; --card:#0f172a; --line:#1e293b; --fg:#f8fafc; --muted:#94a3b8; --cyan:#22d3ee; --green:#10b981; --red:#f43f5e; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:var(--bg); color:var(--fg); padding:24px; }}
  .wrap {{ max-width:920px; margin:0 auto; }}
  header {{ display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--line); padding-bottom:16px; margin-bottom:20px; }}
  h1 {{ margin:0; font-size:20px; color:var(--cyan); }}
  .meta {{ font-family:ui-monospace,Menlo,monospace; font-size:12px; color:var(--muted); }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:14px; }}
  .row {{ display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom:8px; }}
  .badge {{ font-family:ui-monospace,Menlo,monospace; font-size:12px; padding:2px 8px; border-radius:999px; background:#111c33; border:1px solid #334155; color:var(--cyan); }}
  pre {{ background:#060a12; border:1px solid var(--line); padding:10px; border-radius:8px; overflow:auto; font-size:12px; color:#cbd5e1; white-space:pre-wrap; }}
  .actions {{ display:flex; gap:10px; margin-top:12px; }}
  button {{ border:none; border-radius:8px; padding:8px 14px; font-weight:600; cursor:pointer; font-size:13px; }}
  .btn-ok {{ background:var(--green); color:#042f2e; }}
  .btn-no {{ background:var(--red); color:#fff1f2; }}
  .empty {{ text-align:center; padding:48px; color:var(--muted); border:1px dashed var(--line); border-radius:12px; }}
  #toast {{ position:fixed; bottom:20px; right:20px; background:#111827; border:1px solid var(--cyan); padding:10px 14px; border-radius:8px; display:none; font-size:13px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>⚡ RPE-Harness · Local Approval Inbox</h1>
      <div class="meta">DB: {escaped_db} · Loopback-only · Trusted Host Authority</div>
    </div>
    <button onclick="refresh()" style="background:#1e293b;color:#e2e8f0">Refresh</button>
  </header>
  <div id="list"></div>
  <h2 style="font-size:16px">Recent operations</h2>
  <div id="recent"></div>
</div>
<div id="toast"></div>
<script>
const CSRF = {json.dumps(csrf_token)};
function toast(msg) {{
  const el = document.getElementById('toast');
  el.textContent = msg; el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3500);
}}
async function act(endpoint, payload) {{
  try {{
    const res = await fetch(endpoint, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'X-RPE-CSRF': CSRF }},
      body: JSON.stringify(payload),
    }});
    const data = await res.json();
    const error = typeof data.error === 'string' ? data.error : data.error && (data.error.message || JSON.stringify(data.error));
    if (!res.ok) throw new Error(error || `HTTP ${{res.status}}`);
    toast((data.status || 'done') + (error ? ': ' + error : '') + (data.artifact && data.artifact.path ? ' → ' + data.artifact.path : ''));
    await refresh();
  }} catch (error) {{
    toast('Error: ' + error.message);
  }}
}}
function renderItems(id, items, pending) {{
  const root = document.getElementById(id);
  root.innerHTML = '';
  if (!items || !items.length) {{
    root.innerHTML = '<div class="empty">' + (pending ? 'No pending tasks awaiting confirmation.' : 'No recent operations.') + '</div>';
    return;
  }}
  for (const item of items) {{
    const card = document.createElement('div'); card.className = 'card';
    card.innerHTML = `
      <div class="row">
        <strong></strong>
        <span class="badge"></span>
      </div>
      <div class="meta"></div>
      <pre></pre>
      ${{pending ? `<div class="actions">
        <button class="btn-ok">Approve &amp; Execute</button>
        <button class="btn-no">Cancel</button>
      </div>` : ''}}`;
    card.querySelector('strong').textContent = item.capability || 'task';
    card.querySelector('.badge').textContent = `owner: ${{item.owner}} · gen ${{item.generation}} · ${{item.status}}`;
    card.querySelector('.meta').textContent = `op: ${{item.operation_id}} · digest: ${{String(item.digest || '').slice(0,16)}}…`;
    const detail = [item.summary || '(no summary)'];
    if (item.error) detail.push(typeof item.error === 'string' ? item.error : JSON.stringify(item.error));
    if (item.artifact && item.artifact.path) detail.push('Artifact: ' + item.artifact.path);
    card.querySelector('pre').textContent = detail.join('\\n');
    if (pending) {{
      card.querySelector('.btn-ok').onclick = () => act('/api/approve', {{
        operation_id: item.operation_id, owner: item.owner, digest: item.digest, generation: item.generation
      }});
      card.querySelector('.btn-no').onclick = () => act('/api/cancel', {{
        operation_id: item.operation_id, owner: item.owner, generation: item.generation
      }});
    }}
    root.appendChild(card);
  }}
}}
async function refresh() {{
  try {{
    const res = await fetch('/api/pending');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${{res.status}}`);
    renderItems('list', data.pending, true);
    renderItems('recent', data.recent, false);
  }} catch (error) {{
    toast('Error: ' + error.message);
  }}
}}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


class InboxHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], api: HostAPI, *, owner: str | None = None) -> None:
        if address[0] not in _LOOPBACK_HOSTS:
            raise ValueError('local approval inbox must bind to loopback only (127.0.0.1 / localhost / ::1)')
        self.api = api
        self.owner_filter = owner
        self.csrf_token = secrets.token_urlsafe(24)
        # Keep the requested name: binding localhost may resolve to 127.0.0.1.
        self.bind_host = address[0]
        self.address_family = socket.AF_INET6 if address[0] == '::1' else socket.AF_INET
        super().__init__(address, InboxHandler)


class InboxHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def end_headers(self) -> None:
        # Applies to JSON, HTML, and BaseHTTPRequestHandler's own error responses.
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def _trusted_request(self, *, post: bool = False) -> bool:
        hosts = self.headers.get_all('Host', [])
        try:
            if len(hosts) != 1 or any(char.isspace() for char in hosts[0]):
                raise ValueError('invalid Host')
            host = urlsplit('//' + hosts[0])
            bound_host = getattr(self.server, 'bind_host', self.server.server_address[0])
            if (host.hostname not in _LOOPBACK_HOSTS or host.hostname != bound_host.lower()
                    or host.username is not None
                    or host.password is not None or host.path or host.query or host.fragment
                    or host.netloc != hosts[0]
                    or (host.port if host.port is not None else 80) != self.server.server_port):
                raise ValueError('invalid Host')
            if post:
                fetch_sites = self.headers.get_all('Sec-Fetch-Site', [])
                if (len(fetch_sites) > 1 or any(value.strip().lower() not in
                        {'same-origin', 'same-site', 'none'} for value in fetch_sites)):
                    raise ValueError('cross-site or invalid fetch metadata')
                origins = self.headers.get_all('Origin', [])
                if origins:
                    if len(origins) != 1 or any(char.isspace() for char in origins[0]):
                        raise ValueError('invalid Origin')
                    origin = urlsplit(origins[0])
                    if (origin.scheme != 'http' or origin.hostname != host.hostname
                            or origin.username is not None or origin.password is not None
                            or origin.path or origin.query or origin.fragment
                            or (origin.port if origin.port is not None else 80) != self.server.server_port):
                        raise ValueError('cross-origin request')
        except ValueError:
            self._send_json(403, {'error': 'untrusted Host or request origin'})
            return False
        return True

    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get('Content-Length') or '0')
        if length <= 0 or length > 65536:
            raise ValueError('invalid request body size')
        data = json.loads(self.rfile.read(length).decode('utf-8'))
        if not isinstance(data, dict):
            raise ValueError('request body must be a JSON object')
        return data

    def do_GET(self) -> None:
        if not self._trusted_request():
            return
        if self.path == '/':
            page = _render_html(self.server.csrf_token, self.server.api.db_path).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if self.path == '/api/pending':
            self._send_json(200, {
                'pending': list_pending(self.server.api, owner=self.server.owner_filter),
                'recent': list_recent(self.server.api, owner=self.server.owner_filter),
            })
            return
        self._send_json(404, {'error': 'not found'})

    def do_POST(self) -> None:
        if not self._trusted_request(post=True):
            return
        if self.headers.get('X-RPE-CSRF') != self.server.csrf_token:
            self._send_json(403, {'error': 'missing or invalid CSRF token'})
            return
        try:
            body = self._read_json()
            if self.path == '/api/approve':
                res = approve_and_execute(
                    self.server.api,
                    operation_id=body['operation_id'],
                    owner=body['owner'],
                    digest=body['digest'],
                    generation=int(body['generation']),
                    actor='local-inbox-web',
                )
                self._send_json(200, res)
                return
            if self.path == '/api/cancel':
                res = reject_operation(
                    self.server.api,
                    operation_id=body['operation_id'],
                    owner=body['owner'],
                    generation=int(body['generation']),
                    reason=body.get('reason') or 'rejected-in-web-inbox',
                )
                self._send_json(200, res)
                return
            self._send_json(404, {'error': 'not found'})
        except (KeyError, ValueError, TypeError) as exc:
            self._send_json(400, {'error': str(exc)})


def register_inbox(subparsers) -> None:
    inbox = subparsers.add_parser(
        'inbox',
        help='review and approve/cancel pending MCP/HostAPI tasks from the trusted host',
    )
    inbox.add_argument('--db', help='SQLite path; default ~/.local/share/rpe-harness/runs.sqlite')
    inbox.add_argument('--owner', help='optional filter by owner')
    inbox.add_argument('--json', action='store_true', help='list pending operations as JSON and exit')
    inbox.add_argument('--web', action='store_true', help='launch zero-dep loopback Web Approval UI')
    inbox.add_argument('--host', default='127.0.0.1', help='loopback bind host (default 127.0.0.1)')
    inbox.add_argument('--port', type=int, default=18401, help='loopback bind port (default 18401)')


def dispatch_inbox(args) -> dict[str, Any] | int:
    db_path = Path(args.db) if args.db else default_db_path()
    api = HostAPI(db_path)
    if args.web:
        server = InboxHTTPServer((args.host, args.port), api, owner=args.owner)
        url_host = f'[{args.host}]' if ':' in args.host else args.host
        print(f'RPE-Harness Local Approval Inbox listening on http://{url_host}:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    pending = list_pending(api, owner=args.owner)
    if args.json or not sys.stdin.isatty():
        return {'status': 'ok', 'pending': pending}
    if not pending:
        print('No pending operations awaiting confirmation.')
        return {'status': 'ok', 'pending': []}
    outcomes = []
    for item in pending:
        print(f"\n[{item['capability'] or 'task'}] op={item['operation_id']} owner={item['owner']} gen={item['generation']}")
        print(f"digest: {item['digest']}")
        print(item['summary'])
        choice = input('Action [a=approve&execute / c=cancel / s=skip / q=quit]: ').strip().lower()
        if choice == 'q':
            break
        if choice == 'a':
            res = approve_and_execute(
                api,
                operation_id=item['operation_id'],
                owner=item['owner'],
                digest=item['digest'],
                generation=item['generation'],
                actor='local-inbox-cli',
            )
            print(f"-> {res.get('status')}")
            outcomes.append(res)
        elif choice == 'c':
            res = reject_operation(
                api,
                operation_id=item['operation_id'],
                owner=item['owner'],
                generation=item['generation'],
            )
            print(f"-> {res.get('status')}")
            outcomes.append(res)
    return {'status': 'ok', 'processed': len(outcomes), 'outcomes': outcomes}
