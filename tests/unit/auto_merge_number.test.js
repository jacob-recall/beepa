// tests/unit/auto_merge_number.test.js
// Pure, plain-node test for shared/model/contacts.js autoMergeByNumber() — the
// one approved auto-merge (by resolved phone number). Run:
//   docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/auto_merge_number.test.js
import { autoMergeByNumber, normalizeProfiles } from '../../shared/model/contacts.js';

let pass = 0;
function ok(cond, m) { if (!cond) throw new Error('FAIL: ' + m); pass++; }
function profOf(res, rid) {
  return res.profiles.find((p) => p.roomIds.indexOf(rid) !== -1) || null;
}

// Room ids must satisfy ROOMID_RE (…:localhost).
const A = '!a:localhost', B = '!b:localhost', C = '!c:localhost', D = '!d:localhost';

// 1) two ungrouped rooms sharing a number -> one NEW profile, share 'inherit',
//    holding both room ids.
{
  const res = autoMergeByNumber({ profiles: [] }, { [A]: '+15551230001', [B]: '+15551230001' },
    { nameOf: () => 'Dana' });
  ok(res.changed === true, '1: changed');
  ok(res.profiles.length === 1, '1: exactly one profile created');
  const p = res.profiles[0];
  ok(p.share === 'inherit', '1: auto profile share is inherit, got ' + p.share);
  ok(p.displayName === 'Dana', '1: displayName seeded from nameOf');
  ok(p.roomIds.indexOf(A) !== -1 && p.roomIds.indexOf(B) !== -1, '1: both rooms linked');
}

// 2) one already-grouped + one ungrouped sharing a number -> the ungrouped room
//    is linked into the EXISTING profile; that profile's share is untouched.
{
  const start = { profiles: [
    { id: 'cp_x', displayName: 'X', roomIds: [A], share: 'share' },
  ] };
  const res = autoMergeByNumber(start, { [A]: '+15551230002', [B]: '+15551230002' });
  ok(res.changed === true, '2: changed');
  ok(res.profiles.length === 1, '2: no new profile created');
  const px = res.profiles.find((p) => p.id === 'cp_x');
  ok(px && px.roomIds.indexOf(B) !== -1, '2: ungrouped room linked into existing profile');
  ok(px.roomIds.indexOf(A) !== -1, '2: original room retained');
  ok(px.share === 'share', '2: existing profile share unchanged (still share)');
}

// 3) two rooms already in TWO DIFFERENT profiles sharing a number -> unchanged;
//    the two profiles are NEVER merged.
{
  const start = { profiles: [
    { id: 'cp_1', displayName: 'One', roomIds: [A], share: 'private' },
    { id: 'cp_2', displayName: 'Two', roomIds: [B], share: 'share' },
  ] };
  const res = autoMergeByNumber(start, { [A]: '+15551230003', [B]: '+15551230003' });
  ok(res.changed === false, '3: no change when rooms span two profiles');
  ok(res.profiles.length === 2, '3: still two separate profiles');
  ok(profOf(res, A).id === 'cp_1' && profOf(res, B).id === 'cp_2', '3: rooms stay in their own profiles');
}

// 4) a number with a single room -> unchanged (nothing to merge).
{
  const res = autoMergeByNumber({ profiles: [] },
    { [A]: '+15551230004', [C]: '+15559990000' /* also lone */ });
  ok(res.changed === false, '4: lone-room numbers cause no change');
  ok(res.profiles.length === 0, '4: no profiles created');
}

// 5) idempotency: running twice yields changed:false the second time.
{
  const first = autoMergeByNumber({ profiles: [] }, { [A]: '+15551230005', [B]: '+15551230005' });
  ok(first.changed === true, '5: first run merges');
  const second = autoMergeByNumber({ profiles: first.profiles },
    { [A]: '+15551230005', [B]: '+15551230005' });
  ok(second.changed === false, '5: second run is a no-op');
  ok(second.profiles.length === 1, '5: still one profile after re-run');
}

// 6) an auto-created profile is NEVER created with share 'share' — even if the
//    caller were somehow to pass a share hint, autoMerge only ever uses inherit.
{
  const res = autoMergeByNumber({ profiles: [] }, { [A]: '+15551230006', [B]: '+15551230006', [D]: '+15551230006' });
  ok(res.changed === true, '6: merged');
  for (const p of res.profiles) ok(p.share === 'inherit', '6: auto profile never share, got ' + p.share);
}

// Sanity: output is normalized (a room belongs to at most one profile).
{
  const res = autoMergeByNumber({ profiles: [] }, { [A]: '+15551230007', [B]: '+15551230007' });
  const norm = normalizeProfiles(res);
  const seen = new Set();
  for (const p of norm.profiles) for (const rid of p.roomIds) {
    ok(!seen.has(rid), 'sanity: no room in two profiles');
    seen.add(rid);
  }
}

console.log('ok auto_merge_number (' + pass + ' assertions)');
