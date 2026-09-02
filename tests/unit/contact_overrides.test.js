// Plain-node test for apps/user/consent.js's PURE per-contact-override
// planners and its two GUARDED write paths (per-contact-share plan, C3/C4).
//
// What is pinned here is the write discipline itself, not the UI:
//   F3  a read that fails performs ZERO writes — never a fail-to-empty read
//       followed by a blind PUT, which is how a stored 'private' deny gets
//       erased and a withheld contact re-widens to its source policy;
//   F5  a write that would cross the entry cap is REFUSED before any PUT, with
//       a message naming the cap — and an over-cap STORED map still accepts the
//       destructive-only recovery (single-key removal, clear-all), so recovery
//       never needs a raw Matrix client;
//   F7  a profile fan-out mints one key per LINKED handle, but only for handles
//       that reconcile against the imported address book — everything else is
//       reported as unmatched so the caller can refuse it visibly, because a
//       'private' that never applies is a leak the teammate believes closed and
//       a 'share' on a not-yet-imported handle is a dormant grant.
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
//        node tests/unit/contact_overrides.test.js
import {
  planContactOverrideWrite, isDestructiveOnly, planHandleFanOut,
  validImportedContacts, applyContactOverrides, saveProfilesGuarded,
} from '../../apps/user/consent.js';
import { CONTACT_OVERRIDES_CAP } from '../../shared/model/consent.js';

let pass = 0;
let fail = 0;
const failures = [];
function ok(cond, label) { if (cond) { pass++; } else { fail++; failures.push(label); } }
function eqJson(a, b, label) { ok(JSON.stringify(a) === JSON.stringify(b), label + ' -> ' + JSON.stringify(a)); }

const K1 = 'imessage|+15550000001';
const K2 = 'whatsapp|+15550000002';
const KNOWN = new Set(['imessage', 'whatsapp', 'linkedin']);

// ---- planContactOverrideWrite: merge, drop junk, cap ----------------------
let p = planContactOverrideWrite({}, [[K1, 'share']]);
ok(p.ok && p.next[K1] === 'share' && p.count === 1, 'plan: adds one key');
p = planContactOverrideWrite({ [K1]: 'share' }, [[K1, 'private']]);
ok(p.ok && p.next[K1] === 'private', 'plan: overwrites in place');
p = planContactOverrideWrite({ [K1]: 'share', [K2]: 'private' }, [[K1, null]]);
ok(p.ok && !(K1 in p.next) && p.next[K2] === 'private', 'plan: null clears just that key');
// the merge base is the FRESH map: a key we never touched survives
ok(planContactOverrideWrite({ [K2]: 'private' }, [[K1, 'share']]).next[K2] === 'private',
  'plan: merges over the fresh map, never replaces it');
// junk in either position is refused, not coerced
p = planContactOverrideWrite({}, [['nopipe', 'share'], [K1, 'junk'], [null, 'share']]);
ok(p.ok && p.count === 0 && p.refused.length === 3, 'plan: junk keys/values refused, none written');
// a stored map carrying junk is filtered before the merge
p = planContactOverrideWrite({ nopipe: 'share', [K1]: 'junk', [K2]: 'share' }, []);
eqJson(Object.keys(p.next), [K2], 'plan: stored junk filtered out of the base');

// ---- F5: the cap is refused BEFORE any write -----------------------------
const nearCap = {};
for (let i = 0; i < CONTACT_OVERRIDES_CAP; i++) nearCap['imessage|+1' + i] = 'private';
p = planContactOverrideWrite(nearCap, [[K1, 'share']]);
ok(!p.ok && p.reason === 'cap' && p.cap === CONTACT_OVERRIDES_CAP && p.next === null,
  'plan: crossing the cap is refused with the cap named');
// at exactly the cap, replacing an existing key is fine (count does not grow)
p = planContactOverrideWrite(nearCap, [['imessage|+10', 'share']]);
ok(p.ok && p.count === CONTACT_OVERRIDES_CAP, 'plan: at the cap, an in-place change still writes');
// over-cap stored map: a REMOVAL is allowed (it strictly reduces the count)
const overCap = Object.assign({}, nearCap);
overCap['imessage|+1extra'] = 'private';
overCap['imessage|+1extra2'] = 'private';
p = planContactOverrideWrite(overCap, [['imessage|+1extra', null]]);
ok(p.ok && p.count === CONTACT_OVERRIDES_CAP + 1, 'plan: over-cap removal is permitted');
p = planContactOverrideWrite(overCap, [[K1, 'share']]);
ok(!p.ok && p.reason === 'cap', 'plan: over-cap addition is refused');

ok(isDestructiveOnly([[K1, null], [K2, null]]) === true, 'destructive-only: clears');
ok(isDestructiveOnly([[K1, null], [K2, 'share']]) === false, 'destructive-only: mixed is not');
ok(isDestructiveOnly([]) === false, 'destructive-only: empty is not');

// ---- a fake transport, so "zero PUTs" is observable ----------------------
function makeIo(readResult, opts = {}) {
  const io = { writes: [], reads: 0 };
  io.read = async () => {
    io.reads++;
    if (readResult instanceof Error) throw readResult;
    return readResult;
  };
  io.write = async (map) => {
    io.writes.push(map);
    if (opts.writeError) throw opts.writeError;
    return map;
  };
  return io;
}
function httpError(status) {
  const e = new Error('HTTP ' + status);
  e.status = status;
  return e;
}

// ---- F3 ACCEPTANCE: a 500 on the read performs ZERO PUTs -----------------
let io = makeIo(httpError(500));
let res = await applyContactOverrides(io, [[K1, 'private']]);
ok(!res.ok && res.reason === 'read' && res.wrote === false, 'overrides: a throwing read refuses');
ok(io.writes.length === 0, 'overrides: a throwing read performs ZERO PUTs');

io = makeIo({ status: 'error', overrides: null });
res = await applyContactOverrides(io, [[K1, 'private']]);
ok(!res.ok && res.reason === 'read' && io.writes.length === 0,
  "overrides: a read reported 'error' performs ZERO PUTs");

// a 404 read is EMPTY, not an error — the first override must be writable
io = makeIo({ status: 'ok', overrides: {} });
res = await applyContactOverrides(io, [[K1, 'share']]);
ok(res.ok && res.wrote && io.writes.length === 1 && io.writes[0][K1] === 'share',
  'overrides: an absent map writes the first override');

// a failed WRITE is reported, never swallowed (F8)
io = makeIo({ status: 'ok', overrides: {} }, { writeError: httpError(502) });
res = await applyContactOverrides(io, [[K1, 'share']]);
ok(!res.ok && res.reason === 'write' && /502/.test(res.message || ''),
  'overrides: a failed write is reported with its reason');

// ---- F5 ACCEPTANCE: cap refusal performs ZERO PUTs and names the cap ------
io = makeIo({ status: 'ok', overrides: nearCap });
res = await applyContactOverrides(io, [[K1, 'share'], [K2, 'share']]);
ok(!res.ok && res.reason === 'cap' && io.writes.length === 0,
  'cap: a write that would cross the cap performs ZERO PUTs');
ok(String(res.message).indexOf(String(CONTACT_OVERRIDES_CAP)) !== -1,
  'cap: the refusal names the cap');

// ---- over-cap STORED map: recovery stays in-app ---------------------------
io = makeIo({ status: 'over-cap', overrides: overCap });
res = await applyContactOverrides(io, [[K1, 'share']]);
ok(!res.ok && res.reason === 'over-cap' && io.writes.length === 0,
  'over-cap: a non-destructive write is refused, ZERO PUTs');
io = makeIo({ status: 'over-cap', overrides: overCap });
res = await applyContactOverrides(io, [['imessage|+1extra', null]]);
ok(res.ok && res.wrote && !('imessage|+1extra' in io.writes[0]),
  'over-cap: a single-key removal is permitted (recovery)');
io = makeIo({ status: 'over-cap', overrides: overCap });
res = await applyContactOverrides(io, Object.keys(overCap).map((k) => [k, null]));
ok(res.ok && res.wrote && Object.keys(io.writes[0]).length === 0,
  'over-cap: clear-all succeeds and returns the map under the cap');

// ---- F3 for com.jkali.contact_profiles: 500 -> zero PUTs -----------------
let pio = makeIo(httpError(500));
let pres = await saveProfilesGuarded(pio, () => ({ profiles: [] }));
ok(!pres.ok && pres.reason === 'read' && pio.writes.length === 0,
  'profiles: a throwing read performs ZERO PUTs (never a blind empty PUT)');
// and the mutator runs against the FRESH store, not a cached one
pio = makeIo({ profiles: [{ id: 'p1' }] });
pres = await saveProfilesGuarded(pio, (fresh) => ({ profiles: fresh.profiles.concat([{ id: 'p2' }]) }));
ok(pres.ok && pio.writes[0].profiles.length === 2,
  'profiles: the mutator merges over the freshly-read store');

// ---- F7: profile fan-out over linked handles ------------------------------
const imported = new Set([K1, K2]);
const twoHandle = [
  { source: 'imessage', network_id: '+15550000001' },
  { source: 'whatsapp', network_id: '+15550000002' },
];
let fan = planHandleFanOut(twoHandle, imported, KNOWN);
eqJson(fan.keys, [K1, K2], 'fan-out: a two-handle profile produces BOTH keys');
eqJson(fan.unmatched, [], 'fan-out: nothing unmatched when both are imported');
// and both keys reach the stored map in one write, so the next uplink pass
// tombstones both mirrored rows
io = makeIo({ status: 'ok', overrides: {} });
res = await applyContactOverrides(io, fan.keys.map((k) => [k, 'private']));
ok(res.ok && io.writes.length === 1
   && io.writes[0][K1] === 'private' && io.writes[0][K2] === 'private',
'fan-out: both keys are written as private in one PUT');

// a handle that is not in the imported book mints NO key and is reported
fan = planHandleFanOut(twoHandle.concat([{ source: 'imessage', network_id: '+15559999999' }]),
  imported, KNOWN);
ok(fan.keys.length === 2 && fan.unmatched.length === 1
   && /not in your imported contacts/.test(fan.unmatched[0].reason),
'fan-out: an unimported handle is refused visibly, never minted');
// an unknown source likewise
fan = planHandleFanOut([{ source: 'mystery', network_id: '+15550000001' }], imported, KNOWN);
ok(fan.keys.length === 0 && fan.unmatched[0].reason === 'unknown source',
  'fan-out: an unknown source is refused visibly');
// junk handles never crash and never mint
fan = planHandleFanOut([null, 5, {}, { source: '__proto__', network_id: 'x' }], imported, KNOWN);
ok(fan.keys.length === 0 && fan.unmatched.length === 4, 'fan-out: junk handles mint nothing');

// ---- the imported-contacts response is validated before it is keyed on ---
const rows = validImportedContacts({
  contacts: [
    { source: 'imessage', network_id: '+15550000001', display_name: 'Ann' },
    { source: 'imessage', network_id: '+15550000001', display_name: 'dupe' },
    { source: 'mystery', network_id: '+15550000003', display_name: 'X' },
    { source: 'imessage', network_id: 'not-a-handle' },
    { source: 'imessage', network_id: 'a@b.example', display_name: 5 },
    null, 5, [],
  ],
}, KNOWN);
eqJson(rows.map((r) => r.key), [K1, 'imessage|a@b.example'],
  'imported: only known-source, valid, deduped handles survive');
ok(rows[1].display_name === '', 'imported: a non-string display name becomes empty');
eqJson(validImportedContacts(null, KNOWN), [], 'imported: junk body -> []');
eqJson(validImportedContacts({ contacts: 'nope' }, KNOWN), [], 'imported: non-array contacts -> []');

console.log((fail ? 'FAIL' : 'ok') + ' contact_overrides: %d passed, %d failed', pass, fail);
if (fail) {
  for (const f of failures) console.error('  - ' + f);
  process.exit(1);
}
