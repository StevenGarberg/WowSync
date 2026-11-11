import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from wowsync.core import Store, config_values, safe_path, digest, protected
from wowsync.server import make_handler, peer_request


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name in ('a', 'b'):
            (root / name / '_retail_').mkdir(parents=True)
            (root / name / '_forever_').mkdir()
        self.a = Store(root / 'a', root / 'state-a')
        self.b = Store(root / 'b', root / 'state-b')

    def put(self, store, path, text, profile='_retail_'):
        target = store.profile(profile) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())

    def plan(self):
        return self.a.plan('b', '_retail_', self.b.manifest('_retail_'))

    def apply(self, plan, selected):
        return self.a.apply(plan['id'], selected, lambda p: self.b.read('_retail_', p), is_running=lambda: False)


class SyncTests(Fixture):
    def test_new_file_sync_and_restore(self):
        self.put(self.b, 'Interface/AddOns/Test/Test.lua', 'print("hello")')
        plan = self.plan()
        self.assertEqual(plan['entries'][0]['status'], 'incoming')
        result = self.apply(plan, [plan['entries'][0]['id']])
        self.assertEqual(result['files'], 1)
        self.assertEqual(self.a.read('_retail_', 'Interface/AddOns/Test/Test.lua'), b'print("hello")')
        self.a.restore(result['backup'], is_running=lambda: False)
        self.assertFalse((self.a.profile('_retail_') / 'Interface/AddOns/Test/Test.lua').exists())

    def test_existing_different_files_are_conflicts(self):
        self.put(self.a, 'WTF/bindings-cache.wtf', 'local')
        self.put(self.b, 'WTF/bindings-cache.wtf', 'remote')
        plan = self.plan()
        self.assertEqual(plan['entries'][0]['status'], 'conflict')
        result = self.apply(plan, ['WTF/bindings-cache.wtf'])
        self.a.restore(result['backup'], is_running=lambda: False)
        self.assertEqual(self.a.read('_retail_', 'WTF/bindings-cache.wtf'), b'local')

    def test_three_way_remote_local_and_concurrent_edits(self):
        path = 'WTF/Account/EXAMPLE/SavedVariables/Test.lua'
        self.put(self.b, path, 'initial')
        self.apply(self.plan(), [path])
        self.put(self.b, path, 'remote edit')
        self.assertEqual(self.plan()['entries'][0]['status'], 'incoming')
        self.put(self.a, path, 'local edit')
        self.assertEqual(self.plan()['entries'][0]['status'], 'conflict')
        self.put(self.b, path, 'initial')
        self.assertEqual(self.plan()['entries'][0]['status'], 'local-only')

    def test_config_granularity_preserves_display_and_identity(self):
        self.put(self.a, 'WTF/Config.wtf', 'SET gxWindowedResolution "1920x1080"\nSET accountName "LOCAL"\nSET showTutorials "1"\n')
        self.put(self.b, 'WTF/Config.wtf', 'SET gxWindowedResolution "3840x2160"\nSET accountName "REMOTE"\nSET showTutorials "0"\n')
        plan = self.plan()
        entries = {e['key']:e for e in plan['entries']}
        self.assertEqual(entries['gxwindowedresolution']['status'], 'machine-local')
        self.assertFalse(entries['accountname']['selectable'])
        self.assertIsNone(entries['accountname']['remote'])
        self.apply(plan, ['WTF/Config.wtf#showtutorials'])
        values = config_values(self.a.read('_retail_', 'WTF/Config.wtf'))
        self.assertEqual(values['gxwindowedresolution'], '1920x1080')
        self.assertEqual(values['accountname'], 'LOCAL')
        self.assertEqual(values['showtutorials'], '0')

    def test_explicit_display_override(self):
        self.put(self.a, 'WTF/Config.wtf', 'SET uiScale "1"\n')
        self.put(self.b, 'WTF/Config.wtf', 'SET uiScale "0.8"\n')
        self.apply(self.plan(), ['WTF/Config.wtf#uiscale'])
        self.assertEqual(config_values(self.a.read('_retail_', 'WTF/Config.wtf'))['uiscale'], '0.8')

    def test_unknown_config_settings_default_to_machine_local(self):
        self.assertTrue(protected('futuredisplaysetting'))
        self.assertFalse(protected('nameplateglobalscale'))

    def test_large_addon_database_is_supported(self):
        path = self.b.profile('_retail_') / 'Interface/AddOns/Example/Data.lua'
        path.parent.mkdir(parents=True)
        with path.open('wb') as stream:
            stream.write(b'x' * (33 * 1024 * 1024))
        manifest = self.b.manifest('_retail_')
        self.assertEqual(manifest['files']['Interface/AddOns/Example/Data.lua']['size'],33 * 1024 * 1024)

    def test_interrupted_batch_is_recoverable(self):
        self.put(self.b, 'WTF/a.lua', 'source')
        result = self.apply(self.plan(), ['WTF/a.lua'])
        path = self.a.state / 'backups' / result['backup'] / 'journal.json'
        journal = json.loads(path.read_text())
        journal['status'] = 'prepared'
        path.write_text(json.dumps(journal))
        self.a.restore(result['backup'], is_running=lambda:False)
        self.assertFalse((self.a.profile('_retail_') / 'WTF/a.lua').exists())

    def test_identity_selection_cannot_be_forged(self):
        self.put(self.b, 'WTF/Config.wtf', 'SET accountName "REMOTE"\n')
        with self.assertRaises(ValueError):
            self.apply(self.plan(), ['WTF/Config.wtf#accountname'])

    def test_local_changes_invalidate_preview_before_any_write(self):
        self.put(self.b, 'WTF/a.lua', 'source')
        self.put(self.b, 'WTF/b.lua', 'source')
        plan = self.plan()
        self.put(self.a, 'WTF/b.lua', 'new local edit')
        with self.assertRaisesRegex(ValueError, 'Local file changed'):
            self.apply(plan, ['WTF/a.lua', 'WTF/b.lua'])
        self.assertFalse((self.a.profile('_retail_') / 'WTF/a.lua').exists())

    def test_remote_changes_invalidate_preview(self):
        self.put(self.b, 'WTF/a.lua', 'before')
        plan = self.plan()
        self.put(self.b, 'WTF/a.lua', 'after')
        with self.assertRaisesRegex(ValueError, 'Peer changed'):
            self.apply(plan, ['WTF/a.lua'])

    def test_game_running_blocks_writes(self):
        self.put(self.b, 'WTF/a.lua', 'before')
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, 'Close World'):
            self.a.apply(plan['id'], ['WTF/a.lua'], lambda p: b'before', is_running=lambda: True)

    def test_restore_preserves_edits_made_after_sync(self):
        self.put(self.b, 'WTF/a.lua', 'source')
        result = self.apply(self.plan(), ['WTF/a.lua'])
        self.put(self.a, 'WTF/a.lua', 'new edit')
        with self.assertRaisesRegex(ValueError, 'changed after sync'):
            self.a.restore(result['backup'], is_running=lambda: False)
        self.assertEqual(self.a.read('_retail_', 'WTF/a.lua'), b'new edit')

    def test_deletions_never_propagate(self):
        self.put(self.a, 'WTF/a.lua', 'local')
        entry = self.plan()['entries'][0]
        self.assertEqual(entry['status'], 'local-only')
        self.assertFalse(entry['selectable'])

    def test_versions_are_isolated(self):
        self.put(self.b, 'WTF/a.lua', 'retail')
        self.put(self.b, 'WTF/a.lua', 'forever', '_forever_')
        self.apply(self.plan(), ['WTF/a.lua'])
        self.assertFalse((self.a.profile('_forever_') / 'WTF/a.lua').exists())

    def test_path_traversal_and_link_escape(self):
        for name in ('../secret', 'WTF/../../secret', 'WTF/evil:stream', 'WTF/\\escape', '/WTF/test',
                     'WTF/NUL', 'WTF/a.', 'WTF/test?/x'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_path(self.a.profile('_retail_'), name)

    def test_excluded_executables_and_archives(self):
        for name in ('WTF/backup.zip', 'Interface/danger.exe', 'WTF/a.bak'):
            self.put(self.a, name, 'excluded')
        self.assertEqual(self.a.manifest('_retail_')['files'], {})

    def test_duplicate_case_paths_rejected(self):
        info = {'hash': digest(b'a'), 'size': 1}
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            self.a.plan('b', '_retail_', {'files': {'WTF/a.lua':info, 'WTF/A.lua':info}})

    def test_text_delta(self):
        self.put(self.a, 'WTF/a.lua', 'old\n')
        self.put(self.b, 'WTF/a.lua', 'new\n')
        plan = self.plan()
        delta = self.a.diff(plan['id'], 'WTF/a.lua', lambda p:self.b.read('_retail_', p))
        self.assertIn('-old', delta)
        self.assertIn('+new', delta)

    def test_duplicate_config_keys_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            config_values(b'SET test "1"\nSET TEST "2"\n')

    def test_partial_write_failure_rolls_back(self):
        self.put(self.b, 'WTF/a.lua', 'a')
        self.put(self.b, 'WTF/b.lua', 'b')
        plan = self.plan()
        from wowsync.core import atomic_write as original
        def fail_second(path, data):
            if path == self.a.profile('_retail_') / 'WTF/b.lua':
                raise OSError('simulated disk error')
            return original(path, data)
        with patch('wowsync.core.atomic_write', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.apply(plan, ['WTF/a.lua', 'WTF/b.lua'])
        self.assertFalse((self.a.profile('_retail_') / 'WTF/a.lua').exists())
        self.assertEqual(self.a.history()[0]['status'], 'failed')


class HttpTests(Fixture):
    # Use two real HTTP services and synthetic installations, with no live game access.
    def setUp(self):
        super().setUp()
        self.running = patch('wowsync.server.game_running', return_value=False)
        self.running.start()
        self.addCleanup(self.running.stop)
        self.servers = []
        for store in (self.a, self.b):
            server = ThreadingHTTPServer(('127.0.0.1',0), make_handler(store, 'local-session'))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append(server)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.url = f'http://127.0.0.1:{self.servers[0].server_port}'
        self.peer_url = f'http://127.0.0.1:{self.servers[1].server_port}'

    def request(self, path, data=None, headers=None):
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                          headers=headers or {'X-WowSync-Session':'local-session'})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_http_authentication(self):
        with self.assertRaises(HTTPError) as error:
            self.request('/api/status', headers={'X-WowSync-Session':'wrong'})
        self.assertEqual(error.exception.code,401)
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.request('/peer/status')
        error.exception.close()

    def test_full_http_pair_compare_diff_apply_restore(self):
        self.put(self.b, 'WTF/a.lua', 'incoming\n')
        self.request('/api/peers', {'url':self.peer_url, 'token':self.b.settings['token'], 'name':'Cube test'})
        status = self.request('/api/status')
        self.assertNotIn('token', status['peers'][0])
        plan = self.request('/api/compare', {'peer':status['peers'][0]['id'], 'profile':'_retail_'})
        delta = self.request('/api/diff', {'plan':plan['id'], 'entry':'WTF/a.lua'})
        self.assertIn('+incoming',delta['diff'])
        with patch('wowsync.core.game_running', return_value=False):
            result = self.request('/api/apply', {'plan':plan['id'], 'selected':['WTF/a.lua']})
            self.request('/api/restore', {'backup':result['backup']})
        self.assertFalse((self.a.profile('_retail_') / 'WTF/a.lua').exists())

    def test_pairing_token_rotation_revokes_reads(self):
        peer = {'url':self.url, 'token':self.a.settings['token']}
        self.assertEqual(peer_request(peer, '/peer/status')['protocol'],1)
        self.request('/api/rotate-token', {})
        with self.assertRaises(HTTPError) as error:
            peer_request(peer, '/peer/status')
        error.exception.close()

    def test_peer_cannot_write_or_manage(self):
        token = {'Authorization':'Bearer ' + self.a.settings['token']}
        with self.assertRaises(HTTPError) as error:
            self.request('/api/token', {}, headers=token)
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.request('/peer/file', {}, headers=token)
        error.exception.close()


if __name__ == '__main__':
    unittest.main()
