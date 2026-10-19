"""Relay source-owned per-proposal outcomes; no send capability or message bodies."""
import hashlib
import json
import urllib.parse
import urllib.error

DECISION = 'com.jkali.proposal_decision'
STATUS = 'com.jkali.proposal_status'
CLAIM = 'com.jkali.proposal_claim'


def relay(u):
    local_room, master_room = u.meta_get('local_proposals_room'), u.meta_get('master_proposals_room')
    if not local_room or not master_room or not u.active_link_for_dispatch():
        return
    q = lambda value: urllib.parse.quote(value, safe='')
    state = u.local('GET', '/_matrix/client/v3/rooms/' + q(local_room) + '/state')
    if not isinstance(state, list):
        raise ValueError('Invalid proposal state')
    decisions = {e.get('state_key'): e for e in state if isinstance(e, dict) and e.get('type') == DECISION}
    # Durable paging prevents old queues from being starved by recent traffic.
    cursor = int(u.meta_get('proposal_status_cursor') or 0)
    rows = u.db.execute('SELECT rowid,master_event_id,local_event_id,outcome FROM proposal_map WHERE rowid>? ORDER BY rowid LIMIT 100', (cursor,)).fetchall()
    for rowid, master_id, local_id, outcome in rows:
        if not u.active_link_for_dispatch():
            return
        payload = {'schema': 1, 'proposal_event_id': master_id, 'state': {'sent': 'sent', 'ambiguous': 'ambiguous', 'attempted': 'ambiguous', 'refused': 'refused', 'scheduled': 'scheduled'}.get(outcome, 'pending')}
        crm = getattr(u, '_crm_batch', None)
        item = crm.item_for_event(master_id) if crm is not None else None
        if item is not None:
            payload['state'] = item['state']
            if item['approval_json']:
                approval = json.loads(item['approval_json'])
                payload.update(decision='accepted' if approval['request']['decision'] == 'approve' else 'declined', send_state='sent' if item['state'] == 'sent' else 'not_sent' if item['state'] == 'refused' else 'unknown')
        event = decisions.get(local_id)
        c = event.get('content') if event else None
        valid = event and event.get('sender') == u.cfg.local_user and isinstance(c, dict) and c.get('schema') == 1 and c.get('proposal_event_id') == local_id and c.get('decision') in ('accepted', 'declined') and c.get('send_state') in ('sending', 'sent', 'unknown', 'not_sent', 'none', 'scheduled')
        checkpoint = next((e.get('content') for e in state if e.get('type') == CLAIM and e.get('state_key') == local_id and e.get('sender') == u.cfg.local_user), None)
        claim_key = 'proposal_claim:' + hashlib.sha256((local_room + '\0' + (local_id or '')).encode()).hexdigest()
        if item is None and (valid or checkpoint or u.meta_get(claim_key)):
            # First observed user decision wins across browsers/tokens. The
            # remote checkpoint survives local DB loss; neither authorizes an
            # external send by this daemon. The winning browser alone sends.
            with u.db:
                u.db.execute("BEGIN IMMEDIATE")
                encoded = u.meta_get(claim_key)
                claim = json.loads(encoded) if encoded else checkpoint
                if claim is None:
                    if not isinstance(event.get('event_id'), str) or not event['event_id'].startswith('$'):
                        raise ValueError('Decision has no server event identity')
                    claim = {**c, 'decision_event_id': event['event_id']}
                if claim.get('schema') != 1 or claim.get('proposal_event_id') != local_id or claim.get('decision') not in ('accepted', 'declined') or not isinstance(claim.get('decision_event_id'), str):
                    raise ValueError('Invalid decision checkpoint')
                if checkpoint and checkpoint.get('decision_event_id') != claim['decision_event_id']:
                    raise ValueError('Decision checkpoint conflicts with local claim')
                u.meta_set(claim_key, json.dumps(claim, sort_keys=True))
            if checkpoint is None:
                u.local('PUT', '/_matrix/client/v3/rooms/' + q(local_room) + '/state/' + CLAIM + '/' + q(local_id), claim)
            selected = c if valid and c.get('claim_id') == claim['decision_event_id'] and c['decision'] == claim['decision'] else claim
            payload.update(decision=claim['decision'], send_state=selected['send_state'])
        elif valid:
            payload.update(decision=c['decision'], send_state=c['send_state'])
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        cache = 'proposal_status:' + hashlib.sha256((master_room + '\0' + master_id).encode()).hexdigest()
        if u.meta_get(cache) != digest:
            # Map rows survive account rebinding. Only publish into the room
            # containing the exact original manager event under this identity.
            try:
                original = u.master('GET', '/_matrix/client/v3/rooms/' + q(master_room) + '/event/' + q(master_id))
            except urllib.error.HTTPError as error:
                if error.code not in (403, 404):
                    raise
                original = {}
            if original.get('type') == 'com.jkali.proposal' and original.get('sender') == u.cfg.manager_mxid and original.get('event_id') == master_id:
                u.master('PUT', '/_matrix/client/v3/rooms/' + q(master_room) + '/state/' + STATUS + '/' + q(master_id), payload)
                u.meta_set(cache, digest)
        u.meta_set('proposal_status_cursor', str(rowid))
    if len(rows) < 100:
        u.meta_set('proposal_status_cursor', '0')
