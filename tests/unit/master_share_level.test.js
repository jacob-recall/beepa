// Plain-node test for shareLevelLabel — the pure draft/proposal affordance
// label selector extracted from apps/master/main.js (direct-share-level plan,
// slice S4 / design D4).
//
// The uplink (S3) is the sole authority on whether a message is actually
// auto-sent; this function only chooses what the manager's draft button SAYS,
// off the com.jkali.share_level room-state stamp the console reads read-only
// (parseSnapshot). It must under-promise only: "Send" iff the stamp is
// EXACTLY { level: 'direct' }; every other input — 'share', an absent stamp,
// unrecognized/junk content, or non-object content (a stand-in for a read
// error upstream leaving the field null) — must yield "Propose", never "Send".
//
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
//        node tests/unit/master_share_level.test.js

import { shareLevelLabel } from '../../apps/master/main.js';

let pass = 0;
let fail = 0;
const failures = [];
function ok(cond, label) { if (cond) { pass++; } else { fail++; failures.push(label); } }

// ---- S4 acceptance cases, verbatim ----
ok(shareLevelLabel({ level: 'direct' }) === 'Send', "level 'direct' -> Send");
ok(shareLevelLabel({ level: 'share' }) === 'Propose', "level 'share' -> Propose");
ok(shareLevelLabel(null) === 'Propose', 'missing stamp (null) -> Propose');
ok(shareLevelLabel(undefined) === 'Propose', 'missing stamp (undefined) -> Propose');
ok(shareLevelLabel({ level: 'DIRECT' }) === 'Propose', 'junk content {level: "DIRECT"} -> Propose');
ok(shareLevelLabel({ level: 5 }) === 'Propose', 'junk content {level: 5} -> Propose');
ok(shareLevelLabel({}) === 'Propose', 'junk content {} -> Propose');
ok(shareLevelLabel('direct') === 'Propose', 'non-object content (string) -> Propose');
ok(shareLevelLabel(42) === 'Propose', 'non-object content (number) -> Propose');
ok(shareLevelLabel([]) === 'Propose', 'non-object content (array) -> Propose');

// ---- extra edge cases: never over-promise ----
ok(shareLevelLabel({ level: 'private' }) === 'Propose', "level 'private' -> Propose");
ok(shareLevelLabel({ level: ' direct' }) === 'Propose', 'whitespace-mangled level -> Propose');
ok(shareLevelLabel({ level: null }) === 'Propose', 'level: null -> Propose');
ok(shareLevelLabel({ level: 'direct', extra: 'junk' }) === 'Send',
  'extra unknown fields alongside a valid direct level are ignored -> Send');

if (fail) {
  console.error('master_share_level.test.js: ' + fail + ' FAILED, ' + pass + ' passed');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
} else {
  console.log('master_share_level.test.js: all ' + pass + ' checks passed');
}
