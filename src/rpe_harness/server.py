"""Bounded concurrent loopback HTTP service sharing the durable runtime."""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .mcsi import CausalState
from . import __version__
from .runtime import HarnessRuntime, BUILTIN_IDS
from .storage import EventConflict

MAX_BODY = 1_048_576


class APIError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class HarnessHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, runtime, request_timeout, catalog_root=None, skill_snapshot_id=None):
        self.runtime = runtime
        self.request_timeout = request_timeout
        self.catalog_root = catalog_root
        self.skill_snapshot_id = skill_snapshot_id
        self._unified_catalog = None
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, HarnessHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class HarnessHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        # Include browser isolation headers on handler and parser error responses.
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        super().end_headers()

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.request_timeout)
        self.request_expired = False
        self.deadline = threading.Timer(self.server.request_timeout, self._expire_request)
        self.deadline.daemon = True
        self.deadline.start()

    def _expire_request(self):
        self.request_expired = True
        try:
            self.connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def finish(self):
        self.deadline.cancel()
        super().finish()

    @property
    def runtime(self):
        return self.server.runtime

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def _dispatch(self, post):
        try:
            self._validate_request_origin(post=post)
            if not post:
                self.deadline.cancel()
            body = self._read_body() if post else None
            code, result = self._handle(body)
        except APIError as exc:
            code, result = exc.status, {'error': str(exc)}
        except EventConflict as exc:
            code, result = 409, {'error': str(exc), 'error_code': 'event_conflict'}
        except (ValueError, TypeError, KeyError) as exc:
            code, result = 400, {'error': str(exc)}
        except (socket.timeout, TimeoutError):
            code, result = 408, {'error': 'request body timed out'}
        except Exception:
            code, result = 500, {'error': 'internal error'}
        self._json(code, result)

    def _validate_request_origin(self, *, post):
        hosts = self.headers.get_all('Host', [])
        try:
            loopbacks = {'localhost', '127.0.0.1', '[::1]'}
            authorities = {f'{name}:{self.server.server_port}' for name in loopbacks}
            if self.server.server_port == 80:
                authorities.update(loopbacks)
            # Exact authorities also reject malformed bracketed IPv6 literals,
            # userinfo, duplicate/comma-joined hosts, and ambiguous URL syntax.
            if len(hosts) != 1 or hosts[0].lower() not in authorities:
                raise ValueError('invalid Host')
            host = urlsplit('//' + hosts[0])
            if post:
                fetch_sites = self.headers.get_all('Sec-Fetch-Site', [])
                if (len(fetch_sites) > 1 or any(value.strip().lower() not in
                        {'same-origin', 'none'} for value in fetch_sites)):
                    raise ValueError('cross-site request')
                origins = self.headers.get_all('Origin', [])
                if origins:
                    if len(origins) != 1 or any(char.isspace() for char in origins[0]):
                        raise ValueError('invalid Origin')
                    origin = urlsplit(origins[0])
                    if (origin.scheme != 'http' or origin.hostname != host.hostname
                            or origin.username is not None or origin.password is not None
                            or origin.path or origin.query or origin.fragment
                            or origin.netloc.lower() not in authorities
                            or origins[0].lower() != f'http://{origin.netloc.lower()}'
                            or (origin.port if origin.port is not None else 80) != self.server.server_port):
                        raise ValueError('cross-origin request')
        except ValueError:
            raise APIError(403, 'untrusted Host or request origin')

    def _handle(self, body):
        url = urlsplit(self.path)
        path = unquote(url.path)
        rt = self.runtime
        if body is None:
            if path == '/capabilities':
                with rt.lock:
                    catalog = self._capability_catalog()
                    return 200, {'catalog_sha256': catalog.snapshot_id, 'entries': catalog.entries()}
            if path == '/health':
                ready = any(rt.router.skills[sid].enabled and not rt.router.skills[sid].is_dormant
                            for sid in BUILTIN_IDS.values()) and not rt.graph_issues
                return (200 if ready else 503), {'status': 'ok' if ready else 'degraded',
                    'version': __version__, 'skills': len(rt.router.skills), 'graph_issues': rt.graph_issues}
            if path == '/stats':
                return 200, rt.stats()
            if path == '/skills':
                return 200, {'skills': [self._skill(s) for s in rt.router.skills.values()],
                             'total': len(rt.router.skills)}
            if path.startswith('/skills/'):
                sid = path[len('/skills/'):]
                if sid not in rt.router.skills:
                    raise APIError(404, 'skill not found')
                return 200, {'skill': self._skill(rt.router.skills[sid])}
            if path == '/runs':
                limit = int(parse_qs(url.query).get('limit', ['20'])[0])
                return 200, {'runs': rt.store.list(limit)}
            if path.startswith('/runs/'):
                try:
                    return 200, rt.store.get(path[len('/runs/'):])
                except KeyError:
                    raise APIError(404, 'run not found')
            raise APIError(404, 'not found')
        if path == '/capabilities/resolve':
            if set(body) - {'capability', 'input_format', 'constraints'}:
                raise ValueError('unknown capability resolution fields')
            with rt.lock:
                return 200, self._capability_catalog().resolve(body.get('capability'),
                    body.get('input_format'), body.get('constraints', []))
        if path == '/intent/prepare':
            allowed = {'extracted', 'workspace', 'session_id', 'source_ref', 'source_facts'}
            if set(body) - allowed or not allowed - {'source_facts'} <= set(body):
                raise ValueError('intent prepare fields are incomplete or unknown')
            from .intent import interpret_intent, MinimalStateStore
            with rt.lock:
                result = interpret_intent(body['extracted'], catalog=self._capability_catalog(),
                    workspace=body['workspace'], session_id=body['session_id'], source_ref=body['source_ref'],
                    source_facts=body.get('source_facts'))
                if result['status'] == 'ready':
                    result = MinimalStateStore(rt.store.path).capture(result,
                        session_id=body['session_id'], source_ref=body['source_ref'])
            return {'ready': 200, 'needs_context': 422, 'unsupported': 422,
                    'ambiguous': 422, 'conflict': 409, 'rejected': 400}[result['status']], result
        if path == '/intent/show':
            if set(body) != {'state_id', 'session_id'}:
                raise ValueError('intent show requires only state_id and session_id')
            from .intent import MinimalStateStore
            return 200, MinimalStateStore(rt.store.path).show(body['state_id'], session_id=body['session_id'])
        if path == '/route':
            state, result = rt.route(body)
            return 200, {'level': result.level.name,
                'candidates': [{'skill_id': s.skill_id, 'name': s.name,
                                'score': s.matches_state(state)} for s in result.candidates],
                'latency_ms': result.latency_ms, 'confidence': result.confidence,
                'reason': result.reason, 'missing_preconditions': result.missing_preconditions,
                'fallback_to_llm': result.fallback_to_llm, 'state': state.to_dict()}
        if path.startswith('/runs/') and path.endswith('/cancel'):
            run_id = path[len('/runs/'):-len('/cancel')]
            if not run_id or '/' in run_id:
                raise APIError(404, 'run not found')
            if set(body) - {'reason'}:
                raise ValueError('cancel accepts only an optional reason')
            try:
                result = rt.cancel(run_id, reason=body.get('reason'))
            except KeyError:
                raise APIError(404, 'run not found')
            return (409 if result['status'] == 'conflict' else 200), result
        if path in ['/run', '/events']:
            result = rt.run(body, source='hermes' if path == '/events' else 'http')
            status = {'succeeded': 200, 'received': 202, 'running': 202,
                      'rejected': 400, 'failed': 422, 'interrupted': 409,
                      'cancelled': 409}[result['status']]
            return status, result
        if path == '/usage':
            if 'success' not in body:
                raise ValueError('success must be explicit')
            rt.record_usage(body.get('skill_id', ''), body['success'])
            return 200, {'status': 'recorded', 'kind': 'external_report', 'verified': False}
        if path == '/skills/state':
            rt.set_skill_state(body.get('skill_id', ''), enabled=body.get('enabled'),
                               dormant=body.get('dormant'))
            return 200, {'status': 'recorded'}
        if path == '/reload':
            with rt.lock:
                rt.reload()
                self.server._unified_catalog = None
            return 200, {'status': 'reloaded'}
        if path == '/causal/discover':
            state = CausalState.from_event(body)
            active = [sid for sid, s in rt.router.skills.items() if s.enabled]
            dormant = {sid for sid, s in rt.router.skills.items() if s.enabled and s.is_dormant}
            return 200, {'discovered': rt.graph.discover_cold_skills(state, active, dormant)}
        if path == '/causal/chain':
            primary = body.get('primary_skill')
            if primary not in rt.router.skills or not rt.router.skills[primary].enabled:
                raise ValueError('primary_skill must name an enabled skill')
            state = CausalState.from_event(body)
            chain = rt.graph.recommend_chain(state, primary)
            return 200, {'chain': [c for c in chain if c in rt.router.skills
                                   and rt.router.skills[c].enabled
                                   and not rt.router.skills[c].is_dormant],
                         'kind': 'suggestion_only'}
        raise APIError(404, 'not found')

    def _capability_catalog(self):
        from .capability_catalog import build_catalog
        with self.runtime.lock:
            if getattr(self.server, '_unified_catalog', None) is None:
                self.server._unified_catalog = build_catalog(
                    db_path=self.runtime.store.path if self.server.catalog_root is not None else None,
                    catalog_root=self.server.catalog_root, skill_snapshot_id=self.server.skill_snapshot_id)
            return self.server._unified_catalog

    def _skill(self, skill):
        return {'id': skill.skill_id, 'name': skill.name, 'description': skill.description,
                'task_types': [t.value for t in skill.task_types], 'priority': skill.priority,
                'enabled': skill.enabled, 'is_dormant': skill.is_dormant,
                'input_formats': skill.input_formats, 'capabilities': skill.capabilities,
                'executable': skill.skill_id in {*BUILTIN_IDS.values(), 'builtin/json-normalize',
                                                'builtin/xlsx-inspect'}}

    def _read_body(self):
        if self.headers.get('Transfer-Encoding'):
            raise APIError(400, 'transfer encoding is unsupported')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise APIError(400, 'invalid Content-Length')
        if length < 0:
            raise APIError(400, 'negative Content-Length')
        if length > MAX_BODY:
            raise APIError(413, 'request exceeds 1 MiB')
        if length and self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise APIError(415, 'Content-Type must be application/json')
        raw = self.rfile.read(length)
        self.deadline.cancel()
        if self.request_expired:
            raise APIError(408, 'request deadline exceeded')
        if len(raw) != length:
            raise APIError(400, 'incomplete request body')
        try:
            body = json.loads(raw.decode('utf-8')) if length else {}
        except (ValueError, UnicodeError):
            raise APIError(400, 'invalid JSON')
        if not isinstance(body, dict):
            raise APIError(400, 'JSON body must be an object')
        return body

    def _json(self, code, data):
        response = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(response)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


def create_server(runtime, host='127.0.0.1', port=18400, request_timeout=2,
                  catalog_root=None, skill_snapshot_id=None):
    return HarnessHTTPServer((host, port), runtime, request_timeout, catalog_root, skill_snapshot_id)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18400)
    parser.add_argument('--db')
    parser.add_argument('--data-dir')
    parser.add_argument('--catalog-root')
    parser.add_argument('--skill-snapshot-id')
    args = parser.parse_args(argv)
    server = create_server(HarnessRuntime(data_dir=args.data_dir, db_path=args.db), args.host, args.port,
                           catalog_root=args.catalog_root, skill_snapshot_id=args.skill_snapshot_id)
    print(json.dumps({'listening': f'http://{args.host}:{server.server_port}', 'version': __version__}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
