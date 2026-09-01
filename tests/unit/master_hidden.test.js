// Plain-node test for apps/master/hidden.js — no framework.
// Run: node tests/unit/master_hidden.test.js  (or via docker, see tests/run.sh)
//
// The hidden set is convenience UI state only (which teammate labels this
// manager's browser should omit from lists). It is never an authorization
// decision — the uplink still mirrors, the account still exists.

import {
  parseHidden, dumpHidden, hide, unhide, visibleFeed, visibleContacts, visibleUsers,
} from '../../apps/master/hidden.js';

let pass = 0;
let fail = 0;
const failures = [];

function eq(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    pass++;
  } else {
    fail++;
    failures.push(`${label}: expected ${e}, got ${a}`);
  }
}

function setEq(actual, expectedArr, label) {
  eq([...actual].sort(), expectedArr.slice().sort(), label);
}

// parseHidden: only a JSON array of non-empty strings becomes a Set.
setEq(parseHidden('["alice","bob"]'), ['alice', 'bob'], 'parseHidden: two labels');
setEq(parseHidden('["alice","alice"]'), ['alice'], 'parseHidden: duplicates collapse');
setEq(parseHidden('["alice","",42,null,{"x":1},"bob"]'), ['alice', 'bob'],
  'parseHidden: drops empty and non-strings');
setEq(parseHidden(null), [], 'parseHidden: null');
setEq(parseHidden(undefined), [], 'parseHidden: undefined');
setEq(parseHidden('not-json'), [], 'parseHidden: garbage');
setEq(parseHidden('{"alice":true}'), [], 'parseHidden: object is not an array');
setEq(parseHidden('"alice"'), [], 'parseHidden: a string is not an array');

// dumpHidden round-trips through parseHidden.
{
  const dumped = dumpHidden(new Set(['bob', 'alice']));
  setEq(parseHidden(dumped), ['alice', 'bob'], 'dumpHidden/parseHidden round-trip');
}

// hide / unhide return new Sets; ignore empty/non-string labels.
{
  const empty = new Set();
  setEq(hide(empty, 'trialtop'), ['trialtop'], 'hide: adds a label');
  eq(empty.size, 0, 'hide: does not mutate the input');
  setEq(hide(empty, ''), [], 'hide: empty string is ignored');
  setEq(hide(empty, null), [], 'hide: null is ignored');
  setEq(hide(empty, 7), [], 'hide: non-string is ignored');

  const one = new Set(['trialtop']);
  setEq(unhide(one, 'trialtop'), [], 'unhide: removes a label');
  eq(one.size, 1, 'unhide: does not mutate the input');
  setEq(unhide(one, 'nobody'), ['trialtop'], 'unhide: missing label is a no-op');
}

// visible* filters omit hidden teammate labels, leave everything else.
{
  const hidden = new Set(['trialtop']);
  const feed = [
    { title: 'A', userLabel: 'jkali' },
    { title: 'B', userLabel: 'trialtop' },
    { title: 'C', userLabel: 'jkali' },
  ];
  eq(visibleFeed(feed, hidden).map(r => r.title), ['A', 'C'],
    'visibleFeed: drops hidden teammate rows');

  const contacts = [
    { network_id: '+1', label: 'jkali' },
    { network_id: '+2', label: 'trialtop' },
  ];
  eq(visibleContacts(contacts, hidden).map(c => c.network_id), ['+1'],
    'visibleContacts: drops hidden teammate handles');

  const byUser = new Map([
    ['jkali', [{ title: 'A' }]],
    ['trialtop', [{ title: 'B' }]],
  ]);
  eq(visibleUsers(byUser, hidden).map(([label]) => label), ['jkali'],
    'visibleUsers: drops hidden labels from the teammate list');
}

console.log(`\n${pass} passed, ${fail} failed`);
if (fail > 0) {
  console.error('\nFailures:');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
}
