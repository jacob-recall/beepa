// Plain-node test for shared/model/consent.js — no framework.
// Run: node tests/unit/consent.test.js  (or via docker, see tests/run.sh)
// Exits 0 on all-pass, nonzero (via process.exitCode) on any failure.
//
// THE EXPLICIT THREE-LEVEL MODEL (direct-share-level plan, D1). A conversation
// mirrors on its own per-conversation level and nothing else:
//   'share' -> shared, 'direct' -> shared, 'private' -> not shared,
//   ABSENT OR ANY UNRECOGNIZED VALUE -> not shared.
// The contact profile / per-source policy / global policy inputs are accepted
// by resolve() and deliberately IGNORED; every case below that passes a
// share-all policy or a shared profile alongside an unset override is there to
// prove exactly that.
//
// tests/unit/consent_py.test.py mirrors this file case-for-case against
// agents/uplink/consent.py — the resolver the uplink actually enforces with.
// Add a case to one, add it to the other in the same change.

import {
  resolve, effectiveShared, effectiveLevel, resolvedLevel, policyDefaultLevel,
  resolveAll, normalizePolicy, normalizeOverride, overridesFromSync,
  CONSENT_MODEL_EXPLICIT, CONSENT_MODEL_PER_ROOM,
} from '../../shared/model/consent.js';

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

function convo(sourceId, sourceLabel) {
  return { id: '!' + sourceId + ':local', sourceId, sourceLabel: sourceLabel || sourceId };
}

// Every "loud" standing-policy input the old model would have shared on. Each
// is passed alongside an unset/unknown override below; none may share anything.
const LOUD_POLICY = { global: 'share-all', sources: { imessage: 'share-all' } };
const LOUD_PROFILE = { displayName: 'Dana Lewis', share: 'share' };
const DENY_POLICY = { global: 'private', sources: { imessage: 'private-all' } };
const DENY_PROFILE = { displayName: 'Dana Lewis', share: 'private' };

// ---------------------------------------------------------------------------
// 1. Absent override -> private, whatever any other level says (NO INHERITANCE)
// ---------------------------------------------------------------------------
{
  const c = convo('imessage', 'iMessage');
  eq(resolve(c, {}, undefined), { shared: false, reason: 'private' }, 'unset: no policy at all');
  eq(resolve(c, { global: 'private', sources: {} }, undefined),
    { shared: false, reason: 'private' }, 'unset: explicit global private');
  eq(resolve(c, { global: 'share-all', sources: {} }, undefined),
    { shared: false, reason: 'private' }, 'unset: global share-all does NOT share');
  eq(resolve(c, { global: 'private', sources: { imessage: 'share-all' } }, undefined),
    { shared: false, reason: 'private' }, 'unset: per-source share-all does NOT share');
  eq(resolve(c, LOUD_POLICY, undefined, LOUD_PROFILE),
    { shared: false, reason: 'private' }, 'unset: shared profile + both share-all levels do NOT share');
  eq(resolve(c, LOUD_POLICY, null, LOUD_PROFILE),
    { shared: false, reason: 'private' }, 'null override: still private under every share-all');
  eq(effectiveShared(c, LOUD_POLICY, undefined, LOUD_PROFILE), false,
    'unset: effectiveShared false under every share-all');
}

// ---------------------------------------------------------------------------
// 2. 'share' -> shared/explicit, whatever any other level says
// ---------------------------------------------------------------------------
{
  const c = convo('imessage', 'iMessage');
  eq(resolve(c, {}, 'share'), { shared: true, reason: 'explicit' }, 'share: empty policy');
  eq(resolve(c, DENY_POLICY, 'share'), { shared: true, reason: 'explicit' },
    'share: per-source private-all cannot exclude an explicit share');
  eq(resolve(c, DENY_POLICY, 'share', DENY_PROFILE), { shared: true, reason: 'explicit' },
    'share: a private profile cannot exclude an explicit share');
  eq(resolve(c, {}, { state: 'share' }), { shared: true, reason: 'explicit' },
    'share: object form { state } is accepted');
  eq(effectiveShared(c, DENY_POLICY, 'share', DENY_PROFILE), true, 'share: effectiveShared true');
}

// ---------------------------------------------------------------------------
// 3. 'direct' -> shared, reason 'direct' (the NEW level; still just "mirrored"
//    as far as this resolver is concerned — auto-send is a separate gate)
// ---------------------------------------------------------------------------
{
  const c = convo('imessage', 'iMessage');
  eq(resolve(c, {}, 'direct'), { shared: true, reason: 'direct' }, 'direct: empty policy');
  eq(resolve(c, DENY_POLICY, 'direct', DENY_PROFILE), { shared: true, reason: 'direct' },
    'direct: not excludable by policy or profile');
  eq(resolve(c, {}, { state: 'direct' }), { shared: true, reason: 'direct' },
    'direct: object form { state } is accepted');
  eq(effectiveShared(c, {}, 'direct'), true, 'direct: effectiveShared true');
}

// ---------------------------------------------------------------------------
// 4. 'private' -> not shared/excluded, whatever any other level says
// ---------------------------------------------------------------------------
{
  const c = convo('imessage', 'iMessage');
  eq(resolve(c, {}, 'private'), { shared: false, reason: 'excluded' }, 'private: empty policy');
  eq(resolve(c, LOUD_POLICY, 'private', LOUD_PROFILE), { shared: false, reason: 'excluded' },
    'private: beats shared profile and both share-all levels');
  eq(resolve(c, {}, { state: 'private' }), { shared: false, reason: 'excluded' },
    'private: object form { state } is accepted');
  eq(effectiveShared(c, LOUD_POLICY, 'private', LOUD_PROFILE), false, 'private: effectiveShared false');
}

// ---------------------------------------------------------------------------
// 5. THE UNKNOWN-VALUE INVARIANT (F8): absent or ANY unrecognized value is
//    private — even under the loudest possible share-all policy + a shared
//    profile. This class has its own generator in the conformance harness.
// ---------------------------------------------------------------------------
{
  const UNKNOWN = [
    undefined, null, '', 'inherit', 'junk', 'shared', 'Share', 'SHARE', 'share ', ' share',
    'Direct', 'DIRECT', 'direct ', 'share-all', 'private-all', 'auto', '__proto__',
    'constructor', 0, 1, 5, true, false, [], {}, ['share'], ['direct'],
    { state: 'inherit' }, { state: 'junk' }, { state: null }, { state: 5 }, { state: ['share'] },
    { State: 'share' }, { level: 'share' }, { state: 'share-all' },
    // NFKC lookalikes, a zero-width space, a NUL suffix, a NBSP: exact-match
    // canaries against a future trim()/normalize() on one side only.
    '\uff53hare', 'sh\u200bare', 'share\u0000', 'direct\u0000', 'private\u00a0',
  ];
  for (const v of UNKNOWN) {
    const label = 'unknown-override ' + JSON.stringify(v === undefined ? '<undefined>' : v);
    eq(resolve(convo('imessage', 'iMessage'), LOUD_POLICY, v, LOUD_PROFILE),
      { shared: false, reason: 'private' }, label + ': private under every share-all');
    eq(effectiveLevel(v), 'private', label + ': effectiveLevel private');
    eq(effectiveShared(convo('imessage'), LOUD_POLICY, v, LOUD_PROFILE), false,
      label + ': effectiveShared false');
  }
}

// ---------------------------------------------------------------------------
// 6. effectiveLevel: the three levels, from both storage forms
// ---------------------------------------------------------------------------
{
  eq(effectiveLevel('share'), 'share', 'effectiveLevel: share');
  eq(effectiveLevel('direct'), 'direct', 'effectiveLevel: direct');
  eq(effectiveLevel('private'), 'private', 'effectiveLevel: private');
  eq(effectiveLevel({ state: 'share' }), 'share', 'effectiveLevel: object share');
  eq(effectiveLevel({ state: 'direct' }), 'direct', 'effectiveLevel: object direct');
  eq(effectiveLevel({ state: 'private' }), 'private', 'effectiveLevel: object private');
  eq(effectiveLevel(undefined), 'private', 'effectiveLevel: absent -> private');
  eq(effectiveLevel({ state: 'share', migrated: true }), 'share',
    'effectiveLevel: the migration marker does not disturb the level');
}

// ---------------------------------------------------------------------------
// 7. "share everything except one thread" is now a per-conversation job: the
//    standing policy shares nothing, so only the explicitly-set rooms mirror.
// ---------------------------------------------------------------------------
{
  const policy = { global: 'share-all', sources: {} };
  eq(resolve(convo('imessage', 'iMessage'), policy, 'private'), { shared: false, reason: 'excluded' },
    'except-one: the excluded thread is private');
  for (const c of [convo('imessage'), convo('linkedin'), convo('whatsapp')]) {
    eq(resolve(c, policy, undefined), { shared: false, reason: 'private' },
      'except-one: unset thread ' + c.sourceId + ' is private, NOT swept in by share-all');
    eq(resolve(c, policy, 'share'), { shared: true, reason: 'explicit' },
      'except-one: thread ' + c.sourceId + ' shares only when set explicitly');
  }
}

// ---------------------------------------------------------------------------
// 8. resolveAll: batch resolve, plain-object and Map overrides, input order;
//    policy/profiles arguments are accepted and ignored.
// ---------------------------------------------------------------------------
{
  const convos = [
    { id: '!a:local', sourceId: 'imessage', sourceLabel: 'iMessage' },
    { id: '!b:local', sourceId: 'linkedin', sourceLabel: 'LinkedIn' },
    { id: '!c:local', sourceId: 'linkedin', sourceLabel: 'LinkedIn' },
    { id: '!d:local', sourceId: 'imessage', sourceLabel: 'iMessage' },
  ];
  const overridesObj = { '!a:local': 'share', '!c:local': 'direct', '!d:local': 'private' };
  const resObj = resolveAll(convos, LOUD_POLICY, overridesObj);
  eq(resObj.map(r => r.shared), [true, false, true, false], 'resolveAll: plain-object overrides');
  eq(resObj.map(r => r.reason), ['explicit', 'private', 'direct', 'excluded'], 'resolveAll: reasons');
  eq(resObj.map(r => r.convo.id), ['!a:local', '!b:local', '!c:local', '!d:local'],
    'resolveAll: preserves input order');

  const resMap = resolveAll(convos, LOUD_POLICY, new Map([['!a:local', 'private'], ['!b:local', 'share']]));
  eq(resMap.map(r => r.shared), [false, true, false, false], 'resolveAll: Map overrides');
  eq(resMap.map(r => r.reason), ['excluded', 'explicit', 'private', 'private'], 'resolveAll: Map reasons');

  eq(resolveAll(convos, LOUD_POLICY, undefined).map(r => r.shared), [false, false, false, false],
    'resolveAll: no overrides -> all private despite a share-all policy');
  const profiles = { '!a:local': LOUD_PROFILE, '!b:local': LOUD_PROFILE };
  eq(resolveAll(convos, LOUD_POLICY, undefined, profiles).map(r => r.shared),
    [false, false, false, false], 'resolveAll: a shared profiles map shares nothing');
  eq(resolveAll(null, LOUD_POLICY, undefined), [], 'resolveAll: non-array convos -> empty list, no throw');

  // PARITY REGRESSION (found by the conformance harness): the override key is
  // read ONLY from a plain-object convo. `convo && convo.id` yielded "" for the
  // empty-string convo, which matched an override stored under "" and shared a
  // conversation the Python enforcer resolved private.
  const degenerate = [{ id: '', sourceId: '' }, '', 0, null, [], { id: 5 }];
  eq(resolveAll(degenerate, LOUD_POLICY, { '': 'share' }).map(r => r.shared),
    [true, false, false, false, false, false],
    'resolveAll: only a plain-object convo can match an override key');
}

// ---------------------------------------------------------------------------
// 9. normalizePolicy: unchanged. The standing policy still round-trips through
//    storage (the account-data event still exists) — it just no longer decides
//    anything on the conversation path.
// ---------------------------------------------------------------------------
{
  eq(normalizePolicy(undefined), { global: 'private', sources: {} }, 'normalizePolicy: undefined input');
  eq(normalizePolicy(null), { global: 'private', sources: {} }, 'normalizePolicy: null input');
  eq(normalizePolicy({}), { global: 'private', sources: {} }, 'normalizePolicy: empty object');
  eq(normalizePolicy({ global: 'share-all', sources: {} }), { global: 'share-all', sources: {} }, 'normalizePolicy: valid share-all passes through');
  eq(normalizePolicy({ global: 'bogus', sources: {} }), { global: 'private', sources: {} }, 'normalizePolicy: unknown global collapses to private');
  eq(normalizePolicy({ global: 'private', sources: { a: 'share-all', b: 'private-all', c: 'inherit', d: 'junk', e: 123 } }),
    { global: 'private', sources: { a: 'share-all', b: 'private-all' } },
    'normalizePolicy: drops inherit/junk/non-string source states');
  eq(normalizePolicy({ global: 'share-all', sources: null }), { global: 'share-all', sources: {} }, 'normalizePolicy: null sources -> {}');
  // An ARRAY sources (typeof 'object' in JS) must be rejected like a missing
  // one, not walked as {'0':..,'1':..} — matches Python's isinstance(dict).
  eq(normalizePolicy({ global: 'private', sources: ['share-all', 'private-all'] }),
    { global: 'private', sources: {} }, 'normalizePolicy: array sources -> {} (same as no sources)');
}

// ---------------------------------------------------------------------------
// 10. normalizeOverride: 'share'/'direct'/'private' survive; everything else is
//     null (== no recognized level stored == private).
// ---------------------------------------------------------------------------
{
  eq(normalizeOverride(undefined), null, 'normalizeOverride: undefined -> null');
  eq(normalizeOverride(null), null, 'normalizeOverride: null -> null');
  eq(normalizeOverride({}), null, 'normalizeOverride: empty object -> null');
  eq(normalizeOverride('share'), 'share', 'normalizeOverride: bare string share');
  eq(normalizeOverride('direct'), 'direct', 'normalizeOverride: bare string direct');
  eq(normalizeOverride('private'), 'private', 'normalizeOverride: bare string private');
  eq(normalizeOverride('inherit'), null, 'normalizeOverride: bare string inherit -> null');
  eq(normalizeOverride({ state: 'share' }), 'share', 'normalizeOverride: object form share');
  eq(normalizeOverride({ state: 'direct' }), 'direct', 'normalizeOverride: object form direct');
  eq(normalizeOverride({ state: 'private' }), 'private', 'normalizeOverride: object form private');
  eq(normalizeOverride({ state: 'inherit' }), null, 'normalizeOverride: object form inherit -> null');
  eq(normalizeOverride({ state: 'junk' }), null, 'normalizeOverride: object form junk -> null');
  eq(normalizeOverride({ state: 'share', migrated: true }), 'share',
    'normalizeOverride: the D0 migration marker rides along untouched');
}

// ---------------------------------------------------------------------------
// 11. overridesFromSync: carries all three levels; PINNED — a later junk value
//     DELETES an earlier valid one for the same room (a cleared/garbled event
//     must not leave a stale share behind), and the room then resolves private.
// ---------------------------------------------------------------------------
{
  const ov = (content) => ({ type: 'com.jkali.share_override', content });
  const sync = { rooms: { join: {
    '!a:local': { account_data: { events: [ov({ state: 'share' })] } },
    '!b:local': { account_data: { events: [ov('private')] } },
    '!c:local': { account_data: { events: [ov({ state: 'direct' })] } },
    '!d:local': { account_data: { events: [ov({ state: 'inherit' })] } },
    '!e:local': { account_data: { events: [ov({ state: 'share', migrated: true })] } },
    '!f:local': { account_data: { events: [{ type: 'm.tag', content: {} }] } },
  } } };
  eq(overridesFromSync(sync),
    { '!a:local': 'share', '!b:local': 'private', '!c:local': 'direct', '!e:local': 'share' },
    'overridesFromSync: keeps share/direct/private, omits inherit + unrelated types');

  const clobber = { rooms: { join: {
    '!a:local': { account_data: { events: [ov({ state: 'share' }), ov({ state: 'junk' })] } },
    '!b:local': { account_data: { events: [ov({ state: 'direct' }), ov({})] } },
    '!c:local': { account_data: { events: [ov({ state: 'share' }), ov({ state: 'private' })] } },
  } } };
  eq(overridesFromSync(clobber), { '!c:local': 'private' },
    'PINNED overridesFromSync: a later junk/empty event deletes the earlier valid key');
  eq(resolve({ id: '!a:local', sourceId: 'imessage' }, LOUD_POLICY,
    overridesFromSync(clobber)['!a:local'], LOUD_PROFILE),
    { shared: false, reason: 'private' },
    'PINNED: a key deleted by a junk value resolves private, never inherited-shared');

  eq(overridesFromSync({ rooms: { join: { 'not-a-room': { account_data: { events: [ov('share')] } } } } }),
    {}, 'overridesFromSync: non-room-shaped keys never enter the map');
  eq(overridesFromSync(null), {}, 'overridesFromSync: junk input -> {}, no throw');
}

// ---------------------------------------------------------------------------
// 12. Reason string exhaustiveness: every branch's exact value. `reason` is
//     UI-only — authorization reads `shared` / effectiveLevel().
// ---------------------------------------------------------------------------
{
  eq(resolve(convo('x'), {}, undefined).reason, 'private', 'reason: unset -> "private"');
  eq(resolve(convo('x'), { global: 'share-all' }, undefined).reason, 'private',
    'reason: global share-all no longer produces an "all <source>" reason');
  eq(resolve(convo('x'), {}, 'share').reason, 'explicit', 'reason: share -> "explicit"');
  eq(resolve(convo('x'), {}, 'direct').reason, 'direct', 'reason: direct -> "direct"');
  eq(resolve(convo('x'), { global: 'share-all' }, 'private').reason, 'excluded', 'reason: private -> "excluded"');
  eq(resolve(convo('x'), {}, undefined, { displayName: 'Ann', share: 'share' }).reason, 'private',
    'reason: a shared profile no longer produces a "profile: <name>" reason');
}

// ---------------------------------------------------------------------------
// 13. Hostile/degenerate convo shapes never throw and never share (the convo is
//     ignored entirely, so only the override can decide).
// ---------------------------------------------------------------------------
{
  for (const c of [null, undefined, 5, 'imessage', [], {}, { id: '!r:l', sourceId: 5 },
    { id: '!r:l', sourceId: '__proto__' }]) {
    eq(resolve(c, LOUD_POLICY, undefined, LOUD_PROFILE), { shared: false, reason: 'private' },
      'hostile convo ' + JSON.stringify(c === undefined ? '<undefined>' : c) + ': private');
    eq(resolve(c, DENY_POLICY, 'share', DENY_PROFILE), { shared: true, reason: 'explicit' },
      'hostile convo ' + JSON.stringify(c === undefined ? '<undefined>' : c) + ': explicit share still holds');
  }
}


// ---------------------------------------------------------------------------
// 14. THE ACCOUNT DEFAULT (share_policy.default_level, roadmap §6). An explicit
//     per-room override always wins; with NO override the account default
//     decides, and an absent/unrecognized default is 'private'.
//     resolvedLevel() is a NEW function — effectiveLevel() keeps its
//     one-argument "explicit level only" meaning on purpose.
// ---------------------------------------------------------------------------
{
  const DEF = (lv) => ({ global: 'private', sources: {}, default_level: lv });

  // resolvedLevel: override wins, else the default, else private
  eq(resolvedLevel(undefined, DEF('direct')), 'direct', 'resolvedLevel: unset takes a direct default');
  eq(resolvedLevel(undefined, DEF('share')), 'share', 'resolvedLevel: unset takes a share default');
  eq(resolvedLevel(undefined, DEF('private')), 'private', 'resolvedLevel: unset takes a private default');
  eq(resolvedLevel(undefined, {}), 'private', 'resolvedLevel: no default at all -> private');
  eq(resolvedLevel('private', DEF('direct')), 'private',
    'resolvedLevel: an EXPLICIT private beats a direct default');
  eq(resolvedLevel('share', DEF('direct')), 'share', 'resolvedLevel: explicit share beats a direct default');
  eq(resolvedLevel('direct', DEF('private')), 'direct', 'resolvedLevel: explicit direct beats a private default');
  eq(resolvedLevel({ state: 'private' }, DEF('direct')), 'private', 'resolvedLevel: object form, explicit wins');

  // effectiveLevel is UNCHANGED: it never sees the default (an unconverted
  // call site must UNDER-claim, never mis-claim).
  eq(effectiveLevel(undefined), 'private', 'effectiveLevel: still 1-arg, still "explicit level"');
  eq(effectiveLevel('share'), 'share', 'effectiveLevel: unchanged for an explicit level');

  // An UNRECOGNIZED override is "no override": it falls through to the
  // default, never shares on its own, and never beats an explicit private.
  for (const junk of [null, '', 'inherit', 'junk', 'Direct', 'direct ', 'share-all',
    0, 5, true, false, [], {}, ['direct'], { state: 'junk' }, { state: ['share'] },
    { State: 'direct' }, { default_level: 'direct' }]) {
    eq(resolvedLevel(junk, DEF('direct')), 'direct',
      'junk override ' + JSON.stringify(junk) + ' degrades to "no override"');
    eq(resolvedLevel(junk, {}), 'private',
      'junk override ' + JSON.stringify(junk) + ' with no default -> private');
  }

  // policyDefaultLevel: only the three exact strings survive.
  for (const bad of ['Direct', 'direct ', ' direct', 'DIRECT', 'inherit', 'share-all', '',
    null, true, false, 0, 5, [], ['direct'], {}, { state: 'direct' }]) {
    eq(policyDefaultLevel({ default_level: bad }), 'private',
      'policyDefaultLevel: junk ' + JSON.stringify(bad) + ' -> private');
  }
  for (const container of [null, undefined, 5, 'direct', [], ['direct'],
    { default_level: 'direct', extra: 1 }]) {
    const want = (container && container.default_level === 'direct') ? 'direct' : 'private';
    eq(policyDefaultLevel(container), want,
      'policyDefaultLevel: container ' + JSON.stringify(container === undefined ? '<undefined>' : container));
  }
  // Prototype-named placements never reach the resolver.
  eq(policyDefaultLevel(JSON.parse('{"__proto__":{"default_level":"direct"}}')), 'private',
    'policyDefaultLevel: a __proto__-nested default is not a default');
  eq(policyDefaultLevel({ sources: { default_level: 'direct' } }), 'private',
    'policyDefaultLevel: a nested default under sources is not a default');

  // resolve(): the two NEW reasons, and the old ones unchanged.
  eq(resolve(convo('x'), DEF('share'), undefined), { shared: true, reason: 'default-share' },
    'resolve: unset under a share default');
  eq(resolve(convo('x'), DEF('direct'), undefined), { shared: true, reason: 'default-direct' },
    'resolve: unset under a direct default');
  eq(resolve(convo('x'), DEF('private'), undefined), { shared: false, reason: 'private' },
    'resolve: unset under a private default stays "private"');
  eq(resolve(convo('x'), DEF('direct'), 'private'), { shared: false, reason: 'excluded' },
    'resolve: explicit private under a direct default is "excluded"');
  eq(resolve(convo('x'), DEF('direct'), 'share'), { shared: true, reason: 'explicit' },
    'resolve: explicit share under a direct default is still "explicit"');
  eq(resolve(convo('x'), DEF('private'), 'direct'), { shared: true, reason: 'direct' },
    'resolve: explicit direct under a private default is still "direct"');
  eq(effectiveShared(convo('x'), DEF('direct'), undefined), true,
    'effectiveShared follows resolve under a default');
  eq(effectiveShared(convo('x'), DEF('direct'), 'private'), false,
    'effectiveShared: an explicit private is not shared under a direct default');

  // The per-source / global standing policies are STILL dead: only
  // default_level can share an unset conversation.
  eq(resolve(convo('imessage'), LOUD_POLICY, undefined), { shared: false, reason: 'private' },
    'resolve: share-all WITHOUT a default_level still shares nothing');

  // resolveAll threads the policy.
  {
    const list = [{ id: '!a:l' }, { id: '!b:l' }, { id: '!c:l' }];
    eq(resolveAll(list, DEF('direct'), { '!b:l': 'private' }).map((r) => r.reason),
      ['default-direct', 'excluded', 'default-direct'], 'resolveAll: threads default_level');
    eq(resolveAll(list, {}, { '!b:l': 'share' }).map((r) => r.shared),
      [false, true, false], 'resolveAll: no default -> only the explicit share');
  }

  // normalizePolicy keeps default_level only when exactly valid, and a
  // round-trip drops junk rather than laundering it.
  eq(normalizePolicy({ default_level: 'direct' }).default_level, 'direct',
    'normalizePolicy: keeps a valid default_level');
  eq(normalizePolicy({ default_level: 'Direct' }).default_level, undefined,
    'normalizePolicy: drops a near-miss default_level');
  eq(normalizePolicy({ default_level: ['direct'] }).default_level, undefined,
    'normalizePolicy: drops a non-string default_level');
  eq(policyDefaultLevel(normalizePolicy({ default_level: 'inherit' })), 'private',
    'normalizePolicy round-trip: junk reads back as private');
  eq(normalizePolicy(normalizePolicy({ global: 'share-all', default_level: 'share' })),
    { global: 'share-all', sources: {}, default_level: 'share' },
    'normalizePolicy: idempotent over a valid default');
}

// ---------------------------------------------------------------------------
// 15. The consent-model marker constants (F7) — apps/user gates the new
//     account-default control on 3, and the dead standing controls on 2.
// ---------------------------------------------------------------------------
eq(CONSENT_MODEL_EXPLICIT, 3, 'marker: explicit-model version');
eq(CONSENT_MODEL_PER_ROOM, 2, 'marker: per-room-only model version');

// ---------------------------------------------------------------------------
console.log(`\n${pass} passed, ${fail} failed`);
if (fail > 0) {
  console.error('\nFailures:');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
}
