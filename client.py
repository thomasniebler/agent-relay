#!/usr/bin/env python3
"""Small HTTP client that never places a bearer token in shell arguments."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import sys
import urllib.parse
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=['codex', 'claude'], default='codex')
    parser.add_argument('--config', type=Path)
    parser.add_argument('method', choices=['GET', 'POST', 'PATCH'])
    parser.add_argument('path')
    parser.add_argument('--body', default=None, help='JSON payload; omit for GET')
    args = parser.parse_args()
    config = json.loads((args.config or Path(__file__).parent / '.local/clients' / (args.agent + '.json')).read_text())
    base = urllib.parse.urlsplit(config['url'])
    if (base.scheme != 'http' or not is_loopback(base.hostname or '') or base.username
            or base.password or base.query or base.fragment):
        parser.error('client config URL must be an explicit loopback HTTP URL')
    if not args.path.startswith('/') or args.path.startswith('//'):
        parser.error('path must be a local absolute API path')
    api_path = args.path
    url_parts = urllib.parse.urlsplit(api_path)
    is_inbox = args.method == 'GET' and url_parts.path == '/inbox'
    query = urllib.parse.parse_qs(url_parts.query)
    pending_view = is_inbox and query.get('pending_ack', ['false'])[0] == 'true'
    remember_cursor = is_inbox and not pending_view
    if remember_cursor:
        cursor_path = Path(args.config or Path(__file__).parent / '.local/clients' / (args.agent + '.json')).with_suffix('.cursor')
        if 'after' not in query and cursor_path.exists():
            query['after'] = [cursor_path.read_text().strip()]
        query.setdefault('limit', ['100'])
        api_path = urllib.parse.urlunsplit(url_parts._replace(query=urllib.parse.urlencode(query, doseq=True)))
    if args.body == '-':
        data = json.dumps(json.load(sys.stdin)).encode()
    else:
        data = json.dumps(json.loads(args.body)).encode() if args.body else None
    request = urllib.request.Request(config['url'] + api_path, data, method=args.method,
        headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
    try:
        with HTTP.open(request, timeout=30) as response:
            raw = response.read().decode()
            value = json.loads(raw)
            sys.stdout.write(json.dumps(value, ensure_ascii=False) + '\n')
            sys.stdout.flush()
            if remember_cursor and isinstance(value.get('next_cursor'), int):
                lock_path = cursor_path.with_suffix('.lock')
                lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    previous = int(cursor_path.read_text()) if cursor_path.exists() else 0
                    fd, temporary = tempfile.mkstemp(dir=cursor_path.parent, prefix='.cursor-')
                    try:
                        with os.fdopen(fd, 'w') as stream:
                            stream.write(str(max(previous, value['next_cursor'])) + '\n')
                        os.chmod(temporary, 0o600)
                        os.replace(temporary, cursor_path)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
    except urllib.error.HTTPError as error:
        print(error.read().decode())
        sys.exit(1)


if __name__ == '__main__':
    main()
