// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { sanitize } from '../ui/el.js';
import { S } from '../state.js';

const HS = 'http://127.0.0.1:8008';
const SERVER_NAME = 'localhost';
const CHATS_URL = 'http://127.0.0.1:8009';

// ---- validation regexes ----
// Element route target: a room id that is URL-fragment-safe (no #,?,%,\,space,
// controls). Validate-then-concatenate the RAW id (D-4: do NOT encode — the
// charset is fragment-safe and encoding breaks Element's route parser).
const ROOMID_RE = /^![A-Za-z0-9._=/+-]+:localhost$/;
const MXC_RE = /^mxc:\/\/([A-Za-z0-9.\-:]+)\/([A-Za-z0-9_-]+)$/;


// The transport's 401 handler. The monolith called forgetSession() directly;
// as a shared module it exposes a hook the app registers (setOnUnauthorized),
// so shared/ never imports from an app. Behavior is identical.
let onUnauthorized = () => {};
function setOnUnauthorized(fn) { onUnauthorized = fn; }

// ---- API ----
async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (S.token) headers['Authorization'] = 'Bearer ' + S.token;
  const res = await fetch(HS + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 401) { onUnauthorized(); throw new Error('Signed out: session expired.'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(sanitize(data.error || ('HTTP ' + res.status)));
  return data;
}

export { HS, SERVER_NAME, CHATS_URL, ROOMID_RE, MXC_RE, api, onUnauthorized, setOnUnauthorized };
