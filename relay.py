#!/usr/bin/env python3
"""Local HTTP coordination facade. Native Beads and Agent Mail own all data."""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '0.1.0'
MAX_BODY = 262144
TASK_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
SHA = re.compile(r'^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


class RelayError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def require(condition, code, message, status=400):
    if not condition:
        raise RelayError(status, code, message)


def text(data, key, required=True):
    value = data.get(key)
    require(isinstance(value, str) and (bool(value.strip()) or not required),
            'invalid_field', f'{key} must be a nonempty string')
    return value


def fields(data, allowed):
    require(isinstance(data, dict), 'invalid_body', 'Expected a JSON object')
    unknown = sorted(set(data).difference(allowed))
    if unknown:
        labels = ', '.join(name[:80] for name in unknown[:8])
        raise RelayError(400, 'unknown_fields', 'Unknown request field(s): ' + labels)


def loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'


def load_config(filename):
    path = Path(filename)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, 'unsafe_config',
            'Config must be an owned regular file with mode 0600', 500)
    config = json.loads(path.read_text())
    for key in ('br_bin', 'workspace', 'mail_url', 'mail_token', 'mail_project'):
        text(config, key)
    url = urllib.parse.urlparse(config['mail_url'])
    require(url.scheme == 'http' and loopback(url.hostname or '') and not url.username
            and not url.password and not url.query and not url.fragment,
            'unsafe_upstream', 'Mail must use an explicit loopback HTTP URL', 500)
    require(Path(config['br_bin']).is_absolute() and Path(config['workspace']).is_dir(),
            'invalid_config', 'br_bin must be absolute and workspace must exist', 500)
    agents = config.get('agents')
    require(isinstance(agents, dict) and len(agents) >= 2, 'invalid_config',
            'Configure at least two agents', 500)
    tokens, names = set(), set()
    for name, agent in agents.items():
        require(bool(TASK_ID.fullmatch(name)), 'invalid_config', 'Invalid agent identifier', 500)
        token, mail_name = text(agent, 'token'), text(agent, 'mail_name')
        require(len(token) >= 24 and token not in tokens and mail_name not in names,
                'invalid_config', 'Agent credentials and Mail names must be unique', 500)
        tokens.add(token)
        names.add(mail_name)
    return config


class Relay:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()

    def authenticate(self, header):
        candidate = header[7:] if header.startswith('Bearer ') else ''
        for name, agent in self.config['agents'].items():
            if hmac.compare_digest(candidate.encode(), agent['token'].encode()):
                return name
        raise RelayError(401, 'unauthorized', 'Valid agent bearer token required')

    def br(self, *args, actor=None):
        command = [self.config['br_bin'], *args, '--json']
        if actor:
            command += ['--actor', actor]
        try:
            result = subprocess.run(command, cwd=self.config['workspace'],
                                    capture_output=True, text=True, timeout=30)
            payload = json.loads(result.stdout) if result.stdout.strip() else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise RelayError(502, 'tracker_unavailable', 'Task tracker failed; reread before retrying')
        if result.returncode:
            error = payload.get('error', {}) if isinstance(payload, dict) else {}
            code = error.get('code', '')
            status = 404 if code == 'ISSUE_NOT_FOUND' else 409
            raise RelayError(status, 'tracker_conflict',
                             'Task tracker rejected the request; reread task and retry')
        return payload

    def get(self, task_id):
        require(bool(TASK_ID.fullmatch(task_id)), 'invalid_id', 'Invalid task identifier')
        result = self.br('show', task_id)
        require(isinstance(result, list) and len(result) == 1, 'task_not_found',
                'Task not found', 404)
        return result[0]

    @staticmethod
    def context(task):
        raw = task.get('agent_context')
        try:
            value = json.loads(raw) if isinstance(raw, str) else copy.deepcopy(raw or {})
            require(isinstance(value, dict), 'metadata_conflict', 'Task context must be an object', 409)
            require(isinstance(value.get('relay', {}), dict), 'metadata_conflict',
                    'Relay metadata must be an object', 409)
            return value
        except ValueError:
            raise RelayError(409, 'metadata_conflict', 'Task context contains invalid JSON')

    def view(self, task):
        result = {key: value for key, value in task.items()
                  if key not in ('agent_context', 'source_repo_path')}
        meta = self.context(task).get('relay', {})
        result.update(version=task['updated_at'], relay=meta)
        lease = meta.get('lease') or {}
        result['lease_stale'] = bool(lease and lease.get('expires_at', 0) <= time.time())
        return result

    def checked(self, task_id, body):
        task = self.get(task_id)
        require(text(body, 'version') == task['updated_at'], 'stale_version',
                'Task changed; reread before retrying', 409)
        return task

    @staticmethod
    def owned(task, actor):
        require(task.get('assignee') == actor, 'not_owner', 'Only the task owner may do this', 403)

    @staticmethod
    def unblocked(meta):
        require(not any(b.get('open', True) for b in meta.get('blockers', [])),
                'blocked', 'An authorization blocker is open', 409)

    @staticmethod
    def lease(body):
        seconds = body.get('lease_seconds', 600)
        require(type(seconds) is int and 30 <= seconds <= 3600, 'invalid_lease',
                'lease_seconds must be an integer from 30 to 3600')
        return {'session_id': text(body, 'session_id'), 'expires_at': time.time() + seconds}

    def update(self, task, actor, meta, *args):
        context = self.context(task)
        context['relay'] = meta
        self.br('update', task['id'], '--if-unchanged', task['updated_at'],
                '--agent-context', json.dumps(context, separators=(',', ':')), *args, actor=actor)
        return self.view(self.get(task['id']))

    def tasks(self, actor, method, task_id, action, body):
        with self.lock:
            if method == 'POST' and not task_id:
                fields(body, {'title', 'description', 'acceptance_criteria'})
                args = ['create', text(body, 'title')]
                for key in ('description', 'acceptance_criteria'):
                    if key in body:
                        args += ['--' + key.replace('_', '-'), text(body, key, False)]
                task = self.br(*args, actor=actor)
                return self.view(self.get(task['id']))
            if method == 'GET':
                return self.view(self.get(task_id))
            task = self.checked(task_id, body)
            require(task['status'] not in ('closed', 'tombstone'), 'terminal_task',
                    'Terminal tasks cannot be modified through this API', 409)
            meta = self.context(task).get('relay', {})
            if action == 'claim':
                fields(body, {'version', 'session_id', 'lease_seconds'})
                self.unblocked(meta)
                require(not task.get('assignee'), 'already_owned',
                        'Task already has an owner; expiry does not authorize takeover', 409)
                meta['lease'] = self.lease(body)
                return self.update(task, actor, meta, '--claim')
            if action == 'review':
                fields(body, {'version', 'sha', 'verdict', 'evidence'})
                require(task.get('assignee') and task['assignee'] != actor,
                        'independent_review_required', 'Reviewer must differ from owner', 403)
                require(meta.get('handoff', {}).get('to') == actor,
                        'wrong_reviewer', 'Only the named handoff recipient may review', 403)
                require(task['status'] == 'review', 'not_in_review', 'Task must be in review', 409)
                sha, verdict = text(body, 'sha'), text(body, 'verdict')
                require(sha == meta.get('head_sha'), 'wrong_commit', 'Review must match handoff SHA', 409)
                require(verdict in ('accept', 'request_changes'), 'invalid_verdict', 'Invalid review verdict')
                evidence = text(body, 'evidence')
                meta['review'] = {'sha': sha, 'reviewer': actor, 'verdict': verdict, 'evidence': evidence}
                return self.update(task, actor, meta)
            if action == 'heartbeat':
                self.owned(task, actor)
                fields(body, {'version', 'session_id', 'lease_seconds'})
                lease = self.lease(body)
                require(lease['session_id'] == meta.get('lease', {}).get('session_id'),
                        'wrong_session', 'Session does not own this advisory lease', 409)
                meta['lease'] = lease
            elif action == 'blockers':
                fields(body, {'version', 'action', 'target', 'reason'})
                meta.setdefault('blockers', []).append({'action': text(body, 'action'),
                    'target': text(body, 'target'), 'reason': text(body, 'reason'),
                    'reported_by': actor, 'open': True, 'created_at': time.time()})
            elif action == 'handoff':
                self.owned(task, actor)
                fields(body, {'version', 'to', 'sha', 'evidence'})
                self.unblocked(meta)
                target, sha = text(body, 'to'), text(body, 'sha')
                require(target in self.config['agents'] and target != actor, 'invalid_recipient',
                        'Handoff needs a different configured agent')
                require(bool(SHA.fullmatch(sha)), 'invalid_sha', 'Provide a full Git SHA')
                evidence = text(body, 'evidence')
                meta.update(head_sha=sha, evidence=evidence,
                            handoff={'to': target, 'from': actor, 'sha': sha}, review=None)
                return self.update(task, actor, meta, '--status', 'review')
            elif action == 'accept':
                raise RelayError(409, 'not_a_transfer',
                                 'Use independent review to accept review handoffs; ownership stays with author')
            elif not action and method == 'PATCH':
                self.owned(task, actor)
                fields(body, {'version', 'status', 'notes', 'evidence'})
                args = []
                if 'evidence' in body:
                    meta['evidence'] = text(body, 'evidence')
                if 'notes' in body:
                    args += ['--notes', text(body, 'notes', False)]
                if 'status' in body:
                    status_value = text(body, 'status')
                    require(status_value in ('in_progress', 'blocked', 'review', 'closed'),
                            'invalid_status', 'Unsupported task status')
                    if status_value == 'closed':
                        self.unblocked(meta)
                        review = meta.get('review') or {}
                        require(meta.get('head_sha') and review.get('sha') == meta['head_sha']
                                and review.get('reviewer') != actor and review.get('verdict') == 'accept',
                                'review_required', 'Independent acceptance of exact head SHA required', 409)
                        require(set(body) == {'version', 'status'}, 'close_separately',
                                'Record metadata before closing, then close with version and status only')
                        self.br('close', task_id, '--reason', 'Independent review accepted ' + meta['head_sha'],
                                actor=actor)
                        return self.view(self.get(task_id))
                    if status_value == 'review':
                        require(meta.get('head_sha'), 'handoff_required', 'Create a commit handoff first', 409)
                    if status_value == 'in_progress':
                        meta['review'] = None
                        meta.pop('head_sha', None)
                        meta.pop('handoff', None)
                        meta.pop('evidence', None)
                    args += ['--status', status_value]
                    if task['status'] in ('blocked', 'review') and status_value == 'in_progress':
                        args.append('--force')
                return self.update(task, actor, meta, *args)
            else:
                raise RelayError(404, 'not_found', 'Route not found')
            return self.update(task, actor, meta)

    def mail(self, name, arguments):
        packet = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                  'params': {'name': name, 'arguments': arguments}}
        request = urllib.request.Request(self.config['mail_url'], json.dumps(packet).encode(),
            headers={'Authorization': 'Bearer ' + self.config['mail_token'],
                     'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
        try:
            with HTTP.open(request, timeout=30) as response:
                raw = response.read(MAX_BODY + 1).decode()
            require(len(raw) <= MAX_BODY, 'upstream_too_large', 'Upstream response too large', 502)
            if raw.startswith(('event:', 'data:')):
                raw = next(line[6:] for line in raw.splitlines() if line.startswith('data: '))
            packet = json.loads(raw)
            require('error' not in packet, 'mail_error', 'Mail rejected the request', 502)
            result = packet['result']
            require(not result.get('isError'), 'mail_error', 'Mail rejected the request', 409)
            value = json.loads(next(c['text'] for c in result['content'] if c['type'] == 'text'))
            return self.redact(value)
        except RelayError:
            raise
        except (OSError, ValueError, KeyError, StopIteration):
            raise RelayError(502, 'mail_unavailable', 'Mail failed; check delivery before retrying')

    def mail_resource(self, uri):
        packet = {'jsonrpc': '2.0', 'id': 1, 'method': 'resources/read',
                  'params': {'uri': uri}}
        request = urllib.request.Request(self.config['mail_url'], json.dumps(packet).encode(),
            headers={'Authorization': 'Bearer ' + self.config['mail_token'],
                     'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
        try:
            with HTTP.open(request, timeout=30) as response:
                raw = response.read(MAX_BODY + 1).decode()
            require(len(raw) <= MAX_BODY, 'upstream_too_large', 'Upstream response too large', 502)
            if raw.startswith(('event:', 'data:')):
                raw = next(line[6:] for line in raw.splitlines() if line.startswith('data: '))
            result = json.loads(raw)
            require('error' not in result, 'mail_error', 'Mail rejected the request', 404)
            contents = result['result']['contents']
            require(bool(contents) and 'text' in contents[0], 'message_not_found',
                    'Message not found', 404)
            return self.redact(json.loads(contents[0]['text']))
        except RelayError:
            raise
        except (OSError, ValueError, KeyError, StopIteration):
            raise RelayError(502, 'mail_unavailable', 'Mail failed; check delivery before retrying')

    def inbox_contains(self, identity, message_id):
        after = 0
        for _ in range(10000):
            page = self.mail('fetch_inbox_events', {**identity, 'after': after, 'limit': 1000})
            if any(event.get('message_id') == message_id for event in page.get('events', [])):
                return True
            next_cursor = page.get('next_cursor', after)
            if not page.get('has_more') or next_cursor <= after:
                return False
            after = next_cursor
        raise RelayError(503, 'inbox_too_large', 'Inbox history exceeds safe lookup limit')

    def inbox_ack_state(self, agent_name, event):
        if not event.get('ack_required'):
            return {**event, 'acknowledged': None, 'acknowledged_at': None}
        receipt = self.mail('get_message_delivery_receipt', {
            'project_key': self.config['mail_project'], 'message_id': event['message_id']})
        recipient = next((item for item in receipt.get('recipients', [])
                          if item.get('recipient') == agent_name and item.get('kind') == event.get('kind')),
                         None)
        return {**event, 'acknowledged': bool(recipient and recipient.get('acknowledged')),
                'acknowledged_at': recipient.get('acknowledged_at') if recipient else None}

    def inbox_ack_states(self, agent_name, events):
        results = [None] * len(events)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self.inbox_ack_state, agent_name, event): index
                       for index, event in enumerate(events) if event.get('ack_required')}
            for index, event in enumerate(events):
                if not event.get('ack_required'):
                    results[index] = self.inbox_ack_state(agent_name, event)
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return results

    @classmethod
    def redact(cls, value):
        if isinstance(value, dict):
            return {k: cls.redact(v) for k, v in value.items()
                    if k not in ('registration_token', 'sender_token', 'token', 'mail_token',
                                 'project', 'source_repo_path')}
        if isinstance(value, list):
            return [cls.redact(v) for v in value]
        return value

    def dispatch(self, actor, method, path, query, body):
        parts = [urllib.parse.unquote(p) for p in path.split('/') if p]
        if parts == ['tasks'] and method == 'GET':
            with self.lock:
                try:
                    offset = int(query.get('offset', ['0'])[0])
                    limit = int(query.get('limit', ['100'])[0])
                except ValueError:
                    raise RelayError(400, 'invalid_filter', 'offset and limit must be integers')
                require(0 <= offset <= 1000000, 'invalid_filter', 'offset is out of range')
                require(1 <= limit <= 1000, 'invalid_filter', 'limit must be between 1 and 1000')
                args = ['list', '--all', '--limit', str(limit), '--offset', str(offset)]
                if 'state' in query:
                    state = query['state'][0]
                    require(state in ('active', 'open', 'in_progress', 'blocked', 'deferred', 'draft',
                                      'closed', 'tombstone', 'pinned', 'review'),
                            'invalid_filter', 'Unsupported task state')
                    if state != 'active':
                        args += ['--status', state]
                result = self.br(*args)
                response = {k: v for k, v in result.items() if k != 'issues'}
                tasks = [self.view(t) for t in result['issues']]
                if query.get('state', [''])[0] == 'active':
                    tasks = [task for task in tasks
                             if task.get('status') in ('open', 'in_progress', 'blocked', 'review')]
                    response['returned'] = len(tasks)
                return {**response, 'tasks': tasks}
        if parts and parts[0] == 'tasks' and len(parts) <= 3:
            action = parts[2] if len(parts) == 3 else None
            allowed = (len(parts) == 1 and method == 'POST') or (len(parts) == 2 and method in ('GET', 'PATCH')) or (len(parts) == 3 and method == 'POST')
            require(allowed, 'not_found', 'Route not found', 404)
            return self.tasks(actor, method, parts[1] if len(parts) >= 2 else None, action, body)
        agent = self.config['agents'][actor]
        identity = {'project_key': self.config['mail_project'], 'agent_name': agent['mail_name']}
        if parts == ['inbox'] and method == 'GET':
            try:
                after, limit = int(query.get('after', ['0'])[0]), int(query.get('limit', ['100'])[0])
            except ValueError:
                raise RelayError(400, 'invalid_cursor', 'after and limit must be integers')
            require(after >= 0 and 1 <= limit <= 1000, 'invalid_cursor', 'Invalid inbox cursor or limit')
            pending_filter = query.get('pending_ack', ['false'])[0]
            require(pending_filter in ('true', 'false'), 'invalid_filter',
                    'pending_ack must be true or false')
            if pending_filter == 'true':
                require('after' not in query, 'invalid_filter',
                        'pending_ack is a full pending snapshot; omit after')
                cursor, pending, tail = 0, [], 0
                for _ in range(10000):
                    page = self.mail('fetch_inbox_events', {**identity, 'after': cursor, 'limit': 1000})
                    tail = page.get('tail_cursor', tail)
                    events = self.inbox_ack_states(agent['mail_name'], page.get('events', []))
                    pending.extend(event for event in events
                                   if event.get('ack_required') and not event['acknowledged'])
                    next_cursor = page.get('next_cursor', cursor)
                    if not page.get('has_more'):
                        return {'events': pending[:limit], 'pending_total': len(pending),
                                'has_more_pending': len(pending) > limit,
                                'scanned_through_cursor': tail}
                    require(next_cursor > cursor, 'mail_cursor_error',
                            'Inbox cursor did not advance', 502)
                    cursor = next_cursor
                raise RelayError(503, 'inbox_too_large', 'Inbox history exceeds safe pending-ack scan limit')
            page = self.mail('fetch_inbox_events', {**identity, 'after': after, 'limit': limit})
            events = self.inbox_ack_states(agent['mail_name'], page.get('events', []))
            return {**page, 'events': events}
        if len(parts) == 2 and parts[0] == 'messages' and method == 'GET':
            require(parts[1].isdigit(), 'invalid_id', 'Invalid message identifier')
            message_id = int(parts[1])
            require(self.inbox_contains(identity, message_id), 'message_not_found',
                    'Message is not in this agent inbox', 404)
            uri = ('resource://message/' + str(message_id) + '?' + urllib.parse.urlencode(
                {'project': self.config['mail_project'], 'agent': agent['mail_name']}))
            return self.mail_resource(uri)
        if parts == ['messages'] and method == 'POST':
            fields(body, {'to', 'subject', 'body', 'thread_id', 'ack_required', 'idempotency_key'})
            to = body.get('to')
            require(isinstance(to, list) and to and all(isinstance(a, str) and a in self.config['agents'] for a in to),
                    'invalid_recipient', 'Address configured agents explicitly')
            args = {'project_key': self.config['mail_project'], 'sender_name': agent['mail_name'],
                    'to': [self.config['agents'][a]['mail_name'] for a in to],
                    'subject': text(body, 'subject'), 'body_md': text(body, 'body')}
            for key in ('thread_id', 'idempotency_key'):
                if key in body:
                    args[key] = text(body, key)
            if 'ack_required' in body:
                require(type(body['ack_required']) is bool, 'invalid_field', 'ack_required must be boolean')
                args['ack_required'] = body['ack_required']
            if agent.get('mail_registration_token'):
                args['sender_token'] = agent['mail_registration_token']
            return self.mail('send_message', args)
        if len(parts) == 3 and parts[0] == 'messages' and parts[2] == 'ack' and method == 'POST':
            fields(body, set())
            require(parts[1].isdigit(), 'invalid_id', 'Invalid message identifier')
            message_id = int(parts[1])
            require(self.inbox_contains(identity, message_id), 'message_not_found',
                    'Message is not in this agent inbox', 404)
            return self.mail('acknowledge_message', {**identity, 'message_id': message_id})
        if len(parts) == 3 and parts[0] == 'messages' and parts[2] == 'read' and method == 'POST':
            fields(body, set())
            require(parts[1].isdigit(), 'invalid_id', 'Invalid message identifier')
            message_id = int(parts[1])
            require(self.inbox_contains(identity, message_id), 'message_not_found',
                    'Message is not in this agent inbox', 404)
            return self.mail('mark_message_read', {**identity, 'message_id': message_id})
        raise RelayError(404, 'not_found', 'Route not found')


class Handler(BaseHTTPRequestHandler):
    server_version = 'AgentRelay/' + VERSION

    def log_message(self, *_args):
        pass  # Never log bearer tokens, message bodies or query parameters.

    def respond(self, status, body):
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(encoded)

    def handle_request(self):
        try:
            url = urllib.parse.urlsplit(self.path)
            if self.command == 'GET' and url.path == '/health':
                return self.respond(200, {'ok': True, 'version': VERSION})
            actor = self.server.relay.authenticate(self.headers.get('Authorization', ''))
            require(not self.headers.get('Transfer-Encoding'), 'invalid_body', 'Chunked bodies unsupported')
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise RelayError(400, 'invalid_body', 'Invalid Content-Length')
            require(0 <= size <= MAX_BODY, 'body_too_large', 'Request exceeds body limit', 413)
            body = json.loads(self.rfile.read(size)) if size else {}
            require(isinstance(body, dict), 'invalid_body', 'Expected a JSON object')
            value = self.server.relay.dispatch(actor, self.command, url.path,
                                              urllib.parse.parse_qs(url.query), body)
            self.respond(200, value)
        except (ValueError, UnicodeError):
            self.respond(400, {'error': {'code': 'invalid_json', 'message': 'Invalid JSON'}})
        except RelayError as exc:
            self.respond(exc.status, {'error': {'code': exc.code, 'message': exc.message}})
        except Exception:
            self.respond(500, {'error': {'code': 'internal_error', 'message': 'Internal relay error'}})

    do_GET = do_POST = do_PATCH = handle_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18765)
    args = parser.parse_args()
    require(loopback(args.host), 'unsafe_bind', 'Only loopback binding is allowed', 500)
    config = load_config(args.config)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.relay = Relay(config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
