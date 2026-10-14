"""An authenticated, session-owned boundary for independent Harness execution.

The Hermes process keeps its OS sandbox. This parent-side endpoint starts only
the fixed Harness CLI, which can apply the candidate's separate OS sandbox.
"""
from __future__ import annotations

from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import tempfile

MAX_BODY = 1_048_576
_IDS = ('session_id', 'task_id', 'turn_id', 'api_request_id', 'tool_call_id')
_NATIVE = {*_IDS, 'tool_name', 'entry_key', 'task_attempt', 'contract_sha256', 'input_sha256'}


class _Environment(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup = {'tracked_children': 0, 'remaining_children': [],
                        'cleanup_complete': False, 'errors': []}


def _validated_request(body, database, workspace):
    from .hermes import _prepare_request
    from .hermes_gate import contract_for, digest_json
    from ..path_binding import source_digest_nofollow
    if (not isinstance(body, dict) or set(body) != {'payload', 'receipt_id', 'native'}
            or not isinstance(body['native'], dict) or set(body['native']) - _NATIVE
            or not isinstance(body['receipt_id'], str) or not body['receipt_id']):
        raise ValueError('invalid broker envelope')
    native = body['native']
    if (any(not isinstance(native.get(key), str) or not native[key] for key in _IDS)
            or native.get('tool_name') != 'rpe_task'):
        raise PermissionError('native receipt binding required')
    try:
        payload = _prepare_request({'request': body['payload']}, workspace, native['session_id'])
    except (ValueError, TypeError, OSError) as exc:
        raise PermissionError('request leaves the fixed workspace') from exc
    if payload != body['payload']:
        raise PermissionError('submitted paths and session must already be canonical')
    contract_sha = digest_json(contract_for(payload, workspace))
    input_sha = source_digest_nofollow(payload['execution']['input_path'],
        max_bytes=1048576 if payload.get('capability') == 'json.normalize' else None)
    if native.get('contract_sha256') != contract_sha or native.get('input_sha256') != input_sha:
        raise PermissionError('fresh execution contract differs from receipt')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        receipt = db.execute('SELECT kind,metadata_json FROM hermes_host_events WHERE ingest_event_id=?',
                             (body['receipt_id'],)).fetchone()
        if receipt is None or receipt[0] != 'rpe_task_received' or json.loads(receipt[1]) != native:
            raise PermissionError('durable receipt does not match native context')
        rows = [(kind, json.loads(metadata)) for kind, metadata in db.execute(
            'SELECT kind,metadata_json FROM hermes_host_events WHERE session_id=? ORDER BY id',
            (native['session_id'],))]
        if native.get('entry_key'):
            expected = 'entry-' + digest_json([native['entry_key'], contract_sha, input_sha])
            policies = [event for kind, event in rows if kind == 'pre_api_request'
                        and all(event.get(key) == native[key] for key in _IDS[:-1])]
            if (payload.get('event_id') != expected or len(policies) != 1
                    or policies[0].get('request_tool_choice') != 'rpe_task'
                    or policies[0].get('request_parallel_tool_calls') is not False
                    or policies[0].get('request_tool_names') != ['rpe_task']):
                raise PermissionError('actual native API policy does not admit execution')
        # A linked durable outcome closes admission across a broker restart too.
        for kind, event in (rows if native.get('entry_key') else []):
            if (kind != 'post_tool_call' or event.get('turn_id') != native['turn_id']
                    or event.get('tool_name') != 'rpe_task' or not event.get('run_id')):
                continue
            returned = next((e for k, e in rows if k == 'rpe_task_returned'
                             and e.get('receipt_id') == event.get('receipt_id')
                             and e.get('run_id') == event['run_id']
                             and all(e.get(key) == event.get(key) for key in _IDS)), None)
            if returned and returned.get('status') in {'succeeded', 'rejected'}:
                run = db.execute('SELECT status FROM runs WHERE run_id=?', (event['run_id'],)).fetchone()
                if run and run[0] == returned['status']:
                    raise PermissionError('this native turn already has a durable outcome')
    return payload, native


def _validated_skill_request(body, database):
    """Read-only native policy plus an immutable argument-bound RPC receipt."""
    from .hermes_gate import digest_json
    operations = {'discover':'rpe_discover', 'context':'rpe_context',
                  'lookup':'skill_view', 'revalidate':'rpe_task'}
    if (not isinstance(body, dict) or set(body) != {'operation','arguments','receipt_id','native'}
            or body.get('operation') not in operations or not isinstance(body.get('arguments'),dict)
            or not isinstance(body.get('native'),dict) or not isinstance(body.get('receipt_id'),str)):
        raise ValueError('invalid skill RPC envelope')
    native = body['native']
    if (set(native) - {*_IDS, 'tool_name','entry_key','arguments_sha256'}
            or any(not isinstance(native.get(k),str) or not native[k] for k in (*_IDS,'entry_key'))
            or native.get('tool_name') != operations[body['operation']]
            or native.get('arguments_sha256') != digest_json(body['arguments'])):
        raise PermissionError('native argument binding required')
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        receipt = db.execute('SELECT kind,metadata_json FROM hermes_host_events WHERE ingest_event_id=?',
                             (body['receipt_id'],)).fetchone()
        if (receipt is None or receipt[0] != 'rpe_skill_rpc_received'
                or json.loads(receipt[1]) != native):
            raise PermissionError('skill RPC receipt differs')
        policies = [json.loads(row[0]) for row in db.execute(
            "SELECT metadata_json FROM hermes_host_events WHERE kind='pre_api_request' AND session_id=?",
            (native['session_id'],))]
    policies = [p for p in policies if all(p.get(k)==native[k] for k in _IDS[:-1])]
    if (len(policies)!=1 or policies[0].get('request_tool_choice')!=native['tool_name']
            or policies[0].get('request_tool_names')!=[native['tool_name']]
            or policies[0].get('request_parallel_tool_calls') is not False
            or not policies[0].get('request_tools_sha256')):
        raise PermissionError('actual native API policy does not admit skill RPC')
    return native


@contextmanager
def execution_broker(*, db_path, workspace, python, request_timeout=120, extension=None):
    """Yield URL/token env entries; after exit ``env.cleanup`` verifies children."""
    database, directory, interpreter = Path(db_path), Path(workspace), Path(python)
    if (not database.is_absolute() or database != database.resolve()
            or any(p.is_symlink() for p in [database, *database.parents])
            or not directory.is_absolute() or directory != directory.resolve() or not directory.is_dir()
            or any(p.is_symlink() for p in [directory, *directory.parents])
            or not interpreter.is_absolute() or not interpreter.is_file()
            or type(request_timeout) not in (int, float) or not 0 < request_timeout <= 120):
        raise ValueError('broker requires fixed canonical paths and a bounded timeout')
    token = secrets.token_urlsafe(32)
    lock, execution_lock = threading.RLock(), threading.Lock()
    closing = threading.Event()
    jobs, claims, closed_turns, connections, workers = [], {}, set(), set(), set()
    environment = _Environment()
    package_root = str(Path(__file__).resolve().parents[2])

    def stop(job):
        from .hermes_session import _capture_descendants, _stop_descendants, _remaining_descendants
        with job['lock']:
            if job.get('stopped'):
                return
            process = job['process']
            try:
                # Freeze the trusted CLI before capturing its detached candidate.
                if process.poll() is None:
                    os.kill(process.pid, signal.SIGSTOP)
            except OSError as exc:
                job['errors'].append(type(exc).__name__)
            try:
                if job['identity']:
                    _capture_descendants(process.pid, job['identity'], job['descendants'])
                elif process.poll() is None:
                    job['errors'].append('ProcessIdentityMissing')
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                job['errors'].append(type(exc).__name__)
            try:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError) as exc:
                job['errors'].append(type(exc).__name__)
            try:
                # Discovery failure must never skip previously identified children.
                _stop_descendants(job['descendants'])
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                job['errors'].append(type(exc).__name__)
            try:
                job['remaining'] = _remaining_descendants(job['descendants'])
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                job['errors'].append(type(exc).__name__)
            job['stopped'] = True

    def execute(payload, binding):
        from .hermes_session import _process_table, _capture_descendants
        child_env = {key: value for key, value in os.environ.items()
                     if key in {'HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE'}}
        child_env.update(PYTHONPATH=package_root, PYTHONDONTWRITEBYTECODE='1',
                         TMPDIR=temporary.name, TMP=temporary.name, TEMP=temporary.name)
        child_env.update(binding.child_environment())
        argv = [str(interpreter), '-B', '-m', 'rpe_harness', 'events', 'submit',
                '--event', '-', '--db', str(database)]
        with lock:
            if closing.is_set():
                return 503, {'error': {'code': 'broker_closed'}}
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=temporary.name, env=child_env,
                pass_fds=binding.pass_fds, start_new_session=True)
            job = {'process': process, 'identity': None, 'descendants': {}, 'remaining': [],
                   'errors': [], 'lock': threading.RLock(), 'stopped': False}
            jobs.append(job)
        try:
            job['identity'] = _process_table().get(process.pid, (None, None))[1]
            deadline, incoming = time.monotonic() + request_timeout, json.dumps(payload, ensure_ascii=False)
            while True:
                if closing.is_set():
                    return 503, {'error': {'code': 'broker_closed'}}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 504, {'error': {'code': 'harness_timeout'}}
                if job['identity']:
                    _capture_descendants(process.pid, job['identity'], job['descendants'])
                try:
                    stdout, _ = process.communicate(input=incoming, timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    incoming = None
            result = json.loads(stdout)
            if (not isinstance(result, dict) or result.get('status') not in {
                    'succeeded', 'rejected', 'failed', 'interrupted', 'received', 'running', 'error'}
                    or (result['status'] == 'succeeded' and process.returncode != 0)):
                raise ValueError('invalid fixed CLI response')
            return 200, {'returncode': process.returncode, 'result': result}
        except (ValueError, TypeError):
            return 502, {'error': {'code': 'harness_protocol_error'}}
        except (OSError, subprocess.SubprocessError) as exc:
            job['errors'].append(type(exc).__name__)
            return 502, {'error': {'code': 'harness_launch_error'}}
        finally:
            stop(job)

    def skill_response(body):
        if not callable(extension):
            return 404, {'error':{'code':'broker_route_invalid'}}
        try:
            native = _validated_skill_request(body, database)
            signature = json.dumps(body,sort_keys=True,allow_nan=False,ensure_ascii=False)
            receipt_id = body['receipt_id']
            with execution_lock:
                with lock:
                    previous = claims.get(receipt_id)
                    if previous is not None:
                        if previous['signature'] != signature:
                            raise PermissionError('skill RPC receipt changed')
                        if previous['response'] is not None:
                            return previous['response']
                    if closing.is_set():
                        return 503, {'error':{'code':'broker_closed'}}
                    claims[receipt_id] = {'signature':signature,'response':None}
                value = extension(body['operation'],body['arguments'],native)
                if not isinstance(value,dict) or len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode()) > MAX_BODY:
                    raise ValueError('skill RPC response exceeds its budget')
                response = (200, {'result':value})
                with lock:
                    claims[receipt_id]['response'] = response
                return response
        except PermissionError:
            return 403, {'error':{'code':'broker_receipt_invalid'}}
        except (ValueError,TypeError,KeyError):
            return 400, {'error':{'code':'broker_envelope_invalid'}}
        except (OSError,sqlite3.Error):
            return 403, {'error':{'code':'broker_receipt_invalid'}}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(3)
            super().setup()

        def log_message(self, *_):
            pass

        def reply(self, status, result):
            encoded = json.dumps(result, allow_nan=False, ensure_ascii=False).encode()
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(encoded)
            except (OSError, ValueError):
                pass
            self.close_connection = True

        def send_error(self, code, *_args, **_kwargs):
            self.reply(code, {'error': {'code': 'broker_http_invalid'}})

        def do_POST(self):
            if self.path not in ('/task','/skill') or (self.path=='/skill' and not callable(extension)):
                return self.reply(404, {'error': {'code': 'broker_route_invalid'}})
            authorization = self.headers.get('Authorization', '')
            if not hmac.compare_digest(authorization.encode('utf-8'), ('Bearer ' + token).encode('ascii')):
                return self.reply(401, {'error': {'code': 'broker_auth_invalid'}})
            if self.headers.get('Content-Type') != 'application/json':
                return self.reply(415, {'error': {'code': 'broker_content_type_invalid'}})
            lengths = self.headers.get_all('Content-Length', [])
            if (self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1
                    or not lengths[0].isascii() or not lengths[0].isdigit()):
                return self.reply(400, {'error': {'code': 'broker_envelope_invalid'}})
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                return self.reply(413, {'error': {'code': 'broker_body_size_invalid'}})
            try:
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError('incomplete JSON envelope')
                body = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if self.path == '/skill':
                    return self.reply(*skill_response(body))
                payload, native = _validated_request(body, database, directory)
            except PermissionError:
                return self.reply(403, {'error': {'code': 'broker_receipt_invalid'}})
            except (ValueError, TypeError, KeyError):
                return self.reply(400, {'error': {'code': 'broker_envelope_invalid'}})
            except (OSError, sqlite3.Error):
                return self.reply(403, {'error': {'code': 'broker_receipt_invalid'}})
            from .hermes_gate import digest_json
            signature = digest_json(body)
            receipt_id, turn = body['receipt_id'], tuple(native[key] for key in _IDS[:3])
            with lock:
                previous = claims.get(receipt_id)
                if previous:
                    if previous['signature'] != signature:
                        return self.reply(403, {'error': {'code': 'broker_receipt_changed'}})
                    if previous['response'] is not None:
                        return self.reply(*previous['response'])
                    return self.reply(409, {'error': {'code': 'broker_receipt_busy'}})
                if (native.get('entry_key') and turn in closed_turns) or closing.is_set():
                    return self.reply(403, {'error': {'code': 'broker_entry_closed'}})
                claims[receipt_id] = {'signature': signature, 'response': None}
            while not execution_lock.acquire(timeout=0.05):
                if closing.is_set():
                    return self.reply(503, {'error': {'code': 'broker_closed'}})
            try:
                with lock:
                    denied = (native.get('entry_key') and turn in closed_turns) or closing.is_set()
                if denied:
                    response = (403, {'error': {'code': 'broker_entry_closed'}})
                else:
                    from ..path_binding import BoundExecution
                    try:
                        binding = BoundExecution.capture(payload, directory, temporary.name,
                                                         native['input_sha256'])
                    except (OSError, ValueError):
                        response = (403, {'error': {'code': 'broker_path_binding_unavailable'}})
                    else:
                        try:
                            response = execute(payload, binding)
                        finally:
                            binding.close()
                with lock:
                    claims[receipt_id]['response'] = response
                    result = response[1].get('result', {})
                    if (native.get('entry_key') and result.get('run_id')
                            and result.get('status') in {'succeeded', 'rejected'}):
                        closed_turns.add(turn)
            except OSError:
                response = (502, {'error': {'code': 'harness_launch_error'}})
                with lock:
                    claims[receipt_id]['response'] = response
            finally:
                execution_lock.release()
            self.reply(*response)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False
        def process_request(self, connection, address):
            with lock:
                if closing.is_set():
                    self.close_request(connection)
                    return
                worker = threading.Thread(target=self.process_request_thread,
                    args=(connection, address), daemon=True)
                workers.add(worker)
                worker.start()

        def process_request_thread(self, connection, address):
            try:
                super().process_request_thread(connection, address)
            finally:
                with lock:
                    workers.discard(threading.current_thread())

        def get_request(self):
            connection, address = super().get_request()
            with lock:
                if closing.is_set():
                    connection.close()
                    raise OSError('broker is closing')
                connections.add(connection)
            return connection, address

        def close_request(self, connection):
            with lock:
                connections.discard(connection)
            super().close_request(connection)

        def handle_error(self, *_):
            pass

    # Database and its parent are the trusted broker state boundary. Temporary
    # writes must not resolve through attacker-replaceable workspace parents.
    temporary = tempfile.TemporaryDirectory(prefix='.rpe-execution-', dir=database.parent)
    try:
        server = Server(('127.0.0.1', 0), Handler)
    except BaseException:
        temporary.cleanup()
        raise
    server.timeout = 0.1
    environment.update(RPE_HERMES_BROKER_URL=f'http://127.0.0.1:{server.server_port}/task',
                       RPE_HERMES_BROKER_TOKEN=token)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05),
                              name='rpe-owned-execution-broker', daemon=True)
    thread.start()
    try:
        yield environment
    finally:
        with lock:
            closing.set()
            active = list(jobs)
            accepted = list(connections)
        server.shutdown()
        # Per-read timeouts alone cannot stop a continuously dripping request.
        for connection in accepted:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for job in active:
            stop(job)
        server.server_close()
        deadline = time.monotonic() + 3
        with lock:
            pending_workers = list(workers)
        for worker in pending_workers:
            worker.join(timeout=max(0, deadline-time.monotonic()))
        thread.join(timeout=3)
        remaining = sorted({pid for job in jobs for pid in job['remaining']})
        errors = [{'stage': 'external_executor_cleanup', 'error_type': error}
                  for job in jobs for error in job['errors']]
        if thread.is_alive():
            errors.append({'stage': 'external_executor_cleanup', 'error_type': 'BrokerThreadAlive'})
        if any(worker.is_alive() for worker in pending_workers):
            errors.append({'stage': 'external_executor_cleanup', 'error_type': 'BrokerWorkerAlive'})
        try:
            temporary.cleanup()
        except OSError as exc:
            errors.append({'stage': 'external_executor_temp_cleanup', 'error_type': type(exc).__name__})
        environment.cleanup.update(tracked_children=len(jobs), remaining_children=remaining,
            cleanup_complete=not errors and not remaining and all(job['process'].poll() is not None for job in jobs),
            errors=errors)
