// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { ROOMID_RE, api } from '../matrix/client.js';
import { logConsole, updateImsgCard } from './connections.js';
import { sanitizeLine } from './el.js';
import { openConversation } from './nav.js';
import { renderMessageEvent } from './render.js';
import { scheduleFeedRender } from './search.js';
import { SOURCES, handleMgmtEvent, reactToBotReply, sendCmd, startSync } from './sources.js';
import { S, convosBySource, feedManualHidden, feedModel } from '../state.js';

const MXID_RE = /^@[^:]+:localhost$/;      // shape gate for account_data-listed mxids
const SELF_MIN_ROOMS = 5;                  // heuristic threshold: min distinct rooms to claim a self-ghost

// ---- sidebar conversation lists (SEPARATE read path from the command loop) --
// One-shot filtered sync snapshot: room names, space memberships/children, and
// last message. This never feeds the console or the status parser (D-3).
async function fetchSnapshot() {
  const filter = encodeURIComponent(JSON.stringify({
    // Per-room account_data limited to m.tag (HF-9: read low-priority/archive
    // tags in the feed's own data path). Global account_data + presence stay
    // excluded; the sidebar parser ignores account_data, so this is additive.
    room: { timeline: { limit: 5 }, state: { lazy_load_members: true }, account_data: { types: ['m.tag'] } },
    presence: { types: [] }, account_data: { types: [] },
  }));
  return await api('GET', '/_matrix/client/v3/sync?timeout=0&filter=' + filter);
}
function parseSnapshot(data) {
  const rooms = {};
  const join = (data.rooms && data.rooms.join) || {};
  for (const rid of Object.keys(join)) {
    const r = join[rid];
    const info = { id: rid, name: null, isSpace: false, children: [], lastBody: null };
    // Read state from BOTH the sync `state` block AND `timeline` — a newer
    // space (e.g. iMessage) keeps its m.space.child / name in the timeline
    // window, not the `state` block, so state-only misses its children.
    // Functional only: children are still ROOMID_RE ∩ S.joinedSet-gated below (D-5).
    const stateEvents = ((r.state && r.state.events) || []).concat((r.timeline && r.timeline.events) || []);
    const seenChild = new Set();
    for (const e of stateEvents) {
      if (e.type === 'm.room.name' && e.state_key === '') info.name = e.content && e.content.name;
      if (e.type === 'm.room.create' && e.content && e.content.type === 'm.space') info.isSpace = true;
      if (e.type === 'm.space.child' && e.state_key && e.content && Object.keys(e.content).length) {
        if (!seenChild.has(e.state_key)) { seenChild.add(e.state_key); info.children.push(e.state_key); }
      }
    }
    const tl = (r.timeline && r.timeline.events) || [];
    for (let i = tl.length - 1; i >= 0; i--) {
      const e = tl[i];
      if (e.type === 'm.room.message' && e.content && typeof e.content.body === 'string') {
        info.lastBody = e.content.body; break;
      }
    }
    rooms[rid] = info;
  }
  return rooms;
}
// D-5: a space child is listed/navigable ONLY if it is itself a joined room.
function buildConvos(source, rooms) {
  // Match by name prefix: mautrix names its space "WhatsApp (+1...)", iMessage is exact.
  // (Purely which space feeds the tab; D-5's joined-rooms intersection below still
  // governs what is listed/navigable, so this is functional, not a security control.)
  const space = Object.values(rooms).find(r => r.isSpace && typeof r.name === 'string' && r.name.startsWith(source.spaceName));
  const convos = [];
  if (!space) return convos;
  for (const childId of space.children) {
    if (!rooms[childId]) continue;                 // not in joined set -> excluded
    if (!ROOMID_RE.test(childId)) continue;        // malformed id -> excluded
    const r = rooms[childId];
    convos.push({
      id: childId,
      title: sanitizeLine(r.name || childId),
      sub: sanitizeLine(r.lastBody || ''),
      sourceId: source.id,
      sourceLabel: source.label,
    });
  }
  return convos;
}
async function refreshConvos() {
  const rooms = parseSnapshot(await fetchSnapshot());
  S.joinedSet = new Set(Object.keys(rooms));         // authoritative joined set (D-5)
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    convosBySource[s.id] = buildConvos(s, rooms);
  }
}

// ===========================================================================
// Home feed (HM.2) — a SECOND, isolated read path (HF-1). Every function below
// touches only the feed model + render/nav; NONE references handleMgmtEvent,
// logConsole, reactToBotReply, sendCmd, updateImsgCard, or the status parser.
// Bridged portal content (names, last-message bodies) is read only here and in
// the existing snapshot path (parseSnapshot/buildConvos, already D-3-separate);
// it never reaches the command/console path. Nav is only via openConversation.
// ===========================================================================

// HF-4: compute a room's "last message" preview under a strict whitelist.
// Returns {body, ts} for a qualifying event, else null. Reads `body` ONLY,
// never `formatted_body`. Media msgtypes yield a STATIC label, never the
// bridged filename/body. Reactions/redactions/receipts/typing/state are not
// messages and return null (they never update lastBody/lastTs).
function feedPreviewFromEvent(ev) {
  if (!ev || ev.type !== 'm.room.message' || !ev.content) return null;
  let content = ev.content;
  const rel = content['m.relates_to'];
  if (rel && rel.rel_type === 'm.replace') {        // edit: read m.new_content.body only
    content = content['m.new_content'];
    if (!content) return null;
  }
  const mt = content.msgtype;
  let body;
  if ((mt === 'm.text' || mt === 'm.notice') && typeof content.body === 'string') {
    body = content.body;                            // text/notice: the real (sanitized-on-render) body
  } else if (mt === 'm.image') { body = 'Photo'; }  // media: static label ONLY (never the filename)
  else if (mt === 'm.video') { body = 'Video'; }
  else if (mt === 'm.audio') { body = 'Audio'; }
  else if (mt === 'm.file')  { body = 'File'; }
  else { return null; }                             // anything else is not a previewable message
  return { body, ts: typeof ev.origin_server_ts === 'number' ? ev.origin_server_ts : 0 };
}

// HF-5: keep only the LAST qualifying message in a room's timeline slice.
function feedLastPreview(room) {
  const tl = (room && room.timeline && room.timeline.events) || [];
  for (let i = tl.length - 1; i >= 0; i--) {
    const p = feedPreviewFromEvent(tl[i]);
    if (p) return p;
  }
  return null;
}

// ---- Self-identity detection (self-align) — build path. COSMETIC ONLY:
// nothing below sends, validates a room, or touches the from_me trust gate; it
// only populates S.selfMxids, which renderMessageEvent reads for alignment. No
// command/console symbol is referenced.

// (1) Authoritative source: the user-written account_data event
// com.jkali.self_identities ({ mxids: ["@whatsapp_lid-...:localhost", ...] }).
// Only the user's OWN S.token can write account_data, so this is trusted and not
// spoofable by a remote sender. Absent/404 -> empty set. Each listed string must
// look like a valid local mxid to be admitted.
async function fetchSelfIdentityAccountData() {
  const out = new Set();
  try {
    const data = await api('GET', '/_matrix/client/v3/user/' + encodeURIComponent(S.userId) +
      '/account_data/com.jkali.self_identities');
    const arr = data && Array.isArray(data.mxids) ? data.mxids : [];
    for (const m of arr) if (typeof m === 'string' && MXID_RE.test(m)) out.add(m);
  } catch (e) { /* 404 / absent / error -> treat as empty (additive union below) */ }
  return out;
}

// (2) Heuristic auto-derivation (generalizable, zero-setup): for EACH source,
// over that source's already-validated portal rooms (convosBySource — the same
// ROOMID_RE ∩ S.joinedSet ∩ source-space-child set buildConvos produced), tally
// per candidate sender the number of DISTINCT rooms it appears in as an
// m.room.message sender. The user themself is a participant in ALL their own
// conversations, so their own ghost tops the per-source count. Pick the top
// sender per source AS the user's own identity — but ONLY if it clears a
// threshold (>= SELF_MIN_ROOMS distinct rooms AND a strict plurality, >= 2x the
// runner-up) so a chatty single contact in a tiny account is not mislabeled;
// otherwise pick none for that source. Never counts S.userId (@jkali:localhost) or
// the source's own bot mxid. A misfire is at worst a wrong-side bubble — never a
// data/capability issue.
function deriveSelfMxidsHeuristic(join) {
  const winners = new Set();
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    const convos = convosBySource[s.id] || [];        // pre-validated portals only
    const counts = new Map();                          // sender mxid -> # distinct rooms of this source
    for (const c of convos) {
      const room = join[c.id];
      const tl = (room && room.timeline && room.timeline.events) || [];
      const inThisRoom = new Set();
      for (const ev of tl) {
        if (ev && ev.type === 'm.room.message' && typeof ev.sender === 'string') inThisRoom.add(ev.sender);
      }
      for (const sender of inThisRoom) {
        if (sender === S.userId || sender === s.botMxid) continue;   // never the user's mxid or the bot
        counts.set(sender, (counts.get(sender) || 0) + 1);
      }
    }
    let top = null, topN = 0, secondN = 0;
    for (const [sender, n] of counts) {
      if (n > topN) { secondN = topN; top = sender; topN = n; }
      else if (n > secondN) { secondN = n; }
    }
    if (top && topN >= SELF_MIN_ROOMS && topN >= 2 * secondN) winners.add(top); // strict-plurality gate
  }
  return winners;
}

// Rebuild S.selfMxids as the UNION of the two sources. Read-only; alignment only.
async function refreshSelfMxids(join) {
  const next = new Set();
  for (const m of await fetchSelfIdentityAccountData()) next.add(m);  // trusted (own account_data)
  for (const m of deriveSelfMxidsHeuristic(join)) next.add(m);         // cosmetic heuristic (thresholded)
  S.selfMxids = next;
}

// Seed / re-validate the feed model from the SAME validated snapshot path the
// sidebar uses (ROOMID_RE ∩ S.joinedSet ∩ known-source-space child, via
// buildConvos). One record per room; dedup a doubly-listed room by first
// SOURCES order (HF-6). Merge preserves live-fresher previews on re-validation.
async function seedFeed() {
  const data = await fetchSnapshot();
  const rooms = parseSnapshot(data);
  S.joinedSet = new Set(Object.keys(rooms));          // authoritative joined set (D-5)
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    convosBySource[s.id] = buildConvos(s, rooms);   // reuse the validated builder
  }
  const join = (data.rooms && data.rooms.join) || {};
  // HF-9: rebuild the low-priority set from per-room m.tag account_data. A room
  // is low-priority/archived if its tags include m.lowpriority. Read only here,
  // in the feed data path; never routed to the console/status parser.
  const low = new Set();
  for (const rid of Object.keys(join)) {
    const ad = join[rid] && join[rid].account_data;
    for (const e of ((ad && ad.events) || [])) {
      if (e.type === 'm.tag' && e.content && e.content.tags &&
          Object.prototype.hasOwnProperty.call(e.content.tags, 'm.lowpriority')) {
        low.add(rid);
      }
    }
  }
  S.feedLowPriority = low;
  await feedRefreshMuted();                          // HF-9: refresh muted push-rule set
  const seen = new Set();
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    for (const c of (convosBySource[s.id] || [])) {
      if (seen.has(c.id)) continue;                 // HF-6: first SOURCES order wins
      seen.add(c.id);
      const p = feedLastPreview(join[c.id]);        // HF-4 whitelist
      const existing = feedModel.get(c.id);
      if (existing) {
        existing.name = c.title;                    // refresh name; keep original attribution
        if (p && p.ts > existing.lastTs) { existing.lastBody = p.body; existing.lastTs = p.ts; }
      } else {
        feedModel.set(c.id, {
          id: c.id, name: c.title,                  // c.title already sanitizeLine'd by buildConvos
          lastBody: p ? p.body : '', lastTs: p ? p.ts : 0, sourceId: s.id,
        });
      }
    }
  }
  for (const rid of [...feedModel.keys()]) {         // drop rooms no longer validated
    if (!seen.has(rid)) feedModel.delete(rid);
  }
  await refreshSelfMxids(join);                        // self-align: rebuild self identities (alignment only)
}

// HF-9: derive the muted-room set from the user's own push rules. A global
// `room` rule mutes the room whose id equals its rule_id when its actions carry
// no notify action (actions is [], or ['dont_notify'], or otherwise lacks the
// 'notify' string). Reading push rules adds no capability (own account data)
// and stays in the feed data path. On error the previous set is kept.
async function feedRefreshMuted() {
  try {
    const pr = await api('GET', '/_matrix/client/v3/pushrules/');
    const roomRules = (pr && pr.global && Array.isArray(pr.global.room)) ? pr.global.room : [];
    const set = new Set();
    for (const rule of roomRules) {
      if (!rule || typeof rule.rule_id !== 'string') continue;
      if (rule.enabled === false) continue;          // a disabled rule does not mute
      const acts = Array.isArray(rule.actions) ? rule.actions : [];
      const notifies = acts.some(a => a === 'notify');
      if (!notifies) set.add(rule.rule_id);          // "no notify action" -> muted
    }
    S.feedMuted = set;
  } catch (e) { /* keep the previous muted set */ }
}

// HF-9: a room is hidden from the default Home list if it is low-priority/
// archived, muted, or in the client-managed manual-hidden set.
function feedIsHidden(roomId) {
  return feedManualHidden.has(roomId) || S.feedLowPriority.has(roomId) || S.feedMuted.has(roomId);
}

// HF-9: manual hide sets the m.lowpriority room tag. The roomId is used to build
// the tag URL ONLY after it is validated ∈ feedModel ∩ S.joinedSet AND matches
// ROOMID_RE — never a typed/bridged value. Goes through api() (same-origin,
// bearer). Optimistically updates local sets + re-renders so the row leaves the
// list at once; the next seed reads the tag back authoritatively.
async function feedHideRoom(roomId) {
  if (!feedModel.has(roomId) || !S.joinedSet.has(roomId) || !ROOMID_RE.test(roomId)) return;
  try {
    await api('PUT', '/_matrix/client/v3/user/' + encodeURIComponent(S.userId) +
      '/rooms/' + encodeURIComponent(roomId) + '/tags/m.lowpriority', { order: 0.5 });
    feedManualHidden.add(roomId);
    S.feedLowPriority.add(roomId);
    scheduleFeedRender();
  } catch (e) { /* leave the row in place on failure */ }
}
// HF-9: unhide removes the m.lowpriority tag (DELETE) and clears the manual set.
// A still-muted room stays hidden (mute is a separate mechanism). Same roomId
// validation as hide.
async function feedUnhideRoom(roomId) {
  if (!feedModel.has(roomId) || !ROOMID_RE.test(roomId)) return;
  try {
    await api('DELETE', '/_matrix/client/v3/user/' + encodeURIComponent(S.userId) +
      '/rooms/' + encodeURIComponent(roomId) + '/tags/m.lowpriority');
    feedManualHidden.delete(roomId);
    S.feedLowPriority.delete(roomId);
    scheduleFeedRender();
  } catch (e) { /* leave state unchanged on failure */ }
}

// HF-3: pick up genuinely-new portals only via a DEBOUNCED re-validation,
// never by trusting the live stream to add a room.
function scheduleFeedRevalidate() {
  if (S.feedRevalTimer) return;
  S.feedRevalTimer = setTimeout(async () => {
    S.feedRevalTimer = null;
    try { await seedFeed(); scheduleFeedRender(); } catch (e) { /* keep current model */ }
  }, 4000);
}

// The live feed handler. Updates/bubbles ONLY roomIds already in the validated
// feed model; any other roomId is ignored (never added from the live stream).
function feedIngest(data) {
  const join = (data.rooms && data.rooms.join) || {};
  let changed = false, sawUnknown = false;
  for (const rid of Object.keys(join)) {
    if (!feedModel.has(rid)) { sawUnknown = true; continue; }  // HF-3: ignore unknown room ids
    const p = feedLastPreview(join[rid]);            // HF-4/HF-5: last qualifying message only
    if (!p) continue;
    const rec = feedModel.get(rid);
    if (p.ts >= rec.lastTs) { rec.lastBody = p.body; rec.lastTs = p.ts; changed = true; }
  }
  if (sawUnknown) scheduleFeedRevalidate();
  if (changed) scheduleFeedRender();
}

// HF-1/HF-5: the isolated feed /sync long-poll. Its handler (feedIngest) has no
// lexical path to any command/console function. Small timeline limit, ~25000
// long-poll timeout + 3s error backoff, mirroring startSync (but with its own
// S.feedSince/S.feedRunning). No message bodies are logged anywhere.
async function startFeedSync() {
  if (S.feedRunning) return;
  S.feedRunning = true;
  S.feedSince = null;
  while (S.feedRunning && S.token) {
    try {
      const filter = encodeURIComponent(JSON.stringify({
        room: { timeline: { limit: 5 }, state: { types: [] } },
        presence: { types: [] }, account_data: { types: [] },
      }));
      const q = '/_matrix/client/v3/sync?timeout=25000&filter=' + filter +
        (S.feedSince ? '&since=' + encodeURIComponent(S.feedSince) : '');
      const data = await api('GET', q);
      S.feedSince = data.next_batch;
      feedIngest(data);
    } catch (e) {
      if (!S.token) return;
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}

export { MXID_RE, SELF_MIN_ROOMS, fetchSnapshot, parseSnapshot, buildConvos, refreshConvos, feedPreviewFromEvent, feedLastPreview, fetchSelfIdentityAccountData, deriveSelfMxidsHeuristic, refreshSelfMxids, seedFeed, feedRefreshMuted, feedIsHidden, feedHideRoom, feedUnhideRoom, scheduleFeedRevalidate, feedIngest, startFeedSync };
