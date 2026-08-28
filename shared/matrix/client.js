// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { sanitize } from '../ui/el.js';
import { S } from '../state.js';

// `let` (not `const`): apps/master (PLAN-MASTER-SYNC §6.4) talks to a SEPARATE
// homeserver (its own server_name + CS API base) and calls configureMatrixBase()
// once at startup to repoint these. Each app/page gets its own module instance
// (ES modules are cached per document, not globally), so apps/user — which never
// calls configureMatrixBase — keeps these exact defaults, byte-identical to
// before this was made configurable.
let HS = 'http://127.0.0.1:8008';
let SERVER_NAME = 'localhost';
const CHATS_URL = 'http://127.0.0.1:8009';

// ---- validation regexes ----
// Element route target: a room id that is URL-fragment-safe (no #,?,%,\,space,
// controls). Validate-then-concatenate the RAW id (D-4: do NOT encode — the
// charset is fragment-safe and encoding breaks Element's route parser).
let ROOMID_RE = /^![A-Za-z0-9._=/+-]+:localhost$/;
const MXC_RE = /^mxc:\/\/([A-Za-z0-9.\-:]+)\/([A-Za-z0-9_-]+)$/;

// Repoint the transport at a different homeserver (base URL + server_name),
// recomputing ROOMID_RE for that server_name. Called once, before sign-in, by
// an app that is not the default user hub (currently only apps/master). Any
// argument left out keeps its current value.
function configureMatrixBase({ csBase, serverName } = {}) {
  if (typeof csBase === 'string' && csBase) HS = csBase;
  if (typeof serverName === 'string' && serverName) {
    SERVER_NAME = serverName;
    const esc = serverName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    ROOMID_RE = new RegExp('^![A-Za-z0-9._=/+-]+:' + esc + '$');
  }
}


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
  if (!res.ok) {
    // Additive only: the message is unchanged (sanitized, as before); `status`
    // is attached so a caller can distinguish "will never succeed" (4xx) from
    // "retry later" (429/5xx) — apps/master's join backpressure needs this.
    const err = new Error(sanitize(data.error || ('HTTP ' + res.status)));
    err.status = res.status;
    throw err;
  }
  return data;
}

export { HS, SERVER_NAME, CHATS_URL, ROOMID_RE, MXC_RE, api, onUnauthorized, setOnUnauthorized, configureMatrixBase };
