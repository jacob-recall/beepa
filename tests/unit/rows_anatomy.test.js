// Plain-node test for the Triage Rail row anatomy in shared/ui/rows.js:
// indicator stripe, initials avatar with platform badge, "Draft:" preview,
// count pills, and the clustered person row. Uses a recording DOM double.
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/rows_anatomy.test.js
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { initials, indicatorsFor, pendingDrafts, clusterFeed, applyFilter } from '../../shared/model/attention.js';

function node(tag, cls) {
  const n = { tag, className: cls || '', children: [], dataset: {}, textContent: '', attrs: {},
    appendChild(c) { this.children.push(c); return c; }, setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {}, classList: { toggle() {}, add(c) { n.className += ' ' + c; } } };
  n.tabIndex = 0;
  return n;
}
const src = fs.readFileSync(new URL('../../shared/ui/rows.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const ctx = vm.createContext({
  el: (tag, cls, text) => { const n = node(tag, cls); if (text !== undefined) n.textContent = text; return n; },
  $: () => null, sanitizeLine: s => s, feedRelTime: () => '2m', openConvo() {}, scheduleFeedRender() {},
  SOURCES: [{ id: 'whatsapp', icon: 'W', label: 'WhatsApp' }, { id: 'instagram', icon: 'I', label: 'Instagram' }],
  feedModel: new Map(), runtime: { whatsapp: { needsReconnect: false } }, S: { expandedClusters: new Set() },
  initials, indicatorsFor, pendingDrafts, clusterFeed, applyFilter,
});
vm.runInContext(src, ctx);

const rec = { id: '!a:l', name: 'Maya Rodriguez', lastBody: 'hi', lastTs: 50, unread: 2, sourceId: 'whatsapp', drafts: [{ ts: 60, body: 'Totally, Thursday works' }] };
assert.deepEqual([...ctx.rowFlags(rec)], ['unread', 'draft']);
const row = ctx.buildFeedRow(rec);
const stripe = row.children.find(c => c.className === 'stripe');
assert.deepEqual(stripe.children.map(c => c.className), ['seg unread', 'seg draft']);
const avatar = row.children.find(c => c.className.startsWith('avatar'));
assert.equal(avatar.textContent, 'MR');
assert.equal(avatar.children.filter(c => c.className.includes('plat-badge')).length, 1);
const meta = row.children.find(c => c.className === 'meta');
assert.equal(meta.children[1].textContent, 'Suggested: Totally, Thursday works');
const side = row.children.find(c => c.className === 'side');
assert.ok(side.children.some(c => c.className === 'pill unread' && c.textContent === '2'));
assert.ok(side.children.some(c => c.className === 'pill draft' && c.textContent === '1'));
assert.equal(row.dataset.roomId, '!a:l');

// retired draft (activity after it) => no draft flag, normal preview
const quiet = Object.assign({}, rec, { lastTs: 70 });
assert.deepEqual([...ctx.rowFlags(quiet)], ['unread']);
assert.equal(ctx.buildFeedRow(quiet).children.find(c => c.className === 'meta').children[1].textContent, 'hi');

// cluster row: two badges, caret, summed unread, opens the newest member
const item = clusterFeed([rec, { id: '!b:l', name: 'Maya (IG)', lastTs: 10, unread: 1, sourceId: 'instagram', drafts: [] }],
  new Map([['!a:l', { id: 'p', displayName: 'Maya Rodriguez' }], ['!b:l', { id: 'p', displayName: 'Maya Rodriguez' }]]))[0];
const crow = ctx.buildClusterRow(item);
assert.equal(crow.dataset.roomId, '!a:l');
assert.equal(crow.children.find(c => c.className.startsWith('avatar')).children.length, 2);
assert.ok(crow.children.find(c => c.className === 'meta').children[0].children.some(c => c.className === 'cluster-caret'));
assert.ok(crow.children.find(c => c.className === 'side').children.some(c => c.className === 'pill unread' && c.textContent === '3'));
const sub = ctx.buildSubRow(item.members[1]);
assert.equal(sub.children.find(c => c.className === 'meta').children[0].textContent, 'Instagram');
console.log('rows_anatomy.test.js: ok');
