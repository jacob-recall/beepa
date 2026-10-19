"""Read-only native iMessage window for exact CRM response attribution.

Pinned platform-imessage v0.24.4 PlatformAPI.getMessages returns the newest
20 database rows, expanded into message parts, in chronological order.
hasMore is based on raw row count, not exported item count. Message.cursor is
the original Apple nanosecond date; ties are NOT broken with Matrix arrival or
GUIDs. A full latest window can prove a suffix even when older history exists.

This reuses ContactRoomReader's current bridge/portal proof and the daemon's
existing ID-only receipt/echo maps. It never reads Apple's store directly,
persists another contact database, matches message bodies, or invokes a send.
"""
import http.client
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess

from imessage.daemon import _extract_json
from agents.enrich.crm_resolver import EvidenceError

_EVENT = re.compile(r"^\$[^\s]{1,255}$")
_CURSOR = re.compile(r"^[1-9][0-9]{0,18}$")


class NativeHistoryReader:
    def __init__(self, *, room_reader, runner=subprocess.run):
        self.room_reader, self.runner = room_reader, runner

    def _proof(self, room):
        proofs = self.room_reader._imessage(None, room_id=room)
        if (len(proofs) != 1 or proofs[0].get('room_id') != room
                or proofs[0].get('source') != 'imessage'
                or not isinstance(proofs[0].get('bridge_epoch'), str)
                or not isinstance(proofs[0].get('native_id'), str)
                or ';+;' in proofs[0]['native_id']):
            raise ValueError('native-room-unverified')
        return proofs[0]

    def _config(self, proof):
        with open(self.room_reader.imessage_config, encoding='utf-8') as stream:
            cfg = json.load(stream)
        executable = cfg.get('cli_path')
        if (cfg.get('user_id') != proof.get('local_owner_mxid') or cfg.get('bot_id') != proof.get('bot_mxid')
                or not isinstance(executable, str) or not Path(executable).is_absolute()):
            raise ValueError('native-config-changed')
        return executable

    def _maps(self, room, chat, requested, include_confirmed=False):
        path = Path(self.room_reader.imessage_db).resolve()
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('BEGIN')
            if db.execute('SELECT chat_id FROM map WHERE room_id=?', (room,)).fetchall() != [(chat,)]:
                raise ValueError('native-room-changed')
            parts = {event: [] for event in requested}
            for event, native in db.execute(
                    'SELECT n.event_id,n.native_id FROM native_receipt n '
                    'JOIN outbound_event o ON o.event_id=n.event_id AND o.room_id=n.room_id '
                    "WHERE n.room_id=? AND n.chat_id=? AND o.state='confirmed' ORDER BY n.event_id,n.native_id", (room, chat)):
                if include_confirmed:
                    parts.setdefault(event, [])
                if event in parts:
                    parts[event].append(native)
            echoes = dict(db.execute('SELECT msg_id,event_id FROM event_map WHERE chat_id=?', (chat,)))
            return parts, echoes
        finally:
            db.close()

    def snapshot(self, room_id, outgoing_event_ids, *, bridge_epoch=None):
        return self._snapshot(room_id, outgoing_event_ids, bridge_epoch=bridge_epoch)

    def conversation_snapshot(self, room_id, *, bridge_epoch=None):
        """ID-only latest native window, including conversations with no outreach.

        This is a separate read contract: an empty batch target list remains
        invalid for snapshot(). Window coverage never implies native EOF when
        has_more is true. Consumers must retain/reconcile exact overlapping IDs.
        """
        return self._snapshot(room_id, [], bridge_epoch=bridge_epoch, monitoring=True)

    def _snapshot(self, room_id, outgoing_event_ids, *, bridge_epoch=None, monitoring=False):
        empty = {'complete': False, 'window_complete': False, 'source': 'imessage',
                 'room_id': room_id, 'events': [], 'outgoing_parts': {}}
        try:
            if (not isinstance(outgoing_event_ids, (list, tuple, set)) or not (0 if monitoring else 1) <= len(outgoing_event_ids) <= 200
                    or any(not isinstance(event, str) or not _EVENT.fullmatch(event) for event in outgoing_event_ids)):
                raise ValueError('invalid-outgoing-scope')
            requested = sorted(set(outgoing_event_ids))
            proof = self._proof(room_id)
            if bridge_epoch is not None and proof['bridge_epoch'] != bridge_epoch:
                raise ValueError('native-bridge-epoch-changed')
            executable = self._config(proof)
            maps = self._maps(room_id, proof['native_id'], requested, include_confirmed=monitoring)
            process = self.runner([executable, '--no-events', '--json', 'messages', proof['native_id']],
                                  capture_output=True, timeout=20, shell=False)
            if process.returncode != 0 or not isinstance(process.stdout, bytes) or len(process.stdout) > 32 * 1024 * 1024:
                raise ValueError('native-read-unavailable')
            page = _extract_json(process.stdout.decode('utf-8', 'strict'))
            if (not isinstance(page, dict) or not isinstance(page.get('items'), list)
                    or type(page.get('hasMore')) is not bool or len(page['items']) > 2000):
                raise ValueError('native-window-malformed')
            if self._proof(room_id) != proof or self._config(proof) != executable or self._maps(room_id, proof['native_id'], requested, include_confirmed=monitoring) != maps:
                raise ValueError('native-proof-changed-during-read')
            parts, echoes = maps
            if any(not value for value in parts.values()):
                raise ValueError('native-outgoing-unmapped')
            owners = {}
            for event, native_ids in parts.items():
                if not isinstance(event, str) or not _EVENT.fullmatch(event):
                    raise ValueError('native-outgoing-map-malformed')
                for native in native_ids:
                    if not isinstance(native, str) or not native or len(native) > 512:
                        raise ValueError('native-outgoing-map-malformed')
                    owners.setdefault(native, []).append(event)
            events, identities = [], set()
            for message in page['items']:
                if not isinstance(message, dict):
                    raise ValueError('native-message-malformed')
                native, order, timestamp = message.get('id'), message.get('cursor'), message.get('timestamp')
                if (not isinstance(native, str) or not native or len(native) > 512 or native in identities
                        or not isinstance(order, str) or not _CURSOR.fullmatch(order) or int(order) > 9223372036854775807
                        or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                        or not 0 < timestamp <= 8640000000000000 or int(timestamp) != timestamp
                        or timestamp != int(order) // 1_000_000 + 978307200000
                        or type(message.get('isSender')) is not bool or message.get('threadID') != proof['native_id']):
                    raise ValueError('native-message-malformed')
                if any(key in message and message[key] is not None and type(message[key]) is not bool
                       for key in ('isAction', 'isDeleted', 'isHidden')):
                    raise ValueError('native-message-malformed')
                identities.add(native)
                if (message.get('isAction') is True or message.get('isDeleted') is True
                        or message.get('isHidden') is True or message.get('action') is not None):
                    continue
                if native in owners and message['isSender'] is not True:
                    raise ValueError('native-outgoing-direction-changed')
                event_id = echoes.get(native)
                if event_id is not None and (not isinstance(event_id, str) or not _EVENT.fullmatch(event_id)):
                    raise ValueError('native-echo-map-malformed')
                events.append({'native_id': native, 'order': order, 'timestamp': int(timestamp),
                               'incoming': not message['isSender'], 'event_id': event_id,
                               'outgoing_event_ids': sorted(owners.get(native, []))})
            # This sort uses ONLY native source order, never UUID or arrival.
            events.sort(key=lambda event: int(event['order']))
            if monitoring:
                ties = len(events) != len({event['order'] for event in events})
                inverted = any(a['timestamp'] > b['timestamp'] for a, b in zip(events, events[1:]))
                ready = not ties and not inverted and (bool(events) or not page['hasMore'])
                present = {event['native_id'] for event in events}
                return {**empty, 'evidence_kind': 'native_complete', 'window_complete': True,
                        'complete': ready and page['hasMore'] is False, 'monitoring_ready': ready,
                        'reason': 'native-order-tie' if ties else 'native-time-order-inversion' if inverted else '' if ready else 'native-window-boundary',
                        'bridge_epoch': proof['bridge_epoch'], 'bridge_epoch_kind': proof.get('bridge_epoch_kind'),
                        'has_more': page['hasMore'], 'events': events,
                        'outgoing_parts': {event: ids for event, ids in parts.items() if any(native in present for native in ids)}}
            orders = {event['native_id']: int(event['order']) for event in events}
            all_parts = {native for values in parts.values() for native in values}
            present = all_parts <= orders.keys()
            earliest = min((orders[native] for native in all_parts if native in orders), default=None)
            boundary = int(events[0]['order']) if events else None
            covered = present and (page['hasMore'] is False or (boundary is not None and boundary < earliest))
            suffix = [event['order'] for event in events if earliest is not None and int(event['order']) >= earliest]
            ties = len(suffix) != len(set(suffix))
            reason = ('native-order-tie' if ties else 'native-outgoing-outside-window' if not present
                      else 'native-window-boundary' if not covered else '')
            return {**empty, 'complete': covered and not ties, 'window_complete': True, 'reason': reason,
                    'bridge_epoch': proof['bridge_epoch'], 'bridge_epoch_kind': proof.get('bridge_epoch_kind'),
                    'has_more': page['hasMore'], 'boundary_order': str(boundary) if boundary is not None else None,
                    'events': events, 'outgoing_parts': parts}
        except ValueError as error:
            # Only our fixed reason codes may escape; parser/provider exception
            # text can contain credentials, recipient handles or native bodies.
            reason = str(error)
            return {**empty, 'reason': reason if re.fullmatch(r'(native|invalid)-[a-z-]{1,80}', reason) else 'native-evidence-unavailable'}
        except (EvidenceError, OSError, sqlite3.Error, KeyError, TypeError, AttributeError,
                RuntimeError, http.client.HTTPException, subprocess.SubprocessError, UnicodeError):
            return {**empty, 'reason': 'native-evidence-unavailable'}
