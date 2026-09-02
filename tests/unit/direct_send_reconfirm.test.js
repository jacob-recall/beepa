// Plain-node test for apps/user/consent.js's PURE master-identity re-confirm
// helpers (direct-share-level plan D2.11/F12, S2 acceptance): a
// com.jkali.direct_send_suspended account-data content normalizes into the
// affordance the UI renders, and the ack the teammate's confirm writes
// mirrors that SAME identity tuple verbatim (so S3's uplink can compare it
// byte-for-byte before resuming auto-send).
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
//        node tests/unit/direct_send_reconfirm.test.js

import { suspensionAffordance, directSendAckContent } from '../../apps/user/consent.js';

let pass = 0;
let fail = 0;
const failures = [];
function ok(cond, label) { if (cond) { pass++; } else { fail++; failures.push(label); } }

// ---- suspension content -> affordance data -----------------------------------
const full = { master_hs: 'master.example', master_user: '@svc:master.example', manager_mxid: '@mgr:master.example', ts: 1700000000 };
const a = suspensionAffordance(full);
ok(a !== null, 'a full tuple normalizes');
ok(a && a.master_hs === 'master.example', 'master_hs carried through');
ok(a && a.master_user === '@svc:master.example', 'master_user carried through');
ok(a && a.manager_mxid === '@mgr:master.example', 'manager_mxid carried through');
ok(a && a.ts === 1700000000, 'ts carried through');

// ---- missing/malformed content -> null (never a broken confirm) -------------
ok(suspensionAffordance(null) === null, 'null content -> null');
ok(suspensionAffordance(undefined) === null, 'undefined content -> null');
ok(suspensionAffordance('junk') === null, 'string content -> null');
ok(suspensionAffordance([]) === null, 'array content -> null');
ok(suspensionAffordance({}) === null, 'empty object -> null');
const missing = [
  ['missing master_hs',   { master_user: 'u', manager_mxid: 'm', ts: 1 }],
  ['missing master_user', { master_hs: 'h', manager_mxid: 'm', ts: 1 }],
  ['missing manager_mxid', { master_hs: 'h', master_user: 'u', ts: 1 }],
  ['missing ts',           { master_hs: 'h', master_user: 'u', manager_mxid: 'm' }],
  ['non-string master_hs', { master_hs: 1, master_user: 'u', manager_mxid: 'm', ts: 1 }],
  ['non-number ts',        { master_hs: 'h', master_user: 'u', manager_mxid: 'm', ts: '1' }],
  ['empty master_hs',      { master_hs: '', master_user: 'u', manager_mxid: 'm', ts: 1 }],
  ['NaN ts',                { master_hs: 'h', master_user: 'u', manager_mxid: 'm', ts: NaN }],
];
for (const [label, content] of missing) {
  ok(suspensionAffordance(content) === null, 'rejects: ' + label);
}

// ---- ack content mirrors the affordance tuple, verbatim ----------------------
const ack = directSendAckContent(full);
ok(ack !== null, 'ack content built from a valid affordance');
ok(JSON.stringify(ack) === JSON.stringify({
  master_hs: 'master.example', master_user: '@svc:master.example', manager_mxid: '@mgr:master.example', ts: 1700000000,
}), 'ack content is exactly the same four fields, same values, as the suspension tuple');

// directSendAckContent accepts a raw (unnormalized) content too, going
// through the same normalization gate — never writes a malformed ack.
ok(directSendAckContent(full) !== null, 'ack accepts raw suspension content directly');
ok(directSendAckContent({}) === null, 'ack refuses to build from malformed content');
ok(directSendAckContent(null) === null, 'ack refuses null');

// Extra fields on the suspension content must NOT leak into the ack (the ack
// is exactly the four-field identity tuple, nothing else).
const withExtra = Object.assign({}, full, { extra: 'should not appear' });
const ackFromExtra = directSendAckContent(withExtra);
ok(ackFromExtra && !('extra' in ackFromExtra), 'ack content carries no fields beyond the identity tuple');

if (fail) {
  console.error('direct_send_reconfirm.test.js: ' + fail + ' FAILED, ' + pass + ' passed');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
} else {
  console.log('direct_send_reconfirm.test.js: all ' + pass + ' checks passed');
}
