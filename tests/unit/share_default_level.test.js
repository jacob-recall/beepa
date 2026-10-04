// Plain-node test for apps/user/consent.js's PURE account-default helpers
// (roadmap §6): the wording that tells a teammate WHY a conversation is
// shared, and the "set everything to Direct" confirm text.
//
// These are leaves on purpose: the copy is the consent surface. "Private
// (default)" used to mean "you have not set this conversation" under a model
// where that always meant private; under an account default it does not, and a
// label that still said so would be a consent lie.
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
//        node tests/unit/share_default_level.test.js

import {
  reasonText, byDefault, defaultLevelHint, allDirectConfirmText, planAllDirect,
} from '../../apps/user/consent.js';
import { resolve, resolvedLevel } from '../../shared/model/consent.js';

let pass = 0;
let fail = 0;
const failures = [];
function ok(cond, label) { if (cond) { pass++; } else { fail++; failures.push(label); } }

// ---- byDefault is driven by the RESOLVER'S REASON --------------------------
const DEF = (lv) => ({ global: 'private', sources: {}, default_level: lv });
ok(byDefault(resolve({ id: '!a:l' }, DEF('direct'), undefined)) === true,
  'byDefault: unset under a direct default');
ok(byDefault(resolve({ id: '!a:l' }, DEF('share'), undefined)) === true,
  'byDefault: unset under a share default');
ok(byDefault(resolve({ id: '!a:l' }, {}, undefined)) === true,
  'byDefault: unset with no default at all is still "not set individually"');
ok(byDefault(resolve({ id: '!a:l' }, DEF('direct'), 'private')) === false,
  'byDefault: an EXPLICIT private is not a default');
ok(byDefault(resolve({ id: '!a:l' }, DEF('direct'), 'share')) === false,
  'byDefault: an explicit share is not a default');
ok(byDefault(resolve({ id: '!a:l' }, {}, 'direct')) === false,
  'byDefault: an explicit direct is not a default');
ok(byDefault(undefined) === false && byDefault({}) === false,
  'byDefault: a missing/garbage result is not "by default"');

// ---- reasonText names the default WITHOUT implying a deliberate choice -----
const text = (policy, override) => reasonText(resolve({ id: '!a:l' }, policy, override));
ok(text(DEF('direct'), undefined) === 'Direct by default — sent automatically',
  'reasonText: unset under a direct default');
ok(text(DEF('share'), undefined) === 'Shared by default', 'reasonText: unset under a share default');
ok(text({}, undefined) === 'Private by default', 'reasonText: unset with no default');
ok(text({}, 'private') === 'Private', 'reasonText: an explicit private says just "Private"');
ok(text({}, 'share') === 'Shared', 'reasonText: an explicit share');
ok(text({}, 'direct') === 'Direct — sent automatically', 'reasonText: an explicit direct');
// The load-bearing distinction: a Direct-by-default conversation must not read
// the same as one the teammate chose, and neither may read as private.
ok(text(DEF('direct'), undefined) !== text({}, 'direct'),
  'reasonText: Direct-by-default is worded differently from a chosen Direct');
ok(!text(DEF('direct'), undefined).startsWith('Private'),
  'reasonText: a direct default NEVER reads as private');
ok(!text(DEF('share'), undefined).startsWith('Private'),
  'reasonText: a share default NEVER reads as private');

// ---- defaultLevelHint: the control's own one-liner -------------------------
ok(/send as you/.test(defaultLevelHint('direct')),
  'defaultLevelHint(direct) states the manager can send as you');
ok(/haven’t set individually/.test(defaultLevelHint('direct'))
  && /haven’t set individually/.test(defaultLevelHint('share')),
  'defaultLevelHint says "haven’t set individually", not "new conversations"');
ok(!/new conversation/i.test(defaultLevelHint('direct') + defaultLevelHint('share')
  + defaultLevelHint('private')),
  'defaultLevelHint never says "new conversations" (it applies to existing ones too)');
ok(defaultLevelHint('junk') === defaultLevelHint('private'),
  'defaultLevelHint: an unrecognized level reads as private');

// ---- allDirectConfirmText: count + per-source + risk + explicitness --------
const breakdown = {
  bySource: [{ label: 'iMessage', convos: [{ id: '!a:l', title: 'Ann' }, { id: '!b:l', title: 'Bob' }] },
    { label: 'WhatsApp', convos: [{ id: '!c:l', title: 'Cy' }] }],
  all: [{ id: '!a:l', title: 'Ann' }, { id: '!b:l', title: 'Bob' }, { id: '!c:l', title: 'Cy' }],
  plan: { level: 'direct', ids: ['!a:l', '!b:l', '!c:l'], overwritesPrivate: ['!c:l'],
    requiresRiskConfirm: true },
};
const confirm = allDirectConfirmText(breakdown);
ok(/all 3 conversation\(s\)/.test(confirm), 'confirm: states the total count');
ok(/iMessage: 2/.test(confirm) && /WhatsApp: 1/.test(confirm),
  'confirm: per-source breakdown');
ok(/sent as you, without your review/.test(confirm),
  'confirm: carries the verbatim Direct risk copy');
ok(/recipients will not be able to tell the difference/i.test(confirm),
  'confirm: carries the recipients-cannot-tell sentence');
ok(/EXPLICIT Direct setting/.test(confirm) && /will NOT\s+un-Direct them/.test(confirm),
  'confirm: says this writes EXPLICIT overrides a default change will not undo');
ok(/1 of them are currently set to Private/.test(confirm),
  'confirm: names the explicit-private overwrites');
for (const name of ['Ann', 'Bob', 'Cy']) {
  ok(confirm.includes('• ' + name), 'confirm: enumerates ' + name + ' by name');
}
ok(/Type DIRECT to confirm/.test(confirm), 'confirm: asks for the typed word DIRECT');

// ---- planAllDirect dedupes a room that appears under two sources -----------
{
  const p = planAllDirect();
  const ids = p.plan ? p.plan.ids : [];
  ok(new Set(ids).size === ids.length, 'planAllDirect: never lists a room twice');
}

// ---- the resolver the copy is derived from, once more at this layer -------
ok(resolvedLevel(undefined, DEF('direct')) === 'direct'
  && resolvedLevel('private', DEF('direct')) === 'private',
  'resolvedLevel: default applies only without an explicit level');

if (fail) {
  console.error('share_default_level.test.js: ' + fail + ' FAILED, ' + pass + ' passed');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
} else {
  console.log('share_default_level.test.js: all ' + pass + ' checks passed');
}
