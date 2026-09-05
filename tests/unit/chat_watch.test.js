import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = fs.readFileSync(new URL('../../shared/ui/chat.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const requests = [], rendered = [];
const S = { token: 'first-session', joinedSet: new Set(['!A:local', '!B:local']) };
const ctx = vm.createContext({ S, ROOMID_RE: /^!/, convoSeen: new Set(), feedModel: new Map(), runtime: {},
  $: () => null, sanitizeLine: s => s, setActiveNav() {}, showSection() {}, setDetailMode() {},
  setActiveConvoRow() {}, renderMessageEvent: e => rendered.push(e.event_id), convoResolveContent: () => true,
  setTimeout, api: (_method, path) => new Promise((resolve, reject) => requests.push({path, resolve, reject})) });
vm.runInContext(source, ctx);
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };
const history = id => requests.find(r => r.path.includes('/rooms/' + encodeURIComponent(id) + '/messages'));
const polls = () => requests.filter(r => r.path.includes('/sync?'));

const a = ctx.openConvo('!A:local');
const b = ctx.openConvo('!B:local');
history('!A:local').resolve({chunk: [{event_id: '$staleA'}]});
await a;
assert.equal(polls().length, 0, 'obsolete open must not start or reserve a watch');
history('!B:local').resolve({chunk: [{event_id: '$B'}]});
await b;
assert.equal(polls().length, 1, 'selected conversation gets a watch');
assert.deepEqual(rendered, ['$B']);
const oldPoll = polls()[0];
ctx.stopConvoWatch();
S.token = 'second-session';
requests.length = 0;
const again = ctx.openConvo('!B:local');
assert.deepEqual(rendered, ['$B'], 'cache from a previous session must not be rendered');
history('!B:local').resolve({chunk: []});
await again;
oldPoll.resolve({next_batch: 'wrong-session-cursor', rooms: {join: {'!B:local': {timeline: {events: [{event_id: '$old'}]}}}}});
await tick();
assert.equal(S.convoSince, null, 'obsolete poll cannot replace current cursor');
assert.deepEqual(rendered, ['$B']);
assert.equal(S.convoRunning, true, 'obsolete completion cannot stop current watch');
ctx.stopConvoWatch();
polls()[0].resolve({next_batch: 'stopped', rooms: {}});
await tick();
assert.equal(S.convoRunning, false);
console.log('chat_watch: stale open, stale poll, session cache and watcher ownership pass');
