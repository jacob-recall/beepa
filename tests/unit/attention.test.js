// Plain-node test for shared/model/attention.js — the Triage Rail's pure list
// helpers. Pins the FAIL-CLOSED draft rule (unknown activity => not pending),
// the indicator order, profile-only clustering and the list filter.
//
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
// F7: 'scheduled' is its own indicator, ordered between draft and overdue,
// and never implied by a draft.
assert.deepEqual(indicatorsFor({ unread: 1, draft: 1, scheduled: 2, overdue: 1, reconnect: true }),
  ['unread', 'draft', 'scheduled', 'overdue', 'reconnect']);
assert.deepEqual(indicatorsFor({ scheduled: 1 }), ['scheduled']);
assert.deepEqual(indicatorsFor({ draft: 1 }), ['draft'], 'a draft never implies a schedule');
assert.deepEqual(indicatorsFor({ scheduled: 0 }), []);

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
assert.equal(clusterFeed([{ id: '!wa:l', lastTs: 50, scheduled: 2, drafts: [] },
                          { id: '!ig:l', lastTs: 10, scheduled: 1, drafts: [] }], profile)[0].scheduled,
  3, 'cluster scheduled count sums its members');
assert.equal(items[1].kind, 'single');
// a plain object map works too, and an unknown room stays single
assert.equal(clusterFeed(recs, { '!wa:l': { id: 'p1', displayName: 'M' } }).length, 3);

const flags = (it) => it.kind === 'single' ? indicatorsFor({ unread: it.rec.unread }) : indicatorsFor({ unread: it.unread, draft: it.draft });
const ordered = [{ kind: 'single', rec: { id: 'x', unread: 0, lastTs: 90 } }].concat(items);
assert.deepEqual(applyFilter(ordered, 'all', flags).map(i => i.kind), ['single', 'cluster', 'single']);
assert.deepEqual(applyFilter(ordered, 'needs', flags).map(i => i.kind === 'single' ? i.rec.id : 'cluster'), ['cluster', '!g:l', 'x']);
assert.deepEqual(applyFilter(ordered, 'drafts', flags).map(i => i.kind), ['cluster']);
console.log('attention.test.js: ok');
