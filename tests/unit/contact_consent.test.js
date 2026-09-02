// tests/unit/contact_consent.test.js
//
// The contact-share dimension: per-contact override > per-source > global.
// MUST assert the same cases, with the same expected results, as
// tests/unit/contact_consent_py.test.py — if you add one here, add it there.
import {
  resolveContactShare, normalizeContactPolicy,
  normalizeContactOverrides, contactOverrideKey, splitContactOverrideKey,
  CONTACT_OVERRIDES_CAP,
} from '../../shared/model/consent.js';
function eq(a, b, m){ if (JSON.stringify(a)!==JSON.stringify(b)) throw new Error(m+': '+JSON.stringify(a)); }

// default = private
eq(resolveContactShare('imessage', normalizeContactPolicy(undefined)), {shared:false, reason:'private'}, 'default');
// global share-all
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'share-all'})), {shared:true, reason:'all contacts'}, 'global');
// per-source overrides global
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'share-all', sources:{imessage:'private-all'}})), {shared:false, reason:'private'}, 'src-private wins');
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'private', sources:{imessage:'share-all'}})), {shared:true, reason:'all imessage contacts'}, 'src-share wins');
// garbage collapses to safe default
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'yolo', sources:{imessage:'maybe'}})), {shared:false, reason:'private'}, 'garbage safe');
// array-shaped sources (typeof 'object' in JS) must be rejected like no sources,
// not walked as {'0':..,'1':..} — matches Python's isinstance(dict). Parity guard.
eq(normalizeContactPolicy({global:'private', sources:['share-all','private-all']}), {global:'private', sources:{}}, 'array sources -> {}');
eq(normalizeContactPolicy({global:'private', sources:['share-all','private-all']}), normalizeContactPolicy({global:'private'}), 'array sources same as no sources');
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'private', sources:['share-all']})), {shared:false, reason:'private'}, 'array sources resolves like empty -> private');
// PINNED (consent-conformance plan, deny-drop decision): a malformed source id
// drops its private-all rule and falls through to a global share-all. Do NOT
// change one side only — see the conformance harness.
eq(resolveContactShare(5, {global:'share-all', sources:{'5':'private-all'}}),
   {shared:true, reason:'all contacts'},
   'pinned: non-string source drops the private-all rule -> global share-all');

// ---- per-contact overrides (per-contact-share plan, C1) --------------------
const SHARE_ALL = normalizeContactPolicy({ global: 'private', sources: { imessage: 'share-all' } });
const PRIVATE_ALL = normalizeContactPolicy({ global: 'share-all', sources: { imessage: 'private-all' } });

// override WINS over both the per-source and the global level, in both directions
eq(resolveContactShare('imessage', PRIVATE_ALL, 'share'), { shared: true, reason: 'this contact' },
  'override share beats private-all');
eq(resolveContactShare('imessage', SHARE_ALL, 'private'), { shared: false, reason: 'this contact private' },
  'override private beats share-all');
eq(resolveContactShare('imessage', normalizeContactPolicy(undefined), 'share'),
  { shared: true, reason: 'this contact' }, 'override share beats the private default');

// an UNRECOGNIZED / absent override inherits — the contact dimension keeps its
// standing policies (unlike the conversation dimension, where unknown = private)
for (const junk of [undefined, null, '', 'inherit', 'junk', 'Share', 'share-all', 5, true, {}, [], { state: 'share' }]) {
  eq(resolveContactShare('imessage', SHARE_ALL, junk), { shared: true, reason: 'all imessage contacts' },
    'unknown override inherits share-all: ' + JSON.stringify(junk));
  eq(resolveContactShare('imessage', PRIVATE_ALL, junk), { shared: false, reason: 'private' },
    'unknown override inherits private-all: ' + JSON.stringify(junk));
}

// ---- key spec (F5/F6): first-'|' split, SOURCE_KEY_RE prefix --------------
eq(contactOverrideKey('imessage', '+15551234567'), 'imessage|+15551234567', 'key built');
eq(contactOverrideKey('imessage', 'a|b@example.com'), 'imessage|a|b@example.com', "'|' is legal in a network_id");
eq(splitContactOverrideKey('imessage|a|b@example.com'), { source: 'imessage', network_id: 'a|b@example.com' },
  'split once, on the FIRST pipe');
eq(contactOverrideKey('__proto__', 'x'), null, 'prototype-named source rejected');
eq(contactOverrideKey('iMessage', 'x'), null, 'uppercase source rejected');
eq(contactOverrideKey('imessage', ''), null, 'empty network_id rejected');
eq(splitContactOverrideKey('imessage'), null, 'a key with no pipe is invalid');
eq(splitContactOverrideKey('|x'), null, 'an empty source segment is invalid');
eq(splitContactOverrideKey('imessage|'), null, 'an empty network_id segment is invalid');

// ---- normalizeContactOverrides -------------------------------------------
eq(normalizeContactOverrides(undefined), {}, 'absent event -> {}');
eq(normalizeContactOverrides({}), {}, 'no overrides field -> {}');
eq(normalizeContactOverrides({ overrides: { 'imessage|+15551234567': 'share' } }),
  { 'imessage|+15551234567': 'share' }, 'valid entry kept');
// malformed KEYS are dropped (inherit); unknown VALUES are dropped (inherit)
eq(normalizeContactOverrides({ overrides: {
  'imessage|+1': 'share', 'nopipe': 'private', '|x': 'private', 'imessage|': 'private',
  '__proto__|x': 'private', 'imessage|+2': 'junk', 'imessage|+3': 5, 'imessage|+4': 'private',
} }), { 'imessage|+1': 'share', 'imessage|+4': 'private' }, 'malformed keys/values dropped');
// a PRESENT but non-dict overrides field is a READ FAILURE, never {} (F5)
for (const bad of [[], 'share', 5, null, true]) {
  eq(normalizeContactOverrides({ overrides: bad }), null,
    'non-dict overrides field is a read failure: ' + JSON.stringify(bad));
}
// a STORED map over the cap reads as a read failure too
const overCap = {};
for (let i = 0; i <= CONTACT_OVERRIDES_CAP; i++) overCap['imessage|+1' + i] = 'private';
eq(Object.keys(overCap).length > CONTACT_OVERRIDES_CAP, true, 'fixture is over the cap');
eq(normalizeContactOverrides({ overrides: overCap }), null, 'over-cap stored map is a read failure');
const atCap = {};
for (let i = 0; i < CONTACT_OVERRIDES_CAP; i++) atCap['imessage|+1' + i] = 'private';
eq(Object.keys(normalizeContactOverrides({ overrides: atCap })).length, CONTACT_OVERRIDES_CAP,
  'exactly at the cap still reads');
// the output carries no prototype, so no stored key can reach Object.prototype
eq(Object.getPrototypeOf(normalizeContactOverrides({ overrides: {} })), null,
  'output is a null-prototype object');

console.log('ok contact_consent');
