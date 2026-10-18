#!/usr/bin/env python3
"""Small HTTP client that never places a bearer token in shell arguments."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import sys
import urllib.parse
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=['codex', 'claude'], default='codex')
    parser.add_argument('--config', type=Path)
    parser.add_argument('method', choices=['GET', 'POST', 'PATCH'])
    parser.add_argument('path')
    parser.add_argument('--body', default=None, help='JSON payload; omit for GET')
    args = parser.parse_args()
    config = json.loads((args.config or Path(__file__).parent / '.local/clients' / (args.agent + '.json')).read_text())
    if not args.path.startswith('/') or args.path.startswith('//'):
        parser.error('path must be a local absolute API path')
    api_path = args.path
    cursor_path = Path(args.config or Path(__file__).parent / '.local/clients' / (args.agent + '.json')).with_suffix('.cursor')
    remember_cursor = args.method == 'GET' and urllib.parse.urlsplit(api_path).path == '/inbox'
    if remember_cursor:
        url_parts = urllib.parse.urlsplit(api_path)
        query = urllib.parse.parse_qs(url_parts.query)
        if 'after' not in query and cursor_path.exists():
            query['after'] = [cursor_path.read_text().strip()]
        query.setdefault('limit', ['100'])
        api_path = urllib.parse.urlunsplit(url_parts._replace(query=urllib.parse.urlencode(query, doseq=True)))
    data = json.dumps(json.loads(args.body)).encode() if args.body else None
    request = urllib.request.Request(config['url'] + api_path, data, method=args.method,
        headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode()
            value = json.loads(raw)
            sys.stdout.write(json.dumps(value, ensure_ascii=False) + '\n')
            sys.stdout.flush()
            if remember_cursor and isinstance(value.get('next_cursor'), int):
                previous = int(cursor_path.read_text()) if cursor_path.exists() else 0
                fd, temporary = tempfile.mkstemp(dir=cursor_path.parent, prefix='.cursor-')
                with os.fdopen(fd, 'w') as stream:
                    stream.write(str(max(previous, value['next_cursor'])) + '\n')
                os.chmod(temporary, 0o600)
                os.replace(temporary, cursor_path)
    except urllib.error.HTTPError as error:
        print(error.read().decode())
        sys.exit(1)


if __name__ == '__main__':
    main()
