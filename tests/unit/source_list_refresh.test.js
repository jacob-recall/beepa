import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = fs.readFileSync(new URL('../../shared/ui/search.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const timers = [];
const headerCounts = [];
let railRefreshes = 0;
const list = {
  children: [],
  replaceChildren(...children) { this.children = children; },
  appendChild(child) { this.children.push(child); },
};
const search = { value: '' };
const state = { activeNavKey: 'source:discord', sourceViewId: 'discord', feedRenderScheduled: false };
const conversations = { discord: [] };
const context = vm.createContext({
  S: state, convosBySource: conversations, feedModel: new Map(),
  SOURCES: [{ id: 'discord', label: 'Discord' }],
  $: id => id === 'list-body' ? list : id === 'source-search' ? search : null,
  sanitizeLine: value => value,
  elEmpty: text => ({ text }),
  buildConvoRow: conversation => ({ id: conversation.id }),
  setTimeout: callback => timers.push(callback),
});
vm.runInContext(source, context);
context.setSourceViewHook(sourceId => headerCounts.push(conversations[sourceId].length));
context.setFeedRenderHook(() => { railRefreshes++; });
context.renderSourceList();
assert.match(list.children[0].text, /No conversations yet/);

conversations.discord = [
  { id: '!dm:localhost', title: 'DM', lastTs: 1 },
  { id: '!group:localhost', title: 'Group DM', lastTs: 2 },
];
context.scheduleFeedRender();
context.scheduleFeedRender();
assert.equal(timers.length, 1, 'import bursts coalesce into one render');
timers.shift()();
assert.deepEqual(list.children.map(row => row.id), ['!group:localhost', '!dm:localhost']);
assert.deepEqual(headerCounts, [0, 2], 'source header follows newly imported conversations');
assert.equal(railRefreshes, 1);

search.value = 'Group';
context.scheduleFeedRender();
timers.shift()();
assert.deepEqual(list.children.map(row => row.id), ['!group:localhost'], 'live refresh preserves search');

for (const destination of ['people', 'settings', 'source:imessage']) {
  context.scheduleFeedRender();
  state.activeNavKey = destination;
  list.replaceChildren({ id: 'other-view' });
  timers.shift()();
  assert.deepEqual(list.children.map(row => row.id), ['other-view'], 'delayed render respects current navigation');
}
console.log('Source list refresh: imported chats, headers, batching, search and navigation guards pass.');
