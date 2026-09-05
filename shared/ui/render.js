// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { feedPreviewFromEvent } from '../model/message_preview.js';
import { api } from '../matrix/client.js';
import { $, el, sanitize, sanitizeLine, txn, appendLinkified } from './el.js';
import { IMSG_BOT_MXID } from './sources.js';
import { S, convoNamePending, convoNames, convoSeen } from '../state.js';

// A short local wall-clock time for a bubble's decorative .when node.
function convoTime(ts) {
  if (typeof ts !== 'number' || !isFinite(ts)) return '';
  try { return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
  catch (e) { return ''; }
}

// Decorative sender label. mxid localpart, sanitized; NEVER used for ownership
// (CV-R1 derives sent/recv from ev.sender === S.userId only).
function convoLocalpart(mxid) {
  const s = typeof mxid === 'string' ? mxid : '';
  const colon = s.indexOf(':');
  const lp = (colon > 0 ? s.slice(0, colon) : s).replace(/^@/, '');
  return sanitizeLine(lp) || sanitizeLine(s) || 'unknown';
}
// Cached display-name lookup (own account scope). Returns a synchronous best
// guess (cache or localpart) and, on a miss, fetches the profile once and
// patches any existing recv bubbles for that sender in place. Decorative only.
function convoDisplayName(mxid) {
  if (convoNames.has(mxid)) return convoNames.get(mxid);
  convoFetchName(mxid);
  return convoLocalpart(mxid);
}
async function convoFetchName(mxid) {
  if (typeof mxid !== 'string' || !mxid) return;
  if (convoNames.has(mxid) || convoNamePending.has(mxid)) return;
  convoNamePending.add(mxid);
  try {
    const data = await api('GET', '/_matrix/client/v3/profile/' + encodeURIComponent(mxid) + '/displayname');
    const name = sanitizeLine(data && data.displayname) || convoLocalpart(mxid);
    convoNames.set(mxid, name);
    const box = $('convo-messages');
    if (box) {
      for (const b of box.children) {
        if (b.dataset && b.dataset.sender === mxid) {
          const who = b.querySelector('.who');
          if (who) who.textContent = name;         // textContent only — no HTML sink
        }
      }
    }
  } catch (e) {
    convoNames.set(mxid, convoLocalpart(mxid));    // cache the fallback to stop refetching
  } finally {
    convoNamePending.delete(mxid);
  }
}

// CV-R4: the SINGLE shared resolver for BOTH history (/messages) and live
// (/sync) events — mirrors feedPreviewFromEvent EXACTLY. Returns {text, kind}
// for a renderable message, else null (reactions/redactions/state/edits-of-
// nonmessage are skipped). Reads content.body ONLY; NEVER formatted_body.
function convoResolveContent(ev) {
  if (!ev || ev.type !== 'm.room.message' || !ev.content) return null;
  let content = ev.content;
  const rel = content['m.relates_to'];
  if (rel && rel.rel_type === 'm.replace') {        // edit: read m.new_content only
    content = content['m.new_content'];
    if (!content) return null;
  }
  const mt = content.msgtype;
  if ((mt === 'm.text' || mt === 'm.notice') && typeof content.body === 'string') {
    return { text: sanitize(content.body), kind: mt === 'm.notice' ? 'notice' : 'text' };
  }
  if (mt === 'm.image') return { text: '📷 Photo', kind: 'media' };  // static label, never the filename
  if (mt === 'm.video') return { text: '🎥 Video', kind: 'media' };
  if (mt === 'm.audio') return { text: '🎵 Audio', kind: 'media' };
  if (mt === 'm.file')  return { text: '📎 File',  kind: 'media' };
  return null;                                       // not a previewable message
}

// CV-R4: the SINGLE shared renderer. Appends at most one bubble to
// #convo-messages, deduped by event_id (and by 'txn:'+transaction_id so the
// server echo of an optimistic bubble does not double). CV-R1: ownership from
// ev.sender === S.userId (mxid) ONLY. CV-R2: who / body / when are three separate
// el() nodes; the body is sanitize()'d in its own node, never concatenated into
// chrome. CV-D1: caps the list at 200 bubbles (drops oldest).
function renderMessageEvent(ev) {
  const box = $('convo-messages');
  if (!box) return;
  const resolved = convoResolveContent(ev);
  if (!resolved) return;                            // skip: reaction/redaction/state/etc.

  const eid = typeof ev.event_id === 'string' ? ev.event_id : null;
  const txnId = ev.unsigned && typeof ev.unsigned.transaction_id === 'string'
    ? ev.unsigned.transaction_id : null;
  if (eid && convoSeen.has(eid)) return;            // already rendered this event
  if (txnId && convoSeen.has('txn:' + txnId)) {     // echo of our optimistic bubble
    if (eid) convoSeen.add(eid);
    return;
  }
  if (eid) convoSeen.add(eid);
  if (txnId) convoSeen.add('txn:' + txnId);

  // CV-R1: own message => right-aligned "You". TRUE when EITHER (a) ev.sender is
  // us (sent via this bridge), OR (b) a TRUSTED from_me marker: the daemon stamps
  // com.jkali.from_me on messages the user sent from the iMessage app, posted
  // ONLY as @imessagebot. ANTI-SPOOF: the marker is honored ONLY when ev.sender
  // is exactly our own appservice bot (IMSG_BOT_MXID). A ghost (@imessage_*),
  // another bridge's sender, or a remote contact carrying the flag is IGNORED
  // (treated as received/left-aligned) — a remote party can never render as "You".
  const trustedFromMe = !!(ev.content && ev.content['com.jkali.from_me'] === true
                           && ev.sender === IMSG_BOT_MXID);
  // Self-align (cosmetic): a sender that is one of the user's OWN bridge
  // identities (their own ghost, e.g. their WhatsApp @whatsapp_lid-...:localhost)
  // renders as "You"/right-aligned even with no per-message flag. S.selfMxids is
  // built ONLY from trusted own account_data + a thresholded cosmetic heuristic
  // (never from this event) and grants no capability; it does NOT relax the
  // iMessage from_me trust gate above, which stays keyed to IMSG_BOT_MXID.
  const sent = ev.sender === S.userId || trustedFromMe || S.selfMxids.has(ev.sender);
  let cls = 'msg ' + (sent ? 'sent' : 'recv');
  if (resolved.kind === 'media') cls += ' media';
  else if (resolved.kind === 'notice') cls += ' notice';
  const bubble = el('div', cls);
  if (eid) bubble.dataset.eventId = eid;
  if (txnId) bubble.dataset.txnId = txnId;

  // CV-R2: three separate nodes. who is decorative (sanitizeLine); "You" for own.
  const who = el('div', 'who');
  if (sent) { who.textContent = 'You'; }
  else { bubble.dataset.sender = ev.sender; who.textContent = convoDisplayName(ev.sender); }
  bubble.appendChild(who);
  // Sanitized text/label in its own node (CV-R2). Text/notice bodies get
  // conservative linkification (explicit http(s) only, link text == href —
  // see el.js appendLinkified); media labels and other kinds stay plain.
  const bodyNode = el('div', 'body');
  if (resolved.kind === 'text' || resolved.kind === 'notice') appendLinkified(bodyNode, resolved.text);
  else bodyNode.textContent = resolved.text;
  bubble.appendChild(bodyNode);
  bubble.appendChild(el('div', 'when', convoTime(ev.origin_server_ts)));
  box.appendChild(bubble);

  while (box.childElementCount > 200) {             // CV-D1: bounded, drop oldest
    const first = box.firstElementChild;
    if (!first) break;
    if (first.dataset) {
      if (first.dataset.eventId) convoSeen.delete(first.dataset.eventId);
      if (first.dataset.txnId) convoSeen.delete('txn:' + first.dataset.txnId);
    }
    box.removeChild(first);
  }
}

export { convoTime, convoLocalpart, convoDisplayName, convoFetchName, convoResolveContent, renderMessageEvent };
