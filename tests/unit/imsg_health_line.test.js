// Plain-node test for imsgHealthLine (shared/ui/connections.js): the daemon's
// "Delivery journal: {json}" status line becomes one readable counts-only line.
// Run: node tests/unit/imsg_health_line.test.js
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const src = fs.readFileSync(new URL('../../shared/ui/connections.js', import.meta.url), 'utf8')
  .replace(/^import[\s\S]*?;\n/gm, '').replace(/^export \{[\s\S]*?\};/m, '');
const ctx = vm.createContext({ setTimeout, JSON, Math, Date, sanitizeLine: s => s });
vm.runInContext(src, ctx);
const now = 1_000_000 * 1000;
const line = 'Delivery journal: ' + JSON.stringify({ inbound: {}, outbound: { confirmed: 7, ignored: 147, refused: 2 }, last_outbound_ts: 1_000_000 - 90, last_inbound_ts: 1_000_000 - 7200, chats_mapped: 6 });
assert.equal(ctx.imsgHealthLine(line, now), 'Sends: 7 confirmed · 2 refused · last send 2m ago · last inbound 2h ago · 6 chats');
assert.equal(ctx.imsgHealthLine('Delivery journal: not json', now), null);
assert.equal(ctx.imsgHealthLine('[ok] Full Disk Access', now), null);
assert.equal(ctx.imsgHealthLine('Delivery journal: {"outbound":{}}', now), 'Sends: 0 confirmed · last send never · last inbound never');
// room-state form with freshness
const st = ctx.imsgHealthFromState({ outbound: { confirmed: 7 }, last_outbound_ts: 1_000_000 - 90, chats_mapped: 6, updated_at: 1_000_000 - 12, poll_ok: true }, now);
assert.equal(st.fresh, true);
assert.equal(st.text, 'Daemon reporting (12s ago) · Sends: 7 confirmed · last send 2m ago · last inbound never · 6 chats');
const stale = ctx.imsgHealthFromState({ outbound: {}, updated_at: 1_000_000 - 7200, poll_ok: false, poll_error: 'TimeoutExpired' }, now);
assert.equal(stale.fresh, false);
assert.ok(stale.text.startsWith('Daemon NOT reporting for 2h') && stale.text.includes('last Messages poll failed (TimeoutExpired)'));
assert.equal(ctx.imsgHealthFromState('nope', now), null);
console.log('imsg_health_line.test.js: ok');
