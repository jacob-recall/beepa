// Plain-node test for apps/user/consent.js's PURE bulk-action planner
// (direct-share-level plan D3/F11, S2 acceptance, amended 2026-09-02 by an
// explicit product-owner-approved reversal of F11's "bulk never offers
// direct" disposition — see D3 in
// docs/superpowers/plans/2026-09-02-direct-share-level.md): "set all
// conversations in this source to Share/Private/Direct" writes only
// share/private/direct — anything else ('inherit', junk, undefined) is
// refused — and never silently overwrites an existing explicit 'private'
// override; the caller must list every such conversation in the confirm
// before writing. A 'direct' plan is additionally marked
// `requiresRiskConfirm` and its `ids` enumerate EVERY affected room (not
// just the explicit-private overwrites), since the caller's confirm for
// bulk Direct must list every conversation, not only the ones being
// silently overwritten.
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
//        node tests/unit/share_bulk_action.test.js

import { planBulkShareChange } from '../../apps/user/consent.js';

let pass = 0;
let fail = 0;
const failures = [];
function ok(cond, label) { if (cond) { pass++; } else { fail++; failures.push(label); } }

const convos = [
  { id: '!a:localhost', title: 'A' },
  { id: '!b:localhost', title: 'B' },
  { id: '!c:localhost', title: 'C' },
];

// ---- refuses anything but 'share'/'private'/'direct' --------------------
ok(planBulkShareChange(convos, new Map(), 'inherit') === null, 'bulk refuses level "inherit"');
ok(planBulkShareChange(convos, new Map(), 'junk') === null, 'bulk refuses a junk level');
ok(planBulkShareChange(convos, new Map(), undefined) === null, 'bulk refuses an undefined level');

// ---- 'direct': plans correctly, enumerates ALL affected rooms, still
// flags explicit-private overwrites, and requires the risk confirm --------
const overridesMapDirect = new Map([['!a:localhost', 'private'], ['!b:localhost', 'share']]);
const planDirect = planBulkShareChange(convos, overridesMapDirect, 'direct');
ok(planDirect && planDirect.level === 'direct', 'plan level is "direct"');
ok(planDirect && planDirect.ids.length === 3
  && planDirect.ids.includes('!a:localhost') && planDirect.ids.includes('!b:localhost') && planDirect.ids.includes('!c:localhost'),
  'direct plan enumerates every convo in the source, not just the overwrites');
ok(planDirect && planDirect.overwritesPrivate.length === 1 && planDirect.overwritesPrivate[0] === '!a:localhost',
  'direct plan still flags the convo that is currently explicit private');
ok(planDirect && planDirect.requiresRiskConfirm === true,
  'a direct plan is marked as requiring the risk confirm');
const planShareForContrast = planBulkShareChange(convos, overridesMapDirect, 'share');
ok(planShareForContrast && planShareForContrast.requiresRiskConfirm !== true,
  'a share plan is not marked as requiring the risk confirm');

// ---- 'share': writes every convo id, and flags explicit-private overwrites --
const overridesMap = new Map([['!a:localhost', 'private'], ['!b:localhost', 'share']]);
const planShare = planBulkShareChange(convos, overridesMap, 'share');
ok(planShare && planShare.level === 'share', 'plan level is "share"');
ok(planShare && planShare.ids.length === 3
  && planShare.ids.includes('!a:localhost') && planShare.ids.includes('!b:localhost') && planShare.ids.includes('!c:localhost'),
  'plan targets every convo in the source');
ok(planShare && planShare.overwritesPrivate.length === 1 && planShare.overwritesPrivate[0] === '!a:localhost',
  'plan flags exactly the convo that is currently explicit private');

// ---- 'private': never flags an overwrite (setting private never overwrites
// an explicit private silently in a way that matters — nothing to warn about) --
const planPrivate = planBulkShareChange(convos, overridesMap, 'private');
ok(planPrivate && planPrivate.level === 'private', 'plan level is "private"');
ok(planPrivate && planPrivate.overwritesPrivate.length === 0,
  'bulk-to-private never lists an overwrite (private->private is not an overwrite)');

// ---- no explicit-private convos -> empty overwrite list, still returns a plan --
const planNoOverwrite = planBulkShareChange(convos, new Map(), 'share');
ok(planNoOverwrite && planNoOverwrite.overwritesPrivate.length === 0,
  'no overwrite listed when nothing is currently explicit private');
ok(planNoOverwrite && planNoOverwrite.ids.length === 3, 'still targets all convos');

// ---- object-shaped overrides map (not just a Map) is accepted the same way --
const planObjOverrides = planBulkShareChange(convos, { '!a:localhost': 'private' }, 'share');
ok(planObjOverrides && planObjOverrides.overwritesPrivate.length === 1
  && planObjOverrides.overwritesPrivate[0] === '!a:localhost',
  'plain-object overrides map is read the same as a Map');

// ---- malformed convo entries are skipped, not crashed on ----
const messyConvos = [{ id: '!a:localhost' }, null, {}, { id: '' }, { id: 42 }, { id: '!z:localhost', title: 'Z' }];
const planMessy = planBulkShareChange(messyConvos, new Map(), 'private');
ok(planMessy && planMessy.ids.length === 2
  && planMessy.ids.includes('!a:localhost') && planMessy.ids.includes('!z:localhost'),
  'malformed convo entries (null/empty/non-string id) are dropped, valid ones kept');

// ---- empty source -> plan with no ids, not null (level was valid) ----
const planEmpty = planBulkShareChange([], new Map(), 'share');
ok(planEmpty && planEmpty.ids.length === 0 && planEmpty.overwritesPrivate.length === 0,
  'an empty source still returns a (no-op) plan rather than null');

if (fail) {
  console.error('share_bulk_action.test.js: ' + fail + ' FAILED, ' + pass + ' passed');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
} else {
  console.log('share_bulk_action.test.js: all ' + pass + ' checks passed');
}
