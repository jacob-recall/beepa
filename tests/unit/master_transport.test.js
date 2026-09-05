import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { masterTransport } from '../../apps/master/transport.js';

for (const origin of ['https://owner.private.ts.net', 'https://master.example:8444']) {
  const value = masterTransport(new URL(origin), { serverName: 'matrix.example' });
  assert.equal(value.csBase, origin);
  assert.equal(value.enrollBase, origin);
  assert.equal(value.serverName, 'matrix.example');
  assert.equal(value.allowLocalBootstrap, false);
}
for (const host of ['127.0.0.1', 'localhost', '[::1]']) {
  const value = masterTransport(new URL('http://' + host + ':8011'));
  assert.equal(value.csBase, 'http://127.0.0.1:8018');
  assert.equal(value.enrollBase, 'http://127.0.0.1:8019');
  assert.equal(value.allowLocalBootstrap, true);
}
assert.equal(masterTransport(new URL('http://127.0.0.1:8017')).allowLocalBootstrap, false);
assert.equal(masterTransport(new URL('http://127.0.0.1:8017')).csBase, 'http://127.0.0.1:8017');
assert.equal(masterTransport(new URL('https://owner.private.ts.net'), { serverName: '<script>' }).serverName, 'master');
const main = readFileSync(new URL('../../apps/master/main.js', import.meta.url), 'utf8');
assert.match(main, /if \(MASTER_TRANSPORT\.allowLocalBootstrap\) try \{\s*const r = await fetch\('session\.local\.json'/);
assert.match(main, /configureMatrixBase\(\{ csBase: MASTER_BASE, serverName: MASTER_TRANSPORT\.serverName \}\)/);
console.log('master_transport: remote same-origin, legacy local transport and bootstrap isolation pass');
