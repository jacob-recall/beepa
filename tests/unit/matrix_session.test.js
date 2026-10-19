import assert from 'node:assert/strict';
import { api, setOnUnauthorized } from '../../shared/matrix/client.js';
import { S } from '../../shared/state.js';

const originalFetch = globalThis.fetch;
const requests = [];
let signedOut = 0;
globalThis.fetch = (_url, options) => new Promise(resolve => requests.push({ options, resolve }));
setOnUnauthorized(() => { signedOut++; S.token = null; });
try {
  S.token = 'old-synthetic-session';
  const oldRequest = api('GET', '/synthetic');
  assert.equal(requests[0].options.headers.Authorization, 'Bearer old-synthetic-session');
  S.token = 'new-synthetic-session';
  requests[0].resolve({ status: 401 });
  await assert.rejects(oldRequest, /session expired/);
  assert.equal(signedOut, 0);
  assert.equal(S.token, 'new-synthetic-session', 'stale 401 cannot retire a newer session');

  const currentRequest = api('GET', '/synthetic');
  requests[1].resolve({ status: 401 });
  await assert.rejects(currentRequest, /session expired/);
  assert.equal(signedOut, 1, 'current unauthorized session still signs out');
  assert.equal(S.token, null);
} finally {
  globalThis.fetch = originalFetch;
  setOnUnauthorized(() => {});
  S.token = null;
}
console.log('matrix_session: stale 401 isolation and current-session expiration pass');
