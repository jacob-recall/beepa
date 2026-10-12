#!/usr/bin/env python3
"""Opt-in: send ONE self-chat message and verify native/local/master receipts.

An output file is a durable send ledger: rerunning resumes that message, never
sends it again. Uses existing credentials/portal/consent; never marks chats read.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from install_config import read_env, read_manifest
from self_send_verify import Client


def run(root, recipient, output, timeout, master_loopback_url=None):
    if master_loopback_url:
        url = urllib.parse.urlsplit(master_loopback_url)
        if url.scheme != 'http' or url.hostname not in ('localhost', '127.0.0.1', '::1') or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('Master diagnostic override must be a loopback HTTP origin')
    manifest = read_manifest(root) or {}
    state = Path(manifest.get('state_root', root))
    cfg = json.loads((state / 'imessage/daemon.json').read_text())
    if recipient != cfg.get('self_handle'):
        raise ValueError('Recipient must match this installation\'s configured self handle')
    def rows(file, sql, args=()):
        with sqlite3.connect(file.resolve().as_uri() + '?mode=ro', uri=True) as db:
            return db.execute(sql, args).fetchall()
    native_db = state / 'imessage/state.db'
    portals = rows(native_db, 'SELECT chat_id,room_id FROM map WHERE chat_id IN (?,?)',
                   ('any;-;' + recipient, 'iMessage;-;' + recipient))
    if len(portals) != 1:
        raise ValueError('Exactly one existing self portal is required')
    chat, room = portals[0]
    # The new schema must already be activated before a real send.
    rows(native_db, 'SELECT count(*) FROM native_receipt')
    env = read_env(state / 'agents/uplink/local.env.local')
    env.update(read_env(state / 'agents/uplink/uplink.env.local'))
    local = Client(env['LOCAL_HS_URL'], env['LOCAL_TOKEN'], env['LOCAL_USER'])
    if local.call('GET', '/_matrix/client/v3/account/whoami')['user_id'] != cfg['user_id']:
        raise ValueError('Credential identity mismatch')
    q = lambda value: urllib.parse.quote(value, safe='')
    prefix = '/_matrix/client/v3/rooms/' + q(room)
    cli = Path(cfg['cli_path'])
    fingerprint = lambda: {'sha256': hashlib.sha256(cli.read_bytes()).hexdigest(), 'inode': cli.stat().st_ino}
    if output.exists():
        report = json.loads(output.read_text())
        if report['room'] != room or report['user'] != cfg['user_id']:
            raise ValueError('Existing report belongs to a different account/portal')
        if not report.get('event_id'):
            raise ValueError('Previous send is ambiguous; inspect it instead of resending')
    else:
        report = {'room': room, 'user': cfg['user_id'], 'native_before': fingerprint(),
                  'text': 'Beepa read-receipt self-test ' + uuid.uuid4().hex[:12]}
        output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(report, f)
        report['event_id'] = local.send_text(room, report['text'])['event_id']
        output.write_text(json.dumps(report, indent=2) + '\n')
        print('Sent exactly one message: ' + report['text'], flush=True)
    master = None
    try:
        link = local.call('GET', '/_matrix/client/v3/user/' + q(local.user) + '/account_data/com.jkali.master_link')
        if link.get('enabled') is not False and link.get('master_token'):
            master = Client(master_loopback_url or link['master_hs_url'], link['master_token'], link['master_user'])
            if master_loopback_url and master.call('GET', '/_matrix/client/v3/account/whoami')['user_id'] != link['master_user']:
                raise ValueError('Master diagnostic endpoint identity mismatch')
    except (urllib.error.URLError, KeyError):
        pass
    def get_state(client, room_id, kind, target):
        try:
            return client.call('GET', '/_matrix/client/v3/rooms/' + q(room_id) + '/state/' + kind + '/' + q(target), timeout=10)
        except urllib.error.HTTPError as exc:
            if exc.code == 404: return {}
            raise
    previous = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = report['event_id']
        native = rows(native_db, 'SELECT count(*),min(delivered),min(read_ts>0) FROM native_receipt WHERE event_id=?', (event,))[0]
        report['native'] = dict(parts=native[0], delivered=bool(native[1]), read=bool(native[2]))
        receipt = get_state(local, room, 'com.jkali.imessage_receipt', event)
        report['local'] = dict(delivered=receipt.get('com.jkali.delivered') is True, read=receipt.get('com.jkali.read') is True)
        report['master'] = {'mapped': False, 'read': False, 'delivered': False}
        mappings = rows(state / 'agents/uplink/state.db',
                        'SELECT d.master_room_id,d.master_event_id FROM delivery_map d JOIN mirror_rooms m '
                        'ON m.master_room_id=d.master_room_id WHERE m.local_room_id=? AND d.local_event_id=?', (room, event))
        if master and len(mappings) == 1:
            try:
                stamp = get_state(master, mappings[0][0], 'com.jkali.message_receipt', mappings[0][1])
                report['master'] = dict(mapped=True, delivered=stamp.get('delivered') is True, read=stamp.get('read') is True)
            except urllib.error.URLError as exc:
                report['master']['error'] = type(exc).__name__
        report['native_unchanged'] = fingerprint() == report['native_before']
        output.write_text(json.dumps(report, indent=2) + '\n')
        progress = {key: report[key] for key in ('native', 'local', 'master', 'native_unchanged')}
        if progress != previous:
            print(json.dumps(progress), flush=True)
            previous = progress
        if all(report[key].get('read') for key in ('native', 'local', 'master')) and report['native_unchanged']:
            return True
        time.sleep(3)
    print('Not all receipt stages confirmed before timeout; report retained. Rerun resumes without sending.', flush=True)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--recipient', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--master-loopback-url', help='Optional diagnostic origin when the same master runs on this Mac')
    parser.add_argument('--i-am-sending-to-myself', action='store_true')
    args = parser.parse_args()
    if not args.i_am_sending_to_myself:
        parser.error('Explicit --i-am-sending-to-myself required; nothing sent')
    return 0 if run(args.root, args.recipient, args.output, args.timeout, args.master_loopback_url) else 1


if __name__ == '__main__':
    sys.exit(main())
