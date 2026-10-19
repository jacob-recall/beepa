"""ID-only observed provider history; never claims upstream/native completeness.

Reuse ContactRoomReader for current owner/login/portal authority. Each fixed SQL
query reads one bounded PostgreSQL snapshot; only message identity/order and the
minimal backfill gate leave the bridge DB. See the provider-order evidence spec.
"""
import base64
import http.client
import json
from pathlib import Path
import re
import subprocess

from agents.enrich.crm_resolver import EvidenceError, PROVIDERS
from install_config import compose_prefix

_EVENT = re.compile(r'^\$[^\s]{1,255}$')
_NUMBER = re.compile(r'^[1-9][0-9]{0,19}$')
_LIMIT = 2000
# Audited image/source contracts; upgrading a bridge requires verifying its
# native identity/order mapping before adding its immutable digest here.
_IMAGE_DIGESTS = {
    'whatsapp': '86237c4d0d33a1e08910b1f820e6c561f9b8e21dc26943caf266e01021087002',
    'gmessages': 'cecc0b77bd4d64e6e04b7b5961749e3890daad7885b37e6cde5bb24a07735f3e',
    'instagram': '4b2704de94809351884da2640b1b1421920980a60269aa871fa870a8d50e7eb0',
    'linkedin': 'c10f4e6ed9484ff8053a4e84788d2cc5f9f9c519c91e087afa5179be0ad34d03',
    'twitter': '3d744f87953e1441eac229e4440debd46831e0b8a34a0cc13f2a57b11e237b44',
    'discord': '065405ca2f961b2687ca577c4eb65592c139d641342a9611d98b5394f30cf84a',
}


def _require(test, reason):
    if not test:
        raise ValueError(reason)


def _text(value, empty=False):
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= 2048 and '\x00' not in value


def _literal(value):
    _require(_text(value, True), 'provider-scope-invalid')
    # Hex-only SQL literal: no shell, psql-variable or quoted-string injection.
    return "convert_from(decode('" + value.encode('utf-8').hex() + "','hex'),'UTF8')"


def provider_history_query(source, proof):
    """Fixed SELECT, exact current portal/login/owner constraints, no credentials."""
    _require(source in PROVIDERS, 'provider-unsupported')
    owner, room, native, receiver, login = [_literal(proof[key]) for key in
        ('local_owner_mxid', 'room_id', 'native_id', 'receiver', 'login_id')]
    if source == 'discord':
        scope = f'''SELECT p.* FROM portal p JOIN "user" u ON u.dcid=p.receiver
          WHERE p.mxid={room} AND p.dcid={native} AND p.receiver={receiver}
          AND u.mxid={owner} AND u.dcid={login} AND p.type=1
          AND (p.dc_guild_id IS NULL OR p.dc_guild_id='')'''
        messages = '''SELECT m.dcid AS native_id,m.dc_attachment_id AS part_id,m.mxid AS event_id,
          m.dc_sender AS sender_id,m.sender_mxid,m.timestamp*1000000 AS timestamp_ns,
          NULL::text AS original_event_id,NULL::boolean AS is_outgoing
          FROM message m JOIN scope p ON m.dc_chan_id=p.dcid AND m.dc_chan_receiver=p.receiver'''
        backfill = "SELECT false AS pending"
    else:
        scope = f'''SELECT p.* FROM portal p JOIN user_portal up
          ON up.bridge_id=p.bridge_id AND up.portal_id=p.id AND up.portal_receiver=p.receiver
          JOIN user_login ul ON ul.bridge_id=up.bridge_id AND ul.id=up.login_id AND ul.user_mxid=up.user_mxid
          WHERE p.bridge_id='' AND p.mxid={room} AND p.id={native} AND p.receiver={receiver}
          AND p.room_type='dm' AND up.user_mxid={owner} AND up.login_id={login}'''
        original = "m.metadata->>'orig_mxid'" if source == 'gmessages' else 'NULL::text'
        outgoing = "m.metadata->'is_outgoing'" if source == 'gmessages' else 'NULL::jsonb'
        messages = f'''SELECT m.id AS native_id,m.part_id,m.mxid AS event_id,m.sender_id,m.sender_mxid,
          m.timestamp AS timestamp_ns,{original} AS original_event_id,{outgoing} AS is_outgoing
          FROM message m JOIN scope p ON m.bridge_id=p.bridge_id AND m.room_id=p.id AND m.room_receiver=p.receiver'''
        # queue_done/is_done mean a configured stop, not native EOF. Any queued,
        # in-progress or login-mismatched task blocks this observed projection.
        backfill = f'''SELECT EXISTS (SELECT 1 FROM backfill_task b JOIN scope p
          ON b.bridge_id=p.bridge_id AND b.portal_id=p.id AND b.portal_receiver=p.receiver
          WHERE NOT b.is_done OR NOT b.queue_done OR b.user_login_id<>{login}
          OR (b.dispatched_at IS NOT NULL AND (b.completed_at IS NULL OR b.dispatched_at>b.completed_at))) AS pending'''
    return f'''WITH scope AS ({scope}), messages AS ({messages}),
      latest AS (SELECT * FROM messages ORDER BY timestamp_ns DESC,native_id,part_id LIMIT {_LIMIT + 1}),
      backfill AS ({backfill})
      SELECT json_build_object('scope_count',(SELECT count(*) FROM scope),
        'backfill_pending',(SELECT pending FROM backfill),
        'rows',COALESCE((SELECT json_agg(latest) FROM latest),'[]'::json))'''


def _order(source, native, timestamp):
    _require(_text(native) and not native.startswith(('~', '$')), 'provider-native-id-unresolved')
    _require(type(timestamp) is int and 0 < timestamp <= 9223372036854775807, 'provider-timestamp-invalid')
    precision = {'whatsapp': 1000000000, 'gmessages': 1000, 'linkedin': 1000000,
                 'instagram': 1000000, 'discord': 1000000, 'twitter': 1}[source]
    _require(timestamp % precision == 0, 'provider-timestamp-contract')
    if source in ('twitter', 'discord'):
        _require(_NUMBER.fullmatch(native) is not None, 'provider-native-id-unresolved')
    if source == 'twitter':
        # The connector's numeric sequence ID excludes UUID/local-echo values.
        _require(int(native) <= 9223372036854775807, 'provider-native-id-unresolved')
        return int(native)
    if source == 'instagram':
        if native.startswith('fb:mid.$c'):
            payload = native[len('fb:mid.$c'):]
            _require(re.fullmatch(r'[A-Za-z0-9_-]{28}', payload) is not None, 'provider-native-id-unresolved')
            raw = base64.b64decode(payload, altchars=b'-_', validate=True)
            _require(len(raw) == 21 and (int.from_bytes(raw[8:13], 'big') + 1072915200000) * 1000000 == timestamp,
                     'provider-native-timestamp-mismatch')
        elif native.startswith('wa:'):
            _require(len(native.split(':')) == 4 and timestamp % 1000000000 == 0, 'provider-native-id-unresolved')
        else:
            raise ValueError('provider-native-id-unresolved')
    return timestamp


class ProviderHistoryReader:
    def __init__(self, *, room_reader, query=None, runner=subprocess.run):
        self.room_reader, self.runner = room_reader, runner
        self.query = query or self._query
        self._verified_images = set()

    def _proof(self, source, room):
        proofs = self.room_reader._provider(source, room_id=room)
        _require(len(proofs) == 1, 'provider-room-unverified')
        proof = proofs[0]
        _require(proof.get('source') == source and proof.get('room_id') == room
                 and all(_text(proof.get(key)) for key in ('local_owner_mxid','native_id','login_id','counterpart_id','bridge_epoch'))
                 and _text(proof.get('receiver'), True), 'provider-room-unverified')
        return proof

    def _image(self, source):
        prefix = compose_prefix(str(self.room_reader.root), str(Path(__file__).resolve().parents[2]))
        result = self.runner(prefix + ['images','-q','mautrix-' + PROVIDERS[source][0]],
                             capture_output=True, timeout=20, shell=False)
        _require(result.returncode == 0 and isinstance(result.stdout,bytes) and len(result.stdout) < 256, 'provider-image-unverified')
        identity = result.stdout.decode('ascii').strip()
        _require(re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}',identity) is not None, 'provider-image-unverified')
        key = (source, identity)
        if key not in self._verified_images:
            info = self.runner([prefix[0], 'image','inspect','--format','{{json .RepoDigests}}', identity],
                               capture_output=True, timeout=20, shell=False)
            _require(info.returncode == 0 and isinstance(info.stdout,bytes) and len(info.stdout) < 16384, 'provider-image-unverified')
            digests = json.loads(info.stdout.decode('utf-8','strict'))
            expected = 'dock.mau.dev/mautrix/' + PROVIDERS[source][0] + '@sha256:' + _IMAGE_DIGESTS[source]
            _require(isinstance(digests,list) and expected in digests, 'provider-image-unverified')
            self._verified_images.add(key)
        return identity

    def _query(self, source, proof):
        identity = self._image(source)
        args = compose_prefix(str(self.room_reader.root), str(Path(__file__).resolve().parents[2])) + [
            'exec', '-T', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'matrix',
            '-d', PROVIDERS[source][2], '-tAc', provider_history_query(source, proof)]
        result = self.runner(args, capture_output=True, timeout=20, shell=False)
        _require(result.returncode == 0 and isinstance(result.stdout, bytes) and len(result.stdout) <= 16*1024*1024,
                 'provider-history-unavailable')
        _require(self._image(source) == identity, 'provider-image-changed')
        return json.loads(result.stdout.decode('utf-8', 'strict'))

    def snapshot(self, source, room_id, outgoing_event_ids, *, bridge_epoch=None):
        return self._snapshot(source, room_id, outgoing_event_ids, bridge_epoch=bridge_epoch)

    def conversation_snapshot(self, source, room_id, *, bridge_epoch=None):
        """Observed native IDs/order for monitoring, never proof of native EOF."""
        return self._snapshot(source, room_id, [], bridge_epoch=bridge_epoch, monitoring=True)

    def _snapshot(self, source, room_id, outgoing_event_ids, *, bridge_epoch=None, monitoring=False):
        empty = {'source': source, 'room_id': room_id, 'evidence_kind': 'observed_native',
                 'complete': False, 'window_complete': False, 'observed_complete': False,
                 'observed_window_complete': False, 'events': [], 'outgoing_parts': {}}
        try:
            _require(source in PROVIDERS and isinstance(outgoing_event_ids, (list,tuple,set))
                     and (0 if monitoring else 1) <= len(outgoing_event_ids) <= 200
                     and all(isinstance(event,str) and _EVENT.fullmatch(event) for event in outgoing_event_ids), 'provider-request-invalid')
            requested = set(outgoing_event_ids)
            proof = self._proof(source, room_id)
            _require(bridge_epoch is None or proof['bridge_epoch'] == bridge_epoch, 'provider-epoch-changed')
            page = self.query(source, proof)
            _require(self._proof(source, room_id) == proof, 'provider-proof-changed')
            _require(isinstance(page,dict) and type(page.get('scope_count')) is int and page['scope_count'] == 1
                     and type(page.get('backfill_pending')) is bool and isinstance(page.get('rows'),list)
                     and len(page['rows']) <= _LIMIT + 1, 'provider-history-malformed')
            _require(not page['backfill_pending'], 'provider-backfill-pending')
            raw_rows = page['rows']
            groups, seen_parts, matrix_ids = {}, {}, {}
            for row in raw_rows:
                _require(isinstance(row,dict), 'provider-history-malformed')
                native, part, event = row.get('native_id'), row.get('part_id'), row.get('event_id')
                timestamp, sender = row.get('timestamp_ns'), row.get('sender_id')
                order = _order(source, native, timestamp)
                _require(_text(part, True) and _text(sender) and isinstance(event,str) and _EVENT.fullmatch(event), 'provider-message-malformed')
                own = row.get('sender_mxid') == proof['local_owner_mxid'] or sender == proof['login_id']
                incoming = sender == proof['counterpart_id']
                if source == 'gmessages' and row.get('is_outgoing') is True:
                    own = True
                _require(own != incoming, 'provider-direction-unresolved')
                original = row.get('original_event_id')
                _require(original is None or source == 'gmessages' and own and isinstance(original,str) and _EVENT.fullmatch(original), 'provider-original-id-invalid')
                _require(row.get('is_outgoing') is None or type(row['is_outgoing']) is bool, 'provider-direction-unresolved')
                identity = (native,part)
                evidence = (event,timestamp,sender,own,original)
                _require(identity not in seen_parts or seen_parts[identity] == evidence, 'provider-native-map-conflict')
                seen_parts[identity] = evidence
                for mapped in (event,original):
                    if mapped:
                        _require(mapped not in matrix_ids or matrix_ids[mapped] == native, 'provider-matrix-map-conflict')
                        matrix_ids[mapped] = native
                group = groups.setdefault(native, {'order':order,'timestamp':timestamp,'incoming':incoming,'parts':{},'outgoing':set(),'originals':set()})
                _require((group['order'],group['timestamp'],group['incoming']) == (order,timestamp,incoming), 'provider-native-map-conflict')
                group['parts'][part] = event
                if original:
                    group['originals'].add(original)
                matches = requested.intersection((event,original))
                if monitoring and own:
                    matches = {mapped for mapped in (event, original) if mapped}
                _require(not matches or own, 'provider-outgoing-direction-changed')
                group['outgoing'].update(matches)
            events = []
            for native, group in groups.items():
                # Parts are the SAME native message. Choosing its first mapped
                # part for navigation never breaks a tie between messages.
                event = group['parts'].get('') or group['parts'][sorted(group['parts'])[0]]
                if monitoring and group['originals']:
                    _require(len(group['originals']) == 1, 'provider-original-id-conflict')
                    event = next(iter(group['originals']))
                events.append({'native_id':native,'order':str(group['order']), 'timestamp':group['timestamp']//1000000,
                               'incoming':group['incoming'],'event_id':event,'outgoing_event_ids':sorted(group['outgoing'])})
            events.sort(key=lambda event:int(event['order']))
            if monitoring:
                has_more = len(raw_rows) > _LIMIT
                # SQL's raw-row limit can cut a native message into parts. The
                # boundary group is unavailable; retain only the strict suffix.
                if has_more and events:
                    lower = min(int(event['order']) for event in events)
                    events = [event for event in events if int(event['order']) > lower]
                ties = len(events) != len({event['order'] for event in events})
                inverted = any(a['timestamp'] > b['timestamp'] for a, b in zip(events, events[1:]))
                ready = not ties and not inverted and (bool(events) or not has_more)
                return {**empty, 'observed_complete': ready and not has_more,
                        'observed_window_complete': True, 'monitoring_ready': ready,
                        'reason': 'provider-order-tie' if ties else 'provider-time-order-inversion' if inverted else '' if ready else 'provider-window-boundary',
                        'bridge_epoch': proof['bridge_epoch'], 'bridge_epoch_kind': proof.get('bridge_epoch_kind'),
                        'has_more': has_more, 'events': events,
                        'outgoing_parts': {event['event_id']: [event['native_id']] for event in events if not event['incoming']}}
            parts = {event:sorted(native for native,group in groups.items() if event in group['outgoing']) for event in sorted(requested)}
            present = all(parts.values())
            earliest = min((int(event['order']) for event in events if event['outgoing_event_ids']), default=None)
            boundary = int(events[0]['order']) if events else None
            has_more = len(raw_rows) > _LIMIT
            # A bounded cut can split a native message. Require a strictly older
            # boundary message than every requested outgoing before attribution.
            covered = present and (not has_more or boundary is not None and boundary < earliest)
            suffix = [event['order'] for event in events if earliest is not None and int(event['order']) >= earliest]
            ties = len(suffix) != len(set(suffix))
            reason = 'provider-order-tie' if ties else 'provider-outgoing-outside-window' if not present else 'provider-window-boundary' if not covered else ''
            return {**empty, 'observed_complete':covered and not ties, 'observed_window_complete':True,
                    'reason':reason,'bridge_epoch':proof['bridge_epoch'],'bridge_epoch_kind':proof.get('bridge_epoch_kind'),
                    'has_more':has_more,'boundary_order':str(boundary) if boundary is not None else None,
                    'events':events,'outgoing_parts':parts}
        except ValueError as error:
            reason = str(error)
            return {**empty,'reason':reason if re.fullmatch(r'provider-[a-z-]{1,80}',reason) else 'provider-evidence-unavailable'}
        except (EvidenceError, OSError, KeyError, TypeError, AttributeError, RuntimeError,
                http.client.HTTPException, subprocess.SubprocessError, UnicodeError):
            return {**empty,'reason':'provider-evidence-unavailable'}
