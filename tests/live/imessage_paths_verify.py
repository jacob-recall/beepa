#!/usr/bin/env python3
"""Explicit live self-chat checks for local and master Direct iMessage paths.

Requires an existing configured self portal already set to Direct. Does not
change consent, acknowledge a suspended authority, provision accounts or call
the native CLI directly. Each path sends one unique message through Matrix.
"""
import argparse
import hashlib
import ipaddress
import json
import sqlite3
import socket
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from install_config import read_env, read_manifest
from self_send_verify import Client, imessage_self_mxid

q = lambda value: urllib.parse.quote(value, safe='')


def rows(path, sql, args=()):
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        return db.execute(sql, args).fetchall()


def inbound_match(event, text, sender):
    content = event.get('content') or {}
    return (event.get('type') == 'm.room.message' and event.get('sender') == sender
            and content.get('body') == text and content.get('com.jkali.from_me') is not True)


def run(root, paths, timeout, output):
    manifest = read_manifest(root) or {}
    state = Path(manifest.get('state_root', root))
    cfg = json.loads((state / 'imessage/daemon.json').read_text())
    handle = cfg.get('self_handle')
    if not handle or 'REPLACE' in handle:
        raise ValueError('A configured self handle is required')
    imsg_db, uplink_db = state / 'imessage/state.db', state / 'agents/uplink/state.db'
    portals = rows(imsg_db, 'SELECT chat_id,room_id FROM map')
    portals = [(chat, room) for chat, room in portals if chat in
               ('any;-;' + handle, 'iMessage;-;' + handle)]
    if len(portals) != 1:
        raise ValueError('Exactly one existing direct self portal is required')
    _, room = portals[0]
    env = read_env(state / 'agents/uplink/local.env.local')
    env.update(read_env(state / 'agents/uplink/uplink.env.local'))
    local = Client(env['LOCAL_HS_URL'], env['LOCAL_TOKEN'], env['LOCAL_USER'])
    if local.call('GET', '/_matrix/client/v3/account/whoami')['user_id'] != cfg['user_id']:
        raise ValueError('Local credential/config identity mismatch')
    sender = imessage_self_mxid(local, handle)
    master = None
    transport = None
    if 'master' in paths:
        link = local.call('GET', '/_matrix/client/v3/user/' + q(local.user) + '/account_data/com.jkali.master_link')
        if link.get('enabled') is False or not link.get('master_token'):
            raise ValueError('Organization connection is disabled')
        consent = local.call('GET', '/_matrix/client/v3/user/' + q(local.user) + '/rooms/' + q(room)
                             + '/account_data/com.jkali.share_override')
        if consent.get('state') != 'direct':
            raise ValueError('Self conversation must already be Direct; test never changes consent')
        meta = dict(rows(uplink_db, 'SELECT k,v FROM meta'))
        if meta.get('direct_send_suspended_ts'):
            raise ValueError('Master authority is suspended; test never acknowledges it')
        if not rows(uplink_db, 'SELECT 1 FROM mirror_rooms WHERE local_room_id=?', (room,)):
            raise ValueError('Self conversation is not mirrored')
        if not meta.get('proposal_sync_since') or not rows(uplink_db, 'SELECT 1 FROM proposal_map LIMIT 1'):
            raise ValueError('Wait for the normal proposal cold-start pass before testing')
        master_state = Path(manifest.get('master_state_root', state / 'master'))
        manager = read_env(master_state / 'tokens.local')
        address = urllib.parse.urlsplit(link['master_hs_url'])
        ips = {entry[4][0] for entry in socket.getaddrinfo(address.hostname, address.port or 443, type=socket.SOCK_STREAM)}
        networks = [ipaddress.ip_network('100.64.0.0/10'), ipaddress.ip_network('fd7a:115c:a1e0::/48')]
        if address.scheme != 'https' or not any(ipaddress.ip_address(ip) in network for ip in ips for network in networks):
            raise ValueError('Master test requires HTTPS at a resolved Tailscale address')
        master = Client(link['master_hs_url'], manager['MASTER_MANAGER_TOKEN'], link['manager_mxid'])
        if master.call('GET', '/_matrix/client/v3/account/whoami')['user_id'] != master.user:
            raise ValueError('Manager token identity mismatch')
        proposal_room = meta['master_proposals_room']
        transport = {'https': True, 'tailscale_address': True, 'manager_authenticated': True}
    native = Path(cfg['cli_path'])
    fingerprint = lambda: {'inode': native.stat().st_ino, 'sha256': hashlib.sha256(native.read_bytes()).hexdigest()}
    before = fingerprint()
    report = {'started_at': int(time.time()), 'checks': [], 'native_before': before}
    if transport:
        report['master_transport'] = transport

    def save():
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2) + '\n')
            output.chmod(0o600)

    for path in paths:
        text = 'Beepa live self-test ' + path + ' ' + uuid.uuid4().hex[:16]
        check = {'path': path, 'nonce': text, 'status': 'sending'}
        report['checks'].append(check)
        save()
        if path == 'local':
            check['local_event'] = local.send_text(room, text)['event_id']
        else:
            check['master_proposal'] = master.call('PUT', '/_matrix/client/v3/rooms/' + q(proposal_room)
                + '/send/com.jkali.proposal/livecheck_' + uuid.uuid4().hex,
                {'target_room': room, 'body': text, 'created_by': master.user,
                 'origin_ts': int(time.time() * 1000)})['event_id']
        check['status'] = 'waiting_for_inbound'
        save()
        print(path + ': sent one self-test through Matrix; waiting for inbound proof', flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = local.recent_messages(room, 100)
            if path == 'master':
                sent = [e for e in events if e.get('sender') == local.user and
                        (e.get('content') or {}).get('body') == text]
                if sent:
                    check['local_event'] = sent[0]['event_id']
                outcome = rows(uplink_db, 'SELECT outcome FROM proposal_map WHERE master_event_id=?', (check['master_proposal'],))
                if outcome:
                    check['direct_outcome'] = outcome[0][0]
            if check.get('local_event'):
                native_result = rows(imsg_db, 'SELECT state,reason FROM outbound_event WHERE event_id=?', (check['local_event'],))
                if native_result:
                    check['native_outcome'], check['native_reason'] = native_result[0]
            inbound = [e for e in events if inbound_match(e, text, sender)]
            if inbound and check.get('native_outcome') == 'confirmed' and (path == 'local' or check.get('direct_outcome') == 'sent'):
                check['inbound_event'] = inbound[0]['event_id']
                check['status'] = 'passed'
                break
            if check.get('native_outcome') in ('refused', 'ambiguous') or check.get('direct_outcome') in ('fallback', 'ambiguous'):
                check['status'] = 'failed'
                break
            save()
            time.sleep(2)
        else:
            check['status'] = 'timed_out'
        save()
        print(path + ': ' + check['status'] + '; native=' + str(check.get('native_outcome'))
              + '; Direct=' + str(check.get('direct_outcome')), flush=True)
        if check['status'] != 'passed':
            break  # Do not pile up more real sends after a failed path.
    report['native_unchanged'] = before == fingerprint()
    report['passed'] = (len(report['checks']) == len(paths) and
                        all(c['status'] == 'passed' for c in report['checks']) and report['native_unchanged'])
    save()
    return report['passed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=REPO)
    parser.add_argument('--paths', choices=('local', 'master', 'both'), default='both')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--i-am-sending-to-myself', action='store_true')
    args = parser.parse_args()
    if not args.i_am_sending_to_myself:
        parser.error('Explicit --i-am-sending-to-myself is required; nothing sent')
    paths = ['local', 'master'] if args.paths == 'both' else [args.paths]
    return 0 if run(args.root, paths, args.timeout, args.output) else 1


if __name__ == '__main__':
    sys.exit(main())
