"""Disposable two-device demo. Never reads or writes an actual game installation."""
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wowsync.core import Store, save_json
from wowsync.server import make_handler


def main():
    with tempfile.TemporaryDirectory(prefix='wowsync-demo-') as folder:
        root = Path(folder)
        stores = []
        for name in ('Desktop', 'Cube'):
            game = root / name / '_retail_'
            (game / 'WTF').mkdir(parents=True)
            (game / 'WTF/Config.wtf').write_text('SET gxWindowedResolution "' +
                ('2560x1440' if name == 'Desktop' else '1920x1080') + '"\nSET showTutorials "' +
                ('1' if name == 'Desktop' else '0') + '"\n')
            addon = game / 'Interface/AddOns/QuestHelper/QuestHelper.lua'
            addon.parent.mkdir(parents=True)
            addon.write_text('-- Quest helper\nlocal enabled = ' + ('false' if name == 'Desktop' else 'true') + '\n')
            store = Store(root / name, root / (name + '-state'))
            store.settings['device'] = name + ' (demo)'
            stores.append(store)
        addon = stores[1].profile('_retail_') / 'Interface/AddOns/BagTools/BagTools.lua'
        addon.parent.mkdir(parents=True)
        addon.write_text('-- New addon from Cube\nlocal sortBags = true\n')
        peer_server = ThreadingHTTPServer(('127.0.0.1', 8776), make_handler(stores[1], 'demo-peer-session'))
        stores[0].settings['peers'] = [{'id':'cube-demo', 'name':'Cube (demo)', 'url':'http://127.0.0.1:8776',
                                      'token':stores[1].settings['token'], 'profiles':['_retail_']}]
        save_json(stores[0].state / 'settings.json', stores[0].settings)
        server = ThreadingHTTPServer(('127.0.0.1',8775), make_handler(stores[0], 'demo-local-session'))
        threading.Thread(target=peer_server.serve_forever, daemon=True).start()
        print('Disposable demo: http://127.0.0.1:8775/#session=demo-local-session', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            peer_server.shutdown()
            peer_server.server_close()


if __name__ == '__main__':
    main()
