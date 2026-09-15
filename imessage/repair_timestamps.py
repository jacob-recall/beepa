#!/usr/bin/env python3
"""Repair display timestamps by exact native/local/master IDs; never send messages.

Default is a read-only audit. --apply writes only timestamp-correction state.
Original message events, bodies, native sends and dispatch ledgers are untouched.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(0, str(CODE_ROOT / 'shared'))
sys.path.insert(0, str(CODE_ROOT / 'agents/uplink'))
from install_config import read_env, read_manifest, atomic_write
from message_timestamps import CORRECTION_TYPE, ORIGIN_TS, valid_ts
from uplink import Config, Uplink, _mx
import consent

q = lambda value: urllib.parse.quote(value, safe='')


def get_optional(request, path):
    try:
        return request('GET', path)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        return None


def correction(request, room, event_id, timestamp, allowed_sender, apply=False, record=None):
    """Validate the mapped target, then modify only a separate state event."""
    if not valid_ts(timestamp):
        return 'invalid_timestamp'
    base = '/_matrix/client/v3/rooms/' + q(room)
    event = get_optional(request, base + '/event/' + q(event_id))
    if event is None:
        return 'target_unavailable'
    if (event.get('event_id') != event_id or event.get('type') != 'm.room.message'
            or not allowed_sender(event.get('sender'))):
        return 'refused_target'
    # An edit remains an edit; correcting the original must not undo its body.
    path = base + '/state/' + CORRECTION_TYPE + '/' + q(event_id)
    previous = get_optional(request, path)
    wanted = {'version': 1, 'source': 'imessage', 'origin_ts': timestamp}
    if previous == wanted or (previous is None and (event.get('content') or {}).get(ORIGIN_TS) == timestamp):
        return 'already_correct'
    if apply:
        if record is None:
            raise ValueError('A durable repair journal is required before writes')
        record(path, previous, wanted)
        request('PUT', path, wanted)
        if request('GET', path) != wanted:
            raise ValueError('Correction verification failed')
    return 'corrected' if apply else 'would_correct'


def mappings(db):
    """Include every mapped component and the legacy primary event mapping."""
    result = {}
    for chat, room, mid, eid in db.execute(
            'SELECT m.chat_id,m.room_id,e.msg_id,e.event_id FROM event_map e JOIN map m ON m.chat_id=e.chat_id'):
        if eid:
            result[(chat, room, mid, eid)] = None
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'inbound_component' in tables:
        for chat, room, mid, eid in db.execute(
                "SELECT m.chat_id,m.room_id,c.msg_id,c.event_id FROM inbound_component c JOIN map m ON m.room_id=c.room_id WHERE c.event_id<>'' AND c.status LIKE 'confirmed%'"):
            result[(chat, room, mid, eid)] = None
    return list(result)


def read_source_timestamps(daemon, chat, wanted, max_pages=1000):
    """Page the pinned CLI's ascending history using its opaque message cursor."""
    found, cursors = {}, set()
    before = None
    for _ in range(max_pages):
        args = ('messages', chat) + (('--before', before) if before else ())
        page = daemon.cli_json(*args, timeout=45)
        items = page.get('items', [])
        if not isinstance(items, list):
            raise ValueError('Invalid native history page')
        for item in items:
            if isinstance(item, dict) and str(item.get('id')) in wanted:
                found[str(item['id'])] = item.get('timestamp')
        if wanted.issubset(found) or not page.get('hasMore'):
            return found, True
        before = str(items[0].get('cursor') or '') if items else ''
        if not re.fullmatch(r'[0-9]+', before) or before in cursors:
            return found, False
        cursors.add(before)
    return found, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('BEEPA_INSTALL_ROOT', CODE_ROOT)))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    manifest = read_manifest(args.root) or {}
    state = Path(manifest.get('state_root') or args.root).resolve()
    config = json.loads((state / 'imessage/daemon.json').read_text())
    env = read_env(state / 'agents/uplink/local.env.local')
    env.update(read_env(state / 'agents/uplink/uplink.env.local'))
    cfg = Config(env)
    if config['user_id'] != cfg.local_user:
        raise ValueError('Bridge and uplink identities differ')
    im_db = sqlite3.connect('file:' + str(state / 'imessage/state.db') + '?mode=ro', uri=True)
    up_db = sqlite3.connect('file:' + str(state / 'agents/uplink/state.db') + '?mode=ro', uri=True)
    local_user = lambda method, path, body=None: _mx(cfg.local_hs, cfg.local_token, method, path, body)
    local = lambda method, path, body=None: _mx(config['hs_url'], config['as_token'], method, path, body, query={'user_id': config['bot_id']})
    control = get_optional(local_user, '/_matrix/client/v3/user/' + q(cfg.local_user) + '/account_data/com.jkali.master_link')
    if control is None:
        disabled = up_db.execute("SELECT v FROM meta WHERE k='link_disabled'").fetchone()
        link = {} if disabled and disabled[0] == '1' else {
            'master_hs_url': cfg.master_hs, 'master_user': cfg.master_user, 'master_token': cfg.master_token}
    else:
        link = control if isinstance(control, dict) and not control.get('disabled') and control.get('enabled') is not False else {}
    # Adopt only the current control-bound recovered scoped credentials.
    runtime = up_db.execute("SELECT v FROM meta WHERE k='master_runtime'").fetchone()
    probe = object.__new__(Uplink)
    probe.cfg = cfg
    if link and runtime:
        runtime = json.loads(runtime[0])
        if runtime.get('control_fingerprint') == probe.control_fingerprint(control):
            link = runtime['link']
    master = None
    if all(link.get(k) for k in ('master_hs_url', 'master_user', 'master_token')):
        master = lambda method, path, body=None: _mx(link['master_hs_url'], link['master_token'], method, path, body)
    # The daemon module is inert until initialized. Only its read-only CLI path
    # is used; initialize() would touch native send recovery state.
    spec = importlib.util.spec_from_file_location('timestamp_source_reader', CODE_ROOT / 'imessage/daemon.py')
    daemon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon)
    daemon.CLI = config['cli_path']
    records = mappings(im_db)
    native = {}
    counts = {'mapped_components': len(records), 'source_unavailable': 0, 'invalid_timestamp': 0,
              'master_private_or_unmapped': 0, 'source_read_errors': 0, 'source_history_incomplete': 0}
    for chat in dict.fromkeys(r[0] for r in records):
        try:
            wanted = {r[2] for r in records if r[0] == chat}
            native[chat], complete = read_source_timestamps(daemon, chat, wanted)
            if not complete:
                counts['source_history_incomplete'] += 1
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            native[chat] = {}
            counts['source_read_errors'] += 1
    journal = {'version': 1, 'local_user': cfg.local_user, 'entries': {}}
    journal_path = None
    if args.apply:
        folder = state / '.beepa-timestamp-repair' / str(time.time_ns())
        folder.mkdir(parents=True, mode=0o700)
        folder.parent.chmod(0o700)
        for name, db in [('imessage', im_db), ('uplink', up_db)]:
            backup = folder / (name + '.sqlite')
            with sqlite3.connect(backup) as target:
                db.backup(target)
            backup.chmod(0o600)
        journal_path = folder / 'journal.json'
        atomic_write(journal_path, json.dumps(journal, indent=2) + '\n')

    def record(destination, path, previous, wanted):
        key = destination + ':' + path
        journal['entries'].setdefault(key, {'previous': previous, 'wanted': wanted})
        atomic_write(journal_path, json.dumps(journal, indent=2) + '\n')

    def count(destination, outcome):
        key = destination + '_' + outcome
        counts[key] = counts.get(key, 0) + 1

    local_domain = ':' + config['domain']
    allowed_local = lambda sender: isinstance(sender, str) and (
        sender == config['bot_id'] or sender.startswith('@imessage_') and sender.endswith(local_domain))
    checked_rooms = set()
    for chat, room, mid, eid in records:
        if mid not in native[chat]:
            counts['source_unavailable'] += 1
            continue
        ts = native[chat][mid]
        if not valid_ts(ts):
            counts['invalid_timestamp'] += 1
            continue
        ts = int(ts)
        if room not in checked_rooms:
            state_events = get_optional(local, '/_matrix/client/v3/rooms/' + q(room) + '/state')
            if state_events is None:
                count('local', 'room_unavailable')
                continue
            if not any(e.get('type') == 'm.room.create' and e.get('sender') == config['bot_id'] for e in state_events):
                raise ValueError('Local room ownership mismatch')
            checked_rooms.add(room)
        outcome = correction(local, room, eid, ts, allowed_local, args.apply,
                             lambda *values: record('local', *values))
        count('local', outcome)
        if outcome in ('refused_target', 'target_unavailable'):
            continue
        mirror = up_db.execute("SELECT master_room_id FROM mirror_rooms WHERE local_room_id=? AND source='imessage'", (room,)).fetchone()
        level_path = '/_matrix/client/v3/user/' + q(cfg.local_user) + '/rooms/' + q(room) + '/account_data/' + consent.SHARE_OVERRIDE_TYPE
        level = consent.effective_level(get_optional(local_user, level_path))
        if not master or not mirror or level not in ('share', 'direct'):
            counts['master_private_or_unmapped'] += 1
            continue
        lifecycle = up_db.execute('SELECT status FROM mirror_lifecycle WHERE local_room_id=?', (room,)).fetchone()
        if lifecycle and lifecycle[0] == 'revoking':
            counts['master_private_or_unmapped'] += 1
            continue
        target = up_db.execute('SELECT master_event_id FROM delivery_map WHERE master_room_id=? AND local_event_id=?', (mirror[0], eid)).fetchone()
        if not target and up_db.execute('SELECT 1 FROM legacy_mirrors WHERE master_room_id=?', (mirror[0],)).fetchone():
            target = up_db.execute('SELECT master_event_id FROM event_map WHERE local_event_id=?', (eid,)).fetchone()
        if not target or not target[0]:
            counts['master_private_or_unmapped'] += 1
            continue
        # Fresh explicit sharing and unchanged pairing gate every remote write.
        if args.apply:
            current = get_optional(local_user, '/_matrix/client/v3/user/' + q(cfg.local_user) + '/account_data/com.jkali.master_link')
            if current != control or consent.effective_level(get_optional(local_user, level_path)) not in ('share', 'direct'):
                raise ValueError('Pairing or sharing changed; repair paused')
        count('master', correction(master, mirror[0], target[0], ts,
                                  lambda sender: sender == link['master_user'], args.apply,
                                  lambda *values: record('master', *values)))
    counts['applied'] = args.apply
    if journal_path:
        journal['result'] = counts
        atomic_write(journal_path, json.dumps(journal, indent=2) + '\n')
        counts['journal'] = str(journal_path)
    print(json.dumps(counts, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Transport/native exceptions may contain credential URLs or messages.
        code = ' HTTP ' + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else ''
        print('Timestamp repair stopped: ' + type(exc).__name__ + code, file=sys.stderr)
        sys.exit(1)
