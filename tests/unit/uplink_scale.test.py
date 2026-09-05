#!/usr/bin/env python3
"""100-room/100k-event outage fixture using production reference queues.

The 72-hour outage is represented by source timestamps and a refusing fake
transport, not three days of wall-clock sleep. No running service is contacted.
"""
from pathlib import Path
import resource
import sys
import tempfile
import time
import urllib.error
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agents/uplink'))
import uplink


with tempfile.TemporaryDirectory(prefix='beepa-uplink-scale-') as directory:
    cfg = uplink.Config(dict(LOCAL_HS_URL='http://local.invalid', LOCAL_USER='@fixture:local',
        LOCAL_TOKEN='fixture', MASTER_HS_URL='https://master.invalid', MASTER_USER='@fixture:master',
        MASTER_TOKEN='fixture', MANAGER_MXID='@manager:master', MASTER_SPACE='!space:master',
        UPLINK_DB=str(Path(directory) / 'state.db')))
    daemon = uplink.Uplink(cfg)
    old_ts = int((time.time() - 72 * 3600) * 1000)
    for room in range(100):
        source, destination = '!room%d:local' % room, '!mirror%d:master' % room
        daemon.db.execute('INSERT INTO mirror_rooms(local_room_id,master_room_id,source) VALUES (?,?,?)',
                          (source, destination, 'imessage'))
        for page in range(5):
            daemon.enqueue_events(source, destination, [
                dict(event_id='$%d-%d' % (room, page * 200 + event), type='m.room.message',
                     origin_server_ts=old_ts + page * 200 + event)
                for event in range(200)], priority=10)
    assert daemon.db.execute('SELECT count(*) FROM pending_events').fetchone()[0] == 100000
    daemon.db.close()
    daemon.db = daemon._open_db(cfg.db_path)
    assert daemon.sync_health()['pending_events'] == 100000

    def local(method, path, body=None, query=None, **kwargs):
        if path.endswith('/account_data/com.jkali.master_link'):
            raise urllib.error.HTTPError('fixture', 404, 'legacy env', {}, None)
        if path.endswith('/account_data/com.jkali.share_override'):
            return {'state': 'share'}
        if '/event/' in path:
            eid = urllib.parse.unquote(path.rsplit('/', 1)[1])
            return dict(event_id=eid, type='m.room.message', sender=cfg.local_user,
                        origin_server_ts=old_ts, content={'msgtype': 'm.text', 'body': 'fixture'})
        if '/state/m.room.member/' in path:
            return {'displayname': 'Fixture'}
        raise AssertionError((method, path))

    daemon.local = local
    daemon.master = lambda *a, **k: (_ for _ in ()).throw(uplink.MasterUnreachable('72-hour outage fixture'))
    try:
        daemon.deliver_pending()
        raise AssertionError('offline transport must fail delivery')
    except uplink.MasterUnreachable:
        pass
    assert daemon.sync_health()['pending_events'] == 100000
    daemon.enqueue_events('!room0:local', '!mirror0:master', [dict(
        event_id='$fresh-live', type='m.room.message', origin_server_ts=int(time.time() * 1000))])
    sends = []
    def master(method, path, body=None, **kwargs):
        sends.append(urllib.parse.unquote(path.rsplit('/', 1)[1]))
        return {'event_id': '$remote-' + str(len(sends))}
    daemon.master = master
    started = time.monotonic()
    daemon.deliver_pending()
    elapsed = time.monotonic() - started
    assert sends[0] == 'uplink_$fresh-live', 'live message must outrank 100k historical references'
    assert 1 <= len(sends) <= 200, 'one pass must respect the page/work-slice bound'
    assert elapsed < 15, 'healthy fake transport should keep a delivery slice responsive'
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes = rss if sys.platform == 'darwin' else rss * 1024
    assert rss_bytes < 512 * 1024 * 1024, 'fixture RSS exceeded planned 512 MiB target'
    assert daemon.sync_health()['pending_events'] == 100001 - len(sends)
    daemon.db.close()
    print('PASS: 100 rooms / 100000 references / simulated 72h outage; '
          'live first; slice=%d events in %.2fs; RSS=%.1f MiB' % (len(sends), elapsed, rss_bytes / 1024 / 1024))
