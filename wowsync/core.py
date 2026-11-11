"""Filesystem policy and three-way planning. Never evaluates Lua or game files."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import time

MAX_FILE = 128 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
CONFIG_LINE = re.compile(r'^SET\s+([^\s]+)\s+"(.*)"\s*$', re.I)
SKIP_SUFFIXES = {'.bak', '.old', '.tmp', '.zip', '.exe', '.dll', '.bat', '.cmd', '.ps1', '.lnk'}
IDENTITY = {'accountname', 'accountlist', 'agentuid', 'portal', 'textlocale', 'audiolocale'}
LOCAL_PREFIXES = ('gx', 'graphics', 'raid', 'sound_', 'voice', 'shadow', 'msaa', 'ssao',
                  'render', 'lod', 'terrain', 'ground', 'horizon', 'water', 'weather',
                  'volume', 'farclip', 'doodad', 'wmo', 'hw', 'engine', 'cache-', 'mouse')
LOCAL_KEYS = {'uiscale', 'useuiscale', 'vsync', 'maxfps', 'maxfpsbk', 'targetfps',
              'usemaxfps', 'lowlatencymode', 'videooptionsversion', 'physicslevel',
              'projectedtextures', 'refraction', 'sunshafts', 'outlineenginemode'}
SHARED_PREFIXES = ('nameplate', 'floatingcombattext', 'watchframe')
SHARED_KEYS = {'showtutorials', 'alwaysshowactionbars', 'spamfilter', 'removechatdelay',
               'showquesttrackingtooltips', 'autolootdefault', 'checkaddonversion'}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save_json(path: Path, value):
    atomic_write(path, json.dumps(value, indent=2).encode())


def load_json(path: Path, default):
    return json.loads(path.read_text('utf-8')) if path.exists() else default


def is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) &
                                             getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def safe_path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if (not parts or parts[0] not in ('Interface', 'WTF') or
            any(p in ('..', '.') or re.search(r'[<>:"\\|?*\x00-\x1f]', p) or p.endswith((' ', '.')) or
                re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.I)
                for p in parts) or
            PurePosixPath(relative).is_absolute()):
        raise ValueError('Invalid game path')
    current = root
    for part in parts:
        current = current / part
        if is_link(current):
            raise ValueError('Links and junctions are excluded')
    # Root is an already-resolved Store profile. Every appended component is
    # relative, non-traversing, and checked for reparse points above. Resolving
    # every path again is prohibitively expensive for large addon collections.
    return current


def allowed(relative: str) -> bool:
    path = PurePosixPath(relative)
    return (len(path.parts) >= 2 and path.parts[0] in ('Interface', 'WTF')
            and not any(p.startswith('.') for p in path.parts)
            and path.suffix.lower() not in SKIP_SUFFIXES)


def config_values(data: bytes) -> dict[str, str]:
    values = {}
    for line in data.decode('utf-8-sig').splitlines():
        match = CONFIG_LINE.fullmatch(line)
        if match:
            key = match[1].lower()
            if key in values:
                raise ValueError('Duplicate Config.wtf key; review manually')
            values[key] = match[2]
    return values


def protected(key: str) -> bool:
    if key in IDENTITY or key in LOCAL_KEYS or key.startswith(LOCAL_PREFIXES):
        return True
    # New or unknown CVars default to local. A new display setting cannot silently
    # enter bulk sync just because this release does not recognize its name yet.
    return key not in SHARED_KEYS and not key.startswith(SHARED_PREFIXES)


def merge_config(data: bytes, changes: dict[str, str]) -> bytes:
    text = data.decode('utf-8-sig')
    ending = '\r\n' if '\r\n' in text else '\n'
    lines, seen = [], set()
    for line in text.splitlines():
        match = CONFIG_LINE.fullmatch(line)
        key = match[1].lower() if match else None
        if key in changes:
            line = f'SET {match[1]} "{changes[key]}"'
            seen.add(key)
        lines.append(line)
    lines.extend(f'SET {key} "{value}"' for key, value in changes.items() if key not in seen)
    return (ending.join(lines) + ending).encode('utf-8')


def game_running() -> bool:
    if os.name != 'nt':
        result = subprocess.run(['ps', '-A', '-o', 'comm='], capture_output=True, text=True, check=True)
        return any(re.fullmatch(r'wow(?:classic|b|t|_retail_)?(?:\.exe)?', Path(p).name, re.I)
                   for p in result.stdout.splitlines())
    result = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return bool(re.search(r'^"Wow[^"\r\n]*\.exe"', result.stdout, re.I | re.M))


class Store:
    def __init__(self, root: Path, state: Path):
        self.root, self.state = root.resolve(), state.resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings = load_json(state / 'settings.json', {
            'device': os.environ.get('COMPUTERNAME', 'WoW machine'),
            'token': secrets.token_urlsafe(32), 'peers': []})
        save_json(state / 'settings.json', self.settings)
        self.plans = {}

    def profiles(self):
        if not self.root.is_dir():
            return []
        priority = {'_retail_': 0, '_forever_': 1, '_classic_': 2}
        names = (p.name for p in self.root.iterdir() if p.is_dir() and not is_link(p)
                 and p.name.startswith('_') and p.name.endswith('_'))
        return sorted(names, key=lambda n: (priority.get(n, 3), n))

    def profile(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'_[^/\\:]+_', name):
            raise ValueError('Unknown game version')
        path = self.root / name
        if not path.is_dir():
            raise ValueError('Unknown game version')
        if is_link(path):
            raise ValueError('Linked game versions are excluded')
        return path

    def read(self, profile, relative):
        return self._read_at(self.profile(profile), relative)

    @staticmethod
    def _read_at(root, relative):
        if not allowed(relative):
            raise ValueError('Excluded file type')
        path = safe_path(root, relative)
        with path.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_FILE:
                raise ValueError('File exceeds 128 MiB limit')
            data = stream.read(size + 1)
        if len(data) != size:
            raise ValueError('File changed while reading. Compare again.')
        return data

    def manifest(self, profile):
        root = self.profile(profile)
        files, skipped, total = {}, 0, 0
        for folder in ('Interface', 'WTF'):
            start = root / folder
            if not start.exists():
                continue
            safe_path(root, folder)
            for parent, dirs, names in os.walk(start, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and
                                 not is_link(Path(parent) / d))
                for name in sorted(names):
                    relative = (Path(parent) / name).relative_to(root).as_posix()
                    if not allowed(relative):
                        skipped += 1
                        continue
                    if is_link(Path(parent) / name):
                        skipped += 1
                        continue
                    data = self._read_at(root, relative)
                    total += len(data)
                    if total > MAX_TOTAL or len(files) >= 100000:
                        raise ValueError('Installation exceeds snapshot limits')
                    entry = {'hash': digest(data), 'size': len(data)}
                    if relative.lower() == 'wtf/config.wtf':
                        try:
                            entry['config'] = config_values(data)
                        except (ValueError, UnicodeError):
                            entry['configError'] = 'Config.wtf has duplicate keys or unsupported text encoding. Review this file manually; other files can still sync.'
                    files[relative] = entry
        return {'files': files, 'skipped': skipped, 'device': self.settings['device']}

    def baseline_path(self, peer, profile):
        return self.state / 'baselines' / (digest(f'{peer}\0{profile}'.encode()) + '.json')

    def plan(self, peer, profile, remote):
        if not isinstance(remote, dict) or not isinstance(remote.get('files'), dict) or len(remote['files']) > 100000:
            raise ValueError('Invalid peer manifest')
        seen, total = set(), 0
        for path, info in remote['files'].items():
            if not isinstance(path, str) or path.casefold() in seen or not isinstance(info, dict):
                raise ValueError('Ambiguous peer file paths')
            seen.add(path.casefold())
            safe_path(self.profile(profile), path)
            if not allowed(path) or not re.fullmatch(r'[a-f0-9]{64}', str(info.get('hash', ''))):
                raise ValueError('Invalid peer file metadata')
            size = info.get('size')
            if not isinstance(size, int) or not 0 <= size <= MAX_FILE:
                raise ValueError('Invalid peer file size')
            total += size
            if total > MAX_TOTAL:
                raise ValueError('Peer snapshot exceeds size limit')
            if path.lower() == 'wtf/config.wtf':
                config = info.get('config')
                if info.get('configError'):
                    if not isinstance(info['configError'], str):
                        raise ValueError('Invalid config error metadata')
                elif not isinstance(config, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in config.items()):
                    raise ValueError('Invalid config metadata')
        local = self.manifest(profile)
        local_cases = {p.casefold(): p for p in local['files']}
        if any(p.casefold() in local_cases and local_cases[p.casefold()] != p for p in remote['files']):
            raise ValueError('Peer path capitalization differs. Match folder and file capitalization before syncing.')
        baseline = load_json(self.baseline_path(peer, profile), {})
        entries = []
        for path in sorted(set(local['files']) | set(remote['files'])):
            if not allowed(path):
                raise ValueError('Peer sent excluded path')
            safe_path(self.profile(profile), path)
            left, right = local['files'].get(path), remote['files'].get(path)
            if path.lower() == 'wtf/config.wtf':
                if (left or {}).get('configError') or (right or {}).get('configError'):
                    entries.append({'id': path, 'path': path, 'status': 'manual-review',
                                    'error': 'Config.wtf has duplicate keys or unsupported text encoding. Review it manually; this file cannot be synced safely.',
                                    'selectable': False, 'size': (right or left)['size'],
                                    'local': (left or {}).get('hash'), 'remote': (right or {}).get('hash')})
                    continue
                lv, rv = (left or {}).get('config', {}), (right or {}).get('config', {})
                for key in sorted(set(lv) | set(rv)):
                    if not re.fullmatch(r'[a-z0-9_-]+', key) or ('\n' in rv.get(key, '') or '\r' in rv.get(key, '') or '"' in rv.get(key, '')):
                        raise ValueError('Invalid config key or value')
                    identity = key in IDENTITY
                    status = self.classify(lv.get(key), rv.get(key), baseline.get(path + '#' + key))
                    if protected(key) and status != 'same':
                        status = 'private' if identity else 'machine-local'
                    entries.append({'id': path + '#' + key, 'path': path, 'key': key,
                                    'local': None if identity else lv.get(key),
                                    'remote': None if identity else rv.get(key),
                                    'status': status, 'selectable': not identity and key in rv})
                continue
            lh, rh = (left or {}).get('hash'), (right or {}).get('hash')
            entries.append({'id': path, 'path': path, 'status': self.classify(lh, rh, baseline.get(path)),
                            'local': lh, 'remote': rh, 'size': (right or left)['size'],
                            'selectable': right is not None})
        plan_id = secrets.token_urlsafe(18)
        plan = {'id': plan_id, 'peer': peer, 'profile': profile, 'created': time.time(),
                'entries': entries, 'local': local, 'remote': remote}
        self.plans = {k: v for k, v in self.plans.items() if time.time() - v['created'] < 1800}
        self.plans[plan_id] = plan
        return {'id': plan_id, 'entries': entries, 'skipped': local['skipped'],
                'remoteDevice': remote.get('device', 'Peer')}

    @staticmethod
    def classify(local, remote, baseline):
        if local == remote:
            return 'same'
        if remote is None:
            return 'local-only'
        if baseline is None:
            return 'incoming' if local is None else 'conflict'
        if remote == baseline:
            return 'local-only'
        if local == baseline:
            return 'incoming'
        return 'conflict'

    def get_plan(self, plan_id):
        plan = self.plans.get(plan_id)
        if not plan or time.time() - plan['created'] > 1800:
            raise ValueError('Preview expired. Compare again.')
        return plan

    def diff(self, plan_id, entry_id, fetch):
        plan = self.get_plan(plan_id)
        entry = next(e for e in plan['entries'] if e['id'] == entry_id)
        if entry.get('error'):
            return entry['error']
        if 'key' in entry:
            if entry['status'] == 'private':
                return 'Account-specific value is hidden and never synced.'
            return f"This machine: {entry['local']!r}\nPeer: {entry['remote']!r}"
        left = self.read(plan['profile'], entry['path']) if entry['local'] else b''
        right = fetch(entry['path']) if entry['remote'] else b''
        if len(left) + len(right) > 512 * 1024 or b'\x00' in left + right:
            return 'Binary or large file. Compare SHA-256 hashes and size in the preview.'
        try:
            return ''.join(difflib.unified_diff(left.decode('utf-8-sig').splitlines(True),
                           right.decode('utf-8-sig').splitlines(True), fromfile='this machine', tofile='peer'))[:120000]
        except UnicodeError:
            return 'Binary or non-UTF-8 file; text delta is unavailable.'

    def apply(self, plan_id, selected, fetch, is_running=game_running):
        if is_running():
            raise ValueError('Close World of Warcraft before syncing.')
        plan = self.get_plan(plan_id)
        if not isinstance(selected, list) or len(selected) != len(set(selected)):
            raise ValueError('Invalid selection')
        entries = {e['id']: e for e in plan['entries']}
        if any(i not in entries or not entries[i]['selectable'] for i in selected):
            raise ValueError('Selection contains an unavailable item')
        chosen = [entries[i] for i in selected]
        if not chosen:
            raise ValueError('Select at least one item')
        paths = sorted({e['path'] for e in chosen})
        payloads = {}
        # Download and validate the complete selection before any write.
        for path in paths:
            source = fetch(path)
            if digest(source) != plan['remote']['files'][path]['hash']:
                raise ValueError('Peer changed since preview. Compare again.')
            target = safe_path(self.profile(plan['profile']), path)
            old = target.read_bytes() if target.exists() else None
            expected = plan['local']['files'].get(path, {}).get('hash')
            if (digest(old) if old is not None else None) != expected:
                raise ValueError('Local file changed since preview. Compare again.')
            if path.lower() == 'wtf/config.wtf':
                rv = config_values(source)
                if rv != plan['remote']['files'][path]['config']:
                    raise ValueError('Peer config does not match its preview metadata. Compare again.')
                source = merge_config(old or b'', {e['key']: rv[e['key']] for e in chosen if e['path'] == path})
            payloads[path] = (old, source)
        if is_running():
            raise ValueError('WoW started during preview. Close it and compare again.')
        batch = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4)
        backup = self.state / 'backups' / batch
        journal = {'id': batch, 'profile': plan['profile'], 'created': time.time(), 'files': {}, 'status': 'prepared'}
        for path, (old, new) in payloads.items():
            journal['files'][path] = {'before': digest(old) if old is not None else None, 'after': digest(new)}
            if old is not None:
                atomic_write(backup / 'files' / path, old)
        save_json(backup / 'journal.json', journal)
        written = []
        try:
            for path, (old, new) in payloads.items():
                target = safe_path(self.profile(plan['profile']), path)
                current = target.read_bytes() if target.exists() else None
                if current != old:
                    raise ValueError('Local file changed during sync')
                atomic_write(target, new)
                written.append(path)
        except Exception:
            for path in reversed(written):
                target = safe_path(self.profile(plan['profile']), path)
                old, new = payloads[path]
                if target.read_bytes() != new:
                    continue
                if old is None:
                    target.unlink()
                else:
                    atomic_write(target, old)
            journal['status'] = 'failed'
            save_json(backup / 'journal.json', journal)
            raise
        journal['status'] = 'complete'
        save_json(backup / 'journal.json', journal)
        baseline_path = self.baseline_path(plan['peer'], plan['profile'])
        baseline = load_json(baseline_path, {})
        for e in plan['entries']:
            if e['status'] == 'same' or e['id'] in selected:
                # Identity values never enter the baseline.
                if e['status'] != 'private' and e.get('key') not in IDENTITY:
                    baseline[e['id']] = e['remote']
        save_json(baseline_path, baseline)
        del self.plans[plan_id]
        return {'backup': batch, 'files': len(paths)}

    def history(self):
        folder = self.state / 'backups'
        return [load_json(p, {}) for p in sorted(folder.glob('*/journal.json'), reverse=True)][:100]

    def restore(self, batch, is_running=game_running):
        if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{8}', batch):
            raise ValueError('Invalid backup ID')
        if is_running():
            raise ValueError('Close World of Warcraft before restoring.')
        backup = self.state / 'backups' / batch
        journal = load_json(backup / 'journal.json', None)
        if not journal or journal['status'] not in ('complete', 'prepared', 'failed'):
            raise ValueError('Backup is not available for restore')
        root = self.profile(journal['profile'])
        payloads = {}
        for path, info in journal['files'].items():
            target = safe_path(root, path)
            current = digest(target.read_bytes()) if target.exists() else None
            if current == info['before']:
                continue
            if current != info['after']:
                raise ValueError('Files changed after sync. Restore blocked to preserve newer edits.')
            old = (backup / 'files' / path).read_bytes() if info['before'] is not None else None
            if old is not None and digest(old) != info['before']:
                raise ValueError('Backup integrity check failed')
            payloads[path] = old
        for path, old in payloads.items():
            target = safe_path(root, path)
            if old is None:
                target.unlink()
            else:
                atomic_write(target, old)
        journal['status'] = 'restored'
        save_json(backup / 'journal.json', journal)
        return {'files': len(payloads)}
