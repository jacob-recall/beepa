// Plain-node test: the teammate app's Content-Security-Policy exists in TWO
// places that must stay byte-identical — the <meta http-equiv> in
// apps/user/index.html (defense in depth) and the add_header in the
// /apps/user/ location of views/nginx.conf (the enforced one, because
// frame-ancestors / X-Frame-Options are ignored in <meta>). This test guards
// the drift hazard: removing `frame-src http://127.0.0.1:8009` from one copy
// and not the other would silently re-open (or leave closed) a framing surface
// on only one of the two enforcement points.
//
// Run: node tests/unit/csp_parity.test.js  (also wired into tests/run.sh)
// Exits 0 on all-pass, nonzero (via process.exitCode) on any failure.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));

let pass = 0;
let fail = 0;
const failures = [];

function ok(cond, label) {
  if (cond) { pass++; } else { fail++; failures.push(label); }
}

const html = readFileSync(root + 'apps/user/index.html', 'utf8');
const nginx = readFileSync(root + 'views/nginx.conf', 'utf8');

// The app's own <meta> CSP.
const metaMatch = html.match(
  /<meta[^>]*http-equiv="Content-Security-Policy"[^>]*content="([^"]*)"/i);
ok(!!metaMatch, 'apps/user/index.html has a <meta> Content-Security-Policy');
const metaCsp = metaMatch ? metaMatch[1] : null;

// The /apps/user/ location's add_header CSP (NOT the master or catch-all one).
const userBlock = nginx.match(/location\s+\/apps\/user\/\s*\{([\s\S]*?)\n\s*\}/);
ok(!!userBlock, 'views/nginx.conf has a /apps/user/ location block');
const headerMatch = userBlock
  ? userBlock[1].match(/Content-Security-Policy\s+"([^"]*)"/)
  : null;
ok(!!headerMatch, 'the /apps/user/ location sets a Content-Security-Policy header');
const headerCsp = headerMatch ? headerMatch[1] : null;

// Neither copy may carry a frame-src to the retired Element pane (:8009).
ok(metaCsp !== null && !/frame-src/.test(metaCsp),
  '<meta> CSP no longer contains frame-src');
ok(headerCsp !== null && !/frame-src/.test(headerCsp),
  'nginx user CSP no longer contains frame-src');
ok(metaCsp !== null && metaCsp.indexOf('8009') === -1,
  '<meta> CSP no longer references :8009');

// The load-bearing assertion: the two copies are byte-identical.
ok(metaCsp !== null && headerCsp !== null && metaCsp === headerCsp,
  'the <meta> CSP and the nginx /apps/user/ CSP header are byte-identical');
if (metaCsp !== headerCsp) {
  failures.push('  meta   = ' + JSON.stringify(metaCsp));
  failures.push('  nginx  = ' + JSON.stringify(headerCsp));
}

if (fail) {
  console.error('csp_parity.test.js: ' + fail + ' FAILED, ' + pass + ' passed');
  for (const f of failures) console.error('  - ' + f);
  process.exitCode = 1;
} else {
  console.log('csp_parity.test.js: all ' + pass + ' checks passed');
}
