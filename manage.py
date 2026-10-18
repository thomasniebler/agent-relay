#!/usr/bin/env python3
"""Explicit local provisioning and foreground supervision; no global install."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / '.local'


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def rpc(config, name, arguments):
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': name, 'arguments': arguments}}
    request = urllib.request.Request(config['mail_url'], json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + config['mail_token'],
                 'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode()
    if raw.startswith(('event:', 'data:')):
        raw = next(line[6:] for line in raw.splitlines() if line.startswith('data: '))
    result = json.loads(raw)
    if 'error' in result or result.get('result', {}).get('isError'):
        raise RuntimeError('Mail provisioning failed (see private server log)')
    return json.loads(result['result']['content'][0]['text'])


def provision(args):
    os.umask(0o077)
    if LOCAL.exists():
        raise SystemExit('.local exists; refusing to overwrite state or credentials')
    LOCAL.mkdir(mode=0o700)
    for directory in ['bin', 'workspace', 'mail', 'clients', 'logs']:
        (LOCAL / directory).mkdir(mode=0o700)
    provenance = {}
    for label, source, version in [('br', args.br_bin, '0.7.6'),
                                  ('mcp-agent-mail', args.mail_bin, '0.3.39')]:
        source = Path(source).resolve(strict=True)
        check = subprocess.check_output([str(source), '--version'], text=True)
        if version not in check:
            raise SystemExit(f'{label}: expected version {version}')
        destination = LOCAL / 'bin' / label
        shutil.copy2(source, destination)
        destination.chmod(0o700)
        provenance[label] = {'version': version, 'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}
    private_json(LOCAL / 'binary-provenance.json', provenance)
    br = str(LOCAL / 'bin/br')
    workspace = str(LOCAL / 'workspace')
    subprocess.run([br, 'init', '--prefix', 'relay'], cwd=workspace, check=True, capture_output=True)
    subprocess.run([br, 'config', 'set', 'claim_exclusive', 'true'], cwd=workspace, check=True, capture_output=True)
    config = {'br_bin': br, 'workspace': workspace,
              'mail_url': f'http://127.0.0.1:{args.mail_port}/mcp/',
              'mail_token': secrets.token_urlsafe(32), 'mail_project': workspace,
              'agents': {name: {'token': secrets.token_urlsafe(32)} for name in ['codex', 'claude']}}
    private_json(LOCAL / 'config.json', config)
    private_json(LOCAL / 'runtime.json', {'http_port': args.port, 'mail_port': args.mail_port})
    print('Local files provisioned; next: python3 manage.py serve')


def serve():
    os.umask(0o077)
    config_path = LOCAL / 'config.json'
    config = json.loads(config_path.read_text())
    runtime = json.loads((LOCAL / 'runtime.json').read_text())
    mail_log = (LOCAL / 'logs/mail.log').open('a')
    relay_log = (LOCAL / 'logs/relay.log').open('a')
    env = {**os.environ, 'HTTP_BEARER_TOKEN': config['mail_token'],
           'HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED': 'false',
           'STORAGE_ROOT': str(LOCAL / 'mail'),
           'DATABASE_URL': 'sqlite://' + str(LOCAL / 'mail/storage.sqlite3')}
    children = []

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        mail = subprocess.Popen([str(LOCAL / 'bin/mcp-agent-mail'), 'serve', '--host', '127.0.0.1',
            '--port', str(runtime['mail_port']), '--no-tui', '--no-reuse-running'],
            cwd=LOCAL, env=env, stdout=mail_log, stderr=mail_log)
        children.append(mail)
        for _ in range(150):
            if mail.poll() is not None:
                raise RuntimeError('Mail exited; inspect .local/logs/mail.log')
            try:
                rpc(config, 'ensure_project', {'human_key': config['mail_project']})
                break
            except (OSError, RuntimeError):
                time.sleep(.1)
        else:
            raise RuntimeError('Mail readiness timed out')
        changed = False
        for agent, values in config['agents'].items():
            if 'mail_name' not in values:
                registered = rpc(config, 'register_agent', {'project_key': config['mail_project'],
                    'program': agent, 'model': 'session-managed', 'task_description': 'Local collaboration'})
                values['mail_name'] = registered['name']
                if registered.get('registration_token'):
                    values['mail_registration_token'] = registered['registration_token']
                changed = True
            client_path = LOCAL / 'clients' / (agent + '.json')
            if not client_path.exists():
                private_json(client_path, {'url': f'http://127.0.0.1:{runtime["http_port"]}',
                                          'token': values['token'], 'agent': agent})
        if changed:
            temporary = LOCAL / 'config.new'
            private_json(temporary, config)
            temporary.replace(config_path)
        relay = subprocess.Popen([sys.executable, str(ROOT / 'relay.py'), '--config', str(config_path),
            '--host', '127.0.0.1', '--port', str(runtime['http_port'])],
            cwd=ROOT, stdout=relay_log, stderr=relay_log)
        children.append(relay)
        private_json(LOCAL / 'supervisor.pid', {'pid': os.getpid()})
        print(f'Gateway http://127.0.0.1:{runtime["http_port"]}; private clients in .local/clients/', flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(.5)
        raise RuntimeError('A child exited; peer stopped. Inspect .local/logs/')
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        (LOCAL / 'supervisor.pid').unlink(missing_ok=True)
        mail_log.close()
        relay_log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--br-bin', required=True)
    init.add_argument('--mail-bin', required=True)
    init.add_argument('--port', type=int, default=18765)
    init.add_argument('--mail-port', type=int, default=18766)
    commands.add_parser('serve')
    commands.add_parser('stop')
    args = parser.parse_args()
    if args.command == 'init':
        provision(args)
    elif args.command == 'serve':
        serve()
    else:
        pid = json.loads((LOCAL / 'supervisor.pid').read_text())['pid']
        os.kill(pid, signal.SIGTERM)


if __name__ == '__main__':
    main()
