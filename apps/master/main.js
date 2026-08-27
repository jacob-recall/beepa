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
// NO composer, NO send call, anywhere in this file.

import { api, configureMatrixBase, setOnUnauthorized, ROOMID_RE, MXC_RE } from '../../shared/matrix/client.js';
import { $, el, sanitize, sanitizeLine } from '../../shared/ui/el.js';
import { S } from '../../shared/state.js';

// The MASTER homeserver base (same origin the transport is pointed at below).
// Authenticated media (Synapse default) cannot be fetched by a bare <img src>
// — the token must ride in an Authorization header — so real media is fetched
// as bytes with S.token and shown via an object URL (CSP allows img/media blob:).
const MASTER_BASE = 'http://127.0.0.1:8018';

// The master enroll/admin service (master/enroll.py serve). The ONLY thing
// this app calls here is the manager-authenticated POST /admin/add-teammate —
// an admin provisioning action, NOT a Matrix send path and NOT a proposal. The
// service itself verifies the caller is @manager:master before doing anything.
// CSP connect-src is extended by exactly this origin for this one call.
const ENROLL_BASE = 'http://127.0.0.1:8019';

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
  activeView: 'recent',
  openRoomId: null,
  openRoomUser: null,
  openRoomSourceId: null,  // the open mirror room's com.jkali.source, for the per-bubble mini platform badge
  openMirrorOf: null,  // the open mirror room's teammate-local room id (proposal target)
  lastDayKey: null,    // last rendered day-divider key, reset per room open (see renderBubble)
  tailRunning: false,
  tailSince: null,
  pollTimer: null,
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

function localpart(mxid) {
  const s = typeof mxid === 'string' ? mxid : '';
  const colon = s.indexOf(':');
  const lp = (colon > 0 ? s.slice(0, colon) : s).replace(/^@/, '');
  return sanitizeLine(lp) || 'unknown';
}

function parseSnapshot(data) {
  const rooms = {};
  const join = (data.rooms && data.rooms.join) || {};
  for (const rid of Object.keys(join)) {
    const r = join[rid];
    const info = { id: rid, name: null, isSpace: false, children: [], sourceId: null,
                   lastBody: '', lastTs: 0, mirrorOf: null, isProposals: false,
                   profileId: null, profileDisplayName: null };
    // State from BOTH the `state` block and `timeline` (a newer space's
    // create/name/child events can still be in the timeline window).
    const stateEvents = ((r.state && r.state.events) || []).concat((r.timeline && r.timeline.events) || []);
    const seenChild = new Set();
    for (const e of stateEvents) {
      if (e.type === 'm.room.name' && e.state_key === '') info.name = e.content && e.content.name;
      if (e.type === 'm.room.create' && e.content && e.content.type === 'm.space') info.isSpace = true;
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

// Teammate spaces are named "space:<User>" (master/provision.sh). Discovered
// dynamically from whatever the manager is actually joined to — never a
// hardcoded roster — so a newly-provisioned teammate appears with no code
// change. Each mirror room is listed only if it is itself a joined room (same
// "space child must also be joined" gate apps/user's buildConvos uses).
function buildByUser(rooms) {
  const byUser = new Map();
  const proposalsByUser = new Map();
  const proposalsRoomSet = new Set();
  const spaces = Object.values(rooms).filter(r =>
    r.isSpace && typeof r.name === 'string' && r.name.indexOf('space:') === 0);
  for (const sp of spaces) {
    const label = sanitizeLine(sp.name.slice('space:'.length)) || sp.id;
    const convos = [];
    for (const childId of sp.children) {
      const r = rooms[childId];
      if (!r) continue;                              // not in the joined set -> excluded
      if (!ROOMID_RE.test(childId)) continue;         // malformed id -> excluded
      r.userLabel = label;                            // back-reference for the room viewer
      // A proposals room is the write channel, not a conversation: record it as
      // this teammate's proposal target, and NEVER list it as a readable convo.
      if (r.isProposals) {
        proposalsByUser.set(label, childId);
        proposalsRoomSet.add(childId);
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
  MS.proposalsByUser = proposalsByUser;
  MS.proposalsRoomSet = proposalsRoomSet;
  return byUser;
}

// The uplink INVITES the manager into each teammate's proposals room (it cannot
// force-join another account). Auto-accept ONLY invites that carry the
// com.jkali.proposals marker in their invite_state — so the manager can write a
// proposal there — and nothing else. Joining is membership, not a send; the only
// write this app ever performs is the single com.jkali.proposal in submitProposal.
// Mirror-room membership is unchanged from V1 (accepted out of band).
async function autoJoinProposalInvites(data) {
  const invite = (data.rooms && data.rooms.invite) || {};
  let joinedAny = false;
  for (const rid of Object.keys(invite)) {
    if (!ROOMID_RE.test(rid)) continue;
    const evs = (invite[rid].invite_state && invite[rid].invite_state.events) || [];
    const isProposals = evs.some(e => e.type === 'com.jkali.proposals' && e.state_key === '');
    if (!isProposals) continue;
    try {
      await api('POST', '/_matrix/client/v3/rooms/' + encodeURIComponent(rid) + '/join', {});
      joinedAny = true;
    } catch (e) { /* leave un-joined on failure; retried next refresh */ }
  }
  return joinedAny;
}

async function refreshAll() {
  let data = await fetchSnapshot();
  if (await autoJoinProposalInvites(data)) data = await fetchSnapshot();
  const rooms = parseSnapshot(data);
  MS.rooms = rooms;
  MS.byUser = buildByUser(rooms);
  MS.feed = [].concat(...[...MS.byUser.values()]).sort((a, b) => b.lastTs - a.lastTs);
  renderTeammateNav();
  if (MS.activeView === 'recent') renderRecent();
  else if (MS.activeView === 'search') renderSearch();
  else if (typeof MS.activeView === 'string' && MS.activeView.indexOf('teammate:') === 0) {
    renderTeammate(MS.activeView.slice('teammate:'.length));
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
  const sub = $('recent-sub');
  if (sub) {
    const n = MS.byUser.size;
    sub.textContent = 'across ' + n + ' teammate' + (n === 1 ? '' : 's') + ' · shared conversations only';
  }
  const list = $('recent-list');
  if (!list) return;
  list.replaceChildren();
  if (!MS.feed.length) { list.appendChild(elEmpty('No shared conversations yet.')); return; }
  for (const item of groupByProfile(MS.feed.slice(0, 200))) list.appendChild(buildListItem(item));
}

// Sidebar teammate rows (mockup 1f left rail): initials avatar + name + a
// count of that teammate's shared conversations. Purely presentational over
// already-fetched MS.byUser; the count is just that teammate's convo list length.
function renderTeammateNav() {
  const nav = $('nav-teammates');
  if (!nav) return;
  nav.replaceChildren();
  for (const [label, convos] of MS.byUser) {
    const key = 'teammate:' + label;
    const btn = el('button', 'teammate-row');
    btn.type = 'button';
    btn.dataset.navkey = key;
    btn.appendChild(el('span', 'teammate-avatar', initials(label)));
    btn.appendChild(el('span', 'teammate-name', label));
    btn.appendChild(el('span', 'teammate-count', String(convos.length)));
    btn.addEventListener('click', () => navTo(key));
    nav.appendChild(btn);
  }
  setActiveNav(MS.activeView);
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
  $('teammate-title').textContent = label;
  const convos = (MS.byUser.get(label) || []).slice().sort((a, b) => b.lastTs - a.lastTs);
  $('teammate-sub').textContent = convos.length + ' shared conversation' + (convos.length === 1 ? '' : 's');
  const list = $('teammate-list');
  list.replaceChildren();
  if (!convos.length) { list.appendChild(elEmpty('Nothing shared yet.')); return; }
  for (const item of groupByProfile(convos)) list.appendChild(buildListItem(item));
}

// #search-input is a pure client-side filter over the in-memory flattened
// feed; it never builds a URL, sends a command, or navigates.
function renderSearch() {
  const q = (($('search-input') && $('search-input').value) || '').trim().toLowerCase();
  const out = $('search-results');
  if (!out) return;
  out.replaceChildren();
  if (!q) { out.appendChild(elEmpty('Type to search across every teammate.')); return; }
  const rows = MS.feed.filter(c =>
    c.title.toLowerCase().includes(q) || (c.preview || '').toLowerCase().includes(q));
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
  const senderName = (ev.content && typeof ev.content['com.jkali.sender_name'] === 'string')
    ? sanitizeLine(ev.content['com.jkali.sender_name'])
    : localpart(ev.sender);
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
  showSection('view-room');

  try {
    const q = '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/messages?dir=b&limit=100';
    const data = await api('GET', q);
    if (MS.openRoomId !== roomId) return;             // navigated away mid-fetch
    const chunk = Array.isArray(data.chunk) ? data.chunk : [];
    // §8.2/§11: sort by com.jkali.origin_ts — backfill can arrive out of
    // chronological order, so timeline/arrival order is not display order.
    const sorted = chunk.slice().sort((a, c2) => mirrorTs(a) - mirrorTs(c2));
    for (const ev of sorted) renderBubble(ev);
    if (box) box.scrollTop = box.scrollHeight;
  } catch (e) {
    roomStatus('Could not load messages: ' + String(e.message || e));
  }
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
        const box = $('room-messages');
        if (box) box.scrollTop = box.scrollHeight;
      }
    } catch (e) {
      if (!S.token) { MS.tailRunning = false; return; }
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}
function stopTail() { MS.tailRunning = false; MS.openRoomId = null; MS.openRoomUser = null; MS.openRoomSourceId = null; MS.openMirrorOf = null; }

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
  const tmpl = $('proposal-template');
  if (tmpl) tmpl.checked = false;
  // Collapsed by default each time a room is opened (mockup 1g: a footer note
  // + "Suggest a reply" button; the compose fields expand only on request).
  const compose = $('proposal-compose');
  if (compose) compose.classList.add('hidden');
  if (!ctx) { pane.classList.add('hidden'); return; }
  pane.classList.remove('hidden');
  const note = $('proposal-note');
  if (note) {
    note.textContent = "You can't send in this conversation. A suggestion goes to "
      + sanitizeLine(ctx.label) + "'s inbox as a draft — they decide whether to send it.";
  }
  $('proposal-target-label').textContent = sanitizeLine(rec.name || ctx.target);
  loadTemplates(ctx.proposalsRoom).catch(() => {});
}

function proposalStatus(text, isError) {
  const s = $('proposal-status');
  if (!s) return;
  s.textContent = text || '';
  s.classList.toggle('hidden', !text);
  s.classList.toggle('error', !!isError);
}

// Read existing reusable templates (com.jkali.proposal events with template:true)
// from this teammate's proposals room and offer them in a picker. Pure read.
async function loadTemplates(proposalsRoom) {
  const sel = $('proposal-templates');
  if (!sel) return;
  sel.replaceChildren();
  const opt0 = el('option', null, 'Insert a saved template…');
  opt0.value = '';
  sel.appendChild(opt0);
  if (!ROOMID_RE.test(proposalsRoom) || !MS.proposalsRoomSet.has(proposalsRoom)) return;
  try {
    const q = '/_matrix/client/v3/rooms/' + encodeURIComponent(proposalsRoom) + '/messages?dir=b&limit=100';
    const data = await api('GET', q);
    const chunk = Array.isArray(data.chunk) ? data.chunk : [];
    const seen = new Set();
    for (const e of chunk) {
      if (!e || e.type !== 'com.jkali.proposal' || !e.content) continue;
      if (e.content.template !== true) continue;
      const body = typeof e.content.body === 'string' ? e.content.body : '';
      if (!body || seen.has(body)) continue;
      seen.add(body);
      const label = body.length > 60 ? body.slice(0, 57) + '…' : body;
      const opt = el('option', null, sanitizeLine(label));
      opt.value = body;                                 // full body via .value (not HTML)
      sel.appendChild(opt);
    }
  } catch (e) { /* templates are optional; leave just the placeholder */ }
}

// The single guarded write in this app. Defense in depth:
//  - the destination MUST be a ROOMID_RE-valid id that is in the discovered
//    proposals-room allowlist (never a stale/typed id, never a mirror room);
//  - the target_room is shape-checked (it is a foreign teammate-local id, so the
//    master's server-pinned ROOMID_RE does not apply);
//  - the event TYPE is the hardcoded literal 'com.jkali.proposal' — there is no
//    code path here that PUTs /send/m.room.message anywhere.
const LOCAL_ROOMID_RE = /^![^:]+:[A-Za-z0-9.\-:]+$/;   // any-server room-id shape
async function submitProposal() {
  const proposalsRoom = MS.openRoomUser ? MS.proposalsByUser.get(MS.openRoomUser) : null;
  const target = MS.openMirrorOf;
  if (!proposalsRoom || !ROOMID_RE.test(proposalsRoom) || !MS.proposalsRoomSet.has(proposalsRoom)) {
    proposalStatus('No proposals channel for this teammate yet.', true); return;
  }
  if (!target || !LOCAL_ROOMID_RE.test(target)) {
    proposalStatus('This conversation has no valid target room.', true); return;
  }
  const input = $('proposal-input');
  const body = (input && input.value ? input.value : '').trim();
  if (!body) { proposalStatus('Type a suggestion first.', true); return; }
  const isTemplate = !!($('proposal-template') && $('proposal-template').checked);
  const content = {
    target_room: target,
    body,
    created_by: S.userId,
    origin_ts: Date.now(),
  };
  if (isTemplate) content.template = true;
  const txn = 'prop_' + Date.now() + '_' + Math.random().toString(36).slice(2);
  proposalStatus('Sending suggestion…');
  try {
    // NOTE: event type is the literal 'com.jkali.proposal'. This is the only
    // write endpoint in apps/master and it targets ONLY a proposals room.
    await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(proposalsRoom)
      + '/send/com.jkali.proposal/' + encodeURIComponent(txn), content);
    if (input) input.value = '';
    if ($('proposal-template')) $('proposal-template').checked = false;
    proposalStatus('Suggestion sent to ' + sanitizeLine(MS.openRoomUser || 'teammate')
      + ' for review. It was not sent to anyone externally.');
    if (isTemplate) loadTemplates(proposalsRoom).catch(() => {});
  } catch (e) {
    proposalStatus('Could not send suggestion: ' + String(e.message || e), true);
  }
}

// ---- navigation ----
function showSection(id) {
  for (const s of document.querySelectorAll('#content .view')) s.classList.toggle('hidden', s.id !== id);
}
function setActiveNav(key) {
  for (const b of document.querySelectorAll('.tab, .teammate-row')) b.classList.toggle('active', b.dataset.navkey === key);
}
function navTo(key) {
  MS.activeView = key;
  setActiveNav(key);
  if (key === 'recent') { showSection('view-recent'); renderRecent(); }
  else if (key === 'search') { showSection('view-search'); renderSearch(); }
  else if (key === 'addteam') { showSection('view-addteam'); resetAddTeammate(); }
  else if (key.indexOf('teammate:') === 0) { showSection('view-teammate'); renderTeammate(key.slice('teammate:'.length)); }
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
  try { await refreshAll(); } catch (e) { /* stays empty on error */ }
  navTo('recent');
  if (MS.pollTimer) clearInterval(MS.pollTimer);
  // Periodic re-snapshot for freshness (no in-place live merge needed for a
  // manager-facing recent/grouped list — a simple poll is enough here).
  MS.pollTimer = setInterval(() => { refreshAll().catch(() => {}); }, 20000);
}

document.addEventListener('DOMContentLoaded', () => {
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
  const navAdd = $('nav-addteam');
  if (navAdd) { navAdd.dataset.navkey = 'addteam'; navAdd.addEventListener('click', () => navTo('addteam')); }
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
  $('room-back').addEventListener('click', () => { stopTail(); navTo(MS.activeView === 'room' ? 'recent' : MS.activeView); });

  // Expand/collapse the proposal compose fields (pure CSS class toggle — no
  // write path here; the only write is submitProposal(), wired separately below).
  const ptoggle = $('proposal-toggle');
  if (ptoggle) ptoggle.addEventListener('click', () => {
    const compose = $('proposal-compose');
    if (compose) compose.classList.toggle('hidden');
  });

  // Compose-proposal wiring (the one write path — a proposal, not a send).
  const psend = $('proposal-send');
  if (psend) psend.addEventListener('click', () => { submitProposal().catch(() => {}); });
  const ptpl = $('proposal-templates');
  if (ptpl) ptpl.addEventListener('change', () => {
    const input = $('proposal-input');
    if (input && ptpl.value) { input.value = ptpl.value; input.focus(); }
    ptpl.value = '';
  });

  try {
    const t = sessionStorage.getItem('master_token'), u = sessionStorage.getItem('master_user');
    if (t && u) { S.token = t; S.userId = u; enterApp(); return; }
  } catch (e) {}
  $('shell').classList.add('hidden');
  $('view-signin').classList.remove('hidden');
});
