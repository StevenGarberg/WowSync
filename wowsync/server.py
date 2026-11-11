"""Small authenticated HTTP peer service and loopback-only management API."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
import webbrowser

from .core import Store, MAX_FILE, game_running, save_json


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def peer_request(peer, endpoint, params=None, binary=False):
    url = peer['url'] + endpoint + ('?' + urlencode(params) if params else '')
    request = Request(url, headers={'Authorization': 'Bearer ' + peer['token']})
    # Never forward pairing credentials through redirects or environment proxies.
    with build_opener(NoRedirect(), ProxyHandler({})).open(request, timeout=300 if endpoint == '/peer/manifest' else 30) as response:
        data = response.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError('Peer response exceeds size limit')
    return data if binary else json.loads(data)


def make_handler(store, session):
    lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'WoWSync/0.1'

        def log_message(self, format, *args):
            # URLs can contain account/character paths; do not log request details.
            pass

        def send(self, value, status=200, content_type='application/json'):
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def authenticated(self, peer=False):
            if peer:
                actual = self.headers.get('Authorization', '')
                expected = 'Bearer ' + store.settings['token']
            else:
                if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    return False
                actual = self.headers.get('X-WowSync-Session', '')
                expected = session
            return secrets.compare_digest(actual.encode(), expected.encode())

        def do_GET(self):
            self.dispatch('GET')

        def do_POST(self):
            self.dispatch('POST')

        def dispatch(self, method):
            try:
                route = urlsplit(self.path)
                path, query = route.path, parse_qs(route.query)
                if path in ('/', '/app.js', '/style.css') and method == 'GET':
                    file = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                    mime = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8', '/style.css': 'text/css'}[path]
                    self.send((Path(__file__).parent / 'static' / file).read_bytes(), content_type=mime)
                    return
                if path.startswith('/peer/'):
                    if method != 'GET' or not self.authenticated(peer=True):
                        self.send({'error': 'Unauthorized'}, 401)
                        return
                    if not lock.acquire(blocking=False):
                        self.send({'error': 'Source is busy; retry when its current operation finishes.'}, 409)
                        return
                    try:
                        if game_running():
                            raise ValueError('World of Warcraft is running on the source machine. Close it before comparing or syncing.')
                        if path == '/peer/status':
                            self.send({'device': store.settings['device'], 'profiles': store.profiles(), 'protocol': 1})
                        elif path == '/peer/manifest':
                            self.send(store.manifest(query['profile'][0]))
                        elif path == '/peer/file':
                            self.send(store.read(query['profile'][0], query['path'][0]), content_type='application/octet-stream')
                        else:
                            self.send({'error': 'Not found'}, 404)
                    finally:
                        lock.release()
                    return
                if not path.startswith('/api/') or not self.authenticated():
                    self.send({'error': 'Open the local management URL printed in the terminal.'}, 401)
                    return
                data = {}
                if method == 'POST':
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 1024 * 1024:
                        raise ValueError('Invalid request size')
                    data = json.loads(self.rfile.read(length))
                with lock:
                    result = self.api(method, path, query, data)
                self.send(result)
            except (HTTPError, URLError, TimeoutError):
                self.send({'error': 'Peer unavailable or pairing token rejected. Check its address, token, and that WoW is closed.'}, 502)
            except (ValueError, KeyError, StopIteration, OSError, TypeError) as error:
                self.send({'error': str(error)[:500] or 'Invalid request'}, 400)
            except Exception:
                self.send({'error': 'Unexpected error. No success was recorded; review backup history before retrying.'}, 500)

        def api(self, method, path, query, data):
            if method == 'GET' and path == '/api/status':
                return {'device': store.settings['device'], 'install': str(store.root),
                        'profiles': store.profiles(), 'running': game_running(),
                        'peers': [{k: p[k] for k in ('id', 'name', 'url', 'profiles')} for p in store.settings['peers']],
                        'state': str(store.state)}
            if method == 'GET' and path == '/api/history':
                return store.history()
            if method == 'POST' and path == '/api/token':
                return {'token': store.settings['token']}
            if method == 'POST' and path == '/api/rotate-token':
                store.settings['token'] = secrets.token_urlsafe(32)
                save_json(store.state / 'settings.json', store.settings)
                return {'ok': True}
            if method == 'POST' and path == '/api/peers':
                url = str(data['url']).strip().rstrip('/')
                parsed = urlsplit(url)
                if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or
                        parsed.password or parsed.path or parsed.query or parsed.fragment):
                    raise ValueError('Use a peer origin such as http://cube:8765')
                token = str(data['token']).strip()
                if len(token) < 20 or len(token) > 200 or '\r' in token or '\n' in token:
                    raise ValueError('Invalid pairing token')
                if token == store.settings['token']:
                    raise ValueError('That is this machine’s token. Use the other machine’s token.')
                peer = {'id': secrets.token_hex(8), 'url': url, 'token': token}
                status = peer_request(peer, '/peer/status')
                if status.get('protocol') != 1:
                    raise ValueError('Incompatible peer version')
                peer.update(name=str(data.get('name') or status['device'])[:100], profiles=status['profiles'])
                store.settings['peers'].append(peer)
                save_json(store.state / 'settings.json', store.settings)
                return {'ok': True}
            if method == 'POST' and path == '/api/remove-peer':
                store.settings['peers'] = [p for p in store.settings['peers'] if p['id'] != data['peer']]
                save_json(store.state / 'settings.json', store.settings)
                return {'ok': True}
            if method == 'POST' and path == '/api/restore':
                return store.restore(data['backup'], is_running=game_running)
            if method == 'POST' and path == '/api/compare':
                if game_running():
                    raise ValueError('Close World of Warcraft before comparing.')
                peer = self.peer(data['peer'])
                remote = peer_request(peer, '/peer/manifest', {'profile': data['profile']})
                return store.plan(peer['id'], data['profile'], remote)
            if method == 'POST' and path in ('/api/apply', '/api/diff'):
                plan = store.get_plan(data['plan'])
                peer = self.peer(plan['peer'])
                def fetch(relative):
                    return peer_request(peer, '/peer/file', {'profile': plan['profile'], 'path': relative}, binary=True)
                if path == '/api/diff':
                    return {'diff': store.diff(data['plan'], data['entry'], fetch)}
                return store.apply(data['plan'], data['selected'], fetch, is_running=game_running)
            raise ValueError('Unknown API request')

        @staticmethod
        def peer(peer_id):
            return next(p for p in store.settings['peers'] if p['id'] == peer_id)

    return Handler


def main():
    parser = argparse.ArgumentParser(description='WoWSync local network settings sync')
    parser.add_argument('--install', type=Path, default=Path(r'D:\Games\World of Warcraft'))
    parser.add_argument('--state', type=Path, default=Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share'))) / 'WoWSync')
    parser.add_argument('--listen', default='127.0.0.1', help='Use 0.0.0.0 to enable LAN/Tailscale peers')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true', help='Open local management UI')
    args = parser.parse_args()
    if not args.install.is_dir():
        parser.error('Installation directory does not exist; pass --install')
    store = Store(args.install, args.state)
    session = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer((args.listen, args.port), make_handler(store, session))
    server.daemon_threads = True
    host = '127.0.0.1' if args.listen in ('0.0.0.0', '127.0.0.1') else args.listen
    url = f'http://{host}:{server.server_port}/#session={session}'
    print(f'WoWSync running. Local management URL (keep private):\n{url}', flush=True)
    print(f'Private state and backups: {args.state}\nPress Ctrl+C to stop.', flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
