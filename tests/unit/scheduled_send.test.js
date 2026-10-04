// Plain-node test for the teammate's schedule-send layer (roadmap F7):
//
//  - classifySchedules() derives EVERY state from event content only
//    (com.jkali.scheduled_send + the scheduled_cancel STATE event + the
//    daemon's com.jkali.scheduled_outcome records). The same events must
//    classify identically with localStorage empty AND with it poisoned, which
//    is what stops a per-browser cache from hiding a held or sent message.
//  - scheduleRefusal() is the write-time gate, and it refuses exactly what the
//    daemon would refuse later: a '!' body (bridge-command injection), an
//    over-4000 body is clamped, an unattributed/management/unjoined target, and
//    a time outside now+1min .. now+30d.
//  - proposalSendAt() is the manager's 24h horizon on the same single write.
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/scheduled_send.test.js
import assert from 'node:assert/strict';

// classifySchedules / scheduleRefusal are pure, but apps/user/scheduled.js
// imports the app's DOM modules at load time (as apps/user/proposals.js already
// does for ghost_drafts.test.js), so a minimal global shim keeps this a plain
// node run with no browser.
globalThis.localStorage = {
  _v: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._v, k) ? this._v[k] : null; },
  setItem(k, v) { this._v[k] = String(v); },
  removeItem(k) { delete this._v[k]; },
};

const { classifySchedules, scheduleRefusal, MIN_LEAD_MS, MAX_LEAD_MS, SKEW_MAX_MS } =
  await import('../../apps/user/scheduled.js');
const { proposalSendAt } = await import('../../apps/master/main.js');
const { parseProposal } = await import('../../apps/user/proposals.js');

const ROOM = '!conv:localhost';
const OTHER = '!other:localhost';
const ROOMID_RE = /^![A-Za-z0-9._=/+-]+:localhost$/;
const NOW = 1_800_000_000_000;

const send = (id, over = {}) => Object.assign({
  type: 'com.jkali.scheduled_send', event_id: id, origin_server_ts: NOW - 1000,
  content: { target_room: ROOM, body: 'see you at nine', send_at: NOW + 3600000 },
}, over);
const cancel = (id, ts = NOW) => ({ type: 'com.jkali.scheduled_cancel', state_key: id, content: { ts } });
const outcome = (id, state, over = {}) => ({
  type: 'com.jkali.scheduled_outcome', event_id: '$o' + id,
  content: Object.assign({ scheduled_event_id: id, target_room: ROOM, state,
    reason: '', send_at: NOW + 3600000, outcome_ts: NOW + 3600000 }, over),
});

// ---- classification is content-only ----------------------------------------
const events = [
  send('$a'),
  send('$b'), outcome('$b', 'held', { reason: 'superseded' }),
  send('$c'), outcome('$c', 'sent', { sent_event_id: '$sent' }),
  send('$d'), cancel('$d'),
  send('$e'), outcome('$e', 'refused', { reason: 'target' }),
];
const byId = (list) => Object.fromEntries(list.map(s => [s.eventId, s]));

const clean = byId(classifySchedules(events));
assert.equal(clean['$a'].state, 'scheduled');
assert.equal(clean['$b'].state, 'held');
assert.equal(clean['$b'].reason, 'superseded');
assert.equal(clean['$c'].state, 'sent');
assert.equal(clean['$d'].state, 'cancelled');
assert.equal(clean['$e'].state, 'refused');
assert.equal(clean['$a'].body, 'see you at nine', 'the body is carried for re-send/display');

// Poisoning the per-viewer cache changes nothing: nothing here reads it.
localStorage.setItem('com.jkali.proposals_handled', JSON.stringify(['$a', '$b', '$c']));
localStorage.setItem('beepa_scheduled', JSON.stringify({ $a: 'sent', $b: 'scheduled' }));
assert.deepEqual(byId(classifySchedules(events)), clean, 'localStorage never changes classification');

// The newest outcome wins, and an outcome beats a cancel the daemon resolved.
const racy = [send('$f'), cancel('$f'),
  outcome('$f', 'held', { outcome_ts: NOW + 10 }), outcome('$f', 'sent', { outcome_ts: NOW + 20 })];
assert.equal(byId(classifySchedules(racy))['$f'].state, 'sent');

// Malformed events are dropped, not guessed at.
for (const bad of [
  send('$x', { content: { target_room: ROOM, body: '  ', send_at: NOW + 1000 } }),
  send('$x', { content: { target_room: ROOM, body: 'hi', send_at: '9am' } }),
  send('$x', { content: { target_room: ROOM, body: 'hi', send_at: 1.5 } }),
  send('$x', { content: { body: 'hi', send_at: NOW + 1000 } }),
]) assert.deepEqual(classifySchedules([bad]), [], 'malformed scheduled_send dropped');
assert.deepEqual(classifySchedules([send('$g'), outcome('$g', 'invented')])[0].state, 'scheduled',
  'an unknown outcome state is ignored, not trusted');
assert.deepEqual(classifySchedules(null), []);

// Ordering is by fire time, so the cluster reads as a queue.
const ordered = classifySchedules([
  send('$late', { content: { target_room: ROOM, body: 'b', send_at: NOW + 7200000 } }),
  send('$soon', { content: { target_room: ROOM, body: 'a', send_at: NOW + 60000 } }),
]);
assert.deepEqual(ordered.map(s => s.eventId), ['$soon', '$late']);

// ---- write-time refusals ----------------------------------------------------
const rec = { id: ROOM, sourceId: 'whatsapp' };
const joined = new Set([ROOM, OTHER]);
const mgmt = new Set([OTHER]);
const base = { roomId: ROOM, body: 'hello', sendAt: NOW + 3600000, now: NOW,
  serverNow: NOW, record: rec, joined, mgmt, roomIdRe: ROOMID_RE };
const refuse = (over) => scheduleRefusal(Object.assign({}, base, over));

assert.equal(refuse({}), null, 'the happy path is accepted');
assert.match(refuse({ body: '!wa help' }), /cannot start with/, 'a bridge command is refused');
assert.match(refuse({ body: '   \n  !wa help' }), /cannot start with/, 'whitespace does not hide it');
assert.match(refuse({ body: '​!wa help' }), /cannot start with/, 'a zero-width char does not hide it');
assert.equal(refuse({ body: 'hi! there' }), null, "a '!' that is not first is fine");
assert.match(refuse({ body: '   ' }), /Type a message/);
// sanitize()'s 4000 clamp applies to what is written, and a body that is ONLY
// padding past the clamp still has content, so it is accepted (clamped), while
// a body that sanitizes away entirely is refused.
assert.equal(refuse({ body: 'x'.repeat(5000) }), null, 'an over-long body is clamped, not refused');
assert.match(refuse({ body: '‮​' }), /Type a message/, 'a body that sanitizes to nothing is refused');

assert.match(refuse({ record: null }), /messaging conversation/, 'an unknown room is refused');
assert.match(refuse({ record: { id: ROOM } }), /messaging conversation/,
  'a room with no source attribution is refused');
assert.match(refuse({ roomId: OTHER, record: { id: OTHER, sourceId: 'whatsapp' } }),
  /Cannot schedule a message here/, 'a bridge management room is refused');
assert.match(refuse({ roomId: '!nope:other' }), /not available/, 'a foreign room id is refused');
assert.match(refuse({ roomId: 'garbage' }), /not available/);
assert.match(refuse({ joined: new Set() }), /not available/, 'an unjoined room is refused');

assert.match(refuse({ sendAt: NOW + 1000 }), /at least a minute/);
assert.match(refuse({ sendAt: NOW - 60000 }), /at least a minute/);
assert.match(refuse({ sendAt: NOW + MAX_LEAD_MS + 1000 }), /within the next 30 days/);
assert.match(refuse({ sendAt: NaN }), /valid time/);
assert.match(refuse({ sendAt: '9am' }), /valid time/);
assert.match(refuse({ sendAt: NOW + 3600000.5 }), /valid time/);
assert.equal(refuse({ sendAt: NOW + MIN_LEAD_MS }), null, 'exactly the minimum lead is allowed');

// Clock skew is a UX guard, and an unknown server clock never blocks.
assert.match(refuse({ serverNow: NOW + SKEW_MAX_MS + 1000 }), /clock/);
assert.match(refuse({ serverNow: NOW - SKEW_MAX_MS - 1000 }), /clock/);
assert.equal(refuse({ serverNow: NOW + SKEW_MAX_MS - 1 }), null);
assert.equal(refuse({ serverNow: null }), null, 'an unreadable server clock does not block');

// ---- the manager's 24h horizon on the same single write ----------------------
const local = (ms) => {
  const d = new Date(ms); const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
};
assert.equal(proposalSendAt('', NOW), null, 'no time means send it now');
assert.equal(proposalSendAt(null, NOW), null);
assert.equal(typeof proposalSendAt(local(NOW + 3600000), NOW), 'number');
assert.match(proposalSendAt(local(NOW + 25 * 3600000), NOW), /within the next 24 hours/);
assert.match(proposalSendAt(local(NOW - 3600000), NOW), /at least a minute/);
assert.match(proposalSendAt('not-a-date', NOW), /valid time/);

// A manager-timed proposal reaches the teammate carrying its time.
const timed = parseProposal({ type: 'com.jkali.proposal', event_id: '$p', origin_server_ts: NOW,
  content: { target_room: ROOM, body: 'later', origin_ts: NOW, send_at: NOW + 3600000 } });
assert.equal(timed.sendAt, NOW + 3600000);
const untimed = parseProposal({ type: 'com.jkali.proposal', event_id: '$p2', origin_server_ts: NOW,
  content: { target_room: ROOM, body: 'now', origin_ts: NOW, send_at: 'soon' } });
assert.equal(untimed.sendAt, 0, 'a malformed send_at is simply absent');

console.log('scheduled_send.test.js: ok');
