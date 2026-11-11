"""Command-line pull for a receiving machine, using the same planner and backups as the UI."""
from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from .core import Store, digest, game_running, save_json
from .server import peer_request


def selection(entries, prefer_source=False):
    statuses = {'incoming', 'conflict', 'local-only'} if prefer_source else {'incoming'}
    return [entry['id'] for entry in entries if entry['selectable'] and entry['status'] in statuses]


def pull(store, source, profile, *, apply=False, prefer_source=False):
    if game_running():
        raise ValueError('Close World of Warcraft before syncing.')
    status = peer_request(source, '/peer/status')
    if status.get('protocol') != 1 or profile not in status.get('profiles', []):
        raise ValueError('Source does not support the requested version or protocol')
    store.profile(profile)
    remote = peer_request(source, '/peer/manifest', {'profile': profile})
    plan = store.plan(source['id'], profile, remote)
    chosen = selection(plan['entries'], prefer_source)
    report = {'profile': profile, 'source': source['url'], 'destination': str(store.root),
              'statuses': dict(Counter(e['status'] for e in plan['entries'])),
              'selected': len(chosen), 'applied': False}
    if apply and chosen:
        def fetch(path):
            return peer_request(source, '/peer/file', {'profile': profile, 'path': path}, binary=True)
        report.update(store.apply(plan['id'], chosen, fetch, is_running=game_running))
        report['applied'] = True
        # Keep the paired source available in the browser UI. Credentials stay in private state.
        peer = dict(source, name=status['device'], profiles=status['profiles'])
        store.settings['peers'] = [p for p in store.settings['peers'] if p['id'] != source['id']] + [peer]
        save_json(store.state / 'settings.json', store.settings)
    return report


def main():
    parser = argparse.ArgumentParser(description='Preview or pull game settings onto this machine')
    parser.add_argument('--install', type=Path, required=True)
    parser.add_argument('--source', required=True, help='Trusted source origin, e.g. http://desktop:8765')
    parser.add_argument('--token-file', type=Path, required=True, help='Private text file containing the source pairing token')
    parser.add_argument('--profile', required=True, help='Version directory such as _forever_')
    parser.add_argument('--state', type=Path, default=Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share'))) / 'WoWSync')
    parser.add_argument('--prefer-source', action='store_true', help='Also select source versions of conflicting files and local edits; machine-local config stays local')
    parser.add_argument('--apply', action='store_true', help='Write selected changes after backup; without this flag only preview')
    parser.add_argument('--report', type=Path, help='Write a summary JSON report outside the repository')
    args = parser.parse_args()
    url = args.source.strip().rstrip('/')
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or
            parsed.password or parsed.path or parsed.query or parsed.fragment):
        parser.error('Source must be an HTTP(S) origin without a path or credentials')
    token = args.token_file.read_text('utf-8').strip()
    if len(token) < 20 or len(token) > 200 or '\r' in token or '\n' in token:
        parser.error('Invalid source token file')
    store = Store(args.install, args.state)
    if secrets.compare_digest(store.settings['token'], token):
        parser.error('The source token belongs to this machine')
    source = {'id': digest(url.encode())[:16], 'url': url, 'token': token}
    try:
        print('Comparing source and destination. Keep WoW closed...', flush=True)
        report = pull(store, source, args.profile, apply=args.apply, prefer_source=args.prefer_source)
        if args.report:
            save_json(args.report, report)
        for key in ('profile', 'statuses', 'selected', 'applied', 'files', 'backup'):
            if key in report:
                print(f'{key}: {report[key]}', flush=True)
    except Exception as error:
        parser.exit(1, f'Pull failed: {error}\n')


if __name__ == '__main__':
    main()
