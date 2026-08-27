// Plain-node test for shared/model/consent.js — no framework.
// Run: node tests/unit/consent.test.js  (or via docker, see tests/run.sh)
// Exits 0 on all-pass, nonzero (via process.exitCode) on any failure.

import {
  resolve, effectiveShared, resolveAll,
  normalizePolicy, normalizeOverride,
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

// ---------------------------------------------------------------------------
// 1. Global default (no policy, no overrides) -> private
// ---------------------------------------------------------------------------
{
  const c = convo('imessage', 'iMessage');
  eq(resolve(c, {}, undefined), { shared: false, reason: 'private' }, 'default: no policy at all');
  eq(resolve(c, { global: 'private', sources: {} }, undefined),
    { shared: false, reason: 'private' }, 'default: explicit global private');
  eq(effectiveShared(c, {}, undefined), false, 'default: effectiveShared false');
}

// ---------------------------------------------------------------------------
// 2. Global share-all -> shared for every source, including ones with no
//    per-source entry, with reason "all <source>"
// ---------------------------------------------------------------------------
{
  const policy = { global: 'share-all', sources: {} };
  eq(resolve(convo('imessage', 'iMessage'), policy, undefined),
    { shared: true, reason: 'all iMessage' }, 'global share-all: iMessage');
  eq(resolve(convo('linkedin', 'LinkedIn'), policy, undefined),
    { shared: true, reason: 'all LinkedIn' }, 'global share-all: LinkedIn');
  // falls back to sourceId label when no sourceLabel given
  eq(resolve({ id: '!x:local', sourceId: 'whatsapp' }, policy, undefined),
    { shared: true, reason: 'all whatsapp' }, 'global share-all: label falls back to sourceId');
  // falls back to generic token when neither present
  eq(resolve({ id: '!x:local' }, policy, undefined),
    { shared: true, reason: 'all source' }, 'global share-all: generic fallback label');
}

// ---------------------------------------------------------------------------
// 3. Per-source share-all shares only that source, including a conversation
//    of that source arriving later (standing policy) — global stays private.
// ---------------------------------------------------------------------------
{
  const policy = { global: 'private', sources: { imessage: 'share-all' } };
  eq(resolve(convo('imessage', 'iMessage'), policy, undefined),
    { shared: true, reason: 'all iMessage' }, 'per-source share-all: matching source shared');
  eq(resolve(convo('linkedin', 'LinkedIn'), policy, undefined),
    { shared: false, reason: 'private' }, 'per-source share-all: other source stays private (global default)');
  // "newly arrived" conversation of the share-all source — same policy object,
  // conversation object constructed independently -> still shared (standing policy).
  const laterArrival = convo('imessage', 'iMessage');
  eq(resolve(laterArrival, policy, undefined),
    { shared: true, reason: 'all iMessage' }, 'per-source share-all: standing policy covers later-arriving convo');
}

// ---------------------------------------------------------------------------
// 4. Per-source private-all overrides global share-all
// ---------------------------------------------------------------------------
{
  const policy = { global: 'share-all', sources: { linkedin: 'private-all' } };
  eq(resolve(convo('linkedin', 'LinkedIn'), policy, undefined),
    { shared: false, reason: 'private' }, 'per-source private-all beats global share-all');
  eq(resolve(convo('imessage', 'iMessage'), policy, undefined),
    { shared: true, reason: 'all iMessage' }, 'per-source private-all: other sources still shared via global');
}

// per-source 'inherit' falls through to global (both directions)
{
  eq(resolve(convo('imessage'), { global: 'share-all', sources: { imessage: 'inherit' } }, undefined),
    { shared: true, reason: 'all imessage' }, 'per-source inherit falls through to global share-all');
  eq(resolve(convo('imessage'), { global: 'private', sources: { imessage: 'inherit' } }, undefined),
    { shared: false, reason: 'private' }, 'per-source inherit falls through to global private');
}

// ---------------------------------------------------------------------------
// 5. Per-conversation override 'private' excludes despite any higher-level
//    share-all (global and/or per-source).
// ---------------------------------------------------------------------------
{
  eq(resolve(convo('imessage'), { global: 'share-all', sources: {} }, 'private'),
    { shared: false, reason: 'excluded' }, 'per-conv private excludes despite global share-all');
  eq(resolve(convo('imessage'), { global: 'private', sources: { imessage: 'share-all' } }, 'private'),
    { shared: false, reason: 'excluded' }, 'per-conv private excludes despite per-source share-all');
  eq(resolve(convo('imessage'), { global: 'share-all', sources: { imessage: 'share-all' } }, 'private'),
    { shared: false, reason: 'excluded' }, 'per-conv private excludes despite BOTH share-all levels');
}

// ---------------------------------------------------------------------------
// 6. Per-conversation override 'share' includes despite default-private
//    (no policy share-all anywhere, and despite per-source private-all).
// ---------------------------------------------------------------------------
{
  eq(resolve(convo('imessage'), { global: 'private', sources: {} }, 'share'),
    { shared: true, reason: 'explicit' }, 'per-conv share includes despite default-private');
  eq(resolve(convo('imessage'), { global: 'private', sources: { imessage: 'private-all' } }, 'share'),
    { shared: true, reason: 'explicit' }, 'per-conv share includes despite per-source private-all');
  eq(resolve(convo('imessage'), {}, 'share'),
    { shared: true, reason: 'explicit' }, 'per-conv share includes despite empty policy');
}

// ---------------------------------------------------------------------------
// "share everything except one thread": global share-all + one excluded convo
// ---------------------------------------------------------------------------
{
  const policy = { global: 'share-all', sources: {} };
  const excluded = convo('imessage', 'iMessage');
  const others = [convo('imessage', 'iMessage'), convo('linkedin', 'LinkedIn'), convo('whatsapp', 'WhatsApp')];
  eq(resolve(excluded, policy, 'private'), { shared: false, reason: 'excluded' }, 'share-everything-except-one: excluded thread');
  for (const c of others) {
    eq(resolve(c, policy, undefined), { shared: true, reason: 'all ' + c.sourceLabel },
      'share-everything-except-one: other thread ' + c.sourceId + ' still shared');
  }
}

// ---------------------------------------------------------------------------
// "all iMessage but not LinkedIn": per-source share-all + per-source private-all
// ---------------------------------------------------------------------------
{
  const policy = { global: 'private', sources: { imessage: 'share-all', linkedin: 'private-all' } };
  eq(resolve(convo('imessage', 'iMessage'), policy, undefined),
    { shared: true, reason: 'all iMessage' }, 'all-imessage-not-linkedin: iMessage shared');
  eq(resolve(convo('linkedin', 'LinkedIn'), policy, undefined),
    { shared: false, reason: 'private' }, 'all-imessage-not-linkedin: LinkedIn private');
  eq(resolve(convo('whatsapp', 'WhatsApp'), policy, undefined),
    { shared: false, reason: 'private' }, 'all-imessage-not-linkedin: unrelated source stays default private');
}

// ---------------------------------------------------------------------------
// effectiveShared mirrors resolve().shared across all four precedence levels
// ---------------------------------------------------------------------------
{
  eq(effectiveShared(convo('imessage'), { global: 'share-all', sources: {} }, undefined), true, 'effectiveShared: global share-all');
  eq(effectiveShared(convo('imessage'), { global: 'private', sources: { imessage: 'share-all' } }, undefined), true, 'effectiveShared: per-source share-all');
  eq(effectiveShared(convo('imessage'), { global: 'share-all', sources: { imessage: 'private-all' } }, undefined), false, 'effectiveShared: per-source private-all beats global');
  eq(effectiveShared(convo('imessage'), { global: 'share-all' }, 'private'), false, 'effectiveShared: per-conv private beats global share-all');
  eq(effectiveShared(convo('imessage'), {}, 'share'), true, 'effectiveShared: per-conv share beats default');
}

// ---------------------------------------------------------------------------
// resolveAll: batch resolve, both plain-object and Map overrides, in input order
// ---------------------------------------------------------------------------
{
  const policy = { global: 'private', sources: { imessage: 'share-all' } };
  const convos = [
    { id: '!a:local', sourceId: 'imessage', sourceLabel: 'iMessage' },
    { id: '!b:local', sourceId: 'linkedin', sourceLabel: 'LinkedIn' },
    { id: '!c:local', sourceId: 'linkedin', sourceLabel: 'LinkedIn' },
  ];
  const overridesObj = { '!c:local': 'share' };
  const resObj = resolveAll(convos, policy, overridesObj);
  eq(resObj.map(r => r.shared), [true, false, true], 'resolveAll: plain-object overrides shape');
  eq(resObj.map(r => r.reason), ['all iMessage', 'private', 'explicit'], 'resolveAll: plain-object overrides reasons');
  eq(resObj.map(r => r.convo.id), ['!a:local', '!b:local', '!c:local'], 'resolveAll: preserves input order');

  const overridesMap = new Map([['!c:local', 'private']]);
  const resMap = resolveAll(convos, policy, overridesMap);
  eq(resMap.map(r => r.shared), [true, false, false], 'resolveAll: Map overrides shape');
  eq(resMap.map(r => r.reason), ['all iMessage', 'private', 'excluded'], 'resolveAll: Map overrides reasons');

  eq(resolveAll(convos, policy, undefined).map(r => r.shared), [true, false, false], 'resolveAll: no overrides at all');
  eq(resolveAll(null, policy, undefined), [], 'resolveAll: non-array convos -> empty list, no throw');
}

// ---------------------------------------------------------------------------
// normalizePolicy: unknown global collapses to private; only share-all/private-all survive
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
}

// ---------------------------------------------------------------------------
// normalizeOverride: only 'share'/'private' survive; object or bare-string form
// ---------------------------------------------------------------------------
{
  eq(normalizeOverride(undefined), null, 'normalizeOverride: undefined -> null');
  eq(normalizeOverride(null), null, 'normalizeOverride: null -> null');
  eq(normalizeOverride({}), null, 'normalizeOverride: empty object -> null');
  eq(normalizeOverride('share'), 'share', 'normalizeOverride: bare string share');
  eq(normalizeOverride('private'), 'private', 'normalizeOverride: bare string private');
  eq(normalizeOverride('inherit'), null, 'normalizeOverride: bare string inherit -> null');
  eq(normalizeOverride({ state: 'share' }), 'share', 'normalizeOverride: object form share');
  eq(normalizeOverride({ state: 'private' }), 'private', 'normalizeOverride: object form private');
  eq(normalizeOverride({ state: 'inherit' }), null, 'normalizeOverride: object form inherit -> null');
  eq(normalizeOverride({ state: 'junk' }), null, 'normalizeOverride: object form junk -> null');
}

// ---------------------------------------------------------------------------
// Reason string exhaustiveness: every branch's exact reason value
// ---------------------------------------------------------------------------
{
  eq(resolve(convo('x'), {}, undefined).reason, 'private', 'reason: default private');
  eq(resolve(convo('x'), { global: 'share-all' }, undefined).reason, 'all x', 'reason: global share-all interpolates source');
  eq(resolve(convo('x'), { global: 'private', sources: { x: 'share-all' } }, undefined).reason, 'all x', 'reason: per-source share-all interpolates source');
  eq(resolve(convo('x'), { global: 'share-all', sources: { x: 'private-all' } }, undefined).reason, 'private', 'reason: per-source private-all -> "private"');
  eq(resolve(convo('x'), {}, 'share').reason, 'explicit', 'reason: per-conv share -> "explicit"');
  eq(resolve(convo('x'), { global: 'share-all' }, 'private').reason, 'excluded', 'reason: per-conv private -> "excluded"');
}

// ===========================================================================
// PROFILE LEVEL (§12 phase 5) — precedence: per-conv override > profile >
// per-source > global > private. profile arg is { displayName, share }.
// ===========================================================================

// P1. A shared profile shares its members, even with global private + no source.
{
  const prof = { displayName: 'Dana Lewis', share: 'share' };
  eq(resolve(convo('imessage', 'iMessage'), { global: 'private', sources: {} }, undefined, prof),
    { shared: true, reason: 'profile: Dana Lewis' }, 'profile share: shares member despite default-private');
  eq(resolve(convo('linkedin', 'LinkedIn'), { global: 'private', sources: {} }, undefined, prof),
    { shared: true, reason: 'profile: Dana Lewis' }, 'profile share: shares a member on a different source too (same person, 2 platforms)');
  eq(resolve({ id: '!x:local', sourceId: 'imessage' }, {}, undefined, { share: 'share' }),
    { shared: true, reason: 'profile: profile' }, 'profile share: name falls back to generic "profile"');
}

// P2. Profile beats per-source (both directions).
{
  // profile private beats per-source share-all
  eq(resolve(convo('imessage'), { global: 'private', sources: { imessage: 'share-all' } }, undefined,
    { displayName: 'Dana', share: 'private' }),
    { shared: false, reason: 'profile: Dana' }, 'profile private beats per-source share-all');
  // profile share beats per-source private-all
  eq(resolve(convo('imessage'), { global: 'private', sources: { imessage: 'private-all' } }, undefined,
    { displayName: 'Dana', share: 'share' }),
    { shared: true, reason: 'profile: Dana' }, 'profile share beats per-source private-all');
  // profile private beats global share-all
  eq(resolve(convo('imessage'), { global: 'share-all', sources: {} }, undefined,
    { displayName: 'Dana', share: 'private' }),
    { shared: false, reason: 'profile: Dana' }, 'profile private beats global share-all');
}

// P3. Per-conversation override still wins over the profile (both directions).
{
  // per-conv private excludes a member of a SHARED profile
  eq(resolve(convo('imessage'), { global: 'private', sources: {} }, 'private',
    { displayName: 'Dana', share: 'share' }),
    { shared: false, reason: 'excluded' }, 'per-conv private excludes despite profile share');
  // per-conv share includes despite a private profile
  eq(resolve(convo('imessage'), { global: 'private', sources: {} }, 'share',
    { displayName: 'Dana', share: 'private' }),
    { shared: true, reason: 'explicit' }, 'per-conv share includes despite profile private');
}

// P4. profile 'inherit' (or absent) falls through to source/global.
{
  eq(resolve(convo('imessage', 'iMessage'), { global: 'share-all', sources: {} }, undefined, { displayName: 'D', share: 'inherit' }),
    { shared: true, reason: 'all iMessage' }, 'profile inherit falls through to global share-all');
  eq(resolve(convo('imessage', 'iMessage'), { global: 'private', sources: { imessage: 'share-all' } }, undefined, { displayName: 'D', share: 'inherit' }),
    { shared: true, reason: 'all iMessage' }, 'profile inherit falls through to per-source share-all');
  eq(resolve(convo('imessage'), { global: 'private', sources: {} }, undefined, { displayName: 'D', share: 'inherit' }),
    { shared: false, reason: 'private' }, 'profile inherit + nothing else -> private');
  eq(resolve(convo('imessage'), { global: 'private', sources: {} }, undefined, null),
    { shared: false, reason: 'private' }, 'no profile at all -> unchanged private');
}

// P5. effectiveShared threads the profile arg.
{
  eq(effectiveShared(convo('imessage'), { global: 'private', sources: {} }, undefined, { share: 'share' }), true, 'effectiveShared: profile share');
  eq(effectiveShared(convo('imessage'), { global: 'share-all', sources: {} }, undefined, { share: 'private' }), false, 'effectiveShared: profile private beats global share-all');
  eq(effectiveShared(convo('imessage'), { global: 'share-all', sources: {} }, 'private', { share: 'share' }), false, 'effectiveShared: per-conv private beats profile share');
}

// P6. resolveAll with a per-room profiles map (a profile spanning 2 platforms;
//     one member carries a per-conversation private override and drops out).
{
  const policy = { global: 'private', sources: {} };
  const convos = [
    { id: '!im:local', sourceId: 'imessage', sourceLabel: 'iMessage' },
    { id: '!li:local', sourceId: 'linkedin', sourceLabel: 'LinkedIn' },
    { id: '!ex:local', sourceId: 'imessage', sourceLabel: 'iMessage' },
    { id: '!un:local', sourceId: 'whatsapp', sourceLabel: 'WhatsApp' }, // no profile
  ];
  const P = { displayName: 'Dana Lewis', share: 'share' };
  const profiles = { '!im:local': P, '!li:local': P, '!ex:local': P };
  const overrides = { '!ex:local': 'private' }; // per-conv exclusion on one member
  const res = resolveAll(convos, policy, overrides, profiles);
  eq(res.map(r => r.shared), [true, true, false, false], 'resolveAll+profile: 2 platforms shared, excluded member out, non-member private');
  eq(res.map(r => r.reason), ['profile: Dana Lewis', 'profile: Dana Lewis', 'excluded', 'private'], 'resolveAll+profile: reasons');
  // Map form of the profiles argument works too.
  const resMap = resolveAll(convos, policy, overrides, new Map([['!im:local', P], ['!li:local', P], ['!ex:local', P]]));
  eq(resMap.map(r => r.shared), [true, true, false, false], 'resolveAll+profile: Map profiles argument');
  // no profiles argument -> behaves as before (all private under this policy)
  eq(resolveAll(convos, policy, undefined).map(r => r.shared), [false, false, false, false], 'resolveAll: no profiles arg unchanged');
}

// P7. profile reason exhaustiveness.
{
  eq(resolve(convo('x'), {}, undefined, { displayName: 'Ann', share: 'share' }).reason, 'profile: Ann', 'reason: profile share interpolates name');
  eq(resolve(convo('x'), {}, undefined, { displayName: 'Ann', share: 'private' }).reason, 'profile: Ann', 'reason: profile private interpolates name');
}

// ---------------------------------------------------------------------------
console.log(`\n${pass} passed, ${fail} failed`);
if (fail > 0) {
  console.error('\nFailures:');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
}
