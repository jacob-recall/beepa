// apps/master/main.js — read-only manager console (PLAN-MASTER-SYNC.md §6.4).
//
// Reuses the genuinely app-agnostic shared/ leaves: shared/matrix/client.js
// (transport — api()/configureMatrixBase(), no send-side effects of its own)
// and shared/ui/el.js (DOM + sanitize helpers, zero transitive imports) and
// shared/state.js (the plain S.token/S.userId session slot the transport
// reads). Deliberately does NOT import shared/ui/render.js, rows.js, nav.js,
// chat.js, search.js, sources.js, or connections.js: every one of those files
// is import-chained into shared/ui/chat.js's sendConvoMessage and/or
// shared/ui/sources.js's bridge-command sender (sendCmd) at module-evaluation
// time (ES module imports execute eagerly, whichever named export you take),
// so there is no way to import from that graph without the send path
// becoming *present code* in this app's bundle. PLAN-MASTER-SYNC.md is
// explicit that here read-only must be "absent code, not a hidden button",
// so this file re-implements the small, already-duplicated-per-read-path
// patterns those modules use (content whitelist, recency sort, a tailing
// long-poll) locally instead. See apps/master/CLAUDE.md for the full account.
//
// The app-local modules it imports are ./invites.js (identity/trust predicates
// that gate auto-join and rendering) and ./hidden.js (per-browser hide-teammate
// filter — convenience UI state, never authorization). Both are pure leaves so
// a unit test can hold them still; this file only performs what they decide.
//
// NO composer, NO send call, anywhere in this file.

import { api, configureMatrixBase, setOnUnauthorized, ROOMID_RE, MXC_RE } from '../../shared/matrix/client.js';
import { $, el, sanitize, sanitizeLine } from '../../shared/ui/el.js';
import { S } from '../../shared/state.js';
import {
  localpart, spaceLabelFor, invitesToJoin, acceptedSpaces, verifiedChildIds, ROOM_SHAPE_RE,
} from './invites.js';
import { parseHidden, dumpHidden, hide, unhide, visibleFeed, visibleContacts, visibleUsers } from './hidden.js';

// Per-browser hidden-teammate labels (localStorage). Convenience only.
const HIDDEN_KEY = 'beepa_hidden_teammates';

// The MASTER homeserver base (same origin the transport is pointed at below).
// Authenticated media (Synapse default) cannot be fetched by a bare <img src>
// — the token must ride in an Authorization header — so real media is fetched
// as bytes with S.token and shown via an object URL (CSP allows img/media blob:).
const MASTER_BASE = 'http://127.0.0.1:8018';

// The master enroll/admin service (master/enroll.py serve). Manager-authenticated
// POSTs: /admin/add-teammate and /admin/delete-teammate. Neither is a Matrix
// send path nor a proposal. The service verifies the caller is @manager:master
// before doing anything. CSP connect-src is extended by exactly this origin.
const ENROLL_BASE = 'http://127.0.0.1:8019';

// The two custom room-state types the uplink stamps on a teammate's dedicated
// "Contacts" room (agents/uplink/uplink.py): CONTACTS_MARKER on the room itself
// (discovered exactly like the com.jkali.proposals marker), and one
// CONTACT_STATE_TYPE STATE event per shared address-book handle. Read-only here:
// master-side power levels pin @manager to 0 with state_default 100, so the
// manager can never write a com.jkali.contact — these values are pure data.
const CONTACTS_MARKER = 'com.jkali.contacts';
const CONTACT_STATE_TYPE = 'com.jkali.contact';

// Identifier shape gates for a PERSON-targeted proposal. A contact handle may
// only be proposed to when it is an E.164 phone number OR a strict email —
// exactly the same validate-before-write discipline submitProposal applies to a
// target_room with ROOM_SHAPE_RE. The identifier is inert: the master only
// records it in a com.jkali.proposal; the teammate's own guarded local send path
// re-validates it before anything is ever sent.
// Deliberately a LOCAL copy, NOT imported from shared/ui/sources.js: this app
// must never import that module graph (it is import-chained to the send path —
// see this file's header + apps/master/CLAUDE.md), so the master stays
// send-incapable by absent code. A one-line regex duplicated here is the right
// trade to preserve that invariant; do not "dedupe" it by importing sources.js.
const E164_RE = /^\+[1-9]\d{6,14}$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ---- point the shared transport at the MASTER homeserver, not the user hub's.
// Own compose project (matrix-master), own port (127.0.0.1:8018), own
// server_name ("master") — see master/docker-compose.master.yml + provision.sh.
// This call only affects THIS page's module instance of shared/matrix/client.js
// (each document gets its own ES module graph), so apps/user is unaffected.
configureMatrixBase({ csBase: 'http://127.0.0.1:8018', serverName: 'master' });

// ---- master-local state (deliberately separate from shared/state.js's S,
// whose other fields — qr, activeSettingsSource, feedLowPriority, ... — are
// user-hub/bridge concepts that do not apply here) ----
const MS = {
  rooms: {},           // roomId -> {id, name, isSpace, children, sourceId, lastBody, lastTs, userLabel}
  byUser: new Map(),   // teammate label -> [{id, title, preview, lastTs, sourceId, userLabel}]
  feed: [],            // flattened rows across all teammates, recency-sorted
  proposalsByUser: new Map(),  // teammate label -> their proposals room id (write target)
  proposalsRoomSet: new Set(), // every discovered proposals room id (send-guard allowlist)
  contacts: [],        // flattened shared address-book handles across every teammate
  openContact: null,   // the contact whose person-targeted composer is currently open
  activeView: 'recent',
  openRoomId: null,
  openRoomUser: null,
  openRoomSourceId: null,  // the open mirror room's com.jkali.source, for the per-bubble mini platform badge
  openMirrorOf: null,  // the open mirror room's teammate-local room id (proposal target)
  // The {mirror room, teammate label, proposals room, target room} tuple PINNED
  // when a room was opened. submitProposal re-asserts all four against the
  // current snapshot instead of re-resolving them, so a mid-session revocation
  // or label change refuses the write rather than silently redirecting it.
  openProposalCtx: null,
  lastDayKey: null,    // last rendered day-divider key, reset per room open (see renderBubble)
  tailRunning: false,
  tailSince: null,
  pollTimer: null,
  // Join backpressure: room ids whose /join returned a hard (non-429 4xx)
  // failure this session are never retried — a permanently-refused invite must
  // not turn the 20s refresh into an unbounded request loop.
  joinFailed: new Set(),
  // How many items the identity gate hid on the last refresh (surfaced in the
  // sidebar, so a verification failure is visible instead of silent data loss).
  skippedUnverified: { spaces: 0, children: 0 },
  hidden: new Set(),   // teammate labels this browser omits from lists
};

setOnUnauthorized(forgetSession);

// ===========================================================================
// Snapshot: one filtered /sync per refresh, across every room the manager has
// joined (all teammates' spaces + their mirror rooms). Mirrors the shape of
// shared/ui/account-data.js's fetchSnapshot/parseSnapshot (same filter idiom:
// small timeline window, full state, lazy-loaded members) but reads the
// mirror-room-specific state/content fields §8.2 defines instead of bridge
// concepts (SOURCES/mgmt rooms) that do not exist on the master.
// ===========================================================================
async function fetchSnapshot() {
  const filter = encodeURIComponent(JSON.stringify({
    room: { timeline: { limit: 5 }, state: { lazy_load_members: true } },
    presence: { types: [] }, account_data: { types: [] },
  }));
  return await api('GET', '/_matrix/client/v3/sync?timeout=0&filter=' + filter);
}

// CV-R4-equivalent content whitelist (mirrors shared/ui/render.js's
// convoResolveContent exactly in shape): reads content.body ONLY, never
// formatted_body; media msgtypes get a static label, never the bridged
// filename. Kept local (see file header) rather than importing render.js.
function resolveMirrorContent(ev) {
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
  const MEDIA_LABEL = { 'm.image': '📷 Photo', 'm.video': '🎥 Video', 'm.audio': '🎵 Audio', 'm.file': '📎 File' };
  if (mt in MEDIA_LABEL) {
    // v1.5: render the real bytes when the uplink re-uploaded to the master
    // media store (placeholder flag false/absent AND a valid master mxc). When
    // the placeholder flag is set — or no valid mxc is present — keep the v1
    // static label and never a filename. mxc is validated by the SHARED MXC_RE.
    const placeholder = content['com.jkali.media_placeholder'] === true;
    const url = typeof content.url === 'string' ? content.url : null;
    if (!placeholder && url && MXC_RE.test(url)) {
      return { text: MEDIA_LABEL[mt], kind: 'media', mt, mxc: url };
    }
    return { text: MEDIA_LABEL[mt], kind: 'media' };
  }
  return null;
}

// Build the authenticated master media-download URL for a validated mxc. Returns
// null for anything MXC_RE rejects (defense in depth — resolveMirrorContent
// already validated, but never concatenate an unvalidated id into a URL).
function mxcDownloadUrl(mxc) {
  const m = MXC_RE.exec(mxc);
  if (!m) return null;
  return MASTER_BASE + '/_matrix/client/v1/media/download/'
    + encodeURIComponent(m[1]) + '/' + encodeURIComponent(m[2]);
}

// Fetch the media bytes (Authorization: Bearer) and swap the label node for a
// real media element via an object URL. On any failure the label stays — read
// path only, no send. img/video/audio/anchor .src/.href take a blob: URL string
// (not a Trusted-Types script sink); CSP grants img-src/media-src blob:.
async function loadMediaInto(bodyNode, resolved) {
  const url = mxcDownloadUrl(resolved.mxc);
  if (!url) return;
  try {
    const res = await fetch(url, { headers: S.token ? { Authorization: 'Bearer ' + S.token } : {} });
    if (!res.ok) return;
    const obj = URL.createObjectURL(await res.blob());
    let node;
    if (resolved.mt === 'm.image') {
      node = el('img', 'media-el'); node.src = obj; node.alt = resolved.text;
    } else if (resolved.mt === 'm.video') {
      node = el('video', 'media-el'); node.src = obj; node.controls = true;
    } else if (resolved.mt === 'm.audio') {
      node = el('audio', 'media-el'); node.src = obj; node.controls = true;
    } else {
      node = el('a', 'media-file', resolved.text); node.href = obj; node.download = '';
    }
    bodyNode.replaceChildren(node);
  } catch (e) { /* keep the static label on any error */ }
}

// PLAN §8.2/§11: sort/display by com.jkali.origin_ts (the ORIGINAL message
// time the uplink stamped), not origin_server_ts — a normal client cannot
// backdate server timestamps, so historical backfill posted in one burst
// would otherwise cluster at sync-time instead of showing true history order.
function mirrorTs(ev) {
  const c = ev && ev.content;
  const ots = c && typeof c['com.jkali.origin_ts'] === 'number' ? c['com.jkali.origin_ts'] : null;
  return ots != null ? ots : (typeof ev.origin_server_ts === 'number' ? ev.origin_server_ts : 0);
}

// Display-only name for an mxid. The trust predicate (./invites.js localpart)
// returns null for anything it cannot parse — that null is NEVER papered over
// with a sentinel on the decision paths; only here, where the value is about to
// become text on screen, does it fall back to the raw id.
function displayNameForMxid(mxid) {
  return sanitizeLine(localpart(mxid) || (typeof mxid === 'string' ? mxid : ''));
}

function parseSnapshot(data) {
  const rooms = {};
  const join = (data.rooms && data.rooms.join) || {};
  for (const rid of Object.keys(join)) {
    const r = join[rid];
    const info = { id: rid, name: null, isSpace: false, children: [], sourceId: null,
                   lastBody: '', lastTs: 0, mirrorOf: null, isProposals: false,
                   profileId: null, profileDisplayName: null, createSender: null,
                   isContacts: false, contacts: [] };
    // State from BOTH the `state` block and `timeline` (a newer space's
    // create/name/child events can still be in the timeline window).
    const stateEvents = ((r.state && r.state.events) || []).concat((r.timeline && r.timeline.events) || []);
    const seenChild = new Set();
    for (const e of stateEvents) {
      if (e.type === 'm.room.name' && e.state_key === '') info.name = e.content && e.content.name;
      if (e.type === 'm.room.create' && e.content && e.content.type === 'm.space') info.isSpace = true;
      // The create event's SERVER-STAMPED sender is this room's identity: the
      // account that created it. It is the single source both the auto-join
      // gate and the render gate bind a teammate label to (./invites.js).
      // A client cannot forge it, and it is the one identity field Matrix's
      // stripped invite state also carries — so the same predicate works
      // before and after joining.
      if (e.type === 'm.room.create' && e.state_key === '' && info.createSender === null
          && typeof e.sender === 'string') {
        info.createSender = e.sender;
      }
      // The uplink stamps the teammate's REAL local room id into the mirror
      // room's create content (creation_content.com.jkali.mirror_of). It is the
      // target_room a proposal must carry so the teammate knows which of their
      // own conversations the suggestion is for. Read-only value (server state).
      if (e.type === 'm.room.create' && e.content && typeof e.content['com.jkali.mirror_of'] === 'string') {
        info.mirrorOf = e.content['com.jkali.mirror_of'];
      }
      // A room marked com.jkali.proposals is this teammate's dedicated proposal
      // room — the ONLY room this app ever writes into, and only a
      // com.jkali.proposal event (see submitProposal). Never a mirror room.
      if (e.type === 'com.jkali.proposals' && e.state_key === '') info.isProposals = true;
      // A room marked com.jkali.contacts is this teammate's dedicated shared
      // address-book room — read-only here (never written to). Same discovery
      // shape as the proposals marker above.
      if (e.type === CONTACTS_MARKER && e.state_key === '') info.isContacts = true;
      // Each shared contact handle rides as a com.jkali.contact STATE event
      // (state_key = sha1(source|network_id)). Collected raw from room state
      // only (never message content) and sanitized at the render call site.
      if (e.type === CONTACT_STATE_TYPE && typeof e.state_key === 'string' && e.state_key
          && e.content && typeof e.content === 'object') {
        info.contacts.push(e.content);
      }
      // §8.2: the uplink tags each mirror room's platform at creation as a
      // room STATE event (not per-account_data, so it is visible to @manager
      // — a different account than the room's creator) so the master app can
      // show the platform badge.
      if (e.type === 'com.jkali.source' && e.state_key === '' && e.content && typeof e.content.source === 'string') {
        info.sourceId = e.content.source;
      }
      // agents/uplink/uplink.py's create_mirror stamps this state event ONLY
      // when the mirror is a member of a SHARED contact profile (§Phase 5
      // contacts-core report): {id, displayName}. Grouping key for "one person,
      // many platforms" below — never mutated here, read-only room state.
      if (e.type === 'com.jkali.profile' && e.state_key === '' && e.content
          && typeof e.content.id === 'string' && e.content.id) {
        info.profileId = sanitizeLine(e.content.id);
        info.profileDisplayName = typeof e.content.displayName === 'string'
          ? sanitizeLine(e.content.displayName) : null;
      }
      if (e.type === 'm.space.child' && e.state_key && e.content && Object.keys(e.content).length) {
        if (!seenChild.has(e.state_key)) { seenChild.add(e.state_key); info.children.push(e.state_key); }
      }
    }
    const tl = (r.timeline && r.timeline.events) || [];
    for (const ev of tl) {
      const resolved = resolveMirrorContent(ev);
      if (!resolved) continue;
      const ts = mirrorTs(ev);
      if (ts >= info.lastTs) { info.lastBody = resolved.text; info.lastTs = ts; }
    }
    rooms[rid] = info;
  }
  return rooms;
}

// Teammate spaces are named "space:<localpart>" (master/provision.sh) — but the
// NAME alone proves nothing: any teammate can create a room and name it
// anything. The rail is therefore built only from spaces whose label is proven
// by their own creator (acceptedSpaces), and inside a verified space only from
// children whose OWN creator is that same teammate. This is the render half of
// the identity bind the auto-join gate applies to invites; enforcing it twice
// means a mislabeled space is refused whichever way it arrived, including a
// room the manager was already joined to before this gate existed.
//
// Rooms discovered but not joined yet (a pending invite) are simply not listed;
// that is a normal transient state, not a verification failure, and is not
// counted as "hidden" below.
// Normalize one com.jkali.contact STATE content into the flat shape the
// contacts view consumes, or null to DROP it: tombstones (deleted:true) and
// handles without a network_id never render. person_id is the join key to the
// mirror rooms' com.jkali.profile stamp (same person, many platforms); a null
// person_id lists the handle ungrouped. No DOM/sanitize here — this is pure
// data shaping; sanitizeLine is applied at each render call site.
function parseContact(content, label) {
  if (!content || typeof content !== 'object' || content.deleted === true) return null;
  const network_id = typeof content.network_id === 'string' ? content.network_id : '';
  if (!network_id) return null;
  return {
    label,
    source: typeof content.source === 'string' ? content.source : null,
    network_id,
    kind: typeof content.kind === 'string' ? content.kind : null,
    display_name: typeof content.display_name === 'string' ? content.display_name : '',
    person_id: (typeof content.person_id === 'string' && content.person_id) ? content.person_id : null,
    person_display: (typeof content.person_display === 'string' && content.person_display)
      ? content.person_display : '',
  };
}

function buildByUser(rooms) {
  const byUser = new Map();
  const proposalsByUser = new Map();
  const proposalsRoomSet = new Set();
  const proposalCandidates = new Map();   // label -> [roomId] (smallest id wins)
  const allContacts = [];                 // flattened shared handles across teammates
  const accepted = acceptedSpaces(rooms);
  const skipped = { spaces: 0, children: 0 };
  for (const r of Object.values(rooms)) if (r.isSpace) skipped.spaces++;
  skipped.spaces -= accepted.length;      // every space the identity gate refused

  for (const { label, space } of accepted) {
    // Same-label spaces MERGE into one rail entry (never overwrite): a second
    // verified space for the same teammate adds its conversations rather than
    // replacing the first one's.
    const convos = byUser.get(label) || [];
    for (const childId of space.children.slice().sort()) {
      const r = rooms[childId];
      if (!r) continue;                              // not in the joined set -> excluded
      if (!ROOMID_RE.test(childId)) { skipped.children++; continue; }   // malformed id
      // IDENTITY CHECK (render side): the child must have been created by the
      // same teammate whose label this space proved. Without it, @bob:master
      // could space-link rooms he controls under jkali's verified space.
      if (localpart(r.createSender) !== label) { skipped.children++; continue; }
      if (r.isSpace) { skipped.children++; continue; } // a space is never a convo
      if (!r.userLabel) r.userLabel = label;          // first verified claim wins
      // A proposals room is the write channel, not a conversation. It qualifies
      // as a write target ONLY if it is not also a mirror room: a mirror stamped
      // with the com.jkali.proposals marker must never become the destination of
      // the manager's proposal writes (it would put manager-authored text into a
      // conversation mirror). Such a room stays an ordinary read-only convo.
      if (r.isProposals && !r.mirrorOf) {
        const list = proposalCandidates.get(label) || [];
        list.push(childId);
        proposalCandidates.set(label, list);
        proposalsRoomSet.add(childId);
        continue;
      }
      // A contacts room is a read-only address-book source, never a conversation
      // and never a write target. Same "not also a mirror" guard as proposals:
      // its shared handles fold into the flat contacts list (tombstones dropped),
      // tagged with this verified teammate label.
      if (r.isContacts && !r.mirrorOf) {
        for (const cc of r.contacts) {
          const parsed = parseContact(cc, label);
          if (parsed) allContacts.push(parsed);
        }
        continue;
      }
      convos.push({
        id: childId,
        title: sanitizeLine(r.name || childId),
        preview: sanitizeLine(r.lastBody || ''),
        lastTs: r.lastTs || 0,
        sourceId: r.sourceId,
        userLabel: label,
        profileId: r.profileId || null,
        profileDisplayName: r.profileDisplayName || null,
      });
    }
    byUser.set(label, convos);
  }
  // One deterministic write target per teammate: if several proposals rooms
  // were discovered under one label, pick the lexicographically smallest id
  // rather than "whichever the iteration happened to reach last".
  for (const [label, list] of proposalCandidates) {
    proposalsByUser.set(label, list.slice().sort()[0]);
  }
  MS.proposalsByUser = proposalsByUser;
  MS.proposalsRoomSet = proposalsRoomSet;
  MS.contacts = allContacts;
  MS.skippedUnverified = skipped;
  return byUser;
}

// Make identity-gate skips visible instead of silently dropping data: a small
// muted count in the sidebar. textContent only, never HTML.
function renderUnverifiedNote() {
  const n = MS.skippedUnverified || { spaces: 0, children: 0 };
  const total = (n.spaces || 0) + (n.children || 0);
  let note = $('unverified-note');
  if (!note) {
    const anchor = $('teammates-label') || $('nav-teammates');
    if (!anchor || !anchor.parentNode) return;
    note = el('div', 'unverified-note muted');
    note.id = 'unverified-note';
    anchor.parentNode.insertBefore(note, anchor.nextSibling);
  }
  note.textContent = total
    ? total + ' unverified item' + (total === 1 ? '' : 's') + ' hidden'
    : '';
  note.classList.toggle('hidden', total === 0);
}

// The uplink INVITES the manager into the teammate's space, their mirror rooms
// and their proposals room (it cannot force-join another account), so accepting
// those invites is the last hop of the pipeline. Which invites are acceptable is
// decided ENTIRELY by ./invites.js's identity gate — never here, and never on a
// custom state type (stripped invite state does not carry one; that was the bug
// this replaces). Joining is membership, not a send; the only write this app
// ever performs is the single com.jkali.proposal in submitProposal.
//
// Two passes are needed by construction: pass 1 joins the teammate's space,
// pass 2 sees its m.space.child list and can therefore verify the mirrors and
// the proposals room. The third pass is slack; the loop stops as soon as a pass
// joins nothing. Every pass is capped inside invitesToJoin, and an invite whose
// join hard-fails is memoized so it is never retried this session.
async function joinPendingInvites() {
  let data = null;
  for (let pass = 0; pass < 3; pass++) {
    data = await fetchSnapshot();
    const rooms = parseSnapshot(data);
    const vch = verifiedChildIds(acceptedSpaces(rooms));
    const ids = invitesToJoin((data.rooms && data.rooms.invite) || {}, vch, {})
      .filter(id => !MS.joinFailed.has(id) && ROOMID_RE.test(id));
    let joined = 0;
    for (const id of ids) {
      try {
        await api('POST', '/_matrix/client/v3/rooms/' + encodeURIComponent(id) + '/join', {});
        joined++;
      } catch (e) {
        // 4xx other than 429 means "this will not succeed by retrying"
        // (withdrawn invite, forbidden, gone) -> stop asking. 429/5xx/network
        // errors stay retryable on the next refresh.
        const code = e && typeof e.status === 'number' ? e.status : 0;
        if (code >= 400 && code < 500 && code !== 429) MS.joinFailed.add(id);
      }
    }
    if (!joined) break;
  }
  return data;
}

async function refreshAll() {
  const data = await joinPendingInvites();
  const rooms = parseSnapshot(data);
  MS.rooms = rooms;
  MS.byUser = buildByUser(rooms);
  MS.feed = [].concat(...[...MS.byUser.values()]).sort((a, b) => b.lastTs - a.lastTs);
  // Fold this refresh into the persistent contacts index. Fire-and-forget:
  // it is O(contacts) over data already in memory (no extra /sync), and its
  // own try/catch means a failure here never affects rendering below.
  persistContactsIndex().catch(() => {});
  renderUnverifiedNote();
  // The open room can disappear mid-session (the teammate un-shared it, or it
  // failed re-verification). Close the proposal path rather than leaving a
  // composer pointed at a room that is no longer part of the verified set.
  if (MS.openRoomId && !MS.rooms[MS.openRoomId]) {
    const pane = $('proposal-pane');
    if (pane) pane.classList.add('hidden');
    roomStatus('This conversation is no longer shared.');
  }
  if (MS.activeView === 'recent') renderRecent();
  else if (MS.activeView === 'search') renderSearch();
  else if (MS.activeView === 'contacts') renderContacts();
  else if (MS.activeView === 'teammates') renderTeammatesList();
  else if (typeof MS.activeView === 'string' && MS.activeView.indexOf('teammate:') === 0) {
    const label = MS.activeView.slice('teammate:'.length);
    if (MS.hidden.has(label)) navTo('recent');
    else renderTeammate(label);
  }
}

// ===========================================================================
// Rendering — rows, badges, empty states. Same DOM shape (.convo/.avatar/
// .meta/.plat-badge classes, style.css) as apps/user's rows for a consistent
// look, small enough here not to be worth importing rows.js for (which would
// also pull in chat.js's sendConvoMessage transitively — see file header).
// ===========================================================================
function elEmpty(text) { return el('div', 'list-empty', text); }

function relTime(ts) {
  const d = Date.now() - ts;
  if (d < 0) return '';
  if (d < 60000) return 'now';
  if (d < 3600000) return Math.floor(d / 60000) + 'm';
  if (d < 86400000) return Math.floor(d / 3600000) + 'h';
  if (d < 604800000) return Math.floor(d / 86400000) + 'd';
  const dt = new Date(ts);
  return (dt.getMonth() + 1) + '/' + dt.getDate();
}

// Badge derived ONLY from the room's own com.jkali.source state (read in
// parseSnapshot above) — never a bridged message field.
const PLATFORM_ICON = {
  whatsapp: '🟢', imessage: '🔵', gmessages: '📱',
  instagram: '📷', linkedin: '💼', twitter: '✖️',
};
const PLATFORM_LABEL = {
  whatsapp: 'WhatsApp', imessage: 'iMessage', gmessages: 'Google Messages',
  instagram: 'Instagram', linkedin: 'LinkedIn', twitter: 'X (Twitter)',
};
function buildPlatBadge(sourceId) {
  const safe = typeof sourceId === 'string' ? sourceId.replace(/[^a-z]/g, '') : '';
  const cls = 'plat-badge' + (safe ? ' ' + safe : '');
  return el('span', cls, (sourceId && PLATFORM_ICON[sourceId]) || '•');
}
function platformLabel(sourceId) {
  return (sourceId && PLATFORM_LABEL[sourceId]) || '';
}

// Stable badge order for a multi-platform summary row: the same order the
// per-row badges already imply (PLATFORM_ICON's declaration order), with any
// unrecognized source id appended afterwards, alphabetically, rather than
// dropped. Pure data shaping — no rendering here.
const PLATFORM_ORDER = Object.keys(PLATFORM_ICON);
function computePlatforms(sourceIds) {
  const set = new Set((sourceIds || []).filter(Boolean));
  const known = PLATFORM_ORDER.filter(s => set.has(s));
  const rest = [...set].filter(s => !PLATFORM_ORDER.includes(s)).sort();
  return known.concat(rest);
}

// Per-TEAMMATE "which platforms has this person shared" — the distinct set
// of source ids spanning BOTH their shared mirror rooms (MS.byUser, keyed by
// the verified label) AND their shared contact handles (MS.contacts, each
// already tagged with the verified label in buildByUser/parseContact). Same
// dedupe/order as the existing per-person computePlatforms; pure derivation
// over already-fetched MS state — no new reads, called fresh on each render.
function computeUserPlatforms(label) {
  const convos = MS.byUser.get(label) || [];
  const fromRooms = convos.map(c => c.sourceId);
  const fromContacts = MS.contacts.filter(ct => ct.label === label).map(ct => ct.source);
  return computePlatforms(fromRooms.concat(fromContacts));
}

// Shared badge-row builder for the per-teammate platform summary: either a
// row of platform badges (buildPlatBadge — same icons used everywhere else)
// or an explicit "nothing shared yet" note. Never a false positive: a label
// with zero distinct platforms always renders the note, never an empty row
// that could read as "loading" or be mistaken for a real (if sparse) share.
function buildUserPlatformsRow(label) {
  const row = el('span', 'user-platforms-row');
  const platforms = computeUserPlatforms(label);
  if (!platforms.length) {
    row.appendChild(el('span', 'muted user-platforms-empty', 'nothing shared yet'));
  } else {
    for (const src of platforms) row.appendChild(buildPlatBadge(src));
  }
  return row;
}

// One row = one mirror room, whichever list it appears in (Recent / a
// teammate's section / Search results). Shows the conversation name, the
// preview, whose account it is (userLabel), and the platform badge — the
// "each row shows whose account + which platform" requirement (§6.4).
function buildFeedRow(c) {
  const row = el('div', 'convo');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(el('div', 'avatar', (c.title || '?').slice(0, 1).toUpperCase()));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', c.title));
  meta.appendChild(el('div', 'preview', c.preview));
  row.appendChild(meta);
  if (c.lastTs) row.appendChild(el('span', 'when', relTime(c.lastTs)));
  row.appendChild(el('span', 'badge', c.userLabel || ''));
  row.appendChild(buildPlatBadge(c.sourceId));
  row.appendChild(el('span', 'convo-open', 'Open'));  // visual affordance only; the whole row is already clickable/keyboard-activatable below
  const open = () => { openRoom(c.id).catch(() => {}); };
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

// Group a flat conversation list into "one person, many platforms" clusters:
// every convo carrying the SAME com.jkali.profile id (stamped by the uplink
// only on members of a SHARED contact profile — see parseSnapshot above)
// collapses under one header; everything else stays a standalone row, exactly
// as before. Pure grouping over already-fetched data — no new reads, no
// mutation. Order preserved by most-recent activity, same as the flat feed.
function groupByProfile(convos) {
  const order = [];
  const groups = new Map(); // profileId -> group item (also pushed into order once)
  for (const c of convos) {
    if (c.profileId) {
      let g = groups.get(c.profileId);
      if (!g) {
        g = { kind: 'profile', profileId: c.profileId, displayName: c.profileId, members: [], lastTs: 0 };
        groups.set(c.profileId, g);
        order.push(g);
      }
      g.members.push(c);
      if (c.profileDisplayName) g.displayName = c.profileDisplayName; // prefer a real name over the raw id
      if ((c.lastTs || 0) > g.lastTs) g.lastTs = c.lastTs || 0;
    } else {
      order.push({ kind: 'single', convo: c, lastTs: c.lastTs || 0 });
    }
  }
  for (const item of order) {
    if (item.kind === 'profile') item.members.sort((a, b) => (b.lastTs || 0) - (a.lastTs || 0));
  }
  order.sort((a, b) => b.lastTs - a.lastTs);
  return order;
}

// A profile header shown ONCE per person, with their per-platform threads
// nested beneath — each still built by buildFeedRow, so it keeps its own
// source badge, teammate badge, preview and click-to-open behavior unchanged.
// Reuses the shared row renderer; adds no new interaction (click still opens
// the individual mirror room, same as a standalone row).
function buildProfileGroup(g) {
  const wrap = el('div', 'profile-group');
  const header = el('div', 'profile-header');
  header.appendChild(el('span', 'profile-avatar', (g.displayName || '?').slice(0, 1).toUpperCase()));
  header.appendChild(el('span', 'profile-name', g.displayName));
  header.appendChild(el('span', 'profile-count',
    g.members.length + ' thread' + (g.members.length === 1 ? '' : 's')));
  wrap.appendChild(header);
  const members = el('div', 'profile-members');
  for (const c of g.members) members.appendChild(buildFeedRow(c));
  wrap.appendChild(members);
  return wrap;
}

// Renders one list item, grouped or not — the shared entry point both
// renderRecent and renderTeammate use below.
function buildListItem(item) {
  return item.kind === 'profile' ? buildProfileGroup(item) : buildFeedRow(item.convo);
}

function renderRecent() {
  const list = $('list-body');
  if (!list) return;
  list.replaceChildren();
  const feed = visibleFeed(MS.feed, MS.hidden);
  if (!feed.length) { list.appendChild(elEmpty('No shared conversations yet.')); return; }
  for (const item of groupByProfile(feed.slice(0, 200))) list.appendChild(buildListItem(item));
}

// Sidebar teammate rows (mockup 1f left rail): initials avatar + name + a
// count of that teammate's shared conversations. Purely presentational over
// already-fetched MS.byUser; the count is just that teammate's convo list length.
function renderTeammatesList() {
  const list = $('list-body');
  if (!list) return;
  list.replaceChildren();
  const visible = visibleUsers(MS.byUser, MS.hidden);
  if (!visible.length && !MS.hidden.size) {
    list.appendChild(elEmpty('No teammates yet.'));
    return;
  }
  for (const [label, convos] of visible) list.appendChild(buildTeammateRow(label, convos, false));
  if (MS.hidden.size) {
    const head = el('div', 'profile-header');
    head.appendChild(el('span', 'profile-name', 'Hidden'));
    list.appendChild(head);
    for (const label of MS.hidden) {
      list.appendChild(buildTeammateRow(label, MS.byUser.get(label) || [], true));
    }
  }
}

// Same .convo schema as feed/contact rows: avatar, title+preview, platform
// badges, trailing kebab (Hide/Show + Delete). Click opens that teammate's
// conversations; kebab actions do not navigate.
function buildTeammateRow(label, convos, isHiddenRow) {
  const row = el('div', 'convo' + (isHiddenRow ? ' teammate-hidden' : ''));
  row.appendChild(el('div', 'avatar', initials(label)));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', sanitizeLine(label)));
  const n = convos.length;
  meta.appendChild(el('div', 'preview',
    n ? (n + ' conversation' + (n === 1 ? '' : 's')) : 'nothing shared yet'));
  row.appendChild(meta);
  const platforms = computeUserPlatforms(label);
  if (platforms.length) {
    const platRow = el('span', 'profile-platforms');
    for (const src of platforms) platRow.appendChild(buildPlatBadge(src));
    row.appendChild(platRow);
  }
  row.appendChild(buildTeammateKebab(label, isHiddenRow));
  if (!isHiddenRow) {
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    const open = () => navTo('teammate:' + label);
    row.addEventListener('click', open);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
  }
  return row;
}

function closeTeammateKebabs() {
  document.querySelectorAll('.share-menu:not(.hidden)').forEach((m) => m.classList.add('hidden'));
}

function buildTeammateKebab(label, isHiddenRow) {
  const holder = el('span', 'share-controls');
  const kebab = el('button', 'share-kebab', '\u22EE');
  kebab.type = 'button';
  kebab.title = 'More';
  kebab.setAttribute('aria-label', 'Actions for ' + label);
  kebab.setAttribute('aria-haspopup', 'menu');
  const menu = el('div', 'share-menu hidden');
  menu.setAttribute('role', 'menu');
  const hideBtn = el('button', 'share-menu-link', isHiddenRow ? 'Show' : 'Hide');
  hideBtn.type = 'button';
  hideBtn.setAttribute('role', 'menuitem');
  hideBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    closeTeammateKebabs();
    if (isHiddenRow) showTeammate(label);
    else hideTeammate(label);
  });
  const delBtn = el('button', 'share-menu-link teammate-delete', 'Delete');
  delBtn.type = 'button';
  delBtn.setAttribute('role', 'menuitem');
  delBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    closeTeammateKebabs();
    confirmDeleteTeammate(label).then((ok) => {
      if (ok) deleteTeammate(label).catch((err) => {
        window.alert('Could not delete: ' + String((err && err.message) || err));
      });
    });
  });
  menu.appendChild(hideBtn);
  menu.appendChild(delBtn);
  const stop = (e) => e.stopPropagation();
  kebab.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const open = !menu.classList.contains('hidden');
    closeTeammateKebabs();
    if (!open) menu.classList.remove('hidden');
  });
  kebab.addEventListener('keydown', stop);
  menu.addEventListener('click', stop);
  menu.addEventListener('keydown', stop);
  holder.appendChild(kebab);
  holder.appendChild(menu);
  holder.addEventListener('click', stop);
  if (!window.__teammateKebabCloser) {
    window.__teammateKebabCloser = true;
    document.addEventListener('click', closeTeammateKebabs);
  }
  return holder;
}

function confirmDeleteTeammate(label) {
  return new Promise((resolve) => {
    const existing = document.getElementById('teammate-del-backdrop');
    if (existing) existing.remove();
    const backdrop = el('div', 'dialog-backdrop');
    backdrop.id = 'teammate-del-backdrop';
    const dlg = el('div', 'dialog');
    dlg.setAttribute('role', 'dialog');
    dlg.setAttribute('aria-modal', 'true');
    dlg.appendChild(el('div', 'dialog-title', 'Delete ' + sanitizeLine(label) + '?'));
    dlg.appendChild(el('p', 'dialog-body',
      'This deactivates their master account and removes them from this console. You cannot re-add the same username.'));
    const actions = el('div', 'dialog-actions');
    const cancel = el('button', 'btn', 'Cancel');
    cancel.type = 'button';
    const ok = el('button', 'btn danger', 'Delete');
    ok.type = 'button';
    const finish = (val) => {
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
      resolve(val);
    };
    const onKey = (e) => { if (e.key === 'Escape') finish(false); };
    cancel.addEventListener('click', (e) => { e.stopPropagation(); finish(false); });
    ok.addEventListener('click', (e) => { e.stopPropagation(); finish(true); });
    backdrop.addEventListener('click', () => finish(false));
    dlg.addEventListener('click', (e) => e.stopPropagation());
    actions.appendChild(cancel);
    actions.appendChild(ok);
    dlg.appendChild(actions);
    backdrop.appendChild(dlg);
    document.body.appendChild(backdrop);
    document.addEventListener('keydown', onKey);
    ok.focus();
  });
}

async function deleteTeammate(label) {
  const res = await fetch(ENROLL_BASE + '/admin/delete-teammate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + (S.token || '') },
    body: JSON.stringify({ username: label }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data && data.error ? data.error : ('HTTP ' + res.status));
  MS.hidden = unhide(MS.hidden, label);
  saveHidden();
  if (MS.activeView === 'teammate:' + label) navTo('teammates');
  try { await refreshAll(); } catch (e) { /* list may already be empty */ }
  if (MS.activeView === 'teammates') renderTeammatesList();
}

function loadHidden() {
  try { return parseHidden(localStorage.getItem(HIDDEN_KEY)); }
  catch (e) { return new Set(); }
}
function saveHidden() {
  try { localStorage.setItem(HIDDEN_KEY, dumpHidden(MS.hidden)); } catch (e) {}
}
function applyHidden() {
  saveHidden();
  if (typeof MS.activeView === 'string' && MS.activeView.indexOf('teammate:') === 0
      && MS.hidden.has(MS.activeView.slice('teammate:'.length))) {
    navTo('recent');
    return;
  }
  if (MS.activeView === 'recent') renderRecent();
  else if (MS.activeView === 'search') renderSearch();
  else if (MS.activeView === 'contacts') renderContacts();
  else if (MS.activeView === 'teammates') renderTeammatesList();
}
function hideTeammate(label) {
  MS.hidden = hide(MS.hidden, label);
  applyHidden();
}
function showTeammate(label) {
  MS.hidden = unhide(MS.hidden, label);
  applyHidden();
}

// Up to two initials from a teammate label, for the round avatar chip.
function initials(label) {
  const parts = String(label || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  const first = parts[0][0] || '';
  const second = parts.length > 1 ? (parts[parts.length - 1][0] || '') : '';
  return (first + second).toUpperCase();
}

function renderTeammate(label) {
  const convos = (MS.byUser.get(label) || []).slice().sort((a, b) => b.lastTs - a.lastTs);
  const list = $('list-body');
  if (!list) return;
  list.replaceChildren();
  // At-a-glance "shared platforms" summary for this teammate, above their
  // conversation list — additive: the existing empty-state/list rendering
  // below is unchanged either way.
  const summary = el('div', 'user-platforms-summary');
  summary.appendChild(el('span', 'user-platforms-caption', 'Shared platforms:'));
  summary.appendChild(buildUserPlatformsRow(label));
  list.appendChild(summary);
  if (!convos.length) { list.appendChild(elEmpty('Nothing shared yet.')); return; }
  for (const item of groupByProfile(convos)) list.appendChild(buildListItem(item));
}

// #search-input is a pure client-side filter over the in-memory flattened
// feed; it never builds a URL, sends a command, or navigates.
function renderSearch() {
  const q = (($('search-input') && $('search-input').value) || '').trim().toLowerCase();
  const out = $('list-body');
  if (!out) return;
  out.replaceChildren();
  if (!q) { out.appendChild(elEmpty('Type to search across every teammate.')); return; }
  const rows = visibleFeed(MS.feed, MS.hidden).filter(c =>
    c.title.toLowerCase().includes(q) || (c.preview || '').toLowerCase().includes(q)
      || (c.userLabel || '').toLowerCase().includes(q));
  if (!rows.length) { out.appendChild(elEmpty('No conversations match "' + q + '".')); return; }
  for (const c of rows) out.appendChild(buildFeedRow(c));
}

// ===========================================================================
// Read-only conversation viewer. No input element exists anywhere in this
// view (index.html has none); nothing here ever calls
// PUT .../send/m.room.message or any other write endpoint.
// ===========================================================================
function roomStatus(text) {
  const s = $('room-status');
  if (!s) return;
  s.textContent = text || '';
  s.classList.toggle('hidden', !text);
}

function shortTime(ts) {
  if (typeof ts !== 'number' || !isFinite(ts) || !ts) return '';
  try { return new Date(ts).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
  catch (e) { return ''; }
}

// Alignment/attribution comes from the trusted com.jkali.from_me flag the
// uplink stamps. Unlike apps/user's iMessage-bot gate, no sender/bot check is
// needed here: master-side power levels (§8.3) mean only @<teammate>:master
// can ever post into that teammate's own mirror room, so the flag cannot be
// spoofed by anyone else within that room.
// Day-divider label ("Today"/"Yesterday"/a short date), purely presentational
// grouping over each bubble's own already-resolved timestamp — no new reads.
function dayKey(ts) {
  if (typeof ts !== 'number' || !isFinite(ts) || !ts) return null;
  const d = new Date(ts);
  return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
}
function dayLabel(ts) {
  const d = new Date(ts);
  const now = new Date();
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
function maybeInsertDayDivider(box, ts) {
  const key = dayKey(ts);
  if (!key || key === MS.lastDayKey) return;
  MS.lastDayKey = key;
  box.appendChild(el('div', 'day-divider', dayLabel(ts)));
}

function renderBubble(ev) {
  const box = $('room-messages');
  if (!box) return;
  const resolved = resolveMirrorContent(ev);
  if (!resolved) return;                              // reaction/redaction/state/etc. — skip
  const sent = !!(ev.content && ev.content['com.jkali.from_me'] === true);
  // Who to show on a received bubble: the uplink stamps the ORIGIN sender's
  // display name (resolved from the teammate-local room's member state) as
  // com.jkali.origin_sender. The raw ev.sender is always the teammate's own
  // uplink account (@<teammate>:master — sole poster in a mirror room), so it
  // is only a last-resort fallback, never the real correspondent.
  const senderName = (ev.content && typeof ev.content['com.jkali.origin_sender'] === 'string')
    ? sanitizeLine(ev.content['com.jkali.origin_sender'])
    : displayNameForMxid(ev.sender);
  const ts = mirrorTs(ev);
  maybeInsertDayDivider(box, ts);

  const row = el('div', 'msg-row ' + (sent ? 'sent' : 'recv'));
  // Header line: small source-platform badge + who + role/time (mockup 1g:
  // a source logo on every bubble, not just the room header).
  const meta = el('div', 'msg-meta');
  const miniBadge = buildPlatBadge(MS.openRoomSourceId);
  miniBadge.className += ' msg-badge-mini';
  meta.appendChild(miniBadge);
  meta.appendChild(el('span', 'msg-sender', sent ? (MS.openRoomUser || 'Teammate') : senderName));
  meta.appendChild(el('span', 'msg-role-time', (sent ? 'teammate' : 'other party') + ' · ' + shortTime(ts)));
  row.appendChild(meta);

  let cls = 'msg';
  if (resolved.kind === 'media') cls += ' media';
  else if (resolved.kind === 'notice') cls += ' notice';
  const bubble = el('div', cls);
  const bodyNode = el('div', 'body', resolved.text);
  bubble.appendChild(bodyNode);
  row.appendChild(bubble);
  box.appendChild(row);
  // v1.5: when this bubble carries a real re-uploaded master mxc, replace the
  // static label with the fetched media (falls back to the label on any error).
  if (resolved.kind === 'media' && resolved.mxc) loadMediaInto(bodyNode, resolved);
  while (box.childElementCount > 300) {               // bounded, drop oldest
    const first = box.firstElementChild;
    if (!first) break;
    box.removeChild(first);
  }
}

async function openRoom(roomId) {
  // Re-validate against the current snapshot every time — never trust a
  // stale/typed id (same discipline as apps/user's openConversation).
  if (!ROOMID_RE.test(roomId) || !MS.rooms[roomId]) return;
  stopTail();
  MS.openRoomId = roomId;
  const rec = MS.rooms[roomId];
  MS.openRoomUser = rec.userLabel || null;
  MS.openRoomSourceId = rec.sourceId || null;
  MS.openMirrorOf = typeof rec.mirrorOf === 'string' ? rec.mirrorOf : null;
  // PIN the whole proposal context at open time. submitProposal re-asserts this
  // exact tuple against the live snapshot instead of resolving the destination
  // again, so a label change or a revoked/replaced proposals room between open
  // and submit refuses the write rather than redirecting it somewhere new.
  MS.openProposalCtx = {
    mirrorRoomId: roomId,
    label: rec.userLabel || null,
    proposalsRoomId: (rec.userLabel && MS.proposalsByUser.get(rec.userLabel)) || null,
    targetRoom: (typeof rec.mirrorOf === 'string' && rec.mirrorOf) || null,
  };
  MS.lastDayKey = null;
  setupProposalComposer(rec);
  $('room-title').textContent = sanitizeLine(rec.name || roomId);
  const owner = $('room-owner');
  if (rec.userLabel) { owner.textContent = 'shared by ' + rec.userLabel; owner.classList.remove('hidden'); }
  else { owner.textContent = ''; owner.classList.add('hidden'); }
  const badge = $('room-badge');
  const b = buildPlatBadge(rec.sourceId);
  badge.className = b.className;
  badge.textContent = b.textContent;
  $('room-source-label').textContent = platformLabel(rec.sourceId);
  const box = $('room-messages');
  if (box) box.replaceChildren();
  roomStatus('');
  showWorkspace(true);
  setDetailMode('room');

  try {
    const q = '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/messages?dir=b&limit=100';
    const data = await api('GET', q);
    if (MS.openRoomId !== roomId) return;             // navigated away mid-fetch
    const chunk = Array.isArray(data.chunk) ? data.chunk : [];
    // §8.2/§11: sort by com.jkali.origin_ts — backfill can arrive out of
    // chronological order, so timeline/arrival order is not display order.
    const sorted = chunk.slice().sort((a, c2) => mirrorTs(a) - mirrorTs(c2));
    for (const ev of sorted) renderBubble(ev);
  } catch (e) {
    roomStatus('Could not load messages: ' + String(e.message || e));
  }
  await loadSuggestionOverlay();
  if (box) box.scrollTop = box.scrollHeight;
  startTail(roomId);
}

// A room-scoped long-poll tail, mirroring the shape of apps/user's three
// independent sync loops (each read path owns its own loop in this codebase;
// see shared/ui/account-data.js/chat.js) — kept local rather than imported
// from shared/ui/chat.js's startConvoWatch, which lives in the same file as
// sendConvoMessage (see header). Read-only: appends via renderBubble only.
async function startTail(roomId) {
  if (MS.tailRunning) return;
  MS.tailRunning = true;
  MS.tailSince = null;
  while (MS.tailRunning && S.token && MS.openRoomId === roomId) {
    try {
      const filter = encodeURIComponent(JSON.stringify({
        room: { rooms: [roomId], timeline: { limit: 20 }, state: { types: [] } },
        presence: { types: [] }, account_data: { types: [] },
      }));
      const q = '/_matrix/client/v3/sync?timeout=25000&filter=' + filter +
        (MS.tailSince ? '&since=' + encodeURIComponent(MS.tailSince) : '');
      const data = await api('GET', q);
      MS.tailSince = data.next_batch;
      const join = (data.rooms && data.rooms.join) || {};
      const room = join[roomId];
      if (room && room.timeline && Array.isArray(room.timeline.events) && MS.openRoomId === roomId) {
        for (const ev of room.timeline.events) {
          if (MS.openRoomId !== roomId) break;
          renderBubble(ev);
        }
        pinSuggestion();
        const box = $('room-messages');
        if (box) box.scrollTop = box.scrollHeight;
      }
    } catch (e) {
      if (!S.token) { MS.tailRunning = false; return; }
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}
function stopTail() { MS.tailRunning = false; MS.openRoomId = null; MS.openRoomUser = null; MS.openRoomSourceId = null; MS.openMirrorOf = null; MS.openProposalCtx = null; }

// ===========================================================================
// Compose-proposal — the ONE place the manager can write (PLAN §2 v2 / §7).
//
// This is a PROPOSAL, not a send. Submitting writes a single com.jkali.proposal
// event into THIS teammate's dedicated proposals room (marked com.jkali.proposals,
// discovered in the snapshot). It NEVER posts m.room.message, NEVER writes into a
// mirror/conversation room, and NEVER reaches a bridge or any external network.
// The teammate later reviews the suggestion and sends it themselves from their
// own guarded local send path. All send guards live in submitProposal below.
// ===========================================================================
function proposalTargetFor(rec) {
  // A proposal can be composed only when: (a) we know the teammate's real local
  // room this mirror stands for (mirrorOf), and (b) that teammate has a
  // discovered proposals room to write into. Otherwise the composer stays hidden.
  const label = rec && rec.userLabel;
  const target = rec && typeof rec.mirrorOf === 'string' ? rec.mirrorOf : null;
  const proposalsRoom = label ? MS.proposalsByUser.get(label) : null;
  if (!target || !proposalsRoom) return null;
  return { target, proposalsRoom, label };
}

function setupProposalComposer(rec) {
  const pane = $('proposal-pane');
  if (!pane) return;
  const ctx = proposalTargetFor(rec);
  proposalStatus('');
  const input = $('proposal-input');
  if (input) input.value = '';
  if (!ctx) { pane.classList.add('hidden'); return; }
  pane.classList.remove('hidden');
}

function proposalStatus(text, isError) {
  const s = $('proposal-status');
  if (!s) return;
  s.textContent = text || '';
  s.classList.toggle('hidden', !text);
  s.classList.toggle('error', !!isError);
}

// Latest room-targeted proposal for one target_room. Pure. The teammate inbox
// already keeps only the newest pending draft per room (pendingForRoom); this
// overlay does the same so the manager sees one editable bubble, not a stack.
function latestRoomProposal(events, targetRoom) {
  if (!Array.isArray(events) || typeof targetRoom !== 'string' || !targetRoom) return null;
  let best = null;
  for (const e of events) {
    if (!e || e.type !== 'com.jkali.proposal' || !e.content) continue;
    if (e.content.target_room !== targetRoom) continue;
    const body = typeof e.content.body === 'string' ? e.content.body.trim() : '';
    if (!body) continue;
    const ts = typeof e.content.origin_ts === 'number' ? e.content.origin_ts
      : (typeof e.origin_server_ts === 'number' ? e.origin_server_ts : 0);
    if (!best || ts > best.ts) best = { body, eventId: e.event_id, ts };
  }
  return best;
}

function pinSuggestion() {
  const box = $('room-messages');
  const row = box && box.querySelector('.msg-row.suggested');
  if (row && row !== box.lastElementChild) box.appendChild(row);
}

function showSuggestion(body) {
  const box = $('room-messages');
  if (!box) return;
  const text = sanitize(body);
  if (!text) return;
  let row = box.querySelector('.msg-row.suggested');
  if (!row) {
    row = el('div', 'msg-row sent suggested');
    const meta = el('div', 'msg-meta');
    meta.appendChild(el('span', 'msg-role-time', 'suggested'));
    row.appendChild(meta);
    const bubble = el('div', 'msg');
    bubble.appendChild(el('div', 'body'));
    row.appendChild(bubble);
    row.addEventListener('dblclick', startSuggestionEdit);
    box.appendChild(row);
  }
  const bodyNode = row.querySelector('.body');
  if (bodyNode && bodyNode.getAttribute('contenteditable') !== 'true') bodyNode.textContent = text;
  pinSuggestion();
  box.scrollTop = box.scrollHeight;
}

function startSuggestionEdit(e) {
  const bodyNode = e.currentTarget.querySelector('.body');
  if (!bodyNode || bodyNode.getAttribute('contenteditable') === 'true') return;
  const saved = bodyNode.textContent || '';
  bodyNode.setAttribute('contenteditable', 'true');
  bodyNode.focus();
  const range = document.createRange();
  range.selectNodeContents(bodyNode);
  const sel = window.getSelection();
  if (sel) { sel.removeAllRanges(); sel.addRange(range); }
  let done = false;
  const stop = () => {
    if (done) return;
    done = true;
    bodyNode.removeAttribute('contenteditable');
    bodyNode.removeEventListener('blur', onBlur);
    bodyNode.removeEventListener('keydown', onKey);
  };
  const onBlur = () => {
    if (done) return;
    const next = sanitize(bodyNode.textContent || '').trim();
    stop();
    if (!next) { bodyNode.textContent = saved; return; }
    if (next === saved) return;
    submitProposal({ body: next }).catch(() => { bodyNode.textContent = saved; });
  };
  const onKey = (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); bodyNode.blur(); }
    if (ev.key === 'Escape') { ev.preventDefault(); bodyNode.textContent = saved; stop(); }
  };
  bodyNode.addEventListener('blur', onBlur);
  bodyNode.addEventListener('keydown', onKey);
}

async function loadSuggestionOverlay() {
  const ctx = MS.openProposalCtx;
  if (!ctx || !ctx.proposalsRoomId || !ctx.targetRoom || !ctx.mirrorRoomId) return;
  if (!ROOMID_RE.test(ctx.proposalsRoomId) || !MS.proposalsRoomSet.has(ctx.proposalsRoomId)) return;
  try {
    const q = '/_matrix/client/v3/rooms/' + encodeURIComponent(ctx.proposalsRoomId) + '/messages?dir=b&limit=100';
    const data = await api('GET', q);
    if (MS.openRoomId !== ctx.mirrorRoomId) return;
    const latest = latestRoomProposal(Array.isArray(data.chunk) ? data.chunk : [], ctx.targetRoom);
    if (latest) showSuggestion(latest.body);
  } catch (e) { /* overlay is optional; the write path still works */ }
}

// Pure builder for a PERSON-targeted proposal's content (extracted so a unit
// test can hold the shape gate still — tests/unit/proposal_identifier.test.js).
// Returns the com.jkali.proposal content object, or null when the identifier
// fails its shape check (E.164 phone OR strict email) or the body is empty.
// Crucially, it carries NO target_room — a person-targeted proposal names an
// inert identifier the teammate resolves themselves, not a room the master
// could ever address. This is data-shaping only: it never sends anything.
function buildIdentifierProposalContent({ source, identifier, display, body } = {}) {
  const id = typeof identifier === 'string' ? identifier.trim() : '';
  const text = typeof body === 'string' ? body.trim() : '';
  if (!text) return null;
  if (!E164_RE.test(id) && !EMAIL_RE.test(id)) return null;
  // The source must be a valid short source id — the SAME shape the uplink
  // enforces (agents/uplink/uplink.py PROPOSAL_SOURCE_RE). A null/blank/
  // malformed source produces a proposal the uplink sanitizer silently drops,
  // so reject it here (return null) instead of writing a dead proposal.
  if (typeof source !== 'string' || !/^[a-z][a-z0-9_]{0,31}$/.test(source)) return null;
  return {
    target_source: source,
    target_identifier: id,
    target_display: (typeof display === 'string' && display) ? sanitizeLine(display) : null,
    body: text,
    created_by: S.userId,
    origin_ts: Date.now(),
  };
}

// The single guarded write in this app. Defense in depth:
//  - the destination and the target are the ones PINNED when the room was
//    opened (MS.openProposalCtx) — never re-resolved here, so a mid-session
//    change cannot redirect an in-flight suggestion to a different room;
//  - every element of that pinned tuple must STILL hold in the current
//    snapshot: the mirror room still verified under the same label, the same
//    proposals room still that label's discovered target, ROOMID_RE-valid and
//    in the allowlist (never a stale/typed id, never a mirror room);
//  - the target_room is shape-checked with the generic any-server shape (it is
//    a foreign teammate-local id, so the master's server-pinned ROOMID_RE does
//    not apply) and is only recorded, never sent to;
//  - the event TYPE is the hardcoded literal 'com.jkali.proposal' — there is no
//    code path here that PUTs /send/m.room.message anywhere.
async function submitProposal(opts) {
  // ---- PERSON-targeted branch (v-contacts) ------------------------------
  // Same write, same event type, same allowlist gate as the room-targeted
  // path below — only the content shape differs (an identifier, never a
  // target_room). Still resolves the destination to the SELECTED contact's
  // teammate proposals room and re-asserts it against the live allowlist.
  if (opts && opts.kind === 'identifier') {
    const label = opts.label;
    const proposalsRoom = label ? MS.proposalsByUser.get(label) : null;
    if (!proposalsRoom || MS.proposalsByUser.get(label) !== proposalsRoom
        || !ROOMID_RE.test(proposalsRoom) || !MS.proposalsRoomSet.has(proposalsRoom)) {
      contactProposalStatus('No proposals channel for this teammate yet.', true);
      return;
    }
    const content = buildIdentifierProposalContent({
      source: opts.source, identifier: opts.identifier, display: opts.display, body: opts.body,
    });
    if (!content) {
      contactProposalStatus('Enter a message and make sure the contact is a phone number or email.', true);
      return;
    }
    const txn = 'prop_' + Date.now() + '_' + Math.random().toString(36).slice(2);
    contactProposalStatus('Sending suggestion…');
    try {
      // Event type is the literal 'com.jkali.proposal' — the SAME single write
      // endpoint the room-targeted path uses. No m.room.message; the master
      // never starts a chat (integration scenario 12 scans this file for that
      // literal, so keep it unnamed here).
      await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(proposalsRoom)
        + '/send/com.jkali.proposal/' + encodeURIComponent(txn), content);
      const input = $('contact-proposal-input');
      if (input) input.value = '';
      contactProposalStatus('Suggestion sent to ' + sanitizeLine(label || 'teammate')
        + ' for review. It was not sent to anyone externally.');
    } catch (e) {
      contactProposalStatus('Could not send suggestion: ' + String(e.message || e), true);
    }
    return;
  }
  // ---- room-targeted branch (unchanged) ---------------------------------
  const ctx = MS.openProposalCtx;
  if (!ctx || !ctx.label || !ctx.mirrorRoomId) {
    proposalStatus('No conversation is open.', true); return;
  }
  const rec = MS.rooms[ctx.mirrorRoomId];
  if (!rec || rec.userLabel !== ctx.label) {
    proposalStatus('This conversation is no longer shared.', true); return;
  }
  const proposalsRoom = ctx.proposalsRoomId;
  const target = ctx.targetRoom;
  if (!proposalsRoom || MS.proposalsByUser.get(ctx.label) !== proposalsRoom
      || !ROOMID_RE.test(proposalsRoom) || !MS.proposalsRoomSet.has(proposalsRoom)) {
    proposalStatus('No proposals channel for this teammate yet.', true); return;
  }
  if (!target || !ROOM_SHAPE_RE.test(target)) {
    proposalStatus('This conversation has no valid target room.', true); return;
  }
  const input = $('proposal-input');
  const body = (opts && typeof opts.body === 'string' ? opts.body
    : (input && input.value ? input.value : '')).trim();
  if (!body) { proposalStatus('Type a suggestion first.', true); return; }
  const content = {
    target_room: target,
    body,
    created_by: S.userId,
    origin_ts: Date.now(),
  };
  const txn = 'prop_' + Date.now() + '_' + Math.random().toString(36).slice(2);
  try {
    // NOTE: event type is the literal 'com.jkali.proposal'. This is the only
    // write endpoint in apps/master and it targets ONLY a proposals room.
    await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(proposalsRoom)
      + '/send/com.jkali.proposal/' + encodeURIComponent(txn), content);
    if (input) input.value = '';
    proposalStatus('');
    showSuggestion(body);
  } catch (e) {
    proposalStatus('Could not send suggestion: ' + String(e.message || e), true);
  }
}

// ===========================================================================
// Contacts view (v-contacts) — a searchable, person-grouped read of every
// teammate's shared address book (com.jkali.contact state, collected in
// buildByUser). GROUPED BY person_id: a handle and that person's mirror rooms
// (already tagged com.jkali.profile == the same person_id) fold under one
// header, exactly as groupByProfile clusters the conversation feed. A handle
// with a null person_id lists ungrouped. Selecting a handle opens the
// person-targeted composer, whose only write is submitProposal's identifier
// branch above — still a com.jkali.proposal, never a message.
// ===========================================================================

// Search over display_name / person_display / network_id, then group. Returns
// an ordered list of {kind:'person', ...} groups and {kind:'loose', contact}
// singletons. The person group's join key is (teammate label + person_id) so a
// person_id shared by two teammates never merges their address books.
function groupContactsView(contacts, feed, q) {
  const filtered = q
    ? contacts.filter(ct =>
        (ct.display_name || '').toLowerCase().includes(q)
        || (ct.person_display || '').toLowerCase().includes(q)
        || (ct.network_id || '').toLowerCase().includes(q))
    : contacts;
  const order = [];
  const groups = new Map();  // (label person_id) -> group
  for (const ct of filtered) {
    if (ct.person_id) {
      const key = ct.label + ' ' + ct.person_id;
      let g = groups.get(key);
      if (!g) {
        g = { kind: 'person', label: ct.label, personId: ct.person_id,
              display: ct.person_display || ct.display_name || ct.person_id,
              contacts: [], rooms: [] };
        groups.set(key, g);
        order.push(g);
      }
      g.contacts.push(ct);
      if (ct.person_display) g.display = ct.person_display;  // prefer a real name
    } else {
      order.push({ kind: 'loose', contact: ct });
    }
  }
  // Attach each person's mirror rooms (same join key the conversation feed uses:
  // profileId == person_id — and the same teammate).
  for (const g of groups.values()) {
    g.rooms = (feed || []).filter(c => c.profileId === g.personId && c.userLabel === g.label);
    // COMPUTED DIMENSIONALITY: the distinct set of platforms this one person
    // spans, across their handles AND their mirror rooms — a one-glance "this
    // person is on WhatsApp + iMessage + …" summary. Pure derivation over
    // already-grouped data; no extra reads.
    g.platforms = computePlatforms(g.contacts.map(c => c.source).concat(g.rooms.map(r => r.sourceId)));
  }
  return order;
}

// One address-book handle row (clickable/keyboard-activatable -> composer).
function buildContactRow(ct) {
  const row = el('div', 'convo contact-row');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  const name = ct.display_name || ct.person_display || ct.network_id;
  row.appendChild(el('div', 'avatar', (name || '?').slice(0, 1).toUpperCase()));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', sanitizeLine(name)));
  meta.appendChild(el('div', 'preview', sanitizeLine(ct.network_id)));
  row.appendChild(meta);
  row.appendChild(el('span', 'badge', ct.label || ''));
  row.appendChild(buildPlatBadge(ct.source));
  const open = () => selectContact(ct);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

// One person cluster: header + this person's handles + their mirror-room rows
// (reusing buildFeedRow, so each thread keeps its badge/preview/open behavior).
function buildContactPersonGroup(g) {
  const wrap = el('div', 'profile-group');
  const header = el('div', 'profile-header');
  header.appendChild(el('span', 'profile-avatar', (g.display || '?').slice(0, 1).toUpperCase()));
  header.appendChild(el('span', 'profile-name', sanitizeLine(g.display)));
  const h = g.contacts.length, t = g.rooms.length;
  header.appendChild(el('span', 'profile-count',
    h + ' handle' + (h === 1 ? '' : 's') + (t ? ' · ' + t + ' thread' + (t === 1 ? '' : 's') : '')));
  // Platform-badge summary row — reuses buildPlatBadge (the same per-source
  // badge already used on every contact/room row), never new icon rendering.
  const platRow = el('span', 'profile-platforms');
  for (const src of (g.platforms || [])) platRow.appendChild(buildPlatBadge(src));
  header.appendChild(platRow);
  wrap.appendChild(header);
  const members = el('div', 'profile-members');
  for (const ct of g.contacts) members.appendChild(buildContactRow(ct));
  for (const c of g.rooms) members.appendChild(buildFeedRow(c));
  wrap.appendChild(members);
  return wrap;
}

// ===========================================================================
// Persistent contacts index (B1) — a fast, backed-up copy of the contacts
// view folded into one record per person, kept in IndexedDB at the master
// origin. This is a read-side cache/backup ONLY: it is rebuilt from the
// already-in-memory MS state on every refresh (O(contacts), no extra /sync
// or network round-trips) and never gates rendering — every IndexedDB call
// is wrapped in try/catch so the app works fully even where IndexedDB is
// unavailable (a private window, disabled site data), falling back to the
// in-memory MS exactly as before this feature existed.
// ===========================================================================
const CONTACTS_IDB_NAME = 'beepa-master-contacts';
const CONTACTS_IDB_STORE = 'people';
const CONTACTS_IDB_VERSION = 1;

function openContactsDb() {
  return new Promise((resolve, reject) => {
    if (!('indexedDB' in window)) { reject(new Error('indexedDB unavailable')); return; }
    const req = indexedDB.open(CONTACTS_IDB_NAME, CONTACTS_IDB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(CONTACTS_IDB_STORE)) {
        db.createObjectStore(CONTACTS_IDB_STORE, { keyPath: 'key' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

// One record per person (schema — see apps/master/CLAUDE.md / the B1 report):
//   { key, person_id, teammate, display_name, platforms:[...],
//     handles:[{source, value}], rooms:[{room_id, source, name}],
//     first_seen, last_seen }
// `key` is (teammate label + person_id), or (teammate label + the bare
// handle) for a person_id-less contact, so two teammates' address books
// never collide and an ungrouped handle still gets its own durable record.
// Built from groupContactsView with an EMPTY query (i.e. unfiltered — the
// persisted index always covers every contact, independent of the current
// search box) over the already-in-memory MS.contacts/MS.feed.
function buildContactsIndexRecords(nowTs) {
  const groups = groupContactsView(MS.contacts, MS.feed, '');
  const records = [];
  for (const item of groups) {
    if (item.kind === 'person') {
      records.push({
        key: item.label + '|' + item.personId,
        person_id: item.personId,
        teammate: item.label,
        display_name: item.display || '',
        platforms: item.platforms || [],
        handles: item.contacts.map(c => ({ source: c.source, value: c.network_id })),
        rooms: item.rooms.map(r => ({ room_id: r.id, source: r.sourceId, name: r.title })),
        last_seen: nowTs,
      });
    } else {
      const ct = item.contact;
      records.push({
        key: (ct.label || '') + '|handle:' + ct.network_id,
        person_id: null,
        teammate: ct.label,
        display_name: ct.display_name || ct.person_display || ct.network_id,
        platforms: computePlatforms([ct.source]),
        handles: [{ source: ct.source, value: ct.network_id }],
        rooms: [],
        last_seen: nowTs,
      });
    }
  }
  return records;
}

// Fold this refresh's records into IndexedDB, preserving each record's
// earliest-seen `first_seen` (read-then-put per key, one shared transaction).
// Best-effort only: any failure (unavailable/blocked storage, quota) is
// swallowed — the contacts view itself never depends on this succeeding.
async function persistContactsIndex() {
  try {
    const records = buildContactsIndexRecords(Date.now());
    const db = await openContactsDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(CONTACTS_IDB_STORE, 'readwrite');
      const store = tx.objectStore(CONTACTS_IDB_STORE);
      tx.onerror = () => reject(tx.error);
      tx.oncomplete = () => resolve();
      for (const rec of records) {
        const getReq = store.get(rec.key);
        getReq.onsuccess = () => {
          const existing = getReq.result;
          rec.first_seen = (existing && existing.first_seen) || rec.last_seen;
          store.put(rec);
        };
        // getReq.onerror is left to the transaction's onerror above.
      }
    });
    db.close();
  } catch (e) {
    // IndexedDB unavailable or failed — no-op. MS stays the source of truth.
  }
}

// Read the full persisted index back (used by Export). Falls back to a
// freshly-built, in-memory-only record set (first_seen == last_seen) when
// IndexedDB cannot be read, so Export still works with storage disabled.
async function readContactsIndexAll() {
  try {
    const db = await openContactsDb();
    const records = await new Promise((resolve, reject) => {
      const tx = db.transaction(CONTACTS_IDB_STORE, 'readonly');
      const store = tx.objectStore(CONTACTS_IDB_STORE);
      const req = store.getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
    db.close();
    return records;
  } catch (e) {
    const now = Date.now();
    return buildContactsIndexRecords(now).map(r => Object.assign({ first_seen: now }, r));
  }
}

// Export/backup: download the full persisted index as a portable JSON file.
// A plain Blob + a real <a download> click — this is the master web app
// served from the master homeserver (not a sandboxed artifact), so a normal
// browser download works. This reads data only; it is not a write path.
async function exportContacts() {
  const status = $('contacts-export-status');
  try {
    const records = await readContactsIndexAll();
    const payload = { exported_at: new Date().toISOString(), contacts: records };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'beepa-contacts-' + new Date().toISOString().slice(0, 10) + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    if (status) { status.textContent = ''; status.classList.add('hidden'); }
  } catch (e) {
    if (status) {
      status.textContent = 'Could not export contacts: ' + String(e.message || e);
      status.classList.remove('hidden');
    }
  }
}

function renderContacts() {
  const q = (($('contacts-search') && $('contacts-search').value) || '').trim().toLowerCase();
  const list = $('list-body');
  if (!list) return;
  list.replaceChildren();
  const items = groupContactsView(visibleContacts(MS.contacts, MS.hidden), visibleFeed(MS.feed, MS.hidden), q);
  if (!items.length) {
    list.appendChild(elEmpty(q ? 'No contacts match "' + q + '".'
      : 'No shared contacts yet.'));
    return;
  }
  for (const item of items) {
    list.appendChild(item.kind === 'person' ? buildContactPersonGroup(item) : buildContactRow(item.contact));
  }
}

function contactProposalStatus(text, isError) {
  const s = $('contact-proposal-status');
  if (!s) return;
  s.textContent = text || '';
  s.classList.toggle('hidden', !text);
  s.classList.toggle('error', !!isError);
}

// Open the person-targeted composer for one handle. The composer's ONLY write
// is submitProposal({kind:'identifier', ...}) — a com.jkali.proposal, never a
// send. The identifier shown is the contact's own network_id (inert data).
function selectContact(ct) {
  if (!ct) return;
  MS.openContact = ct;
  const name = ct.person_display || ct.display_name || ct.network_id;
  $('contact-name').textContent = sanitizeLine(name);
  const badge = $('contact-badge');
  const b = buildPlatBadge(ct.source);
  badge.className = b.className;
  badge.textContent = b.textContent;
  $('contact-identifier').textContent = sanitizeLine(ct.network_id);
  const roomsBox = $('contact-rooms');
  if (roomsBox) {
    roomsBox.replaceChildren();
    const rooms = ct.person_id
      ? MS.feed.filter(c => c.profileId === ct.person_id && c.userLabel === ct.label) : [];
    for (const c of rooms) roomsBox.appendChild(buildFeedRow(c));
  }
  const input = $('contact-proposal-input');
  if (input) input.value = '';
  const proposalsRoom = ct.label ? MS.proposalsByUser.get(ct.label) : null;
  const send = $('contact-proposal-send');
  if (send) send.disabled = !proposalsRoom;
  contactProposalStatus(proposalsRoom ? '' : 'No proposals channel for this teammate yet.', !proposalsRoom);
  showWorkspace(true);
  setDetailMode('contact');
}

async function submitContactProposal() {
  const ct = MS.openContact;
  if (!ct) { contactProposalStatus('No contact selected.', true); return; }
  const input = $('contact-proposal-input');
  const body = (input && input.value) ? input.value : '';
  await submitProposal({
    kind: 'identifier',
    label: ct.label,
    source: ct.source,
    identifier: ct.network_id,
    display: ct.person_display || ct.display_name,
    body,
  });
}

// ---- navigation (same two-pane shell as apps/user) ----
function showSection(id) {
  for (const s of document.querySelectorAll('#content .view')) s.classList.toggle('hidden', s.id !== id);
}
function setActiveNav(key) {
  const rail = (typeof key === 'string' && key.indexOf('teammate:') === 0) ? 'teammates' : key;
  for (const b of document.querySelectorAll('.navitem')) {
    b.classList.toggle('active', b.dataset.navkey === key || b.dataset.navkey === rail);
  }
}
function showWorkspace(twoPane) {
  showSection('view-workspace');
  const listPane = $('list-pane');
  if (listPane) listPane.classList.toggle('hidden', !twoPane);
  const ws = $('workspace');
  if (ws) ws.classList.toggle('admin-only', !twoPane);
}
function setDetailMode(mode) {
  const pane = $('msgr-convo');
  const room = $('detail-room');
  const admin = $('detail-admin');
  const contact = $('detail-contact');
  if (room) room.classList.toggle('hidden', mode !== 'room');
  if (admin) admin.classList.toggle('hidden', mode !== 'admin');
  if (contact) contact.classList.toggle('hidden', mode !== 'contact');
  if (pane) pane.classList.toggle('no-selection', mode === 'empty' || mode === 'admin');
}
function showListSearch(show) {
  const input = $('search-input');
  if (input) input.classList.toggle('hidden', !show);
}
function closePopovers() {
  const n = $('settings-popover');
  if (n) n.classList.add('hidden');
  const b = $('nav-settings-toggle');
  if (b) b.setAttribute('aria-expanded', 'false');
}
function togglePopover(popId, btnId) {
  const pop = $(popId);
  const btn = $(btnId);
  if (!pop || !btn) return;
  const wasHidden = pop.classList.contains('hidden');
  closePopovers();
  if (wasHidden) {
    pop.classList.remove('hidden');
    btn.setAttribute('aria-expanded', 'true');
  }
}
function showContactsSearch(show) {
  const input = $('contacts-search');
  if (input) input.classList.toggle('hidden', !show);
  const btn = $('contacts-export');
  if (btn) btn.classList.toggle('hidden', !show);
}
function navTo(key) {
  closePopovers();
  if (MS.openRoomId && key !== 'room') {
    stopTail();
    setDetailMode('empty');
  }
  showContactsSearch(key === 'contacts');
  MS.activeView = key;
  setActiveNav(key);
  if (key === 'recent') {
    showWorkspace(true);
    showListSearch(false);
    setDetailMode('empty');
    renderRecent();
  } else if (key === 'search') {
    showWorkspace(true);
    showListSearch(true);
    setDetailMode('empty');
    renderSearch();
  } else if (key === 'contacts') {
    showWorkspace(true);
    showListSearch(false);
    setDetailMode('empty');
    renderContacts();
  } else if (key === 'teammates') {
    showWorkspace(true);
    showListSearch(false);
    setDetailMode('empty');
    renderTeammatesList();
  } else if (key === 'addteam') {
    showWorkspace(false);
    setDetailMode('admin');
    resetAddTeammate();
  } else if (key.indexOf('teammate:') === 0) {
    showWorkspace(true);
    showListSearch(false);
    setDetailMode('empty');
    renderTeammate(key.slice('teammate:'.length));
  }
}

// ---- add / link a teammate (manager-only; see ENROLL_BASE above) ----
// Reset the panel to its blank state whenever it is (re)opened, so a previous
// code is never left visible on screen.
function resetAddTeammate() {
  const err = $('addteam-error'); const result = $('addteam-result');
  if (err) { err.textContent = ''; err.classList.add('hidden'); }
  if (result) result.classList.add('hidden');
}

async function addTeammate() {
  const err = $('addteam-error');
  const result = $('addteam-result');
  const btn = $('addteam-btn');
  if (err) { err.textContent = ''; err.classList.add('hidden'); }
  if (result) result.classList.add('hidden');
  const username = ($('addteam-user').value || '').trim();
  if (!username) {
    if (err) { err.textContent = 'Enter a username.'; err.classList.remove('hidden'); }
    return;
  }
  if (btn) btn.disabled = true;
  try {
    // Deliberately NOT api() (which is pointed at the master CS API on 8018);
    // this is a direct call to the enroll/admin service on 8019 with the
    // signed-in manager's master token. Not a Matrix send.
    const res = await fetch(ENROLL_BASE + '/admin/add-teammate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + (S.token || '') },
      body: JSON.stringify({ username }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data && data.error ? data.error : ('HTTP ' + res.status));
    // textContent only — never innerHTML. sanitizeLine for the short fields,
    // sanitize for the (longer) redeem command.
    $('addteam-user-out').textContent = sanitizeLine(data.username || username);
    $('addteam-code').textContent = sanitizeLine(data.code || '');
    $('addteam-cmd').textContent = sanitize(data.redeem_cmd || '');
    if (result) result.classList.remove('hidden');
  } catch (e) {
    if (err) { err.textContent = String((e && e.message) || e); err.classList.remove('hidden'); }
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---- session ----
function forgetSession() {
  S.token = null; S.userId = null;
  MS.rooms = {}; MS.byUser = new Map(); MS.feed = [];
  stopTail();
  if (MS.pollTimer) { clearInterval(MS.pollTimer); MS.pollTimer = null; }
  try { sessionStorage.removeItem('master_token'); sessionStorage.removeItem('master_user'); } catch (e) {}
  const shell = $('shell'); if (shell) shell.classList.add('hidden');
  const signin = $('view-signin'); if (signin) signin.classList.remove('hidden');
}
async function signIn(user, pass) {
  const body = {
    type: 'm.login.password',
    identifier: { type: 'm.id.user', user },
    password: pass,
    initial_device_display_name: 'Master Console',
  };
  const data = await api('POST', '/_matrix/client/v3/login', body);
  S.token = data.access_token; S.userId = data.user_id;
  try { sessionStorage.setItem('master_token', S.token); sessionStorage.setItem('master_user', S.userId); } catch (e) {}
}
async function signOut() {
  try { await api('POST', '/_matrix/client/v3/logout', {}); } catch (e) {}
  forgetSession();
}

async function enterApp() {
  $('whoami').textContent = S.userId;
  $('view-signin').classList.add('hidden');
  $('shell').classList.remove('hidden');
  MS.hidden = loadHidden();
  try { await refreshAll(); } catch (e) { /* stays empty on error */ }
  navTo('recent');
  if (MS.pollTimer) clearInterval(MS.pollTimer);
  // Periodic re-snapshot for freshness (no in-place live merge needed for a
  // manager-facing recent/grouped list — a simple poll is enough here).
  MS.pollTimer = setInterval(() => { refreshAll().catch(() => {}); }, 20000);
}

// buildIdentifierProposalContent and latestRoomProposal are exported so a
// plain-node unit test can exercise the shape gates in isolation
// (tests/unit/proposal_identifier.test.js). Exporting them makes this module
// importable outside the browser, so the one top-level DOM binding below is
// guarded — importing under node must not touch `document`. In the browser
// `document` always exists and behavior is unchanged.
export { buildIdentifierProposalContent, latestRoomProposal };

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', () => {
  $('btn-signin').addEventListener('click', async () => {
    const err = $('signin-error');
    err.classList.add('hidden');
    try {
      await signIn($('in-user').value.trim(), $('in-pass').value);
      $('in-pass').value = '';
      await enterApp();
    } catch (e) {
      err.textContent = String(e.message || e);
      err.classList.remove('hidden');
    }
  });
  $('in-pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btn-signin').click(); });
  $('signout').addEventListener('click', signOut);

  $('nav-recent').dataset.navkey = 'recent';
  $('nav-recent').addEventListener('click', () => navTo('recent'));
  $('nav-search').dataset.navkey = 'search';
  $('nav-search').addEventListener('click', () => navTo('search'));
  const navContacts = $('nav-contacts');
  if (navContacts) {
    navContacts.dataset.navkey = 'contacts';
    navContacts.addEventListener('click', () => navTo('contacts'));
  }
  const navTeammates = $('nav-teammates');
  if (navTeammates) {
    navTeammates.dataset.navkey = 'teammates';
    navTeammates.addEventListener('click', () => navTo('teammates'));
  }
  const st = $('nav-settings-toggle');
  if (st && !st.dataset.wired) {
    st.dataset.wired = '1';
    st.addEventListener('click', (e) => {
      e.stopPropagation();
      togglePopover('settings-popover', 'nav-settings-toggle');
    });
  }
  const navAdd = $('nav-addteam');
  if (navAdd) { navAdd.dataset.navkey = 'addteam'; navAdd.addEventListener('click', () => navTo('addteam')); }
  if (!window.__masterPopoverCloser) {
    window.__masterPopoverCloser = true;
    document.addEventListener('click', (e) => {
      const pop = $('settings-popover');
      if (!pop || pop.classList.contains('hidden')) return;
      if (pop.contains(e.target)) return;
      const btn = $('nav-settings-toggle');
      if (btn && btn.contains(e.target)) return;
      closePopovers();
    });
  }
  const addBtn = $('addteam-btn');
  if (addBtn) addBtn.addEventListener('click', () => { addTeammate().catch(() => {}); });
  const addInput = $('addteam-user');
  if (addInput) addInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addTeammate().catch(() => {}); } });
  // Clipboard convenience only — reads nothing back, sends nothing anywhere;
  // just copies the code text already shown on screen via textContent.
  const copyBtn = $('addteam-copy');
  if (copyBtn) copyBtn.addEventListener('click', () => {
    const code = $('addteam-code');
    const text = code ? code.textContent : '';
    if (text && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(() => {});
    }
  });
  $('search-input').addEventListener('input', renderSearch);
  const contactsSearch = $('contacts-search');
  if (contactsSearch) contactsSearch.addEventListener('input', renderContacts);
  // Export/backup only — reads the persisted index (or falls back to the
  // in-memory MS) and downloads it as JSON. No write path.
  const contactsExport = $('contacts-export');
  if (contactsExport) contactsExport.addEventListener('click', () => { exportContacts().catch(() => {}); });
  const contactBack = $('contact-back');
  if (contactBack) contactBack.addEventListener('click', () => {
    setDetailMode('empty');
    navTo('contacts');
  });
  const contactSend = $('contact-proposal-send');
  if (contactSend) contactSend.addEventListener('click', () => { submitContactProposal().catch(() => {}); });
  const contactInput = $('contact-proposal-input');
  if (contactInput) contactInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submitContactProposal().catch(() => {}); }
  });
  $('room-back').addEventListener('click', () => {
    stopTail();
    setDetailMode('empty');
    navTo(MS.activeView === 'room' ? 'recent' : MS.activeView);
  });

  // Compose-proposal wiring (the one write path — a proposal, not a send).
  const psend = $('proposal-send');
  if (psend) psend.addEventListener('click', () => { submitProposal().catch(() => {}); });
  const pinput = $('proposal-input');
  if (pinput) pinput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submitProposal().catch(() => {}); }
  });

  // restore session, else auto-login from a provisioned local session file
  (async () => {
    try {
      const t = sessionStorage.getItem('master_token'), u = sessionStorage.getItem('master_user');
      if (t && u) { S.token = t; S.userId = u; await enterApp(); return; }
    } catch (e) {}
    // Passwordless local login (see apps/user): master-setup.sh writes
    // apps/master/session.local.json with the manager token. Loopback-only,
    // same-origin fetch (CSP connect-src 'self'). GET only — no non-GET call is
    // added here, so harness scenario 7's write-surface scan is unaffected.
    try {
      const r = await fetch('session.local.json', { cache: 'no-store' });
      if (r.ok) {
        const s = await r.json();
        if (s && s.access_token && s.user_id) {
          S.token = s.access_token; S.userId = s.user_id;
          try { sessionStorage.setItem('master_token', S.token); sessionStorage.setItem('master_user', S.userId); } catch (e) {}
          await enterApp(); return;
        }
      }
    } catch (e) {}
    $('shell').classList.add('hidden');
    $('view-signin').classList.remove('hidden');
  })();
});
