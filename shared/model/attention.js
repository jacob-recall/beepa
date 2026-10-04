// Pure, zero-import helpers for the Triage Rail list: initials, draft
// pending/retired tests, the row indicator stack, person clustering, and the
// list filter. Shared by apps/user (through shared/ui) and apps/master (which
// imports this leaf directly — it carries no shared/ui import, so importing
// it adds no send path to the master bundle). No DOM, no network, no S.

const INDICATOR_ORDER = ['unread', 'draft', 'overdue', 'reconnect'];

function initials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  const first = parts[0][0] || '';
  const last = parts.length > 1 ? (parts[parts.length - 1][0] || '') : '';
  return (first + last).toUpperCase();
}

function finitePositive(n) { return typeof n === 'number' && isFinite(n) && n > 0; }

// FAIL CLOSED: a draft is pending only when the room's last activity is KNOWN
// and the draft is newer than it. Unknown/zero activity => not pending.
function draftPending(draftTs, lastActivityTs) {
  return finitePositive(draftTs) && finitePositive(lastActivityTs) && draftTs > lastActivityTs;
}

function byTsDesc(a, b) { return (b.ts || 0) - (a.ts || 0); }

function pendingDrafts(drafts, lastActivityTs) {
  return (Array.isArray(drafts) ? drafts : [])
    .filter(d => d && draftPending(d.ts, lastActivityTs)).sort(byTsDesc);
}

function retiredDrafts(drafts, lastActivityTs, now, keepMs = 86400000) {
  return (Array.isArray(drafts) ? drafts : [])
    .filter(d => d && finitePositive(d.ts) && !draftPending(d.ts, lastActivityTs) && (now - d.ts) < keepMs)
    .sort(byTsDesc);
}

function indicatorsFor(flags) {
  const f = flags || {};
  return INDICATOR_ORDER.filter(k => !!f[k]);
}

function profileFor(roomProfile, roomId) {
  if (!roomProfile) return null;
  const p = typeof roomProfile.get === 'function' ? roomProfile.get(roomId) : roomProfile[roomId];
  return (p && typeof p.id === 'string' && p.id) ? p : null;
}

// One item per contact profile (members = that profile's rooms present in
// `records`), one item per unlinked room. Rooms merge ONLY through a profile
// link, never by name. `unread` sums members; `draft` counts members' pending
// drafts; `lastTs` is the newest member's.
function clusterFeed(records, roomProfile) {
  const order = [];
  const clusters = new Map();
  for (const rec of (Array.isArray(records) ? records : [])) {
    if (!rec || typeof rec.id !== 'string') continue;
    const p = profileFor(roomProfile, rec.id);
    const pend = pendingDrafts(rec.drafts, rec.lastTs).length;
    if (!p) { order.push({ kind: 'single', rec, lastTs: rec.lastTs || 0 }); continue; }
    let c = clusters.get(p.id);
    if (!c) {
      c = { kind: 'cluster', profileId: p.id, displayName: p.displayName || '', members: [], lastTs: 0, unread: 0, draft: 0 };
      clusters.set(p.id, c);
      order.push(c);
    }
    if (p.displayName) c.displayName = p.displayName;
    c.members.push(rec);
    c.unread += (typeof rec.unread === 'number' && rec.unread > 0) ? rec.unread : 0;
    c.draft += pend;
    if ((rec.lastTs || 0) > c.lastTs) c.lastTs = rec.lastTs || 0;
  }
  for (const c of clusters.values()) c.members.sort((a, b) => (b.lastTs || 0) - (a.lastTs || 0));
  order.sort((a, b) => b.lastTs - a.lastTs);
  return order;
}

function applyFilter(items, filter, flags) {
  const list = Array.isArray(items) ? items : [];
  if (filter === 'drafts') return list.filter(it => flags(it).includes('draft'));
  if (filter === 'needs') {
    const flagged = [], rest = [];
    for (const it of list) (flags(it).length ? flagged : rest).push(it);
    return flagged.concat(rest);
  }
  return list;
}

export { INDICATOR_ORDER, initials, draftPending, pendingDrafts, retiredDrafts, indicatorsFor, clusterFeed, applyFilter };
