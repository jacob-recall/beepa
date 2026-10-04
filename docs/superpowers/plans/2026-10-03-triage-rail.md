# Triage Rail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the teammate app and the manager console to the Triage Rail layout: attention-signalling rows, clustered multi-platform people, ghost drafts in the composer instead of a Proposals tab, read state, and the backend (uplink) support that makes the manager see what the teammate sees and that stops the Direct auto-send double text.

**Architecture:** All new list/row logic is pure functions in a new zero-import module `shared/model/attention.js`, consumed by `shared/ui/rows.js` and `shared/ui/search.js` (teammate) and by `apps/master/main.js` (manager, which must not import `shared/ui/`). Read state rides on the existing `/sync` payloads (`unread_notifications`, `m.receipt`, `m.fully_read`). Drafts stay in `apps/user/proposals.js` but render into the composer; the feed record carries the room's unhandled drafts and the pending test runs at render time, so a draft retires the moment the thread moves on. The uplink gains one refusal gate (`superseded`), one owned state event per mirror (`com.jkali.read_state`), and one cosmetic content stamp (`com.jkali.origin_avatar`).

**Tech Stack:** Native ES modules (no bundler), Python 3 stdlib (uplink), SQLite, Matrix client-server API, plain-node and plain-python unit tests under `tests/unit/`, discovered by `tests/run.py`.

**Spec:** `docs/superpowers/specs/2026-10-03-product-roadmap-design.md` (F1, F2, F3 avatars-from-bridge only, F5 partial). Layout: artifact pages "Beepa Layout Studies" and "Triage Rail".

## Global Constraints

- No new send path. The browser sends only through `sendConvoMessage()` in `shared/ui/chat.js`. The daemon sends only through `_auto_send()` behind `_direct_send_gate()` in `agents/uplink/uplink.py`.
- `shared/` never imports from `apps/`. Apps extend shared UI through `set<X>Hook()` callbacks or by writing fields on `S` / feed records.
- `apps/master/main.js` must not import any of `shared/ui/render.js, rows.js, nav.js, chat.js, search.js, sources.js, connections.js`. It MAY import `shared/model/*.js` leaves with zero `shared/ui` imports, and `shared/ui/el.js`.
- No `innerHTML`. Build DOM with `el()` and `textContent`. CSP in both `index.html` files is unchanged (`img-src 'self' blob:` already allows avatar blobs). `tests/unit/csp_parity.test.js` must stay green.
- Fail closed: unknown last-activity means a draft is not pending; a missing receipt renders no caption; a gate that cannot read the timeline refuses.
- `localStorage` is per-viewer convenience only, never authorization.
- Consent resolver files (`shared/model/consent.js`, `agents/uplink/consent.py`) are NOT touched by this plan.
- Do not commit or push. The repo profile is conservative; report changed files at the end of each task.
- Run after every task: `tests/run.py --unit-only` (or `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node <test>` for a single JS test, `python3 <test>` for Python).

---


## Security review (2026-10-03, pilotfish:security-reviewer, read-only) — Tasks 5 and 6

Verdict: PROCEED WITH FIXES. Nothing widens the uplink auto-send path and nothing adds a second browser send path. Dispositions below are binding on Tasks 5, 6 and 7.

| # | Finding | Sev | Disposition |
|---|---|---|---|
| F14 | Ghost rendered `sanitizeLine(body)` (64-char clamp, newlines collapsed) but sent the raw body (8000 clamp): the teammate could approve text they never saw. | P1 | **FIX** (Task 5): display and send the SAME string, `sanitize(body)`; `prefillComposer(shown)` on Edit/Restore. Row previews keep `sanitizeLine`. Test: the string handed to `sendConvoMessage` equals the rendered `.ghost-text` for a 500-char body. |
| F5 | D2-3 admits `origin_server_ts` up to +60 s; a future-dated proposal hides real activity in that window from the superseded gate. | P2 | **FIX** (Task 6): compare against `min(ots, now_ms)`. Test: `ots = now + 30000`, local message at `now` → `superseded`. |
| F9 | Read-state leg in `tail_once` skipped the per-write consent recheck and put a master call inside local ingestion (today zero master calls there). | P2 | **FIX** (Task 6): write only when `active_link_for_dispatch()` and `archive_level(room) in ("share","direct")`; `self.master(..., timeout=15)`; catch `MasterUnreachable` explicitly; never raise. |
| F12 | Inbound content is copied whole; a remote party can inject `com.jkali.origin_avatar` / `com.jkali.from_proposal`; planned `suggestionStates()` did not require `from_me`. | P2 | **FIX** (Task 6 + 7): in `_forward_message` unconditionally `pop` `ORIGIN_AVATAR_KEY` before the conditional set; `pop` `FROM_PROPOSAL_KEY` and `AUTO_SENT_FROM_PROPOSAL_KEY` unless `content[FROM_ME_KEY] is True`. In `suggestionStates()` match both keys only on messages with `content['com.jkali.from_me'] === true`. Test for each. |
| F13b | `_master_avatar_for` cached successes only: an unfetchable/oversized avatar re-downloads (≤25 MB) per message. | P2 | **FIX** (Task 6): cache failure as `"-"` per local mxc; treat as "no avatar". |
| F13c | `destination_binding` does not clear `avatar:%` / `read_state:%` meta: stale old-master mxc stamps and a suppressed first read-state PUT after rebinding. | P2 | **FIX** (Task 6): extend the `DELETE FROM meta` in `agents/uplink/durable_sync.py` (~line 290) with `OR k LIKE 'avatar:%' OR k LIKE 'read_state:%'`. |
| F17 | `parseProposal` prefers manager-controlled `content.origin_ts`; a far-future value keeps a ghost pending forever. | P2 | **FIX** (Task 5): `ts = min(origin_server_ts, origin_ts)` when both present. Test: `origin_ts: Date.now()+1e12` → `draftPending(p.ts, lastTs) === false`. |
| F3 | Superseded gate's `/messages` GET runs before the cheap cap gate; a hostile master can amplify local reads. | P3 | **Accept, with the cheap reorder**: place D2-12 AFTER D2-6 (last gate). Order is safe either way (the cap tick happens in `_auto_send`). |
| F4 | Superseded self-limits a manager burst to one auto-send per room per batch (P1's own send supersedes P2). | P3 | Accept; state it in `agents/uplink/CLAUDE.md` next to the gate. |
| F6 | Empty `/messages` page reads as quiet. | P3 | Accept; docstring says so. |
| F7 | TOCTOU between gate read and PUT is inherent. | P3 | Accept; CLAUDE.md says "reduces, does not eliminate". |
| F10 | `teammate_read_ts` is behavioral telemetry, product-authorized (spec) but new. | P3 | Accept; add one line to the apps/user sharing copy (`#settings-section-sharing` lead) and `docs/SHARE-LOGIC.md`. |
| F11 | Revocation leaves the state event readable to the kicked manager up to their leave, like messages. | P3 | Accept; no new erasure claims. |
| F13d | `_media_retryable` side effect is correct only because the avatar call precedes the media block. | P3 | Accept; add a comment at the call site. |
| F16 | Retire-all-on-send hides sibling suggestions via localStorage instead of showing them retired. | P3 | **Adopt the simplification** (Task 5): do NOT mark siblings handled on send; the sent message bumps `lastTs` and `draftPending` retires them visibly. |
| F1, F2, F8, F13a, F15, F18 | Verified clean: gate is a pure extra refusal; no leak in logs/audit; manager is PL 0 with `state_default: 100`; avatar fetch is the existing media class with size cap + consent recheck; only one `/send/m.room.message` PUT exists; browser retirement is strictly laxer than the daemon gate (safe direction). | — | Accept. |

---

### Task 1: Pure attention model

**Files:**
- Create: `shared/model/attention.js`
- Test: `tests/unit/attention.test.js`

**Interfaces:**
- Produces:
  - `initials(name: string): string` two uppercase letters from the first and last word, `'?'` when empty.
  - `draftPending(draftTs: number, lastActivityTs: number): boolean` true only when both are finite positive numbers and `draftTs > lastActivityTs`.
  - `pendingDrafts(drafts: Array<{ts:number}>, lastActivityTs: number): Array` the subset that is pending, newest first.
  - `retiredDrafts(drafts, lastActivityTs, now: number, keepMs = 86400000): Array` not pending, `now - ts < keepMs`, newest first.
  - `indicatorsFor({unread, draft, overdue, reconnect}): string[]` ordered subset of `['unread','draft','overdue','reconnect']`.
  - `clusterFeed(records: Array<FeedRecord>, roomProfile: Map<string,{id,displayName}>|Object): Array<Item>` where `Item` is `{kind:'single', rec}` or `{kind:'cluster', profileId, displayName, members: FeedRecord[], lastTs, unread, draft}`; members sorted by `lastTs` desc; items sorted by `lastTs` desc.
  - `applyFilter(items: Item[], filter: 'all'|'needs'|'drafts', flags: (item) => string[]): Item[]` 'all' unchanged, 'needs' = flagged first (stable), 'drafts' = only items whose flags include 'draft'.

- [ ] **Step 1: Write the failing test**

```js
// tests/unit/attention.test.js
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/attention.test.js
import assert from 'node:assert/strict';
import {
  initials, draftPending, pendingDrafts, retiredDrafts, indicatorsFor, clusterFeed, applyFilter,
} from '../../shared/model/attention.js';

assert.equal(initials('Maya Rodriguez'), 'MR');
assert.equal(initials('maya'), 'M');
assert.equal(initials('  '), '?');
assert.equal(initials('Product launch crew'), 'PC');

// fail closed: unknown activity => not pending
assert.equal(draftPending(100, 0), false);
assert.equal(draftPending(100, undefined), false);
assert.equal(draftPending(100, NaN), false);
assert.equal(draftPending(100, 50), true);
assert.equal(draftPending(100, 100), false);
assert.equal(draftPending(100, 150), false);

const drafts = [{ ts: 10, id: 'a' }, { ts: 30, id: 'c' }, { ts: 20, id: 'b' }];
assert.deepEqual(pendingDrafts(drafts, 15).map(d => d.id), ['c', 'b']);
assert.deepEqual(pendingDrafts(drafts, 0).map(d => d.id), []);
assert.deepEqual(retiredDrafts(drafts, 15, 1000, 2000).map(d => d.id), ['a']);
assert.deepEqual(retiredDrafts(drafts, 15, 100000, 2000).map(d => d.id), []);

assert.deepEqual(indicatorsFor({ unread: 2, draft: 1, overdue: 0, reconnect: true }), ['unread', 'draft', 'reconnect']);
assert.deepEqual(indicatorsFor({}), []);

const recs = [
  { id: '!wa:l', name: 'Maya (WA)', lastTs: 50, unread: 2, sourceId: 'whatsapp', drafts: [{ ts: 60 }] },
  { id: '!ig:l', name: 'Maya (IG)', lastTs: 10, unread: 0, sourceId: 'instagram', drafts: [] },
  { id: '!g:l', name: 'Group', lastTs: 40, unread: 4, sourceId: 'imessage', drafts: [] },
];
const profile = new Map([['!wa:l', { id: 'p1', displayName: 'Maya Rodriguez' }], ['!ig:l', { id: 'p1', displayName: 'Maya Rodriguez' }]]);
const items = clusterFeed(recs, profile);
assert.equal(items.length, 2);
assert.equal(items[0].kind, 'cluster');
assert.equal(items[0].displayName, 'Maya Rodriguez');
assert.deepEqual(items[0].members.map(m => m.id), ['!wa:l', '!ig:l']);
assert.equal(items[0].unread, 2);
assert.equal(items[0].lastTs, 50);
assert.equal(items[0].draft, 1, 'cluster draft count = pending drafts across members');
assert.equal(items[1].kind, 'single');
// a plain object map works too, and an unknown room stays single
assert.equal(clusterFeed(recs, { '!wa:l': { id: 'p1', displayName: 'M' } }).length, 3);

const flags = (it) => it.kind === 'single' ? indicatorsFor({ unread: it.rec.unread }) : indicatorsFor({ unread: it.unread, draft: it.draft });
const ordered = [{ kind: 'single', rec: { id: 'x', unread: 0, lastTs: 90 } }].concat(items);
assert.deepEqual(applyFilter(ordered, 'all', flags).map(i => i.kind), ['single', 'cluster', 'single']);
assert.deepEqual(applyFilter(ordered, 'needs', flags).map(i => i.kind === 'single' ? i.rec.id : 'cluster'), ['cluster', '!g:l', 'x']);
assert.deepEqual(applyFilter(ordered, 'drafts', flags).map(i => i.kind), ['cluster']);
console.log('attention.test.js: ok');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/attention.test.js`
Expected: FAIL with `Cannot find module '.../shared/model/attention.js'`

- [ ] **Step 3: Write the implementation**

```js
// shared/model/attention.js
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/attention.test.js`
Expected: `attention.test.js: ok`

---

### Task 2: Read state in the feed model and the conversation view

**Files:**
- Modify: `shared/state.js` (add `feedFilter`, `expandedClusters`, `roomProfile` fields)
- Modify: `shared/ui/account-data.js` (`fetchSnapshot`, `parseSnapshot`, `seedFeed`, `feedIngest`, `startFeedSync`)
- Modify: `shared/ui/chat.js` (`openConvo`, `startConvoWatch`, new `markRead`, `receiptsEnabled`, read caption)
- Modify: `shared/ui/render.js` (`renderMessageEvent` tags own bubbles so the caption can find them; new `applyReadCaption`)
- Test: `tests/unit/read_state.test.js`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - Feed record fields: `unread: number` (server `notification_count`), `remoteReadTs: number` (newest `m.receipt` ts from a non-self sender, 0 when none), `drafts: Array` (set by Task 5, default `[]`).
  - `account-data.js` exports `parseReadState(room, selfIds: Set<string>): { unread, remoteReadTs }` (pure).
  - `chat.js` exports `markRead(roomId, eventId)` and `receiptsEnabled()`; `render.js` exports `applyReadCaption(remoteReadTs)`.
  - `localStorage` key `beepa_send_receipts`: `'0'` disables receipt sending; anything else enables.

- [ ] **Step 1: Write the failing test**

```js
// tests/unit/read_state.test.js
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/read_state.test.js
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// account-data.js imports the whole shared/ui graph; evaluate only the pure
// function under test the way chat_watch.test.js does.
const src = fs.readFileSync(new URL('../../shared/ui/account-data.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const ctx = vm.createContext({ S: {}, SOURCES: [{ id: 'whatsapp', botMxid: '@whatsappbot:localhost' }], setTimeout });
vm.runInContext(src, ctx);
const self = new Set(['@me:localhost', '@whatsapp_123:localhost']);

const room = {
  unread_notifications: { notification_count: 3, highlight_count: 0 },
  ephemeral: { events: [{ type: 'm.receipt', content: {
    '$e1': { 'm.read': { '@me:localhost': { ts: 900 }, '@whatsapp_555:localhost': { ts: 700 } } },
    '$e2': { 'm.read': { '@whatsapp_555:localhost': { ts: 800 }, '@whatsappbot:localhost': { ts: 999 } } },
  } }] },
};
const rs = ctx.parseReadState(room, self);
assert.equal(rs.unread, 3);
assert.equal(rs.remoteReadTs, 800, 'newest receipt from a non-self, non-bot sender');
const flat = (o) => ({ unread: o.unread, remoteReadTs: o.remoteReadTs });   // cross-realm objects: compare fields
assert.deepEqual(flat(ctx.parseReadState({}, self)), { unread: 0, remoteReadTs: 0 });
assert.deepEqual(flat(ctx.parseReadState({ unread_notifications: { notification_count: 'x' } }, self)), { unread: 0, remoteReadTs: 0 });
// a receipt for a sender in selfIds never counts as the other party
assert.equal(ctx.parseReadState({ ephemeral: { events: [{ type: 'm.receipt', content: { '$a': { 'm.read': { '@me:localhost': { ts: 5 } } } } }] } }, self).remoteReadTs, 0);
console.log('read_state.test.js: ok');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/read_state.test.js`
Expected: FAIL with `ctx.parseReadState is not a function`

- [ ] **Step 3: Add state fields**

In `shared/state.js`, inside `export const S = { ... }` add after `timestampCorrections: new Map(),`:

```js
  feedFilter: 'all',                 // 'all' | 'needs' | 'drafts' (Triage Rail chips)
  expandedClusters: new Set(),       // profile ids whose sub-rows are open
  roomProfile: new Map(),            // roomId -> {id, displayName}; written by apps/user/consent.js
  convoRemoteReadTs: 0,              // other-party read ts for the OPEN room (caption only)
```

- [ ] **Step 4: Parse read state in account-data.js**

In `shared/ui/account-data.js`:

(a) In `fetchSnapshot()` change the `room` filter to include receipts:
```js
    room: { timeline: { limit: 6, not_types: [CORRECTION_TYPE] }, state: { lazy_load_members: true }, account_data: { types: ['m.tag'] }, ephemeral: { types: ['m.receipt'] } },
```
(b) In `startFeedSync()` change the filter to:
```js
        room: { timeline: { limit: 6 }, state: { types: [] }, ephemeral: { types: ['m.receipt'] } },
```
(c) Add this pure function after `feedLastPreview`:
```js
// Read state from one /sync room section. `unread` is the SERVER's
// notification_count (never a local count); `remoteReadTs` is the newest
// m.receipt timestamp from a sender that is NOT us, NOT one of our own bridge
// ghosts and NOT a bridge bot — i.e. the other party actually read. Missing or
// malformed => 0 (no caption, no badge). Pure; no DOM.
function parseReadState(room, selfIds) {
  const out = { unread: 0, remoteReadTs: 0 };
  if (!room || typeof room !== 'object') return out;
  const n = room.unread_notifications && room.unread_notifications.notification_count;
  if (typeof n === 'number' && isFinite(n) && n > 0) out.unread = n;
  const bots = new Set(SOURCES.map(s => s.botMxid).filter(Boolean));
  const evs = (room.ephemeral && Array.isArray(room.ephemeral.events)) ? room.ephemeral.events : [];
  for (const e of evs) {
    if (!e || e.type !== 'm.receipt' || !e.content || typeof e.content !== 'object') continue;
    for (const perEvent of Object.values(e.content)) {
      const reads = perEvent && perEvent['m.read'];
      if (!reads || typeof reads !== 'object') continue;
      for (const [user, info] of Object.entries(reads)) {
        if (selfIds.has(user) || bots.has(user)) continue;
        const ts = info && info.ts;
        if (typeof ts === 'number' && isFinite(ts) && ts > out.remoteReadTs) out.remoteReadTs = ts;
      }
    }
  }
  return out;
}
function selfIdSet() {
  const s = new Set(S.selfMxids || []);
  if (S.userId) s.add(S.userId);
  return s;
}
```
(d) In `seedFeed()`, where a record is created or refreshed, carry read state. Replace the `if (existing) {...} else {...}` block with:
```js
      const rs = parseReadState(join[c.id], selfIdSet());
      const existing = feedModel.get(c.id);
      if (existing) {
        existing.name = c.title;                    // refresh name; keep original attribution
        if (p && (p.ts > existing.lastTs || corrections?.size)) { existing.lastBody = p.body; existing.lastTs = p.ts; }
        existing.unread = rs.unread;
        if (rs.remoteReadTs > (existing.remoteReadTs || 0)) existing.remoteReadTs = rs.remoteReadTs;
        if (!Array.isArray(existing.drafts)) existing.drafts = [];
      } else {
        feedModel.set(c.id, {
          id: c.id, name: c.title,                  // c.title already sanitizeLine'd by buildConvos
          lastBody: p ? p.body : '', lastTs: p ? p.ts : 0, sourceId: s.id,
          unread: rs.unread, remoteReadTs: rs.remoteReadTs, drafts: [],
        });
      }
```
Note `refreshSelfMxids(join)` runs AFTER this loop today; move the `await refreshSelfMxids(join);` line to run BEFORE the `const seen = new Set();` line so `selfIdSet()` is current.

(e) In `feedIngest(data)`, inside the `for (const rid of Object.keys(join))` loop, after the `if (!feedModel.has(rid)) {...}` line add:
```js
    const rec0 = feedModel.get(rid);
    const rs = parseReadState(join[rid], selfIdSet());
    if (join[rid] && join[rid].unread_notifications && rs.unread !== rec0.unread) { rec0.unread = rs.unread; changed = true; }
    if (rs.remoteReadTs > (rec0.remoteReadTs || 0)) {
      rec0.remoteReadTs = rs.remoteReadTs; changed = true;
      if (S.openRoomId === rid) { S.convoRemoteReadTs = rs.remoteReadTs; applyReadCaption(rs.remoteReadTs); }
    }
```
and add `applyReadCaption` to the import from `./render.js`.

(f) Add `parseReadState` to the export list.

- [ ] **Step 5: Caption + read markers in render.js and chat.js**

In `shared/ui/render.js`, in `renderMessageEvent`, right after `bubble.dataset.messageTs = String(ts);` add:
```js
  if (sent) bubble.dataset.own = '1';
```
Add this function before the export and export it:
```js
// "Read" caption under the newest OWN bubble whose time is at or before the
// other party's newest receipt. Caption only; removed from every other bubble
// so at most one shows. No receipt (0) => no caption anywhere.
function applyReadCaption(remoteReadTs) {
  const box = $('convo-messages');
  if (!box) return;
  let target = null;
  for (const b of box.children) {
    const old = b.querySelector('.read-caption');
    if (old) old.remove();
    if (b.dataset && b.dataset.own === '1') {
      const ts = Number(b.dataset.messageTs);
      if (isFinite(ts) && remoteReadTs > 0 && ts <= remoteReadTs) target = b;
    }
  }
  if (target) target.appendChild(el('div', 'read-caption', 'Read'));
}
```
In `shared/ui/chat.js`:
(a) Import `applyReadCaption` from `./render.js`.
(b) Add before `openConvo`:
```js
// Read receipts are a per-viewer convenience (not authorization): '0' turns off
// marking conversations read on the phone when opened here. Default on.
function receiptsEnabled() {
  try { return localStorage.getItem('beepa_send_receipts') !== '0'; } catch (e) { return true; }
}
// Advance BOTH the fully-read marker and the public read receipt to eventId.
// Only for a validated, OPEN room; failure is logged and nothing else happens.
async function markRead(roomId, eventId) {
  if (!receiptsEnabled()) return;
  if (typeof eventId !== 'string' || !eventId.startsWith('$')) return;
  if (!ROOMID_RE.test(roomId) || !S.joinedSet.has(roomId) || S.openRoomId !== roomId) return;
  try {
    await api('POST', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/read_markers',
      { 'm.fully_read': eventId, 'm.read': eventId });
    const rec = feedModel.get(roomId);
    if (rec && rec.unread) { rec.unread = 0; scheduleFeedRender(); }
  } catch (e) { /* receipt failure never affects anything else */ }
}
function newestRenderedEventId() {
  const box = $('convo-messages');
  if (!box) return null;
  for (let i = box.children.length - 1; i >= 0; i--) {
    const id = box.children[i].dataset && box.children[i].dataset.eventId;
    if (id) return id;
  }
  return null;
}
```
`scheduleFeedRender` must be imported from `./search.js` (search.js already imports rows.js which imports chat.js; this is the existing cyclic cluster, fine for named function imports used at call time).
(c) In `openConvo`, after `convoSetStatus('');` add `S.convoRemoteReadTs = (feedModel.get(roomId) || {}).remoteReadTs || 0;`. After the history render block (`for (const ev of chunk) renderMessageEvent(ev); if (box) box.scrollTop = ...`) add inside the same `if (S.openRoomId === roomId)`:
```js
      applyReadCaption(S.convoRemoteReadTs);
      markRead(roomId, newestRenderedEventId());
```
(d) In `startConvoWatch`, change the filter to include `ephemeral: { types: ['m.receipt'] }`, and after the `if (toCache.length) cacheAppend(...)` line add:
```js
        applyReadCaption(S.convoRemoteReadTs);
        if (document.visibilityState !== 'hidden') markRead(watchRoom, newestRenderedEventId());
```
Also read receipts from the watch payload: before `const room = join[watchRoom];` nothing; after the `if (room && room.timeline ...) { ... }` block add:
```js
      if (room && room.ephemeral) {
        const rs = parseReadState(room, new Set([S.userId, ...(S.selfMxids || [])]));
        if (rs.remoteReadTs > S.convoRemoteReadTs) { S.convoRemoteReadTs = rs.remoteReadTs; applyReadCaption(rs.remoteReadTs); }
      }
```
with `parseReadState` imported from `./account-data.js`.
(e) Export `markRead, receiptsEnabled` from chat.js.
(f) Settings toggle: in `apps/user/index.html` inside `#settings-section-advanced` after the `<p class="settings-lead">` add:
```html
                <label class="settings-check"><input id="setting-receipts" type="checkbox" checked> Mark conversations read on your phone when you open them here</label>
```
and in `apps/user/main.js` `DOMContentLoaded` handler add:
```js
  const rc = $('setting-receipts');
  if (rc) {
    try { rc.checked = localStorage.getItem('beepa_send_receipts') !== '0'; } catch (e) {}
    rc.addEventListener('change', () => { try { localStorage.setItem('beepa_send_receipts', rc.checked ? '1' : '0'); } catch (e) {} });
  }
```
(g) CSS in `shared/style/beepa.css` under the bubbles section:
```css
.msg .read-caption { display: block; font-size: 10.5px; color: var(--color-accent-700); text-align: right; margin-top: 1px; }
.settings-check { display: flex; gap: 8px; align-items: center; font-size: 13px; }
.settings-check input { width: auto; }
```

- [ ] **Step 6: Run tests**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/read_state.test.js` → `ok`.
Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/chat_watch.test.js` → passes (its vm context needs the new free names: add `applyReadCaption() {}, parseReadState: () => ({unread:0, remoteReadTs:0}), scheduleFeedRender() {}, document: { visibilityState: 'visible' }` to the `createContext` object in that test).
Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine sh -c 'for f in shared/ui/*.js shared/model/*.js apps/user/*.js; do node --check "$f" || exit 1; done'` → no output.

---

### Task 3: Row anatomy and filter chips (teammate list)

**Files:**
- Modify: `shared/ui/rows.js` (`buildFeedRow`, new `buildAvatar`, `buildStripe`, `buildClusterRow`, `buildSubRow`)
- Modify: `shared/ui/search.js` (`renderHome`, new `ensureHomeFilters`, replaces `ensureHomeHiddenToggle` chip placement)
- Modify: `shared/style/beepa.css` (row anatomy, stripe, pills, chips)
- Modify: `apps/user/style.css` (chips row layout)
- Test: `tests/unit/rows_anatomy.test.js`

**Interfaces:**
- Consumes: Task 1 (`initials`, `indicatorsFor`, `pendingDrafts`, `clusterFeed`, `applyFilter`), Task 2 record fields.
- Produces: `rows.js` exports `rowFlags(rec)` (pure: `indicatorsFor({unread: rec.unread, draft: pendingDrafts(rec.drafts, rec.lastTs).length, reconnect: runtime[rec.sourceId]?.needsReconnect})`), `buildAvatar(name, sourceIds: string[])`, `buildStripe(flags)`, `buildClusterRow(item)`, `buildSubRow(rec)`. DOM shape of a row:

```
.convo[data-room-id]
  .stripe > i.seg.unread | i.seg.draft | i.seg.overdue | i.seg.reconnect   (only active ones)
  .avatar  "MR"  > .plat-badge.<source> (one per source, stacked)
  .meta > .title (+ .tag.cluster-caret for clusters) / .preview (prefix "Draft: " when a draft is pending)
  .side > .when / .pill.unread N / .pill.draft N
  [decorator output]
```

- [ ] **Step 1: Write the failing test**

```js
// tests/unit/rows_anatomy.test.js
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/rows_anatomy.test.js
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { initials, indicatorsFor, pendingDrafts, clusterFeed, applyFilter } from '../../shared/model/attention.js';

// Minimal DOM double: el() from el.js is replaced by a recorder.
function node(tag, cls) {
  const n = { tag, className: cls || '', children: [], dataset: {}, textContent: '', attrs: {},
    appendChild(c) { this.children.push(c); return c; }, setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {}, classList: { toggle() {}, add() {} } };
  n.tabIndex = 0;
  return n;
}
const src = fs.readFileSync(new URL('../../shared/ui/rows.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const ctx = vm.createContext({
  el: (tag, cls, text) => { const n = node(tag, cls); if (text !== undefined) n.textContent = text; return n; },
  $: () => null, sanitizeLine: s => s, feedRelTime: () => '2m', openConvo() {},
  SOURCES: [{ id: 'whatsapp', icon: 'W' }, { id: 'instagram', icon: 'I' }],
  feedModel: new Map(), runtime: { whatsapp: { needsReconnect: false } }, S: { expandedClusters: new Set() },
  initials, indicatorsFor, pendingDrafts, clusterFeed, applyFilter,
});
vm.runInContext(src, ctx);

const rec = { id: '!a:l', name: 'Maya Rodriguez', lastBody: 'hi', lastTs: 50, unread: 2, sourceId: 'whatsapp', drafts: [{ ts: 60, body: 'Totally, Thursday works' }] };
assert.deepEqual(ctx.rowFlags(rec), ['unread', 'draft']);
const row = ctx.buildFeedRow(rec);
const stripe = row.children.find(c => c.className === 'stripe');
assert.deepEqual(stripe.children.map(c => c.className), ['seg unread', 'seg draft']);
const avatar = row.children.find(c => c.className.startsWith('avatar'));
assert.equal(avatar.textContent, 'MR');
assert.equal(avatar.children.filter(c => c.className.includes('plat-badge')).length, 1);
const meta = row.children.find(c => c.className === 'meta');
assert.equal(meta.children[1].textContent, 'Draft: Totally, Thursday works');
const side = row.children.find(c => c.className === 'side');
assert.ok(side.children.some(c => c.className === 'pill unread' && c.textContent === '2'));
assert.ok(side.children.some(c => c.className === 'pill draft' && c.textContent === '1'));

// retired draft (activity after it) => no draft flag, normal preview
const quiet = Object.assign({}, rec, { lastTs: 70 });
assert.deepEqual(ctx.rowFlags(quiet), ['unread']);
assert.equal(ctx.buildFeedRow(quiet).children.find(c => c.className === 'meta').children[1].textContent, 'hi');

// cluster row: two badges, caret, summed unread
const item = clusterFeed([rec, { id: '!b:l', name: 'Maya (IG)', lastTs: 10, unread: 1, sourceId: 'instagram', drafts: [] }],
  new Map([['!a:l', { id: 'p', displayName: 'Maya Rodriguez' }], ['!b:l', { id: 'p', displayName: 'Maya Rodriguez' }]]))[0];
const crow = ctx.buildClusterRow(item);
assert.equal(crow.dataset.roomId, '!a:l', 'cluster row opens the newest member');
assert.equal(crow.children.find(c => c.className.startsWith('avatar')).children.length, 2);
assert.ok(crow.children.find(c => c.className === 'meta').children[0].children.some(c => c.className === 'cluster-caret'));
assert.ok(crow.children.find(c => c.className === 'side').children.some(c => c.className === 'pill unread' && c.textContent === '3'));
console.log('rows_anatomy.test.js: ok');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/rows_anatomy.test.js`
Expected: FAIL with `ctx.rowFlags is not a function`

- [ ] **Step 3: Implement rows.js**

Add import at top of `shared/ui/rows.js`:
```js
import { initials, indicatorsFor, pendingDrafts } from '../model/attention.js';
import { S } from '../state.js';   // merge into the existing state import line
```
Add these functions before `buildFeedRow` and rewrite `buildFeedRow`:
```js
// Indicator flags for one feed record. Pure over the record + runtime.
function rowFlags(rec) {
  return indicatorsFor({
    unread: rec.unread,
    draft: pendingDrafts(rec.drafts, rec.lastTs).length,
    reconnect: !!(rec.sourceId && runtime[rec.sourceId] && runtime[rec.sourceId].needsReconnect),
  });
}
// 4px left edge split into one segment per active indicator (CSS colors by class).
function buildStripe(flags) {
  const s = el('div', 'stripe');
  for (const f of flags) s.appendChild(el('i', 'seg ' + f));
  return s;
}
// Two-letter initials with the platform logo(s) badged at the lower right.
// sourceIds come ONLY from feed records (the room's space), never from content.
function buildAvatar(name, sourceIds) {
  const a = el('div', 'avatar', initials(name));
  for (const sid of (sourceIds || []).filter(Boolean)) {
    const b = buildPlatBadge(sid);
    b.classList.add('avatar-plat');
    a.appendChild(b);
  }
  return a;
}
function buildSide(rec, flags, unreadCount, draftCount) {
  const side = el('div', 'side');
  if (rec.lastTs) side.appendChild(el('span', 'when', feedRelTime(rec.lastTs)));
  if (flags.includes('unread')) side.appendChild(el('span', 'pill unread', String(unreadCount)));
  if (flags.includes('draft')) side.appendChild(el('span', 'pill draft', String(draftCount)));
  if (flags.includes('reconnect')) {
    const flag = el('span', 'pill reconnect', '!');
    flag.title = 'This account’s bridge login needs reconnecting — reopen its card in Settings to re-pair.';
    side.appendChild(flag);
  }
  return side;
}
function wireOpen(row, roomId) {
  row.dataset.roomId = roomId;                        // active-row match key (layout only)
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  const open = () => openConvo(roomId);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
}

// A feed row (Triage Rail anatomy). click → openConvo (the only validated nav path).
function buildFeedRow(r) {
  const name = sanitizeLine(r.name || r.id);
  const flags = rowFlags(r);
  const pend = pendingDrafts(r.drafts, r.lastTs);
  const row = el('div', 'convo');
  row.appendChild(buildStripe(flags));
  row.appendChild(buildAvatar(name, [r.sourceId]));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', name));
  const preview = el('div', 'preview');
  if (pend.length) {
    preview.appendChild(el('span', 'draft-prefix', 'Draft: '));
    preview.appendChild(document.createTextNode(sanitizeLine(pend[0].body || '')));
    preview.textContent = 'Draft: ' + sanitizeLine(pend[0].body || '');   // textContent only (HF-4)
  } else {
    preview.textContent = sanitizeLine(r.lastBody || '');
  }
  meta.appendChild(preview);
  row.appendChild(meta);
  row.appendChild(buildSide(r, flags, r.unread || 0, pend.length));
  wireOpen(row, r.id);
  if (convoRowDecorator) convoRowDecorator(row, { id: r.id, sourceId: r.sourceId });
  return row;
}

// One row per person (contact profile) with every platform badged on the
// avatar and a caret; opening the row opens the NEWEST member. Expanded
// clusters (S.expandedClusters) render a sub-row per member beneath.
function buildClusterRow(item) {
  const newest = item.members[0];
  const flags = indicatorsFor({ unread: item.unread, draft: item.draft,
    reconnect: item.members.some(m => m.sourceId && runtime[m.sourceId] && runtime[m.sourceId].needsReconnect) });
  const row = el('div', 'convo cluster');
  row.appendChild(buildStripe(flags));
  const sources = [...new Set(item.members.map(m => m.sourceId).filter(Boolean))];
  row.appendChild(buildAvatar(item.displayName || newest.name, sources));
  const meta = el('div', 'meta');
  const title = el('div', 'title', sanitizeLine(item.displayName || newest.name || ''));
  const open = S.expandedClusters.has(item.profileId);
  const caret = el('button', 'cluster-caret', (open ? '▾ ' : '▸ ') + item.members.length);
  caret.type = 'button';
  caret.title = open ? 'Collapse platforms' : 'Show platforms';
  caret.addEventListener('click', (e) => {
    e.stopPropagation();
    if (open) S.expandedClusters.delete(item.profileId); else S.expandedClusters.add(item.profileId);
    scheduleFeedRender();
  });
  title.appendChild(caret);
  meta.appendChild(title);
  const pend = pendingDrafts(newest.drafts, newest.lastTs);
  meta.appendChild(el('div', 'preview', pend.length ? 'Draft: ' + sanitizeLine(pend[0].body || '') : sanitizeLine(newest.lastBody || '')));
  row.appendChild(meta);
  row.appendChild(buildSide(newest, flags, item.unread, item.draft));
  wireOpen(row, newest.id);
  if (convoRowDecorator) convoRowDecorator(row, { id: newest.id, sourceId: newest.sourceId });
  return row;
}
// Member sub-row under an expanded cluster: platform + its own state.
function buildSubRow(rec) {
  const flags = rowFlags(rec);
  const pend = pendingDrafts(rec.drafts, rec.lastTs);
  const row = el('div', 'convo sub');
  row.appendChild(buildStripe(flags));
  const b = buildPlatBadge(rec.sourceId); b.classList.add('sub-plat');
  row.appendChild(b);
  const meta = el('div', 'meta');
  const source = SOURCES.find(s => s.id === rec.sourceId);
  meta.appendChild(el('div', 'title', sanitizeLine((source && source.label) || rec.sourceId || '')));
  meta.appendChild(el('div', 'preview', pend.length ? 'Draft: ' + sanitizeLine(pend[0].body || '') : sanitizeLine(rec.lastBody || '')));
  row.appendChild(meta);
  row.appendChild(buildSide(rec, flags, rec.unread || 0, pend.length));
  wireOpen(row, rec.id);
  return row;
}
```
Remove the three duplicated `preview.appendChild(...)` lines in `buildFeedRow` so only the `preview.textContent = ...` assignment remains in the draft branch. `scheduleFeedRender` is imported from `./search.js` (cyclic cluster, call-time use). Update the export line:
```js
export { buildPlatBadge, buildFeedRow, buildClusterRow, buildSubRow, buildAvatar, buildStripe, rowFlags, setActiveConvoRow, buildConvoRow, elEmpty, setConvoRowDecorator };
```

- [ ] **Step 4: Filter chips and clustered render in search.js**

Import at top of `shared/ui/search.js`:
```js
import { buildConvoRow, buildFeedRow, buildClusterRow, buildSubRow, rowFlags, elEmpty } from './rows.js';
import { clusterFeed, applyFilter, indicatorsFor } from '../model/attention.js';
```
Replace `renderHome()` with:
```js
function itemFlags(it) {
  return it.kind === 'single' ? rowFlags(it.rec) : indicatorsFor({ unread: it.unread, draft: it.draft });
}
function renderHome() {
  const list = $('list-body');
  if (!list) return;
  const k = S.activeNavKey;
  if (k && (k.indexOf('source:') === 0 || k === 'people' || k === 'settings')) return;
  ensureHomeFilters();
  const q = (($('home-search') && $('home-search').value) || '').trim().toLowerCase();
  const all = [...feedModel.values()].sort((a, b) => b.lastTs - a.lastTs);
  const visible = S.feedShowHidden ? all : all.filter(r => !feedIsHidden(r.id));
  const matched = q
    ? visible.filter(r => sanitizeLine(r.name).toLowerCase().includes(q) ||
                          sanitizeLine(r.lastBody || '').toLowerCase().includes(q))
    : visible;
  const items = applyFilter(clusterFeed(matched, S.roomProfile), S.feedFilter, itemFlags).slice(0, 200);
  list.replaceChildren();
  if (!items.length) {
    list.appendChild(elEmpty(q ? 'No conversations match your search.'
      : (S.feedFilter === 'drafts' ? 'No suggestions waiting.' : 'No conversations yet.')));
    return;
  }
  for (const it of items) {
    if (it.kind === 'single') { list.appendChild(buildFeedRow(it.rec)); continue; }
    list.appendChild(buildClusterRow(it));
    if (S.expandedClusters.has(it.profileId)) for (const m of it.members) list.appendChild(buildSubRow(m));
  }
  if (feedRenderHook) feedRenderHook();
}

// Chips above the Home list: Needs you / All / Drafts + Show hidden. Built once
// with el()/textContent; pure client-side state (S.feedFilter, S.feedShowHidden).
const FILTERS = [['needs', 'Needs you'], ['all', 'All'], ['drafts', 'Drafts']];
function ensureHomeFilters() {
  const list = $('list-body');
  if (!list || !list.parentNode) return;
  let bar = $('home-filters');
  if (!bar) {
    bar = el('div', 'home-filters');
    bar.id = 'home-filters';
    for (const [key, label] of FILTERS) {
      const chip = el('button', 'chip');
      chip.type = 'button'; chip.dataset.filter = key; chip.textContent = label;
      chip.addEventListener('click', () => { S.feedFilter = key; renderHome(); });
      bar.appendChild(chip);
    }
    const hidden = el('button', 'chip chip-hidden');
    hidden.type = 'button'; hidden.id = 'home-hidden-toggle';
    hidden.addEventListener('click', () => { S.feedShowHidden = !S.feedShowHidden; renderHome(); });
    bar.appendChild(hidden);
    list.parentNode.insertBefore(bar, list);
  }
  let needs = 0, drafts = 0;
  for (const r of feedModel.values()) { const f = rowFlags(r); if (f.length) needs++; if (f.includes('draft')) drafts++; }
  for (const chip of bar.querySelectorAll('.chip[data-filter]')) {
    const key = chip.dataset.filter;
    chip.classList.toggle('on', S.feedFilter === key);
    chip.setAttribute('aria-pressed', S.feedFilter === key ? 'true' : 'false');
    chip.textContent = key === 'needs' ? 'Needs you · ' + needs : key === 'drafts' ? 'Drafts · ' + drafts : 'All';
  }
  const hidden = $('home-hidden-toggle');
  if (hidden) { hidden.textContent = S.feedShowHidden ? 'Hide hidden' : 'Show hidden'; hidden.classList.toggle('on', S.feedShowHidden); }
}
```
Delete the old `ensureHomeHiddenToggle` function and replace its name in the export list with `ensureHomeFilters`. In `renderSourceList`, keep `buildConvoRow` as is.

- [ ] **Step 5: CSS**

Append to `shared/style/beepa.css`:
```css
/* ── Triage Rail rows ── */
.convo { position: relative; gap: 10px; padding: 9px 12px 9px 8px; }
.convo .stripe { width: 4px; height: 40px; flex: 0 0 4px; border-radius: 2px; display: grid; grid-auto-rows: 1fr; gap: 2px; overflow: hidden; }
.convo .stripe .seg { display: block; border-radius: 2px; }
.seg.unread, .pill.unread { background: #2f6fed; }
.seg.draft, .pill.draft { background: #7c5cd6; }
.seg.overdue, .pill.overdue { background: #c9810a; }
.seg.reconnect, .pill.reconnect { background: #d64545; }
.convo .avatar { width: 40px; height: 40px; flex: 0 0 40px; font-size: 14px; position: relative; overflow: visible; }
.avatar .avatar-plat { position: absolute; right: -3px; bottom: -3px; width: 16px; height: 16px; border: 2px solid var(--color-surface); box-sizing: content-box; }
.avatar .avatar-plat + .avatar-plat { right: 9px; }
.convo .title { font-size: 15px; display: flex; align-items: center; gap: 6px; }
.convo .preview { font-size: 12.5px; }
.convo .side { display: grid; justify-items: end; gap: 4px; flex: 0 0 auto; }
.convo .side .when { margin-left: 0; font-size: 11px; }
.pill { height: 18px; min-width: 18px; padding: 0 6px; border-radius: 9px; display: inline-grid; place-items: center; font: 600 10.5px var(--font-body); color: #fff; }
.convo .draft-prefix, .convo .preview.draft { color: #7c5cd6; font-weight: 500; }
.cluster-caret { border: 0; background: transparent; font: 11px var(--font-body); color: var(--color-muted); padding: 0 4px; cursor: pointer; }
.cluster-caret:hover { color: var(--color-text); }
.convo.sub { padding-left: 22px; background: color-mix(in srgb, var(--color-bg) 50%, var(--color-surface)); }
.convo.sub .sub-plat { width: 18px; height: 18px; }
.convo.sub .title { font-size: 13px; font-weight: 500; }
.home-filters { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 12px 8px; background: var(--color-surface); }
.chip { height: 24px; padding: 0 10px; border-radius: 12px; border: 1px solid var(--color-divider); background: var(--color-surface); color: var(--color-muted); font: 12px var(--font-body); cursor: pointer; }
.chip.on { background: var(--color-text); color: var(--color-surface); border-color: var(--color-text); }
.chip-hidden { margin-left: auto; }
```
Remove the old `.feed-showhidden` rules from `beepa.css` (three rules) since the toggle is now a chip.

- [ ] **Step 6: Run tests**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/rows_anatomy.test.js` → `ok`.
Run the syntax check from Task 2 step 6. Open `http://127.0.0.1:8011/apps/user/index.html` and confirm rows show initials, badge, stripe, chips. Report what was seen.

---

### Task 4: Clustering into the conversation pane (tabs, header chip)

**Files:**
- Modify: `apps/user/consent.js` (`loadConsentState` writes `S.roomProfile`; new `headerChip` via `setConvoHeaderHook`)
- Modify: `shared/ui/chat.js` (`openConvo` renders `#convo-tabs`, title from profile; `setConvoHeaderHook`)
- Modify: `apps/user/index.html` (`#convo-tabs`, `#convo-head-extra`, `#convo-sub`)
- Modify: `apps/user/style.css`, `shared/style/beepa.css`
- Test: `tests/unit/convo_tabs.test.js`

**Interfaces:**
- Consumes: `S.roomProfile` (Task 2 field), `clusterFeed`.
- Produces: `chat.js` exports `siblingRooms(roomId): FeedRecord[]` (pure over `S.roomProfile` + `feedModel`: all records sharing the room's profile id, newest first, including the room itself; `[roomId's record]` when unlinked) and `setConvoHeaderHook(fn(roomId, headExtraEl))`.

- [ ] **Step 1: Write the failing test**

```js
// tests/unit/convo_tabs.test.js
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/convo_tabs.test.js
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../../shared/ui/chat.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const feedModel = new Map([
  ['!wa:l', { id: '!wa:l', name: 'Maya (WA)', lastTs: 50, sourceId: 'whatsapp', unread: 2 }],
  ['!ig:l', { id: '!ig:l', name: 'Maya (IG)', lastTs: 10, sourceId: 'instagram', unread: 0 }],
  ['!g:l', { id: '!g:l', name: 'Group', lastTs: 40, sourceId: 'imessage', unread: 0 }],
]);
const S = { token: 't', joinedSet: new Set(feedModel.keys()), roomProfile: new Map([
  ['!wa:l', { id: 'p', displayName: 'Maya Rodriguez' }], ['!ig:l', { id: 'p', displayName: 'Maya Rodriguez' }]]) };
const ctx = vm.createContext({ S, ROOMID_RE: /^!/, convoSeen: new Set(), feedModel, runtime: {}, setTimeout,
  $: () => null, el: () => ({ appendChild() {}, classList: { add() {}, toggle() {} }, dataset: {} }), sanitizeLine: s => s,
  messageTimestamp: () => 0, timestampCorrections: () => new Map(), IMSG_BOT_MXID: '@b:l',
  setActiveNav() {}, showSection() {}, setDetailMode() {}, setActiveConvoRow() {}, renderMessageEvent() {},
  convoResolveContent: () => true, applyReadCaption() {}, parseReadState: () => ({ unread: 0, remoteReadTs: 0 }),
  scheduleFeedRender() {}, buildPlatBadge: () => ({ className: '', textContent: '', classList: { add() {} } }),
  SOURCES: [], document: { visibilityState: 'visible' }, api: () => new Promise(() => {}) });
vm.runInContext(source, ctx);
assert.deepEqual(ctx.siblingRooms('!ig:l').map(r => r.id), ['!wa:l', '!ig:l'], 'newest first, includes self');
assert.deepEqual(ctx.siblingRooms('!g:l').map(r => r.id), ['!g:l'], 'unlinked room is its own only sibling');
assert.deepEqual(ctx.siblingRooms('!nope:l'), []);
console.log('convo_tabs.test.js: ok');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/convo_tabs.test.js`
Expected: FAIL with `ctx.siblingRooms is not a function`

- [ ] **Step 3: HTML**

In `apps/user/index.html` replace the `<header id="convo-head">…</header>` block with:
```html
            <header id="convo-head">
              <button id="convo-back" type="button" title="Back to list">&#8592;</button>
              <span id="convo-badge" class="plat-badge"></span>
              <div id="convo-who">
                <span id="convo-title"></span>
                <span id="convo-sub" class="muted"></span>
              </div>
              <span class="spacer"></span>
              <span id="convo-head-extra"></span>
              <button id="convo-add-contact" type="button" title="Add to contact">Add to contact</button>
            </header>
            <div id="convo-tabs" class="hidden" role="tablist"></div>
```

- [ ] **Step 4: chat.js tabs, title, header hook**

In `shared/ui/chat.js` add:
```js
import { SOURCES } from '../model/source_catalog.js';
let convoHeaderHook = null;
function setConvoHeaderHook(fn) { convoHeaderHook = typeof fn === 'function' ? fn : null; }

// Every feed record sharing this room's contact profile (newest first), or
// just the room itself when it is unlinked. Rooms merge ONLY via S.roomProfile
// (written from the stored contact profiles), never by name.
function siblingRooms(roomId) {
  const rec = feedModel.get(roomId);
  if (!rec) return [];
  const p = S.roomProfile && (typeof S.roomProfile.get === 'function' ? S.roomProfile.get(roomId) : S.roomProfile[roomId]);
  if (!p || !p.id) return [rec];
  const out = [];
  for (const r of feedModel.values()) {
    const q = typeof S.roomProfile.get === 'function' ? S.roomProfile.get(r.id) : S.roomProfile[r.id];
    if (q && q.id === p.id) out.push(r);
  }
  return out.sort((a, b) => (b.lastTs || 0) - (a.lastTs || 0));
}

// Platform tabs under the header for a clustered person; hidden for one room.
function renderConvoTabs(roomId) {
  const host = $('convo-tabs');
  if (!host) return;
  const sibs = siblingRooms(roomId);
  host.replaceChildren();
  host.classList.toggle('hidden', sibs.length < 2);
  if (sibs.length < 2) return;
  for (const r of sibs) {
    const tab = el('button', 'convo-tab' + (r.id === roomId ? ' on' : ''));
    tab.type = 'button'; tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', r.id === roomId ? 'true' : 'false');
    tab.appendChild(buildPlatBadge(r.sourceId));
    const source = SOURCES.find(s => s.id === r.sourceId);
    tab.appendChild(el('span', 'convo-tab-label', (source && source.label) || r.sourceId || ''));
    if (r.unread) tab.appendChild(el('span', 'pill unread', String(r.unread)));
    if (r.id !== roomId) tab.appendChild(el('span', 'convo-tab-when', feedRelTime(r.lastTs)));
    tab.addEventListener('click', () => { if (r.id !== roomId) openConvo(r.id); });
    host.appendChild(tab);
  }
}
```
(`feedRelTime` import from `../model/message_preview.js`.) In `openConvo`, replace the title/badge block:
```js
  const rec = feedModel.get(roomId);
  const p = S.roomProfile && (typeof S.roomProfile.get === 'function' ? S.roomProfile.get(roomId) : S.roomProfile[roomId]);
  const titleEl = $('convo-title');
  if (titleEl) titleEl.textContent = sanitizeLine((p && p.displayName) || (rec && rec.name) || roomId);
  const sub = $('convo-sub');
  if (sub) sub.textContent = '';
  const badge = $('convo-badge');
  if (badge) {
    const b = buildPlatBadge(rec && rec.sourceId);
    badge.className = b.className;
    badge.textContent = b.textContent;
  }
  renderConvoTabs(roomId);
  const extra = $('convo-head-extra');
  if (extra) { extra.replaceChildren(); if (convoHeaderHook) { try { convoHeaderHook(roomId, extra, sub); } catch (e) { /* app hook must not break open */ } } }
  const input = $('convo-input');
  if (input) input.placeholder = 'Reply on ' + (((SOURCES.find(s => s.id === (rec && rec.sourceId)) || {}).label) || 'this platform') + '…';
```
Export `siblingRooms, setConvoHeaderHook`.

- [ ] **Step 5: consent.js writes S.roomProfile and the header chip**

In `apps/user/consent.js` `loadConsentState()`, change the profiles line to:
```js
  try {
    profileMap = roomProfileMap(await readProfiles());
    S.roomProfile = new Map(Object.entries(profileMap).map(([rid, p]) => [rid, { id: p.id, displayName: p.displayName }]));
  } catch (e) { /* keep previous cache */ }
```
(import `S` from `../../shared/state.js` if not already.) Add, and register inside `initConsentUI()` alongside `setConvoRowDecorator(decorateRow)`:
```js
import { setConvoHeaderHook } from '../../shared/ui/chat.js';
// Read-only header chip: the OPEN room's level from the shared resolver, plus
// "N of M shared" across the person's platforms. Never a control; the share
// control stays in the row kebab (decorateRow) with its write discipline.
function headerChip(roomId, host, sub) {
  // effectiveLevel() (shared/model/consent.js, already imported here) is the
  // explicit per-conversation level: 'share' | 'direct' | 'private' (absent or
  // unrecognized => 'private'). resolve() returns {shared, reason} and carries
  // NO level field — do not read `.level` off effectiveFor().
  const level = effectiveLevel(overrides.get(roomId));
  const chip = el('span', 'share-badge' + (level === 'private' ? '' : ' shared'),
    level === 'direct' ? 'Direct' : level === 'share' ? 'Shared' : 'Private');
  host.appendChild(chip);
  const sibs = siblingRooms(roomId);
  if (sub && sibs.length > 1) {
    const shared = sibs.filter(r => effectiveLevel(overrides.get(r.id)) !== 'private').length;
    sub.textContent = shared + ' of ' + sibs.length + ' conversations shared';
  }
}
```
(`siblingRooms` imported from chat.js; `effectiveLevel` and the module-local `overrides` Map are already in scope in consent.js.) Call `setConvoHeaderHook(headerChip);` in `initConsentUI`. Acceptance: open a `direct` room → chip reads "Direct"; a person with one `share` and one `private` room → sub-line reads "1 of 2 conversations shared".

- [ ] **Step 6: CSS**

`apps/user/style.css`:
```css
#convo-who { display: flex; flex-direction: column; min-width: 0; }
#convo-sub { font-size: 11.5px; }
#convo-tabs { display: flex; gap: 2px; padding: 0 12px; flex: 0 0 auto; }
#convo-tabs.hidden { display: none !important; }
```
`shared/style/beepa.css`:
```css
#convo-tabs { background: var(--color-surface); border-bottom: 1px solid var(--color-divider); }
.convo-tab { border: 0; border-bottom: 2px solid transparent; background: transparent; padding: 7px 10px; display: inline-flex; gap: 6px; align-items: center; font: 500 12.5px var(--font-body); color: var(--color-muted); cursor: pointer; }
.convo-tab.on { color: var(--color-text); border-color: var(--color-text); }
.convo-tab .plat-badge { width: 14px; height: 14px; }
.convo-tab-when { font-size: 10.5px; color: var(--color-muted); }
#convo-head .share-badge { margin-right: 8px; }
```

- [ ] **Step 7: Run tests**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/convo_tabs.test.js` → `ok`. Re-run `chat_watch.test.js` (add `buildPlatBadge`, `SOURCES`, `feedRelTime: () => ''` to its context). Syntax check. In the browser, link two rooms to one contact in People and confirm one row + tabs.

---

### Task 5: Ghost drafts replace the Proposals tab

**Files:**
- Modify: `apps/user/proposals.js` (keep parse/partition/pendingForRoom/rowGesture/discovery/send legs; replace the list/detail render with ghost + identifier rows)
- Modify: `shared/ui/chat.js` (`sendConvoMessage(targetRoom, bodyOverride, meta)` stamps `com.jkali.from_proposal`; `openConvo` calls `setComposerGhostHook`)
- Modify: `shared/ui/nav.js` (remove `listMode`/Proposals toggle, `renderHomeLayer` always chats)
- Modify: `apps/user/index.html` (remove `#list-mode`; add `#convo-ghost` inside `#detail-chat` above `#convo-compose`)
- Modify: `apps/user/style.css` (ghost styles; delete Proposals-inbox rules)
- Modify: `apps/user/CLAUDE.md` (proposals section + new content stamp)
- Test: update `tests/unit/proposal_classification.test.js`, `tests/unit/proposal_row.test.js`; add `tests/unit/ghost_drafts.test.js`

**Interfaces:**
- Consumes: `pendingDrafts`, `retiredDrafts`, `draftPending` (Task 1); `rec.drafts` (Task 2); `sendConvoMessage`.
- Produces:
  - Feed record `drafts: Array<{eventId, body, ts, template}>` = every unhandled room-targeted, non-auto-sent, non-ambiguous proposal for that room (pending OR retired; the render-time `draftPending` decides which).
  - `proposals.js` exports `attachDrafts(proposals, handled, feedModel)` (pure: writes `drafts` on each record, clears records with none, returns count of rooms with drafts) and `identifierDrafts(proposals, handled)` (pending person-targeted ones).
  - `chat.js` exports `setComposerGhostHook(fn(roomId))`; `sendConvoMessage(targetRoom, bodyOverride, meta)` where `meta = { fromProposal: eventId }` adds content key `'com.jkali.from_proposal': eventId` (cosmetic provenance; the master reads it to show "sent by teammate").
  - `localStorage` `HANDLED_KEY` semantics unchanged (dismissed/sent ids).

- [ ] **Step 1: Write the failing test**

```js
// tests/unit/ghost_drafts.test.js
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/ghost_drafts.test.js
import assert from 'node:assert/strict';
import { parseProposal, attachDrafts, identifierDrafts } from '../../apps/user/proposals.js';
import { pendingDrafts, retiredDrafts } from '../../shared/model/attention.js';

const mk = (c, id) => parseProposal({ type: 'com.jkali.proposal', event_id: id, content: c });
const props = [
  mk({ target_room: '!a:l', body: 'one', origin_ts: 100 }, '$1'),
  mk({ target_room: '!a:l', body: 'two', origin_ts: 300 }, '$2'),
  mk({ target_room: '!a:l', body: 'auto', origin_ts: 400, 'com.jkali.auto_sent': true, sent_event_id: '$x' }, '$3'),
  mk({ target_room: '!b:l', body: 'dismissed', origin_ts: 500 }, '$4'),
  mk({ target_identifier: '+14155550142', target_source: 'imessage', body: 'new chat', origin_ts: 600 }, '$5'),
];
const feed = new Map([
  ['!a:l', { id: '!a:l', lastTs: 200, drafts: [] }],
  ['!b:l', { id: '!b:l', lastTs: 10, drafts: [{ eventId: 'stale' }] }],
]);
const handled = new Set(['$4']);
const rooms = attachDrafts(props, handled, feed);
assert.equal(rooms, 1);
assert.deepEqual(feed.get('!a:l').drafts.map(d => d.eventId), ['$2', '$1'], 'auto-sent excluded, newest first');
assert.deepEqual(feed.get('!b:l').drafts, [], 'dismissed removed; stale cleared');
assert.deepEqual(pendingDrafts(feed.get('!a:l').drafts, 200).map(d => d.eventId), ['$2']);
assert.deepEqual(retiredDrafts(feed.get('!a:l').drafts, 200, 1000).map(d => d.eventId), ['$1']);
assert.deepEqual(identifierDrafts(props, handled).map(p => p.eventId), ['$5']);
console.log('ghost_drafts.test.js: ok');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/ghost_drafts.test.js`
Expected: FAIL with `does not provide an export named 'attachDrafts'`. (If the import fails earlier because `proposals.js` imports DOM-touching modules at top level, that is fine in node: the shared modules only touch `document` inside functions. If any import throws at module evaluation, guard it exactly like `apps/master/main.js` does.)

- [ ] **Step 3: sendConvoMessage meta + ghost hook in chat.js**

In `shared/ui/chat.js`:
```js
async function sendConvoMessage(targetRoom, bodyOverride, meta) {
```
and where `content` is built:
```js
  const content = { msgtype: 'm.text', body };
  // Cosmetic provenance only (same role as the uplink's auto_sent_from_proposal
  // stamp): lets the mirrored copy say "sent by teammate" against a suggestion.
  // Never read by any trust/guard logic.
  if (meta && typeof meta.fromProposal === 'string' && /^\$[A-Za-z0-9._~:+/=-]{1,255}$/.test(meta.fromProposal)) {
    content['com.jkali.from_proposal'] = meta.fromProposal;
  }
```
Use `content` in both the optimistic `renderMessageEvent({... content ...})` and the `api('PUT', ...)` body. Add:
```js
let composerGhostHook = null;
function setComposerGhostHook(fn) { composerGhostHook = typeof fn === 'function' ? fn : null; }
```
and in `openConvo` after `renderConvoTabs(roomId);` add `if (composerGhostHook) { try { composerGhostHook(roomId); } catch (e) {} }`. Export `setComposerGhostHook`.

- [ ] **Step 4: HTML and nav**

`apps/user/index.html`: delete the whole `<div id="list-mode" class="list-mode">…</div>` block. Inside `#detail-chat`, insert before `<div id="convo-compose">`:
```html
            <div id="convo-ghost" class="hidden"></div>
```
Keep `#detail-proposal` (used for person-targeted drafts).

`shared/ui/nav.js`: delete `listMode`, `showListMode`, `setListMode`, `wireListMode`, `setProposalsViewHook`/`proposalsViewHook`, and the `wireListMode()` call in `buildNav`. Replace `renderHomeLayer` with:
```js
function renderHomeLayer() {
  showListSearch('home');
  setDetailMode(S.openRoomId ? 'chat' : 'empty');
  renderHome();
  const convoPane = $('msgr-convo');
  if (S.openRoomId) { if (convoPane) convoPane.classList.remove('no-selection'); setActiveConvoRow(S.openRoomId); }
  else { if (convoPane) convoPane.classList.add('no-selection'); setActiveConvoRow(null); }
}
```
In `navTo`, the `home` branch becomes `setWorkspaceLayout(true); renderHomeLayer();` and remove `showListMode(false)` calls. Remove `setListMode, setProposalsViewHook` from the export list (keep `renderHomeLayer`).

- [ ] **Step 5: Rewrite the render layer of proposals.js**

Keep lines 1–230 (imports, HANDLED_KEY, parse, partition, pendingForRoom, rowGesture, discovery, fetchProposals, helpers, sendProposal, sendIdentifierProposal), with ONE change in `parseProposal` (F17 — the manager controls `content.origin_ts`, so a far-future value must never keep a ghost pending):
```js
  const serverTs = typeof e.origin_server_ts === 'number' ? e.origin_server_ts : 0;
  const claimed = typeof c.origin_ts === 'number' ? c.origin_ts : 0;
  const ts = (serverTs && claimed) ? Math.min(serverTs, claimed) : (serverTs || claimed);
``` Change imports: drop `setProposalsViewHook`; add `sanitize` to the `el.js` import; add `setComposerGhostHook` from chat.js, `scheduleFeedRender` from `../../shared/ui/search.js`, and `pendingDrafts, retiredDrafts` from `../../shared/model/attention.js`. Replace everything from `// After send/reject` down to (not including) `function initProposalsUI` with:

```js
// ---- feed-record drafts (the ghost's data) -----------------------------------
// PURE: write each room's unhandled, room-targeted, actionable proposals onto
// its feed record as `drafts` (newest first) and clear rooms with none. Whether
// a draft is PENDING or RETIRED is decided at render time by attention.js's
// draftPending(draft.ts, rec.lastTs), so a phone reply that bumps lastTs
// retires the ghost on the next render with no extra bookkeeping.
function attachDrafts(proposals, handled, feed) {
  const byRoom = new Map();
  for (const p of (Array.isArray(proposals) ? proposals : [])) {
    if (!p || p.kind !== 'room' || p.autoSent || p.ambiguous) continue;
    if (handled && typeof handled.has === 'function' && handled.has(p.eventId)) continue;
    if (!byRoom.has(p.targetRoom)) byRoom.set(p.targetRoom, []);
    byRoom.get(p.targetRoom).push({ eventId: p.eventId, body: p.body, ts: p.ts, template: p.template });
  }
  let rooms = 0;
  for (const rec of feed.values()) {
    const list = byRoom.get(rec.id);
    rec.drafts = list ? list.sort((a, b) => b.ts - a.ts) : [];
    if (rec.drafts.length) rooms++;
  }
  return rooms;
}
function identifierDrafts(proposals, handled) {
  return (Array.isArray(proposals) ? proposals : [])
    .filter(p => p && p.kind === 'identifier' && !p.autoSent && !p.ambiguous && !(handled && handled.has(p.eventId)))
    .sort((a, b) => b.ts - a.ts);
}

// ---- ghost composer ----------------------------------------------------------
let ghostIndex = 0;      // which pending draft the ghost shows (0 = newest)
let ghostRoom = null;

function renderGhost(roomId) {
  const host = $('convo-ghost');
  if (!host) return;
  if (roomId !== ghostRoom) { ghostRoom = roomId; ghostIndex = 0; }
  host.replaceChildren();
  const rec = feedModel.get(roomId);
  if (!rec) { host.classList.add('hidden'); return; }
  const pending = pendingDrafts(rec.drafts, rec.lastTs);
  const retired = retiredDrafts(rec.drafts, rec.lastTs, Date.now());
  if (!pending.length && !retired.length) { host.classList.add('hidden'); return; }
  host.classList.remove('hidden');
  if (pending.length) {
    if (ghostIndex >= pending.length) ghostIndex = 0;
    const d = pending[ghostIndex];
    const box = el('div', 'ghost pending');
    const cap = el('div', 'ghost-cap');
    cap.appendChild(el('span', '', (d.template ? 'Template suggested' : 'Suggested by your manager') + ' · ' + feedRelTime(d.ts) + ' ago'));
    if (pending.length > 1) {
      const sw = el('button', 'ghost-switch', (ghostIndex + 1) + ' of ' + pending.length + ' ↕');
      sw.type = 'button'; sw.title = 'Show the next suggestion';
      sw.addEventListener('click', () => { ghostIndex = (ghostIndex + 1) % pending.length; renderGhost(roomId); });
      cap.appendChild(sw);
    } else cap.appendChild(el('span', 'muted', 'Retires if the thread moves on'));
    box.appendChild(cap);
    // F14: display and send the SAME string. sanitize() keeps newlines, strips
    // bidi/zero-width/control chars and clamps at 4000 — sanitizeLine would show
    // 64 chars of a body that then sends at full length.
    const shown = sanitize(d.body);
    box.appendChild(el('div', 'ghost-text', shown));
    const acts = el('div', 'ghost-acts');
    const dismiss = el('button', 'ghost-btn', 'Dismiss'); dismiss.type = 'button';
    dismiss.addEventListener('click', () => { markHandled({ eventId: d.eventId }); afterHandled(); });
    const edit = el('button', 'ghost-btn', 'Edit'); edit.type = 'button';
    edit.addEventListener('click', () => { prefillComposer(shown); markHandled({ eventId: d.eventId }); afterHandled(); });
    const send = el('button', 'ghost-btn primary', 'Send as me'); send.type = 'button';
    send.addEventListener('click', async () => {
      send.disabled = true;
      const ok = await sendConvoMessage(roomId, shown, { fromProposal: d.eventId });   // explicit target, same guard
      send.disabled = false;
      if (!ok) return;
      markHandled({ eventId: d.eventId });
      // F16: siblings are NOT marked handled — the sent message bumps lastTs and
      // draftPending retires them visibly (struck through, Restore) on render.
      afterHandled();
    });
    acts.appendChild(dismiss); acts.appendChild(edit); acts.appendChild(send);
    box.appendChild(acts);
    host.appendChild(box);
  }
  for (const d of retired.slice(0, 2)) {
    const box = el('div', 'ghost retired');
    const cap = el('div', 'ghost-cap');
    cap.appendChild(el('span', '', 'Manager suggested ' + feedRelTime(d.ts) + ' ago · the thread moved on'));
    const restore = el('button', 'ghost-switch', 'Restore'); restore.type = 'button';
    restore.addEventListener('click', () => { prefillComposer(sanitize(d.body)); markHandled({ eventId: d.eventId }); afterHandled(); });
    cap.appendChild(restore);
    box.appendChild(cap);
    box.appendChild(el('div', 'ghost-text', sanitize(d.body)));
    host.appendChild(box);
  }
}

function afterHandled() {
  attachDrafts(allProposals, loadHandled(), feedModel);
  scheduleFeedRender();
  if (S.openRoomId) renderGhost(S.openRoomId);
  renderIdentifierRows();
}

// ---- person-targeted drafts: a row at the top of the chat list -----------------
// No inbox: a "start a new chat" suggestion has no room, so it surfaces as a
// pseudo-row above the list (draft stripe) that opens the existing detail pane.
function renderIdentifierRows() {
  const list = $('list-body');
  if (!list) return;
  for (const old of list.querySelectorAll('.convo.identifier-draft')) old.remove();
  if (S.activeNavKey !== 'home') return;
  const rows = identifierDrafts(allProposals, loadHandled());
  for (const p of rows.reverse()) {
    const row = el('div', 'convo identifier-draft');
    row.setAttribute('role', 'button'); row.tabIndex = 0;
    const stripe = el('div', 'stripe'); stripe.appendChild(el('i', 'seg draft')); row.appendChild(stripe);
    row.appendChild(buildPlatBadge(p.targetSource));
    const meta = el('div', 'meta');
    meta.appendChild(el('div', 'title', 'New chat suggested: ' + sanitizeLine(p.targetDisplay || p.targetIdentifier)));
    meta.appendChild(el('div', 'preview', 'Draft: ' + sanitizeLine(p.body).slice(0, 90)));
    row.appendChild(meta);
    const open = () => { setDetailMode('proposal'); renderDetail(p); };
    row.addEventListener('click', open);
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    list.insertBefore(row, list.firstChild);
  }
}

// Detail pane for a person-targeted draft (unchanged send leg: confirm + sendCmd start-chat).
function renderDetail(p) {
  const host = $('proposal-detail-body');
  if (!host) return;
  host.replaceChildren();
  const title = $('proposal-detail-title');
  if (title) title.textContent = targetName(p);
  const to = el('div', 'proposal-to');
  to.appendChild(buildPlatBadge(sourceOf(p)));
  to.appendChild(el('span', 'proposal-to-name', targetName(p)));
  to.appendChild(el('span', 'muted', ' — starts a NEW iMessage chat'));
  host.appendChild(to);
  host.appendChild(el('p', 'muted proposal-note', 'Suggested by your manager — not sent yet.'));
  const ta = el('textarea', 'proposal-body-full'); ta.value = p.body; host.appendChild(ta);
  const err = el('div', 'proposal-card-error hidden'); host.appendChild(err);
  const actions = el('div', 'proposal-actions');
  const sendBtn = el('button', 'proposal-btn proposal-send primary', 'Send'); sendBtn.type = 'button';
  sendBtn.addEventListener('click', () => sendProposal(p, ta.value, err, sendBtn));
  const rejectBtn = el('button', 'proposal-btn proposal-dismiss', 'Reject'); rejectBtn.type = 'button';
  rejectBtn.addEventListener('click', () => { markHandled(p); setDetailMode('empty'); afterHandled(); });
  actions.appendChild(sendBtn); actions.appendChild(rejectBtn);
  host.appendChild(actions);
  const back = $('proposal-back');
  if (back && !back.dataset.wired) { back.dataset.wired = '1'; back.addEventListener('click', () => setDetailMode('empty')); }
}

// ---- refresh ----------------------------------------------------------------------
async function refresh() {
  let proposals;
  try { proposals = await fetchProposals(); } catch (e) { return; }
  const sig = (arr) => arr.map((p) => p.eventId).sort().join(',');
  if (sig(proposals) === sig(allProposals)) { if (S.openRoomId) renderGhost(S.openRoomId); return; }
  allProposals = proposals;
  afterHandled();
}
let pollTimer = null;
```
Update `sendProposal`'s success branch: `if (ok) { markHandled(p); afterHandled(); setDetailMode('empty'); return; }` and remove its `focusListAfterHandle` references. Replace `initProposalsUI` with:
```js
function initProposalsUI() {
  setComposerGhostHook(renderGhost);
  setFeedRenderHook(() => { renderIdentifierRows(); if (S.openRoomId) renderGhost(S.openRoomId); });
  setAfterSendHook((roomId) => {
    // A reply typed by the teammate retires every pending draft for the room.
    const rec = feedModel.get(roomId);
    for (const d of pendingDrafts((rec && rec.drafts) || [], (rec && rec.lastTs) || 0)) markHandled({ eventId: d.eventId });
    afterHandled();
  });
  if (!pollTimer) pollTimer = setInterval(() => { refresh().catch(() => {}); }, 10000);
  refresh().catch(() => {});
}
export { initProposalsUI, parseProposal, partitionProposals, pendingForRoom, rowGesture, attachDrafts, identifierDrafts };
```
`setFeedRenderHook` is single-slot and `apps/user/main.js` already sets it to `refreshPlatformRail`. Change `main.js` to: `setFeedRenderHook(() => { refreshPlatformRail(); });` is NOT enough — make `search.js`'s hook a list: replace `let feedRenderHook = null; function setFeedRenderHook(fn) {...}` with
```js
const feedRenderHooks = [];
function setFeedRenderHook(fn) { if (typeof fn === 'function') feedRenderHooks.push(fn); }
function runFeedRenderHooks() { for (const fn of feedRenderHooks) { try { fn(); } catch (e) { /* one app hook must not break the list */ } } }
```
and replace every `if (feedRenderHook) feedRenderHook();` with `runFeedRenderHooks();`.
Delete from `proposals.js` every now-unused symbol (`selectedId`, `showDismissed`, `showSent`, `kebabOpen`, `updateCount`, `buildRow`, `buildHistoryRow`, `buildAmbiguousRow`, `buildTopBar`, `renderList`, `select`, `wireProposalBack`, `renderProposalsView`, `proposalsListShowing`, `attachQuickActions`, `openWithDraft`). Keep `partitionProposals`, `pendingForRoom`, `rowGesture` exported for the existing tests.

Ambiguous records ("may already have been sent") lost their inbox row: render them as a `.ghost ambiguous` box in `renderGhost` for the room (`allProposals.filter(p => p.kind === 'room' && p.ambiguous && p.targetRoom === roomId)`), text "A suggestion may already have been sent here — check above before replying", no buttons.

- [ ] **Step 6: CSS and docs**

`apps/user/style.css`: delete every rule from `/* ---- Proposals: Slack-threads-style inbox ---- */` through `.proposal-quick-no:hover {...}` EXCEPT `.proposal-card-error`, `.proposal-card-error.hidden`, `.proposal-actions`, `.proposal-btn`, `.proposal-btn.primary`, `.proposal-btn.proposal-dismiss`, `#proposal-detail-body`, `.proposal-to`, `.proposal-to-name`, `.proposal-body-full`, `.proposal-note`. Add:
```css
#convo-ghost { flex: 0 0 auto; padding: 8px 14px 0; display: grid; gap: 6px; background: var(--color-neutral-200); }
#convo-ghost.hidden { display: none !important; }
.ghost { border: 1px dashed #7c5cd6; background: #ece6fa; border-radius: 10px; padding: 8px 12px; display: grid; gap: 6px; }
.ghost-cap { display: flex; justify-content: space-between; gap: 8px; font-size: 11px; color: #7c5cd6; }
.ghost-text { font-size: 13px; color: var(--color-text); opacity: .75; white-space: pre-wrap; }
.ghost-acts { display: flex; gap: 8px; justify-content: flex-end; }
.ghost-btn { height: 28px; padding: 0 12px; border-radius: 7px; font-size: 12.5px; }
.ghost-btn.primary { background: #7c5cd6; border-color: #7c5cd6; color: #fff; }
.ghost-switch { border: 0; background: transparent; color: inherit; font: inherit; text-decoration: underline; cursor: pointer; padding: 0; }
.ghost.retired { border-style: solid; border-color: var(--color-divider); background: var(--color-surface); }
.ghost.retired .ghost-cap { color: var(--color-muted); }
.ghost.retired .ghost-text { text-decoration: line-through; opacity: .5; }
.ghost.ambiguous { border-color: #c9810a; background: #fbeed3; }
.ghost.ambiguous .ghost-cap { color: #8a5a00; }
.convo.identifier-draft .plat-badge { width: 22px; height: 22px; }
```
`apps/user/CLAUDE.md`: in the `proposals.js` bullet, replace the inbox description with: drafts render as a ghost in `#convo-ghost` above the composer; pending = `draftPending(origin_ts, room.lastTs)` from `shared/model/attention.js` (fail closed); person-targeted drafts are pseudo-rows at the top of the Home list; sending a ghost calls `sendConvoMessage(targetRoom, body, { fromProposal })`, which stamps the cosmetic `com.jkali.from_proposal` content key (never read by trust logic). Keep every invariant bullet.

- [ ] **Step 7: Fix the existing tests and run**

`tests/unit/proposal_row.test.js` and `proposal_classification.test.js`: they import `parseProposal, partitionProposals, pendingForRoom, rowGesture` — unchanged exports, so they should pass; run them. If `proposals.js` now fails to import under node because `search.js`/`chat.js` evaluate DOM at module load, fix the offending top-level statement by guarding it with `typeof document !== 'undefined'` (that is a bug worth fixing, not a test problem).
Run: `tests/run.py --unit-only` → all green. Browser check: a proposal for the open room appears as the ghost; replying from the phone (or sending any message) retires it.

---

### Task 6: Uplink — superseded gate, read-state mirror, avatar stamp (security)

**Files:**
- Modify: `agents/uplink/uplink.py` (`_direct_send_gate`, new `room_quiet_since`, `tail_once` read-state leg, new `_mirror_read_state`, `_forward_message` avatar stamp, new `_member_profile`, `_master_avatar_for`)
- Modify: `agents/uplink/CLAUDE.md` (gate list: twelve gates; new state event; new content stamp)
- Modify: `apps/user/CLAUDE.md` / `docs/SYSTEM-DESIGN.md` Direct paragraph (mention `superseded`)
- Test: `tests/unit/uplink_superseded.test.py`, `tests/unit/uplink_read_state.test.py`

**Interfaces:**
- Produces:
  - Gate reason string `"superseded"` (NOT in `QUIET_GATES`): refused when any `m.room.message` in the target room has `origin_server_ts > proposal origin_server_ts`, or when the timeline cannot be read.
  - Master mirror-room state event `com.jkali.read_state` (state_key `""`) content `{ "teammate_read_ts": int, "remote_read_ts": int, "updated_ts": int }` written by the teammate's uplink account (PL 100 in its own mirror rooms; the manager is PL 0 so cannot forge it).
  - Mirrored message content key `com.jkali.origin_avatar`: a MASTER `mxc://` for the origin sender's avatar, or absent.
  - `com.jkali.from_proposal` (written by the teammate app, Task 5) passes through `_forward_message` untouched (it already copies content).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/uplink_superseded.test.py
# Run: python3 tests/unit/uplink_superseded.test.py
import os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import uplink

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1
    else: failed += 1; print("FAIL: " + name)

class Cfg:  # the fields _direct_send_gate reads
    direct_send_cap = 5; manager_mxid = "@manager:master"; master_user = "@me:master"; local_user = "@me:localhost"

def make(messages_response):
    u = object.__new__(uplink.Uplink)
    u.cfg = Cfg()
    u.db = uplink.Uplink._open_db(os.path.join(tempfile.mkdtemp(prefix="uplink-sup-"), "state.db"))
    u.db.execute("INSERT INTO mirror_rooms (local_room_id, master_room_id) VALUES (?,?)", ("!t:localhost", "!m:master"))
    u.read_room_level = lambda room: "direct"
    def local(method, path, body=None, query=None, timeout=60):
        if "/messages" in path:
            if isinstance(messages_response, Exception): raise messages_response
            return messages_response
        raise AssertionError("unexpected local call " + path)
    u.local = local
    return u

now = int(time.time() * 1000)
ev = {"sender": "@manager:master", "origin_server_ts": now - 1000, "event_id": "$p"}
clean = {"target_room": "!t:localhost", "body": "hello there"}

# quiet room: newest message is OLDER than the proposal -> passes to the cap gate and sends
u = make({"chunk": [{"type": "m.room.message", "origin_server_ts": now - 5000}]})
body, gate = u._direct_send_gate(ev, clean, cold_start=False, suspended=False)
check("quiet room passes", body == "hello there" and gate is None)

# activity after the proposal -> superseded
u = make({"chunk": [{"type": "m.room.message", "origin_server_ts": now - 500}]})
body, gate = u._direct_send_gate(ev, clean, cold_start=False, suspended=False)
check("later message refuses", body is None and gate == "superseded")

# unreadable timeline -> fail closed
u = make(OSError("boom"))
body, gate = u._direct_send_gate(ev, clean, cold_start=False, suspended=False)
check("read failure refuses", body is None and gate == "superseded")

# non-message events after the proposal do not count
u = make({"chunk": [{"type": "m.reaction", "origin_server_ts": now - 100}]})
body, gate = u._direct_send_gate(ev, clean, cold_start=False, suspended=False)
check("reaction is not activity", gate is None)

check("superseded is a loud gate", "superseded" not in uplink.Uplink.QUIET_GATES)

# F5: a future-dated proposal (within D2-3's +60s tolerance) must not hide a reply at `now`
u = make({"chunk": [{"type": "m.room.message", "origin_server_ts": now}]})
body, gate = u._direct_send_gate(dict(ev, origin_server_ts=now + 30000), clean, cold_start=False, suspended=False)
check("future-dated proposal still superseded by a reply at now", gate == "superseded")

# F12: inbound provenance stamps are stripped unless the event is from_me
fw = object.__new__(uplink.Uplink)
fw.cfg = Cfg(); fw.self_mxids = set()
fw.timestamp_event = lambda room, source, e: e
fw._member_profile = lambda room, sender: ("Someone", None)
fw.active_link_for_dispatch = lambda: False      # stop before the PUT; we only inspect content
captured = {}
orig = uplink.Uplink._forward_message
# _forward_message returns early at active_link_for_dispatch; capture content via a wrapped stamp_timestamp
real_stamp = uplink.stamp_timestamp
def spy(content, ev): captured.update(content); return real_stamp(content, ev)
uplink.stamp_timestamp = spy
try:
    inbound = {"type": "m.room.message", "event_id": "$in", "sender": "@whatsapp_555:localhost",
               "origin_server_ts": now, "content": {"msgtype": "m.text", "body": "hi",
               "com.jkali.origin_avatar": "mxc://attacker.example/x", "com.jkali.from_proposal": "$p",
               "com.jkali.auto_sent_from_proposal": "$p"}}
    try: fw._forward_message("!t:localhost", "!m:master", "whatsapp", inbound)
    except Exception: pass
    check("inbound origin_avatar stripped", "com.jkali.origin_avatar" not in captured)
    check("inbound from_proposal stripped on non-from_me", "com.jkali.from_proposal" not in captured)
    check("inbound auto_sent_from_proposal stripped on non-from_me", "com.jkali.auto_sent_from_proposal" not in captured)
finally:
    uplink.stamp_timestamp = real_stamp
print("uplink_superseded: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
```

```python
# tests/unit/uplink_read_state.test.py
# Run: python3 tests/unit/uplink_read_state.test.py
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import uplink

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1
    else: failed += 1; print("FAIL: " + name)

room = {
    "account_data": {"events": [{"type": "m.fully_read", "content": {"event_id": "$e2"}}]},
    "ephemeral": {"events": [{"type": "m.receipt", "content": {
        "$e1": {"m.read": {"@me:localhost": {"ts": 900}, "@whatsapp_555:localhost": {"ts": 700}}},
        "$e2": {"m.read": {"@whatsapp_123:localhost": {"ts": 950}, "@whatsapp_555:localhost": {"ts": 800}, "@whatsappbot:localhost": {"ts": 999}}},
    }}]},
}
selfs = {"@me:localhost", "@whatsapp_123:localhost"}
bots = {"@whatsappbot:localhost"}
rs = uplink.read_state_from_room(room, selfs, bots)
check("teammate read = newest own/ghost receipt", rs["teammate_read_ts"] == 950)
check("remote read = newest other-party receipt", rs["remote_read_ts"] == 800)
check("empty room -> zeros", uplink.read_state_from_room({}, selfs, bots) == {"teammate_read_ts": 0, "remote_read_ts": 0})

# _mirror_read_state writes once per change, never on no-change
puts = []
u = object.__new__(uplink.Uplink)
u.db = uplink.Uplink._open_db(os.path.join(tempfile.mkdtemp(prefix="uplink-rs-"), "state.db"))
u.master = lambda method, path, body=None, query=None, timeout=60: puts.append((method, path, body)) or {}
u._mirror_read_state("!local:localhost", "!m:master", rs)
u._mirror_read_state("!local:localhost", "!m:master", rs)
check("one PUT for an unchanged state", len(puts) == 1)
check("state type + key", puts[0][1].endswith("/state/com.jkali.read_state/") and puts[0][0] == "PUT")
check("content carries both ts", puts[0][2]["teammate_read_ts"] == 950 and puts[0][2]["remote_read_ts"] == 800)
u._mirror_read_state("!local:localhost", "!m:master", dict(rs, remote_read_ts=1000))
check("changed state writes again", len(puts) == 2)
print("uplink_read_state: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 tests/unit/uplink_superseded.test.py` → `FAIL: later message refuses` (gate is None today) and `FAIL: superseded is a loud gate`.
Run: `python3 tests/unit/uplink_read_state.test.py` → `AttributeError: module 'uplink' has no attribute 'read_state_from_room'`.

- [ ] **Step 3: Implement the superseded gate**

In `agents/uplink/uplink.py`, add a module constant near `DIRECT_SEND_FRESH_MS`:
```python
SUPERSEDED_SCAN_LIMIT = 10                  # D2-12: newest local messages to inspect
READ_STATE_TYPE = "com.jkali.read_state"    # mirror-room state the uplink owns (read state)
ORIGIN_AVATAR_KEY = "com.jkali.origin_avatar"
```
Add a method next to `read_room_level`:
```python
    def room_quiet_since(self, local_room_id, since_ts):
        """D2-12: True iff NO m.room.message in the target room is newer than
        since_ts. The daemon-side twin of the app's draft-retirement rule: a
        proposal the conversation has moved past is never auto-sent (that is
        the double text). FAIL CLOSED — any read error, bad id or junk => False.
        """
        if not isinstance(local_room_id, str) or not ROOMID_RE.match(local_room_id):
            return False
        if not isinstance(since_ts, int) or isinstance(since_ts, bool):
            return False
        try:
            data = self.local("GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(local_room_id, safe="")
                              + "/messages", query={"dir": "b", "limit": str(SUPERSEDED_SCAN_LIMIT),
                                                    "filter": json.dumps({"types": ["m.room.message"]})})
        except Exception:                          # noqa: BLE001 — fail closed
            return False
        chunk = data.get("chunk") if isinstance(data, dict) else None
        if not isinstance(chunk, list):
            return False
        for e in chunk:
            if not isinstance(e, dict) or e.get("type") != "m.room.message":
                continue
            ts = e.get("origin_server_ts")
            if isinstance(ts, int) and not isinstance(ts, bool) and ts > since_ts:
                return False
        return True
```
In `_direct_send_gate`, AFTER the D2-6 cap check (F3: the only network read among the gates runs last) and before `return body, None`:
```python
        # D2-12: conversation quiet since the proposal was made. Any message
        # from anyone (the teammate's own phone reply included) after the
        # proposal supersedes it. F5: compare against the proposal time CLAMPED
        # TO NOW — D2-3 tolerates +60s of future-dating, and a future ots would
        # otherwise hide real activity in that window from this gate.
        if not self.room_quiet_since(target, min(ots, now_ms)):
            return None, "superseded"
```
Note `ots` and `now_ms` are already bound above (D2-3). Docstring of `room_quiet_since` must add: "An EMPTY page reads as quiet (a new room); a non-list chunk or any error reads as NOT quiet." and "Reduces the double text to the one-round-trip window between this read and the PUT; it cannot eliminate it." Leave `QUIET_GATES` unchanged (so `superseded` logs WARNING + audit `refused:superseded`). The refused proposal still files as an ordinary draft; the teammate app's render-time rule then shows it as retired.

- [ ] **Step 4: Implement read-state mirroring**

Module-level pure function (near `sanitize_proposal_content`):
```python
def read_state_from_room(room, self_ids, bot_ids):
    """Teammate + other-party read timestamps from one LOCAL /sync room section.

    teammate_read_ts: newest m.read receipt from the teammate's own account or
    one of their attested bridge ghosts. remote_read_ts: newest receipt from
    anyone else except a bridge bot. Pure; missing/malformed => 0.
    """
    out = {"teammate_read_ts": 0, "remote_read_ts": 0}
    if not isinstance(room, dict):
        return out
    for e in ((room.get("ephemeral") or {}).get("events") or []):
        if not isinstance(e, dict) or e.get("type") != "m.receipt" or not isinstance(e.get("content"), dict):
            continue
        for per_event in e["content"].values():
            reads = per_event.get("m.read") if isinstance(per_event, dict) else None
            if not isinstance(reads, dict):
                continue
            for user, info in reads.items():
                ts = info.get("ts") if isinstance(info, dict) else None
                if not isinstance(ts, int) or isinstance(ts, bool):
                    continue
                if user in bot_ids:
                    continue
                key = "teammate_read_ts" if user in self_ids else "remote_read_ts"
                if ts > out[key]:
                    out[key] = ts
    return out
```
Method:
```python
    def _mirror_read_state(self, local_room_id, master_room_id, state):
        """PUT com.jkali.read_state on the mirror when it changed (meta-cached).
        A state event the uplink owns (PL 100 in its own mirror rooms) is the
        honest carrier: receipts cannot be forwarded across homeservers. Write
        failure is logged and retried on the next change; never raised."""
        key = "read_state:" + local_room_id
        payload = {"teammate_read_ts": int(state.get("teammate_read_ts") or 0),
                   "remote_read_ts": int(state.get("remote_read_ts") or 0)}
        sig = "%d:%d" % (payload["teammate_read_ts"], payload["remote_read_ts"])
        if self.meta_get(key) == sig:
            return
        payload["updated_ts"] = int(time.time() * 1000)
        try:
            # F9: short timeout — this runs from local ingestion, which must never
            # stall on a sleeping master.
            self.master("PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(master_room_id, safe="")
                        + "/state/" + READ_STATE_TYPE + "/", payload, timeout=15)
            self.meta_set(key, sig)
        except MasterUnreachable:
            log.debug("read_state not mirrored (master unreachable)")
        except Exception as e:                     # noqa: BLE001 — cosmetic state, never blocks delivery
            log.debug("read_state not mirrored (%s)", type(e).__name__)
```
In `tail_once`, inside the per-room loop right after `master_room_id = row[0]`, add:
```python
            try:
                bots = {s.get("botMxid") for s in getattr(self, "_source_bots", []) if s.get("botMxid")}
                selfs = set(self.self_mxids) | {self.cfg.local_user}
                rs = read_state_from_room(room, selfs, bots)
                # F9: the SAME per-write consent recheck every other master write
                # performs, so an unshared room never leaks its read state.
                if ((rs["teammate_read_ts"] or rs["remote_read_ts"])
                        and self.active_link_for_dispatch()
                        and self.archive_level(local_room_id) in ("share", "direct")):
                    self._mirror_read_state(local_room_id, master_room_id, rs)
            except Exception as e:                 # noqa: BLE001 — never affects event forwarding
                log.debug("read_state skipped (%s)", type(e).__name__)
```
Where `_source_bots` does not exist, derive bot ids from `shared/source_catalog.json` the way the uplink already resolves sources (grep `source_catalog` in uplink.py and reuse that loader; if it exposes bot mxids under a different name, use that). If no loader exists, read the JSON once in `__init__` into `self._source_bots`.

- [ ] **Step 5: Avatar stamp**

Replace `_display_name` with a profile reader and keep `_display_name` as a thin wrapper:
```python
    def _member_profile(self, local_room_id, sender):
        """(displayname, avatar_url) from local member state; (sender, None) on failure."""
        try:
            res = self.local("GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(local_room_id, safe="")
                             + "/state/m.room.member/" + urllib.parse.quote(sender, safe=""))
            name = res.get("displayname") or sender
            avatar = res.get("avatar_url")
            return name, (avatar if isinstance(avatar, str) and MXC_RE.match(avatar) else None)
        except urllib.error.HTTPError:
            return sender, None

    def _display_name(self, local_room_id, sender):
        return self._member_profile(local_room_id, sender)[0]

    def _master_avatar_for(self, local_mxc, local_room_id):
        """Re-upload an avatar once per local mxc (meta-cached) -> master mxc or None.

        F13b: failures are cached too (sentinel "-"), so an unfetchable or
        oversized avatar costs ONE attempt per mxc, not one per message. A new
        avatar is a new mxc and therefore a new key.
        """
        key = "avatar:" + local_mxc
        cached = self.meta_get(key)
        if cached == "-":
            return None
        if cached:
            return cached
        new_uri = self._reupload_media({"url": local_mxc, "msgtype": "m.image"}, local_room_id)
        self.meta_set(key, new_uri or "-")
        return new_uri
```
`MXC_RE` exists in uplink.py (line ~216). Add a module constant `FROM_PROPOSAL_KEY = "com.jkali.from_proposal"` next to `ORIGIN_AVATAR_KEY`. In `_forward_message`, IMMEDIATELY after the `content[FROM_ME_KEY] = (...)` assignment add (F12):
```python
        # Provenance stamps are OURS. A remote party / bridge can put these keys
        # in their own content; strip anything we did not stamp ourselves, and
        # keep the teammate-authored one only on a message the from_me gate owns.
        content.pop(ORIGIN_AVATAR_KEY, None)
        if content.get(FROM_ME_KEY) is not True:
            content.pop(FROM_PROPOSAL_KEY, None)
            content.pop(AUTO_SENT_FROM_PROPOSAL_KEY, None)
```
Then replace `content[ORIGIN_SENDER_KEY] = self._display_name(local_room_id, sender)` with:
```python
        name, avatar = self._member_profile(local_room_id, sender)
        content[ORIGIN_SENDER_KEY] = name
        # F13d: this _reupload_media call MUST stay before the media block below —
        # that block resets _media_retryable on entry, so the media_retry insert
        # reads the media call's flag, not this one's. Do not reorder.
        if avatar:
            master_avatar = self._master_avatar_for(avatar, local_room_id)
            if master_avatar:
                content[ORIGIN_AVATAR_KEY] = master_avatar
```
`_reupload_media(content, local_room_id=None)` (line ~1329) enforces `cfg.media_max` on the body and re-checks consent before uploading; an avatar over the cap yields None.

F13c: in `agents/uplink/durable_sync.py` (~line 290, `destination_binding`), extend the meta delete so a destination change forgets both caches:
```python
            self.db.execute("DELETE FROM meta WHERE k IN ('master_contacts_room','master_proposals_room',"
                            "'proposal_sync_since','sync_since') OR k LIKE 'mname:%' OR k LIKE 'last_event:%' "
                            "OR k LIKE 'avatar:%' OR k LIKE 'read_state:%'")
```
(keep whatever fixed keys the existing statement already lists; only add the two `LIKE` clauses).

- [ ] **Step 6: Docs**

`agents/uplink/CLAUDE.md`: gate list gains D2-12 `superseded` ("conversation quiet since origin", compared against the proposal time clamped to now; fail closed except an empty page = quiet; loud; it reduces the double text to the one-round-trip window between its read and the PUT and cannot eliminate it; it also limits a manager burst to one auto-send per room per batch because P1's own send supersedes P2). Add `com.jkali.read_state` (uplink-owned mirror state, written only for share/direct rooms, left behind on revocation like message content) and `com.jkali.origin_avatar` / `com.jkali.from_proposal` (cosmetic stamps; inbound copies are stripped unless from_me) to the "what the uplink stamps" section. Also one line in `docs/SHARE-LOGIC.md` and in `apps/user/index.html`'s `#settings-section-sharing` lead: sharing a conversation also shares your read position in it with the manager (F10). Root `CLAUDE.md` and `docs/SYSTEM-DESIGN.md`: "eleven gates" → "twelve gates", add "a conversation that has moved on since the proposal" to the Direct requirement sentence.

- [ ] **Step 7: Run tests**

Run: `python3 tests/unit/uplink_superseded.test.py` and `python3 tests/unit/uplink_read_state.test.py` → all passed.
Run: `python3 tests/unit/uplink_proposals.test.py tests/unit/uplink_proposal_sanitize.test.py` and `tests/run.py --unit-only` → green. If `uplink_proposals.test.py` now hits `room_quiet_since` through `_direct_send_gate`, give its fixture `u.room_quiet_since = lambda room, ts: True`.
Integration (on demand, mutates the disposable stacks only): `tests/integration/run.sh 10_proposal_down`.

---

### Task 7: Manager console parity

**Files:**
- Modify: `apps/master/main.js` (parseSnapshot reads `com.jkali.read_state`; rail switcher; row anatomy; cluster rows; header read lines; suggestion stack; avatar on bubbles)
- Modify: `apps/master/index.html` (`#nav-teammates-rail`, header sub lines, `#proposal-stack`)
- Modify: `apps/master/style.css`, `shared/style/beepa.css` (reuse Task 3/5 classes)
- Modify: `apps/master/CLAUDE.md`
- Modify: `tests/unit/master_timeline.test.js` (lines 92–126 exercise the deleted single-bubble overlay; replace them as in Step 7b)
- Test: `tests/unit/master_suggestions.test.js`, keep `master_share_level.test.js` green

**Interfaces:**
- Consumes: `shared/model/attention.js` (allowed leaf import), `com.jkali.read_state`, `com.jkali.origin_avatar`, `com.jkali.from_proposal`, `com.jkali.auto_sent_from_proposal`.
- Produces: `main.js` exports `suggestionStates(proposals, roomEvents, readState, now)` (pure): for each room-targeted proposal `{eventId, body, ts}` returns `{...p, state}` where `state ∈ 'sent' | 'auto' | 'retired' | 'seen' | 'pending'` by this rule, first match wins:
  1. `sent`: a mirrored message carries `com.jkali.from_proposal === eventId`.
  2. `auto`: a mirrored message carries `com.jkali.auto_sent_from_proposal === eventId`.
  3. `retired`: any mirrored `m.room.message` has `mirrorTs > p.ts`.
  4. `seen`: `readState.teammate_read_ts >= p.ts`.
  5. `pending`.
  Also `latestRoomProposal` is replaced by `roomProposals(events, targetRoom)` returning ALL (newest first); keep `latestRoomProposal` as `roomProposals(...)[0] || null` so `master_timeline.test.js` stays valid.

- [ ] **Step 1: Write the failing test**

```js
// tests/unit/master_suggestions.test.js
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/master_suggestions.test.js
import assert from 'node:assert/strict';
import { suggestionStates, roomProposals, latestRoomProposal } from '../../apps/master/main.js';

const props = [
  { eventId: '$a', body: 'A', ts: 100 },
  { eventId: '$b', body: 'B', ts: 200 },
  { eventId: '$c', body: 'C', ts: 300 },
  { eventId: '$d', body: 'D', ts: 400 },
  { eventId: '$e', body: 'E', ts: 500 },
];
const msgs = [
  { type: 'm.room.message', ts: 150, content: { body: 'x', 'com.jkali.from_me': true, 'com.jkali.from_proposal': '$a' } },
  { type: 'm.room.message', ts: 250, content: { body: 'y', 'com.jkali.from_me': true, 'com.jkali.auto_sent_from_proposal': '$b' } },
  { type: 'm.room.message', ts: 350, content: { body: 'z' } },
];
const states = suggestionStates(props, msgs, { teammate_read_ts: 450 }, 1000);
assert.deepEqual(states.map(s => s.state), ['sent', 'auto', 'retired', 'seen', 'pending']);
// F12: a RECEIVED message carrying from_proposal never marks a suggestion sent
const forged = [{ type: 'm.room.message', ts: 50, content: { body: 'z', 'com.jkali.from_proposal': '$e' } }];
assert.deepEqual(suggestionStates(props.slice(4), forged, null, 1000).map(s => s.state), ['pending']);
assert.deepEqual(suggestionStates(props.slice(4), [], null, 1000).map(s => s.state), ['pending']);

const events = [
  { type: 'com.jkali.proposal', event_id: '$1', content: { target_room: '!t:l', body: 'one', origin_ts: 10 } },
  { type: 'com.jkali.proposal', event_id: '$2', content: { target_room: '!t:l', body: 'two', origin_ts: 30 } },
  { type: 'com.jkali.proposal', event_id: '$3', content: { target_room: '!o:l', body: 'other', origin_ts: 40 } },
  { type: 'com.jkali.proposal', event_id: '$4', content: { target_room: '!t:l', body: '   ', origin_ts: 50 } },
];
assert.deepEqual(roomProposals(events, '!t:l').map(p => p.eventId), ['$2', '$1']);
assert.equal(latestRoomProposal(events, '!t:l').eventId, '$2');
console.log('master_suggestions.test.js: ok');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/master_suggestions.test.js`
Expected: FAIL `does not provide an export named 'suggestionStates'`.

- [ ] **Step 3: Pure helpers in main.js**

Add import: `import { initials as initials2, indicatorsFor, clusterFeed, applyFilter } from '../../shared/model/attention.js';` (keep the local `initials()`; or delete the local one and use the import — one definition only). Replace `latestRoomProposal` with:
```js
function roomProposals(events, targetRoom) {
  if (!Array.isArray(events) || typeof targetRoom !== 'string' || !targetRoom) return [];
  const out = [];
  for (const e of events) {
    if (!e || e.type !== 'com.jkali.proposal' || !e.content) continue;
    if (e.content.target_room !== targetRoom) continue;
    const body = typeof e.content.body === 'string' ? e.content.body.trim() : '';
    if (!body) continue;
    const ts = typeof e.content.origin_ts === 'number' ? e.content.origin_ts
      : (typeof e.origin_server_ts === 'number' ? e.origin_server_ts : 0);
    out.push({ body, eventId: e.event_id, ts });
  }
  return out.sort((a, b) => b.ts - a.ts);
}
function latestRoomProposal(events, targetRoom) { return roomProposals(events, targetRoom)[0] || null; }

// Pure state per suggestion. `messages` are {type, ts, content} (ts = mirrorTs).
function suggestionStates(proposals, messages, readState, now) {
  const msgs = (Array.isArray(messages) ? messages : []).filter(m => m && m.type === 'm.room.message');
  // F12: provenance stamps are trusted ONLY on the teammate's own messages
  // (the from_me flag the uplink stamps server-side) — the same gate renderBubble
  // already applies to auto_sent_from_proposal. A remote party's content cannot
  // mark a suggestion "sent".
  const own = msgs.filter(m => m.content && m.content['com.jkali.from_me'] === true);
  const readTs = readState && typeof readState.teammate_read_ts === 'number' ? readState.teammate_read_ts : 0;
  return (Array.isArray(proposals) ? proposals : []).map(p => {
    let state = 'pending';
    if (own.some(m => m.content['com.jkali.from_proposal'] === p.eventId)) state = 'sent';
    else if (own.some(m => m.content['com.jkali.auto_sent_from_proposal'] === p.eventId)) state = 'auto';
    else if (msgs.some(m => typeof m.ts === 'number' && m.ts > p.ts)) state = 'retired';
    else if (readTs >= p.ts && p.ts > 0) state = 'seen';
    return Object.assign({}, p, { state });
  });
}
```
Add `roomProposals, suggestionStates` to the export line.

- [ ] **Step 4: Snapshot reads read_state; rail switcher; rows; header**

(a) `parseSnapshot`: add `readState: null, avatarByEvent: null` to `info`; in the state loop add
```js
      if (e.type === 'com.jkali.read_state' && e.state_key === '' && e.content && typeof e.content === 'object') {
        info.readState = { teammate_read_ts: Number(e.content.teammate_read_ts) || 0, remote_read_ts: Number(e.content.remote_read_ts) || 0 };
      }
```
(b) `buildByUser` convo records: add `mirrorOf: r.mirrorOf || null, unread: (r.readState && r.readState.teammate_read_ts < r.lastTs) ? 1 : 0, readState: r.readState, drafts: []` (the record today has no `mirrorOf`; without it `MS.proposalsByRoom.get(c.mirrorOf)` is always undefined and no draft ever renders). Pending drafts for the manager = this manager's proposals for the room in state pending/seen; compute lazily in render from a per-teammate cache `MS.proposalsByRoom` filled by a new `loadProposalsIndex()` that reads each proposals room's last 100 events once per refresh (`/messages?dir=b&limit=100`, the same call `loadSuggestionOverlay` makes) and stores `roomProposals` per `target_room`. Set `c.drafts = (MS.proposalsByRoom.get(c.mirrorOf) || []).map(p => ({ eventId: p.eventId, body: p.body, ts: p.ts }))` so `pendingDrafts(c.drafts, c.lastTs)` works with the SAME rule as the teammate app.
(c) Rail: in `index.html` after `#nav-primary` add `<nav id="nav-teammates-rail" class="nav-platforms" aria-label="Teammates"></nav>`. In `main.js` add `renderTeammateRail()` called at the end of `refreshAll`: one `button.navitem.nav-icon.teammate-rail-btn` per visible label with a `.avatar.avatar-sm` initials chip and a `.n` pill for that teammate's pending-suggestion count; click → `navTo('teammate:' + label)`; `.active` when `MS.activeView === 'teammate:' + label`.
(d) `buildFeedRow(c)`: same anatomy as Task 3 (stripe from `indicatorsFor({unread: c.unread, draft: pendingDrafts(c.drafts, c.lastTs).length})`, `.avatar` initials + `.avatar-plat` badge, meta title/preview with `Draft:` prefix when the manager's draft is pending, `.side` with when + pills). Keep the `.badge` teammate label when the active view is Recent/Search (drop it in a single teammate's view). Preview for a pending draft: `'My draft · ' + body`.
(e) Clusters: replace `buildProfileGroup` with a collapsed cluster row like Task 3's `buildClusterRow` (caret in `MS.expandedClusters`), plus `buildSubRow` under it when expanded. For a person whose other platform is NOT shared, the cluster's caret text reads `'▸ ' + n + ' of ' + total` where `total` comes from the contacts index (`MS.contacts` handles for that `person_id` + label, deduped by source) — name the hidden platform in a `.preview` suffix `'· <Label> not shared'`, never a body.
(f) Header: in `index.html` `#room-heading` add `<div id="room-read" class="room-sub-row muted"></div>`. In `openRoom` set two lines from `rec.readState`: `remote_read_ts >= rec.lastTs ? 'Other party read the latest message' : ''` and `teammate_read_ts >= rec.lastTs ? teammateLabel + ' has read everything' : teammateLabel + ' has unread messages'`; when `rec.readState` is null, text `'Read state unknown'`.

- [ ] **Step 5: Suggestion stack**

`index.html`: inside `#proposal-pane` before `#proposal-compose` add `<div id="proposal-stack"></div>`. In `main.js` replace `showSuggestion`, `pinSuggestion`, `startSuggestionEdit`, `loadSuggestionOverlay` with:
```js
function renderSuggestionStack() {
  const host = $('proposal-stack');
  const ctx = MS.openProposalCtx;
  if (!host || !ctx) return;
  host.replaceChildren();
  const rec = MS.rooms[ctx.mirrorRoomId];
  const props = MS.proposalsByRoom.get(ctx.targetRoom) || [];
  const msgs = [...MS.roomEvents.values()].map(ev => ({ type: ev.type, ts: mirrorTs(ev), content: ev.content }));
  const states = suggestionStates(props, msgs, rec && rec.readState, Date.now()).reverse(); // oldest at top
  for (const s of states.slice(-6)) {
    const card = el('div', 'sug ' + s.state);
    const cap = el('div', 'sug-cap');
    const label = { sent: '✓ Sent by ' + (ctx.label || 'teammate'), auto: '⚡ Sent as ' + (ctx.label || 'teammate') + ' automatically',
      retired: 'Retired · the thread moved on', seen: 'Seen by ' + (ctx.label || 'teammate') + ' · not sent', pending: 'Pending · not yet seen' }[s.state];
    cap.appendChild(el('span', '', label + ' · ' + shortTime(s.ts)));
    if (s.state === 'pending' || s.state === 'seen') {
      const edit = el('button', 'sug-link', 'Edit'); edit.type = 'button';
      edit.addEventListener('click', () => { const input = $('proposal-input'); if (input) { input.value = s.body; input.focus(); } });
      cap.appendChild(edit);
    }
    card.appendChild(cap);
    card.appendChild(el('div', 'sug-text', sanitize(s.body)));
    host.appendChild(card);
  }
  host.scrollTop = host.scrollHeight;
}
async function loadProposalsIndex() {
  const byRoom = new Map();
  for (const [label, prid] of MS.proposalsByUser) {
    if (!ROOMID_RE.test(prid) || !MS.proposalsRoomSet.has(prid)) continue;
    try {
      const data = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(prid) + '/messages?dir=b&limit=100');
      const chunk = Array.isArray(data.chunk) ? data.chunk : [];
      const targets = new Set(chunk.map(e => e && e.content && e.content.target_room).filter(t => typeof t === 'string'));
      for (const t of targets) byRoom.set(t, roomProposals(chunk, t));
    } catch (e) { /* keep whatever we had for this teammate */ }
  }
  MS.proposalsByRoom = byRoom;
}
```
Add `proposalsByRoom: new Map(), expandedClusters: new Set()` to `MS`. Call `await loadProposalsIndex()` in `refreshAll` before `buildByUser` (so rows can show drafts), and `renderSuggestionStack()` at the end of `openRoom`, in `startTail` after `reconcileNativeEchoes()`, and after a successful `submitProposal` (push the new proposal into `MS.proposalsByRoom` optimistically: `{ body, eventId: result.event_id, ts: content.origin_ts }` unshifted onto the target's list, then render). Remove the `.msg-row.suggested` handling in `renderBubble` and `startTail` (the `suggestion` variable lines). Edit-in-place via dblclick is replaced by the Edit link (fills the bar; Enter sends a NEW suggestion — the old one shows as retired once the new one lands only if the teammate sends; otherwise both stay pending, which is the honest state).

- [ ] **Step 6: Avatars on bubbles**

In `renderBubble`, build the sender avatar for received rows:
```js
  if (!sent) {
    const av = el('span', 'avatar avatar-sm', initials(senderName));
    const mxc = ev.content && ev.content['com.jkali.origin_avatar'];
    if (typeof mxc === 'string' && MXC_RE.test(mxc)) loadAvatarInto(av, mxc);
    meta.insertBefore(av, meta.firstChild);
  }
```
with
```js
const avatarBlobs = new Map();   // master mxc -> object URL (bounded)
async function loadAvatarInto(node, mxc) {
  try {
    let obj = avatarBlobs.get(mxc);
    if (!obj) {
      const url = mxcDownloadUrl(mxc);
      if (!url) return;
      const res = await fetch(url, { headers: S.token ? { Authorization: 'Bearer ' + S.token } : {} });
      if (!res.ok) return;
      obj = URL.createObjectURL(await res.blob());
      if (avatarBlobs.size > 200) avatarBlobs.clear();
      avatarBlobs.set(mxc, obj);
    }
    const img = el('img', 'avatar-img'); img.src = obj; img.alt = '';
    node.replaceChildren(img);
  } catch (e) { /* initials stay */ }
}
```
Also render avatars on list rows: `c.avatarMxc` is not available per room (avatars are per message); keep initials on rows for now.

- [ ] **Step 7: CSS and docs**

`apps/master/style.css` add: `.teammate-rail-btn { position: relative; } .teammate-rail-btn .n { position: absolute; top: 2px; right: 2px; min-width: 14px; height: 14px; border-radius: 7px; background: #7c5cd6; color: #fff; font-size: 9px; display: grid; place-items: center; padding: 0 3px; } .avatar-sm { width: 24px; height: 24px; font-size: 10px; border-radius: 50%; display: inline-grid; place-items: center; background: var(--color-neutral-300); color: var(--color-neutral-700); overflow: hidden; } .avatar-img { width: 100%; height: 100%; object-fit: cover; } #proposal-stack { display: grid; gap: 6px; padding: 8px 16px 0; max-height: 40vh; overflow-y: auto; }`
`shared/style/beepa.css` add the suggestion cards: `.sug { border-radius: 10px; padding: 8px 12px; display: grid; gap: 4px; border: 1px solid var(--color-divider); background: var(--color-surface); } .sug-cap { display: flex; justify-content: space-between; align-items: center; font-size: 11px; color: var(--color-muted); gap: 8px; } .sug-text { font-size: 13px; white-space: pre-wrap; } .sug.pending, .sug.seen { border-color: #7c5cd6; background: #ece6fa; } .sug.pending .sug-cap, .sug.seen .sug-cap { color: #7c5cd6; } .sug.sent, .sug.auto { border-color: transparent; background: var(--color-bubble-sent); } .sug.sent .sug-cap, .sug.auto .sug-cap { color: var(--color-accent-700); } .sug.retired { opacity: .6; } .sug.retired .sug-text { text-decoration: line-through; } .sug-link { border: 0; background: transparent; color: inherit; font: inherit; text-decoration: underline; cursor: pointer; padding: 0; }`
`apps/master/CLAUDE.md`: note the new allowed leaf import (`shared/model/attention.js`, zero `shared/ui` imports — verify with `grep -n "import" shared/model/attention.js` returning nothing), the suggestion stack replacing the single overlay, `com.jkali.read_state` as read-only state, and that `com.jkali.from_proposal` is cosmetic.

- [ ] **Step 7b: Update tests/unit/master_timeline.test.js for the stack**

Add `'proposal-stack'` to the `elements` Map ids on line 37. Replace the block from the comment `// A mirrored outgoing event acknowledges its exact proposal` through the line `ctx.stopTail(); polls().at(-1).resolve({ next_batch: 'done', rooms: {} }); await tick();` (the stale-overlay block) with:

```js
// A mirrored outgoing event acknowledges its EXACT proposal, not another
// proposal containing identical text; the stack shows pending vs sent.
vm.runInContext(`
  MS.proposalsByUser.set('owner', '!proposals:master');
  MS.proposalsRoomSet.add('!proposals:master');
  MS.rooms['!A:master'].mirrorOf = '!local:source';
  MS.proposalsByRoom.set('!local:source', [
    { eventId: '$different-proposal', body: 'hello', ts: 900 },
    { eventId: '$proposal', body: 'hello', ts: 800 },
  ]);
`, ctx);
const stackOpen = ctx.openRoom('!A:master');
history().resolve({ chunk: [msg('$ack', 'hello', { 'com.jkali.auto_sent_from_proposal': '$proposal' })] }); await stackOpen;
const stack = elements.get('proposal-stack');
const cardStates = () => stack.children.map(c => c.className.replace('sug ', ''));
assert.deepEqual(cardStates(), ['auto', 'pending'], 'exact id acknowledged; same text alone is not acknowledgement');
// A later mirrored message retires the still-pending suggestion, never marks it sent.
ctx.renderBubble({ ...msg('$later', 'anything'), origin_server_ts: 5000 });
ctx.renderSuggestionStack();
assert.deepEqual(cardStates(), ['auto', 'retired']);
ctx.stopTail(); polls().at(-1).resolve({ next_batch: 'done', rooms: {} }); await tick();

// The stack renders ONLY the open room's target: switching rooms clears it.
const otherOpen = ctx.openRoom('!B:master'); history().resolve({ chunk: [] }); await otherOpen;
assert.equal(stack.children.length, 0);
assert.equal(ctx.ms.openRoomId, '!B:master');
ctx.stopTail(); polls().at(-1).resolve({ next_batch: 'done', rooms: {} }); await tick();
```
`openRoom` no longer issues a second `/messages` read (the proposals index is loaded in `refreshAll`), so `history()` after an open now refers to the room history request only. Run the file; fix any remaining reference to `showSuggestion`, `pinSuggestion` or `.msg-row.suggested`.

- [ ] **Step 8: Run tests**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/master_suggestions.test.js` → `ok`.
Run: `tests/run.py --unit-only` → green, including `master_timeline.test.js`, `master_share_level.test.js`, `csp_parity.test.js`.
Static send-path assertion (what integration scenario 7 checks): `grep -c "m.room.message" apps/master/main.js` must only match read/parse sites, never a `/send/m.room.message` string: `grep -n "send/m.room.message" apps/master/main.js` → no output.
Browser: open the console, pick a teammate in the rail, open a shared room, type a suggestion + Enter: card appears pending; after the teammate's ghost "Send as me", it flips to sent within one poll.

---

## Self-review

- Spec coverage: F1 (Task 2, 3), F2 (Task 5, Task 6 gate, Task 7 stack), F3 bridge avatars (Task 6 stamp + Task 7 bubbles; teammate-side avatars from member state are NOT in this plan — the teammate app keeps initials; add in the F3 slice), F5 partial (read state + sender identity mirrored, Task 6/7; the shared read-only render layer refactor is deferred — Task 7 duplicates row DOM by hand once more, deliberately, to keep the master's absent-send-code property without a module split in this slice). Clustering in both seats: Tasks 3, 4, 7. Layout decisions table: all rows covered except CRM tags (F4, out of scope by decision).
- Placeholders: none. Each step carries code or an exact edit.
- Type consistency: `rec.drafts` items are `{eventId, body, ts, template?}` in Tasks 1, 3, 5, 7; `draftPending(draftTs, lastActivityTs)` argument order is the same everywhere; `suggestionStates` reads `com.jkali.from_proposal` (Task 5 writes it) and `com.jkali.auto_sent_from_proposal` (existing uplink key). `S.roomProfile` is a `Map` written in Task 4 and read with the Map-or-object accessor in Tasks 1 and 4.
